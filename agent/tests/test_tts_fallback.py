import asyncio
import time

import pytest
from livekit.agents import APIConnectionError, APITimeoutError, tts

from tts_fallback import fallback_tts, parse_voice, session_tts_conn_options

SAMPLE_RATE = 24000
DEFAULT_CONN = session_tts_conn_options(5)


class FakeChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        fake: FakeTTS = self._tts  # type: ignore[assignment]
        fake.calls += 1
        if fake.fail:
            raise APIConnectionError("provider down")
        try:
            # Like the Inference TTS: the timeout is the longest wait for the next audio.
            await asyncio.wait_for(asyncio.sleep(fake.delay_s), self._conn_options.timeout)
        except TimeoutError:
            raise APITimeoutError() from None
        output_emitter.initialize(
            request_id=fake.name, sample_rate=SAMPLE_RATE, num_channels=1, mime_type="audio/pcm"
        )
        output_emitter.push(b"\x00\x00" * (SAMPLE_RATE // 10))  # 100 ms of silence
        output_emitter.flush()


class FakeTTS(tts.TTS):
    def __init__(self, name: str, *, fail: bool = False, delay_s: float = 0.0) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
        )
        self.name, self.fail, self.delay_s, self.calls = name, fail, delay_s, 0

    def synthesize(self, text, *, conn_options=DEFAULT_CONN):
        return FakeChunkedStream(tts=self, input_text=text, conn_options=conn_options)


async def speak(adapter: tts.TTS, timeout_s: float = 0.2) -> float:
    """Synthesizes one sentence; returns the seconds of audio produced."""
    seconds = 0.0
    async with adapter.synthesize(
        "Hello there.", conn_options=session_tts_conn_options(timeout_s)
    ) as s:
        async for ev in s:
            seconds += ev.frame.duration
    return seconds


def test_parse_voice():
    assert parse_voice("cartesia/sonic-3:abc-123") == ("cartesia/sonic-3", "abc-123")
    assert parse_voice(" deepgram/aura-2 ") == ("deepgram/aura-2", None)
    with pytest.raises(ValueError):
        parse_voice(":abc")


async def test_failed_primary_voice_hands_the_sentence_to_the_backup():
    primary, backup = FakeTTS("fish", fail=True), FakeTTS("cartesia")

    audio_s = await speak(fallback_tts([primary, backup]))

    assert audio_s == pytest.approx(0.1, abs=0.02)
    assert backup.calls == 1
    assert primary.calls <= 2  # one attempt, plus at most one background recovery probe


async def test_stalled_primary_voice_falls_back_within_the_timeout():
    primary, backup = FakeTTS("fish", delay_s=5), FakeTTS("cartesia")
    started = time.monotonic()

    audio_s = await speak(fallback_tts([primary, backup]), timeout_s=0.2)

    assert audio_s > 0
    assert time.monotonic() - started < 1.0  # the timeout, not the 5 s stall, decided


async def test_healthy_primary_voice_leaves_the_backup_alone():
    primary, backup = FakeTTS("fish"), FakeTTS("cartesia")

    assert await speak(fallback_tts([primary, backup])) > 0
    assert backup.calls == 0


async def test_all_voices_failing_ends_after_one_pass():
    primary, backup = FakeTTS("fish", fail=True), FakeTTS("cartesia", fail=True)
    started = time.monotonic()

    with pytest.raises(APIConnectionError):
        await speak(fallback_tts([primary, backup]))

    assert primary.calls <= 2 and backup.calls <= 2  # no replay of the chain
    assert time.monotonic() - started < 1.5
