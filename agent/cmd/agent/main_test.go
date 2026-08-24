package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"k8s.io/client-go/rest"
)

func TestJitterStaysWithinBounds(t *testing.T) {
	// Jitter must spread reconnects without ever collapsing to zero — a delay
	// of zero would be the busy loop the backoff exists to prevent.
	low := time.Duration(float64(baseReconnectDelay) * (1 - reconnectJitter))
	high := time.Duration(float64(baseReconnectDelay) * (1 + reconnectJitter))

	var sawBelow, sawAbove bool
	for range 1000 {
		delay := jitter(baseReconnectDelay)
		if delay < low || delay > high {
			t.Fatalf("jittered delay %s outside [%s, %s]", delay, low, high)
		}
		if delay < baseReconnectDelay {
			sawBelow = true
		}
		if delay > baseReconnectDelay {
			sawAbove = true
		}
	}
	// Spread in both directions, or it is a constant offset rather than jitter.
	if !sawBelow || !sawAbove {
		t.Fatal("jitter did not spread either side of the base delay")
	}
}

func TestBackoffClimbsAndIsCapped(t *testing.T) {
	// The sequence a permanently-refused agent walks: 3s, 6s, 12s… capped, so
	// one revocation does not become an indefinite request loop.
	delay := baseReconnectDelay
	seen := []time.Duration{delay}
	for range 10 {
		delay = min(delay*2, maxReconnectDelay)
		seen = append(seen, delay)
	}

	if seen[1] != 6*time.Second || seen[2] != 12*time.Second {
		t.Fatalf("unexpected backoff climb: %v", seen[:3])
	}
	for _, delay := range seen {
		if delay > maxReconnectDelay {
			t.Fatalf("backoff exceeded the cap: %v", seen)
		}
	}
	if seen[len(seen)-1] != maxReconnectDelay {
		t.Fatalf("backoff did not reach the cap: %v", seen)
	}
}

func TestEnrolmentPortDefaultsToOneAboveTheGateway(t *testing.T) {
	// Matches what the platform does when AGENT_ENROLMENT_PORT is unset, so an
	// agent given only --gateway still finds the enrolment listener.
	cases := []struct{ gateway, configured, want string }{
		{"gateway.example:5551", "", "gateway.example:5552"},
		{"127.0.0.1:9000", "", "127.0.0.1:9001"},
		{"gateway.example:5551", "elsewhere:7000", "elsewhere:7000"},
		// Nothing sensible to derive; hand it back rather than invent a port.
		{"not-an-endpoint", "", "not-an-endpoint"},
	}
	for _, testCase := range cases {
		if got := enrolmentAddress(testCase.configured, testCase.gateway); got != testCase.want {
			t.Fatalf("enrolmentAddress(%q, %q) = %q, want %q",
				testCase.configured, testCase.gateway, got, testCase.want)
		}
	}
}

// The agent's own throttling, which is the difference between collection time
// tracking the cluster and collection time tracking a rate limiter.
//
// The property under test is that the *defaults are applied at all*. A
// rest.Config with QPS and Burst left at zero reads like "no limit" and is
// actually client-go's 5/10 — so the failure this guards against is deleting
// the call, not passing a wrong number, and a test asserting only that a
// supplied value survives would pass with the call gone.
func TestTheAgentRaisesItsOwnAPIServerLimits(t *testing.T) {
	config := &rest.Config{}

	applyRateLimits(config, defaultAPIQPS, defaultAPIBurst)

	if config.QPS <= 5 {
		t.Fatalf("QPS is %v; client-go's unset default is 5, so an investigation "+
			"of twenty reads would spend most of its time waiting", config.QPS)
	}
	if config.Burst <= 10 {
		t.Fatalf("Burst is %d; client-go's unset default is 10, below one "+
			"investigation's read count", config.Burst)
	}
}

// The structural half: the only way to get a rest.Config out of this program
// applies the limits, so there is no call site to forget. Before this the
// limits were applied in main(), which no test runs — deleting that one line
// left every assertion above passing.
func TestEveryConfigThisProgramProducesIsRateLimited(t *testing.T) {
	path := filepath.Join(t.TempDir(), "kubeconfig")
	kubeconfig := `apiVersion: v1
kind: Config
current-context: c
contexts:
  - name: c
    context: {cluster: c, user: u}
clusters:
  - name: c
    cluster: {server: "https://127.0.0.1:6443", insecure-skip-tls-verify: true}
users:
  - name: u
    user: {token: t}
`
	if err := os.WriteFile(path, []byte(kubeconfig), 0o600); err != nil {
		t.Fatal(err)
	}

	config, err := loadConfig(path, defaultAPIQPS, defaultAPIBurst)
	if err != nil {
		t.Fatalf("loadConfig: %v", err)
	}

	if config.QPS != float32(defaultAPIQPS) || config.Burst != defaultAPIBurst {
		t.Fatalf("loadConfig returned QPS=%v Burst=%d; client-go's silent "+
			"defaults of 5/10 throttle a single investigation", config.QPS, config.Burst)
	}
}

func TestASuppliedLimitIsHonoured(t *testing.T) {
	config := &rest.Config{}

	applyRateLimits(config, 12.5, 25)

	if config.QPS != 12.5 || config.Burst != 25 {
		t.Fatalf("got QPS=%v Burst=%d, want 12.5/25", config.QPS, config.Burst)
	}
}

func TestANonPositiveLimitLeavesTheDefaultAlone(t *testing.T) {
	// Assigning zero directly would mean "use client-go's default" to a reader
	// and "no limit" to nobody — the one value that must not be written through.
	config := &rest.Config{QPS: 20, Burst: 40}

	applyRateLimits(config, 0, 0)

	if config.QPS != 20 || config.Burst != 40 {
		t.Fatalf("a zero overwrote a configured limit: QPS=%v Burst=%d", config.QPS, config.Burst)
	}
}

func TestTheEnvironmentFallbacksRejectNonsense(t *testing.T) {
	t.Setenv("AGENT_API_QPS", "not-a-number")
	t.Setenv("AGENT_API_BURST", "-3")

	if got := envOrFloat("AGENT_API_QPS", defaultAPIQPS); got != defaultAPIQPS {
		t.Fatalf("envOrFloat returned %v for garbage input, want the fallback", got)
	}
	if got := envOrInt("AGENT_API_BURST", defaultAPIBurst); got != defaultAPIBurst {
		t.Fatalf("envOrInt returned %d for a negative input, want the fallback", got)
	}
}
