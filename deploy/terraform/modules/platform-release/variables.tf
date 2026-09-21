variable "name" {
  description = "The Helm release name and the chart's fullnameOverride."
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
  type = string
}

variable "timeout_seconds" {
  type    = number
  default = 900
}

variable "database" {
  description = "Where Postgres is. sslmode is required so that choosing plaintext is a visible decision."
  type = object({
    host     = string
    port     = optional(number, 5432)
    username = string
    password = string
    name     = string
    sslmode  = string
    # A Secret holding `ca.crt`, the root the server certificate chains to.
    # Empty with a verify mode means the image's system roots.
    ca_secret_name = optional(string, "")
    # Or the root itself, which this module then writes into a Secret in the
    # release's namespace — needed when that namespace is created here, so no
    # Secret can exist in it beforehand. RDS's bundle is the case in point.
    ca_pem = optional(string, "")
  })
  sensitive = true

  validation {
    condition     = contains(["disable", "require", "verify-ca", "verify-full"], var.database.sslmode)
    error_message = "database.sslmode must be disable, require, verify-ca or verify-full."
  }
}

variable "redis" {
  type = object({
    host       = string
    port       = optional(number, 6379)
    auth_token = string
    tls        = bool
    # A Secret holding `ca.crt`; empty verifies against the image's system
    # roots, which is what a publicly-signed endpoint (ElastiCache) needs.
    ca_secret_name = optional(string, "")
  })
  sensitive = true
}

variable "platform" {
  description = "Passed to platform-values; see its variables.tf for each field and its refusals."
  type = object({
    auth = object({
      mode                   = string
      oidc_issuer            = optional(string, "")
      oidc_audience          = optional(string, "")
      oidc_tenant_claim      = optional(string, "")
      oidc_role_mappings     = optional(string, "")
      tokens_secret_name     = optional(string, "")
      allow_insecure_no_auth = optional(bool, false)
    })
    tenancy_mode               = optional(string, "single")
    rbac_default_role          = optional(string, "viewer")
    replica_count              = optional(number, 2)
    image_repository           = optional(string, "ghcr.io/ravisinghrajput95/k8s-agent-backend")
    image_tag                  = optional(string, "")
    openai_api_key_secret_name = optional(string, "")
    hostname                   = optional(string, "")
    ingress_class_name         = optional(string, "")
    ingress_annotations        = optional(map(string), {})
    ingress_tls_secret_name    = optional(string, "")
    agent_gateway = optional(object({
      enabled             = optional(bool, false)
      hostname            = optional(string, "")
      ca_secret_name      = optional(string, "")
      service_annotations = optional(map(string), {})
    }), {})
  })
  # No backend image is published, so there is nothing to default to — and the
  # default that used to sit here named an unpublished repository, which renders
  # and then ImagePullBackOffs.
  validation {
    condition     = length(trimspace(var.platform.image_repository)) > 0
    error_message = "platform.image_repository is required: build backend/Dockerfile and push it to a registry the cluster can pull from."
  }

}
