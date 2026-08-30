package collectors

import (
	"strings"
	"testing"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// The defect this file exists for: a 404 on a *list* read is not an empty
// result. metrics-server is absent from most clusters, so the agent 404s on
// apis/metrics.k8s.io/v1beta1/nodes; reporting that as EMPTY — a status the
// platform counts as *usable* — makes an uninstalled metrics-server read as an
// idle cluster, raises evidence completeness, and raises the confidence of a
// diagnosis that saw less than the kubeconfig path would have.
//
// Found by running backend/tests/test_agent_transport.py against a real
// cluster with no metrics-server: the agent said usable, kubectl said
// unavailable, for the same cluster at the same moment.
func TestANotFoundOnAListReadIsUnavailableNotEmpty(t *testing.T) {
	notFound := apierrors.NewNotFound(schema.GroupResource{Resource: "nodes"}, "")

	if got := statusFor(notFound, false); got != agentv1.EvidenceStatus_EVIDENCE_STATUS_UNAVAILABLE {
		t.Errorf("list 404 = %v, want UNAVAILABLE", got)
	}
	if got := statusFor(notFound, true); got != agentv1.EvidenceStatus_EVIDENCE_STATUS_EMPTY {
		t.Errorf("named 404 = %v, want EMPTY", got)
	}
}

func TestTheOtherStatusesDoNotDependOnNaming(t *testing.T) {
	forbidden := apierrors.NewForbidden(schema.GroupResource{Resource: "pods"}, "web", nil)
	timeout := apierrors.NewTimeoutError("too slow", 1)
	other := apierrors.NewInternalError(errBoom{})

	for _, named := range []bool{true, false} {
		if got := statusFor(forbidden, named); got != agentv1.EvidenceStatus_EVIDENCE_STATUS_FORBIDDEN {
			t.Errorf("forbidden(named=%v) = %v", named, got)
		}
		if got := statusFor(timeout, named); got != agentv1.EvidenceStatus_EVIDENCE_STATUS_TIMEOUT {
			t.Errorf("timeout(named=%v) = %v", named, got)
		}
		if got := statusFor(other, named); got != agentv1.EvidenceStatus_EVIDENCE_STATUS_FAILED {
			t.Errorf("internal(named=%v) = %v", named, got)
		}
	}
}

type errBoom struct{}

func (errBoom) Error() string { return "boom" }

// A refused read must say who was refused.
//
// client-go reports "unknown" for every error on a raw request — the agent
// reads raw on purpose, so this is the normal path, not an edge case — while
// the API server's own sentence sits in the response body DoRaw hands back.
// Losing it makes an investigation degraded by one user's narrow RBAC
// indistinguishable from one degraded by a broken cluster, which is the single
// distinction app/kubernetes/access.py exists to draw.
func TestARefusalCarriesTheApiServersOwnSentence(t *testing.T) {
	body := []byte(`{"kind":"Status","status":"Failure","message":` +
		`"pods is forbidden: User \"alice@acme.com\" cannot list resource \"pods\"",` +
		`"reason":"Forbidden","code":403}`)

	got := detailFor(apierrors.NewGenericServerResponse(
		403, "get", schema.GroupResource{Resource: "pods"}, "", "unknown", 0, false), body)

	if !strings.Contains(got, "alice@acme.com") {
		t.Errorf("detail = %q, want the API server's message naming the user", got)
	}
	if got == "unknown" {
		t.Error("detail is client-go's placeholder; the body was not read")
	}
}

func TestAnUnreadableBodyFallsBackRatherThanInventing(t *testing.T) {
	err := apierrors.NewForbidden(schema.GroupResource{Resource: "pods"}, "web", errBoom{})

	for _, body := range [][]byte{nil, []byte(""), []byte("not json")} {
		got := detailFor(err, body)
		if got == "" {
			t.Errorf("empty detail for body %q", body)
		}
	}
}

func TestOnlyAStatusBodyIsReadAsARefusal(t *testing.T) {
	// A body that is valid JSON and carries a `message` but is not a Status —
	// a proxy's error envelope, an admission webhook's response, a partial
	// object. Reading `message` off anything would put an unrelated sentence in
	// the evidence record and present it as the API server's reason.
	err := apierrors.NewForbidden(schema.GroupResource{Resource: "pods"}, "web", errBoom{})
	body := []byte(`{"kind":"PodList","items":[],"message":"upstream connect error"}`)

	if got := detailFor(err, body); strings.Contains(got, "upstream connect error") {
		t.Errorf("a non-Status body was read as a refusal: %q", got)
	}
}

func TestClientGosPlaceholderIsNeverTheDetail(t *testing.T) {
	// The fallback chain's own trap. With no usable body, `Status().Message` is
	// the literal string "unknown" on every raw-request error — returning it
	// would put client-go's placeholder in the evidence record, which is the
	// defect this whole function exists to remove rather than relocate.
	err := apierrors.NewGenericServerResponse(
		403, "get", schema.GroupResource{Resource: "pods"}, "", "unknown", 0, false)

	got := detailFor(err, nil)
	if got == "unknown" {
		t.Errorf("detail = %q; the placeholder was passed straight through", got)
	}
	if !strings.EqualFold(got, "Forbidden") {
		t.Errorf("detail = %q, want the reason when there is no message", got)
	}
}

func TestARealMessageBeginningWithUnknownIsKept(t *testing.T) {
	// The mirror of the defect above. `unknown field "spec.replicas"` is a real
	// API server sentence; matching the placeholder by prefix would throw it
	// away and report "BadRequest" instead of the field that was wrong.
	err := apierrors.NewBadRequest(`unknown field "spec.replicas"`)

	if got := detailFor(err, nil); !strings.Contains(got, "spec.replicas") {
		t.Errorf("detail = %q, want the server's own message", got)
	}
}
