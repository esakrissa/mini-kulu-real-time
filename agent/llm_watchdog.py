"""LLM watchdog: a per-attempt timeout, a backup model, and a spoken apology if both fail.

The primary and the backup sit behind the SDK's llm.FallbackAdapter. Each attempt may stay
silent for at most `timeout_s`: the Inference LLM passes it to its HTTP client as the read
timeout, so it trips when the first token is late or when the stream stalls mid-answer, and
the next model takes the same turn. Both models run on LiveKit Inference, so the backup needs
no extra credentials.

If every model fails, the turn ends with an unrecoverable LLM error. Apology then says a short
line, so the caller isn't left in silence wondering whether the call dropped.
"""

import logging
from collections.abc import Callable, Sequence

from livekit.agents import APIConnectOptions, ErrorEvent, inference, llm

logger = logging.getLogger("mini-kulu-agent")

PRIMARY_MODEL = "google/gemma-4-31b-it"
# A different vendor from the primary, non-reasoning (low time to first token).
BACKUP_MODEL = "openai/gpt-4.1-mini"

APOLOGY = "Sorry, I lost my train of thought there. Could you say that again?"

# One pass through the chain per turn. The session's default (3 retries, 2 s apart) would
# replay the whole chain and could leave the caller waiting ~30 s before anything happens.
SESSION_LLM_CONN_OPTIONS = APIConnectOptions(max_retry=0)


def build_llm(
    timeout_s: float, models: Sequence[str] = (PRIMARY_MODEL, BACKUP_MODEL)
) -> llm.FallbackAdapter:
    return fallback_llm([inference.LLM(model=m) for m in models], timeout_s)


def fallback_llm(llms: Sequence[llm.LLM], timeout_s: float) -> llm.FallbackAdapter:
    return llm.FallbackAdapter(
        list(llms),
        attempt_timeout=timeout_s,
        # A model that timed out is skipped, not retried: the caller is already waiting.
        max_retry_per_llm=0,
    )


class Apology:
    """Speaks APOLOGY when an LLM turn failed on every model.

    Every failed turn gets one: skipping some (a rate limit) left a caller in silence when the
    greeting and the first answer failed back to back. The SDK ends the session after a few
    failed turns in a row (SessionConnectOptions.max_unrecoverable_errors, 3), which bounds it.
    """

    def __init__(self, say: Callable[[str], object]) -> None:
        self._say = say

    def on_error(self, ev: ErrorEvent) -> None:
        err = ev.error
        if getattr(err, "type", None) != "llm_error" or getattr(err, "recoverable", True):
            return
        logger.warning("LLM turn failed on every model, apologizing to the caller")
        try:
            self._say(APOLOGY)
        except RuntimeError:  # the session is already closing
            pass
