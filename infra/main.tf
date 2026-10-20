# The app stack: one VM that runs the LiveKit agent worker, its secrets and its network.
# The agent dials out to LiveKit Cloud (signalling over WSS, media over UDP), so nothing
# needs an inbound port except SSH through Identity-Aware Proxy for debugging.

# --- Network --------------------------------------------------------------------------------

resource "google_compute_network" "main" {
  name                    = "mini-kulu"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "main" {
  name          = "mini-kulu-${var.region}"
  network       = google_compute_network.main.id
  ip_cidr_range = "10.10.0.0/24"
}

resource "google_compute_firewall" "iap_ssh" {
  name    = "mini-kulu-allow-iap-ssh"
  network = google_compute_network.main.id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # Google's IAP TCP forwarding range: `gcloud compute ssh --tunnel-through-iap` only.
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["mini-kulu-agent"]
}

# --- Identity -------------------------------------------------------------------------------

# The VM's identity. It can read its own secrets and source objects, nothing else.
resource "google_service_account" "agent" {
  account_id   = "mini-kulu-agent"
  display_name = "Mini-Kulu agent VM"
}

# --- Secrets --------------------------------------------------------------------------------

locals {
  # Names only: for_each can't be built from ephemeral values.
  secret_names = concat(
    ["livekit-api-key", "livekit-api-secret"],
    var.otlp_endpoint == "" ? [] : ["otlp-headers"],
    var.tts_provider == "elevenlabs" ? ["elevenlabs-api-key"] : [],
    local.client_enabled ? ["client-passcode"] : [],
  )
  secret_values = {
    "livekit-api-key"    = var.livekit_api_key
    "livekit-api-secret" = var.livekit_api_secret
    "otlp-headers"       = var.otlp_headers
    "elevenlabs-api-key" = var.elevenlabs_api_key
    "client-passcode"    = var.client_passcode
  }
}

resource "google_secret_manager_secret" "this" {
  for_each = toset(local.secret_names)

  secret_id = "mini-kulu-${each.value}"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "this" {
  for_each = google_secret_manager_secret.this

  secret = each.value.id
  # Write-only: sent to Secret Manager, never written to the plan or the state.
  secret_data_wo         = local.secret_values[each.key]
  secret_data_wo_version = var.secrets_version
}

resource "google_secret_manager_secret_iam_member" "agent" {
  # The agent doesn't need the web client's passcode.
  for_each = { for name, s in google_secret_manager_secret.this : name => s if name != "client-passcode" }

  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.agent.member
}
