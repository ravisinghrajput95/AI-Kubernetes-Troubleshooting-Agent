// Command agent connects one Kubernetes cluster to the platform.
//
// It dials out and receives work on that connection. Nothing here listens, and
// nothing here decides what to investigate — see the package README for why the
// boundary is drawn where it is.
//
// Identity: the agent enrols once, exchanging a single-use bootstrap token for
// a certificate whose private key it generates and never transmits. After that
// it renews itself at two-thirds of certificate life, so nobody has to touch a
// thousand clusters to keep them connected.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"math/rand"
	"net"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/collectors"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/identity"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/transport"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

var version = "0.2.0-m4b"

// How often to check whether the certificate has reached its renewal point.
// Cheap enough to be irrelevant — the check is a clock comparison, not a
// network call — and an hour is fine against a 90-day certificate.
//
// Exposed as a flag only so the integration suite can watch a real rotation
// happen in seconds rather than assert the arithmetic and hope.
const defaultRenewalCheck = time.Hour

// Reconnection pacing.
//
// The backoff exists because M4b made permanent refusal possible: a revoked
// certificate is rejected at every reconnect, and a flat three-second retry
// turns one revocation into an indefinite request loop — multiplied by however
// many clusters were revoked together. Found by revoking a running agent and
// watching the gateway log rather than by a test.
const (
	baseReconnectDelay = 3 * time.Second
	maxReconnectDelay  = time.Minute
	// A stream that lasted this long counts as a working connection, so the
	// next blip starts from the base delay rather than wherever the last
	// outage ended up.
	healthyStream = 30 * time.Second
	// Fraction of the delay to randomise, either way.
	reconnectJitter = 0.2
)

