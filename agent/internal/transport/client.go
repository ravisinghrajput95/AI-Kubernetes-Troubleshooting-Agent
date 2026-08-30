// Package transport carries the agent's side of the stream.
package transport

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/collectors"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/identity"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/policy"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

type Options struct {
	Endpoint     string
	ClusterID    string
	AgentVersion string
	KubeVersion  string
	// The agent's certificate, swappable while in use. When set the stream is
	// mTLS and the platform reads this agent's identity off the certificate
	// rather than off `hello`.
	Identity *identity.Holder
	// Plaintext development mode: no certificate, a shared token in metadata,
	// and a cluster id this agent asserts about itself. Matches the platform's
	// AGENT_GATEWAY_TLS=disabled and is refused by a gateway in mTLS mode.
	Insecure       bool
	BootstrapToken string
	// Reads run concurrently, bounded so a large collection cannot flood the
	// API server this agent is a guest on.
	MaxConcurrent int
}

type Client struct {
	options   Options
	collector *collectors.Collector
	log       *slog.Logger
}

func New(options Options, collector *collectors.Collector, log *slog.Logger) *Client {
	if options.MaxConcurrent <= 0 {
		options.MaxConcurrent = 8
	}
	return &Client{options: options, collector: collector, log: log}
}

// Run dials the platform and serves work until the context ends.
//
// The agent dials out and never listens: no inbound port is opened into the
// cluster, which is the constraint the entire transport is shaped around.
func (c *Client) Run(ctx context.Context) error {
	transportCredentials, err := c.credentials()
	if err != nil {
		return err
	}

	connection, err := grpc.NewClient(c.options.Endpoint, grpc.WithTransportCredentials(transportCredentials))
	if err != nil {
		return fmt.Errorf("dial %s: %w", c.options.Endpoint, err)
	}
	defer connection.Close()

	if c.options.Insecure && c.options.BootstrapToken != "" {
		ctx = metadata.AppendToOutgoingContext(ctx, "x-agent-token", c.options.BootstrapToken)
	}

	stream, err := agentv1.NewAgentGatewayClient(connection).Connect(ctx)
	if err != nil {
		return fmt.Errorf("open stream: %w", err)
	}

	hello := &agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Hello{Hello: &agentv1.AgentHello{
		ClusterId:         c.options.ClusterID,
		AgentVersion:      c.options.AgentVersion,
		KubernetesVersion: c.options.KubeVersion,
		SupportedKinds:    policy.SupportedKinds(),
		ProtocolVersion:   1,
	}}}
	if err := stream.Send(hello); err != nil {
		return fmt.Errorf("send hello: %w", err)
	}
	c.log.Info("connected",
		"endpoint", c.options.Endpoint,
		"cluster", c.options.ClusterID,
		"identity", c.identityDescription(),
	)

	// One writer goroutine: gRPC streams are not safe for concurrent Send, and
	// collection fans out.
	var writeLock sync.Mutex
	send := func(message *agentv1.AgentMessage) {
		writeLock.Lock()
		defer writeLock.Unlock()
		if err := stream.Send(message); err != nil {
			c.log.Warn("send failed", "error", err)
		}
	}

	for {
		message, err := stream.Recv()
		if err != nil {
			return fmt.Errorf("stream closed: %w", err)
		}

		switch payload := message.GetPayload().(type) {
		case *agentv1.PlatformMessage_Collect:
			go c.serve(ctx, payload.Collect, send)
		case *agentv1.PlatformMessage_Heartbeat:
			// An expiring certificate is reported here rather than only logged
			// locally: the platform is the one that can act on it, and
			// `AgentHealth.degradation` is the field the schema already
			// reserved for a connected-but-troubled agent.
			health := &agentv1.AgentHealth{}
			if c.options.Identity != nil {
				health.Degradation = c.options.Identity.Degradation(time.Now())
			}
			send(&agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Health{Health: health}})
		default:
			c.log.Debug("ignoring message the platform sent", "type", fmt.Sprintf("%T", payload))
		}
	}
}

