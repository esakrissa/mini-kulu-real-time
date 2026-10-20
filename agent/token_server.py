"""Token endpoint and static host for the browser client (web/).

POST /api/token follows LiveKit's standard token-endpoint format, so the browser uses
livekit-client's TokenSource.endpoint() unchanged. The server, not the client, decides what
a token allows: one room, the `mini-kulu` agent, no metadata.

Room names and identities are random and unguessable, and a client may send them back to
rejoin after a page refresh. Hosted (Cloud Run), CLIENT_PASSCODE puts the token endpoint behind
a passcode and every endpoint is rate limited per IP (access.py).

    uv run python token_server.py      # http://localhost:8080 (HOST/PORT env override)

It listens on localhost only by default; set HOST=0.0.0.0 inside a container.
"""

import logging
import os
import re
import secrets
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from access import Access, too_many
from tokens import AGENT_NAME, make_token

logger = logging.getLogger("token-server")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ROOM_RE = re.compile(r"^web-[0-9a-f]{16}$")
IDENTITY_RE = re.compile(r"^user-[0-9a-f]{16}$")
MAX_NAME_LEN = 64

CONFIG_KEY = web.AppKey("config", dict)
ACCESS_KEY = web.AppKey("access", Access)
MAX_PASSCODE_LEN = 128


class BadRequest(Exception):
    pass


def parse_request(body: object) -> tuple[str, str, str | None]:
    """Validate a token request; returns (room_name, identity, display_name)."""
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")

    room = body.get("room_name") or f"web-{secrets.token_hex(8)}"
    identity = body.get("participant_identity") or f"user-{secrets.token_hex(8)}"
    if not isinstance(room, str) or not ROOM_RE.match(room):
        raise BadRequest("room_name must be one this server issued")
    if not isinstance(identity, str) or not IDENTITY_RE.match(identity):
        raise BadRequest("participant_identity must be one this server issued")

    name = body.get("participant_name") or None
    if name is not None and (not isinstance(name, str) or len(name) > MAX_NAME_LEN):
        raise BadRequest(f"participant_name must be a string of at most {MAX_NAME_LEN} chars")

    if body.get("participant_metadata") or body.get("participant_attributes"):
        raise BadRequest("participant metadata and attributes are not supported")

    room_config = body.get("room_config") or {}
    if not isinstance(room_config, dict) or set(room_config) - {"agents"}:
        raise BadRequest("room_config may only name the agent")
    agents = room_config.get("agents") or []
    requested = {a.get("agent_name") for a in agents if isinstance(a, dict)}
    if not isinstance(agents, list) or len(requested) != len(agents) or requested - {AGENT_NAME}:
        raise BadRequest(f"only the {AGENT_NAME!r} agent can be dispatched")

    return room, identity, name


async def client_config(request: web.Request) -> web.Response:
    access = request.app[ACCESS_KEY]
    return web.json_response(
        {"passcode_required": access.required, "unlocked": access.unlocked(request)},
        headers={"Cache-Control": "no-store"},
    )


async def unlock(request: web.Request) -> web.Response:
    access = request.app[ACCESS_KEY]
    if wait := access.unlock_limit.hit(access.client_ip(request)):
        return too_many(wait, "passcode attempts")
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    attempt = body.get("passcode") if isinstance(body, dict) else None
    if not isinstance(attempt, str) or len(attempt) > MAX_PASSCODE_LEN:
        return web.json_response({"error": "passcode must be a string"}, status=400)
    if not access.check_passcode(attempt.strip()):
        logger.info("wrong passcode from %s", access.client_ip(request))
        return web.json_response({"error": "wrong passcode"}, status=401)
    response = web.json_response({"unlocked": True}, headers={"Cache-Control": "no-store"})
    access.set_cookie(response)
    return response


async def issue_token(request: web.Request) -> web.Response:
    access = request.app[ACCESS_KEY]
    if not access.unlocked(request):
        return web.json_response({"error": "passcode required"}, status=401)
    if wait := access.token_limit.hit(access.client_ip(request)):
        return too_many(wait, "token requests")
    try:
        body = await request.json() if request.can_read_body else {}
        room, identity, name = parse_request(body)
    except ValueError:
        return web.json_response({"error": "invalid JSON"}, status=400)
    except BadRequest as e:
        return web.json_response({"error": str(e)}, status=400)
    # A missing room_name means a new call (a rejoin sends its room back): cap those per day.
    if not (isinstance(body, dict) and body.get("room_name")):
        if wait := access.daily_calls.hit("all"):
            return too_many(wait, "calls today")

    cfg = request.app[CONFIG_KEY]
    token = make_token(room, identity, name, api_key=cfg["api_key"], api_secret=cfg["api_secret"])
    logger.info("issued token room=%s identity=%s", room, identity)
    return web.json_response(
        {"server_url": cfg["url"], "participant_token": token},
        status=201,
        headers={"Cache-Control": "no-store"},
    )


async def healthz(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def static_file(name: str):
    async def handler(_: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-cache"})

    return handler


def create_app(
    url: str, api_key: str, api_secret: str, access: Access | None = None
) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app[CONFIG_KEY] = {"url": url, "api_key": api_key, "api_secret": api_secret}
    app[ACCESS_KEY] = access or Access(signing_key=api_secret.encode())
    app.router.add_get("/api/config", client_config)
    app.router.add_post("/api/unlock", unlock)
    app.router.add_post("/api/token", issue_token)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", static_file("index.html"))
    app.router.add_get("/app.js", static_file("app.js"))
    return app


def main() -> None:
    load_dotenv(".env.local")
    logging.basicConfig(level=logging.INFO)
    secret = os.environ["LIVEKIT_API_SECRET"]
    on = ("1", "true", "yes", "on")
    access = Access(
        passcode=os.getenv("CLIENT_PASSCODE", "").strip(),
        signing_key=secret.encode(),
        # Both on in Cloud Run: HTTPS only, and Google's front end sets X-Forwarded-For.
        secure_cookie=os.getenv("COOKIE_SECURE", "0").lower() in on,
        trust_forwarded=os.getenv("TRUST_FORWARDED", "0").lower() in on,
    )
    logger.info("passcode gate %s", "on" if access.required else "off")
    app = create_app(os.environ["LIVEKIT_URL"], os.environ["LIVEKIT_API_KEY"], secret, access)
    web.run_app(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
