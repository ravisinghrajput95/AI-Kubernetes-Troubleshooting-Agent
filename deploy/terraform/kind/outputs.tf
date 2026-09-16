output "namespace" {
  value = local.namespace
}

output "api_token" {
  value     = random_password.api_token.result
  sensitive = true
}
