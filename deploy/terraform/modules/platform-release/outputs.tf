output "namespace" {
  value = helm_release.this.namespace
}

output "state_secret_name" {
  value = kubernetes_secret_v1.state.metadata[0].name
}

output "database_url" {
  value     = local.database_url
  sensitive = true
}

output "redis_url" {
  value     = local.redis_url
  sensitive = true
}

output "values_yaml" {
  description = "The values the release was installed with. Secret names, never secret values."
  value       = helm_release.this.values[0]
}
