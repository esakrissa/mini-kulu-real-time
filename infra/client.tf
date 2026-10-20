# The hosted web client: the token server and the page (agent/token_server.py + web/) as a
# scale-to-zero Cloud Run service, behind a passcode and per-IP rate limits (agent/access.py).
# CI builds the image (client.Dockerfile) and passes it in as client_image; empty = no client.
#
# Free tier: Cloud Run bills by request here (CPU only while a request runs), and a call only
# touches it for a token, so a demo uses a tiny share of the monthly 180k vCPU-seconds and
# 2M requests. max 1 instance also makes the in-memory rate limits global.

locals {
  client_enabled = var.client_image != ""
  client_secrets = local.client_enabled ? ["livekit-api-key", "livekit-api-secret", "client-passcode"] : []
}

resource "google_service_account" "client" {
  count = local.client_enabled ? 1 : 0

  account_id   = "mini-kulu-client"
  display_name = "Mini-Kulu hosted web client (Cloud Run)"
}

# It mints LiveKit tokens, so it reads the LiveKit key and secret, plus its passcode.
resource "google_secret_manager_secret_iam_member" "client" {
  for_each = toset(local.client_secrets)

  secret_id = google_secret_manager_secret.this[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.client[0].member
}

resource "google_cloud_run_v2_service" "client" {
  count = local.client_enabled ? 1 : 0

  name                = "mini-kulu-client"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.client[0].email
    max_instance_request_concurrency = 40
    timeout                          = "30s"

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.client_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle          = true # CPU only during requests: request-based billing
        startup_cpu_boost = false
      }

      env {
        name  = "LIVEKIT_URL"
        value = var.livekit_url
      }
      env {
        name  = "COOKIE_SECURE"
        value = "1"
      }
      env {
        name  = "TRUST_FORWARDED"
        value = "1"
      }

      dynamic "env" {
        for_each = {
          LIVEKIT_API_KEY    = "livekit-api-key"
          LIVEKIT_API_SECRET = "livekit-api-secret"
          CLIENT_PASSCODE    = "client-passcode"
        }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.this[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_version.this,
    google_secret_manager_secret_iam_member.client,
  ]
}

# Anyone can load the page; the passcode guards the token endpoint.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count = local.client_enabled ? 1 : 0

  name     = google_cloud_run_v2_service.client[0].name
  location = google_cloud_run_v2_service.client[0].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Optional custom domain (e.g. a subdomain you own). Google issues the TLS certificate once the
# DNS record from the client_dns_records output resolves. The identity applying this must be a
# verified owner of the domain in Google Search Console (infra/README.md).
resource "google_cloud_run_domain_mapping" "client" {
  count = local.client_enabled && var.client_domain != "" ? 1 : 0

  name     = var.client_domain
  location = var.region

  metadata {
    namespace = var.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.client[0].name
  }
}
