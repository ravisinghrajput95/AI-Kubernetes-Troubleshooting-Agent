// Package collectors turns a resolved read into an evidence record.
package collectors

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	"github.com/ravisinghrajput95/ai-kubernetes-agent/agent/internal/policy"
	"google.golang.org/protobuf/types/known/timestamppb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/rest"
)

// Collector performs reads against one cluster.
type Collector struct {
	client rest.Interface
	// impersonate applies the calling user's identity to every read, so the
	// *cluster* decides what a request may see. Off by default: an agent whose
	// ServiceAccount lacks the `impersonate` verb would have every read
	// refused, and an agent enrolled before this existed has exactly that
	// ClusterRole. The enrolment manifest turns it on and grants the verb in
	// the same document, so the two cannot be out of step for a cluster
	// enrolled after this shipped.
	impersonate bool
	cluster     string
}

func New(client rest.Interface, cluster string, impersonate bool) *Collector {
	return &Collector{client: client, cluster: cluster, impersonate: impersonate}
}

// Collect resolves a spec and performs the read.
//
// A failure is returned as a record with a status, never as an error: the
// platform's evidence layer treats "could not look" as citable data, and a
// dropped record would make a gap indistinguishable from a healthy result.
// Collect performs one read on behalf of `actor`.
//
// **The actor is much of the reason the agent is a separate process.** F13's
// guarantee is that the platform cannot see more than the calling user can, and
// it holds on the kubeconfig path because `kubectl --as` makes the API server
// apply that user's RBAC. On this path the actor arrived on the wire and was
// *discarded*, so every agent read ran as the agent's own ServiceAccount —
// broad read across the cluster, for any caller who could reach the platform.
// `proto/agent/v1/collection.proto` has documented the opposite since M2.
func (c *Collector) Collect(
	ctx context.Context,
	spec *agentv1.EvidenceSpec,
	actor *agentv1.Impersonation,
) *agentv1.EvidenceRecord {
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

	if policy.AllContainers(spec.GetKind(), spec.GetParameters()) {
		return c.collectEveryContainer(ctx, spec, actor, record, read, started)
	}

	body, err := c.perform(ctx, read, actor)
	record.DurationMs = time.Since(started).Milliseconds()

	if err != nil {
		record.Status = statusFor(err, read.Named)
		record.Detail = detailFor(err, body)
		return record
	}

	record.Payload = wrap(body, read.Text)
	record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_OK
	return record
}

// perform issues one resolved read and returns its raw body.
//
// Raw bytes, deliberately: decoding into typed objects would drop fields this
// binary's compiled-in schema does not know and reorder keys on the way back
// out, so the same read would differ between an agent and the local path. See
// the note in the package README.
//
// The body is returned alongside the error because client-go reports `unknown`
// for every error on a raw request and the API server's own sentence is in the
// body — see `detailFor`.
func (c *Collector) perform(
	ctx context.Context,
	read policy.Read,
	actor *agentv1.Impersonation,
) ([]byte, error) {
	request := c.client.Get().AbsPath(read.Path)
	for key, values := range read.Query {
		for _, value := range values {
			request = request.Param(key, value)
		}
	}
	return c.impersonated(request, actor).DoRaw(ctx)
}

