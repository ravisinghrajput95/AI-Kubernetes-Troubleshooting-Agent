variable "auth" {
  description = <<-EOT
    How callers authenticate. `mode` has no default, for the reason AUTH_MODE
    has none in the platform and `auth.mode` has none in the chart: a module
    that picks a mode is a deployment authenticating by a decision nobody made.
  EOT
  type = object({
    mode                   = string
    oidc_issuer            = optional(string, "")
    oidc_audience          = optional(string, "")
    oidc_tenant_claim      = optional(string, "")
    oidc_role_mappings     = optional(string, "")
    tokens_secret_name     = optional(string, "")
    allow_insecure_no_auth = optional(bool, false)
  })

  validation {
    condition     = contains(["oidc", "token", "disabled"], var.auth.mode)
    error_message = "auth.mode must be one of oidc, token or disabled. There is no default."
  }
  validation {
    condition     = var.auth.mode != "oidc" || (var.auth.oidc_issuer != "" && var.auth.oidc_audience != "")
    error_message = "auth.mode=oidc requires oidc_issuer and oidc_audience."
  }
  validation {
    condition     = var.auth.mode != "token" || var.auth.tokens_secret_name != ""
    error_message = "auth.mode=token requires tokens_secret_name: a Secret holding API_TOKENS, created outside this module so tokens never enter Terraform state."
  }
  validation {
    condition     = var.auth.mode != "disabled" || var.auth.allow_insecure_no_auth
    error_message = "auth.mode=disabled holds a kubeconfig and authenticates nobody. Set allow_insecure_no_auth = true to acknowledge it, for a throwaway environment only."
  }
}

variable "tenancy_mode" {
  description = "single (one implicit tenant) or shared (row-level security; needs an unprivileged database role — see README)."
  type        = string
  default     = "single"

  validation {
    condition     = contains(["single", "shared"], var.tenancy_mode)
    error_message = "tenancy_mode must be single or shared."
  }
  validation {
    condition     = var.tenancy_mode != "shared" || var.auth.mode != "disabled"
    error_message = "tenancy_mode=shared requires authentication: every caller being anonymous means every caller is the same tenant."
  }
  validation {
    condition     = var.tenancy_mode != "shared" || var.auth.mode != "oidc" || var.auth.oidc_tenant_claim != ""
    error_message = "tenancy_mode=shared with auth.mode=oidc requires oidc_tenant_claim, or every tenant lands in `default`."
  }
}

variable "rbac_default_role" {
  description = "What a caller with no binding gets. The chart ships viewer; the platform refuses anything above viewer in shared tenancy."
  type        = string
  default     = "viewer"

  validation {
    condition     = contains(["viewer", "operator", "admin", "owner", "none", ""], var.rbac_default_role)
    error_message = "rbac_default_role must be viewer, operator, admin, owner or none."
  }
  validation {
    condition     = var.tenancy_mode != "shared" || contains(["viewer", "none", ""], var.rbac_default_role)
    error_message = "tenancy_mode=shared refuses a default role above viewer: anyone the IdP can place in a tenant would administer it."
  }
}

variable "state_secret_name" {
  description = "The Secret holding DATABASE_URL and REDIS_URL."
  type        = string

  validation {
    condition     = var.state_secret_name != ""
    error_message = "state_secret_name is required."
  }
}

variable "fullname" {
  description = "The chart's fullnameOverride: the Deployment, Service, Ingress and gateway Service are named from it."
  type        = string
  default     = "k8s-agent"
}

variable "replica_count" {
  type    = number
  default = 2
}

variable "image_repository" {
  description = <<-EOT
    The backend image. The default is the published one; override it with your
    own build. This named the same path while nothing published it, so a
    deployment that set no image rendered cleanly and then sat in
    ImagePullBackOff — hence the validation below, which refuses an empty one.
  EOT
  type        = string
  default     = "ghcr.io/ravisinghrajput95/k8s-agent-backend"

  validation {
    condition     = length(trimspace(var.image_repository)) > 0
    error_message = "image_repository is required: build backend/Dockerfile and push it to a registry the cluster can pull from."
  }
}

variable "image_tag" {
  description = "Empty uses the chart's appVersion."
  type        = string
  default     = ""
}

variable "openai_api_key_secret_name" {
  description = "Optional. Without it the deterministic diagnosis is used and nothing leaves the deployment."
  type        = string
  default     = ""
}

variable "agent_gateway" {
  description = <<-EOT
    The gateway cluster agents dial into. `hostname` is the name agents reach
    from outside; it becomes both a name on the gateway's serving certificate
    and the address the enrolment manifest tells agents to dial, which must
    agree or every agent's TLS handshake fails before enrolment begins.
  EOT
  type = object({
    enabled             = optional(bool, false)
    hostname            = optional(string, "")
    ca_secret_name      = optional(string, "")
    service_annotations = optional(map(string), {})
  })
  default = {}

  validation {
    condition     = !var.agent_gateway.enabled || var.agent_gateway.hostname != ""
    error_message = "agent_gateway.enabled requires hostname: agents dial it from outside the cluster, and the gateway's certificate must name it."
  }
  validation {
    condition     = !var.agent_gateway.enabled || var.agent_gateway.ca_secret_name != ""
    error_message = "agent_gateway.enabled requires ca_secret_name. Without it every replica generates its own development CA, and an agent can verify at most one of them."
  }
}

variable "hostname" {
  description = "The console and API hostname. Empty disables the ingress."
  type        = string
  default     = ""
}

variable "ingress_class_name" {
  type    = string
  default = ""
}

variable "ingress_annotations" {
  type    = map(string)
  default = {}
}

variable "ingress_tls_secret_name" {
  description = "A TLS Secret for the ingress, e.g. one cert-manager maintains. Empty serves plain HTTP at the ingress."
  type        = string
  default     = ""
}

variable "extra_volumes" {
  description = "Chart extraVolumes, e.g. the CA bundle a verifying Postgres or Redis URL names."
  type        = any
  default     = []
}

variable "extra_volume_mounts" {
  type    = any
  default = []
}
