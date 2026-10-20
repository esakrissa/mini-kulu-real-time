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

# Not a credential, but it identifies the LiveKit project: kept out of plan output (public CI logs).
variable "livekit_url" {
  description = "wss://<project>.livekit.cloud"
  type        = string
  sensitive   = true

  validation {
    condition     = startswith(var.livekit_url, "wss://")
    error_message = "livekit_url must be a wss:// URL (is TF_VAR_livekit_url set?)."
  }
}

# The API key and secret are never stored in the state: they only reach write-only arguments.
variable "livekit_api_key" {
  type      = string
  sensitive = true
  ephemeral = true

  validation {
    condition     = length(var.livekit_api_key) > 0
    error_message = "livekit_api_key is empty (is TF_VAR_livekit_api_key set?)."
  }
}

variable "livekit_api_secret" {
  type      = string
  sensitive = true
  ephemeral = true

  validation {
    condition     = length(var.livekit_api_secret) > 0
    error_message = "livekit_api_secret is empty (is TF_VAR_livekit_api_secret set?)."
  }
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

  # An endpoint without credentials would deploy an agent whose every export is rejected.
  validation {
    condition     = var.otlp_endpoint == "" || length(var.otlp_headers) > 0
    error_message = "otlp_endpoint is set but otlp_headers is empty (is TF_VAR_otlp_headers / the OTLP_HEADERS secret set?)."
  }
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

variable "tts_provider" {
  description = "\"inference\" (LiveKit Inference voices only) or \"elevenlabs\" (an ElevenLabs voice first, Inference as the fallback)."
  type        = string
  default     = "inference"

  validation {
    condition     = contains(["inference", "elevenlabs"], var.tts_provider)
    error_message = "tts_provider must be \"inference\" or \"elevenlabs\"."
  }
}

variable "elevenlabs_api_key" {
  description = "ElevenLabs API key, only used with tts_provider = \"elevenlabs\"."
  type        = string
  default     = ""
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = var.tts_provider != "elevenlabs" || length(var.elevenlabs_api_key) > 0
    error_message = "tts_provider is \"elevenlabs\" but elevenlabs_api_key is empty (is the ELEVEN_API_KEY secret set?)."
  }
}

variable "elevenlabs_voice_id" {
  description = "ElevenLabs voice ID. Empty: the plugin's default voice."
  type        = string
  default     = ""
}

variable "client_image" {
  description = "Container image of the hosted web client (built by CI from client.Dockerfile). Empty: no Cloud Run client."
  type        = string
  default     = ""
}

variable "client_passcode" {
  description = "Passcode the hosted client asks for before it hands out a LiveKit token."
  type        = string
  default     = ""
  sensitive   = true
  ephemeral   = true

  # A public token endpoint without a passcode would spend the LiveKit credit for anyone.
  validation {
    condition     = var.client_image == "" || length(var.client_passcode) >= 8
    error_message = "client_image is set but client_passcode is shorter than 8 characters (is the CLIENT_PASSCODE secret set?)."
  }
}

variable "client_domain" {
  description = "Custom domain for the hosted client, e.g. demo.example.com. Empty: only the run.app URL."
  type        = string
  default     = ""
}
