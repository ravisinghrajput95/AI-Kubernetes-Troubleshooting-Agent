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
| **`--kind` apply** | The release waits on readiness and becomes ready, so the platform connected with the URL `platform-release` formatted; every platform connection Postgres reports is **TLS**; a plaintext connection is **refused** (the control that makes the previous line mean something); `/health/ready` says both stores are ok; the token Secret authenticates and a wrong token is refused | `rediss://` — Redis here speaks plaintext, see below; anything AWS-specific |
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
- **`sslmode=require`, not `verify-full`.** The connection is encrypted and
  `rds.force_ssl` makes the server refuse plaintext, but the platform image
  carries no RDS CA bundle, so the server certificate is not verified. Redis
  uses `rediss://` and verifies against the image's system CAs, which is
  expected to trust ElastiCache's certificate and has not been observed doing so.
  The kind apply cannot close this: a self-signed Redis would be refused by that
  verification, and weakening it to pass would test a configuration nobody
  should ship — so there the URL is `redis://` with the password, and the
  `rediss` branch is covered only by the mocked test.
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
