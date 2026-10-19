"""TTS fallback: a second voice from another vendor takes over when the first one fails.

Fish Audio (primary) and Cartesia (backup) both run on LiveKit Inference, behind the SDK's
tts.FallbackAdapter, so the backup needs no extra credentials. A provider error or a stalled
stream (no audio for `timeout_s`) moves the sentence to the next voice; the adapter keeps
probing the failed one in the background and switches back once it recovers.

The timeout must stay above the LLM watchdog's: the TTS socket also goes quiet while it waits
for the LLM's next sentence, and a slow LLM should trip the LLM watchdog, not this one.
"""

from collections.abc import Sequence

from livekit.agents import APIConnectOptions, inference, tts

# "model:voice", as in LiveKit's string descriptors.
PRIMARY_VOICE = "fishaudio/s2.1-pro:fa4c9eb3dccc4806b382b40d61c6b10a"
BACKUP_VOICE = "cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"


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


def build_tts(voices: Sequence[str] = (PRIMARY_VOICE, BACKUP_VOICE)) -> tts.FallbackAdapter:
    instances = []
    for spec in voices:
        model, voice = parse_voice(spec)
        instances.append(
            inference.TTS(model=model, voice=voice) if voice else inference.TTS(model=model)
        )
    return fallback_tts(instances)


def fallback_tts(instances: Sequence[tts.TTS]) -> tts.FallbackAdapter:
    # A voice that failed is skipped, not retried: the caller is already waiting.
    return tts.FallbackAdapter(list(instances), max_retry_per_tts=0)
