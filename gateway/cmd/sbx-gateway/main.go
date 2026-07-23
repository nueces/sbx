package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/nueces/sbx/gateway/internal/capability"
	"github.com/nueces/sbx/gateway/internal/config"
	gatewaynet "github.com/nueces/sbx/gateway/internal/netstack"
	"github.com/nueces/sbx/gateway/internal/qemustream"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
)

func readConfig(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if info.Mode()&os.ModeSymlink != 0 || info.Size() > 1<<20 {
		return nil, errors.New("unsafe config file")
	}
	return os.ReadFile(path)
}

type stringFlags []string

func (values *stringFlags) String() string { return fmt.Sprint([]string(*values)) }
func (values *stringFlags) Set(value string) error {
	*values = append(*values, value)
	return nil
}

func validateIdentity(cfg config.Config, vmID, generation, digest string) error {
	if vmID == "" || generation == "" || digest == "" || cfg.VMID != vmID || cfg.Generation != generation || cfg.Digest != digest {
		return errors.New("worker identity or config digest mismatch")
	}
	return nil
}

func run() error {
	socket := flag.String("socket", "", "controller-owned QEMU Unix socket path")
	configPath := flag.String("config", "", "immutable worker JSON config")
	prepare := flag.Bool("prepare", false, "normalize and sign the config, then exit")
	vmID := flag.String("vm-id", "", "expected VM identity")
	generation := flag.String("generation", "", "expected VM generation")
	digest := flag.String("digest", "", "expected immutable config digest")
	mediatorAddress := flag.String("mediator-address", "", "per-VM mediator loopback endpoint")
	capabilityKeyFD := flag.Int("capability-key-fd", -1, "inherited mediator capability key descriptor")
	workerIncarnation := flag.String("worker-incarnation", "", "worker incarnation bound to mediator capabilities")
	var ingressFDs stringFlags
	flag.Var(&ingressFDs, "ingress-fd", "controller-owned VM/generation/rule=descriptor listener")
	flag.Parse()
	if *configPath == "" || !filepath.IsAbs(*configPath) {
		return errors.New("--config must be an absolute path")
	}
	data, err := readConfig(*configPath)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	if *prepare {
		cfg, err := config.Prepare(data)
		if err != nil {
			return fmt.Errorf("config: %w", err)
		}
		return json.NewEncoder(os.Stdout).Encode(cfg)
	}
	if *socket == "" || !filepath.IsAbs(*socket) {
		return errors.New("--socket must be an absolute path")
	}
	cfg, err := config.Parse(data)
	if err != nil {
		return fmt.Errorf("config: %w", err)
	}
	if err := validateIdentity(cfg, *vmID, *generation, *digest); err != nil {
		return err
	}
	if _, err := os.Lstat(*socket); !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("refusing existing socket path: %w", err)
	}
	listener, err := net.Listen("unix", *socket)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	defer listener.Close()
	defer os.Remove(*socket)
	if err := os.Chmod(*socket, 0o600); err != nil {
		return fmt.Errorf("secure socket: %w", err)
	}
	network, err := gatewaynet.New()
	if err != nil {
		return fmt.Errorf("netstack: %w", err)
	}
	defer network.Close()
	registeredIngress, err := gatewaynet.RegisterIngress(cfg, ingressFDs)
	if err != nil {
		return fmt.Errorf("ingress: %w", err)
	}
	totalFlows := gatewaynet.NewFlowQuota(cfg.Quotas.TCPFlows)
	ingress := gatewaynet.NewIngressForwarder(network, registeredIngress, totalFlows, cfg.Quotas.IngressFlows)
	defer ingress.Close()
	resolver := syntheticdns.New(cfg)
	var mediator *gatewaynet.Mediator
	for _, rule := range cfg.Rules {
		if rule.Protocol != "http" {
			continue
		}
		if *capabilityKeyFD < 3 || *workerIncarnation == "" || *mediatorAddress == "" {
			return errors.New("HTTP policy requires mediator endpoint, incarnation, and inherited capability key")
		}
		keyFile := os.NewFile(uintptr(*capabilityKeyFD), "mediator-capability-key")
		if keyFile == nil {
			return errors.New("invalid mediator capability key descriptor")
		}
		key, keyErr := capability.ReadKey(keyFile)
		keyFile.Close()
		if keyErr != nil {
			return keyErr
		}
		mediator, err = gatewaynet.NewMediator(*mediatorAddress, key, cfg.VMID, *workerIncarnation)
		if err != nil {
			return fmt.Errorf("mediator: %w", err)
		}
		break
	}
	dhcp, err := network.ServeDHCP()
	if err != nil {
		return fmt.Errorf("DHCP: %w", err)
	}
	defer dhcp.Close()
	dns, err := network.ServeDNS(resolver)
	if err != nil {
		return fmt.Errorf("DNS: %w", err)
	}
	defer dns.Close()
	network.ForwardTCP(cfg, resolver, mediator, totalFlows)
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{
		"status": "ready", "version": 1, "vm_id": cfg.VMID,
		"generation": cfg.Generation, "digest": cfg.Digest, "mediator": mediator != nil,
		"ingress": len(registeredIngress),
	}); err != nil {
		return fmt.Errorf("readiness: %w", err)
	}
	conn, err := listener.Accept()
	if err != nil {
		return fmt.Errorf("accept: %w", err)
	}
	defer conn.Close()
	stream := qemustream.New(conn, 600*time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	errs := make(chan error, 2)
	var outputMu sync.Mutex
	var networkReady sync.Once
	go func() {
		for {
			frame, err := stream.ReadFrame()
			if err != nil {
				errs <- err
				return
			}
			network.Inject(frame)
			networkReady.Do(func() {
				ingress.Start()
				outputMu.Lock()
				fmt.Fprintln(os.Stdout, `{"status":"network-ready"}`)
				outputMu.Unlock()
			})
		}
	}()
	go func() {
		for {
			frame := network.Read(ctx)
			if frame == nil {
				errs <- context.Canceled
				return
			}
			if err := stream.WriteFrame(frame); err != nil {
				errs <- err
				return
			}
		}
	}()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case err = <-errs:
			cancel()
			if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("data plane stopped (%+v): %w", stream.Stats(), err)
		case <-ticker.C:
			stats := stream.Stats()
			outputMu.Lock()
			fmt.Fprintf(os.Stdout, "{\"status\":\"stats\",\"frames_read\":%d,\"frames_written\":%d,\"frames_rejected\":%d}\n", stats.Read, stats.Written, stats.Rejected)
			outputMu.Unlock()
		}
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "sbx-gateway: %v\n", err)
		os.Exit(1)
	}
}
