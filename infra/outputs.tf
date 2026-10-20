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

output "client_url" {
  description = "The hosted web client."
  value = (
    !local.client_enabled ? null :
    var.client_domain != "" ? "https://${var.client_domain}" : google_cloud_run_v2_service.client[0].uri
  )
}

output "client_dns_records" {
  description = "DNS records to create for client_domain (DNS only, not proxied)."
  value = [
    for r in try(google_cloud_run_domain_mapping.client[0].status[0].resource_records, []) :
    { type = r.type, name = r.name, value = r.rrdata }
  ]
}
