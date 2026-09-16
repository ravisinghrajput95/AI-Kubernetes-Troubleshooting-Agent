# Terraform

The managed Postgres, Redis, secret and DNS plumbing around
[`deploy/helm/k8s-agent`](../helm/k8s-agent), on AWS.

> **This has never been applied.** No RDS instance, ElastiCache group, Route 53
> record or Helm release has been created from it. It is validated, tested
> against mocked providers, and its values are rendered through the real chart
> in CI. None of that shows that AWS accepts these resources, that the pods can
> reach them, or that the platform starts against them. Read it as a reviewed
> starting point, not a deployment known to work.

| Path | What it is |
|---|---|
| `modules/platform-values/` | Helm values from infrastructure outputs. **No providers**, so it is the one part that runs for real anywhere. |
| `aws/` | RDS PostgreSQL 17, ElastiCache Redis, the `DATABASE_URL`/`REDIS_URL` Secret, the Helm release, and Route 53 CNAMEs for the console and the agent gateway. Does **not** create the VPC or the EKS cluster. |

## What has been run

```bash
python scripts/terraform_verify.py              # validate, test, render through the chart
python scripts/mutation_check.py --suite terraform
```

| Check | What it proves | What it does not |
|---|---|---|
| `terraform validate` | Both modules parse against the provider schemas (aws 6.x, kubernetes 2.38, helm 3.x) | Anything about a real account |
| `terraform test` on `platform-values` (10 runs) | The values each input produces, and that each refusal fires | — nothing is mocked here |
| `terraform test` on `aws` (9 runs) | The wiring: URLs have the scheme and TLS mode the platform needs and land in the Secret the chart mounts; state admits only the workload security group; encryption, `rds.force_ssl` and deletion protection are on; no credential appears in Helm values; records point at the load balancers the chart created | That AWS accepts any of it — every provider but `random` is mocked |
| Values → chart contract | Every key the module sets exists in the chart's `values.yaml` (Helm ignores an unknown key silently); `helm template` accepts the values; the rendered ConfigMap says what was asked, with `CORS_ORIGINS` read back by the platform's own `Settings` | That the release installs or becomes ready |
| Mutation pairs | Six defects, each confirmed to fail the test named for it | — |

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
