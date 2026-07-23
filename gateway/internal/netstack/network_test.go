package netstack

import (
	"net"
	"testing"
)

func TestNetworkLifecycle(t *testing.T) {
	network, err := New()
	if err != nil {
		t.Fatal(err)
	}
	defer network.Close()
	if err := network.AddLocalAddress(net.ParseIP("198.18.0.1")); err != nil {
		t.Fatal(err)
	}
	network.Inject([]byte{0}) // malformed Ethernet input must not panic.
}
