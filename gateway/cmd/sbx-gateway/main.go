package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/nueces/sbx/gateway/internal/capability"
	"github.com/nueces/sbx/gateway/internal/config"
	"github.com/nueces/sbx/gateway/internal/hardening"
	gatewaynet "github.com/nueces/sbx/gateway/internal/netstack"
	"github.com/nueces/sbx/gateway/internal/qemustream"
	"github.com/nueces/sbx/gateway/internal/syntheticdns"
	"golang.org/x/sys/unix"
)

func readConfig(path string, private bool) ([]byte, error) {
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), "worker-config")
	if file == nil {
		unix.Close(fd)
		return nil, errors.New("unsafe config file")
	}
	defer file.Close()
	var info unix.Stat_t
	if err := unix.Fstat(fd, &info); err != nil {
		return nil, err
	}
	if info.Mode&unix.S_IFMT != unix.S_IFREG || info.Size > 1<<20 || private && (info.Uid != uint32(os.Getuid()) || info.Mode&0o777 != 0o600) {
		return nil, errors.New("unsafe config file")
	}
	return io.ReadAll(io.LimitReader(file, 1<<20+1))
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

func incarnationProof(token []byte, vmID, generation, digest, incarnation string) string {
	mac := hmac.New(sha256.New, token)
	fmt.Fprintf(mac, "%s\x00%s\x00%s\x00%s", vmID, generation, digest, incarnation)
	return hex.EncodeToString(mac.Sum(nil))
}

func inheritedQEMUListener(fd int, path string) (net.Listener, error) {
	if fd < 3 {
		return nil, errors.New("invalid controller QEMU listener descriptor")
	}
	file := os.NewFile(uintptr(fd), "controller-qemu-listener")
	if file == nil {
		return nil, errors.New("invalid controller QEMU listener descriptor")
	}
	listener, err := net.FileListener(file)
	file.Close()
	if err != nil {
		return nil, fmt.Errorf("controller QEMU listener: %w", err)
	}
	address, ok := listener.Addr().(*net.UnixAddr)
	info, pathErr := os.Lstat(path)
	var systemInfo unix.Stat_t
	statErr := unix.Lstat(path, &systemInfo)
	if !ok || address.Name != path || pathErr != nil || statErr != nil || info.Mode()&os.ModeSymlink != 0 || info.Mode()&os.ModeSocket == 0 || info.Mode().Perm() != 0o600 || systemInfo.Uid != uint32(os.Getuid()) {
		listener.Close()
		return nil, errors.New("controller QEMU listener ownership mismatch")
	}
	return listener, nil
}

func prepareDNS(network *gatewaynet.Network, cfg config.Config, journal *os.File) (*syntheticdns.Resolver, error) {
	resolver, err := syntheticdns.NewWithJournal(cfg, cfg.Quotas.DNSMappings, journal, cfg.VMID, cfg.Generation, cfg.Digest)
	if err != nil {
		return nil, err
	}
	for _, rule := range cfg.Rules {
		if strings.Contains(rule.Host, "*") || net.ParseIP(rule.Host) != nil {
			continue
		}
		if _, err := resolver.Resolve(rule.Host); err != nil {
			return nil, fmt.Errorf("prime synthetic DNS: %w", err)
		}
	}
	// Register every restored wildcard address before cached guest traffic can arrive.
	for _, mapping := range resolver.Snapshot() {
		if err := network.AddLocalAddress(net.ParseIP(mapping.IP)); err != nil {
			return nil, fmt.Errorf("restore synthetic DNS address: %w", err)
		}
	}
	return resolver, nil
}

