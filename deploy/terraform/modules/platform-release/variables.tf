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
}
