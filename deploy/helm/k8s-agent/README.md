# k8s-agent Helm chart

Backlog item 35. Deploys the **platform**, not the cluster agent — customer
clusters get an agent from `POST /agents/enrolment` or `agentctl`, which returns
an apply-able manifest.

```bash
helm install k8s-agent deploy/helm/k8s-agent -f my-values.yaml
```

## Two things this chart deliberately does not do

**It does not bundle Postgres or Redis.** A subchart database is a database
nobody backs up, sized for a demo, holding the only copy of every customer's
incident history. Point `database.urlSecret` at a managed instance. There is no
in-cluster fallback, on purpose.

**It does not pre-set anything insecure.** `docker-compose.yml` shipped
`ALLOW_INSECURE_NO_AUTH` once — a `compose up` that published a port,
authenticated nobody, and supplied its own acknowledgement. A chart that does
the same has a longer reach. `helm install` with `auth.mode=disabled` and no
acknowledgement **fails at render time** and names the value to set.

## It refuses bad configurations at `helm template`, not at CrashLoopBackOff

The platform already validates its own configuration at startup and refuses to
boot on a bad one. That is the real control and this does not replace it — what
it adds is timing. Every refusal below is verified:

| Configuration | Result |
|---|---|
| `auth.mode=disabled` without `allowInsecureNoAuth` | refused |
| `auth.mode=oidc` without issuer or audience | refused |
| `auth.mode=token` without a secret name | refused |
| `auth.mode` not one of oidc/token/disabled | refused |
| `database` without `redis`, or vice versa | refused |
| `replicaCount > 1` with no shared state | refused |
| `autoscaling.enabled` with no shared state | refused |
| `tenancy.mode=shared` without a database | refused |
| `tenancy.mode=shared` with auth disabled | refused |
| `tenancy.mode=shared` with `rbac.defaultRole` above viewer | refused |
| `tenancy.mode=shared` + oidc without `tenantClaim` | refused |
| `tenancy.mode` not single/shared | refused |
| `tenancy.mode=shared` with no tenant rate limit | **warns** — a fairness gap, not unsafe |

The multi-replica check is the one worth calling out: without shared state every
worker keeps jobs in its own memory, so whether a poll finds an investigation
depends on which pod the load balancer picked. Nothing errors; results just go
missing about (N-1)/N of the time.

## Minimal production values

```yaml
replicaCount: 3

image:
  repository: ghcr.io/you/k8s-agent-backend
  tag: "1.0.0"

auth:
  mode: oidc
  oidc:
    issuer: https://example.okta.com/oauth2/default
    audience: api://k8s-agent
    roleMappings: "k8s-sre=admin,k8s-oncall=operator,k8s-eng=viewer"

rbac:
  defaultRole: viewer

database:
  urlSecret: {name: k8s-agent-db, key: DATABASE_URL}
redis:
  urlSecret: {name: k8s-agent-redis, key: REDIS_URL}

agentGateway:
  enabled: true
  port: 9443
  caSecret: {name: k8s-agent-ca, certKey: ca.crt, keyKey: ca.key}

metrics:
  serviceMonitor:
    enabled: true
    # Required by a default kube-prometheus-stack. See below.
    labels:
      release: kube-prometheus-stack

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: k8s-agent.example.com
      paths: [{path: /, pathType: Prefix}]
  tls:
    - secretName: k8s-agent-tls
      hosts: [k8s-agent.example.com]
```

Create the secrets out of band — never inline in values, where they land in
`helm get values` and your CI logs:

```bash
kubectl create secret generic k8s-agent-db --from-literal=DATABASE_URL='postgresql://…'
kubectl create secret generic k8s-agent-redis --from-literal=REDIS_URL='rediss://…'
kubectl create secret generic k8s-agent-ca --from-file=ca.crt --from-file=ca.key
```

## The kubeconfig identity needs `impersonate`

**The most likely way a first install fails**, and it was found by installing
this chart rather than by reading it.

Every cluster read runs as the *calling user* — that is what makes "the platform
cannot see more than you can" true. It requires the identity in your mounted
kubeconfig to hold the `impersonate` verb. Without it every read is refused and
every investigation fails with:

> No cluster read succeeded. Investigations run as the calling user, so this is
> usually their Kubernetes RBAC rather than a broken cluster.

That message is accurate and still sends you to the wrong place, because the
missing permission is the *platform's*, not the user's.

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: k8s-agent-impersonator}
rules:
  - apiGroups: [""]
    resources: ["users", "groups", "serviceaccounts"]
    verbs: ["impersonate"]
