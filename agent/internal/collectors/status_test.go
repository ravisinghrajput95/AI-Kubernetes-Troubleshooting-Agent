package collectors

import (
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
