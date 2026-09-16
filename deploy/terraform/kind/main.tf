# modules/platform-release, applied to a kind cluster.
#
# The cloud roots cannot be applied without an account; this can, and it runs
# the same module they do. What stands in for the managed services is chosen to
# keep the properties the URLs depend on rather than to be convenient:
#
# - **Postgres refuses plaintext.** Its pg_hba has only `hostssl` lines, which
#   is what `rds.force_ssl = 1` does on RDS. A release that becomes ready
#   therefore connected with TLS, and `sslmode=require` is shown to work in the
#   platform's own driver rather than assumed to.
# - **Redis requires its password**, as an ElastiCache auth token does. It does
#   not speak TLS: redis-py verifies `rediss://` against the image's system
#   CAs, a self-signed server would be refused, and weakening that check to
#   pass here would test a configuration nobody should ship. So the `rediss`
#   branch is still unexercised, and the README says so.
#
# Applied and destroyed by `scripts/terraform_verify.py --kind`.

resource "kubernetes_namespace_v1" "this" {
  metadata {
    name = var.namespace
  }
}

locals {
  namespace = kubernetes_namespace_v1.this.metadata[0].name
  pg_host   = "postgres.${local.namespace}.svc.cluster.local"
}

resource "random_password" "database" {
  length  = 32
  special = false
}

resource "random_password" "redis" {
  length  = 48
  special = false
}

resource "random_password" "api_token" {
  length  = 40
  special = false
}

# --- Postgres, TLS only -------------------------------------------------------

resource "tls_private_key" "postgres" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "postgres" {
  private_key_pem       = tls_private_key.postgres.private_key_pem
  validity_period_hours = 24
  dns_names             = [local.pg_host, "postgres"]
  allowed_uses          = ["server_auth", "digital_signature", "key_encipherment"]
  subject {
    common_name = local.pg_host
  }
}

resource "kubernetes_secret_v1" "postgres_tls" {
  metadata {
    name      = "postgres-tls"
    namespace = local.namespace
  }
  data = {
    "tls.crt" = tls_self_signed_cert.postgres.cert_pem
    "tls.key" = tls_private_key.postgres.private_key_pem
  }
}

resource "kubernetes_config_map_v1" "postgres_hba" {
  metadata {
    name      = "postgres-hba"
    namespace = local.namespace
  }
  data = {
    # `local` for the image's own init scripts over the socket; every TCP
    # connection must be TLS. There is deliberately no `host` line.
    "pg_hba.conf" = <<-EOT
      local   all all                trust
      hostssl all all 0.0.0.0/0      scram-sha-256
      hostssl all all ::/0           scram-sha-256
    EOT
  }
}

resource "kubernetes_deployment_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = local.namespace
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "postgres" }
    }
    template {
      metadata {
        labels = { app = "postgres" }
      }
      spec {
        security_context {
          # postgres:17-alpine runs as uid/gid 70, and refuses a key file
          # anyone but its owner or group can read.
          fs_group = 70
        }
        container {
          name  = "postgres"
          image = "postgres:17-alpine"
          args = [
            "-c", "ssl=on",
            "-c", "ssl_cert_file=/tls/tls.crt",
            "-c", "ssl_key_file=/tls/tls.key",
            "-c", "hba_file=/hba/pg_hba.conf",
          ]
          env {
            name  = "POSTGRES_USER"
            value = "k8sagent"
          }
          env {
            name  = "POSTGRES_PASSWORD"
            value = random_password.database.result
          }
          env {
            name  = "POSTGRES_DB"
            value = "k8sagent"
          }
          env {
            name  = "PGDATA"
            value = "/var/lib/postgresql/data/pgdata"
          }
          port {
            container_port = 5432
          }
          readiness_probe {
            exec {
              command = ["pg_isready", "-U", "k8sagent", "-h", "127.0.0.1"]
            }
            period_seconds = 3
          }
          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
          }
          volume_mount {
            name       = "tls"
            mount_path = "/tls"
            read_only  = true
          }
          volume_mount {
            name       = "hba"
            mount_path = "/hba"
            read_only  = true
          }
        }
        volume {
          name = "data"
          empty_dir {}
        }
        volume {
          name = "tls"
          secret {
            secret_name  = kubernetes_secret_v1.postgres_tls.metadata[0].name
            default_mode = "0640"
          }
        }
        volume {
          name = "hba"
          config_map {
            name = kubernetes_config_map_v1.postgres_hba.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "postgres" {
  metadata {
    name      = "postgres"
    namespace = local.namespace
  }
  spec {
    selector = { app = "postgres" }
    port {
      port        = 5432
      target_port = 5432
    }
  }
}

# --- Redis, password required -------------------------------------------------

resource "kubernetes_secret_v1" "redis" {
  metadata {
    name      = "redis-auth"
    namespace = local.namespace
  }
  data = {
    password = random_password.redis.result
  }
}

resource "kubernetes_deployment_v1" "redis" {
  metadata {
    name      = "redis"
    namespace = local.namespace
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "redis" }
    }
    template {
      metadata {
        labels = { app = "redis" }
      }
      spec {
        container {
          name    = "redis"
          image   = "redis:7-alpine"
          command = ["sh", "-c", "exec redis-server --requirepass \"$REDIS_PASSWORD\""]
          env {
            name = "REDIS_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.redis.metadata[0].name
                key  = "password"
              }
            }
          }
          port {
            container_port = 6379
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "redis" {
  metadata {
    name      = "redis"
    namespace = local.namespace
  }
  spec {
    selector = { app = "redis" }
    port {
      port        = 6379
      target_port = 6379
    }
  }
}

# --- The platform -------------------------------------------------------------

resource "kubernetes_secret_v1" "tokens" {
  metadata {
    name      = "k8s-agent-tokens"
    namespace = local.namespace
  }
  data = {
    API_TOKENS = "${random_password.api_token.result}:tf-verify@example.com:platform-admins"
  }
}

module "release" {
  source = "../modules/platform-release"

  name             = "k8s-agent"
  namespace        = local.namespace
  create_namespace = false
  chart_path       = "${path.module}/../../helm/k8s-agent"
  timeout_seconds  = 600

  database = {
    host     = local.pg_host
    username = "k8sagent"
    password = random_password.database.result
    name     = "k8sagent"
    sslmode  = "require"
  }

  redis = {
    host       = "redis.${local.namespace}.svc.cluster.local"
    auth_token = random_password.redis.result
    tls        = false
  }

  platform = {
    auth             = { mode = "token", tokens_secret_name = kubernetes_secret_v1.tokens.metadata[0].name }
    replica_count    = 2
    image_repository = var.image_repository
    image_tag        = var.image_tag
  }

  depends_on = [
    kubernetes_deployment_v1.postgres,
    kubernetes_service_v1.postgres,
    kubernetes_deployment_v1.redis,
    kubernetes_service_v1.redis,
  ]
}
