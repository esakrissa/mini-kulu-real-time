import asyncio

from rejoin import RejoinGrace

GRACE_S = 0.05


def recorder() -> tuple[list[str], RejoinGrace]:
    reasons: list[str] = []
    return reasons, RejoinGrace(GRACE_S, reasons.append)


async def test_expires_when_nobody_rejoins():
    ended = asyncio.Event()
    grace = RejoinGrace(GRACE_S, lambda _reason: ended.set())
    grace.user_left()
    assert grace.waiting
    await asyncio.wait_for(ended.wait(), timeout=1)
    assert not grace.waiting


async def test_rejoin_cancels_the_countdown():
    reasons, grace = recorder()
    grace.user_left()
    grace.user_joined()
    await asyncio.sleep(GRACE_S * 3)
    assert reasons == []
    assert not grace.waiting


async def test_second_leave_does_not_extend_the_deadline():
    reasons: list[str] = []
    grace = RejoinGrace(GRACE_S * 2, reasons.append)
    grace.user_left()
    await asyncio.sleep(GRACE_S)
    grace.user_left()  # e.g. a second tab closing; must not restart the timer
    await asyncio.sleep(GRACE_S * 1.6)
    assert reasons == ["user did not rejoin"]


async def test_join_without_leave_is_a_no_op():
    reasons, grace = recorder()
    grace.user_joined()
    await asyncio.sleep(GRACE_S * 2)
    assert reasons == []


async def test_can_wait_again_after_a_rejoin():
    ended = asyncio.Event()
    grace = RejoinGrace(GRACE_S, lambda _reason: ended.set())
    grace.user_left()
    grace.user_joined()
    grace.user_left()
    await asyncio.wait_for(ended.wait(), timeout=1)


async def test_hang_up_ends_at_once_without_waiting():
    reasons, grace = recorder()
    grace.user_hung_up()
    grace.user_left()
    assert reasons == ["user hung up"]  # synchronously, no grace period
    assert not grace.waiting
    await asyncio.sleep(GRACE_S * 2)
    assert reasons == ["user hung up"]  # and no second end from a stray timer


async def test_hang_up_after_the_countdown_started_ends_once():
    reasons, grace = recorder()
    grace.user_left()
    grace.user_hung_up()
    grace.user_left()
    await asyncio.sleep(GRACE_S * 2)
    assert reasons == ["user hung up"]


async def test_rejoin_after_hang_up_clears_it():
    # A hang-up the user takes back (rejoins) must not end the next refresh early.
    reasons, grace = recorder()
    grace.user_hung_up()
    grace.user_joined()
    grace.user_left()
    assert reasons == []
    assert grace.waiting
