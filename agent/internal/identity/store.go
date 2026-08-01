package identity

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// File names under the identity directory.
const (
	keyFile  = "agent.key"
	certFile = "agent.crt"
	caFile   = "ca.crt"
)

// Material is one usable identity: the certificate, the key that proves it, and
// the CA that verifies the platform in return.
type Material struct {
	Certificate tls.Certificate
	Leaf        *x509.Certificate
	CABundle    []byte
}

// Store keeps the agent's identity on disk between restarts.
//
// Persisting it is what makes a restart cheap: an agent that regenerated its
// key on every start would need a fresh bootstrap token every time, which at
// fleet scale means a human per restart.
type Store struct {
	dir string
}

func NewStore(dir string) *Store {
	return &Store{dir: dir}
}

func (s *Store) path(name string) string {
	return filepath.Join(s.dir, name)
}

// Exists reports whether this agent has already enrolled.
func (s *Store) Exists() bool {
	for _, name := range []string{keyFile, certFile, caFile} {
		if _, err := os.Stat(s.path(name)); err != nil {
			return false
		}
	}
	return true
}

// Save writes a newly issued identity, replacing any previous one.
//
// The key is written 0600 from the moment it is created rather than chmod-ed
// afterwards, because the gap between the two is a window in which a private
// key is readable by anything on the host.
func (s *Store) Save(certPEM, keyPEM, caPEM []byte) error {
	if err := os.MkdirAll(s.dir, 0o700); err != nil {
		return fmt.Errorf("create %s: %w", s.dir, err)
	}
	if err := writeFile(s.path(keyFile), keyPEM, 0o600); err != nil {
		return err
	}
	if err := writeFile(s.path(certFile), certPEM, 0o644); err != nil {
		return err
	}
	return writeFile(s.path(caFile), caPEM, 0o644)
}

// Load reads the stored identity.
func (s *Store) Load() (*Material, error) {
	certPEM, err := os.ReadFile(s.path(certFile))
	if err != nil {
		return nil, fmt.Errorf("read certificate: %w", err)
	}
	keyPEM, err := os.ReadFile(s.path(keyFile))
	if err != nil {
		return nil, fmt.Errorf("read key: %w", err)
	}
	caPEM, err := os.ReadFile(s.path(caFile))
	if err != nil {
		return nil, fmt.Errorf("read CA bundle: %w", err)
	}
	return NewMaterial(certPEM, keyPEM, caPEM)
}

// NewMaterial validates a certificate and key together and pairs them.
func NewMaterial(certPEM, keyPEM, caPEM []byte) (*Material, error) {
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("the certificate and key do not match: %w", err)
	}
	leaf, err := ParseCertificate(certPEM)
	if err != nil {
		return nil, err
	}
	pair.Leaf = leaf
	return &Material{Certificate: pair, Leaf: leaf, CABundle: caPEM}, nil
}

// RootPool is the CA an agent verifies the platform against.
func (m *Material) RootPool() (*x509.CertPool, error) {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(m.CABundle) {
		return nil, fmt.Errorf("the stored CA bundle contains no certificate")
	}
	return pool, nil
}

func writeFile(path string, data []byte, mode os.FileMode) error {
	// Written to a temporary file and renamed, so a crash mid-write cannot
	// leave half a certificate where a whole one was.
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, data, mode); err != nil {
		return fmt.Errorf("write %s: %w", temporary, err)
	}
	if err := os.Rename(temporary, path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}

// Holder is the agent's current identity, swappable while in use.
//
// This is what makes rotation invisible to a running collection. Go consults
// `GetClientCertificate` on every handshake, so replacing what this holds is
// enough for the *next* connection to use the new certificate — and the
// connection already open is never touched. The old certificate stays valid for
// the remaining third of its life, so nothing has to reconnect at rotation
// time and no in-flight collection is interrupted.
type Holder struct {
	mutex    sync.RWMutex
	material *Material
}

func NewHolder(material *Material) *Holder {
	return &Holder{material: material}
}

func (h *Holder) Material() *Material {
	h.mutex.RLock()
	defer h.mutex.RUnlock()
	return h.material
}

func (h *Holder) Replace(material *Material) {
	h.mutex.Lock()
	defer h.mutex.Unlock()
	h.material = material
}

// ClientTLS is the configuration for dialling the platform.
func (h *Holder) ClientTLS(serverName string) (*tls.Config, error) {
	pool, err := h.Material().RootPool()
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		MinVersion: tls.VersionTLS13,
		RootCAs:    pool,
		ServerName: serverName,
		// Per handshake, not per config: this is the seam rotation goes
		// through.
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			material := h.Material()
			if material == nil {
				return nil, fmt.Errorf("this agent has no certificate")
			}
			return &material.Certificate, nil
		},
	}, nil
}

// DueForRenewal reports whether the held certificate has reached 2/3 of its life.
func (h *Holder) DueForRenewal(now time.Time) bool {
	material := h.Material()
	if material == nil {
		return false
	}
	return DueForRenewal(material.Leaf, now)
}

// Degradation is what to report on AgentHealth, or "".
func (h *Holder) Degradation(now time.Time) string {
	material := h.Material()
	if material == nil {
		return ""
	}
	return Degradation(material.Leaf, now)
}
