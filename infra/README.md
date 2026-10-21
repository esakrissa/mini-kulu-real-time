# infra

OpenTofu for the agent's GCP side. Two stacks:

| Stack | State | What it creates |
|---|---|---|
| `bootstrap/` | local, applied once by hand | APIs, the GCS state bucket, GitHub Actions identities (Workload Identity Federation), the client's image repository, a budget alert |
| `.` (this folder) | GCS bucket from `bootstrap/` | network + IAP SSH firewall rule, the agent's service account, Secret Manager secrets, a source bucket, the agent VM, and (optional) the hosted web client on Cloud Run |

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
| Secret Manager | 6 active secret versions, 10k accesses/month | 2–5 versions, read once per boot / client cold start |
| Cloud Run (hosted client) | 180k vCPU-s, 360k GiB-s, 2M requests/month (request-based billing) | scale to zero, max 1 instance; one call = a few requests |
| Artifact Registry | 0.5 GB storage | the client image (~60 MB), newest 3 kept |
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

Secret Manager's free tier is 6 active secret versions. LiveKit key + secret, OTLP headers and
the client passcode use 4; the two tool credentials make 6. Turning on ElevenLabs as well would
be a 7th (about USD 0.06/month).

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
| Variable | `OTLP_ENDPOINT` | optional |
| Secret | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | from LiveKit Cloud |
| Secret | `OTLP_HEADERS` | optional |
| Variable | `TTS_PROVIDER`, `ELEVEN_VOICE_ID` + secret `ELEVEN_API_KEY` | optional: ElevenLabs mode |
| Variable | `WEB_SEARCH` = `true` + secret `EXA_API_KEY` | optional: the agent's web search tool (Exa bills per search) |
| Variable | `REPO_TOOLS` = `true` + secret `GH_MCP_TOKEN` | optional: the agent reads this repository (fine-grained token, public read-only) |
| Variable | `CLIENT_ENABLED` = `true` + secret `CLIENT_PASSCODE` (8+ chars) | optional: the hosted client |
| Variable | `CLIENT_DOMAIN` | optional: a custom domain for it (see below) |

Until the secrets are set, `tofu plan` stops at variable validation, so a half-configured
repository can't replace the VM with an empty config. Also create a `production` environment (Settings → Environments) for the apply job; add
required reviewers there if applies should wait for a click.

## Hosted web client (Cloud Run)

With `CLIENT_ENABLED=true`, every Infra run builds `client.Dockerfile` (the token server, its
three dependencies and `web/`, ~60 MB), tagged with a hash of those files. On `main` the deployer
pushes it to the Artifact Registry repository from `bootstrap/` and the same `tofu apply` points
the Cloud Run service at it. The service scales to zero, runs at most one instance, and reads
the LiveKit key/secret and its passcode from Secret Manager.

Access (`agent/access.py`): the page is public; `POST /api/unlock` trades the passcode for a
signed, HttpOnly session cookie (12 h), and `POST /api/token` only answers with that cookie.
Per client IP: 5 passcode attempts and 10 tokens per 10 minutes; at most 100 new calls a day.

### Custom domain

`google_cloud_run_domain_mapping` needs the identity that applies it (the `tofu-deployer`
service account in CI) to be a **verified owner** of the domain:

1. [Google Search Console](https://search.google.com/search-console) → Add property → Domain →
   `example.com`; add the TXT record it shows at your DNS provider and click Verify.
2. In that property: Settings → Users and permissions → Add user → the
   `deployer_service_account` email from `tofu -chdir=bootstrap output` → Owner.
3. Set the `CLIENT_DOMAIN` variable (e.g. `demo.example.com`) and re-run the Infra workflow.
4. Create the record from `tofu output client_dns_records` (a CNAME to `ghs.googlehosted.com.`)
   **as DNS only** (on Cloudflare: grey cloud). Google issues the certificate once it resolves,
   usually within 15–60 minutes.

## Tearing down

```bash
cd infra && tofu destroy               # everything that runs; nothing billable is left
cd bootstrap && tofu destroy           # optional: budget, CI identities, state bucket
```

The state bucket refuses to be deleted while it still holds state versions; empty it first
(`gcloud storage rm -r gs://<project>-tofu-state`) if you really want it gone.
