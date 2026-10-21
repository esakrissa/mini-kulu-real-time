"""Synthetic caller: joins a room over WebRTC, talks to the agent, and measures each turn.

It dispatches the agent into a fresh room, speaks pre-rendered prompts as a real audio
track, and records client-perceived latency: from the end of the caller's speech to the
first audible frame of the agent's reply. This is the number a user actually feels, so it
includes network, STT endpointing, LLM, TTS and jitter buffers.

Prompts are rendered with macOS `say` and cached under .cache/prompts/. The text scenario
types its questions instead (LiveKit's `lk.chat` text stream, which the agent reads like
speech), so it needs no `say`, and checks the agent's tools: web search and its own repository.

    uv run python synthetic_caller.py --turns 10
    uv run python synthetic_caller.py --scenario rejoin   # leave + rejoin mid-conversation
    uv run python synthetic_caller.py --scenario text     # typed questions for the agent's tools
"""

import argparse
import asyncio
import gc
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
from livekit import rtc

from latency_stats import summarize
from rejoin import HANGUP_RPC
from tokens import make_token

load_dotenv(".env.local")
logger = logging.getLogger("synthetic-caller")

SAMPLE_RATE = 48000
FRAME_MS = 10
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
# int16 RMS above which a received frame counts as speech rather than silence/comfort noise.
SPEECH_RMS = 500
RENDER_TIMEOUT_S = 60
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


# Text scenario: typed questions that need the agent's tools, with words a right answer contains.
# The first needs web search (EXA_API_KEY on the agent), the second its repository (GH_MCP_TOKEN).
TEXT_PROMPTS = [
    (
        "What's the weather in Ubud right now?",
        ("degree", "°", "celsius", "fahrenheit", "rain", "shower", "sunny", "clear", "cloud"),
    ),
    (
        "How do you handle a browser refresh in the middle of a call? Check your own code.",
        ("rejoin", "grace", "same room", "identity", "reconnect"),
    ),
]
CHAT_TOPIC = "lk.chat"
# A tool turn can be a filler line, a pause while the tool runs, then the answer. The turn is
# over once the agent has been listening this long: above the agent's 4 s tool deadline.
TOOL_QUIET_S = 5.0

# Rejoin scenario: ask the agent to remember something, leave, rejoin, ask for it back.
REJOIN_REMEMBER = "Please remember the number forty two."
REJOIN_RECALL = "What number did I ask you to remember?"
REJOIN_EXPECT = ("42", "forty two", "forty-two")


@dataclass
class TurnResult:
    turn: int
    prompt: str
    heard: str  # what the agent's STT transcribed
    reply: str  # what the agent said
    latency_ms: float | None  # end of caller speech -> first agent audio
    error: str | None = None


def is_audible(samples: np.ndarray, threshold: float = SPEECH_RMS) -> bool:
    """True if an int16 frame is loud enough to be speech, not silence or comfort noise."""
    if samples.size == 0:
        return False
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) > threshold


def load_wav(path: Path) -> np.ndarray:
    """Load a prompt as 48 kHz mono int16, rejecting files that can't be spoken."""
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1:
            raise ValueError(f"{path.name}: expected {SAMPLE_RATE} Hz mono")
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if samples.size == 0:
        raise ValueError(f"{path.name}: no audio frames")
    return samples


