// Command agent connects one Kubernetes cluster to the platform.
//
// It dials out and receives work on that connection. Nothing here listens, and
// nothing here decides what to investigate — see the package README for why the
// boundary is drawn where it is.
package main

import (
	"context"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/collectors"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/transport"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

var version = "0.1.0-m4a"

func main() {
	endpoint := flag.String("gateway", envOr("AGENT_GATEWAY", "127.0.0.1:5051"), "platform gateway address")
	cluster := flag.String("cluster", envOr("AGENT_CLUSTER_ID", ""), "cluster identifier this agent reports as")
	token := flag.String("token", envOr("AGENT_BOOTSTRAP_TOKEN", ""), "bootstrap token presented on connect")
	kubeconfig := flag.String("kubeconfig", envOr("KUBECONFIG", ""), "kubeconfig path; in-cluster config when empty")
	once := flag.Bool("once", false, "exit when the stream closes instead of reconnecting")
	flag.Parse()

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	if *cluster == "" {
		log.Error("a cluster id is required")
		os.Exit(2)
	}

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
		BootstrapToken: *token,
		AgentVersion:   version,
		KubeVersion:    kubeVersion,
	}, collector, log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	for {
		if err := client.Run(ctx); err != nil {
			log.Warn("disconnected", "error", err)
		}
		if *once || ctx.Err() != nil {
			return
		}
		// A gateway restart or a rolling deploy must not need the agent
		// restarted across a thousand clusters.
		select {
		case <-ctx.Done():
			return
		case <-time.After(3 * time.Second):
		}
	}
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
