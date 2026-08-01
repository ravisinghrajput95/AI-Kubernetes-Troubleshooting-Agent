package identity

import (
	"crypto/ecdsa"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// issue makes a self-signed leaf with a chosen validity window, standing in for
// one the platform would have signed.
func issue(t *testing.T, notBefore time.Time, life time.Duration) ([]byte, []byte, *ecdsa.PrivateKey) {
	t.Helper()

	key, err := GenerateKey()
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "test-cluster"},
		NotBefore:    notBefore,
		NotAfter:     notBefore.Add(life),
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create certificate: %v", err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM, err := EncodeKey(key)
	if err != nil {
		t.Fatalf("encode key: %v", err)
	}
	return certPEM, keyPEM, key
}

func TestTheCsrCarriesNoPrivateKey(t *testing.T) {
	// The property the whole registration flow rests on: what crosses the wire
	// proves possession of a key without disclosing it.
	keyPEM, csrPEM, err := NewKeyAndRequest()
	if err != nil {
		t.Fatalf("new key and request: %v", err)
	}

	if !strings.Contains(string(csrPEM), "CERTIFICATE REQUEST") {
		t.Fatalf("expected a CSR, got: %s", csrPEM)
	}
	if strings.Contains(string(csrPEM), "PRIVATE KEY") {
		t.Fatal("the CSR contains private key material")
	}

	block, _ := pem.Decode(csrPEM)
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatalf("parse CSR: %v", err)
	}
	// Proof of possession: the platform refuses a CSR that fails this.
	if err := csr.CheckSignature(); err != nil {
		t.Fatalf("the CSR is not correctly self-signed: %v", err)
	}

	key, err := DecodeKey(keyPEM)
	if err != nil {
		t.Fatalf("decode key: %v", err)
	}
	public, ok := csr.PublicKey.(*ecdsa.PublicKey)
	if !ok || !public.Equal(&key.PublicKey) {
		t.Fatal("the CSR asks for a different key than the one generated")
	}
}

func TestTheKeyIsP256(t *testing.T) {
	// The platform's CA refuses anything else, so a mismatch here would be a
	// registration failure discovered in production.
	key, err := GenerateKey()
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	if name := key.Curve.Params().Name; name != "P-256" {
		t.Fatalf("expected P-256, got %s", name)
	}
}

func TestRenewalHappensAtTwoThirdsOfLife(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	life := 90 * 24 * time.Hour
	certPEM, _, _ := issue(t, start, life)

	leaf, err := ParseCertificate(certPEM)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}

	want := start.Add(60 * 24 * time.Hour)
	if got := RenewAt(leaf); !got.Equal(want) {
		t.Fatalf("renew at %s, want %s", got, want)
	}

	cases := []struct {
		name string
		when time.Time
		due  bool
	}{
		{"fresh", start.Add(time.Hour), false},
		{"just before two thirds", want.Add(-time.Minute), false},
		{"exactly two thirds", want, true},
		{"after two thirds", want.Add(time.Hour), true},
		// The overlap window: still due, and still valid, for a third of its
		// life. That is what makes a failed renewal survivable.
		{"deep into the overlap", start.Add(85 * 24 * time.Hour), true},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := DueForRenewal(leaf, testCase.when); got != testCase.due {
				t.Fatalf("due=%v, want %v", got, testCase.due)
			}
		})
	}
}

func TestRenewalScalesWithCertificateLife(t *testing.T) {
	// A short-lived certificate must renew proportionally sooner without
	// anything being reconfigured — which is what makes the integration suite
	// able to observe a rotation in seconds.
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	certPEM, _, _ := issue(t, start, 30*time.Second)

	leaf, err := ParseCertificate(certPEM)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got := RenewAt(leaf); !got.Equal(start.Add(20 * time.Second)) {
		t.Fatalf("renew at %s, want %s", got, start.Add(20*time.Second))
	}
}

func TestDegradationOnlySpeaksWhenRenewalIsFailing(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	life := 90 * 24 * time.Hour
	certPEM, _, _ := issue(t, start, life)
	leaf, _ := ParseCertificate(certPEM)

	// Past the renewal point but with plenty of overlap left: nothing to say.
	if message := Degradation(leaf, start.Add(65*24*time.Hour)); message != "" {
		t.Fatalf("expected silence during the overlap window, got %q", message)
	}
	// Inside the last tenth: renewal has been failing for weeks.
	if message := Degradation(leaf, start.Add(86*24*time.Hour)); message == "" {
		t.Fatal("expected a degradation message near expiry")
	}
	if message := Degradation(leaf, start.Add(life+time.Hour)); !strings.Contains(message, "expired") {
		t.Fatalf("expected an expiry message, got %q", message)
	}
	if message := Degradation(nil, start); message != "" {
		t.Fatalf("expected silence with no certificate, got %q", message)
	}
}

