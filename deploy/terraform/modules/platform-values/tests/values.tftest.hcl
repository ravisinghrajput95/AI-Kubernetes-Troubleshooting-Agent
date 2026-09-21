# This module has no providers, so `apply` here runs for real — nothing is
# mocked. What it cannot show is whether the chart accepts the result; that is
# scripts/terraform_verify.sh, which renders these values with `helm template`.

variables {
  auth              = { mode = "token", tokens_secret_name = "k8s-agent-tokens" }
  state_secret_name = "k8s-agent-state"
  # Required: no backend image is published, so every caller names its own.
  image_repository = "registry.example.com/k8s-agent-backend"
}

run "state_comes_from_one_secret_for_both_urls" {
  command = apply

  assert {
    condition     = output.values.database.urlSecret == { name = "k8s-agent-state", key = "DATABASE_URL" }
    error_message = "DATABASE_URL must come from the state secret."
  }
  assert {
    condition     = output.values.redis.urlSecret == { name = "k8s-agent-state", key = "REDIS_URL" }
    error_message = "REDIS_URL must come from the same secret: both or neither is the platform's rule."
  }
}

run "no_ingress_without_a_hostname" {
  command = apply

  assert {
    condition     = output.values.ingress.enabled == false && length(output.values.config.corsOrigins) == 0
    error_message = "With no hostname there is nothing to route or to allow."
  }
}

run "the_gateway_certificate_names_the_address_agents_are_told_to_dial" {
  command = apply

  variables {
    agent_gateway = { enabled = true, hostname = "gateway.agents.example.com", ca_secret_name = "k8s-agent-ca" }
  }

  assert {
    condition     = jsonencode(output.values.agentGateway.dnsNames) == jsonencode(["gateway.agents.example.com"])
    error_message = "The serving certificate must name the hostname agents dial."
  }
  assert {
    condition     = output.values.agentGateway.advertise == "gateway.agents.example.com:9443"
    error_message = "The enrolment manifest must tell agents to dial a name the certificate carries."
  }
  assert {
    condition     = output.values.agentGateway.service.type == "LoadBalancer"
    error_message = "Agents dial in from other clusters, so the gateway needs an external address."
  }
}

run "the_console_origin_is_the_hostname_over_https" {
  command = apply

  variables {
    hostname                = "agent.example.com"
    ingress_tls_secret_name = "agent-example-com-tls"
  }

  assert {
    condition     = jsonencode(output.values.config.corsOrigins) == jsonencode(["https://agent.example.com"])
    error_message = "The console's origin must be allowed, and only it."
  }
  assert {
    condition     = jsonencode(output.values.ingress.tls[0].hosts) == jsonencode(["agent.example.com"])
    error_message = "TLS must cover the hostname the ingress serves."
  }
}

# --- Refusals ------------------------------------------------------------------
#
# Each mirrors one the chart makes at render time and the platform makes at
# startup. They exist for timing: a plan that fails names the variable, where a
# pod that crashloops names it in a log line.

run "an_absent_auth_mode_is_refused" {
  command = plan
  variables {
    auth = { mode = "" }
  }
  expect_failures = [var.auth]
}

run "disabled_auth_needs_its_acknowledgement" {
  command = plan
  variables {
    auth = { mode = "disabled" }
  }
  expect_failures = [var.auth]
}

run "token_auth_needs_a_secret_not_inline_tokens" {
  command = plan
  variables {
    auth = { mode = "token" }
  }
  expect_failures = [var.auth]
}

run "shared_tenancy_refuses_a_permissive_default_role" {
  command = plan
  variables {
    tenancy_mode      = "shared"
    rbac_default_role = "admin"
  }
  expect_failures = [var.rbac_default_role]
}

run "shared_tenancy_over_oidc_needs_a_tenant_claim" {
  command = plan
  variables {
    auth         = { mode = "oidc", oidc_issuer = "https://idp.example.com", oidc_audience = "k8s-agent" }
    tenancy_mode = "shared"
  }
  expect_failures = [var.tenancy_mode]
}

run "a_gateway_without_a_shared_ca_is_refused" {
  command = plan
  variables {
    agent_gateway = { enabled = true, hostname = "gateway.agents.example.com" }
  }
  expect_failures = [var.agent_gateway]
}
