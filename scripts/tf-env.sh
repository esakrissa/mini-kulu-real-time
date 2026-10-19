# Source this (don't run it) to pass agent/.env.local to OpenTofu without printing anything:
#   source scripts/tf-env.sh
_env_file="$(git rev-parse --show-toplevel)/agent/.env.local"
_get() { grep -E "^$1=" "$_env_file" | tail -1 | cut -d= -f2-; }
export TF_VAR_livekit_url="$(_get LIVEKIT_URL)"
export TF_VAR_livekit_api_key="$(_get LIVEKIT_API_KEY)"
export TF_VAR_livekit_api_secret="$(_get LIVEKIT_API_SECRET)"
export TF_VAR_otlp_endpoint="$(_get OTEL_EXPORTER_OTLP_ENDPOINT)"
export TF_VAR_otlp_headers="$(_get OTEL_EXPORTER_OTLP_HEADERS)"
unset -f _get
unset _env_file
