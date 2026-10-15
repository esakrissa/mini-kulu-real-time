"""Grace period after the user leaves, so a browser refresh can rejoin the same agent.

By default an AgentSession closes as soon as its participant disconnects, and a refreshed
page would rejoin an empty room: token-based dispatch only fires when a room is created.
Instead the agent waits `grace_s` for a user to come back (same room, same identity) and
keeps the conversation context. If nobody returns in time, `on_expire` ends the job.
"""

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger("mini-kulu-agent")


class RejoinGrace:
    def __init__(self, grace_s: float, on_expire: Callable[[], None]) -> None:
        self.grace_s = grace_s
        self._on_expire = on_expire
        self._timer: asyncio.TimerHandle | None = None

    @property
    def waiting(self) -> bool:
        return self._timer is not None

    def user_left(self) -> None:
        """The last user left: start the countdown (once; a second call doesn't extend it)."""
        if self._timer is None:
            logger.info("user left; waiting %.0f s for a rejoin", self.grace_s)
            self._timer = asyncio.get_running_loop().call_later(self.grace_s, self._expire)

    def user_joined(self) -> None:
        """A user is back: cancel the countdown."""
        if self._timer is not None:
            logger.info("user rejoined within the grace period")
            self._timer.cancel()
            self._timer = None

    def _expire(self) -> None:
        self._timer = None
        logger.info("no user rejoined within %.0f s; ending the session", self.grace_s)
        self._on_expire()
