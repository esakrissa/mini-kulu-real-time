"""Synthetic caller: joins a room over WebRTC, talks to the agent, and measures each turn.

It dispatches the agent into a fresh room, speaks pre-rendered prompts as a real audio
track, and records client-perceived latency: from the end of the caller's speech to the
first audible frame of the agent's reply. This is the number a user actually feels, so it
includes network, STT endpointing, LLM, TTS and jitter buffers.

Prompts are rendered with macOS `say` and cached under .cache/prompts/.

    uv run python synthetic_caller.py --turns 10
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc

from latency_stats import summarize

load_dotenv(".env.local")
logger = logging.getLogger("synthetic-caller")

AGENT_NAME = "mini-kulu"
SAMPLE_RATE = 48000
FRAME_MS = 10
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
# int16 RMS above which a received frame counts as speech rather than silence/comfort noise.
SPEECH_RMS = 500
CACHE_DIR = Path(__file__).parent / ".cache" / "prompts"

PROMPTS = [
    "What is WebRTC, in one sentence?",
    "Give me one tip for running a good meeting.",
    "What is the capital of Indonesia?",
    "How many minutes are in three hours?",
    "Suggest a name for a friendly robot.",
    "Why is the sky blue? Keep it short.",
    "What does an infrastructure engineer do?",
    "Tell me a fun fact about Bali.",
    "What is two plus two?",
    "Say goodbye in Indonesian.",
]


@dataclass
class TurnResult:
    turn: int
    prompt: str
    heard: str  # what the agent's STT transcribed
    reply: str  # what the agent said
    latency_ms: float | None  # end of caller speech -> first agent audio
    error: str | None = None


def render_prompt(text: str, voice: str) -> np.ndarray:
    """Render text to 48 kHz mono int16 with macOS `say`, cached by content hash."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice}:{text}".encode()).hexdigest()[:16]
    path = CACHE_DIR / f"{key}.wav"
    if not path.exists():
        subprocess.run(
            ["say", "-v", voice, "-o", str(path), f"--data-format=LEI16@{SAMPLE_RATE}", text],
            check=True,
        )
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE and wf.getnchannels() == 1
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)


def make_token(room_name: str, identity: str) -> str:
    # Embedding a RoomAgentDispatch in the token makes LiveKit dispatch the agent
    # as soon as this participant joins.
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)])
        )
        .to_jwt()
    )


