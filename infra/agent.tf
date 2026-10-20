# The agent worker on one free-tier e2-micro VM.
#
# Deploys are immutable: the agent's source files are uploaded under a content hash, and the
# startup script names that hash, so any code change replaces the VM (a ~3 minute gap with no
# agent; fine for a demo). No container image or registry: the VM installs the locked
# dependencies with uv, which keeps the whole stack inside the free tier.

locals {
  agent_dir = "${path.module}/../agent"

  # Explicit allowlist: nothing else from agent/ (like .env.local) can be uploaded by mistake.
  agent_files = [
    ".python-version",
    "agent.py",
    "llm_watchdog.py",
    "pyproject.toml",
    "rejoin.py",
    "tokens.py",
    "tts_fallback.py",
    "turn_metrics.py",
    "uv.lock",
  ]

  source_hash   = substr(sha256(join("", [for f in local.agent_files : filesha256("${local.agent_dir}/${f}")])), 0, 12)
  source_prefix = "agent-${local.source_hash}"

  # uv standalone binary, pinned and checksum-checked on the VM.
  uv_version = "0.12.21"
  uv_sha256  = "23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0"
}

resource "google_storage_bucket" "source" {
  name     = "${var.project_id}-agent-source"
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true # only holds copies of files that live in git

  # Each apply deletes the previous release's objects (rollback = apply an older commit);
  # this only cleans up anything left behind.
  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket_object" "agent" {
  for_each = toset(local.agent_files)

  bucket = google_storage_bucket.source.name
  name   = "${local.source_prefix}/${each.value}"
  source = "${local.agent_dir}/${each.value}"
}

resource "google_storage_bucket_iam_member" "agent_source" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.agent.member
}

resource "google_compute_instance" "agent" {
  name         = "mini-kulu-agent"
  machine_type = var.machine_type
  tags         = ["mini-kulu-agent"]

  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 20
      type  = "pd-standard" # the free tier covers 30 GB of pd-standard; the default (pd-balanced) is billed
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id

    # Ephemeral public IP for outbound WebRTC. Cloud NAT would avoid it but is billed by the hour.
    access_config {}
  }

  service_account {
    email  = google_service_account.agent.email
    scopes = ["cloud-platform"] # access is limited by IAM, not scopes
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = templatefile("${path.module}/startup.sh.tftpl", {
    project_id         = var.project_id
    bucket             = google_storage_bucket.source.name
    prefix             = local.source_prefix
    files              = join(" ", local.agent_files)
    uv_version         = local.uv_version
    uv_sha256          = local.uv_sha256
    livekit_url        = var.livekit_url
    otlp_endpoint      = var.otlp_endpoint
    noise_cancellation = var.noise_cancellation ? "1" : "0"
    tts_provider       = var.tts_provider
    eleven_voice_id    = var.elevenlabs_voice_id
    secrets            = { for name, s in google_secret_manager_secret.this : name => s.secret_id }
  })

  # The VM reads these on boot, so they must exist first.
  depends_on = [
    google_storage_bucket_object.agent,
    google_storage_bucket_iam_member.agent_source,
    google_secret_manager_secret_version.this,
    google_secret_manager_secret_iam_member.agent,
  ]
}
