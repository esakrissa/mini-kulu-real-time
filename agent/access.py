"""Who may get a token from the hosted client: a passcode gate and per-IP rate limits.

A public URL that hands out tokens spends LiveKit inference credit for anyone who finds it.
With CLIENT_PASSCODE set, a browser first trades the passcode for a signed session cookie
(POST /api/unlock), and /api/token only answers with that cookie. Every endpoint is rate
limited per client IP, and new calls are capped per day. Without a passcode (local use) the
gate is open, the limits still apply.

State lives in memory, so the limits are per instance: the Cloud Run service runs at most
one instance, which makes them global.
"""

import hashlib
import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from aiohttp import web

COOKIE_NAME = "mk_session"
SESSION_TTL_S = 12 * 3600


@dataclass
class SlidingWindow:
    """At most `limit` events per `window_s` seconds for each key."""

    limit: int
    window_s: float
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def hit(self, key: str, now: float | None = None) -> float:
        """Records an event. Returns 0 if allowed, else the seconds until the next one is."""
        now = time.monotonic() if now is None else now
        q = self._events[key]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return self.window_s - (now - q[0])
        q.append(now)
        if len(self._events) > 10_000:  # forget idle keys so memory stays bounded
            for k in [k for k, v in self._events.items() if not v]:
                del self._events[k]
        return 0.0


@dataclass
class Access:
    passcode: str = ""
    signing_key: bytes = b""
    secure_cookie: bool = False
    trust_forwarded: bool = False
    unlock_limit: SlidingWindow = field(default_factory=lambda: SlidingWindow(5, 600))
    token_limit: SlidingWindow = field(default_factory=lambda: SlidingWindow(10, 600))
    daily_calls: SlidingWindow = field(default_factory=lambda: SlidingWindow(100, 86_400))

    @property
    def required(self) -> bool:
        return bool(self.passcode)

    def _key(self) -> bytes:
        # Changing the passcode (or the LiveKit secret) invalidates every session cookie.
        return hashlib.sha256(
            b"mini-kulu-client\0" + self.signing_key + b"\0" + self.passcode.encode()
        ).digest()

    def check_passcode(self, attempt: str) -> bool:
        return bool(self.passcode) and hmac.compare_digest(attempt.encode(), self.passcode.encode())

    def make_cookie(self, now: float | None = None) -> str:
        exp = int((time.time() if now is None else now) + SESSION_TTL_S)
        sig = hmac.new(self._key(), str(exp).encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def cookie_ok(self, value: str | None, now: float | None = None) -> bool:
        if not self.required:
            return True
        exp, _, sig = (value or "").partition(".")
        if not exp.isdigit() or int(exp) < (time.time() if now is None else now):
            return False
        expected = hmac.new(self._key(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def unlocked(self, request: web.Request) -> bool:
        return self.cookie_ok(request.cookies.get(COOKIE_NAME))

    def set_cookie(self, response: web.StreamResponse) -> None:
        response.set_cookie(
            COOKIE_NAME,
            self.make_cookie(),
            max_age=SESSION_TTL_S,
            httponly=True,
            secure=self.secure_cookie,
            samesite="Strict",
            path="/api",
        )

    def client_ip(self, request: web.Request) -> str:
        # Behind Google's front end the real client is the last X-Forwarded-For entry; earlier
        # ones come from the client and can be forged.
        if self.trust_forwarded:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.rsplit(",", 1)[-1].strip()
        return request.remote or "unknown"


def too_many(retry_after_s: float, what: str) -> web.Response:
    wait = max(1, int(retry_after_s + 0.999))
    return web.json_response(
        {"error": f"too many {what}, try again in {wait} s", "retry_after_s": wait},
        status=429,
        headers={"Retry-After": str(wait), "Cache-Control": "no-store"},
    )
