variable "region" {
  type = string
}

variable "eks_cluster_name" {
  description = "The EKS cluster the platform runs in. This module does not create it."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  description = "Subnets for the database and Redis. At least two, in different availability zones."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "RDS and ElastiCache subnet groups need at least two subnets in different availability zones."
  }
}

variable "workload_security_group_id" {
  description = <<-EOT
    The security group the platform's pods carry — the EKS cluster security
    group, or a security group for pods. Postgres and Redis admit this group
    and nothing else.
  EOT
  type        = string
}

variable "name" {
  description = "Prefix for every AWS resource, and the Helm release name."
  type        = string
  default     = "k8s-agent"
}

variable "namespace" {
  type    = string
  default = "k8s-agent"
}

variable "create_namespace" {
  type    = bool
  default = true
}

variable "chart_path" {
  description = "The chart this module installs. Defaults to the one in this repository."
  type        = string
  default     = null
}

# --- Platform -----------------------------------------------------------------

variable "auth" {
  description = "Passed to the platform-values module; see its variables.tf. mode has no default."
  type = object({
    mode                   = string
    oidc_issuer            = optional(string, "")
    oidc_audience          = optional(string, "")
    oidc_tenant_claim      = optional(string, "")
    oidc_role_mappings     = optional(string, "")
    tokens_secret_name     = optional(string, "")
    allow_insecure_no_auth = optional(bool, false)
  })
}

variable "tenancy_mode" {
  type    = string
  default = "single"
}

variable "rbac_default_role" {
  type    = string
  default = "viewer"
}

variable "replica_count" {
  type    = number
  default = 2
}

variable "image_repository" {
  description = "The backend image. Defaults to the published one."
  type        = string
  default     = "ghcr.io/ravisinghrajput95/k8s-agent-backend"
}

variable "image_tag" {
  description = "Empty uses the chart's appVersion."
  type        = string
  default     = ""
}

variable "openai_api_key_secret_name" {
  type    = string
  default = ""
}

variable "agent_gateway" {
  type = object({
    enabled        = optional(bool, false)
    hostname       = optional(string, "")
    ca_secret_name = optional(string, "")
  })
  default = {}
}

# --- DNS ----------------------------------------------------------------------

variable "hostname" {
  description = "The console and API hostname. Empty disables the ingress and its record."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "The hosted zone for hostname and agent_gateway.hostname. Empty creates no records."
  type        = string
  default     = ""
}

variable "ingress_class_name" {
  type    = string
  default = "alb"
}

variable "ingress_annotations" {
  type = map(string)
  default = {
    "alb.ingress.kubernetes.io/scheme"      = "internet-facing"
    "alb.ingress.kubernetes.io/target-type" = "ip"
    # A progress stream is held open for the length of an investigation.
    "alb.ingress.kubernetes.io/load-balancer-attributes" = "idle_timeout.timeout_seconds=600"
  }
}

variable "ingress_tls_secret_name" {
  type    = string
  default = ""
}

# --- Postgres -----------------------------------------------------------------

variable "postgres" {
  type = object({
    engine_version        = optional(string, "17")
    instance_class        = optional(string, "db.t4g.medium")
    allocated_storage_gb  = optional(number, 50)
    multi_az              = optional(bool, true)
    backup_retention_days = optional(number, 14)
    deletion_protection   = optional(bool, true)
    database_name         = optional(string, "k8sagent")
    master_username       = optional(string, "k8sagent")
    performance_insights  = optional(bool, true)
    apply_immediately     = optional(bool, false)
    skip_final_snapshot   = optional(bool, false)
  })
  default = {}

  validation {
    condition     = tonumber(split(".", var.postgres.engine_version)[0]) >= 17
    error_message = "PostgreSQL 17 is the only major version the platform has been run against (compose, CI, integration-verify). Lower it here only after running the backend-integration suite against the version you want."
  }
}

variable "rds_ca_bundle_url" {
  description = "Where the RDS CA bundle is read from, so the platform can verify the instance (sslmode=verify-full)."
  type        = string
  default     = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
}

# --- Redis --------------------------------------------------------------------

variable "redis" {
  type = object({
    engine_version     = optional(string, "7.1")
    node_type          = optional(string, "cache.t4g.small")
    num_cache_clusters = optional(number, 2)
  })
  default = {}
}
