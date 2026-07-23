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
	"time"

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
	resolver := syntheticdns.New(cfg)
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
	network.ForwardTCP(cfg, resolver)
	fmt.Fprintln(os.Stdout, `{"status":"ready"}`)
	conn, err := listener.Accept()
	if err != nil {
		return fmt.Errorf("accept: %w", err)
	}
	defer conn.Close()
	stream := qemustream.New(conn, 600*time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	errs := make(chan error, 2)
	go func() {
		for {
			frame, err := stream.ReadFrame()
			if err != nil {
				errs <- err
				return
			}
			network.Inject(frame)
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
			fmt.Fprintf(os.Stdout, "{\"frames_read\":%d,\"frames_written\":%d,\"frames_rejected\":%d}\n", stats.Read, stats.Written, stats.Rejected)
		}
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "sbx-gateway: %v\n", err)
		os.Exit(1)
	}
}
