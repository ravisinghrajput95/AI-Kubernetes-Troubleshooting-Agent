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
  database_url = format(
    "postgresql://%s:%s@%s:%d/%s?sslmode=%s",
    urlencode(var.database.username),
    urlencode(var.database.password),
    var.database.host,
    var.database.port,
    var.database.name,
    var.database.sslmode,
  )

  # The token is the password, with no username — ElastiCache's shape, and
  # plain Redis `requirepass` reads the same.
  redis_url = format(
    "%s://:%s@%s:%d/0",
    var.redis.tls ? "rediss" : "redis",
    urlencode(var.redis.auth_token),
    var.redis.host,
    var.redis.port,
  )

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
}

resource "helm_release" "this" {
  name      = var.name
  namespace = local.namespace
  chart     = var.chart_path
  values    = [module.values.values_yaml]

  # Readiness consults Postgres, so a release that waits is a release whose
  # database is reachable, with credentials that work, from inside the pods —
  # the one thing a plan cannot show.
  wait    = true
  timeout = var.timeout_seconds
}
