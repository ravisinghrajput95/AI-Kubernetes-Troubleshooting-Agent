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
- **Liveness and readiness are the same endpoint.** The platform has one
  `/health` and does not yet distinguish them (backlog item 42).
- **No graceful drain.** In-flight investigations fail on pod termination and
  are reaped by lease expiry (backlog item 43). See `docs/UPGRADE.md`.
- **No Terraform module.** Item 35 named "Terraform/Helm"; this is the Helm
  half. The Terraform half is the managed Postgres, Redis, DNS and secrets
  around it, which is provider-specific and not written.
