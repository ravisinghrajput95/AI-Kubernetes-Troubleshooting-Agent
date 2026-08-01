// Package identity holds the agent's own credential: it generates the key,
// keeps it on disk, and decides when the certificate needs replacing.
//
// The private key is generated here and never leaves the process that made it.
// Registration sends a certificate signing request — the public half and a
// proof that this agent holds the other one — so no path exists by which a
// cluster's identity key could reach the platform, a log, or the wire.
//
// Nothing in this package speaks gRPC. Rotation arithmetic and key handling are
// the parts that must be right, and keeping them free of a transport is what
// lets them be tested without one.
package identity

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"time"
)

// RenewalFraction is where in a certificate's life renewal is attempted.
//
// Two thirds, per ADR-005. The remaining third is the overlap window: the old
// certificate stays valid throughout, so a failed renewal has weeks of retries
// left and a successful one never has to interrupt anything.
const RenewalFraction = 2.0 / 3.0

// DegradedFraction is how little life must remain before the agent starts
// saying so on its health messages. Well past the renewal point, so seeing it
// means renewal has been failing for a long time.
const DegradedFraction = 0.10

// GenerateKey makes the key this agent will be known by. P-256 matches what the
// platform's CA will certify.
func GenerateKey() (*ecdsa.PrivateKey, error) {
	return ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
}

// EncodeKey serialises a private key as PKCS#8 PEM, for writing to disk.
func EncodeKey(key *ecdsa.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("encode private key: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

// DecodeKey reads a PKCS#8 PEM private key.
func DecodeKey(data []byte) (*ecdsa.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, fmt.Errorf("no PEM block in the private key file")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse private key: %w", err)
	}
	key, ok := parsed.(*ecdsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("the agent key must be an EC P-256 key")
	}
	return key, nil
}

// CertificateRequest builds a CSR for the given key.
//
// The subject is deliberately uninformative. The platform discards everything
// here except the public key and names the certificate from the cluster the
// bootstrap token was issued for, so anything this agent put in its subject
// would be decoration at best and a false claim at worst.
func CertificateRequest(key *ecdsa.PrivateKey) ([]byte, error) {
	template := &x509.CertificateRequest{
		Subject:            pkix.Name{CommonName: "agent"},
		SignatureAlgorithm: x509.ECDSAWithSHA256,
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, template, key)
	if err != nil {
		return nil, fmt.Errorf("create certificate request: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

// NewKeyAndRequest generates a key and the CSR that asks for it to be certified.
func NewKeyAndRequest() (keyPEM []byte, csrPEM []byte, err error) {
	key, err := GenerateKey()
	if err != nil {
		return nil, nil, err
	}
	if keyPEM, err = EncodeKey(key); err != nil {
		return nil, nil, err
	}
	if csrPEM, err = CertificateRequest(key); err != nil {
		return nil, nil, err
	}
	return keyPEM, csrPEM, nil
}

// ParseCertificate reads the leaf out of a PEM chain.
func ParseCertificate(data []byte) (*x509.Certificate, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, fmt.Errorf("no PEM block in the certificate")
	}
	return x509.ParseCertificate(block.Bytes)
}

// RenewAt is when the agent should try to replace this certificate.
//
// Computed from the certificate's own validity rather than from a configured
// interval, so an agent given a short-lived certificate renews proportionally
// sooner without being told to.
func RenewAt(leaf *x509.Certificate) time.Time {
	life := leaf.NotAfter.Sub(leaf.NotBefore)
	return leaf.NotBefore.Add(time.Duration(float64(life) * RenewalFraction))
}

// DueForRenewal reports whether renewal should be attempted now.
func DueForRenewal(leaf *x509.Certificate, now time.Time) bool {
	return !now.Before(RenewAt(leaf))
}

// Degradation describes an expiring certificate for AgentHealth, or "" when
// there is nothing worth saying.
//
// The platform's `AgentHealth.degradation` field exists for exactly this: a
// still-connected agent that is in trouble. An agent whose renewals are failing
// is working perfectly right up until it is not, and this is the only warning
// anyone gets.
func Degradation(leaf *x509.Certificate, now time.Time) string {
	if leaf == nil {
		return ""
	}
	life := leaf.NotAfter.Sub(leaf.NotBefore)
	remaining := leaf.NotAfter.Sub(now)
	if remaining <= 0 {
		return "this agent's certificate has expired; renewal has been failing"
	}
	if float64(remaining) > float64(life)*DegradedFraction {
		return ""
	}
	return fmt.Sprintf(
		"this agent's certificate expires in %s and renewal is failing",
		remaining.Round(time.Hour),
	)
}