```

Bind it to whatever identity your kubeconfig authenticates as. Your **users**
then need their own read access — the platform grants them nothing.

Set `impersonateUsers: false` to turn it off, which makes every read run as the
kubeconfig's own identity. Correct only where that credential is already scoped
to exactly what its users may see.

Agent-reached clusters are unaffected: the agent's own ServiceAccount bounds
what can be read there.

## A ServiceMonitor nobody selects scrapes nothing, silently, forever

`metrics.serviceMonitor.enabled: true` is **not enough on a default
kube-prometheus-stack**, and the failure has no symptom: the object is created,
valid, and accepted, and no Prometheus ever looks at it.

kube-prometheus-stack ships `serviceMonitorSelectorNilUsesHelmValues: true`,
which makes its Prometheus select only ServiceMonitors labelled with its own
release name:

```yaml
# what the stack's Prometheus is actually configured with
serviceMonitorSelector:
  matchLabels:
    release: kube-prometheus-stack   # or whatever you called the release
```

So set the matching label:

```yaml
metrics:
  serviceMonitor:
    enabled: true
    labels:
      release: kube-prometheus-stack
```

Confirm it rather than assuming — the label is the release name, and yours may
differ:

```bash
kubectl -n monitoring get prometheus -o jsonpath='{.items[*].spec.serviceMonitorSelector}'
```

And confirm the result, because that is the only check that distinguishes a
working scrape from a plausible one:

```bash
# the target must exist AND be up. Zero targets is not "no unhealthy targets".
kubectl -n monitoring port-forward svc/prometheus-operated 9090 &
curl -s localhost:9090/api/v1/targets \
  | jq '.data.activeTargets[] | select(.scrapePool | test("k8s-agent"))
        | {scrapeUrl, health, lastError}'
```

This is exercised on every CI run — `deploy/verify/prometheus.yaml` reproduces
the stack's *restrictive* default rather than the permissive
`serviceMonitorSelector: {}` a harness reaches for when it wants to go green,
and the assertion fails if no target appears. See
`docs/INTEGRATION_VERIFICATION.md`.

## Sizing

Peak heap is about **5× the stored result**, measured flat across cluster sizes:
13.4 MB at the `MAX_LIST_ITEMS` ceiling, roughly **76 investigations per GB**.
With `jobMaxConcurrent: 4` the 2Gi default limit has ample headroom.

**Scale replicas, not `jobMaxConcurrent`.** The ceiling is per worker process —
scaling slots, agent processes and the Postgres pool each left throughput at
~12/s, while workers 1→2 gave 12.1 → 23.0/s, linear. A saturated worker samples
~92% idle with every non-idle sample in a socket wait, because one Python
process serialises HTTP, every agent's gRPC stream, the queue consumer and
analysis.

That is also why the HPA on CPU is offered but not recommended: CPU is not the
signal. Scale on `k8sagent_queue_depth` through an external metrics adapter if
you can.

## The agent gateway Service

Separate from the HTTP Service, because an agent stream is long-lived and the
worker holding the socket is the only one that can collect through it. Routing
an investigation to that worker is the platform's job; keeping the stream pinned
is the Service's. The enrolment listener is `port + 1` and requests no client
certificate — gRPC's Python bindings have no request-but-don't-require mode — so
a fleet that has finished enrolling can firewall it off.

## Known gaps

- **No `NetworkPolicy` template.** Egress rules depend on where your Postgres,
  Redis, OpenAI endpoint and notification destinations are, and a wrong default
  here silently breaks collection.
- **A rolling upgrade still drops the occasional request.** Measured over five
  rolling upgrades under load: 4 of 5 dropped requests before
  `/health/ready` + `preStop` were wired, 1 of 5 after. Raise
  `probes.preStopSeconds` if that matters to you; eliminating it entirely needs
  a load balancer that drains connections, not a longer sleep.
- **No Terraform module.** Item 35 named "Terraform/Helm"; this is the Helm
  half. The Terraform half is the managed Postgres, Redis, DNS and secrets
  around it, which is provider-specific and not written.
- **The `agentGateway` path is not in the CI verification job.** The chart is
  installed, upgraded and asserted against on every run, but kubeconfig-only:
  the gateway, mTLS enrolment and M8a routing were exercised by hand in §21 of
  the audit and would need a Go agent build and a second image in the job.
