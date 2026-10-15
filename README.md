# mini-kulu-realtime

A small, working version of a realtime voice-agent path: browser ⇄ LiveKit (WebRTC) ⇄ Python agent
(STT → LLM → TTS), with per-stage latency metrics, resilience, and all infra in OpenTofu on GCP.

> Work in progress. See the day-by-day status below.

## Layout

```
agent/          LiveKit Agents worker (Python, asyncio)
web/            minimal browser client (LiveKit JS)
infra/          OpenTofu: GCP project resources, IAM, Secret Manager, deploy target, budget
observability/  dashboard JSON + metric definitions
docs/           architecture, migration plan, latency results
```

## Run it locally

Requires [uv](https://docs.astral.sh/uv/) and a LiveKit Cloud project.

```bash
cd agent
cp .env.example .env.local   # fill in LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
uv sync
uv run python agent.py dev          # the agent worker; wait for "registered worker"
uv run python token_server.py       # token endpoint + web page on http://localhost:8080
```

Open http://localhost:8080 in Chrome or Safari, click **Start call** and talk. The page gets a
token from `POST /api/token` (LiveKit's standard token-endpoint format, so the client uses
`TokenSource.endpoint()`), and the token dispatches the `mini-kulu` agent into a fresh room.

**Refresh and hang-up.** Reloading the page within ~25 s rejoins the same room as the same
identity, and the agent carries on with the same conversation: it waits
`AGENT_REJOIN_GRACE_S` (30 s) for the user to come back before ending. **Hang up** tells the agent
over RPC that the call is over, so it ends at once instead of waiting.

## Test without a microphone

`agent/synthetic_caller.py` joins over WebRTC like a browser, speaks prompts rendered with macOS
`say`, and measures each turn from the end of its speech to the first audible reply.

```bash
cd agent
uv run python synthetic_caller.py --turns 10 --out results.jsonl   # p50/p95 turn latency
uv run python synthetic_caller.py --scenario rejoin --away 25       # leave, rejoin, check memory
uv run pytest -q && uv run ruff check .
```

Latency measured on a laptop is not meaningful (local voice-activity detection can't keep up in
real time there); real numbers come from the cloud deployment.

## Status

- [x] Day 1: LiveKit quickstart agent running locally ([screenshot](docs/day1-console.png))
- [x] Day 2: own browser client + token endpoint, refresh rejoins the same conversation
- [ ] Day 3: per-stage timing → metrics
- [ ] Day 4: OpenTofu + budget alert
- [ ] Day 5: CI plan/apply, deployed
- [ ] Day 6: p50/p95 dashboard, resilience
- [ ] Day 7: README, migration plan, video
