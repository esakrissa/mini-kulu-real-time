# Latency results

## First cloud run (19 Oct 2026, n = 8 turns)

Setup: agent on the free-tier e2-micro in us-central1 (`infra/`), noise cancellation off,
LiveKit Inference for STT (AssemblyAI Universal 3.6 Pro), LLM (Gemma 4 31B) and TTS (Fish Audio
S2.1 Pro). Caller: `synthetic_caller.py --turns 8` from a laptop in Bali (prompts rendered with
macOS `say`). Agent-side numbers come from the `mini_kulu.turn.stage_latency` log lines.

| Stage | n | p50 | p95 | max |
|---|---|---|---|---|
| `stt_final` | 8 | 0.32 s | 1.24 s | 1.24 s |
| `end_of_turn` | 8 | 0.37 s | 2.50 s | 2.50 s |
| `llm_ttft` | 9 | 0.20 s | 0.89 s | 0.89 s |
| `tts_ttfb` | 9 | 0.40 s | 0.61 s | 0.61 s |
| `e2e` | 7 | 1.04 s | 2.92 s | 2.92 s |

Client-side (caller stops sending speech → first agent audio, includes the Bali ↔ US path):
p50 1.58 s, p95 3.47 s.

For comparison, the same pipeline with the agent on a 2012 laptop gave e2e 2.7–3.2 s: local
voice-activity detection fell behind real time there. On the e2-micro there were no "slower
than realtime" warnings, load average 0.2, and memory at ~750 of 969 MB plus ~90 MB of swap.

Notes:
- With 8 turns, p95 is just the slowest turn. The README numbers come from 30+ turns on the
  Grafana dashboard.
- The p95 `end_of_turn` (2.5 s) is the first turn: the turn detector waited out a pause in the
  synthetic speech. The agent also logged "transcript arrives after turn has been committed"
  once.
- Rejoin after 10 s away: PASS on the deployed agent (it remembered a number across the
  reconnect).
