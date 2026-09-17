# Helm values for deploy/helm/k8s-agent, derived from infrastructure outputs.
#
# **No providers, deliberately.** Everything provider-specific lives in the
# root module that calls this one; what is left is a pure function from inputs
# to the values document. That is what makes it the one part of this directory
# that can be *run* without a cloud account: `terraform apply` on it touches
# nothing, and its output is fed to `helm template` against the real chart by
# scripts/terraform_verify.sh. The chart's own `_validate.tpl` then judges the
# values, so a disagreement between what Terraform produces and what the chart
# accepts fails there rather than at `helm install`.
#
# The refusals in variables.tf mirror the chart's, which mirror the platform's.
# They are a convenience for timing, never the control: the platform refuses at
# startup regardless.

terraform {
  required_version = ">= 1.9"
}

locals {
  gateway_names = var.agent_gateway.enabled && var.agent_gateway.hostname != "" ? [var.agent_gateway.hostname] : []

  values = {
    # Pinned, so the Services a caller looks up afterwards have a name it can
    # compute rather than the chart's release-name-contains-chart-name rule.
    fullnameOverride = var.fullname
    replicaCount     = var.replica_count
    image = merge(
      { repository = var.image_repository },
      var.image_tag == "" ? {} : { tag = var.image_tag },
    )

    auth = {
      mode = var.auth.mode
      oidc = {
        issuer       = var.auth.oidc_issuer
        audience     = var.auth.oidc_audience
        tenantClaim  = var.auth.oidc_tenant_claim
        roleMappings = var.auth.oidc_role_mappings
      }
      tokensSecret = {
        name = var.auth.tokens_secret_name
        key  = "API_TOKENS"
      }
      allowInsecureNoAuth = var.auth.allow_insecure_no_auth
    }

    rbac    = { defaultRole = var.rbac_default_role }
    tenancy = { mode = var.tenancy_mode }

    # Both or neither is the platform's rule; this module only ever produces
    # both, because it is only called with managed state behind it.
    database = { urlSecret = { name = var.state_secret_name, key = "DATABASE_URL" } }
    redis    = { urlSecret = { name = var.state_secret_name, key = "REDIS_URL" } }

    openai = {
      apiKeySecret = { name = var.openai_api_key_secret_name, key = "OPENAI_API_KEY" }
    }

    agentGateway = {
      enabled = var.agent_gateway.enabled
      service = {
        type        = var.agent_gateway.enabled ? "LoadBalancer" : "ClusterIP"
        annotations = var.agent_gateway.service_annotations
      }
      caSecret  = { name = var.agent_gateway.ca_secret_name, certKey = "ca.crt", keyKey = "ca.key" }
      dnsNames  = local.gateway_names
      advertise = length(local.gateway_names) > 0 ? "${local.gateway_names[0]}:9443" : ""
    }

    ingress = {
      enabled     = var.hostname != ""
      className   = var.ingress_class_name
      annotations = var.ingress_annotations
      hosts       = var.hostname == "" ? [] : [{ host = var.hostname, paths = [{ path = "/", pathType = "Prefix" }] }]
      tls         = var.hostname == "" || var.ingress_tls_secret_name == "" ? [] : [{ secretName = var.ingress_tls_secret_name, hosts = [var.hostname] }]
    }

    config = {
      corsOrigins = var.hostname == "" ? [] : ["https://${var.hostname}"]
    }

    podDisruptionBudget = { enabled = var.replica_count > 1, minAvailable = 1 }

    extraVolumes      = var.extra_volumes
    extraVolumeMounts = var.extra_volume_mounts
  }
}

output "values" {
  description = "The Helm values document, as an object."
  value       = local.values
}

output "values_yaml" {
  description = "The Helm values document, as YAML for helm_release or `helm template -f -`."
  value       = yamlencode(local.values)
}
