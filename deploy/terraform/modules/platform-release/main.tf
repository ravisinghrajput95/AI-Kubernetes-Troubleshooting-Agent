# The Kubernetes half of a deployment: the Secret holding DATABASE_URL and
# REDIS_URL, the chart values, and the Helm release.
#
# **It is its own module so that the half which can be applied here is the
# half that is.** Cloud roots (`aws/`) create state and pass its coordinates in;
# the `kind/` root passes coordinates of a Postgres and Redis it runs in-cluster
# and is applied by `scripts/terraform_verify.py --kind`. Both format their URLs
# and install their release through this file, so a URL the platform cannot
# parse, or a release that never becomes ready, fails on a laptop rather than in
# an account.
#
# The caller configures the kubernetes and helm providers.

terraform {
  required_version = ">= 1.9"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}

locals {
  trust_path = "/etc/k8s-agent/trust"

  # verify-full against the mounted root when one is given, and against the
  # system store (libpq 16+'s `sslrootcert=system`) when a verify mode is asked
  # for without one. `require` encrypts and verifies nothing, which is why a CA
  # can be supplied at all.
  # Whether this module writes the CA Secret is decided from values known at
  # plan — the verify mode and whether a Secret was named — never from the PEM,
  # which is unknown when its certificate is generated in the same apply. The
  # first version counted on the PEM and the kind apply refused to plan
  # ("Invalid count argument"); the mocked AWS test could not see it, because
  # there the bundle comes from a data source read at plan time.
  database_verifies      = startswith(nonsensitive(var.database.sslmode), "verify-")
  database_ca_secret_ref = nonsensitive(var.database.ca_secret_name)
  database_ca_managed    = local.database_verifies && local.database_ca_secret_ref == ""
  database_ca_secret     = local.database_ca_managed ? "${var.name}-database-ca" : local.database_ca_secret_ref
  database_ca = (
    local.database_ca_secret_ref != "" || var.database.ca_pem != ""
    ? "${local.trust_path}/database/ca.crt"
    : "system"
  )
  database_url = format(
    "postgresql://%s:%s@%s:%d/%s?sslmode=%s%s",
    urlencode(var.database.username),
    urlencode(var.database.password),
    var.database.host,
    var.database.port,
    var.database.name,
    var.database.sslmode,
    startswith(var.database.sslmode, "verify-") ? "&sslrootcert=${local.database_ca}" : "",
  )

  # The token is the password, with no username — ElastiCache's shape, and
  # plain Redis `requirepass` reads the same. redis-py verifies the chain and
  # the hostname by default; `ssl_ca_certs` only chooses the root.
  redis_url = format(
    "%s://:%s@%s:%d/0%s",
    var.redis.tls ? "rediss" : "redis",
    urlencode(var.redis.auth_token),
    var.redis.host,
    var.redis.port,
    var.redis.tls && var.redis.ca_secret_name != "" ? "?ssl_cert_reqs=required&ssl_ca_certs=${local.trust_path}/redis/ca.crt" : "",
  )

  # A Secret's *name* is not a credential, and without unwrapping it the chart
  # values — which must stay readable with `helm get values` — become sensitive.
  trust = [
    for role, secret in {
      database = local.database_ca_secret
      redis    = nonsensitive(var.redis.ca_secret_name)
    } : { role = role, secret = secret } if secret != ""
  ]

  namespace = var.create_namespace ? kubernetes_namespace_v1.this[0].metadata[0].name : var.namespace
}

resource "kubernetes_namespace_v1" "this" {
  count = var.create_namespace ? 1 : 0
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_secret_v1" "state" {
  metadata {
    name      = "${var.name}-state"
    namespace = local.namespace
  }
  data = {
    DATABASE_URL = local.database_url
    REDIS_URL    = local.redis_url
  }
}

resource "kubernetes_secret_v1" "database_ca" {
  count = local.database_ca_managed ? 1 : 0
  metadata {
    name      = local.database_ca_secret
    namespace = local.namespace
  }
  data = {
    "ca.crt" = var.database.ca_pem
  }
}

module "values" {
  source = "../platform-values"

  fullname                   = var.name
  auth                       = var.platform.auth
  tenancy_mode               = var.platform.tenancy_mode
  rbac_default_role          = var.platform.rbac_default_role
  state_secret_name          = kubernetes_secret_v1.state.metadata[0].name
  replica_count              = var.platform.replica_count
  image_repository           = var.platform.image_repository
  image_tag                  = var.platform.image_tag
  openai_api_key_secret_name = var.platform.openai_api_key_secret_name
  hostname                   = var.platform.hostname
  ingress_class_name         = var.platform.ingress_class_name
  ingress_annotations        = var.platform.ingress_annotations
  ingress_tls_secret_name    = var.platform.ingress_tls_secret_name
  agent_gateway              = var.platform.agent_gateway

  extra_volumes = [
    for item in local.trust : { name = "trust-${item.role}", secret = { secretName = item.secret } }
  ]
  extra_volume_mounts = [
    for item in local.trust : {
      name      = "trust-${item.role}"
      mountPath = "${local.trust_path}/${item.role}"
      readOnly  = true
    }
  ]
}

resource "helm_release" "this" {
  name      = var.name
  namespace = local.namespace
  chart     = var.chart_path
  values    = [module.values.values_yaml]

  depends_on = [kubernetes_secret_v1.database_ca]

  # Readiness consults Postgres, so a release that waits is a release whose
  # database is reachable, with credentials that work, from inside the pods —
  # the one thing a plan cannot show.
  wait    = true
  timeout = var.timeout_seconds
}