func main() {
	endpoint := flag.String("gateway", envOr("AGENT_GATEWAY", "127.0.0.1:5051"), "platform gateway address (mTLS)")
	enrolEndpoint := flag.String("enrol", envOr("AGENT_ENROLMENT", ""), "platform enrolment address; defaults to the gateway port plus one")
	cluster := flag.String("cluster", envOr("AGENT_CLUSTER_ID", ""), "cluster identifier this agent reports as")
	token := flag.String("bootstrap-token", envOr("AGENT_BOOTSTRAP_TOKEN", ""), "single-use enrolment token from `agentctl issue-token`")
	identityDir := flag.String("identity-dir", envOr("AGENT_IDENTITY_DIR", ""), "keep the key and certificate in this directory instead of a Kubernetes Secret")
	identitySecret := flag.String("identity-secret", envOr("AGENT_IDENTITY_SECRET", "k8s-ops-agent-identity"), "Secret holding this agent's key and certificate")
	identityNamespace := flag.String("identity-namespace", envOr("POD_NAMESPACE", ""), "namespace of that Secret; defaults to the pod's own")
	caFile := flag.String("ca-file", envOr("AGENT_CA_FILE", ""), "platform CA bundle, for verifying the gateway during enrolment")
	insecureMode := flag.Bool("insecure", envOr("AGENT_INSECURE", "") == "1", "plaintext, no certificate; local development only")
	kubeconfig := flag.String("kubeconfig", envOr("KUBECONFIG", ""), "kubeconfig path; in-cluster config when empty")
	renewalCheck := flag.Duration("renewal-check", defaultRenewalCheck, "how often to check whether the certificate has reached its renewal point")
	once := flag.Bool("once", false, "exit when the stream closes instead of reconnecting")
	flag.Parse()

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	if *cluster == "" {
		log.Error("a cluster id is required")
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	config, err := loadConfig(*kubeconfig)
	if err != nil {
		log.Error("could not build a Kubernetes client", "error", err)
		os.Exit(1)
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		log.Error("could not build a Kubernetes client", "error", err)
		os.Exit(1)
	}

	var holder *identity.Holder
	if *insecureMode {
		log.Warn("running in insecure mode: plaintext, and this agent's cluster id is asserted rather than proved",
			"note", "matches the platform's AGENT_GATEWAY_TLS=disabled; for local development only")
	} else {
		store, err := identityStore(clientset, *identityDir, *identityNamespace, *identitySecret)
		if err != nil {
			log.Error("could not decide where to keep this agent's identity", "error", err)
			os.Exit(1)
		}

		material, err := establishIdentity(ctx, store, *enrolEndpoint, *endpoint, *cluster, *token, *caFile, log)
		if err != nil {
			log.Error("could not establish this agent's identity", "error", err)
			os.Exit(1)
		}
		holder = identity.NewHolder(material)

		// Renewal runs alongside the stream and never interrupts it: a new
		// certificate is written and swapped in, and the connection already
		// open keeps using the old one, which stays valid for the remaining
		// third of its life.
		go transport.KeepFresh(ctx, store, holder,
			*endpoint, *cluster, version, *renewalCheck, log)
	}

	kubeVersion := "unknown"
	if info, err := clientset.Discovery().ServerVersion(); err == nil {
		kubeVersion = info.GitVersion
	} else {
		// Not fatal: the agent still serves reads, and an unknown server
		// version is reported rather than guessed.
		log.Warn("could not read the server version", "error", err)
	}

	collector := collectors.New(clientset.CoreV1().RESTClient(), *cluster)
	client := transport.New(transport.Options{
		Endpoint:       *endpoint,
		ClusterID:      *cluster,
		AgentVersion:   version,
		KubeVersion:    kubeVersion,
		Identity:       holder,
		Insecure:       *insecureMode,
		BootstrapToken: *token,
	}, collector, log)

	delay := baseReconnectDelay
	for {
		opened := time.Now()
		if err := client.Run(ctx); err != nil {
			log.Warn("disconnected", "error", err, "retrying_in", delay)
		}
		if *once || ctx.Err() != nil {
			return
		}

		// A gateway restart or a rolling deploy must not need the agent
		// restarted across a thousand clusters — but nor should an agent the
		// platform is permanently refusing hammer it every three seconds. A
		// stream that lasted is treated as success and resets the backoff.
		if time.Since(opened) >= healthyStream {
			delay = baseReconnectDelay
		}

		select {
		case <-ctx.Done():
			return
		case <-time.After(jitter(delay)):
		}
		delay = min(delay*2, maxReconnectDelay)
	}
}

// jitter spreads reconnects so a fleet that lost its gateway together does not
// come back in lockstep. Deterministic pacing at a thousand clusters is a
// thundering herd aimed at the component that just recovered.
func jitter(delay time.Duration) time.Duration {
	spread := float64(delay) * reconnectJitter
	return delay - time.Duration(spread) + time.Duration(rand.Float64()*2*spread)
}

// establishIdentity loads this agent's certificate, enrolling if it has none.
//
// Enrolment happens once in an agent's life. A stored identity is reused on
// every restart, which is what stops a rolling deploy from needing a fresh
// bootstrap token per pod.
// identityStore decides where this agent keeps its credential.
//
// A directory when one is named, which covers `docker run` and a laptop. A
// Kubernetes Secret otherwise, because that is the only durable store every
// distribution has — see `SecretStore` for the list of places a
// PersistentVolumeClaim silently fails to bind.
func identityStore(
	client kubernetes.Interface,
	dir string,
	namespace string,
	name string,
) (identity.Store, error) {
	if dir != "" {
		return identity.NewStore(dir), nil
	}

	if namespace == "" {
		// Falls back to the projected service account namespace, which every
		// pod has whether or not the manifest set POD_NAMESPACE.
		data, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
		if err != nil {
			return nil, fmt.Errorf(
				"no --identity-dir and no namespace to put a Secret in; set " +
					"POD_NAMESPACE or --identity-namespace when running outside a cluster",
			)
		}
		namespace = strings.TrimSpace(string(data))
	}

	return identity.NewSecretStore(client, namespace, name), nil
}

func establishIdentity(
	ctx context.Context,
	store identity.Store,
	enrolEndpoint string,
	gatewayEndpoint string,
	cluster string,
	token string,
	caFile string,
	log *slog.Logger,
) (*identity.Material, error) {
	if store.Exists() {
		material, err := store.Load()
		if err == nil {
			log.Info("loaded stored identity",
				"expires", material.Leaf.NotAfter.Format(time.RFC3339),
				"renews", identity.RenewAt(material.Leaf).Format(time.RFC3339),
			)
			return material, nil
		}
		// Falling through to enrolment would need a token that is probably not
		// present, so say what is actually wrong.
		log.Error("the stored identity could not be loaded", "error", err)
		return nil, err
	}

	if token == "" {
		return nil, errNoIdentity{}
	}

	return transport.Enrol(ctx, store, transport.EnrolOptions{
		Endpoint:     enrolmentAddress(enrolEndpoint, gatewayEndpoint),
		Token:        token,
		ClusterID:    cluster,
		CAFile:       caFile,
		AgentVersion: version,
	}, log)
}

// enrolmentAddress defaults the enrolment port to one above the gateway's,
// which is what the platform does when AGENT_ENROLMENT_PORT is unset.
func enrolmentAddress(configured, gateway string) string {
	if configured != "" {
		return configured
	}
	host, port, err := net.SplitHostPort(gateway)
	if err != nil {
		return gateway
	}
	number, err := strconv.Atoi(port)
	if err != nil {
		return gateway
	}
	return net.JoinHostPort(host, strconv.Itoa(number+1))
}

type errNoIdentity struct{}

func (errNoIdentity) Error() string {
	return "this agent has no stored certificate and no --bootstrap-token to obtain" +
		" one. Issue a token on the platform with `python -m app.agentctl" +
		" issue-token --cluster <id>`, or pass --insecure for local development."
}

func loadConfig(kubeconfig string) (*rest.Config, error) {
	if kubeconfig != "" {
		return clientcmd.BuildConfigFromFlags("", kubeconfig)
	}
	if config, err := rest.InClusterConfig(); err == nil {
		return config, nil
	}
	return clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		clientcmd.NewDefaultClientConfigLoadingRules(),
		&clientcmd.ConfigOverrides{},
	).ClientConfig()
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
