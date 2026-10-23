import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .db import Broadcast, BroadcastStatus, Order, OrderStatus, User, UTCDateTime, utcnow

Sessions = async_sessionmaker[AsyncSession]

ERROR_MAX = 2000
_SOURCE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_OPEN = (OrderStatus.QUEUED, OrderStatus.PROCESSING)


class ActiveOrderExists(Exception):
    """The user already has a queued or processing order."""


@dataclass(frozen=True)
class TgProfile:
    tg_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    is_premium: bool | None = None


@dataclass
class Stats:
    users_total: int
    users_new_day: int
    users_new_week: int
    users_active_week: int
    users_subscribed: int
    users_blocked: int
    done_day: int
    done_total: int
    failed_day: int
    queued: int
    processing: bool


def parse_source(payload: str | None) -> str | None:
    """The label from `/start <label>`, or None if it is missing or not [A-Za-z0-9_-]{1,64}."""
    if payload is not None and _SOURCE_RE.fullmatch(payload):
        return payload
    return None


# --- users ---


async def touch_user(sessions: Sessions, p: TgProfile, now: datetime | None = None) -> None:
    """Create the user or refresh the profile and last_seen; writing to the bot means it is not blocked."""
    now = now or utcnow()
    profile = dict(
        username=p.username,
        first_name=p.first_name,
        last_name=p.last_name,
        language_code=p.language_code,
        is_premium=bool(p.is_premium),
    )
    stmt = insert(User).values(tg_id=p.tg_id, first_seen=now, last_seen=now, **profile)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.tg_id], set_={**profile, "last_seen": now, "blocked": False, "blocked_at": None}
    )
    async with sessions.begin() as s:
        await s.execute(stmt)


async def set_source_if_empty(sessions: Sessions, tg_id: int, source: str | None) -> None:
    if source is None:
        return
    async with sessions.begin() as s:
        await s.execute(update(User).where(User.tg_id == tg_id, User.source.is_(None)).values(source=source))


async def set_subscription(sessions: Sessions, tg_id: int, subscribed: bool, now: datetime | None = None) -> None:
    async with sessions.begin() as s:
        await s.execute(
            update(User).where(User.tg_id == tg_id).values(is_subscribed=subscribed, subscription_checked_at=now or utcnow())
        )


async def set_blocked(sessions: Sessions, tg_id: int, blocked: bool, now: datetime | None = None) -> None:
    """An unknown user is left alone. The first block moment is kept; unblocking clears it."""
    if blocked:
        blocked_at = func.coalesce(User.blocked_at, literal(now or utcnow(), UTCDateTime))
    else:
        blocked_at = None
    async with sessions.begin() as s:
        await s.execute(update(User).where(User.tg_id == tg_id).values(blocked=blocked, blocked_at=blocked_at))


async def all_users(sessions: Sessions) -> list[User]:
    async with sessions() as s:
        return list((await s.scalars(select(User).order_by(User.first_seen, User.tg_id))).all())


async def broadcast_audience(sessions: Sessions) -> list[int]:
    async with sessions() as s:
        stmt = select(User.tg_id).where(User.blocked.is_(False)).order_by(User.first_seen, User.tg_id)
        return list((await s.scalars(stmt)).all())


# --- orders ---


async def active_order(sessions: Sessions, tg_id: int) -> Order | None:
    async with sessions() as s:
        stmt = select(Order).where(Order.tg_id == tg_id, Order.status.in_(_OPEN)).order_by(Order.id).limit(1)
        return await s.scalar(stmt)


async def active_order_ids(sessions: Sessions) -> set[int]:
    async with sessions() as s:
        return set((await s.scalars(select(Order.id).where(Order.status.in_(_OPEN)))).all())


async def create_order(
    sessions: Sessions,
    *,
    tg_id: int,
    file_id: str,
    file_size: int | None,
    duration: float | None,
    mime_type: str | None,
    file_name: str | None,
) -> tuple[int, int]:
    """Queue an order; returns (order_id, position in the line).
    Raises ActiveOrderExists if the user already has an open one (the unique index decides, so races are safe)."""
    try:
        async with sessions.begin() as s:
            order = Order(
                tg_id=tg_id, file_id=file_id, file_size=file_size, duration=duration, mime_type=mime_type, file_name=file_name
            )
            s.add(order)
            await s.flush()
            ahead = await s.scalar(
                select(func.count()).select_from(Order).where(Order.status == OrderStatus.QUEUED, Order.id < order.id)
            )
            busy = await s.scalar(select(func.count()).select_from(Order).where(Order.status == OrderStatus.PROCESSING))
            return order.id, ahead + 1 + (1 if busy else 0)
    except IntegrityError as e:
        # other violations (an unknown user: foreign key) are not ours to hide
        if "UNIQUE constraint failed: orders.tg_id" in str(e.orig):
            raise ActiveOrderExists(tg_id) from e
        raise


async def set_status_message(sessions: Sessions, order_id: int, message_id: int) -> None:
    async with sessions.begin() as s:
        await s.execute(update(Order).where(Order.id == order_id).values(status_message_id=message_id))


