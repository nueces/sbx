package netstack

import (
	"testing"
	"time"
)

func TestDNSRateCeilingIsBurstBoundedAndRefills(t *testing.T) {
	now := time.Unix(1, 0)
	limiter := newDNSLimiter(now)
	for range dnsQueryBurst {
		if !limiter.allow(now) {
			t.Fatal("DNS burst rejected below its ceiling")
		}
	}
	if limiter.allow(now) || limiter.denied.Load() != 1 {
		t.Fatal("DNS burst ceiling was not classified")
	}
	for range dnsQueriesPerSecond / 2 {
		if !limiter.allow(now.Add(500 * time.Millisecond)) {
			t.Fatal("DNS rate limiter did not refill deterministically")
		}
	}
	if limiter.allow(now.Add(500 * time.Millisecond)) {
		t.Fatal("DNS rate limiter exceeded 100 queries per second")
	}
}
