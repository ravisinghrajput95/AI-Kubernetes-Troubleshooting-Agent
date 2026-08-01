package main

import (
	"testing"
	"time"
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
