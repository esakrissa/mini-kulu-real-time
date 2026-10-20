import pytest
from aiohttp.test_utils import TestClient, TestServer
from livekit import api

from access import Access, SlidingWindow
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


# -- passcode gate and rate limits (access.py) ----------------------------------------------


def gated(**kw):
    return Access(passcode="open sesame", signing_key=SECRET.encode(), **kw)


@pytest.fixture
async def gated_client():
    async with TestClient(TestServer(create_app(URL, KEY, SECRET, gated()))) as c:
        yield c


async def test_without_a_passcode_the_gate_is_open(client):
    resp = await client.get("/api/config")
    assert await resp.json() == {"passcode_required": False, "unlocked": True}


async def test_gated_token_needs_the_passcode_cookie(gated_client):
    assert await (await gated_client.get("/api/config")).json() == {
        "passcode_required": True,
        "unlocked": False,
    }
    status, body = await post(gated_client)
    assert (status, body["error"]) == (401, "passcode required")

    wrong = await gated_client.post("/api/unlock", json={"passcode": "nope"})
    assert wrong.status == 401

    right = await gated_client.post("/api/unlock", json={"passcode": " open sesame "})
    assert right.status == 200
    cookie = right.cookies["mk_session"]
    assert cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/api"

    assert (await (await gated_client.get("/api/config")).json())["unlocked"] is True
    status, body = await post(gated_client)
    assert status == 201


def test_session_cookie_is_signed_and_expires():
    access = gated()
    cookie = access.make_cookie(now=1_000)
    assert access.cookie_ok(cookie, now=1_000)
    assert not access.cookie_ok(cookie, now=1_000 + 13 * 3600)  # past the 12 h TTL
    exp, sig = cookie.split(".")
    assert not access.cookie_ok(f"{int(exp) + 999}.{sig}", now=1_000)  # tampered expiry
    assert not access.cookie_ok("garbage", now=1_000)
    # A new passcode invalidates old sessions.
    other = Access(passcode="new one", signing_key=SECRET.encode())
    assert not other.cookie_ok(cookie, now=1_000)


async def test_passcode_attempts_are_rate_limited(gated_client):
    for _ in range(5):
        await gated_client.post("/api/unlock", json={"passcode": "guess"})
    resp = await gated_client.post("/api/unlock", json={"passcode": "open sesame"})
    assert resp.status == 429
    assert int(resp.headers["Retry-After"]) > 0


async def test_token_requests_are_rate_limited_per_ip(client):
    statuses = [(await post(client))[0] for _ in range(11)]
    assert statuses == [201] * 10 + [429]


async def test_new_calls_are_capped_per_day_but_rejoins_are_not():
    access = Access(signing_key=SECRET.encode(), daily_calls=SlidingWindow(2, 86_400))
    async with TestClient(TestServer(create_app(URL, KEY, SECRET, access))) as c:
        _, first = await post(c)
        await post(c)
        status, _ = await post(c)
        assert status == 429
        claims = verify(first["participant_token"])
        status, _ = await post(
            c, {"room_name": claims.video.room, "participant_identity": claims.identity}
        )
        assert status == 201


def test_sliding_window_frees_up_after_the_window():
    w = SlidingWindow(2, 10)
    assert w.hit("a", now=0) == 0 and w.hit("a", now=1) == 0
    assert w.hit("a", now=2) == pytest.approx(8)
    assert w.hit("b", now=2) == 0  # keys are independent
    assert w.hit("a", now=10.5) == 0


def test_client_ip_trusts_only_the_last_forwarded_hop():
    from aiohttp.test_utils import make_mocked_request

    req = make_mocked_request("POST", "/", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert Access(trust_forwarded=True).client_ip(req) == "203.0.113.9"
    assert Access(trust_forwarded=False).client_ip(req) != "203.0.113.9"
