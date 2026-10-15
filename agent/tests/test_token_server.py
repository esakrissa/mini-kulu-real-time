import pytest
from aiohttp.test_utils import TestClient, TestServer
from livekit import api

from token_server import create_app
from tokens import AGENT_NAME

URL, KEY, SECRET = (
    "wss://example.livekit.cloud",
    "devkey",
    "a-test-secret-that-is-at-least-32-chars",
)


@pytest.fixture
async def client():
    async with TestClient(TestServer(create_app(URL, KEY, SECRET))) as c:
        yield c


async def post(client, body=None):
    resp = await client.post("/api/token", json=body if body is not None else {})
    return resp.status, await resp.json()


def verify(token: str):
    return api.TokenVerifier(KEY, SECRET).verify(token)


async def test_issues_a_token_for_a_fresh_room(client):
    status, body = await post(client)
    assert status == 201
    assert body["server_url"] == URL
    claims = verify(body["participant_token"])
    assert claims.video.room.startswith("web-")
    assert claims.identity.startswith("user-")
    assert [a.agent_name for a in claims.room_config.agents] == [AGENT_NAME]


async def test_accepts_what_the_sdk_sends_for_agent_dispatch(client):
    # TokenSource.endpoint().fetch({agentName}) packs the agent into room_config.
    status, body = await post(client, {"room_config": {"agents": [{"agent_name": AGENT_NAME}]}})
    assert status == 201


async def test_rejoin_keeps_room_and_identity(client):
    _, first = await post(client)
    c1 = verify(first["participant_token"])
    status, second = await post(
        client, {"room_name": c1.video.room, "participant_identity": c1.identity}
    )
    assert status == 201
    c2 = verify(second["participant_token"])
    assert (c2.video.room, c2.identity) == (c1.video.room, c1.identity)


@pytest.mark.parametrize(
    "body",
    [
        {"room_config": {"agents": [{"agent_name": "someone-else"}]}},
        {"room_config": {"agents": [{"agent_name": AGENT_NAME}], "max_participants": 100}},
        {"room_name": "caller-123"},  # not a room this server issued
        {"room_name": "web-../../etc"},
        {"participant_identity": "admin"},
        {"participant_metadata": "{}"},
        {"participant_attributes": {"role": "admin"}},
        {"participant_name": "x" * 65},
        ["not", "an", "object"],
    ],
)
async def test_rejects_fields_clients_may_not_set(client, body):
    status, resp = await post(client, body)
    assert status == 400, resp
    assert "error" in resp


async def test_rejects_invalid_json(client):
    resp = await client.post(
        "/api/token", data="{nope", headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400


async def test_serves_the_page_and_health(client):
    assert (await client.get("/healthz")).status == 200
    page = await client.get("/")
    assert page.status == 200
    assert "app.js" in await page.text()
