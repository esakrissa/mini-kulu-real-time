# Migration plan: AWS → GCP for a realtime voice-agent platform

One page on moving a running voice-agent product from AWS to GCP without dropping calls. It
assumes a typical shape: agent workers on ECS/EC2, a Python API, Postgres on RDS, objects in
S3, DNS in Route 53, LiveKit (Cloud or self-hosted) as the media layer. The order matters more
than the details: **stateless first, data last, every step reversible.**

## 0. Before anything moves

- **Everything in OpenTofu on both sides.** Import the AWS resources that aren't in code yet
  (`tofu import` / `import` blocks), so the old side can be rebuilt and torn down from code.
- **The same metrics on both clouds.** The turn-latency histogram (`stt_final`, `end_of_turn`,
  `llm_ttft`, `tts_ttfb`, `e2e`) with a `cloud` label, plus error rate and calls served, in one
  dashboard. The migration's go/no-go is "GCP p95 ≤ AWS p95, error rate no worse", and that
  needs both sides on the same graph before the first call moves.
- **Write down the rollback for each step** before doing it, with who can run it.
- **Keyless CI into GCP** (Workload Identity Federation, as in `infra/bootstrap`), a planner
  identity for pull requests and a deployer only `main` can assume.

## 1. Parallel run: agent workers first

Agent workers are the easy part: they hold no state and only dial out to LiveKit.

1. Deploy GCP workers that register with the **same LiveKit project** under the same agent name,
   in the region nearest the SFU's region for the customers they serve.
2. Weight dispatch: start GCP at ~5% of new calls (separate agent name or dispatch rules,
   drained by deploy), compare the dashboards for a few days, then 25 / 50 / 100%.
3. Calls in progress never move. A worker that's draining (SIGTERM) finishes its calls, so a
   rollback is "send new calls back to AWS", not "cut calls".

Rollback: set the GCP share to 0%. Nothing else changed.

## 2. Stateless services: API, token service, webhooks

Run them on Cloud Run (or GKE) with the same container images, pointing at the **AWS database
over a private link** (HA VPN or Cross-Cloud Interconnect) during the overlap. Budget for the
extra cross-cloud latency on database calls and keep it off the voice path: nothing in a turn
should wait on a cross-cloud query.

## 3. Database cutover

Two options, picked by size and the allowed write downtime:

| | Dump/restore | Replication (Database Migration Service or logical replication) |
|---|---|---|
| How | `pg_dump` → Cloud SQL / AlloyDB, `pg_restore` | continuous CDC from RDS into Cloud SQL, cut over when the lag is ~0 |
| Write downtime | the whole dump + restore (minutes for GBs, hours for 100s of GBs) | seconds to a minute: stop writes, wait for lag 0, switch |
| Complexity | low, easy to rehearse | needs `rds.logical_replication`, a replication user, watching lag; sequences and large objects need extra steps |
| Fits | small databases, a maintenance window is OK | live product, calls happen around the clock |

For a live voice product: **replication**, rehearsed end to end on a staging copy first. The
cutover itself:

1. Lower the DNS TTLs and connection-pool lifetimes a day ahead.
2. Put the API in read-only / queue-writes mode (calls keep working; only writes pause).
3. Wait for replication lag 0, check row counts and checksums on the hot tables, fix sequences.
4. Point the services at Cloud SQL (one config change, deployed by CI), lift read-only.
5. Keep **reverse replication** (GCP → AWS) running for the rollback window, so going back
   doesn't lose the writes made after the switch.

Rollback: point back at RDS while the reverse replication is current. After the window (e.g. a
week with no incidents), stop it and snapshot RDS before deleting it.

## 4. Objects and DNS

- **S3 → GCS** with Storage Transfer Service: a bulk copy, then incremental syncs; services read
  from GCS with S3 as the fallback until the last sync, then write to GCS only.
- **DNS switch last.** Move public names to weighted records (Route 53 or Cloud DNS) with the
  TTL already low, shift the weight, watch error rates and turn latency per region, finish at
  100%. Move the zone itself to Cloud DNS (if wanted) only after the traffic has moved.

Rollback: shift the weight back; with a 60 s TTL most clients follow within minutes.

## 5. Decommission

After the rollback window: stop reverse replication, final RDS snapshot, `tofu destroy` the AWS
stacks module by module, and check the AWS bill drops to zero the next month. Keep the snapshot
and the AWS OpenTofu code for a while: cheap, and the fastest way back.

## What could go wrong

| Risk | Mitigation |
|---|---|
| Latency regression in a region | per-region p95 on the shared dashboard; the dispatch weight is the dial |
| Cross-cloud DB latency during the overlap | private interconnect; keep DB calls off the turn path; keep the overlap short |
| Replication breaks on schema changes | freeze migrations during the cutover week |
| Secrets drift between clouds | one source of truth (Secret Manager), synced from CI |
| Egress bill from AWS for the copy | estimate from bucket size first; AWS waives some egress for moves off AWS, ask |
