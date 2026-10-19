# infra

OpenTofu for the agent's GCP side. Two stacks:

| Stack | State | What it creates |
|---|---|---|
| `bootstrap/` | local, applied once by hand | APIs, the GCS state bucket, GitHub Actions identities (Workload Identity Federation), a budget alert |
| `.` (this folder) | GCS bucket from `bootstrap/` | network + IAP SSH firewall rule, the agent's service account, Secret Manager secrets, a source bucket, the agent VM |

```
GitHub Actions ──OIDC──► Workload Identity Federation ──► tofu-planner (PRs) / tofu-deployer (main)
                                                                  │ tofu apply
             ┌────────────────────────────────────────────────────┘
             ▼
  agent source (GCS, content-addressed) ──► e2-micro VM: uv sync --frozen, systemd unit
  Secret Manager (LiveKit key/secret) ─────►   `python agent.py start` ──WSS + UDP──► LiveKit Cloud
```

The VM only dials out (LiveKit signalling over WSS, media over UDP), so nothing listens on a
public port. SSH goes through Identity-Aware Proxy.

## Cost: inside the GCP free tier

Everything is sized to the [free tier](https://cloud.google.com/free/docs/free-cloud-features)
(it needs a billing account, but usage inside the limits isn't charged):

| Resource | Free tier | Used here |
|---|---|---|
| Compute Engine | 1 e2-micro in us-central1/us-east1/us-west1, 30 GB pd-standard | 1 e2-micro, 20 GB pd-standard |
| External IPv4 (ephemeral) | first 744 hours/month on standard VMs, then USD 0.005/hour | 1 address, up to 744 hours |
| Egress | 1 GB/month from North America | agent audio to LiveKit: ~0.5 MB per call minute |
| Secret Manager | 6 active secret versions, 10k accesses/month | 2–3 versions, read once per boot |
| Cloud Storage | 5 GB-months in US regions | state file + ~0.5 MB per agent release |
| Workload Identity Federation, IAM, budgets | free | |

The IP allowance is the 0-price first tier of the "External IP Charge on a Standard VM" SKU in
the Cloud Billing catalog (checked October 2026); like the e2-micro hours, it is shared by the
whole billing account. Cloud NAT would avoid the public address but is billed by the hour. The
budget alert in `bootstrap/` emails the billing admins at 10%, 50% and 100% of a small amount, so
any spend shows up. Run `tofu destroy` when you're not demoing.

The agent runs on 0.25 vCPU (bursting to 2) and 1 GB of RAM plus 2 GB swap, with noise
cancellation off by default. If the agent logs "inference is slower than realtime", set
`machine_type = "e2-small"` (billed, roughly USD 12/month).

## Secrets

The LiveKit API key and secret are **ephemeral variables** passed to Secret Manager through
**write-only arguments** (`secret_data_wo`, OpenTofu 1.11+), so they never appear in a plan or
in the state. The state can't tell when a write-only value changes: bump `secrets_version` to
push new values. The VM reads them on boot with its own service account, which can access only
these secrets and the source bucket.

## First-time setup

Needs `tofu` (1.11+), `gcloud` signed in with Owner on the project and Billing Account
Administrator (or Costs Manager) on its billing account.

```bash
gcloud auth application-default login

# 1. Bootstrap (once). Fill in terraform.tfvars from the example first.
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars
tofu init && tofu apply

# 2. The app stack. Secrets come from agent/.env.local through TF_VAR_* env vars.
cd ..
cp terraform.tfvars.example terraform.tfvars
source ../scripts/tf-env.sh
export TF_VAR_project_id=<project>   # the backend block reads it at init
tofu init && tofu apply
```

First boot takes a few minutes (uv downloads Python 3.12 and the locked dependencies). Then:

```bash
tofu output -raw setup_logs | sh     # the startup script's progress
tofu output -raw agent_logs | sh     # "registered worker" means the agent is up
```

Talk to it from the local web page: `cd agent && uv run python token_server.py`, open
http://localhost:8080 (the token server runs on your machine; the agent runs on GCP).

## Deploys

A change to any of the agent's source files gives a new content hash, so `tofu apply` uploads
the files under a new prefix and **replaces the VM** (immutable deploys, a few minutes with no
agent). Rolling back is applying the previous commit.

## CI (`.github/workflows/infra.yml`)

Pull requests run `tofu plan` as `tofu-planner` (read-only) and put the plan in the job summary.
Pushes to `main` run `tofu apply` as `tofu-deployer`, which GCP only trusts for
`refs/heads/main` tokens from this repository. No service account keys exist.

Repository settings, from `tofu -chdir=infra/bootstrap output`:

| Kind | Name | Value |
|---|---|---|
| Variable | `GCP_PROJECT_ID` | project ID |
| Variable | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `workload_identity_provider` output |
| Variable | `GCP_PLANNER_SA` | `planner_service_account` output |
| Variable | `GCP_DEPLOYER_SA` | `deployer_service_account` output |
| Variable | `LIVEKIT_URL` | `wss://<project>.livekit.cloud` |
| Variable | `OTLP_ENDPOINT` | optional |
| Secret | `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | from LiveKit Cloud |
| Secret | `OTLP_HEADERS` | optional |

Also create a `production` environment (Settings → Environments) for the apply job; add
required reviewers there if applies should wait for a click.

## Tearing down

```bash
cd infra && tofu destroy               # everything that runs; nothing billable is left
cd bootstrap && tofu destroy           # optional: budget, CI identities, state bucket
```

The state bucket refuses to be deleted while it still holds state versions; empty it first
(`gcloud storage rm -r gs://<project>-tofu-state`) if you really want it gone.
