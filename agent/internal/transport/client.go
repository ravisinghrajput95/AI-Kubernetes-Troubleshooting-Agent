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
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/policy"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

type Options struct {
	Endpoint       string
	ClusterID      string
	BootstrapToken string
	AgentVersion   string
	KubeVersion    string
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
	connection, err := grpc.NewClient(
		c.options.Endpoint,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return fmt.Errorf("dial %s: %w", c.options.Endpoint, err)
	}
	defer connection.Close()

	if c.options.BootstrapToken != "" {
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
	c.log.Info("connected", "endpoint", c.options.Endpoint, "cluster", c.options.ClusterID)

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
			send(&agentv1.AgentMessage{Payload: &agentv1.AgentMessage_Health{
				Health: &agentv1.AgentHealth{},
			}})
		default:
			c.log.Debug("ignoring message the platform sent", "type", fmt.Sprintf("%T", payload))
		}
	}
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

			record := c.collector.Collect(ctx, spec)
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
