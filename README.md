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

## Run the agent locally

Requires [uv](https://docs.astral.sh/uv/) and a LiveKit Cloud project.

```bash
cd agent
cp .env.example .env.local   # fill in LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
uv sync
uv run python agent.py dev
```

Then open your project's **Agents → Console** in LiveKit Cloud, set the agent name to `mini-kulu`,
and start a session.

## Status

- [x] Day 1: LiveKit quickstart agent running locally ([screenshot](docs/day1-console.png))
- [ ] Day 2: own browser client, STT → LLM → TTS round trip
- [ ] Day 3: per-stage timing → metrics
- [ ] Day 4: OpenTofu + budget alert
- [ ] Day 5: CI plan/apply, deployed
- [ ] Day 6: p50/p95 dashboard, resilience
- [ ] Day 7: README, migration plan, video
