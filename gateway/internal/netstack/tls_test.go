package netstack

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"io"
	"math/big"
	"net"
	"testing"
	"time"

	"github.com/nueces/sbx/gateway/internal/config"
)

func certificate(t *testing.T, template, parent *x509.Certificate, public any, signer *ecdsa.PrivateKey) []byte {
	t.Helper()
	der, err := x509.CreateCertificate(rand.Reader, template, parent, public, signer)
	if err != nil {
		t.Fatal(err)
	}
	return der
}

func passthroughPKI(t *testing.T) (*x509.CertPool, tls.Certificate, tls.Certificate, [32]byte) {
	t.Helper()
	now := time.Now()
	rootKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	root := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "sbx passthrough root"},
		NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), IsCA: true,
		BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	rootDER := certificate(t, root, root, &rootKey.PublicKey, rootKey)
	root, _ = x509.ParseCertificate(rootDER)
	issue := func(serial int64, name string, usage x509.ExtKeyUsage) tls.Certificate {
		key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		template := &x509.Certificate{
			SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: name},
			NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour),
			KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage},
		}
		if usage == x509.ExtKeyUsageServerAuth {
			template.DNSNames = []string{"localhost"}
		}
		der := certificate(t, template, root, &key.PublicKey, rootKey)
		return tls.Certificate{Certificate: [][]byte{der, rootDER}, PrivateKey: key}
	}
	server := issue(2, "localhost", x509.ExtKeyUsageServerAuth)
	client := issue(3, "guest", x509.ExtKeyUsageClientAuth)
	roots := x509.NewCertPool()
	roots.AddCert(root)
	return roots, server, client, sha256.Sum256(server.Certificate[0])
}

func TestExactTLSPassthroughPreservesPinnedMTLSAndPayload(t *testing.T) {
	roots, serverCertificate, clientCertificate, pin := passthroughPKI(t)
	listener, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{
		Certificates: []tls.Certificate{serverCertificate}, ClientAuth: tls.RequireAndVerifyClientCert,
		ClientCAs: roots, MinVersion: tls.VersionTLS12,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	payload := []byte("GET / HTTP/1.1\r\nAuthorization: SBX_SECRET_fake\r\n\r\n")
	seen := make(chan []byte, 1)
	go func() {
		conn, acceptErr := listener.Accept()
		if acceptErr != nil {
			seen <- nil
			return
		}
		defer conn.Close()
		body := make([]byte, len(payload))
		_, _ = io.ReadFull(conn, body)
		seen <- body
		_, _ = conn.Write([]byte("ok"))
	}()

	port := uint16(listener.Addr().(*net.TCPAddr).Port)
	rule := config.Rule{ID: "pinned", Protocol: "tls", Host: "localhost", Ports: []uint16{port}, AllowRanges: []string{"127.0.0.0/8"}}
	policy := config.Config{Rules: []config.Rule{rule}}
	if matched, ok := policy.Match("tls", "localhost", port); !ok || matched.ID != rule.ID {
		t.Fatal("exact passthrough rule did not match")
	}
	if _, ok := policy.Match("tls", "localhost", port+1); ok {
		t.Fatal("passthrough allowed the wrong port")
	}

	guest, gateway := net.Pipe()
	forwarder := &TCPForwarder{}
	result := make(chan error, 1)
	go func() {
		_, _, err := forwarder.passthrough(context.Background(), gateway, rule, "localhost", port)
		result <- err
	}()
	client := tls.Client(guest, &tls.Config{
		Certificates: []tls.Certificate{clientCertificate}, RootCAs: roots, ServerName: "localhost",
		MinVersion: tls.VersionTLS12,
		VerifyConnection: func(state tls.ConnectionState) error {
			if sha256.Sum256(state.PeerCertificates[0].Raw) != pin {
				return x509.CertificateInvalidError{Cert: state.PeerCertificates[0], Reason: x509.NotAuthorizedToSign}
			}
			return nil
		},
	})
	if err := client.Handshake(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Write(payload); err != nil {
		t.Fatal(err)
	}
	response := make([]byte, 2)
	if _, err := io.ReadFull(client, response); err != nil || string(response) != "ok" {
		t.Fatalf("response %q: %v", response, err)
	}
	client.Close()
	if got := <-seen; string(got) != string(payload) {
		t.Fatalf("passthrough inspected or mutated payload: %q", got)
	}
	if err := <-result; err != nil {
		t.Fatal(err)
	}
}

func TestTLSPassthroughRejectsWrongAndMissingSNI(t *testing.T) {
	for _, serverName := range []string{"other.example", ""} {
		t.Run(serverName, func(t *testing.T) {
			guest, gateway := net.Pipe()
			result := make(chan error, 1)
			go func() {
				defer gateway.Close()
				forwarder := &TCPForwarder{}
				_, _, err := forwarder.passthrough(
					context.Background(), gateway,
					config.Rule{Protocol: "tls", AllowRanges: []string{"127.0.0.0/8"}},
					"localhost", 443,
				)
				result <- err
			}()
			client := tls.Client(guest, &tls.Config{InsecureSkipVerify: true, ServerName: serverName}) // local rejection fixture
			if err := client.Handshake(); err == nil {
				t.Fatal("TLS passthrough accepted invalid SNI")
			}
			client.Close()
			if err := <-result; err == nil {
				t.Fatal("TLS passthrough did not fail closed")
			}
		})
	}
}
