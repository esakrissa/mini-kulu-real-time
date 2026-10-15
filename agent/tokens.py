"""Participant tokens that dispatch the agent, shared by the token server and the test caller."""

import os
from datetime import timedelta

from livekit import api

AGENT_NAME = "mini-kulu"

# LiveKit closes a room `departure_timeout` seconds after the last user leaves (default 20 s).
# Keep it above the agent's rejoin grace (AGENT_REJOIN_GRACE_S, 30 s) so the agent decides
# when a refresh has taken too long, not the server.
DEPARTURE_TIMEOUT_S = 60
TOKEN_TTL = timedelta(minutes=15)


def make_token(
    room_name: str,
    identity: str,
    name: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> str:
    # The RoomAgentDispatch makes LiveKit dispatch the agent when this join creates the room.
    token = (
        api.AccessToken(
            api_key or os.environ["LIVEKIT_API_KEY"],
            api_secret or os.environ["LIVEKIT_API_SECRET"],
        )
        .with_identity(identity)
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True)
        )
        .with_room_config(
            api.RoomConfiguration(
                departure_timeout=DEPARTURE_TIMEOUT_S,
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)],
            )
        )
    )
    if name:
        token = token.with_name(name)
    return token.to_jwt()
