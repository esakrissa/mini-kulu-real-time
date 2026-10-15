from livekit import api

from tokens import AGENT_NAME, DEPARTURE_TIMEOUT_S, make_token

KEY, SECRET = "devkey", "a-test-secret-that-is-at-least-32-chars"


def claims(**kwargs) -> api.access_token.Claims:
    token = make_token("room-1", "user-1", api_key=KEY, api_secret=SECRET, **kwargs)
    return api.TokenVerifier(KEY, SECRET).verify(token)


def test_grants_join_for_one_room_only():
    c = claims()
    assert c.identity == "user-1"
    assert c.video.room == "room-1"
    assert c.video.room_join and c.video.can_publish and c.video.can_subscribe
    assert not c.video.room_admin and not c.video.room_create


def test_dispatches_the_agent():
    agents = claims().room_config.agents
    assert [a.agent_name for a in agents] == [AGENT_NAME]


def test_room_outlives_the_rejoin_grace():
    # The agent waits 30 s for a refresh; the server must not close the room before that.
    assert claims().room_config.departure_timeout == DEPARTURE_TIMEOUT_S
    assert DEPARTURE_TIMEOUT_S > 30


def test_display_name_is_optional():
    assert claims().name in ("", None)
    assert claims(name="Esa").name == "Esa"