func run() error {
	socket := flag.String("socket", "", "controller-owned QEMU Unix socket path")
	configPath := flag.String("config", "", "immutable worker JSON config")
	prepare := flag.Bool("prepare", false, "normalize and sign the config, then exit")
	vmID := flag.String("vm-id", "", "expected VM identity")
	generation := flag.String("generation", "", "expected VM generation")
	digest := flag.String("digest", "", "expected immutable config digest")
	qemuListenerFD := flag.Int("qemu-listener-fd", -1, "inherited controller-owned QEMU listener")
	incarnationTokenFD := flag.Int("incarnation-token-fd", -1, "inherited fresh worker authentication token")
	mediatorAddress := flag.String("mediator-address", "", "per-VM mediator loopback endpoint")
	capabilityKeyFD := flag.Int("capability-key-fd", -1, "inherited mediator capability key descriptor")
	workerIncarnation := flag.String("worker-incarnation", "", "worker incarnation bound to mediator capabilities")
	supervisorLifetimeFD := flag.Int("supervisor-lifetime-fd", -1, "inherited supervisor lifetime descriptor")
	dnsStateFD := flag.Int("dns-state-fd", -1, "inherited per-generation synthetic DNS journal")
	var ingressFDs stringFlags
	flag.Var(&ingressFDs, "ingress-fd", "controller-owned VM/generation/rule=descriptor listener")
	flag.Parse()
	if *configPath == "" || !filepath.IsAbs(*configPath) {
		return errors.New("--config must be an absolute path")
	}
	data, err := readConfig(*configPath, !*prepare)
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
	if *workerIncarnation == "" || *incarnationTokenFD < 3 {
		return errors.New("worker incarnation authentication is required")
	}
	if *supervisorLifetimeFD < 3 {
		return errors.New("supervisor lifetime descriptor is required")
	}
	tokenFile := os.NewFile(uintptr(*incarnationTokenFD), "worker-incarnation-token")
	if tokenFile == nil {
		return errors.New("invalid worker incarnation token descriptor")
	}
	token, err := capability.ReadKey(tokenFile)
	tokenFile.Close()
	if err != nil {
		return fmt.Errorf("worker incarnation token: %w", err)
	}
	proof := incarnationProof(token, cfg.VMID, cfg.Generation, cfg.Digest, *workerIncarnation)
	listener, err := inheritedQEMUListener(*qemuListenerFD, *socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	lifetime := os.NewFile(uintptr(*supervisorLifetimeFD), "supervisor-lifetime")
	if lifetime == nil {
		return errors.New("invalid supervisor lifetime descriptor")
	}
	defer lifetime.Close()
	lifetimeLost := make(chan struct{})
	go func() {
		io.Copy(io.Discard, lifetime)
		close(lifetimeLost)
		listener.Close()
	}()
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
	if *dnsStateFD < 3 {
		return errors.New("synthetic DNS journal descriptor is required")
	}
	journal := os.NewFile(uintptr(*dnsStateFD), "synthetic-dns-state")
	if journal == nil {
		return errors.New("invalid synthetic DNS journal descriptor")
	}
	defer journal.Close()
	resolver, err := prepareDNS(network, cfg, journal)
	if err != nil {
		return fmt.Errorf("synthetic DNS state: %w", err)
	}
	var mediator *gatewaynet.Mediator
	allowedPorts := make([]uint16, 0, len(cfg.Rules)+1)
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
		_, rawPort, _ := net.SplitHostPort(*mediatorAddress)
		port, _ := net.LookupPort("tcp", rawPort)
		allowedPorts = append(allowedPorts, uint16(port))
		break
	}
	for _, rule := range cfg.Rules {
		if rule.Protocol != "http" {
			allowedPorts = append(allowedPorts, rule.Ports...)
		}
	}
	hardeningStatus, err := hardening.Apply(cfg.Quotas.OpenFiles-cfg.Quotas.OpenFiles/2, allowedPorts)
	if err != nil {
		return fmt.Errorf("hardening: %w", err)
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
	forwarder := network.ForwardTCP(cfg, resolver, mediator, totalFlows)
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{
		"status": "ready", "version": 1, "vm_id": cfg.VMID,
		"generation": cfg.Generation, "digest": cfg.Digest, "incarnation": *workerIncarnation,
		"incarnation_proof": proof, "mediator": mediator != nil,
		"ingress": len(registeredIngress), "hardening": hardeningStatus.Encode(),
	}); err != nil {
		return fmt.Errorf("readiness: %w", err)
	}
	conn, err := listener.Accept()
	if err != nil {
		return fmt.Errorf("accept: %w", err)
	}
	defer conn.Close()
	qemuPID, err := peerPID(conn)
	if err != nil {
		return fmt.Errorf("QEMU peer identity: %w", err)
	}
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
			var readinessErr error
			networkReady.Do(func() {
				if readinessErr = ingress.DiscardPending(); readinessErr != nil {
					return
				}
				outputMu.Lock()
				_, readinessErr = fmt.Fprintf(os.Stdout, "{\"status\":\"network-ready\",\"qemu_pid\":%d}\n", qemuPID)
				outputMu.Unlock()
				if readinessErr == nil {
					ingress.Start()
				}
			})
			if readinessErr != nil {
				errs <- fmt.Errorf("network readiness: %w", readinessErr)
				return
			}
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
		case <-lifetimeLost:
			cancel()
			return nil
		case err = <-resolver.Errors():
			return fmt.Errorf("synthetic DNS state failed: %w", err)
		case err = <-errs:
			cancel()
			if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("data plane stopped (%+v): %w", stream.Stats(), err)
		case <-ticker.C:
			stats, flows := stream.Stats(), forwarder.Stats()
			outputMu.Lock()
			fmt.Fprintf(os.Stdout, "{\"status\":\"stats\",\"frames_read\":%d,\"frames_written\":%d,\"frames_rejected\":%d,\"tcp_active\":%d,\"tcp_rejected\":%d,\"destination_rejected\":%d,\"ingress_active\":%d,\"dns_active\":%d,\"dns_rejected\":%d,\"dns_mappings\":%d,\"bytes_in\":%d,\"bytes_out\":%d}\n", stats.Read, stats.Written, stats.Rejected, totalFlows.Active(), totalFlows.Rejected(), flows.PerDestinationRejected, ingress.Active(), dns.Active(), dns.Rejected(), resolver.Len(), flows.Uploaded, flows.Downloaded)
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
