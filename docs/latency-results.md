# Latency results

## Longer calls run out of CPU on the e2-micro (22 Oct 2026)

Four calls through the hosted page (Safari, real microphone), 01:09–01:19 WITA, release
`agent-9c1ab4a324f4` with the web-search and repo tools. The caller heard the agent's audio
break up in longer calls, and the agent once said web search wasn't available.

What the VM showed (journal + Cloud Monitoring `instance/cpu/usage_time`):

- **Every call is fine for about 2.5 minutes, then the CPU is capped.** The e2-micro sustains
  0.25 vCPU and bursts above that while it has credit. The call used ~0.37 vCPU (21.9 CPU-s per
  minute) until 17:12 UTC, then the hypervisor held the whole VM at ~0.21 vCPU (12–13 CPU-s per
  minute) for the rest of the hour. From then on the worker's event loop stalled in steps of
  exactly ~0.21 s with no CPU time used (the VM wasn't scheduled), 1,065 "VAD inference is
  slower than realtime" warnings, VAD lag growing to 27 s and 45 s, `stt_final` up to 38 s, one
  `e2e` of 42.8 s and "process is unresponsive". The agent's outgoing audio is produced in the
  same starved process, which is the breaking up the caller heard.
- **The turn detector ran on the VM.** In `start` mode outside LiveKit Cloud the SDK picks the
  local `v1-mini` model; 6 of its predictions timed out ("eot prediction timed out") once the CPU
  was capped. LiveKit's docs warn about exactly this on burstable instances.
- **Session audio was recorded for LiveKit's agent insights**: both sides resampled and encoded
  to Opus for the whole call. Measured on the laptop: ~7% of a core for the encoder alone.
- **Memory**: 675 of 969 MB used and 282 MB in swap between calls. The forkserver preloads the
  local turn-detector weights (~244 MB) for every job.
- **Web search**: one search hit the 4 s deadline (4.25 s) while the CPU was capped, and the
  tool's limit of 3 searches per call was reached in the first call. Both answers told the model
  only that search didn't work, so it said search "isn't available".

Changes (PR "Fit a call into the e2-micro"): the turn detector runs on LiveKit Inference
(`AGENT_TURN_DETECTOR=v1`, a free monthly allowance; with no local fallback, a turn that can't
reach it ends on the silence delay) and its local weights aren't preloaded; session audio isn't
recorded (`AGENT_RECORD_AUDIO`; transcripts and traces still are); 6 searches per call, and the
limit is said plainly. The page now shows every tool call with its result or error.

## Calls on the hosted page (20 Oct 2026)

Nine short calls through https://mini-kulu.esakrissa.com (Safari with a real microphone, and
the fake-microphone screenshot runs), release `agent-117afb30d27e` on the e2-micro, VM booted
~6 min before the first call. Agent-side numbers from the logs; Grafana (fixed dashboard, see
below) gives the same counts and p50s within bucket interpolation.

| Stage | n | p50 | p95 | max |
|---|---|---|---|---|
| `stt_final` | 27 | 0.38 s | 0.58 s | 0.78 s |
| `end_of_turn` | 28 | 0.48 s | 2.50 s | 2.50 s |
| `llm_ttft` | 33 | 0.19 s | 0.42 s | 0.87 s |
| `tts_ttfb` | 33 | 0.40 s | 0.58 s | 0.61 s |
| `e2e` | 23 | 1.20 s | 2.92 s | 2.94 s |

Without the turns where the turn detector waited its full 2.5 s: e2e p50 1.09 s, p95 1.36 s
(n = 16). LiveKit Cloud's agent insights agree: the 7-turn call `RM_KHSCm8SAcAzb` averages a
~1.06 s reply, the 3-turn Safari call `RM_sanRnHqLwgVb` ~1.01 s (e2e 1.20 and 0.95 s).

- **No CPU starvation**: no "slower than realtime" warnings, load 0.0 between calls; only
  "event loop blocked" at job start (imports, 0.1–2.4 s, before the greeting). Calls of 1–2
  minutes never use up the burst.
- **The p95 is the turn detector.** 7 of 28 user turns hit the 2.5 s maximum endpointing delay
  (the model judged the sentence unfinished, e.g. a trailing pause after "Can you hear me"). The
  next lever is the endpointing delays and the turn detector's threshold, not more CPU.
- LLM first token 0.19 s p50 (19 Oct's 30-turn run: 0.75 s while CPU-starved).

**The dashboard showed nothing for these calls until it was fixed.** Every call runs in its own
job process, so it gets its own series (`service.instance.id`), and with a 60 s export interval a
short call leaves one or two samples. `increase()` and `rate()` need two samples and never count
the first one, so the panels read 0 turns. The queries now take each series' last value minus its
value one window earlier (or 0 for a new series); see [observability/README.md](../observability/README.md).

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
