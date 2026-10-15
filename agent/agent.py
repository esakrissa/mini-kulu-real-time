"""Mini-Kulu voice agent: STT -> LLM -> TTS over LiveKit.

Day 1: the stock LiveKit Agents pipeline, all models served by LiveKit Inference
(one set of LiveKit credentials, no extra provider keys).
"""

import logging
import os

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, TurnHandlingOptions, inference, room_io
from livekit.plugins import ai_coustics

from rejoin import RejoinGrace
from tokens import AGENT_NAME  # explicit dispatch: only join rooms that ask for this name

load_dotenv(".env.local")

logger = logging.getLogger("mini-kulu-agent")

# How long the agent waits for the user to come back (e.g. a browser refresh) before ending.
REJOIN_GRACE_S = float(os.getenv("AGENT_REJOIN_GRACE_S", "30"))

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
        llm=inference.LLM(model="google/gemma-4-31b-it"),
        tts=inference.TTS(
            model="fishaudio/s2.1-pro",
            voice="fa4c9eb3dccc4806b382b40d61c6b10a",
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
        ),
    )

    grace = RejoinGrace(REJOIN_GRACE_S, lambda: ctx.shutdown("user did not rejoin"))

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
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_S
                ),
            ),
        ),
    )

    await session.generate_reply(instructions="Greet the user briefly and offer your help.")


if __name__ == "__main__":
    agents.cli.run_app(server)
