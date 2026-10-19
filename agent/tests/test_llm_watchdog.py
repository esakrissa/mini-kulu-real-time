import asyncio
import time
from types import SimpleNamespace

import pytest
from livekit.agents import APIConnectionError, APITimeoutError, llm

from llm_watchdog import APOLOGY, SESSION_LLM_CONN_OPTIONS, Apology, fallback_llm


class FakeStream(llm.LLMStream):
    async def _run(self) -> None:
        fake: FakeLLM = self._llm  # type: ignore[assignment]
        fake.calls += 1
        try:
            # Like the Inference LLM: the attempt timeout is the longest wait for the next bytes.
            await asyncio.wait_for(asyncio.sleep(fake.delay_s), self._conn_options.timeout)
        except TimeoutError:
            raise APITimeoutError() from None
        self._event_ch.send_nowait(
            llm.ChatChunk(id="1", delta=llm.ChoiceDelta(role="assistant", content=fake.reply))
        )


class FakeLLM(llm.LLM):
    def __init__(self, reply: str, delay_s: float = 0.0) -> None:
        super().__init__()
        self.reply = reply
        self.delay_s = delay_s
        self.calls = 0

    def chat(self, *, chat_ctx, tools=None, conn_options=SESSION_LLM_CONN_OPTIONS, **_kwargs):
        return FakeStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


async def answer(model: llm.LLM) -> str:
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="hello")
    parts = []
    async with model.chat(chat_ctx=ctx, conn_options=SESSION_LLM_CONN_OPTIONS) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                parts.append(chunk.delta.content)
    return "".join(parts)


async def test_stalled_primary_falls_back_within_the_timeout():
    primary, backup = FakeLLM("primary", delay_s=5), FakeLLM("backup")
    started = time.monotonic()

    reply = await answer(fallback_llm([primary, backup], timeout_s=0.2))

    assert reply == "backup"
    assert time.monotonic() - started < 1.0  # the watchdog, not the 5 s stall, decided
    assert backup.calls == 1
    # The adapter also probes the failed primary in the background, to switch back once it
    # recovers: one attempt plus at most one probe.
    assert primary.calls <= 2


async def test_healthy_primary_answers_without_touching_the_backup():
    primary, backup = FakeLLM("primary"), FakeLLM("backup")

    assert await answer(fallback_llm([primary, backup], timeout_s=0.2)) == "primary"
    assert backup.calls == 0


async def test_all_models_failing_ends_the_turn_after_one_pass():
    primary, backup = FakeLLM("p", delay_s=5), FakeLLM("b", delay_s=5)
    started = time.monotonic()

    with pytest.raises(APIConnectionError):
        await answer(fallback_llm([primary, backup], timeout_s=0.2))

    # One pass: an attempt each plus at most one background recovery probe. A replay of the
    # chain (the session's default of 3 retries) would show 4+ calls per model and take ~6 s.
    assert primary.calls <= 2 and backup.calls <= 2
    assert time.monotonic() - started < 1.5


def llm_error(recoverable: bool = False, kind: str = "llm_error"):
    return SimpleNamespace(error=SimpleNamespace(type=kind, recoverable=recoverable))


def test_apology_on_unrecoverable_llm_error_only():
    said = []
    apology = Apology(said.append)

    apology.on_error(llm_error(recoverable=True))  # the next model is still trying
    apology.on_error(llm_error(kind="tts_error"))
    assert said == []

    apology.on_error(llm_error())
    assert said == [APOLOGY]


def test_apology_is_rate_limited():
    said, now = [], [100.0]
    apology = Apology(said.append, min_gap_s=10, clock=lambda: now[0])

    apology.on_error(llm_error())
    now[0] += 5
    apology.on_error(llm_error())
    now[0] += 6
    apology.on_error(llm_error())

    assert said == [APOLOGY, APOLOGY]


def test_apology_ignores_a_closing_session():
    def closed(_text: str) -> None:
        raise RuntimeError("AgentSession isn't running")

    Apology(closed).on_error(llm_error())  # must not raise
