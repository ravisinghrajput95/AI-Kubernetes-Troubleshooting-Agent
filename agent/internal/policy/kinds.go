// Package policy decides what this agent will do, independently of what it is
// asked to do.
//
// This is a security control, not validation, and it is the reason the agent
// exists as a separate process at all. Enforced only on the platform, the
// read-only guarantee would be a promise the customer cannot verify; enforced
// here, a compromised platform, a hostile tenant, or a bug cannot mutate a
// cluster because there is no code path that would.
package policy

import (
	"fmt"
	"net/url"
	"strings"
)

// Read is a resolved API read: a path and a query, and nothing else. There is
// no field here in which a verb, a body, or a shell fragment can travel.
type Read struct {
	// Path under the API server root, already escaped.
	Path string
	// Query parameters, already escaped.
	Query url.Values
	// The kubectl invocation that would produce the same bytes. Retained for
	// the human audit trail; it is a record of what happened and is never
	// executed.
	EquivalentCommand string
	// True when the response is plain text rather than JSON (logs).
	Text bool
	// True when the read names a single object, so a 404 means *that object*
	// is gone. On a list read a 404 means the API itself is not served here,
	// which is a different fact and must not be reported as an empty result.
	// See `statusFor` in internal/collectors.
	Named bool
}

// resource describes how one evidence kind maps onto the API.
type resource struct {
	// API group path: "api/v1" or "apis/apps/v1".
	group string
	// Plural resource name in the API.
	plural string
	// kubectl's name for it, used only to build the equivalent command.
	kubectl string
	// Cluster-scoped resources ignore a namespace.
	clusterScoped bool
	// kubectl sub-command in the equivalent-command record. Empty means "get";
	// metrics are read from an API group but surfaced to humans as `top`,
	// because that is the command they would run to reproduce it.
	verb string
}

// The complete set of evidence kinds this agent will serve.
//
// A kind that is not in this table is refused, not interpreted. Adding one is a
// deliberate act that ships in an agent release — which is exactly the cost
// ADR-002 accepts in exchange for the platform being unable to invent work.
var kinds = map[string]resource{
	"k8s.pods":            {group: "api/v1", plural: "pods", kubectl: "pods"},
	"k8s.events":          {group: "api/v1", plural: "events", kubectl: "events"},
	"k8s.services":        {group: "api/v1", plural: "services", kubectl: "services"},
	"k8s.endpoints":       {group: "api/v1", plural: "endpoints", kubectl: "endpoints"},
	"k8s.configmaps":      {group: "api/v1", plural: "configmaps", kubectl: "configmaps"},
	"k8s.pvc":             {group: "api/v1", plural: "persistentvolumeclaims", kubectl: "pvc"},
	"k8s.namespaces":      {group: "api/v1", plural: "namespaces", kubectl: "namespaces", clusterScoped: true},
	"k8s.nodes":           {group: "api/v1", plural: "nodes", kubectl: "nodes", clusterScoped: true},
	"k8s.pv":              {group: "api/v1", plural: "persistentvolumes", kubectl: "pv", clusterScoped: true},
	"k8s.deployments":     {group: "apis/apps/v1", plural: "deployments", kubectl: "deployments"},
	"k8s.statefulsets":    {group: "apis/apps/v1", plural: "statefulsets", kubectl: "statefulsets"},
	"k8s.daemonsets":      {group: "apis/apps/v1", plural: "daemonsets", kubectl: "daemonsets"},
	"k8s.jobs":            {group: "apis/batch/v1", plural: "jobs", kubectl: "jobs"},
	"k8s.cronjobs":        {group: "apis/batch/v1", plural: "cronjobs", kubectl: "cronjobs"},
	"k8s.ingress":         {group: "apis/networking.k8s.io/v1", plural: "ingresses", kubectl: "ingress"},
	"k8s.networkpolicies": {group: "apis/networking.k8s.io/v1", plural: "networkpolicies", kubectl: "networkpolicies"},

	// The deep-investigation reads. Every one of these was collected by the
	// kubeconfig path and refused by this agent, so an agent-reached cluster
	// silently produced a shallower investigation than the same cluster read
	// locally — a partial view presented as a whole one, which is the outcome
	// the evidence layer exists to prevent. Held by
	// backend/tests/test_provider_parity.py, which derives the required set
	// from the collectors rather than from this list.
	"k8s.serviceaccounts":   {group: "api/v1", plural: "serviceaccounts", kubectl: "serviceaccount"},
	"k8s.resourcequotas":    {group: "api/v1", plural: "resourcequotas", kubectl: "resourcequotas"},
	"k8s.limitranges":       {group: "api/v1", plural: "limitranges", kubectl: "limitranges"},
	"k8s.endpointslices":    {group: "apis/discovery.k8s.io/v1", plural: "endpointslices", kubectl: "endpointslices"},
	"k8s.storageclasses":    {group: "apis/storage.k8s.io/v1", plural: "storageclasses", kubectl: "storageclasses", clusterScoped: true},
	"k8s.volumeattachments": {group: "apis/storage.k8s.io/v1", plural: "volumeattachments", kubectl: "volumeattachments", clusterScoped: true},

	// metrics-server. Frequently absent, which is a normal degradation: the
	// read fails, the platform records unavailable evidence, and a diagnosis
	// states that metrics were not consulted rather than assuming health.
	"k8s.metrics.nodes": {
		group: "apis/metrics.k8s.io/v1beta1", plural: "nodes", kubectl: "nodes",
		clusterScoped: true, verb: "top",
	},
	"k8s.metrics.pods": {
		group: "apis/metrics.k8s.io/v1beta1", plural: "pods", kubectl: "pods", verb: "top",
	},
}