// credentials decides how this agent proves who it is.
//
// The mTLS path is the default and the only one in which the platform knows
// the agent's identity rather than being told it. The insecure path exists for
// local development, matches the platform's AGENT_GATEWAY_TLS=disabled, and is
// refused outright by a gateway running in mTLS mode — so choosing it wrongly
// fails to connect rather than silently downgrading.
func (c *Client) credentials() (credentials.TransportCredentials, error) {
	if c.options.Insecure {
		return insecure.NewCredentials(), nil
	}
	if c.options.Identity == nil {
		return nil, fmt.Errorf("no certificate: enrol first, or pass --insecure for local development")
	}
	config, err := c.options.Identity.ClientTLS(serverName(c.options.Endpoint))
	if err != nil {
		return nil, err
	}
	return credentials.NewTLS(config), nil
}

func (c *Client) identityDescription() string {
	if c.options.Insecure {
		return "declared (plaintext development mode)"
	}
	material := c.options.Identity.Material()
	return fmt.Sprintf("certificate expiring %s", material.Leaf.NotAfter.Format(time.RFC3339))
}

// serve answers one collection request, streaming each record as it is
// produced rather than batching: a slow read must not delay evidence that is
// already available.
func (c *Client) serve(
	ctx context.Context,
	request *agentv1.CollectionRequest,
	send func(*agentv1.AgentMessage),
) {
	if deadline := request.GetBudget().GetDeadlineMs(); deadline > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(deadline)*time.Millisecond)
		defer cancel()
	}

	specs := request.GetSpecs()

	// **An impersonating agent will not serve an unattributed read.**
	//
	// Falling back to the agent's own broad-read ServiceAccount for a request
	// that named nobody is exactly the hole impersonation closes, and it would
	// be reachable by omitting one wire field. The platform's own event ingress
	// makes the same refusal for the same reason: an alert-triggered
	// investigation with no identity would read as the service account, so
	// `EVENT_SOURCES` requires a subject. Refused as FORBIDDEN rather than
	// dropped, because a citable gap is the point of the evidence layer.
	if c.collector.Impersonates() && request.GetActor().GetUsername() == "" {
		for _, spec := range specs {
			send(&agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Evidence{
				Evidence: &agentv1.EvidenceEnvelope{
					InvestigationId: request.GetInvestigationId(),
					RequestId:       request.GetRequestId(),
					Record: &agentv1.EvidenceRecord{
						Kind:   spec.GetKind(),
						Target: spec.GetTarget(),
						Status: agentv1.EvidenceStatus_EVIDENCE_STATUS_FORBIDDEN,
						Detail: "This agent impersonates the calling user and the " +
							"request named none, so there is no identity to read as.",
						CollectorId: "agent",
					},
				},
			}})
		}
		send(&agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Done{Done: &agentv1.CollectionDone{
			InvestigationId: request.GetInvestigationId(),
			RequestId:       request.GetRequestId(),
			RecordsEmitted:  int32(len(specs)),
			SpecsRequested:  int32(len(specs)),
		}}})
		return
	}

	limit := make(chan struct{}, c.options.MaxConcurrent)
	var wait sync.WaitGroup
	var emitted int
	var counter sync.Mutex

	for _, spec := range specs {
		wait.Add(1)
		go func(spec *agentv1.EvidenceSpec) {
			defer wait.Done()
			limit <- struct{}{}
			defer func() { <-limit }()

			record := c.collector.Collect(ctx, spec, request.GetActor())
			send(&agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Evidence{
				Evidence: &agentv1.EvidenceEnvelope{
					InvestigationId: request.GetInvestigationId(),
					RequestId:       request.GetRequestId(),
					Record:          record,
				},
			}})

			counter.Lock()
			emitted++
			counter.Unlock()
		}(spec)
	}

	wait.Wait()

	// Always sent, even when nothing was produced. It is what lets the platform
	// tell "the agent finished and found nothing" from "the stream broke".
	send(&agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Done{Done: &agentv1.CollectionDone{
		InvestigationId: request.GetInvestigationId(),
		RequestId:       request.GetRequestId(),
		RecordsEmitted:  int32(emitted),
		SpecsRequested:  int32(len(specs)),
	}}})
}
