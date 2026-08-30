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

// API-server rate limits this agent applies to itself.
//
// client-go's own defaults are 5 QPS / burst 10, which throttle a single
// investigation: it issues on the order of twenty reads, so everything past
// the tenth waits about a second. 50/100 keeps one investigation unthrottled
// with room for a few concurrent ones, and is still an order of magnitude
// below what an API server serves comfortably.
const (
	defaultAPIQPS   = 50.0
	defaultAPIBurst = 100
)

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
	impersonate := flag.Bool("impersonate", envOr("AGENT_IMPERSONATE", "") == "1", "read as the calling user, so the cluster applies their RBAC (needs the impersonate verb)")
	apiQPS := flag.Float64("api-qps", envOrFloat("AGENT_API_QPS", defaultAPIQPS), "sustained API-server requests per second this agent allows itself")
	apiBurst := flag.Int("api-burst", envOrInt("AGENT_API_BURST", defaultAPIBurst), "burst above --api-qps before client-side throttling kicks in")
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

	config, err := loadConfig(*kubeconfig, *apiQPS, *apiBurst)
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

	collector := collectors.New(clientset.CoreV1().RESTClient(), *cluster, *impersonate)
	if *impersonate {
		log.Info("reads run as the calling user; this agent's ServiceAccount needs the impersonate verb")
	} else {
		log.Warn("reads run as this agent's ServiceAccount, not as the calling user. " +
			"The platform's guarantee that it cannot see more than you can does not hold " +
			"on this agent. Re-apply the enrolment manifest, or pass --impersonate with a " +
			"ClusterRole granting the impersonate verb.")
	}
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

// loadConfig builds this agent's rest.Config — and applies its rate limits,
// because there is no other way to obtain one.
//
// The limits are applied *here* rather than at the call site on purpose. As a
// separate step in main() they were correct, tested, and deletable without a
// single test noticing: main() is not under test, so `applyRateLimits` could
// stop being called and every assertion about it would still pass. Folding it
// into the only constructor makes "a config from this program is rate limited"
// structural rather than remembered — the same reason the platform puts its
// authorisation check in one router dependency instead of on every route.
func loadConfig(kubeconfig string, qps float64, burst int) (*rest.Config, error) {
	config, err := discoverConfig(kubeconfig)
	if err != nil {
		return nil, err
	}
	applyRateLimits(config, qps, burst)
	return config, nil
}

func discoverConfig(kubeconfig string) (*rest.Config, error) {
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

// applyRateLimits sets the agent's client-side ceiling on API-server traffic.
//
// **A rest.Config that leaves these at zero is not unlimited — it is 5 QPS with
// a burst of 10**, client-go's defaults, applied silently. One investigation
// issues on the order of twenty reads, so everything past the tenth waits about
// a second and the agent logs "Waited before sending request ... client-side
// throttling". Collection time becomes a function of the rate limiter rather
// than of the cluster: invisible at test size, dominant on a large one. It was
// noted as an observation during the §21 in-cluster run and is fixed here.
//
// These are still limits, not the absence of one. An agent that can flood its
// own API server is a worse neighbour than one that is slow, so the defaults
// are raised only to where a single investigation does not queue behind itself
// — and they are flags, because the right ceiling belongs to whoever owns the
// cluster rather than to us.
//
// A non-positive value leaves client-go's default in place rather than
// disabling the limiter, which is what a zero would mean if assigned directly.
func applyRateLimits(config *rest.Config, qps float64, burst int) {
	if qps > 0 {
		config.QPS = float32(qps)
	}
	if burst > 0 {
		config.Burst = burst
	}
}

func envOrInt(key string, fallback int) int {
	if value, err := strconv.Atoi(os.Getenv(key)); err == nil && value > 0 {
		return value
	}
	return fallback
}

func envOrFloat(key string, fallback float64) float64 {
	if value, err := strconv.ParseFloat(os.Getenv(key), 64); err == nil && value > 0 {
		return value
	}
	return fallback
}
