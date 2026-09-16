output "database_endpoint" {
  value = aws_db_instance.this.address
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "state_secret_name" {
  description = "The Secret holding DATABASE_URL and REDIS_URL."
  value       = kubernetes_secret_v1.state.metadata[0].name
}

output "helm_values" {
  description = "The values the release was installed with. Holds secret names, never secret values."
  value       = module.values.values_yaml
}