class Caller:
    def __init__(self, room: rtc.Room, source: rtc.AudioSource) -> None:
        self.room = room
        self.source = source
        self.agent_state = "unknown"
        self.state_changed = asyncio.Event()
        self.pending_speech: np.ndarray | None = None
        self.speech_done = asyncio.Event()
        self.speech_end_at: float | None = None
        self.first_audio_at: float | None = None
        self.awaiting_reply = False
        self.user_transcripts: list[str] = []
        self.agent_transcripts: list[str] = []
        self.local_track_sid: str | None = None

    # -- outgoing audio: a continuous "microphone" of silence, with speech spliced in --
    async def mic_loop(self) -> None:
        silence = np.zeros(SAMPLES_PER_FRAME, dtype=np.int16)
        while True:
            speech, self.pending_speech = self.pending_speech, None
            if speech is not None:
                for i in range(0, len(speech), SAMPLES_PER_FRAME):
                    chunk = speech[i : i + SAMPLES_PER_FRAME]
                    if len(chunk) < SAMPLES_PER_FRAME:
                        chunk = np.pad(chunk, (0, SAMPLES_PER_FRAME - len(chunk)))
                    await self.source.capture_frame(self._frame(chunk))
                # Frames still queued in the source haven't been sent yet.
                self.speech_end_at = time.perf_counter() + self.source.queued_duration
                self.awaiting_reply = True
                self.speech_done.set()
            await self.source.capture_frame(self._frame(silence))

    @staticmethod
    def _frame(samples: np.ndarray) -> rtc.AudioFrame:
        return rtc.AudioFrame(samples.tobytes(), SAMPLE_RATE, 1, len(samples))

    # -- incoming audio: timestamp the first audible frame after we stop talking --
    async def listen(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        async for ev in stream:
            if not self.awaiting_reply:
                continue
            samples = np.frombuffer(ev.frame.data, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            if rms > SPEECH_RMS:
                self.first_audio_at = time.perf_counter()
                self.awaiting_reply = False

    async def on_transcription(self, reader: rtc.TextStreamReader) -> None:
        text = (await reader.read_all()).strip()
        attrs = reader.info.attributes or {}
        if not text:
            return
        if attrs.get("lk.transcribed_track_id") == self.local_track_sid:
            if attrs.get("lk.transcription_final") == "true":
                self.user_transcripts.append(text)
        else:
            self.agent_transcripts.append(text)

    def set_agent_state(self, state: str) -> None:
        if state != self.agent_state:
            logger.debug("agent state: %s -> %s", self.agent_state, state)
            self.agent_state = state
            self.state_changed.set()

    async def wait_for_state(self, state: str, step_timeout: float) -> None:
        async with asyncio.timeout(step_timeout):
            while self.agent_state != state:
                self.state_changed.clear()
                await self.state_changed.wait()

    async def turn(self, n: int, prompt: str, audio: np.ndarray, step_timeout: float) -> TurnResult:
        await self.wait_for_state("listening", step_timeout)
        self.user_transcripts.clear()
        self.agent_transcripts.clear()
        self.first_audio_at = None
        self.speech_done.clear()
        self.pending_speech = audio
        try:
            await asyncio.wait_for(self.speech_done.wait(), step_timeout)
            await self.wait_for_state("speaking", step_timeout)
            await self.wait_for_state("listening", step_timeout)
            await asyncio.sleep(0.3)  # let the last transcription stream close
        except TimeoutError:
            self.awaiting_reply = False
            return TurnResult(n, prompt, "", "", None, error="timeout")
        latency = None
        if self.first_audio_at and self.speech_end_at:
            latency = round((self.first_audio_at - self.speech_end_at) * 1000, 1)
        return TurnResult(
            n,
            prompt,
            heard=" ".join(self.user_transcripts),
            reply=" ".join(self.agent_transcripts),
            latency_ms=latency,
        )


async def run(turns: int, voice: str, step_timeout: float, out: Path | None) -> list[TurnResult]:
    prompts = [PROMPTS[i % len(PROMPTS)] for i in range(turns)]
    audio = [render_prompt(p, voice) for p in prompts]

    room_name = f"caller-{int(time.time())}"
    room = rtc.Room()
    source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=100)
    caller = Caller(room, source)
    tasks: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, _pub, participant: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.kind == (
            rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        ):
            spawn(caller.listen(track))

    @room.on("participant_attributes_changed")
    def _on_attrs(changed: dict[str, str], participant: rtc.Participant) -> None:
        if "lk.agent.state" in changed:
            caller.set_agent_state(changed["lk.agent.state"])

    @room.on("participant_connected")
    def _on_join(participant: rtc.RemoteParticipant) -> None:
        if "lk.agent.state" in participant.attributes:
            caller.set_agent_state(participant.attributes["lk.agent.state"])

    room.register_text_stream_handler(
        "lk.transcription", lambda reader, _identity: spawn(caller.on_transcription(reader))
    )

    logger.info("joining room %s", room_name)
    await room.connect(os.environ["LIVEKIT_URL"], make_token(room_name, "synthetic-caller"))
    pub = await room.local_participant.publish_track(
        rtc.LocalAudioTrack.create_audio_track("mic", source),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    caller.local_track_sid = pub.sid
    spawn(caller.mic_loop())

    results: list[TurnResult] = []
    try:
        # The agent greets first; wait for it to finish before the first prompt.
        await caller.wait_for_state("speaking", step_timeout)
        for n, (prompt, samples) in enumerate(zip(prompts, audio, strict=True), start=1):
            result = await caller.turn(n, prompt, samples, step_timeout)
            results.append(result)
            print(
                f"[{n:>2}] {result.latency_ms if result.latency_ms is not None else '-':>7} ms"
                f"  heard={result.heard!r}\n      reply={result.reply!r}"
                + (f"  ERROR={result.error}" if result.error else ""),
                flush=True,
            )
    finally:
        await room.disconnect()
        for task in tasks:
            task.cancel()

    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    if latencies:
        print("\nend-to-end turn latency (ms):", json.dumps(summarize(latencies)))
    if out:
        with out.open("a") as f:
            for r in results:
                f.write(json.dumps({"room": room_name, **asdict(r)}) + "\n")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument("--voice", default="Samantha", help="macOS `say` voice")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-step timeout (s)")
    parser.add_argument("--out", type=Path, help="append per-turn results as JSON lines")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    asyncio.run(run(args.turns, args.voice, args.timeout, args.out))


if __name__ == "__main__":
    main()
