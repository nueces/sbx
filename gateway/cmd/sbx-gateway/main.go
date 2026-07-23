package main

import (
	"context"
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

func loadConfig(path string) (config.Config, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return config.Config{}, err
	}
	if info.Mode()&os.ModeSymlink != 0 || info.Size() > 1<<20 {
		return config.Config{}, errors.New("unsafe config file")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return config.Config{}, err
	}
	return config.Parse(data)
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
	vmID := flag.String("vm-id", "", "expected VM identity")
	generation := flag.String("generation", "", "expected VM generation")
	digest := flag.String("digest", "", "expected immutable config digest")
	flag.Parse()
	if *socket == "" || !filepath.IsAbs(*socket) {
		return errors.New("--socket must be an absolute path")
	}
	if *configPath == "" || !filepath.IsAbs(*configPath) {
		return errors.New("--config must be an absolute path")
	}
	cfg, err := loadConfig(*configPath)
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
	stream := qemustream.New(conn, 30*time.Second)
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
	err = <-errs
	cancel()
	if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return nil
	}
	return err
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "sbx-gateway: %v\n", err)
		os.Exit(1)
	}
}