def render_prompt(text: str, voice: str) -> np.ndarray:
    """Render text to 48 kHz mono int16 with macOS `say`, cached by content hash."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice}:{text}".encode()).hexdigest()[:16]
    path = CACHE_DIR / f"{key}.wav"
    if path.exists():
        return load_wav(path)
    # Render to a temp file and only move it into the cache once it's valid: a wedged or
    # killed `say` once left a 1.6 GB file with an empty header that the cache then trusted.
    tmp = path.with_suffix(".partial.wav")
    try:
        subprocess.run(
            ["say", "-v", voice, "-o", str(tmp), f"--data-format=LEI16@{SAMPLE_RATE}", text],
            check=True,
            # `say` can wedge when the speech service is overloaded; fail instead of hanging.
            timeout=RENDER_TIMEOUT_S,
        )
        samples = load_wav(tmp)
        tmp.replace(path)
        return samples
    finally:
        tmp.unlink(missing_ok=True)


class Caller:
    """One caller identity that can join, leave and rejoin the same room."""

    def __init__(self, room_name: str, identity: str = "synthetic-caller") -> None:
        self.room_name = room_name
        self.identity = identity
        self.room: rtc.Room | None = None
        self.source: rtc.AudioSource | None = None
        self.tasks: set[asyncio.Task] = set()
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

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def join(self) -> None:
        room = rtc.Room()
        self.room = room
        self.agent_state = "unknown"

        @room.on("track_subscribed")
        def _on_track(track: rtc.Track, _pub, participant: rtc.RemoteParticipant) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO and participant.kind == (
                rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            ):
                self._spawn(self.listen(track))

        @room.on("participant_attributes_changed")
        def _on_attrs(changed: dict[str, str], participant: rtc.Participant) -> None:
            if "lk.agent.state" in changed:
                self.set_agent_state(changed["lk.agent.state"])

        @room.on("participant_connected")
        def _on_join(participant: rtc.RemoteParticipant) -> None:
            if "lk.agent.state" in participant.attributes:
                self.set_agent_state(participant.attributes["lk.agent.state"])

        room.register_text_stream_handler(
            "lk.transcription", lambda reader, _identity: self._spawn(self.on_transcription(reader))
        )

        logger.info("joining room %s as %s", self.room_name, self.identity)
        await room.connect(os.environ["LIVEKIT_URL"], make_token(self.room_name, self.identity))
        # On a rejoin the agent is already in the room, so no participant_connected fires.
        for participant in room.remote_participants.values():
            _on_join(participant)

        self.source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=100)
        pub = await room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("mic", self.source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        self.local_track_sid = pub.sid
        self._spawn(self.mic_loop(self.source))

    async def hang_up(self) -> None:
        """Leave for good: tell the agent first, so it ends now instead of waiting for a rejoin."""
        agent = self.agent_participant()
        if agent is not None:
            try:
                await self.room.local_participant.perform_rpc(
                    destination_identity=agent.identity, method=HANGUP_RPC, payload=""
                )
            except rtc.RpcError as e:
                logger.warning("hang-up RPC failed (%s); the agent will time out instead", e)
        await self.leave()

    def agent_participant(self) -> rtc.RemoteParticipant | None:
        if self.room is None or not self.room.isconnected():
            return None
        agents = (
            p
            for p in self.room.remote_participants.values()
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        )
        return next(agents, None)

    async def leave(self) -> None:
        # Stop our tasks before disconnecting so no native (FFI) handles outlive the room.
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.room is not None:
            await self.room.disconnect()
        if self.source is not None:
            await self.source.aclose()
        self.room = self.source = None

    # -- outgoing audio: a continuous "microphone" of silence, with speech spliced in --
    async def mic_loop(self, source: rtc.AudioSource) -> None:
        silence = np.zeros(SAMPLES_PER_FRAME, dtype=np.int16)
        while True:
            speech, self.pending_speech = self.pending_speech, None
            if speech is not None:
                for i in range(0, len(speech), SAMPLES_PER_FRAME):
                    chunk = speech[i : i + SAMPLES_PER_FRAME]
                    if len(chunk) < SAMPLES_PER_FRAME:
                        chunk = np.pad(chunk, (0, SAMPLES_PER_FRAME - len(chunk)))
                    await source.capture_frame(self._frame(chunk))
                # Frames still queued in the source haven't been sent yet.
                self.speech_end_at = time.perf_counter() + source.queued_duration
                self.awaiting_reply = True
                self.speech_done.set()
            await source.capture_frame(self._frame(silence))

    @staticmethod
    def _frame(samples: np.ndarray) -> rtc.AudioFrame:
        return rtc.AudioFrame(samples.tobytes(), SAMPLE_RATE, 1, len(samples))

    # -- incoming audio: timestamp the first audible frame after we stop talking --
    async def listen(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            async for ev in stream:
                if not self.awaiting_reply:
                    continue
                if is_audible(np.frombuffer(ev.frame.data, dtype=np.int16)):
                    self.first_audio_at = time.perf_counter()
                    self.awaiting_reply = False
        finally:
            await stream.aclose()

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

    async def wait_until_quiet(self, step_timeout: float, quiet_s: float = TOOL_QUIET_S) -> None:
        """Wait for the agent to speak, then to stay listening for `quiet_s`."""
        await self.wait_for_state("speaking", step_timeout)
        async with asyncio.timeout(step_timeout):
            while True:
                await self.wait_for_state("listening", step_timeout)
                self.state_changed.clear()
                try:
                    await asyncio.wait_for(self.state_changed.wait(), quiet_s)
                except TimeoutError:
                    return

    async def type_turn(self, n: int, prompt: str, step_timeout: float) -> TurnResult:
        """Type `prompt` into the chat; latency runs from sending it to the first agent audio
        (on a tool turn, that's the filler line)."""
        await self.wait_for_state("listening", step_timeout)
        self.agent_transcripts.clear()
        self.first_audio_at = None
        self.speech_end_at = time.perf_counter()
        self.awaiting_reply = True
        await self.room.local_participant.send_text(prompt, topic=CHAT_TOPIC)
        try:
            await self.wait_until_quiet(step_timeout)
            await asyncio.sleep(0.3)  # let the last transcription stream close
        except TimeoutError:
            self.awaiting_reply = False
            return TurnResult(n, prompt, prompt, " ".join(self.agent_transcripts), None, "timeout")
        latency = None
        if self.first_audio_at:
            latency = round((self.first_audio_at - self.speech_end_at) * 1000, 1)
        return TurnResult(n, prompt, prompt, " ".join(self.agent_transcripts), latency)

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


def print_result(result: TurnResult) -> None:
    latency = result.latency_ms if result.latency_ms is not None else "-"
    print(
        f"[{result.turn:>2}] {latency:>7} ms  heard={result.heard!r}\n      reply={result.reply!r}"
        + (f"  ERROR={result.error}" if result.error else ""),
        flush=True,
    )


def render_all(prompts: list[str], voice: str) -> dict[str, np.ndarray]:
    """Render every prompt before joining: `say` is slow and blocking, so it must never run
    while we're in a room (it would stall the event loop and the WebRTC connection)."""
    return {p: render_prompt(p, voice) for p in dict.fromkeys(prompts)}


async def run_turns(
    caller: Caller,
    prompts: list[str],
    audio: dict[str, np.ndarray],
    step_timeout: float,
    start: int = 1,
) -> list[TurnResult]:
    results = []
    for n, prompt in enumerate(prompts, start=start):
        result = await caller.turn(n, prompt, audio[prompt], step_timeout)
        print_result(result)
        results.append(result)
    return results


async def run(turns: int, voice: str, step_timeout: float, out: Path | None) -> list[TurnResult]:
    caller = Caller(f"caller-{int(time.time())}")
    prompts = [PROMPTS[i % len(PROMPTS)] for i in range(turns)]
    audio = render_all(prompts, voice)
    await caller.join()
    try:
        # The agent greets first; wait for it to start before the first prompt.
        await caller.wait_for_state("speaking", step_timeout)
        results = await run_turns(caller, prompts, audio, step_timeout)
    finally:
        await caller.hang_up()

    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    if latencies:
        print("\nend-to-end turn latency (ms):", json.dumps(summarize(latencies)))
    if out:
        with out.open("a") as f:
            for r in results:
                f.write(json.dumps({"room": caller.room_name, **asdict(r)}) + "\n")
    return results


def answered(reply: str, expect: tuple[str, ...]) -> bool:
    reply = reply.lower()
    return any(word in reply for word in expect)


async def run_text(step_timeout: float, prompts=TEXT_PROMPTS) -> bool:
    """Type each question and check the reply contains one of its expected words."""
    caller = Caller(f"caller-text-{int(time.time())}")
    await caller.join()
    ok = True
    try:
        # Let the greeting finish: its last audio still arrives after the agent is listening,
        # and would count as the first reply to a question typed right away.
        await caller.wait_until_quiet(step_timeout, quiet_s=1.0)
        for n, (prompt, expect) in enumerate(prompts, start=1):
            result = await caller.type_turn(n, prompt, step_timeout)
            print_result(result)
            if result.error or not answered(result.reply, expect):
                print("      FAIL: no expected words in the reply")
                ok = False
    finally:
        await caller.hang_up()
    print("\ntext:", "PASS" if ok else "FAIL")
    return ok


async def run_rejoin(voice: str, step_timeout: float, away_s: float) -> bool:
    """Leave mid-conversation, rejoin as the same identity, and check the agent remembers."""
    caller = Caller(f"caller-rejoin-{int(time.time())}")
    audio = render_all([REJOIN_REMEMBER, REJOIN_RECALL], voice)
    await caller.join()
    try:
        await caller.wait_for_state("speaking", step_timeout)
        await run_turns(caller, [REJOIN_REMEMBER], audio, step_timeout)
        await caller.leave()
        print(f"     left the room; rejoining in {away_s:.0f} s (like a browser refresh)")
        await asyncio.sleep(away_s)
        await caller.join()
        [result] = await run_turns(caller, [REJOIN_RECALL], audio, step_timeout, start=2)
    finally:
        await caller.hang_up()
    ok = result.error is None and any(w in result.reply.lower() for w in REJOIN_EXPECT)
    print("\nrejoin:", "PASS (agent kept the conversation)" if ok else "FAIL")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["turns", "rejoin", "text"], default="turns")
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument("--voice", default="Samantha", help="macOS `say` voice")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-step timeout (s)")
    parser.add_argument("--away", type=float, default=3.0, help="rejoin scenario: seconds away")
    parser.add_argument("--out", type=Path, help="append per-turn results as JSON lines")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    # Third-party loggers stay at WARNING: the SDK logs every agent-session byte stream
    # it ignores at INFO, which buries the per-turn output.
    logging.basicConfig(level=logging.WARNING)
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    if args.scenario == "rejoin":
        ok = asyncio.run(run_rejoin(args.voice, args.timeout, args.away))
    elif args.scenario == "text":
        ok = asyncio.run(run_text(args.timeout))
    else:
        ok = all(
            r.error is None
            for r in asyncio.run(run(args.turns, args.voice, args.timeout, args.out))
        )
    # The room and its event-handler closures form reference cycles. Collect them now,
    # while the native SDK is still loaded; at interpreter exit its atexit hook tears the
    # library down first and every leftover handle then fails to drop.
    gc.collect()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
