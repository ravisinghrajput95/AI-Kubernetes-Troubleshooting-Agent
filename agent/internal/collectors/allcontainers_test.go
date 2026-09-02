package collectors

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	agentv1 "github.com/ravisinghrajput95/ai-kubernetes-agent/agent/gen/agentv1"
	"k8s.io/client-go/rest"
)

// F24. The API server serves one container per log read and answers a
// multi-container pod that names none with `BadRequest: a container name must
// be specified`. kubectl hides that by expanding `--all-containers` on the
// client; the agent did not expand it at all, so every pod with a sidecar lost
// its logs entirely on the agent path while the same pod read through a
// kubeconfig kept them.
//
// Asserted on the requests that reached the API server, for the same reason the
// impersonation tests are: an expansion computed correctly and never issued
// reads identically to a working one from inside the process.

type logServer struct {
	// requests records every path+container that arrived, in order.
	requests []string
	// failing names a container whose log read returns a 400 with a message,
	// so the "first error wins" behaviour can be checked. Empty means no
	// container fails — it must not match the unnamed read a non-expanded
	// request makes, which is what the first version of this fixture did.
	failing string
	logs    map[string]string
	pod     string
}

func (s *logServer) start(t *testing.T) rest.Interface {
	t.Helper()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/log") {
			container := r.URL.Query().Get("container")
			s.requests = append(s.requests, "log:"+container+":previous="+r.URL.Query().Get("previous"))
			if s.failing != "" && container == s.failing {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusBadRequest)
				body, _ := json.Marshal(map[string]any{
					"kind": "Status", "status": "Failure",
					"message": `previous terminated container "` + container + `" in pod "web" not found`,
					"code":    400,
				})
				_, _ = w.Write(body)
				return
			}
			w.Header().Set("Content-Type", "text/plain")
			_, _ = w.Write([]byte(s.logs[container]))
			return
		}
		s.requests = append(s.requests, "pod")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(s.pod))
	}))
	t.Cleanup(server.Close)

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
	return client
}

func logSpec(parameters map[string]string) *agentv1.EvidenceSpec {
	namespace := "payments"
	return &agentv1.EvidenceSpec{
		Kind:       "k8s.logs",
		Target:     &agentv1.ResourceRef{Kind: "Pod", Name: "web", Namespace: &namespace},
		Parameters: parameters,
	}
}

// An init container and two regular ones, one of which says nothing.
const sidecarPod = `{"spec":{
  "initContainers":[{"name":"setup"}],
  "containers":[{"name":"alpha"},{"name":"quiet"}]
}}`

func TestEveryContainersLogsAreRead(t *testing.T) {
	server := &logServer{
		pod:  sidecarPod,
		logs: map[string]string{"setup": "INIT-SPEAKS\n", "alpha": "ALPHA-SPEAKS\n", "quiet": ""},
	}
	record := New(server.start(t), "prod", false).Collect(
		context.Background(),
		logSpec(map[string]string{"all_containers": "true", "tail": "200"}),
		nil,
	)

	if record.GetStatus() != agentv1.EvidenceStatus_EVIDENCE_STATUS_OK {
		t.Fatalf("status = %v, detail = %q", record.GetStatus(), record.GetDetail())
	}

	var payload struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(record.GetPayload(), &payload); err != nil {
		t.Fatalf("payload: %v", err)
	}

	// kubectl's order: init containers first, then regular. A container that
	// logged nothing contributes nothing and is not an error.
	if payload.Text != "INIT-SPEAKS\nALPHA-SPEAKS\n" {
		t.Errorf("logs = %q, want the init container's output then alpha's", payload.Text)
	}

	want := []string{"pod", "log:setup:previous=", "log:alpha:previous=", "log:quiet:previous="}
	if strings.Join(server.requests, ",") != strings.Join(want, ",") {
		t.Errorf("requests = %v, want %v", server.requests, want)
	}
}

func TestWithoutAllContainersNoPodIsRead(t *testing.T) {
	// The control. Without it the test above passes for an agent that reads the
	// pod on every log request, which would be a different defect wearing the
	// same green.
	server := &logServer{pod: sidecarPod, logs: map[string]string{"": "DEFAULT\n"}}
	record := New(server.start(t), "prod", false).Collect(
		context.Background(),
		logSpec(map[string]string{"tail": "200"}),
		nil,
	)

	if record.GetStatus() != agentv1.EvidenceStatus_EVIDENCE_STATUS_OK {
		t.Fatalf("status = %v, detail = %q", record.GetStatus(), record.GetDetail())
	}
	if len(server.requests) != 1 || server.requests[0] != "log::previous=" {
		t.Errorf("requests = %v, want exactly one log read and no pod read", server.requests)
	}
}

func TestPreviousIsCarriedOntoEveryContainer(t *testing.T) {
	// `PodPreviousLogsCollector` sends `previous` and `all_containers`
	// together. Expanding the second while dropping the first would serve the
	// current container under a record named for the previous one — the defect
	// this expansion sits next to.
	server := &logServer{
		pod:  sidecarPod,
		logs: map[string]string{"setup": "", "alpha": "OLD\n", "quiet": ""},
	}
	New(server.start(t), "prod", false).Collect(
		context.Background(),
		logSpec(map[string]string{"all_containers": "true", "previous": "true"}),
		nil,
	)

	for _, request := range server.requests {
		if strings.HasPrefix(request, "log:") && !strings.HasSuffix(request, "previous=true") {
			t.Errorf("%q did not ask for the previous container", request)
		}
	}
}

func TestTheFirstContainersErrorIsTheReadsError(t *testing.T) {
	// The platform maps "previous terminated"/"not found" onto EMPTY, which it
	// can only do if the API server's sentence reaches it unchanged. A
	// generic failure here would turn "this pod has never restarted" into
	// "this cluster could not be read".
	server := &logServer{
		pod:     sidecarPod,
		failing: "setup",
		logs:    map[string]string{"alpha": "ALPHA\n"},
	}
	record := New(server.start(t), "prod", false).Collect(
		context.Background(),
		logSpec(map[string]string{"all_containers": "true", "previous": "true"}),
		nil,
	)

	if record.GetStatus() == agentv1.EvidenceStatus_EVIDENCE_STATUS_OK {
		t.Fatal("a failing container was reported as a successful read")
	}
	if !strings.Contains(record.GetDetail(), "previous terminated container") {
		t.Errorf("detail = %q, want the API server's own sentence", record.GetDetail())
	}
}

func TestAPodWithNoContainersIsNotASuccessfulEmptyRead(t *testing.T) {
	// An empty payload with status OK would read as "we looked and this pod
	// says nothing", which is the inflated-completeness failure the 404-to-EMPTY
	// mapping already cost once.
	server := &logServer{pod: `{"spec":{}}`, logs: map[string]string{}}
	record := New(server.start(t), "prod", false).Collect(
		context.Background(),
		logSpec(map[string]string{"all_containers": "true"}),
		nil,
	)

	if record.GetStatus() == agentv1.EvidenceStatus_EVIDENCE_STATUS_OK {
		t.Errorf("status = OK for a pod reporting no containers; detail = %q", record.GetDetail())
	}
}
