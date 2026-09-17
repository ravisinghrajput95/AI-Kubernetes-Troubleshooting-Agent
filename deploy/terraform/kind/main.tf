# modules/platform-release, applied to a kind cluster.
#
# The cloud roots cannot be applied without an account; this can, and it runs
# the same module they do. What stands in for the managed services is chosen to
# keep the properties the URLs depend on rather than to be convenient:
#
# - **Postgres refuses plaintext.** Its pg_hba has only `hostssl` lines, which
#   is what `rds.force_ssl = 1` does on RDS. A release that becomes ready
#   therefore connected with TLS, from the platform's own driver.
# - **Redis requires its password and speaks only TLS**, as an ElastiCache group
#   with transit encryption does.
# - **Both certificates chain to a private CA the platform is given**, so the
#   URLs verify rather than merely encrypt: `sslmode=verify-full` for Postgres,
#   `rediss://` with the CA for Redis. That is the mechanism an RDS CA bundle
#   uses, exercised here with a root no public store holds — so a connection
#   that succeeds was verified, not waved through by a system root. Postgres
#   takes the root as `ca_pem`, the branch the AWS root uses for RDS's bundle.
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

resource "tls_private_key" "ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "ca" {
  private_key_pem       = tls_private_key.ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 24
  allowed_uses          = ["cert_signing", "crl_signing", "digital_signature"]
  subject {
    common_name = "k8s-agent terraform verification CA"
  }
}

resource "tls_private_key" "server" {
  for_each    = toset(["postgres", "redis"])
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "server" {
  for_each        = toset(["postgres", "redis"])
  private_key_pem = tls_private_key.server[each.key].private_key_pem
  dns_names       = ["${each.key}.${local.namespace}.svc.cluster.local"]
  subject {
    common_name = "${each.key}.${local.namespace}.svc.cluster.local"
  }
}

resource "tls_locally_signed_cert" "server" {
  for_each              = toset(["postgres", "redis"])
  cert_request_pem      = tls_cert_request.server[each.key].cert_request_pem
  ca_private_key_pem    = tls_private_key.ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.ca.cert_pem
  validity_period_hours = 24
  allowed_uses          = ["server_auth", "digital_signature", "key_encipherment"]
}

resource "kubernetes_secret_v1" "server_tls" {
  for_each = toset(["postgres", "redis"])
  metadata {
    name      = "${each.key}-tls"
    namespace = local.namespace
  }
  data = {
    "tls.crt" = tls_locally_signed_cert.server[each.key].cert_pem
    "tls.key" = tls_private_key.server[each.key].private_key_pem
    "ca.crt"  = tls_self_signed_cert.ca.cert_pem
  }
}

# What the platform is given: the root, and nothing else.
resource "kubernetes_secret_v1" "state_ca" {
  metadata {
    name      = "state-ca"
    namespace = local.namespace
  }
  data = {
    "ca.crt" = tls_self_signed_cert.ca.cert_pem
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
            secret_name  = kubernetes_secret_v1.server_tls["postgres"].metadata[0].name
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
          name  = "redis"
          image = "redis:7-alpine"
          # `--port 0`: no plaintext listener at all.
          command = [
            "sh", "-c",
            "exec redis-server --port 0 --tls-port 6379 --tls-cert-file /tls/tls.crt --tls-key-file /tls/tls.key --tls-ca-cert-file /tls/ca.crt --tls-auth-clients no --requirepass \"$REDIS_PASSWORD\"",
          ]
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
          volume_mount {
            name       = "tls"
            mount_path = "/tls"
            read_only  = true
          }
        }
        volume {
          name = "tls"
          secret {
            secret_name  = kubernetes_secret_v1.server_tls["redis"].metadata[0].name
            default_mode = "0644"
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
    sslmode  = "verify-full"
    # The branch the AWS root takes with RDS's bundle: the module writes the
    # Secret itself, in a namespace it may have just created.
    ca_pem = tls_self_signed_cert.ca.cert_pem
  }

  redis = {
    host           = "redis.${local.namespace}.svc.cluster.local"
    auth_token     = random_password.redis.result
    tls            = true
    ca_secret_name = kubernetes_secret_v1.state_ca.metadata[0].name
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
