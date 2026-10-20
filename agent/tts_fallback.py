"""TTS fallback: a second voice from another vendor takes over when the first one fails.

Fish Audio (primary) and Cartesia (backup) both run on LiveKit Inference, behind the SDK's
tts.FallbackAdapter, so the backup needs no extra credentials. A provider error or a stalled
stream (no audio for `timeout_s`) moves the sentence to the next voice; the adapter keeps
probing the failed one in the background and switches back once it recovers.

The timeout must stay above the LLM watchdog's: the TTS socket also goes quiet while it waits
for the LLM's next sentence, and a slow LLM should trip the LLM watchdog, not this one.

ElevenLabs mode (AGENT_TTS_PROVIDER=elevenlabs + ELEVEN_API_KEY) puts an ElevenLabs voice at the
front of the chain, billed to your own ElevenLabs account; the Inference voices stay behind it
as the fallback. It is off by default, so the agent still only needs LiveKit credentials.
"""

import logging
from collections.abc import Sequence

from livekit.agents import APIConnectOptions, inference, tts

# Imported at module level: LiveKit plugins must register on the main thread, before the
# job processes start.
from livekit.plugins import elevenlabs

logger = logging.getLogger("mini-kulu-agent")

# "model:voice", as in LiveKit's string descriptors.
PRIMARY_VOICE = "fishaudio/s2.1-pro:fa4c9eb3dccc4806b382b40d61c6b10a"
BACKUP_VOICE = "cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"

PROVIDERS = ("inference", "elevenlabs")
# Flash v2.5: ElevenLabs' lowest-latency streaming model (~75 ms model time). v3/v4 go through
# the text-to-dialogue API and are slower to first audio.
ELEVENLABS_MODEL = "eleven_flash_v2_5"


def parse_voice(spec: str) -> tuple[str, str | None]:
    """'model:voice' -> (model, voice); a bare model uses the provider's default voice."""
    model, _, voice = spec.strip().partition(":")
    if not model:
        raise ValueError(f"empty TTS model in {spec!r}")
    return model, voice or None


def session_tts_conn_options(timeout_s: float) -> APIConnectOptions:
    # One pass through the chain per sentence: the session's default (3 retries, 2 s apart,
    # 10 s timeout) would keep the caller waiting long before the backup voice speaks.
    return APIConnectOptions(max_retry=0, timeout=timeout_s)


def use_elevenlabs(provider: str, api_key: str | None) -> bool:
    """Whether the ElevenLabs voice leads the chain. Without a key, keep talking on Inference."""
    provider = provider.strip().lower() or "inference"
    if provider not in PROVIDERS:
        raise ValueError(f"AGENT_TTS_PROVIDER must be one of {PROVIDERS}, not {provider!r}")
    if provider == "elevenlabs" and not api_key:
        logger.warning("AGENT_TTS_PROVIDER=elevenlabs but ELEVEN_API_KEY is empty; using Inference")
        return False
    return provider == "elevenlabs"


def elevenlabs_tts(api_key: str, voice_id: str | None = None, model: str | None = None) -> tts.TTS:
    return elevenlabs.TTS(
        api_key=api_key,
        voice_id=voice_id or elevenlabs.tts.DEFAULT_VOICE_ID,
        model=model or ELEVENLABS_MODEL,
    )


def build_tts(
    voices: Sequence[str] = (PRIMARY_VOICE, BACKUP_VOICE), first: Sequence[tts.TTS] = ()
) -> tts.FallbackAdapter:
    """The Inference voices in order, after any voices in `first` (e.g. ElevenLabs)."""
    instances = list(first)
    for spec in voices:
        model, voice = parse_voice(spec)
        instances.append(
            inference.TTS(model=model, voice=voice) if voice else inference.TTS(model=model)
        )
    return fallback_tts(instances)


def fallback_tts(instances: Sequence[tts.TTS]) -> tts.FallbackAdapter:
    # A voice that failed is skipped, not retried: the caller is already waiting.
    return tts.FallbackAdapter(list(instances), max_retry_per_tts=0)
