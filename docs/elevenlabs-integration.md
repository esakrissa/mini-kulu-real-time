# ElevenLabs TTS integration (research notes)

**Status:** researched 14 Oct 2026, **not implemented**. Reference for adding ElevenLabs as a
TTS provider, most likely as the primary voice with the current LiveKit Inference TTS as the
automatic fallback (the "second TTS provider" resilience requirement).

## TL;DR

- LiveKit's official plugin `livekit-plugins-elevenlabs` works with your **own ElevenLabs API
  key** (`ELEVEN_API_KEY`). It is billed by ElevenLabs, not by LiveKit Inference credits
  (ElevenLabs is not one of LiveKit Inference's TTS providers).
- Plugin **1.8.6** (same version as `livekit-agents` here) supports **`eleven_v4`** and
  **`eleven_v4_turbo`** (verified in the plugin source, `models.py`).
- For a voice agent use **`eleven_v4_turbo`**. Plain `eleven_v4` is the high-quality,
  non-real-time model.
- Wrap it with `tts.FallbackAdapter([elevenlabs, fish_audio])`. If ElevenLabs fails (bad key,
  outage, quota), the session keeps talking in the fallback voice.

## What the plugin supports (v1.8.6)

`livekit/plugins/elevenlabs/models.py`:

```python
TTSModels = Literal[
    "eleven_monolingual_v1", "eleven_multilingual_v1", "eleven_multilingual_v2",
    "eleven_turbo_v2", "eleven_turbo_v2_5", "eleven_flash_v2_5", "eleven_flash_v2",
    "eleven_v3", "eleven_v3_conversational", "eleven_v4", "eleven_v4_turbo",
]
DIALOGUE_TTS_MODEL_PREFIXES = ("eleven_v3", "eleven_v4")
```

Implementation details that matter:

- **v3/v4 models go through ElevenLabs' text-to-dialogue API**, not the regular text-to-speech
  API. Streaming uses `wss://…/text-to-dialogue/multi-stream-input`, with one voice per TTS
  instance as usual.
- That endpoint takes a **narrower set of parameters**. The plugin logs a warning and ignores
  `chunk_length_schedule`, `streaming_latency`, `enable_ssml_parsing` and
  `apply_language_text_normalization` for dialogue models. No SSML, so pronunciation control
  goes through pronunciation dictionaries or inline audio tags instead.
- Auth: the `api_key=` argument or the `ELEVEN_API_KEY` env var. The constructor raises if
  neither is set.
- Default model is `eleven_turbo_v2_5`. Default output is `mp3_22050_32`. Worth testing
  `encoding="pcm_24000"` to skip MP3 decoding and see whether time to first audio improves.
- `enable_logging=False` switches ElevenLabs to zero-retention mode.
- The v4 support was about two weeks old when checked, so treat it as unproven until measured.
  The proven fallback within ElevenLabs is **`eleven_flash_v2_5`** (regular streaming websocket,
  lowest latency of their v2 line).

## Proposed wiring (sketch, untested)

```python
# agent.py
from livekit.agents import inference, tts
from livekit.plugins import elevenlabs

primary_tts = elevenlabs.TTS(
    model="eleven_v4_turbo",
    voice_id=os.environ.get("ELEVEN_VOICE_ID", elevenlabs.tts.DEFAULT_VOICE_ID),
    # encoding="pcm_24000",  # try vs. the mp3 default when measuring TTFB
)
fallback_tts = inference.TTS(
    model="fishaudio/s2.1-pro",
    voice="fa4c9eb3dccc4806b382b40d61c6b10a",
)

session = AgentSession(
    # ...stt, llm, turn_handling as today...
    tts=tts.FallbackAdapter([primary_tts, fallback_tts], max_retry_per_tts=1),
)
```

`FallbackAdapter` (in `livekit.agents.tts`, checked in the installed 1.8.6 source):

- `FallbackAdapter(tts: list[TTS], max_retry_per_tts: int = 2, sample_rate: int | None = None)`.
- With no `sample_rate`, it uses the highest rate among its TTS instances and resamples the
  others.
- It emits **`tts_availability_changed`** (`AvailabilityChangedEvent(tts, available)`). Hook it
  to log and count fallbacks. It's a good metric and alert for the dashboard: "primary TTS
  unavailable".
- Lower `max_retry_per_tts` (for example 1) so a dead primary fails over fast instead of
  retrying for seconds mid-turn.

Config and secrets:

- `.env.example`: add empty `ELEVEN_API_KEY=` and optional `ELEVEN_VOICE_ID=`.
- Cloud: store `ELEVEN_API_KEY` in GCP Secret Manager next to the LiveKit credentials and
  expose it to the agent as an env var.
- Consider a `TTS_PRIMARY=elevenlabs|inference` env toggle, so the agent still runs without an
  ElevenLabs key.

## Latency (vendor claims, to be verified)

ElevenLabs' published numbers for **v4 Turbo** are ~100 ms median inference and ~150 ms
median time to first speech over WebSocket. Both are their own measurements with network time
excluded (Sept 2026). Plain v4 is listed as not real-time. Flash v2.5 is quoted around ~75 ms.

Our own metric is **TTS time to first audio** per turn from the agent's metrics. Compare
ElevenLabs v4 Turbo vs Fish Audio S2.1 Pro on the same prompts, from the cloud deployment,
not the laptop.

## Pricing (third-party sources, check ElevenLabs' pricing page)

| Model | API price | Note |
|---|---|---|
| `eleven_v4` | ~$0.08 / 1k chars | quality model, not real-time |
| `eleven_v4_turbo` | ~$0.04 / 1k chars | real-time model for agents |
| `eleven_flash_v2_5` | ~$0.04 / 1k chars | proven low-latency option |
| Fish Audio S2.1 Pro (LiveKit Inference, current) | $15 / 1M chars ($0.015 / 1k) | billed to LiveKit credit |

- The launch discount on v4 ended 12 Oct 2026.
- Subscription plans meter in credits, about 1 credit per character. **Free plan: ~10k
  credits/month** (about 10 minutes of speech), with **no commercial license**. That's fine
  for a demo; commercial use starts on the Starter plan.
- Rough sizing: an agent reply is ~100 characters, so a 30-turn latency run is ~3k characters.
- Sources disagreed on some details (launch date, a possible price cut), so confirm before
  relying on exact numbers.

## Testing plan when implemented

1. **Happy path:** 30+ turns with ElevenLabs as primary; record TTS time to first audio p50/p95.
2. **Fallback demo:** set `ELEVEN_API_KEY` to an invalid value (or block the host). The
   conversation must continue in the Fish Audio voice, and `tts_availability_changed` must fire
   and be logged and counted.
3. **Mid-session failure:** revoke or break the key during a call (or simulate an error) and
   confirm the next turn falls back without dropping the session.
4. Unit test: a fake failing TTS + a fake working TTS in a `FallbackAdapter`, asserting the
   fallback is used and the availability event fires.

## Out of scope but related

- ElevenLabs also has realtime **STT** in the same plugin (`elevenlabs.STT(model="scribe_v2_realtime")`)
  if we ever want an STT fallback.

## Sources

- Plugin source: `livekit-plugins-elevenlabs==1.8.6` (`models.py`, `tts.py`), and
  `livekit-agents==1.8.6` (`tts/fallback_adapter.py`)
- [LiveKit docs: ElevenLabs TTS](https://docs.livekit.io/agents/models/tts/elevenlabs)
- [LiveKit Inference pricing](https://livekit.io/pricing/inference)
- [Eleven v4 explained: v4 vs Turbo, pricing (eesel)](https://www.eesel.ai/blog/eleven-v4)
- [Eleven v4 pricing 2026 (eesel)](https://www.eesel.ai/blog/eleven-v4-pricing)
- [Eleven v4 and v4 Turbo for voice agents (Digital Applied)](https://www.digitalapplied.com/blog/eleven-v4-turbo-voice-agents-latency-pricing)
- [Eleven v4 launch, price, vs v3 (Versely)](https://www.versely.studio/blog/eleven-v4-launch)
- [ElevenLabs pricing overview (UsagePricing)](https://www.usagepricing.com/blueprint/elevenlabs)
