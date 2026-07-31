// Package collectors turns a resolved read into an evidence record.
package collectors

import (
	"context"
	"errors"
	"fmt"
	"time"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/policy"
	"google.golang.org/protobuf/types/known/timestamppb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/rest"
)

// Collector performs reads against one cluster.
type Collector struct {
	client  rest.Interface
	cluster string
}

func New(client rest.Interface, cluster string) *Collector {
	return &Collector{client: client, cluster: cluster}
}

// Collect resolves a spec and performs the read.
//
// A failure is returned as a record with a status, never as an error: the
// platform's evidence layer treats "could not look" as citable data, and a
// dropped record would make a gap indistinguishable from a healthy result.
func (c *Collector) Collect(ctx context.Context, spec *agentv1.EvidenceSpec) *agentv1.EvidenceRecord {
	started := time.Now()
	namespace, name := targetOf(spec)

	record := &agentv1.EvidenceRecord{
		Id:          evidenceID(spec, c.cluster, namespace, name),
		Kind:        spec.GetKind(),
		Source:      agentv1.EvidenceSource_EVIDENCE_SOURCE_KUBECTL,
		Target:      spec.GetTarget(),
		CollectorId: "agent",
		CollectedAt: timestamppb.New(started),
	}

	read, err := policy.Resolve(spec.GetKind(), namespace, name, spec.GetParameters())
	if err != nil {
		// Refused, not attempted. The platform may name a kind this agent
		// knows; anything else stops here.
		record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_NOT_APPLICABLE
		record.Detail = err.Error()
		record.DurationMs = time.Since(started).Milliseconds()
		return record
	}

	command := read.EquivalentCommand
	record.EquivalentCommand = &command

	request := c.client.Get().AbsPath(read.Path)
	for key, values := range read.Query {
		for _, value := range values {
			request = request.Param(key, value)
		}
	}

	// Raw bytes, deliberately: decoding into typed objects would drop fields
	// this binary's compiled-in schema does not know and reorder keys on the
	// way back out, so the same read would differ between an agent and the
	// local path. See the note in the package README.
	body, err := request.DoRaw(ctx)
	record.DurationMs = time.Since(started).Milliseconds()

	if err != nil {
		record.Status = statusFor(err)
		record.Detail = detailFor(err)
		return record
	}

	record.Payload = wrap(body, read.Text)
	record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_OK
	return record
}

// wrap presents a text response as a one-key object so the wire payload is
// always JSON, whatever the read returned.
func wrap(body []byte, text bool) []byte {
	if !text {
		return body
	}
	encoded, err := jsonString(string(body))
	if err != nil {
		return []byte(`{"text":""}`)
	}
	return []byte(`{"text":` + encoded + `}`)
}

func statusFor(err error) agentv1.EvidenceStatus {
	switch {
	case apierrors.IsForbidden(err), apierrors.IsUnauthorized(err):
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_FORBIDDEN
	case apierrors.IsTimeout(err), apierrors.IsServerTimeout(err), errors.Is(err, context.DeadlineExceeded):
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_TIMEOUT
	case apierrors.IsNotFound(err):
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_EMPTY
	default:
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_FAILED
	}
}

func detailFor(err error) string {
	if status := apierrors.APIStatus(nil); errors.As(err, &status) {
		return status.Status().Message
	}
	return err.Error()
}

func targetOf(spec *agentv1.EvidenceSpec) (namespace, name string) {
	target := spec.GetTarget()
	if target == nil {
		return "", ""
	}
	return target.GetNamespace(), target.GetName()
}

// evidenceID mirrors the platform's `kind:target.key` form, so a record
// collected remotely lands under the same id it would have locally.
func evidenceID(spec *agentv1.EvidenceSpec, cluster, namespace, name string) string {
	target := spec.GetTarget()
	kind := "cluster"
	if target != nil && target.GetKind() != "" {
		kind = target.GetKind()
	}
	if name == "" {
		name = "_cluster/" + cluster
	}
	if namespace != "" {
		return fmt.Sprintf("%s:%s/%s/%s", spec.GetKind(), kind, namespace, name)
	}
	return fmt.Sprintf("%s:%s/%s", spec.GetKind(), kind, name)
}
