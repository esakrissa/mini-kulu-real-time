output "agent_instance" {
  value = google_compute_instance.agent.name
}

output "agent_release" {
  description = "Content hash of the agent source running on the VM."
  value       = local.source_prefix
}

output "agent_logs" {
  description = "Follow the agent's logs."
  value       = "gcloud compute ssh ${google_compute_instance.agent.name} --zone ${var.zone} --project ${var.project_id} --tunnel-through-iap -- sudo journalctl -fu mini-kulu-agent"
}

output "setup_logs" {
  description = "The startup script's output (first boot takes a few minutes)."
  value       = "gcloud compute instances get-serial-port-output ${google_compute_instance.agent.name} --zone ${var.zone} --project ${var.project_id} | grep startup-script"
}
