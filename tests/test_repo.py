import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from uniqbot import repo
from uniqbot.db import (
    Broadcast,
    BroadcastStatus,
    Order,
    OrderStatus,
    User,
    init_db,
    make_engine,
    make_sessions,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def sessions(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    await init_db(engine)
    yield make_sessions(engine)
    await engine.dispose()


def profile(tg_id: int = 1, username: str | None = "vlad", **kw) -> repo.TgProfile:
    return repo.TgProfile(tg_id=tg_id, username=username, first_name="Vlad", **kw)


async def user(sessions, tg_id: int) -> User:
    return next(u for u in await repo.all_users(sessions) if u.tg_id == tg_id)


async def order(sessions, order_id: int) -> Order:
    async with sessions() as s:
        return await s.get(Order, order_id)


async def new_order(sessions, tg_id: int = 1, **kw) -> tuple[int, int]:
    args = dict(tg_id=tg_id, file_id=f"f{tg_id}", file_size=1000, duration=10, mime_type="video/mp4", file_name=None)
    return await repo.create_order(sessions, **{**args, **kw})


async def test_touch_user_upserts_and_refreshes_profile(sessions):
    await repo.touch_user(sessions, profile(username="old", language_code="ru"), now=NOW)
    later = NOW + timedelta(hours=3)
    await repo.touch_user(sessions, profile(username="new", is_premium=True), now=later)

    users = await repo.all_users(sessions)
    assert len(users) == 1
    u = users[0]
    assert (u.username, u.language_code, u.is_premium) == ("new", None, True)
    assert u.first_seen == NOW
    assert u.last_seen == later
    assert u.first_seen.tzinfo is not None


async def test_source_only_first_label(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    await repo.set_source_if_empty(sessions, 1, None)
    assert (await user(sessions, 1)).source is None
    await repo.set_source_if_empty(sessions, 1, "reels")
    await repo.set_source_if_empty(sessions, 1, "stories")
    assert (await user(sessions, 1)).source == "reels"

    assert repo.parse_source("bad label!") is None
    assert repo.parse_source("a" * 65) is None
    assert repo.parse_source("") is None
    assert repo.parse_source(None) is None
    assert repo.parse_source("a" * 64) == "a" * 64
    assert repo.parse_source("ig_reels-1") == "ig_reels-1"


async def test_blocked_flag(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    await repo.set_blocked(sessions, 1, True, now=NOW)
    u = await user(sessions, 1)
    assert u.blocked is True
    assert u.blocked_at == NOW

    # a repeated Forbidden keeps the first moment
    await repo.set_blocked(sessions, 1, True, now=NOW + timedelta(hours=1))
    assert (await user(sessions, 1)).blocked_at == NOW

    await repo.touch_user(sessions, profile(), now=NOW + timedelta(hours=2))
    u = await user(sessions, 1)
    assert u.blocked is False
    assert u.blocked_at is None

    await repo.set_blocked(sessions, 999, True)  # unknown user: nothing happens
    assert len(await repo.all_users(sessions)) == 1


async def test_subscription_flag(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    assert (await user(sessions, 1)).is_subscribed is None

    await repo.set_subscription(sessions, 1, True, now=NOW)
    u = await user(sessions, 1)
    assert u.is_subscribed is True
    assert u.subscription_checked_at == NOW

    await repo.set_subscription(sessions, 1, False, now=NOW + timedelta(minutes=5))
    assert (await user(sessions, 1)).is_subscribed is False
    await repo.set_subscription(sessions, 999, True)  # unknown user: nothing happens


async def test_queue_positions(sessions):
    for tg_id in (1, 2, 3):
        await repo.touch_user(sessions, profile(tg_id), now=NOW)

    a, pos_a = await new_order(sessions, 1)
    b, pos_b = await new_order(sessions, 2)
    assert (pos_a, pos_b) == (1, 2)

    claimed = await repo.claim_next(sessions, now=NOW)
    assert claimed.id == a
    assert claimed.status == OrderStatus.PROCESSING

    _, pos_c = await new_order(sessions, 3)
    assert pos_c == 3  # B ahead + A being processed + 1


async def test_claim_next_is_fifo_and_empty_queue_gives_none(sessions):
    assert await repo.claim_next(sessions) is None
    for tg_id in (1, 2):
        await repo.touch_user(sessions, profile(tg_id), now=NOW)
    a, _ = await new_order(sessions, 1)
    b, _ = await new_order(sessions, 2)

    first = await repo.claim_next(sessions, now=NOW)
    assert (first.id, first.started_at) == (a, NOW)
    assert (await repo.claim_next(sessions, now=NOW)).id == b
    assert await repo.claim_next(sessions) is None
    assert (await order(sessions, a)).started_at == NOW


async def test_one_active_order(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    assert await repo.active_order(sessions, 1) is None

    oid, _ = await new_order(sessions, mime_type="video/quicktime", file_name="IMG_1.MOV")
    queued = await repo.active_order(sessions, 1)
    assert (queued.id, queued.status) == (oid, OrderStatus.QUEUED)
    assert (queued.mime_type, queued.file_name) == ("video/quicktime", "IMG_1.MOV")

    await repo.claim_next(sessions, now=NOW)
    assert (await repo.active_order(sessions, 1)).status == OrderStatus.PROCESSING

    await repo.finish_order(sessions, oid, OrderStatus.DONE, now=NOW)
    assert await repo.active_order(sessions, 1) is None

    failed_id, _ = await new_order(sessions)
    await repo.claim_next(sessions, now=NOW)
    await repo.finish_order(sessions, failed_id, OrderStatus.FAILED, error="boom", now=NOW)
    assert await repo.active_order(sessions, 1) is None


async def test_second_open_order_is_refused(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    first, _ = await new_order(sessions)
    with pytest.raises(repo.ActiveOrderExists):
        await new_order(sessions)
    assert await repo.active_order_ids(sessions) == {first}

    await repo.claim_next(sessions, now=NOW)  # still open while processing
    with pytest.raises(repo.ActiveOrderExists):
        await new_order(sessions)
    assert await repo.active_order_ids(sessions) == {first}


@pytest.mark.parametrize("final", [OrderStatus.DONE, OrderStatus.FAILED])
async def test_new_order_after_the_previous_one_finished(sessions, final):
    await repo.touch_user(sessions, profile(), now=NOW)
    first, _ = await new_order(sessions)
    await repo.claim_next(sessions, now=NOW)
    await repo.finish_order(sessions, first, final, now=NOW)

    second, position = await new_order(sessions)
    assert (second > first, position) == (True, 1)


async def test_concurrent_orders_of_one_user_let_one_through(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    results = await asyncio.gather(*[new_order(sessions) for _ in range(5)], return_exceptions=True)

    created = [r for r in results if isinstance(r, tuple)]
    refused = [r for r in results if isinstance(r, repo.ActiveOrderExists)]
    assert (len(created), len(refused)) == (1, 4)
    assert await repo.active_order_ids(sessions) == {created[0][0]}


async def test_order_of_unknown_user_is_not_taken_for_a_second_open_order(sessions):
    with pytest.raises(IntegrityError):  # the foreign key, not ActiveOrderExists
        await new_order(sessions, 404)
    assert await repo.active_order_ids(sessions) == set()


async def test_status_message_is_stored(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    oid, _ = await new_order(sessions)
    await repo.set_status_message(sessions, oid, 777)
    assert (await order(sessions, oid)).status_message_id == 777


async def test_finish_done_counts_video_and_failed_keeps_error(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    ok, _ = await new_order(sessions)
    await repo.claim_next(sessions, now=NOW)
    params = {"speed": 1.03, "caption": "привет"}
    done_at = NOW + timedelta(minutes=3)
    await repo.finish_order(sessions, ok, OrderStatus.DONE, params=params, now=done_at)

    o = await order(sessions, ok)
    assert (o.status, o.finished_at, o.error) == (OrderStatus.DONE, done_at, None)
    assert json.loads(o.params_json) == params
    assert (await user(sessions, 1)).videos_done == 1

    bad, _ = await new_order(sessions)
    await repo.claim_next(sessions, now=NOW)
    await repo.finish_order(sessions, bad, OrderStatus.FAILED, error="x" * 3000, now=NOW)
    o = await order(sessions, bad)
    assert o.status == OrderStatus.FAILED
    assert len(o.error) == 2000
    assert o.params_json is None
    assert (await user(sessions, 1)).videos_done == 1


async def test_finished_order_is_not_finished_twice(sessions):
    await repo.touch_user(sessions, profile(), now=NOW)
    oid, _ = await new_order(sessions)
    await repo.claim_next(sessions, now=NOW)
    await repo.finish_order(sessions, oid, OrderStatus.DONE, now=NOW)
    await repo.finish_order(sessions, oid, OrderStatus.DONE, now=NOW)
    await repo.finish_order(sessions, oid, OrderStatus.FAILED, error="late", now=NOW)
    await repo.finish_order(sessions, 999, OrderStatus.DONE, now=NOW)  # unknown order: nothing happens

    assert (await order(sessions, oid)).status == OrderStatus.DONE
    assert (await user(sessions, 1)).videos_done == 1
    with pytest.raises(ValueError):
        await repo.finish_order(sessions, oid, OrderStatus.QUEUED)


async def test_requeue_processing(sessions):
    for tg_id in (1, 2):
        await repo.touch_user(sessions, profile(tg_id), now=NOW)
    a, _ = await new_order(sessions, 1)
    b, _ = await new_order(sessions, 2)
    await repo.claim_next(sessions, now=NOW)

    assert await repo.requeue_processing(sessions) == 1
    assert await repo.requeue_processing(sessions) == 0
    o = await order(sessions, a)
    assert (o.status, o.started_at) == (OrderStatus.QUEUED, None)
    assert (await repo.claim_next(sessions, now=NOW)).id == a  # it keeps its place in the line
    assert (await order(sessions, b)).status == OrderStatus.QUEUED


async def test_active_order_ids(sessions):
    for tg_id in (1, 2, 3):
        await repo.touch_user(sessions, profile(tg_id), now=NOW)
    assert await repo.active_order_ids(sessions) == set()
    a, _ = await new_order(sessions, 1)
    b, _ = await new_order(sessions, 2)
    c, _ = await new_order(sessions, 3)
    await repo.claim_next(sessions, now=NOW)
    await repo.finish_order(sessions, a, OrderStatus.DONE, now=NOW)
    await repo.claim_next(sessions, now=NOW)
    assert await repo.active_order_ids(sessions) == {b, c}


async def test_stats_windows(sessions):
    empty = await repo.stats(sessions, NOW)
    assert (empty.users_total, empty.done_total, empty.queued, empty.processing) == (0, 0, 0, False)

    # users first seen 2 hours, 3 days and 10 days ago
    for tg_id, ago in ((1, timedelta(hours=2)), (2, timedelta(days=3)), (3, timedelta(days=10))):
        await repo.touch_user(sessions, profile(tg_id, username=f"u{tg_id}"), now=NOW - ago)
    await repo.set_subscription(sessions, 1, True, now=NOW)
    await repo.set_subscription(sessions, 2, False, now=NOW)
    await repo.set_blocked(sessions, 3, True, now=NOW)

    # done 1 h and 3 d ago, failed 30 min and 3 d ago, one being processed, one waiting
    for finish_status, ago in (
        (OrderStatus.DONE, timedelta(hours=1)),
        (OrderStatus.DONE, timedelta(days=3)),
        (OrderStatus.FAILED, timedelta(minutes=30)),
        (OrderStatus.FAILED, timedelta(days=3)),
    ):
        oid, _ = await new_order(sessions, 1)
        await repo.claim_next(sessions, now=NOW - ago)
        await repo.finish_order(sessions, oid, finish_status, error="e", now=NOW - ago)
    await new_order(sessions, 2)
    await new_order(sessions, 3)
    await repo.claim_next(sessions, now=NOW)

    s = await repo.stats(sessions, NOW)
    assert s.users_total == 3
    assert s.users_new_day == 1
    assert s.users_new_week == 2
    assert s.users_active_week == 2
    assert s.users_subscribed == 1
    assert s.users_blocked == 1
    assert (s.done_day, s.done_total) == (1, 2)
    assert s.failed_day == 1
    assert (s.queued, s.processing) == (1, True)


async def test_all_users_by_first_seen_and_audience_without_blocked(sessions):
    await repo.touch_user(sessions, profile(30), now=NOW - timedelta(days=1))
    await repo.touch_user(sessions, profile(10), now=NOW - timedelta(days=3))
    await repo.touch_user(sessions, profile(20), now=NOW - timedelta(days=2))
    await repo.set_blocked(sessions, 20, True, now=NOW)

    assert [u.tg_id for u in await repo.all_users(sessions)] == [10, 20, 30]
    assert await repo.broadcast_audience(sessions) == [10, 30]


async def test_broadcast_rows(sessions):
    bid = await repo.create_broadcast(
        sessions, admin_id=5, source_chat_id=5, source_message_id=42, total=3, status=BroadcastStatus.RUNNING
    )
    await repo.update_broadcast(sessions, bid, sent=2, blocked=1, failed=0)
    async with sessions() as s:
        b = await s.get(Broadcast, bid)
    assert (b.status, b.total, b.sent, b.blocked, b.failed, b.finished_at) == (
        BroadcastStatus.RUNNING, 3, 2, 1, 0, None,
    )

    assert await repo.interrupt_running_broadcasts(sessions) == 1
    assert await repo.interrupt_running_broadcasts(sessions) == 0
    async with sessions() as s:
        b = await s.get(Broadcast, bid)
    assert b.status == BroadcastStatus.INTERRUPTED
    assert b.sent == 2  # what was already sent is kept

    done = await repo.create_broadcast(
        sessions, admin_id=5, source_chat_id=5, source_message_id=43, total=1, status=BroadcastStatus.RUNNING
    )
    await repo.update_broadcast(
        sessions, done, sent=1, blocked=0, failed=0, status=BroadcastStatus.DONE, finished=True, now=NOW
    )
    async with sessions() as s:
        b = await s.get(Broadcast, done)
    assert (b.status, b.finished_at) == (BroadcastStatus.DONE, NOW)
    assert await repo.interrupt_running_broadcasts(sessions) == 0  # a finished one is left alone
