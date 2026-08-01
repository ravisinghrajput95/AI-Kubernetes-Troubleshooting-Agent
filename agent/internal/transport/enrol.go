package transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"net"
	"os"
	"time"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/identity"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

// EnrolOptions is what the agent needs to obtain its first certificate.
type EnrolOptions struct {
	// The platform's enrolment listener, which requests no client certificate
	// because an enrolling agent has none to present.
	Endpoint string
	// Single-use, issued out of band by `agentctl issue-token`.
	Token string
	// The cluster this agent is being enrolled as. Checked against the token's
	// binding by the platform; it is not the agent's to decide.
	ClusterID string
	// The platform CA, copied to this host out of band. Strongly preferred:
	// without it the first connection is trust-on-first-use.
	CAFile       string
	AgentVersion string
}

// Enrol exchanges a bootstrap token for a certificate and stores it.
//
// The key is generated here and stays here. What crosses the wire is a CSR,
// which carries the public half and a signature proving this process holds the
// private one.
func Enrol(
	ctx context.Context,
	store identity.Store,
	options EnrolOptions,
	log *slog.Logger,
) (*identity.Material, error) {
	keyPEM, csrPEM, err := identity.NewKeyAndRequest()
	if err != nil {
		return nil, err
	}

	config, err := enrolmentTLS(options, log)
	if err != nil {
		return nil, err
	}

	connection, err := grpc.NewClient(
		options.Endpoint,
		grpc.WithTransportCredentials(credentials.NewTLS(config)),
	)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", options.Endpoint, err)
	}
	defer connection.Close()

	timed, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	response, err := agentv1.NewAgentGatewayClient(connection).Register(timed, &agentv1.RegistrationRequest{
		BootstrapToken:            options.Token,
		ClusterId:                 options.ClusterID,
		CertificateSigningRequest: csrPEM,
		AgentVersion:              options.AgentVersion,
	})
	if err != nil {
		return nil, fmt.Errorf("register with %s: %w", options.Endpoint, err)
	}

	material, err := identity.NewMaterial(response.GetCertificate(), keyPEM, response.GetCaBundle())
	if err != nil {
		return nil, err
	}
	if err := store.Save(response.GetCertificate(), keyPEM, response.GetCaBundle()); err != nil {
		return nil, err
	}

	log.Info("enrolled",
		"cluster", options.ClusterID,
		"expires", material.Leaf.NotAfter.Format(time.RFC3339),
		"renews", identity.RenewAt(material.Leaf).Format(time.RFC3339),
		"gateway", response.GetGatewayEndpoint(),
	)
	return material, nil
}

// Renew asks for the next certificate, authenticated by the current one.
//
// No bootstrap token and no human: this is what makes rotation possible across
// a thousand clusters. The platform reads the cluster off the presented
// certificate, so a renewal cannot rename the agent even if it asked to.
//
// The new material is stored and swapped into the holder. **The stream already
// running is not touched** — it keeps the old certificate, which stays valid
// for the remaining third of its life, and the next dial picks up the new one.
func Renew(
	ctx context.Context,
	store identity.Store,
	holder *identity.Holder,
	endpoint string,
	clusterID string,
	agentVersion string,
	log *slog.Logger,
) error {
	keyPEM, csrPEM, err := identity.NewKeyAndRequest()
	if err != nil {
		return err
	}

	config, err := holder.ClientTLS(serverName(endpoint))
	if err != nil {
		return err
	}

	connection, err := grpc.NewClient(endpoint, grpc.WithTransportCredentials(credentials.NewTLS(config)))
	if err != nil {
		return fmt.Errorf("dial %s: %w", endpoint, err)
	}
	defer connection.Close()

	timed, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	response, err := agentv1.NewAgentGatewayClient(connection).Register(timed, &agentv1.RegistrationRequest{
		ClusterId:                 clusterID,
		CertificateSigningRequest: csrPEM,
		AgentVersion:              agentVersion,
	})
	if err != nil {
		return fmt.Errorf("renew: %w", err)
	}

	material, err := identity.NewMaterial(response.GetCertificate(), keyPEM, response.GetCaBundle())
	if err != nil {
		return err
	}
	if err := store.Save(response.GetCertificate(), keyPEM, response.GetCaBundle()); err != nil {
		return err
	}
	holder.Replace(material)

	log.Info("renewed certificate",
		"expires", material.Leaf.NotAfter.Format(time.RFC3339),
		"renews", identity.RenewAt(material.Leaf).Format(time.RFC3339),
		"note", "the existing stream keeps the previous certificate until it reconnects",
	)
	return nil
}

// KeepFresh renews at two-thirds of certificate life, for as long as it runs.
//
// It never closes the stream. A renewal that forced a reconnect would drop
// whatever collection was in flight across the whole fleet at once, which is
// the failure this scheduling exists to avoid.
func KeepFresh(
	ctx context.Context,
	store identity.Store,
	holder *identity.Holder,
	endpoint string,
	clusterID string,
	agentVersion string,
	interval time.Duration,
	log *slog.Logger,
) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		if !holder.DueForRenewal(time.Now()) {
			continue
		}
		if err := Renew(ctx, store, holder, endpoint, clusterID, agentVersion, log); err != nil {
			// Not fatal, and deliberately quiet about retrying: the current
			// certificate is good for another third of its life, so there is
			// time for many more attempts before anything breaks.
			log.Warn("certificate renewal failed; will retry", "error", err)
		}
	}
}

func enrolmentTLS(options EnrolOptions, log *slog.Logger) (*tls.Config, error) {
	config := &tls.Config{MinVersion: tls.VersionTLS13, ServerName: serverName(options.Endpoint)}

	if options.CAFile == "" {
		// The bootstrap trust problem, stated rather than hidden: an agent that
		// has never spoken to the platform has nothing to verify it with. The
		// CA bundle returned by Register is pinned for every connection after
		// this one, so the exposure is this single call.
		log.Warn("enrolling without --ca-file: trusting the platform on first use",
			"advice", "copy the CA bundle to this host and pass --ca-file to remove this window")
		config.InsecureSkipVerify = true
		return config, nil
	}

	bundle, err := os.ReadFile(options.CAFile)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", options.CAFile, err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(bundle) {
		return nil, fmt.Errorf("%s contains no certificate", options.CAFile)
	}
	config.RootCAs = pool
	return config, nil
}

// serverName is the host half of an endpoint, which is the name the platform's
// certificate must carry.
func serverName(endpoint string) string {
	host, _, err := net.SplitHostPort(endpoint)
	if err != nil {
		return endpoint
	}
	return host
}
