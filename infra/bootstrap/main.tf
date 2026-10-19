# One-time setup for a fresh GCP project, applied by hand with local state:
# APIs, the remote state bucket for ../ (the app stack), keyless GitHub Actions
# identities (plan on PRs, apply on main) and a budget alert on the project.

terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.46"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# The Billing Budgets API rejects user credentials unless a quota project is set.
provider "google" {
  alias                 = "billing"
  project               = var.project_id
  billing_project       = var.project_id
  user_project_override = true
}

data "google_project" "this" {}

locals {
  # Everything the app stack and CI use is enabled here, so neither needs Service Usage admin.
  services = [
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "iap.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ]
}

resource "google_project_service" "this" {
  for_each = toset(local.services)

  service = each.value
  # Turning APIs off on destroy breaks anything else in the project; they cost nothing while on.
  disable_on_destroy = false
}

# --- Remote state for the app stack ---------------------------------------------------------

resource "google_storage_bucket" "state" {
  name     = "${var.project_id}-tofu-state"
  location = var.region # us-central1/us-east1/us-west1 storage is in the free tier

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Keep the last 5 versions of the state file, so a bad apply can be rolled back.
  lifecycle_rule {
    condition {
      num_newer_versions = 5
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.this]
}

# --- Budget alert ---------------------------------------------------------------------------

# A budget only sends emails (to the billing account's admins); it never stops spending.
# Set it low: everything here is meant to fit in the free tier, so any spend is a surprise.
resource "google_billing_budget" "project" {
  provider = google.billing

  billing_account = var.billing_account
  display_name    = "${var.project_id} guardrail"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      # Must be in the billing account's currency; leaving currency_code out uses it.
      units = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.1
  }
  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.this]
}