func TestTheStoreRoundTrips(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(dir)

	if store.Exists() {
		t.Fatal("a fresh directory should hold no identity")
	}

	certPEM, keyPEM, _ := issue(t, time.Now().Add(-time.Hour), 90*24*time.Hour)
	caPEM, _, _ := issue(t, time.Now().Add(-time.Hour), 3650*24*time.Hour)

	if err := store.Save(certPEM, keyPEM, caPEM); err != nil {
		t.Fatalf("save: %v", err)
	}
	if !store.Exists() {
		t.Fatal("the identity should be present after saving")
	}

	material, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if material.Leaf.Subject.CommonName != "test-cluster" {
		t.Fatalf("unexpected subject %q", material.Leaf.Subject.CommonName)
	}
	if _, err := material.RootPool(); err != nil {
		t.Fatalf("root pool: %v", err)
	}
}

func TestTheStoredKeyIsNotWorldReadable(t *testing.T) {
	dir := t.TempDir()
	certPEM, keyPEM, _ := issue(t, time.Now(), time.Hour)

	if err := NewStore(dir).Save(certPEM, keyPEM, certPEM); err != nil {
		t.Fatalf("save: %v", err)
	}

	info, err := os.Stat(filepath.Join(dir, keyFile))
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		t.Fatalf("the private key is readable by others: %o", mode)
	}
}

func TestAMismatchedCertificateAndKeyAreRefused(t *testing.T) {
	certPEM, _, _ := issue(t, time.Now(), time.Hour)
	_, otherKeyPEM, _ := issue(t, time.Now(), time.Hour)

	if _, err := NewMaterial(certPEM, otherKeyPEM, certPEM); err == nil {
		t.Fatal("expected a certificate and an unrelated key to be refused")
	}
}

func TestRotationSwapsWithoutDisturbingWhatIsHeld(t *testing.T) {
	// The mechanism that makes rotation invisible to a live stream: the TLS
	// config asks the holder per handshake, so replacing the material changes
	// what the *next* connection presents and nothing else.
	first, firstKey, _ := issue(t, time.Now().Add(-time.Hour), 90*24*time.Hour)
	material, err := NewMaterial(first, firstKey, first)
	if err != nil {
		t.Fatalf("material: %v", err)
	}
	holder := NewHolder(material)

	config, err := holder.ClientTLS("gateway.example")
	if err != nil {
		t.Fatalf("client TLS: %v", err)
	}

	before, err := config.GetClientCertificate(nil)
	if err != nil {
		t.Fatalf("get client certificate: %v", err)
	}
	if before.Leaf.SerialNumber.Cmp(material.Leaf.SerialNumber) != 0 {
		t.Fatal("the holder served a certificate it was not given")
	}

	second, secondKey, _ := issue(t, time.Now(), 90*24*time.Hour)
	replacement, err := NewMaterial(second, secondKey, second)
	if err != nil {
		t.Fatalf("material: %v", err)
	}
	holder.Replace(replacement)

	// The same *config* now serves the new certificate — no redial, no new
	// credentials object, nothing for the caller to rebuild.
	after, err := config.GetClientCertificate(nil)
	if err != nil {
		t.Fatalf("get client certificate: %v", err)
	}
	if after.Leaf.NotBefore.Equal(before.Leaf.NotBefore) {
		t.Fatal("the holder is still serving the old certificate after rotation")
	}
}

func TestTheHolderRequiresTls13(t *testing.T) {
	certPEM, keyPEM, _ := issue(t, time.Now(), time.Hour)
	material, err := NewMaterial(certPEM, keyPEM, certPEM)
	if err != nil {
		t.Fatalf("material: %v", err)
	}
	config, err := NewHolder(material).ClientTLS("gateway.example")
	if err != nil {
		t.Fatalf("client TLS: %v", err)
	}
	if config.MinVersion != 0x0304 {
		t.Fatalf("expected TLS 1.3 minimum, got %x", config.MinVersion)
	}
	if config.InsecureSkipVerify {
		t.Fatal("an established identity must verify the platform")
	}
}