// collectEveryContainer serves a log read that names no container.
//
// The API server has one log endpoint per container and refuses a
// multi-container pod that names none. kubectl expands `--all-containers`
// client-side; this is the same expansion, and without it every pod with a
// sidecar returned `BadRequest` and lost its logs entirely on the agent path
// while keeping them on the kubeconfig path (F24).
//
// Three properties are load-bearing:
//
//   - **Every read still goes through `policy.Resolve`.** The pod read and each
//     per-container log read are resolved exactly as a platform-issued spec
//     would be, so this expansion cannot reach a path the policy package would
//     have refused. It adds no capability; it spends reads the agent already
//     serves.
//   - **kubectl's container order, including init containers.** Verified
//     against a live cluster: `--all-containers` returns the init container's
//     output first, then the regular containers. A container that logged
//     nothing contributes nothing and is not an error.
//   - **The first error is the read's error, which is what kubectl does** and,
//     more importantly, what keeps the platform's classification identical on
//     both paths: `PodPreviousLogsCollector` maps "previous terminated" and
//     "not found" onto EMPTY, and it can only do that if the sentence reaches
//     it unchanged.
func (c *Collector) collectEveryContainer(
	ctx context.Context,
	spec *agentv1.EvidenceSpec,
	actor *agentv1.Impersonation,
	record *agentv1.EvidenceRecord,
	read policy.Read,
	started time.Time,
) *agentv1.EvidenceRecord {
	namespace, name := targetOf(spec)

	podRead, err := policy.Resolve("k8s.pods", namespace, name, nil)
	if err != nil {
		record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_NOT_APPLICABLE
		record.Detail = err.Error()
		record.DurationMs = time.Since(started).Milliseconds()
		return record
	}

	podBody, err := c.perform(ctx, podRead, actor)
	if err != nil {
		record.DurationMs = time.Since(started).Milliseconds()
		record.Status = statusFor(err, podRead.Named)
		record.Detail = detailFor(err, podBody)
		return record
	}

	containers, err := containerNames(podBody)
	if err != nil {
		record.DurationMs = time.Since(started).Milliseconds()
		record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_FAILED
		record.Detail = err.Error()
		return record
	}

	// Parameters are copied rather than mutated: the spec's map is the
	// caller's, and one container's name must not leak into the next read.
	base := map[string]string{}
	for key, value := range spec.GetParameters() {
		base[key] = value
	}

	var combined []byte
	for _, container := range containers {
		parameters := map[string]string{}
		for key, value := range base {
			parameters[key] = value
		}
		parameters["container"] = container

		containerRead, err := policy.Resolve("k8s.logs", namespace, name, parameters)
		if err != nil {
			record.DurationMs = time.Since(started).Milliseconds()
			record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_NOT_APPLICABLE
			record.Detail = err.Error()
			return record
		}

		body, err := c.perform(ctx, containerRead, actor)
		if err != nil {
			record.DurationMs = time.Since(started).Milliseconds()
			record.Status = statusFor(err, containerRead.Named)
			record.Detail = detailFor(err, body)
			return record
		}
		combined = append(combined, body...)
	}

	record.DurationMs = time.Since(started).Milliseconds()
	record.Payload = wrap(combined, read.Text)
	record.Status = agentv1.EvidenceStatus_EVIDENCE_STATUS_OK
	return record
}

