# Keyless GitHub Actions auth (Workload Identity Federation): no service account keys exist.
# The pool only trusts OIDC tokens from one repository. Two identities:
#   planner  - any workflow run in the repo (pull requests): read-only, plus the state lock
#   deployer - runs on refs/heads/main only: can change the app stack's resources

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.this]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions OIDC"

  attribute_condition = "assertion.repository == \"${var.github_repository}\""
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  pool = google_iam_workload_identity_pool.github.name

  planner_roles = [
    "roles/viewer",
    "roles/iam.securityReviewer", # read IAM policies on secrets, buckets and service accounts
    "roles/storage.objectViewer", # read the agent source objects
  ]

  deployer_roles = [
    "roles/compute.instanceAdmin.v1",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin", # firewall rules
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser", # attach the agent's service account to the VM
    "roles/secretmanager.admin",
    "roles/storage.admin",
  ]
}

resource "google_service_account" "planner" {
  account_id   = "tofu-planner"
  display_name = "OpenTofu plan (GitHub Actions, pull requests)"
}

resource "google_service_account" "deployer" {
  account_id   = "tofu-deployer"
  display_name = "OpenTofu apply (GitHub Actions, main)"
}

resource "google_project_iam_member" "planner" {
  for_each = toset(local.planner_roles)

  project = var.project_id
  role    = each.value
  member  = google_service_account.planner.member
}

resource "google_project_iam_member" "deployer" {
  for_each = toset(local.deployer_roles)

  project = var.project_id
  role    = each.value
  member  = google_service_account.deployer.member
}

# Both need to read and lock the state.
resource "google_storage_bucket_iam_member" "state" {
  for_each = {
    planner  = google_service_account.planner.member
    deployer = google_service_account.deployer.member
  }

  bucket = google_storage_bucket.state.name
  role   = "roles/storage.objectAdmin"
  member = each.value
}

resource "google_service_account_iam_member" "planner_wif" {
  service_account_id = google_service_account.planner.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${local.pool}/attribute.repository/${var.github_repository}"
}

# The provider's attribute_condition already limits tokens to this repository.
resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${local.pool}/attribute.ref/refs/heads/main"
}
