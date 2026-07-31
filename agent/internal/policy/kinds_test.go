package policy

import (
	"strings"
	"testing"
)

// The agent's central security property. The platform names a kind of evidence
// this agent already knows how to collect; it cannot describe an operation.
// Enforced here rather than on the platform, because enforced there it would be
// a promise the customer cannot verify.
func TestAnUnknownKindIsRefused(t *testing.T) {
	for _, kind := range []string{
		"k8s.exec",
		"k8s.secrets.values",
		"",
		"k8s.pods; rm -rf /",
		"../../api/v1/pods",
	} {
		if Supports(kind) {
			t.Errorf("Supports(%q) = true, want false", kind)
		}
		if _, err := Resolve(kind, "default", "web", nil); err == nil {
			t.Errorf("Resolve(%q) succeeded, want a refusal", kind)
		}
	}
}

func TestEveryAdvertisedKindResolves(t *testing.T) {
	// An agent must not advertise a kind it then refuses: the platform plans
	// against `supported_kinds`, and a gap there becomes an unexplained gap in
	// an investigation.
	for _, kind := range SupportedKinds() {
		if !Supports(kind) {
			t.Errorf("advertised %q but Supports says no", kind)
		}
		if _, err := Resolve(kind, "default", "web", nil); err != nil {
			t.Errorf("advertised %q but Resolve failed: %v", kind, err)
		}
	}
}

func TestAHostileNameCannotEscapeThePath(t *testing.T) {
	// A name is data. It becomes one escaped path segment and can never
	// traverse out of the resource it belongs to.
	read, err := Resolve("k8s.pods", "default", "../../../secrets", nil)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if strings.Contains(read.Path, "../") {
		t.Errorf("path escaped: %q", read.Path)
	}
	if !strings.HasPrefix(read.Path, "/api/v1/namespaces/default/pods/") {
		t.Errorf("path left its resource: %q", read.Path)
	}
}

func TestAHostileNamespaceCannotEscapeThePath(t *testing.T) {
	read, err := Resolve("k8s.pods", "../../nodes", "web", nil)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if strings.Contains(read.Path, "../") {
		t.Errorf("path escaped: %q", read.Path)
	}
}

func TestSelectorsTravelAsQueryNotAsPath(t *testing.T) {
	read, err := Resolve("k8s.pods", "default", "", map[string]string{
		"label_selector": "app=web",
	})
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if got := read.Query.Get("labelSelector"); got != "app=web" {
		t.Errorf("labelSelector = %q, want app=web", got)
	}
	if strings.Contains(read.Path, "app=web") {
		t.Errorf("a selector reached the path: %q", read.Path)
	}
}

func TestClusterScopedResourcesIgnoreANamespace(t *testing.T) {
	read, err := Resolve("k8s.nodes", "default", "", nil)
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if strings.Contains(read.Path, "namespaces") {
		t.Errorf("nodes were namespaced: %q", read.Path)
	}
}

func TestTheEquivalentCommandIsRecordedNotExecuted(t *testing.T) {
	// The agent never runs kubectl. It records what would have produced the
	// same bytes, so a human can reproduce a remote read by hand.
	read, err := Resolve("k8s.pods", "", "", map[string]string{"all_namespaces": "true"})
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	want := "kubectl get pods -A -o json"
	if read.EquivalentCommand != want {
		t.Errorf("EquivalentCommand = %q, want %q", read.EquivalentCommand, want)
	}
}

func TestALogReadNeedsAPod(t *testing.T) {
	if _, err := Resolve("k8s.logs", "default", "", nil); err == nil {
		t.Error("a log read without a pod name succeeded, want a refusal")
	}
	read, err := Resolve("k8s.logs", "default", "web", map[string]string{"previous": "true"})
	if err != nil {
		t.Fatalf("Resolve: %v", err)
	}
	if !read.Text {
		t.Error("a log read should be marked as text")
	}
	if read.Query.Get("previous") != "true" {
		t.Error("previous did not reach the query")
	}
}
