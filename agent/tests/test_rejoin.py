import asyncio

from rejoin import RejoinGrace

GRACE_S = 0.05


async def test_expires_when_nobody_rejoins():
    expired = asyncio.Event()
    grace = RejoinGrace(GRACE_S, expired.set)
    grace.user_left()
    assert grace.waiting
    await asyncio.wait_for(expired.wait(), timeout=1)
    assert not grace.waiting


async def test_rejoin_cancels_the_countdown():
    calls = []
    grace = RejoinGrace(GRACE_S, lambda: calls.append("expired"))
    grace.user_left()
    grace.user_joined()
    await asyncio.sleep(GRACE_S * 3)
    assert calls == []
    assert not grace.waiting


async def test_second_leave_does_not_extend_the_deadline():
    calls = []
    grace = RejoinGrace(GRACE_S * 2, lambda: calls.append("expired"))
    grace.user_left()
    await asyncio.sleep(GRACE_S)
    grace.user_left()  # e.g. a second tab closing; must not restart the timer
    await asyncio.sleep(GRACE_S * 1.6)
    assert calls == ["expired"]


async def test_join_without_leave_is_a_no_op():
    calls = []
    grace = RejoinGrace(GRACE_S, lambda: calls.append("expired"))
    grace.user_joined()
    await asyncio.sleep(GRACE_S * 2)
    assert calls == []


async def test_can_wait_again_after_a_rejoin():
    expired = asyncio.Event()
    grace = RejoinGrace(GRACE_S, expired.set)
    grace.user_left()
    grace.user_joined()
    grace.user_left()
    await asyncio.wait_for(expired.wait(), timeout=1)
