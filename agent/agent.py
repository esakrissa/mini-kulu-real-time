"""Mini-Kulu voice agent: STT -> LLM -> TTS over LiveKit.

All models are served by LiveKit Inference (one set of LiveKit credentials, no extra provider
keys), with an optional ElevenLabs voice in front. The LLM runs behind a watchdog with a backup
model (llm_watchdog), the voice behind a fallback to a second vendor (tts_fallback). Each
turn's per-stage latency is recorded by turn_metrics and pushed over OTLP.
"""

import logging
import os

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference, room_io
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import ai_coustics

from llm_watchdog import BACKUP_MODEL, PRIMARY_MODEL, SESSION_LLM_CONN_OPTIONS, Apology, build_llm
from rejoin import HANGUP_RPC, RejoinGrace
from tokens import AGENT_NAME  # explicit dispatch: only join rooms that ask for this name
from tts_fallback import (
    BACKUP_VOICE,
    PRIMARY_VOICE,
    build_tts,
    elevenlabs_tts,
    session_tts_conn_options,
    use_elevenlabs,
)
from turn_metrics import process_turn_metrics

load_dotenv(".env.local")

logger = logging.getLogger("mini-kulu-agent")

# How long the agent waits for the user to come back (e.g. a browser refresh) before ending.
REJOIN_GRACE_S = float(os.getenv("AGENT_REJOIN_GRACE_S", "30"))

# LLM watchdog: the longest the caller waits for the model's next bytes before the backup
# model takes the turn. Gemma's time to first token is ~0.2 s at p50 and ~0.9 s at p95.
LLM_TIMEOUT_S = float(os.getenv("AGENT_LLM_TIMEOUT_S", "3"))
# Primary first. Overriding it (e.g. a model name that doesn't exist) demos the failover.
LLM_MODELS = [
    m.strip()
    for m in os.getenv("AGENT_LLM_MODELS", f"{PRIMARY_MODEL},{BACKUP_MODEL}").split(",")
    if m.strip()
]

# TTS fallback: the longest wait for audio before the backup voice takes the sentence. Keep it
# above LLM_TIMEOUT_S (the TTS also waits on the LLM's next sentence). Voices are model:voice,
# primary first; a primary that doesn't exist demos the failover.
TTS_TIMEOUT_S = float(os.getenv("AGENT_TTS_TIMEOUT_S", "5"))
TTS_VOICES = [
    v.strip()
    for v in os.getenv("AGENT_TTS_VOICES", f"{PRIMARY_VOICE},{BACKUP_VOICE}").split(",")
    if v.strip()
]

# ElevenLabs mode: "elevenlabs" puts an ElevenLabs voice (your own key) in front of TTS_VOICES.
# Off by default; without ELEVEN_API_KEY it logs a warning and stays on Inference.
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
USE_ELEVENLABS = use_elevenlabs(os.getenv("AGENT_TTS_PROVIDER", "inference"), ELEVEN_API_KEY)
ELEVEN_VOICE_ID = os.getenv("ELEVEN_VOICE_ID") or None
ELEVEN_MODEL = os.getenv("ELEVEN_MODEL") or None

# ai-coustics noise cancellation: on for real calls. Turn it off (=0) for synthetic test runs:
# it costs CPU on a slow laptop and counts against the plan's monthly noise-cancellation minutes.
NOISE_CANCELLATION = os.getenv("AGENT_NOISE_CANCELLATION", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

INSTRUCTIONS = """You are Kulu, a helpful voice assistant that joins live calls.
Keep answers short and conversational: one to three sentences.
Never use markdown, lists, emojis, or symbols that can't be spoken."""


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


server = AgentServer(
    # Keep one job process pre-warmed so a new call doesn't pay the import cost.
    num_idle_processes=int(os.getenv("AGENT_IDLE_PROCESSES", "1")),
    # Importing the SDK takes ~9 s on a slow laptop; the 10 s default is too tight.
    initialize_process_timeout=float(os.getenv("AGENT_INIT_TIMEOUT_S", "60")),
)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: agents.JobContext) -> None:
    session = AgentSession(
        stt=inference.STT(model="assemblyai/universal-3-6-pro", language="en"),
        llm=build_llm(LLM_TIMEOUT_S, LLM_MODELS),
        tts=build_tts(
            TTS_VOICES,
            first=[elevenlabs_tts(ELEVEN_API_KEY, ELEVEN_VOICE_ID, ELEVEN_MODEL)]
            if USE_ELEVENLABS
            else [],
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
        ),
        conn_options=SessionConnectOptions(
            llm_conn_options=SESSION_LLM_CONN_OPTIONS,
            tts_conn_options=session_tts_conn_options(TTS_TIMEOUT_S),
        ),
    )

    # If the primary and the backup both fail a turn, say so instead of going silent.
    apology = Apology(lambda text: session.say(text, add_to_chat_ctx=False))
    session.on("error", apology.on_error)

    # Per-stage latency (STT, end of turn, LLM TTFT, TTS TTFB, end to end) for every turn.
    turn_metrics = process_turn_metrics()
    session.on("conversation_item_added", turn_metrics.on_conversation_item_added)
    ctx.add_shutdown_callback(turn_metrics.flush)

    grace = RejoinGrace(REJOIN_GRACE_S, ctx.shutdown)

    def is_user(p: rtc.RemoteParticipant) -> bool:
        return p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT

    @ctx.room.on("participant_disconnected")
    def _on_left(p: rtc.RemoteParticipant) -> None:
        if is_user(p) and not any(is_user(o) for o in ctx.room.remote_participants.values()):
            grace.user_left()

    @ctx.room.on("participant_connected")
    def _on_joined(p: rtc.RemoteParticipant) -> None:
        if is_user(p):
            grace.user_joined()

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_options=room_io.RoomOptions(
            # We decide when to end (RejoinGrace), so a refresh keeps the same session.
            # The returning user must use the same identity: room_io re-links by identity.
            close_on_disconnect=False,
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=(
                    ai_coustics.audio_enhancement(model=ai_coustics.EnhancerModel.QUAIL_VF_S)
                    if NOISE_CANCELLATION
                    else None
                ),
            ),
        ),
    )

    # Registered after start(), once the room is connected and local_participant exists.
    @ctx.room.local_participant.register_rpc_method(HANGUP_RPC)
    async def _on_hang_up(data: rtc.RpcInvocationData) -> str:
        grace.user_hung_up()
        return "ok"

    await session.generate_reply(instructions="Greet the user briefly and offer your help.")


if __name__ == "__main__":
    agents.cli.run_app(server)
