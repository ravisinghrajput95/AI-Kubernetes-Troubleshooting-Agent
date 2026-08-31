package transport

import (
	"context"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"log/slog"
	"math/big"
	"sync"
	"testing"
	"time"

	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/identity"
)

// The platform's CA backdates a leaf's NotBefore to tolerate clock skew, and
// the agent's renewal point is two thirds of NotBefore → NotAfter. For a short
// enough certificate the renewal point is therefore already in the past when
// the certificate is issued, and the agent has no way to tell: a certificate
// records when it became valid, never when it was issued.
//
// Found by running an agent for three minutes against a ninety-second
// certificate, not by reading the arithmetic. It renewed twelve times a minute
// and would have gone on doing so — one CA signature and one
// `agent_certificates` row per tick, per agent, indefinitely.
const skewBackdate = 5 * time.Minute

// held builds a Holder over a certificate issued `at`, exactly as the platform
// would issue it: valid from `at - skewBackdate` to `at + life`.
func held(t *testing.T, at time.Time, life time.Duration) *identity.Holder {
	t.Helper()

	key, err := identity.GenerateKey()
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "soak-cluster"},
		NotBefore:    at.Add(-skewBackdate),
		NotAfter:     at.Add(life),
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create certificate: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM, err := identity.EncodeKey(key)
	if err != nil {
		t.Fatalf("encode key: %v", err)
	}
	material, err := identity.NewMaterial(certPEM, keyPEM, certPEM)
	if err != nil {
		t.Fatalf("material: %v", err)
	}
	return identity.NewHolder(material)
}

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// TestRenewalIsRatedByCertificateLifeNotByTheCheckInterval is the regression
// for that soak finding.
//
// It drives the scheduling loop itself rather than the arithmetic underneath
// it. `MinRenewalInterval` being right is not the property — the property is
// that the loop consults it, and a test of the helper alone survives deleting
// the call.
func TestRenewalIsRatedByCertificateLifeNotByTheCheckInterval(t *testing.T) {
	const (
		life          = 90 * time.Second
		checkEvery    = 5 * time.Second
		simulatedSpan = 30 * time.Minute
	)

	base := time.Date(2026, 8, 31, 10, 0, 0, 0, time.UTC)
	holder := held(t, base, life)

	// Renewal is already overdue at issue, which is the whole premise. If this
	// stops being true the test below passes for the wrong reason.
	if !holder.DueForRenewal(base) {
		t.Fatalf("a %s certificate backdated %s is not overdue at issue; premise gone", life, skewBackdate)
	}

	var mutex sync.Mutex
	clock := base
	renewals := 0

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		keepFresh(ctx, holder, time.Millisecond, func() time.Time {
			mutex.Lock()
			defer mutex.Unlock()
			// Each look at the clock advances it by one check interval, so the
			// loop sees exactly the ticks a real agent would over the span.
			clock = clock.Add(checkEvery)
			if clock.Sub(base) > simulatedSpan {
				cancel()
			}
			return clock
		}, func() error {
			mutex.Lock()
			defer mutex.Unlock()
			renewals++
			// The platform hands back another certificate of the same short
			// life, which is what makes this a loop rather than one mistake.
			holder.Replace(held(t, clock, life).Material())
			return nil
		}, quiet())
	}()

	select {
	case <-done:
	case <-time.After(30 * time.Second):
		t.Fatal("the renewal loop did not finish")
	}

	mutex.Lock()
	defer mutex.Unlock()

	ticks := int(simulatedSpan / checkEvery)
	// Unbounded, the agent renews on every tick: 360 times over half an hour.
	// Bounded by a third of the certificate's remaining life, it renews at
	// most once per 30s: 60 times. The floor matters as much as the ceiling —
	// an agent that stopped renewing would let the certificate expire.
	ceiling := int(simulatedSpan/(life/3)) + 2
	if renewals > ceiling {
		t.Fatalf("renewed %d times over %s (%d ticks); the rate is bound to the check interval, not to certificate life (ceiling %d)", renewals, simulatedSpan, ticks, ceiling)
	}
	if renewals < 2 {
		t.Fatalf("renewed %d times over %s; renewal stopped happening at all", renewals, simulatedSpan)
	}
}

// A healthy certificate must be unaffected: the bound is a floor under a
// pathological case, not a change to the schedule anyone deploys.
func TestTheRateBoundDoesNotDelayANormalRenewal(t *testing.T) {
	base := time.Date(2026, 8, 31, 10, 0, 0, 0, time.UTC)
	holder := held(t, base, 90*24*time.Hour)

	// Two thirds of a 90-day life, less the backdate's pull, is ~60 days.
	if holder.DueForRenewal(base.Add(59 * 24 * time.Hour)) {
		t.Fatal("due after 59 days of a 90-day certificate")
	}
	if !holder.DueForRenewal(base.Add(61 * 24 * time.Hour)) {
		t.Fatal("not due after 61 days of a 90-day certificate")
	}
	// And the gap the bound would impose is shorter than the gap the schedule
	// already leaves, so it can never be what decides.
	at := base.Add(61 * 24 * time.Hour)
	if got := holder.MinRenewalInterval(at); got > 30*24*time.Hour {
		t.Fatalf("minimum interval %s exceeds the third of life left at renewal", got)
	}
}
