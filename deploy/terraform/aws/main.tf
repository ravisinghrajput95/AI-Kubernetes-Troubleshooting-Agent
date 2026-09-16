# The managed state, secret and DNS plumbing around deploy/helm/k8s-agent, on AWS.
#
# **Written, validated and tested against mocked providers; never applied.**
# See README.md for exactly what has and has not been run. Nothing here should
# be read as a deployment that is known to work until someone has applied it.

locals {
  chart_path = coalesce(var.chart_path, "${path.module}/../../helm/k8s-agent")
  tags       = { "app.kubernetes.io/part-of" = var.name }
}

# --- Credentials --------------------------------------------------------------
#
# These live in Terraform state. Use a remote backend with encryption and
# restricted access; that is the trade for a URL the chart can mount without a
# second secrets operator.

resource "random_password" "database" {
  length  = 40
  special = false # the password is interpolated into a URL
}

resource "random_password" "redis" {
  length  = 64
  special = false # ElastiCache restricts auth-token characters; this is a URL too
}

# --- Postgres -----------------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
  tags       = local.tags
}

resource "aws_security_group" "database" {
  name        = "${var.name}-postgres"
  description = "Postgres for ${var.name}: the platform's pods only"
  vpc_id      = var.vpc_id
  tags        = local.tags
}

resource "aws_vpc_security_group_ingress_rule" "database" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = var.workload_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "platform pods"
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-postgres${split(".", var.postgres.engine_version)[0]}"
  family = "postgres${split(".", var.postgres.engine_version)[0]}"
  tags   = local.tags

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.postgres.engine_version
  instance_class = var.postgres.instance_class

  db_name  = var.postgres.database_name
  username = var.postgres.master_username
  password = random_password.database.result

  allocated_storage     = var.postgres.allocated_storage_gb
  max_allocated_storage = var.postgres.allocated_storage_gb * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = var.postgres.multi_az

  # The CA private key is a file, not a row, so a database restore does not
  # restore agent identity — docs/RUNBOOK_BACKUP_RESTORE.md. Backups here cover
  # investigations, reports, memberships and the revocation list.
  backup_retention_period   = var.postgres.backup_retention_days
  deletion_protection       = var.postgres.deletion_protection
  skip_final_snapshot       = var.postgres.skip_final_snapshot
  final_snapshot_identifier = var.postgres.skip_final_snapshot ? null : "${var.name}-final"
  copy_tags_to_snapshot     = true

  performance_insights_enabled = var.postgres.performance_insights
  auto_minor_version_upgrade   = true
  apply_immediately            = var.postgres.apply_immediately

  tags = local.tags
}

# --- Redis --------------------------------------------------------------------

resource "aws_elasticache_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
  tags       = local.tags
}

resource "aws_security_group" "redis" {
  name        = "${var.name}-redis"
  description = "Redis for ${var.name}: the platform's pods only"
  vpc_id      = var.vpc_id
  tags        = local.tags
}

resource "aws_vpc_security_group_ingress_rule" "redis" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = var.workload_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  description                  = "platform pods"
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = var.name
  description          = "Latency layer for ${var.name}; Postgres is the truth"
  engine               = "redis"
  engine_version       = var.redis.engine_version
  node_type            = var.redis.node_type
  num_cache_clusters   = var.redis.num_cache_clusters
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  # Cluster mode stays off: the platform uses one logical database, pub/sub
  # and multi-key operations a sharded keyspace would split.
  automatic_failover_enabled = var.redis.num_cache_clusters > 1
  multi_az_enabled           = var.redis.num_cache_clusters > 1

  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  auth_token                 = random_password.redis.result

  apply_immediately = false
  tags              = local.tags
}

# --- Kubernetes ---------------------------------------------------------------
#
# The Secret, values and release are modules/platform-release — the same code
# the kind/ root applies on a laptop, which is the only part of this directory
# that has been applied anywhere.

module "release" {
  source = "../modules/platform-release"

  name             = var.name
  namespace        = var.namespace
  create_namespace = var.create_namespace
  chart_path       = local.chart_path

  # sslmode=require encrypts and does not verify the server certificate: the
  # platform image carries no RDS CA bundle, and verify-full would fail every
  # connection. rds.force_ssl makes the server refuse plaintext regardless.
  database = {
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    username = aws_db_instance.this.username
    password = random_password.database.result
    name     = aws_db_instance.this.db_name
    sslmode  = "require"
  }

  # rediss:// because transit encryption is on; a group with an auth token
  # refuses plaintext.
  redis = {
    host       = aws_elasticache_replication_group.this.primary_endpoint_address
    port       = aws_elasticache_replication_group.this.port
    auth_token = random_password.redis.result
    tls        = true
  }

  platform = {
    auth                       = var.auth
    tenancy_mode               = var.tenancy_mode
    rbac_default_role          = var.rbac_default_role
    replica_count              = var.replica_count
    image_tag                  = var.image_tag
    openai_api_key_secret_name = var.openai_api_key_secret_name
    hostname                   = var.hostname
    ingress_class_name         = var.ingress_class_name
    ingress_annotations        = var.ingress_annotations
    ingress_tls_secret_name    = var.ingress_tls_secret_name
    agent_gateway = merge(var.agent_gateway, {
      # An NLB, because a gateway stream is a long-lived gRPC connection an
      # HTTP load balancer would time out and re-balance.
      service_annotations = {
        "service.beta.kubernetes.io/aws-load-balancer-type"            = "external"
        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type" = "ip"
        "service.beta.kubernetes.io/aws-load-balancer-scheme"          = "internet-facing"
      }
    })
  }
}

# --- DNS ----------------------------------------------------------------------

data "kubernetes_ingress_v1" "this" {
  count = var.hostname != "" && var.route53_zone_id != "" ? 1 : 0
  metadata {
    name      = var.name
    namespace = module.release.namespace
  }
}

resource "aws_route53_record" "console" {
  count   = length(data.kubernetes_ingress_v1.this)
  zone_id = var.route53_zone_id
  name    = var.hostname
  type    = "CNAME"
  ttl     = 300
  records = [data.kubernetes_ingress_v1.this[0].status[0].load_balancer[0].ingress[0].hostname]
}

data "kubernetes_service_v1" "gateway" {
  count = var.agent_gateway.enabled && var.route53_zone_id != "" ? 1 : 0
  metadata {
    name      = "${var.name}-gateway"
    namespace = module.release.namespace
  }
}

resource "aws_route53_record" "gateway" {
  count   = length(data.kubernetes_service_v1.gateway)
  zone_id = var.route53_zone_id
  name    = var.agent_gateway.hostname
  type    = "CNAME"
  ttl     = 300
  records = [data.kubernetes_service_v1.gateway[0].status[0].load_balancer[0].ingress[0].hostname]
}