async def claim_next(sessions: Sessions, now: datetime | None = None) -> Order | None:
    """Move the oldest queued order to processing and return it."""
    oldest = select(Order.id).where(Order.status == OrderStatus.QUEUED).order_by(Order.id).limit(1).scalar_subquery()
    stmt = (
        update(Order)
        .where(Order.id == oldest, Order.status == OrderStatus.QUEUED)
        .values(status=OrderStatus.PROCESSING, started_at=now or utcnow())
        .returning(Order)
        .execution_options(synchronize_session=False)
    )
    async with sessions.begin() as s:
        return (await s.execute(stmt)).scalar_one_or_none()


async def finish_order(
    sessions: Sessions,
    order_id: int,
    status: OrderStatus,
    *,
    error: str | None = None,
    params: dict | None = None,
    now: datetime | None = None,
) -> None:
    """Close an open order as done or failed; done counts one more video. A closed order stays as it is."""
    if status not in (OrderStatus.DONE, OrderStatus.FAILED):
        raise ValueError(f"an order is finished as done or failed, not {status}")
    stmt = (
        update(Order)
        .where(Order.id == order_id, Order.status.in_(_OPEN))
        .values(
            status=status,
            finished_at=now or utcnow(),
            error=error[:ERROR_MAX] if error is not None else None,
            params_json=json.dumps(params, ensure_ascii=False) if params is not None else None,
        )
        .returning(Order.tg_id)
        .execution_options(synchronize_session=False)
    )
    async with sessions.begin() as s:
        tg_id = (await s.execute(stmt)).scalar_one_or_none()
        if tg_id is not None and status == OrderStatus.DONE:
            await s.execute(update(User).where(User.tg_id == tg_id).values(videos_done=User.videos_done + 1))


async def requeue_processing(sessions: Sessions) -> int:
    """After a restart: orders left in processing go back to the line. Returns how many."""
    stmt = (
        update(Order)
        .where(Order.status == OrderStatus.PROCESSING)
        .values(status=OrderStatus.QUEUED, started_at=None)
        .execution_options(synchronize_session=False)
    )
    async with sessions.begin() as s:
        return (await s.execute(stmt)).rowcount


# --- stats ---


async def stats(sessions: Sessions, now: datetime | None = None) -> Stats:
    """Counters for /stats; a day and a week are counted back from `now`."""
    now = now or utcnow()
    day, week = now - timedelta(days=1), now - timedelta(days=7)

    async def count(s: AsyncSession, model, *where) -> int:
        return await s.scalar(select(func.count()).select_from(model).where(*where))

    async with sessions() as s:
        return Stats(
            users_total=await count(s, User),
            users_new_day=await count(s, User, User.first_seen >= day),
            users_new_week=await count(s, User, User.first_seen >= week),
            users_active_week=await count(s, User, User.last_seen >= week),
            users_subscribed=await count(s, User, User.is_subscribed.is_(True)),
            users_blocked=await count(s, User, User.blocked.is_(True)),
            done_day=await count(s, Order, Order.status == OrderStatus.DONE, Order.finished_at >= day),
            done_total=await count(s, Order, Order.status == OrderStatus.DONE),
            failed_day=await count(s, Order, Order.status == OrderStatus.FAILED, Order.finished_at >= day),
            queued=await count(s, Order, Order.status == OrderStatus.QUEUED),
            processing=await count(s, Order, Order.status == OrderStatus.PROCESSING) > 0,
        )


# --- broadcasts ---


async def create_broadcast(
    sessions: Sessions,
    *,
    admin_id: int,
    source_chat_id: int,
    source_message_id: int,
    total: int,
    status: BroadcastStatus,
) -> int:
    async with sessions.begin() as s:
        row = Broadcast(
            admin_id=admin_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            total=total,
            status=status,
        )
        s.add(row)
        await s.flush()
        return row.id


async def update_broadcast(
    sessions: Sessions,
    bid: int,
    *,
    sent: int,
    blocked: int,
    failed: int,
    status: BroadcastStatus | None = None,
    finished: bool = False,
    now: datetime | None = None,
) -> None:
    values: dict = dict(sent=sent, blocked=blocked, failed=failed)
    if status is not None:
        values["status"] = status
    if finished:
        values["finished_at"] = now or utcnow()
    async with sessions.begin() as s:
        await s.execute(update(Broadcast).where(Broadcast.id == bid).values(**values))


async def interrupt_running_broadcasts(sessions: Sessions, now: datetime | None = None) -> int:
    """After a restart: a broadcast that was running does not continue; it keeps its sent count."""
    stmt = (
        update(Broadcast)
        .where(Broadcast.status == BroadcastStatus.RUNNING)
        .values(status=BroadcastStatus.INTERRUPTED, finished_at=now or utcnow())
        .execution_options(synchronize_session=False)
    )
    async with sessions.begin() as s:
        return (await s.execute(stmt)).rowcount
