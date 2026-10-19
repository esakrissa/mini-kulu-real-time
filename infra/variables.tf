variable "project_id" {
  description = "GCP project prepared by infra/bootstrap."
  type        = string
}

# One e2-micro VM in us-central1, us-east1 or us-west1 is in the free tier.
variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "machine_type" {
  description = "e2-micro is free (one per billing account). e2-small is the next step up if it can't keep up."
  type        = string
  default     = "e2-micro"
}

variable "livekit_url" {
  description = "wss://<project>.livekit.cloud"
  type        = string
}

# The API key and secret are never stored in the state: they only reach write-only arguments.
variable "livekit_api_key" {
  type      = string
  sensitive = true
  ephemeral = true
}

variable "livekit_api_secret" {
  type      = string
  sensitive = true
  ephemeral = true
}

variable "otlp_endpoint" {
  description = "OTLP/HTTP endpoint for the turn-latency metric (e.g. Grafana Cloud). Empty: metrics are only logged."
  type        = string
  default     = ""
}

variable "otlp_headers" {
  description = "OTEL_EXPORTER_OTLP_HEADERS value (e.g. Authorization=Basic ...). Only used with otlp_endpoint."
  type        = string
  default     = ""
  sensitive   = true
  ephemeral   = true
}

variable "secrets_version" {
  description = "Bump to push new values of the write-only secrets (the state can't tell they changed)."
  type        = number
  default     = 1
}

variable "noise_cancellation" {
  description = "ai-coustics noise cancellation. Off by default: it is CPU-heavy for an e2-micro and the free plan has 100 min/month."
  type        = bool
  default     = false
}
