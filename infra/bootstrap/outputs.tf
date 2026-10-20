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

output "image_repository" {
  description = "Where CI pushes the client image (the CLIENT_ENABLED flow in infra.yml)."
  value       = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
