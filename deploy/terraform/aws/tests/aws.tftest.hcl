# Every provider but `random` is mocked. That proves the wiring — which secret a
# URL lands in, what a security group admits, that a URL has the scheme and TLS
# mode the platform needs — and proves **nothing** about whether AWS accepts
# these resources or whether pods can reach them. README.md says which is which.

mock_provider "aws" {
  mock_data "aws_eks_cluster" {
    defaults = {
      endpoint              = "https://eks.example.com"
      certificate_authority = [{ data = "Y2E=" }]
    }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address = "k8s-agent.abc123.eu-west-1.rds.amazonaws.com"
      port    = 5432
    }
  }
  mock_resource "aws_elasticache_replication_group" {
    defaults = {
      primary_endpoint_address = "master.k8s-agent.abc123.euw1.cache.amazonaws.com"
      port                     = 6379
    }
  }
}

mock_provider "kubernetes" {
  mock_data "kubernetes_ingress_v1" {
    defaults = {
      status = [{ load_balancer = [{ ingress = [{ hostname = "k8s-agent-123.eu-west-1.elb.amazonaws.com" }] }] }]
    }
  }
  mock_data "kubernetes_service_v1" {
    defaults = {
      status = [{ load_balancer = [{ ingress = [{ hostname = "k8s-agent-gw-456.elb.eu-west-1.amazonaws.com" }] }] }]
    }
  }
}

mock_provider "helm" {}

mock_provider "http" {
  mock_data "http" {
    defaults = {
      status_code   = 200
      response_body = "-----BEGIN CERTIFICATE-----\nMIIBrds\n-----END CERTIFICATE-----\n"
    }
  }
}

variables {
  region                     = "eu-west-1"
  eks_cluster_name           = "prod"
  vpc_id                     = "vpc-0123456789abcdef0"
  private_subnet_ids         = ["subnet-aaaa", "subnet-bbbb"]
  workload_security_group_id = "sg-0workloads"
  auth                       = { mode = "token", tokens_secret_name = "k8s-agent-tokens" }
}

run "the_database_url_is_encrypted_and_names_the_instance" {
  command = apply

  assert {
    condition     = startswith(module.release.database_url, "postgresql://k8sagent:")
    error_message = "DATABASE_URL must be a postgresql:// URL for the configured user."
  }
  assert {
    condition     = endswith(module.release.database_url, "@k8s-agent.abc123.eu-west-1.rds.amazonaws.com:5432/k8sagent?sslmode=verify-full&sslrootcert=/etc/k8s-agent/trust/database/ca.crt")
    error_message = "DATABASE_URL must name the instance and verify it against the mounted RDS root."
  }
  assert {
    condition     = length([for v in yamldecode(module.release.values_yaml).extraVolumes : v if v.secret.secretName == "k8s-agent-database-ca"]) == 1
    error_message = "The chart must mount the Secret holding the RDS CA bundle the URL names."
  }
}

run "the_redis_url_uses_tls_and_the_auth_token" {
  command = apply

  assert {
    condition     = startswith(module.release.redis_url, "rediss://:")
    error_message = "Transit encryption is on, so a redis:// URL would be refused."
  }
  assert {
    condition     = strcontains(module.release.redis_url, random_password.redis.result)
    error_message = "REDIS_URL must carry the replication group's auth token."
  }
}

run "state_is_reachable_only_from_the_platforms_pods" {
  command = apply

  assert {
    condition     = aws_vpc_security_group_ingress_rule.database.referenced_security_group_id == "sg-0workloads" && aws_vpc_security_group_ingress_rule.database.cidr_ipv4 == null
    error_message = "Postgres must admit the workload security group and no CIDR."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.redis.referenced_security_group_id == "sg-0workloads" && aws_vpc_security_group_ingress_rule.redis.cidr_ipv4 == null
    error_message = "Redis must admit the workload security group and no CIDR."
  }
  assert {
    condition     = aws_db_instance.this.publicly_accessible == false
    error_message = "The database must not be publicly accessible."
  }
}

run "state_is_encrypted_and_survives_a_mistaken_destroy" {
  command = apply

  assert {
    condition     = aws_db_instance.this.storage_encrypted && aws_db_instance.this.deletion_protection
    error_message = "Postgres holds every investigation and the revocation list: encrypted, and protected from deletion by default."
  }
  assert {
    condition     = aws_elasticache_replication_group.this.transit_encryption_enabled && aws_elasticache_replication_group.this.at_rest_encryption_enabled
    error_message = "Redis must be encrypted in transit and at rest."
  }
  assert {
    condition     = one([for p in aws_db_parameter_group.this.parameter : p.value if p.name == "rds.force_ssl"]) == "1"
    error_message = "The server must refuse plaintext, not only the client decline to send it."
  }
}

run "the_release_reads_state_from_the_secret_this_module_wrote" {
  command = apply

  assert {
    condition     = yamldecode(module.release.values_yaml).database.urlSecret.name == module.release.state_secret_name
    error_message = "The chart must mount the secret holding DATABASE_URL."
  }
  assert {
    condition     = yamldecode(module.release.values_yaml).redis.urlSecret.name == module.release.state_secret_name
    error_message = "The chart must mount the secret holding REDIS_URL."
  }
  assert {
    condition     = !strcontains(module.release.values_yaml, random_password.database.result) && !strcontains(module.release.values_yaml, random_password.redis.result)
    error_message = "No credential may appear in Helm values: they are readable with `helm get values`."
  }
}

run "records_point_at_the_load_balancers_the_chart_created" {
  command = apply

  variables {
    hostname        = "agent.example.com"
    route53_zone_id = "Z0123456789"
    agent_gateway   = { enabled = true, hostname = "gateway.agent.example.com", ca_secret_name = "k8s-agent-ca" }
  }

  assert {
    condition     = one(aws_route53_record.console[0].records) == "k8s-agent-123.eu-west-1.elb.amazonaws.com"
    error_message = "The console record must point at the ingress's load balancer."
  }
  assert {
    condition     = one(aws_route53_record.gateway[0].records) == "k8s-agent-gw-456.elb.eu-west-1.amazonaws.com"
    error_message = "The gateway record must point at the gateway Service's load balancer."
  }
  assert {
    condition     = data.kubernetes_service_v1.gateway[0].metadata[0].name == "k8s-agent-gateway"
    error_message = "The gateway Service is <fullnameOverride>-gateway; looking up any other name finds nothing."
  }
}

run "no_records_without_a_zone" {
  command = apply

  variables {
    hostname = "agent.example.com"
  }

  assert {
    condition     = length(aws_route53_record.console) == 0 && length(aws_route53_record.gateway) == 0
    error_message = "Without a zone no record is created."
  }
}

run "only_postgres_17_has_been_run" {
  command = plan
  variables {
    postgres = { engine_version = "16.4" }
  }
  expect_failures = [var.postgres]
}

run "one_subnet_is_refused" {
  command = plan
  variables {
    private_subnet_ids = ["subnet-aaaa"]
  }
  expect_failures = [var.private_subnet_ids]
}
