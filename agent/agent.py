"""Mini-Kulu voice agent: STT -> LLM -> TTS over LiveKit.

All models are served by LiveKit Inference (one set of LiveKit credentials, no extra provider
keys), with an optional ElevenLabs voice in front. The LLM runs behind a watchdog with a backup
model (llm_watchdog), the voice behind a fallback to a second vendor (tts_fallback). Each
turn's per-stage latency is recorded by turn_metrics and pushed over OTLP. Optional tools from
remote MCP servers (agent_tools): web search through Exa, and reading this project's own repo.

Sized for one shared-core e2-micro (0.25 vCPU sustained, bursts for a couple of minutes): the
turn detector runs on LiveKit Inference instead of on the VM, and the session audio isn't
recorded. With both on the VM, a call used ~0.4 vCPU, ran out of burst after ~2.5 minutes and
the audio started breaking up (docs/latency-results.md).
"""

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    TurnHandlingOptions,
    inference,
    llm,
    room_io,
)
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import ai_coustics

from agent_tools import exa_search, github_files, project_file_tool, web_search_tool
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

# "v1" (default) runs the end-of-turn model on LiveKit Inference (a free monthly allowance on
# every plan). The SDK's default outside LiveKit Cloud is "v1-mini", which runs on our CPU: on
# the e2-micro its predictions timed out and starved the audio. With v1 there is no local
# fallback (that would load ~244 MB of weights mid-call): if Inference can't be reached, turns
# end on the silence delay instead.
TURN_DETECTOR = os.getenv("AGENT_TURN_DETECTOR", "v1")
if TURN_DETECTOR == "v1":
    # Nothing local to preload then; saves the e2-micro ~244 MB of RAM (it was swapping).
    os.environ.setdefault("LIVEKIT_AGENTS_PRELOAD_LOCAL_INFERENCE", "0")

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

# Tools from remote MCP servers, each on only when its credential is set. A tool call may take
# at most AGENT_TOOL_TIMEOUT_S (connecting included); past that the answer comes without it.
EXA_API_KEY = os.getenv("EXA_API_KEY", "")
GH_MCP_TOKEN = os.getenv("GH_MCP_TOKEN", "")
TOOL_TIMEOUT_S = float(os.getenv("AGENT_TOOL_TIMEOUT_S", "4"))
# Searches are billed per request (Exa, ~$0.007 each), so each call gets a limited number.
MAX_SEARCHES = int(os.getenv("AGENT_MAX_SEARCHES", "6"))
# Every tool call is reported to the page on this text-stream topic (web/app.js shows it).
TOOL_EVENTS_TOPIC = "mini_kulu.tool"

# Session audio for LiveKit Cloud's agent insights: encoding both sides to Opus costs CPU for
# the whole call. Transcripts and traces are still uploaded.
RECORD_AUDIO = os.getenv("AGENT_RECORD_AUDIO", "0").strip().lower() in ("1", "true", "yes", "on")

INSTRUCTIONS = """You are Kulu, a helpful voice assistant that joins live calls.
Keep answers short and conversational: one to three sentences.
Never use markdown, lists, emojis, or symbols that can't be spoken."""

TOOL_INSTRUCTIONS = {
    "web_search": "For news, weather, prices or anything recent, use web_search first. "
    "Answer from the results in your own words; don't read out web addresses.",
    "read_project_file": "You run on mini-kulu, an open-source project. When asked how you "
    "work or are built, read the most relevant file with read_project_file and answer from it.",
}


class Assistant(Agent):
    def __init__(self, tools: list[llm.Tool]) -> None:
        names = [t.info.name for t in tools]
        extra = [TOOL_INSTRUCTIONS[n] for n in names if n in TOOL_INSTRUCTIONS]
        super().__init__(instructions="\n".join([INSTRUCTIONS, *extra]), tools=tools)


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
            turn_detection=inference.TurnDetector(
                version=TURN_DETECTOR, local_fallback=TURN_DETECTOR != "v1"
            ),
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

    # Tool calls as they happen, for the page: what was asked, the result or the error.
    sending: set[asyncio.Task[object]] = set()

    def send_tool_event(event: dict[str, object]) -> None:
        if not ctx.room.isconnected():
            return
        task = asyncio.create_task(
            ctx.room.local_participant.send_text(
                json.dumps(event), topic=TOOL_EVENTS_TOPIC, compress=False
            )
        )
        sending.add(task)
        task.add_done_callback(sent)

    def sent(task: asyncio.Task[object]) -> None:
        sending.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("tool event not sent: %s", task.exception())

    # Remote tools connect in the background, so a slow MCP server never delays the greeting.
    tools: list[llm.Tool] = []
    remotes = []
    if EXA_API_KEY:
        search = exa_search(EXA_API_KEY, TOOL_TIMEOUT_S)
        tools.append(
            web_search_tool(
                search, MAX_SEARCHES, turn_metrics.record_tool_call, on_event=send_tool_event
            )
        )
        remotes.append(search)
    if GH_MCP_TOKEN:
        files = github_files(GH_MCP_TOKEN, TOOL_TIMEOUT_S)
        tools.append(
            project_file_tool(
                files, on_latency=turn_metrics.record_tool_call, on_event=send_tool_event
            )
        )
        remotes.append(files)
    for remote in remotes:
        remote.connect()
        ctx.add_shutdown_callback(remote.aclose)

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
        agent=Assistant(tools),
        record={"audio": RECORD_AUDIO},
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
