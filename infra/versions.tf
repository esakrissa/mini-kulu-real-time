terraform {
  # 1.11+: ephemeral variables and write-only arguments keep the LiveKit secret out of the state.
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.46"
    }
  }

  # The bucket comes from infra/bootstrap. OpenTofu evaluates variables in the backend block,
  # so `tofu init` only needs TF_VAR_project_id.
  backend "gcs" {
    bucket = "${var.project_id}-tofu-state"
    prefix = "mini-kulu"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone

  default_labels = {
    app = "mini-kulu"
  }
}
