# Latency results

## 30-turn run with metrics in Grafana Cloud (19 Oct 2026)

Same setup as below, on release `agent-af2a33983bbe` (LLM watchdog on, no failover happened),
one continuous 30-turn synthetic call about 15 minutes after the VM booted. Agent-side numbers
from the logs; the dashboard's bucket-interpolated values are in brackets.

| Stage | n | p50 | p95 | max |
|---|---|---|---|---|
| `stt_final` | 30 | 0.41 s | 0.76 s | 1.00 s |
| `end_of_turn` | 30 | 1.03 s | 2.50 s | 2.53 s |
| `llm_ttft` | 31 | 0.75 s | 1.50 s | 1.74 s |
| `tts_ttfb` | 31 | 0.50 s | 0.52 s | 0.75 s |
| `e2e` | 30 | 2.23 s [2.34 s] | 3.04 s [3.69 s] | 3.21 s |

Client-side: p50 3.27 s, p95 4.09 s.

**This is twice the first run's e2e, and the dashboard plus the agent log say why: CPU.** The
agent logged "VAD inference is slower than realtime" (0.2–0.3 s behind) and 53 "event loop
blocked" warnings during the call; the first run had neither. An e2-micro is a shared-core VM
(0.25 vCPU sustained, bursting to 2): this VM had just spent its burst on `uv sync` at boot, then
ran five minutes of non-stop audio. The slower voice-activity detection delays the end-of-turn
decision, and a blocked event loop also inflates the measured LLM first token (0.75 s vs 0.20 s;
nothing in the watchdog fired). Idle, the VM is 98% idle with no CPU steal.

So the free e2-micro is fine for a demo call, not for sustained load. The fix is more CPU
(`machine_type` in `infra/`): e2-small doubles the sustained share to 0.5 vCPU, e2-standard-2
gives two full vCPUs. At Kulu's scale: size the worker pool on measured CPU per concurrent call,
and alert on the VAD warning.


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