// containerNames lists a pod's containers in the order kubectl reads them.
//
// Init containers first, then regular, then ephemeral — matching kubectl's own
// iteration, so `--all-containers` output is in the same order through either
// provider. Read out of the raw pod JSON rather than a typed object for the
// same reason every other read here is raw.
func containerNames(podBody []byte) ([]string, error) {
	var pod struct {
		Spec struct {
			InitContainers []struct {
				Name string `json:"name"`
			} `json:"initContainers"`
			Containers []struct {
				Name string `json:"name"`
			} `json:"containers"`
			EphemeralContainers []struct {
				Name string `json:"name"`
			} `json:"ephemeralContainers"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(podBody, &pod); err != nil {
		return nil, fmt.Errorf("could not read the pod to list its containers: %w", err)
	}

	names := []string{}
	for _, group := range [][]struct {
		Name string `json:"name"`
	}{pod.Spec.InitContainers, pod.Spec.Containers, pod.Spec.EphemeralContainers} {
		for _, container := range group {
			if container.Name != "" {
				names = append(names, container.Name)
			}
		}
	}
	if len(names) == 0 {
		return nil, errors.New("the pod reported no containers to read logs from")
	}
	return names, nil
}

// impersonated applies the calling user's identity to a request.
//
// Headers rather than a `rest.Config` field, because one agent serves every
// caller through one shared client: an identity baked into the config would be
// whichever caller set it last, applied to whoever's read went out next. Per
// request, an identity cannot outlive the read it belongs to.
func (c *Collector) impersonated(
	request *rest.Request,
	actor *agentv1.Impersonation,
) *rest.Request {
	if !c.impersonate || actor.GetUsername() == "" {
		return request
	}
	request = request.SetHeader("Impersonate-User", actor.GetUsername())
	if groups := actor.GetGroups(); len(groups) > 0 {
		request = request.SetHeader("Impersonate-Group", groups...)
	}
	return request
}

// Impersonates reports whether this agent applies the caller's identity, so the
// platform can say which guarantee an investigation actually ran under rather
// than assuming the stronger one.
func (c *Collector) Impersonates() bool { return c.impersonate }

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

// statusFor maps an API error onto the evidence status the platform reasons
// over.
//
// **A 404 means two different things and only one of them is EMPTY.** On a
// named read it means that object is gone, which the platform can treat as a
// successful empty observation. On a *list* read it means the API itself is
// not served by this cluster — metrics-server not installed, a group version
// this agent hardcodes that the cluster does not have — and reporting that as
// EMPTY says "we looked and there is nothing" about a thing nobody could look
// at.
//
// EMPTY is a *usable* status on the platform, so the difference is not
// cosmetic. It raises evidence completeness, which raises the confidence score
// of a diagnosis that saw less of the cluster, and it makes an absent
// metrics-server read as an idle one — the exact thing
// `docs/OBSERVABILITY_INTEGRATIONS.md` states must never happen: missing
// metrics must not look like healthy metrics.
//
// Found by running the differential suite against a real cluster with no
// metrics-server: the agent reported k8s.metrics.nodes usable and the
// kubeconfig path reported it unavailable, for the same cluster at the same
// moment.
func statusFor(err error, named bool) agentv1.EvidenceStatus {
	switch {
	case apierrors.IsForbidden(err), apierrors.IsUnauthorized(err):
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_FORBIDDEN
	case apierrors.IsTimeout(err), apierrors.IsServerTimeout(err), errors.Is(err, context.DeadlineExceeded):
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_TIMEOUT
	case apierrors.IsNotFound(err) && named:
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_EMPTY
	case apierrors.IsNotFound(err):
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_UNAVAILABLE
	default:
		return agentv1.EvidenceStatus_EVIDENCE_STATUS_FAILED
	}
}

// detailFor is why a refused read can name who was refused.
//
// **client-go reports `"unknown"` for every error on a raw request**, and the
// agent reads raw on purpose — decoding into typed objects would drop fields
// this binary's compiled-in schema does not know. So `transformResponse` cannot
// decode the error body either, and synthesises a `Status` whose `Message` is
// the literal string `unknown`. `err.Error()` says the same. The API server's
// actual sentence — `pods is forbidden: User "alice@acme.com" cannot list
// resource "pods" in API group "" at the cluster scope` — is sitting in the
// response body, which `DoRaw` hands back alongside the error.
//
// That sentence is the whole product of `app/kubernetes/access.py`: it exists
// to tell a locked door from a broken cluster, and to say *whose* RBAC closed
// the door. Through a kubeconfig it gets kubectl's message; through an agent it
// used to get "unknown", so an investigation degraded by one user's narrow
// permissions was indistinguishable from one degraded by a broken cluster.
//
// Found while proving impersonation end to end — the read was correctly
// refused and could not say by whom.
func detailFor(err error, body []byte) string {
	if message := statusMessage(body); message != "" {
		return message
	}
	if status := apierrors.APIStatus(nil); errors.As(err, &status) {
		if message := status.Status().Message; message != "" && !isPlaceholder(message) {
			return message
		}
		if reason := status.Status().Reason; reason != "" {
			return string(reason)
		}
	}
	return err.Error()
}

// client-go's placeholder when it could not decode an error response.
const unknownMessage = "unknown"

// isPlaceholder distinguishes client-go's stand-in from a real server message.
//
// It emits `unknown`, or `unknown (get pods)` when it knows the request that
// failed. Matched precisely rather than by prefix, because a genuine API server
// message can begin with the same word — `unknown field "spec.replicas"` on a
// rejected object is one — and discarding that would trade this defect for its
// mirror image.
func isPlaceholder(message string) bool {
	if message == unknownMessage {
		return true
	}
	return strings.HasPrefix(message, unknownMessage+" (") && strings.HasSuffix(message, ")")
}

// statusMessage reads the API server's own sentence out of an error body.
//
// Parsed with `encoding/json` into a minimal shape rather than decoded through
// the scheme: the scheme is what failed in the first place, and a Status has
// exactly one field worth reading here.
func statusMessage(body []byte) string {
	if len(body) == 0 {
		return ""
	}
	var status struct {
		Kind    string `json:"kind"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(body, &status); err != nil {
		return ""
	}
	if status.Kind != "Status" {
		return ""
	}
	return status.Message
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
