output "state_bucket" {
  value = google_storage_bucket.state.name
}

# The values below go into the GitHub repository's Actions variables (see infra/README.md).
output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "planner_service_account" {
  value = google_service_account.planner.email
}

output "deployer_service_account" {
  value = google_service_account.deployer.email
}
