# Terraform

The managed Postgres, Redis, secret and DNS plumbing around
[`deploy/helm/k8s-agent`](../helm/k8s-agent), on AWS.

> **The AWS half has never been applied.** No RDS instance, ElastiCache group,
> Route 53 record or EKS release has been created from it. **The Kubernetes
> half has** — `modules/platform-release`, the module the AWS root calls, is
> applied to kind in CI against a Postgres that refuses plaintext, and the
> platform it installs becomes ready and serves. Read the AWS resources as a
> reviewed starting point, not a deployment known to work.

| Path | What it is | Applied? |
|---|---|---|
| `modules/platform-values/` | Helm values from infrastructure coordinates. **No providers.** | yes, in every run |
| `modules/platform-release/` | The `DATABASE_URL`/`REDIS_URL` Secret, the values, and the Helm release, waiting on readiness | **yes, on kind** |
| `aws/` | RDS PostgreSQL 17, ElastiCache Redis, security groups, Route 53 CNAMEs; calls `platform-release`. Does **not** create the VPC or the EKS cluster. | no |
| `kind/` | `platform-release` beside an in-cluster Postgres (TLS only) and Redis (password required) | **yes** |

## What has been run

```bash
python scripts/terraform_verify.py                     # validate, test, render through the chart
python scripts/terraform_verify.py --kind \
    --kubeconfig ~/.kube/config --context kind-dev \
    --image k8s-agent-backend:tfverify                 # ...and apply the Kubernetes half
python scripts/mutation_check.py --suite terraform
```

| Check | What it proves | What it does not |
|---|---|---|
| `terraform validate` | All four roots and modules parse against the provider schemas | Anything about a real account |
| `terraform test` on `platform-values` (10 runs) | The values each input produces, and that each refusal fires | — nothing is mocked here |
| `terraform test` on `aws` (9 runs) | The wiring: `sslmode=require` and `rediss://` on the URLs, state admitting only the workload security group, encryption, `rds.force_ssl`, deletion protection, no credential in Helm values, records pointing at the chart's load balancers | That AWS accepts any of it — every provider but `random` is mocked |
| Values → chart contract | Every key set exists in the chart's `values.yaml` (Helm ignores unknown keys silently); `helm template` accepts the values; the ConfigMap says what was asked, `CORS_ORIGINS` read back through the platform's `Settings` | — |
| **`--kind` apply** | The release waits on readiness and becomes ready, so the platform connected with the URLs `platform-release` formatted; every platform connection Postgres reports is **TLS**; plaintext is **refused** by both Postgres and Redis (the controls that make TLS mean something); both stores **verify** — the mounted root connects, the system roots fail certificate verification, a wrong name fails the hostname check; `/health/ready` says both stores are ok; the token Secret authenticates and a wrong token is refused | Anything AWS-specific: RDS's real bundle, ElastiCache's certificate |
| Mutation pairs | Five defects under `terraform test`, one under pytest, each confirmed to fail the test named for it | — |

**The apply's wait was mutation-checked by hand**, because a wait that returns
early would make "became ready" meaningless: with `sslmode = "disable"` against
the TLS-only Postgres, the platform's pool timed out, the pods never became
ready, and the apply failed at its deadline with `context deadline exceeded`.

**Writing it found a chart defect.** The chart rendered `CORS_ORIGINS`
comma-joined, and the platform reads a list setting from the environment as
JSON, so *any* `config.corsOrigins` value failed startup. Nothing that installs
the chart set it. Fixed in the chart and pinned by `backend/tests/test_helm_chart.py`,
which renders the chart and hands the result to `Settings`.

## Using it

```hcl
module "k8s_agent" {
  source = "./deploy/terraform/aws"

  region                     = "eu-west-1"
  eks_cluster_name           = "prod"
  vpc_id                     = "vpc-..."
  private_subnet_ids         = ["subnet-...", "subnet-..."]
  workload_security_group_id = "sg-..."   # what the platform's pods carry

  auth = {
    mode          = "oidc"                  # required; there is no default
    oidc_issuer   = "https://idp.example.com"
    oidc_audience = "k8s-agent"
  }

  hostname        = "agent.example.com"
  route53_zone_id = "Z..."

  agent_gateway = {
    enabled        = true
    hostname       = "gateway.agent.example.com"
    ca_secret_name = "k8s-agent-ca"         # created by you; see below
  }
}
```

It needs the AWS Load Balancer Controller in the cluster: the ingress defaults to
the `alb` class and the gateway Service to an NLB.

## Decisions, and the gaps they leave

- **`auth.mode` has no default**, like `AUTH_MODE` and the chart's `auth.mode`.
  `disabled` needs `allow_insecure_no_auth = true` as well; `token` needs a
  Secret you create, so tokens never enter Terraform state.
- **The agent CA is not generated here.** Its private key is the one
  irreplaceable file (`docs/RUNBOOK_BACKUP_RESTORE.md`), and generating it in
  Terraform would put it in state. Create the Secret (`ca.crt`, `ca.key`) out of
  band. The gateway is refused without one: every replica would otherwise
  generate its own development CA, and an agent can verify at most one.
- **The database and Redis passwords are in state.** They are interpolated into
  the URLs the chart mounts. Use an encrypted remote backend with restricted
  access, or replace `kubernetes_secret_v1.state` with an External Secrets
  operator reading from Secrets Manager.
- **Postgres is `verify-full` against RDS's CA bundle, and that was a gap until
  the chart could mount one.** The module first shipped `sslmode=require`:
  encrypted, and any certificate for any name accepted, because the platform
  image carries no RDS root and the chart had no way to add one. The chart now
  takes `extraVolumes`/`extraVolumeMounts`; `platform-release` mounts a CA and
  names it in the URL (`sslrootcert=` for Postgres, `ssl_ca_certs=` for Redis);
  the AWS root downloads the bundle over `data "http"` (checked to be PEM — 108
  certificates when fetched on 2026-09-17). **On kind** both stores run on a
  private root, and from inside a platform pod the script requires the mounted
  root to connect, the system roots to fail *certificate verification*, and the
  right root addressed by IP to fail *on the name* — each refusal checked for
  its reason, since a DNS error also refuses. Hand-mutated to
  `sslmode = "require"`, the run fails naming the connection as unverified.
  **ElastiCache** uses `rediss://` against the system roots, which redis-py 8.1
  verifies — chain and hostname — by default; that the system roots trust
  ElastiCache's certificate has not been observed. **Applying found one defect
  the mocked tests could not**: the CA Secret's `count` depended on the PEM,
  unknown at plan when the certificate is generated in the same apply, and the
  kind root refused to plan. It is decided from plan-time values now.
- **`tenancy_mode = "shared"` needs a database role this module does not
  create.** Row-level security is inert for a role that bypasses it, and the
  platform refuses to start `shared` on such a role
  (`Database.assert_row_level_security_applies`). Whether the RDS master user
  is one has not been checked. Create an unprivileged role and point
  `DATABASE_URL` at it; this module cannot, because the database is private and
  Terraform's runner usually is not in the VPC.
- **Redis cluster mode stays off.** The platform uses one logical database,
  pub/sub, and multi-key operations a sharded keyspace would split.
- **The ALB idle timeout is raised to 600 s**, because the progress stream is
  held open for the length of an investigation. Not observed through a real ALB.
- **PostgreSQL 17 only.** It is the only major version the platform has been run
  against; the module refuses others until someone runs the
  backend-integration suite on them.