// SupportedKinds is what the agent advertises in its hello, so the platform can
// plan against this agent rather than assume a uniform fleet.
func SupportedKinds() []string {
	out := make([]string, 0, len(kinds)+1)
	for kind := range kinds {
		out = append(out, kind)
	}
	out = append(out, "k8s.logs")
	return out
}

// Supports reports whether this agent will serve a kind at all.
func Supports(kind string) bool {
	if kind == "k8s.logs" {
		return true
	}
	_, ok := kinds[kind]
	return ok
}

// Resolve turns an evidence kind and its target into an API read.
//
// Every branch here builds a path from a table entry and escapes the caller's
// values into it. No caller-supplied string ever becomes a path segment
// unescaped, and none of them can select a verb.
func Resolve(kind, namespace, name string, parameters map[string]string) (Read, error) {
	if kind == "k8s.logs" {
		return resolveLogs(namespace, name, parameters)
	}

	entry, ok := kinds[kind]
	if !ok {
		return Read{}, fmt.Errorf("unknown evidence kind %q", kind)
	}

	allNamespaces := parameters["all_namespaces"] == "true"
	path := "/" + entry.group
	scope := "cluster"

	if !entry.clusterScoped && !allNamespaces && namespace != "" {
		path += "/namespaces/" + url.PathEscape(namespace)
		scope = "-n " + namespace
	} else if !entry.clusterScoped && allNamespaces {
		scope = "-A"
	}

	path += "/" + entry.plural
	named := name != ""
	if named {
		path += "/" + url.PathEscape(name)
	}

	query := url.Values{}
	if selector := parameters["label_selector"]; selector != "" {
		query.Set("labelSelector", selector)
	}
	if selector := parameters["field_selector"]; selector != "" {
		query.Set("fieldSelector", selector)
	}
	if limit := parameters["limit"]; limit != "" {
		query.Set("limit", limit)
	}

	return Read{
		Path:              path,
		Query:             query,
		EquivalentCommand: command(entry, name, scope, query),
		Named:             named,
	}, nil
}

func resolveLogs(namespace, name string, parameters map[string]string) (Read, error) {
	if name == "" || namespace == "" {
		return Read{}, fmt.Errorf("a log read needs a namespace and a pod name")
	}

	path := "/api/v1/namespaces/" + url.PathEscape(namespace) + "/pods/" + url.PathEscape(name) + "/log"
	query := url.Values{}
	extra := []string{}

	if container := parameters["container"]; container != "" {
		query.Set("container", container)
		extra = append(extra, "-c "+container)
	}
	if tail := parameters["tail"]; tail != "" {
		query.Set("tailLines", tail)
		extra = append(extra, "--tail="+tail)
	}
	if parameters["previous"] == "true" {
		query.Set("previous", "true")
		extra = append(extra, "--previous")
	}
	// Recorded, never queried. The API server has no `all_containers`
	// parameter — kubectl expands the flag client-side into one read per
	// container — so this belongs in the human-readable command and nowhere
	// else. `AllContainers` below is what actually drives the expansion.
	if parameters["all_containers"] == "true" {
		extra = append(extra, "--all-containers=true")
	}

	suffix := ""
	if len(extra) > 0 {
		suffix = " " + strings.Join(extra, " ")
	}

	return Read{
		Named:             true,
		Path:              path,
		Query:             query,
		EquivalentCommand: fmt.Sprintf("kubectl logs %s -n %s%s", name, namespace, suffix),
		Text:              true,
	}, nil
}

// AllContainers reports whether a log read covers every container in the pod.
//
// The API server serves one container per read and answers a multi-container
// pod with `BadRequest: a container name must be specified` when none is
// named. kubectl hides that by expanding `--all-containers` on the client: it
// reads the pod and fetches one log per container. The agent did not expand it
// at all, so **every pod with a sidecar lost its logs entirely on the agent
// path** while the same pod read through a kubeconfig kept them (F24).
//
// The decision lives here rather than in the collector because policy is what
// decides what a read means. The collector performs the expansion, and each
// read it issues is resolved back through `Resolve` — so the expansion cannot
// reach any path this package would not have allowed on its own.
func AllContainers(kind string, parameters map[string]string) bool {
	return kind == "k8s.logs" && parameters["all_containers"] == "true"
}

func command(entry resource, name, scope string, query url.Values) string {
	verb := entry.verb
	if verb == "" {
		verb = "get"
	}

	parts := []string{"kubectl", verb, entry.kubectl}
	if name != "" {
		parts = append(parts, name)
	}
	if scope != "cluster" {
		parts = append(parts, scope)
	}
	if selector := query.Get("labelSelector"); selector != "" {
		parts = append(parts, "-l", selector)
	}
	if selector := query.Get("fieldSelector"); selector != "" {
		parts = append(parts, "--field-selector", selector)
	}
	if verb == "get" {
		// `kubectl top` has no -o json; saying it did would make the audit
		// trail a command that fails when a human runs it.
		parts = append(parts, "-o", "json")
	}
	return strings.Join(parts, " ")
}
