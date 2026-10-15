"""Grace period after the user leaves, so a browser refresh can rejoin the same agent.

By default an AgentSession closes as soon as its participant disconnects, and a refreshed
page would rejoin an empty room: token-based dispatch only fires when a room is created.
Instead the agent waits `grace_s` for a user to come back (same room, same identity) and
keeps the conversation context. If nobody returns in time, `on_end` ends the job.

A refresh and a hang-up look the same on the wire (both are client-initiated disconnects),
so a client that leaves on purpose calls the HANGUP_RPC method on the agent first, and the
session then ends as soon as the user is gone instead of waiting out the grace period.
"""

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger("mini-kulu-agent")

HANGUP_RPC = "mini_kulu.hang_up"


class RejoinGrace:
    def __init__(self, grace_s: float, on_end: Callable[[str], None]) -> None:
        self.grace_s = grace_s
        self._on_end = on_end
        self._timer: asyncio.TimerHandle | None = None
        self._hung_up = False

    @property
    def waiting(self) -> bool:
        return self._timer is not None

    def user_hung_up(self) -> None:
        """The user said they're leaving for good: end as soon as they're gone."""
        self._hung_up = True

    def user_left(self) -> None:
        """The last user left: end now after a hang-up, else start the countdown (once)."""
        if self._hung_up:
            self._cancel()
            logger.info("user hung up; ending the session")
            self._on_end("user hung up")
        elif self._timer is None:
            logger.info("user left; waiting %.0f s for a rejoin", self.grace_s)
            self._timer = asyncio.get_running_loop().call_later(self.grace_s, self._expire)

    def user_joined(self) -> None:
        """A user is back: cancel the countdown."""
        self._hung_up = False
        if self._timer is not None:
            logger.info("user rejoined within the grace period")
            self._cancel()

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _expire(self) -> None:
        self._timer = None
        logger.info("no user rejoined within %.0f s; ending the session", self.grace_s)
        self._on_end("user did not rejoin")
