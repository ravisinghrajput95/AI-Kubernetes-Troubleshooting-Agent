package collectors

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	"k8s.io/client-go/rest"
)

// Asserted on the headers that reached the API server, never on the Collector.
//
// A header built correctly and never sent reads exactly like a working one from
// inside the process — the platform already shipped that defect once, with
// Loki's `X-Scope-OrgID` set on an object that was never handed to the client.
// So every test here runs a real HTTP server and inspects what arrived.
func capture(t *testing.T, impersonate bool, actor *agentv1.Impersonation) http.Header {
	t.Helper()

	var got http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"kind":"PodList","items":[]}`))
	}))
	defer server.Close()

	client, err := rest.RESTClientFor(&rest.Config{
		Host:    server.URL,
		APIPath: "/api",
		ContentConfig: rest.ContentConfig{
			GroupVersion:         &groupVersion,
			NegotiatedSerializer: codecs,
		},
	})
	if err != nil {
		t.Fatalf("RESTClientFor: %v", err)
	}

	record := New(client, "prod", impersonate).Collect(
		context.Background(),
		&agentv1.EvidenceSpec{
			Kind:       "k8s.pods",
			Target:     &agentv1.ResourceRef{Kind: "pods"},
			Parameters: map[string]string{"all_namespaces": "true"},
		},
		actor,
	)
	if record.GetStatus() != agentv1.EvidenceStatus_EVIDENCE_STATUS_OK {
		t.Fatalf("read failed: %s", record.GetDetail())
	}
	return got
}

func TestTheCallingUserReachesTheApiServer(t *testing.T) {
	// F13's guarantee: the platform cannot see more than the caller can. It
	// holds on the kubeconfig path because `kubectl --as` makes the API server
	// apply that user's RBAC. This is the same thing on the agent path, and
	// until now the actor arrived on the wire and was discarded.
	headers := capture(t, true, &agentv1.Impersonation{
		Username: "alice@acme.com",
		Groups:   []string{"sre", "oncall"},
	})

	if got := headers.Get("Impersonate-User"); got != "alice@acme.com" {
		t.Errorf("Impersonate-User = %q, want alice@acme.com", got)
	}
	groups := headers.Values("Impersonate-Group")
	if len(groups) != 2 || groups[0] != "sre" || groups[1] != "oncall" {
		t.Errorf("Impersonate-Group = %v, want [sre oncall]", groups)
	}
}

func TestGroupsAreSeparateHeadersNotOneJoinedString(t *testing.T) {
	// Kubernetes reads `Impersonate-Group` as a repeated header. Joining them
	// with a comma produces one group literally named "sre,oncall", which
	// matches no binding — so the read is refused and looks like the user
	// simply lacking access.
	headers := capture(t, true, &agentv1.Impersonation{
		Username: "alice@acme.com",
		Groups:   []string{"sre", "oncall"},
	})
	for _, value := range headers.Values("Impersonate-Group") {
		if len(value) > 0 && (value == "sre,oncall" || value == "sre, oncall") {
			t.Errorf("groups were joined into one header value: %q", value)
		}
	}
}

func TestAnAgentWithoutImpersonationSendsNoIdentity(t *testing.T) {
	// The compatibility path. An agent enrolled before this existed has a
	// ClusterRole without the impersonate verb; sending the header anyway would
	// have the API server refuse every read, and `app/kubernetes/access.py`
	// would report it as the *caller's* RBAC being too narrow — blaming the
	// user for the agent's missing grant.
	headers := capture(t, false, &agentv1.Impersonation{Username: "alice@acme.com"})

	if got := headers.Get("Impersonate-User"); got != "" {
		t.Errorf("Impersonate-User = %q on a non-impersonating agent", got)
	}
}

func TestNoActorMeansNoHeaderEvenWhenImpersonating(t *testing.T) {
	// `serve` refuses an unattributed request before it reaches here, so this
	// is defence in depth: nothing may quietly turn "nobody asked" into a read
	// as the agent's own broad-read ServiceAccount.
	for _, actor := range []*agentv1.Impersonation{nil, {}, {Username: ""}} {
		headers := capture(t, true, actor)
		if got := headers.Get("Impersonate-User"); got != "" {
			t.Errorf("Impersonate-User = %q for actor %v", got, actor)
		}
	}
}

func TestOneAgentsIdentityDoesNotLeakIntoTheNextRead(t *testing.T) {
	// One agent serves every caller through one shared client. An identity
	// applied to the *config* rather than to the request would be whichever
	// caller set it last, applied to whoever's read went out next — a
	// cross-user read with no error and no log line.
	first := capture(t, true, &agentv1.Impersonation{Username: "alice@acme.com"})
	second := capture(t, true, &agentv1.Impersonation{Username: "bob@acme.com"})

	if first.Get("Impersonate-User") != "alice@acme.com" {
		t.Fatalf("first read was not alice: %q", first.Get("Impersonate-User"))
	}
	if got := second.Get("Impersonate-User"); got != "bob@acme.com" {
		t.Errorf("second read carried %q, want bob@acme.com", got)
	}
}
