from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fakes import FakeBot, make_deps, register, tg_user
from sqlalchemy import select

from uniqbot import repo
from uniqbot.broadcast import BroadcastResult, send_broadcast
from uniqbot.db import Broadcast, BroadcastStatus

ADMIN_CHAT, POST_ID = 11, 5
MARKUP = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Канал", url="https://t.me/test_channel")]])
METHOD = CopyMessage(chat_id=1, from_chat_id=ADMIN_CHAT, message_id=POST_ID)


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(method=METHOD, message="Forbidden: bot was blocked by the user")


def bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=METHOD, message="Bad Request: chat not found")


def retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=METHOD, message="Flood control", retry_after=seconds)


@pytest.fixture
async def env(tmp_path):
    bot = FakeBot()
    deps, engine = await make_deps(tmp_path, bot=bot)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def new_row(total: int) -> int:
        """The broadcasts row exists before the send starts, as in the bot."""
        return await repo.create_broadcast(
            deps.sessions, admin_id=ADMIN_CHAT, source_chat_id=ADMIN_CHAT, source_message_id=POST_ID,
            total=total, status=BroadcastStatus.RUNNING,
        )

    async def run(audience: list[int], bid: int | None = None, **kwargs) -> tuple[BroadcastResult, int]:
        bid = bid or await new_row(len(audience))
        result = await send_broadcast(
            bot, deps.sessions, bid, chat_id=ADMIN_CHAT, message_id=POST_ID, reply_markup=MARKUP,
            audience=audience, sleep=sleep, **kwargs,
        )
        return result, bid

    async def row(bid: int) -> Broadcast:
        async with deps.sessions() as s:
            return await s.scalar(select(Broadcast).where(Broadcast.id == bid))

    yield SimpleNamespace(bot=bot, deps=deps, sleeps=sleeps, new_row=new_row, run=run, row=row)
    await engine.dispose()


async def is_blocked(env, tg_id: int) -> bool:
    return next(u for u in await repo.all_users(env.deps.sessions) if u.tg_id == tg_id).blocked


async def test_broadcast_paces_and_uses_copy_message(env):
    result, _ = await env.run([1, 2])

    assert result == BroadcastResult(sent=2, blocked=0, failed=0)
    assert env.bot.called("copy_message") == [
        dict(chat_id=u, from_chat_id=ADMIN_CHAT, message_id=POST_ID, reply_markup=MARKUP) for u in (1, 2)
    ]
    assert env.sleeps == [0.05]  # 20 a second: one gap between the two sends, none after the last


async def test_broadcast_pace_follows_the_rate(env):
    await env.run([1, 2, 3], rate_per_sec=4)
    assert env.sleeps == [0.25, 0.25]


async def test_broadcast_retry_after(env):
    env.bot.copy_errors[1] = [retry_after(3)]

    result, _ = await env.run([1, 2])

    assert result.sent == 2
    assert [c["chat_id"] for c in env.bot.called("copy_message")] == [1, 1, 2]  # the same recipient again
    assert env.sleeps == [3, 0.05]  # the wait, and the pacing is still there after it


async def test_broadcast_forbidden_marks_blocked(env):
    for user_id in (1, 2, 3):
        await register(env.deps, tg_user(user_id))
    env.bot.copy_errors[1] = [forbidden()]
    env.bot.copy_errors[2] = [bad_request()]

    result, bid = await env.run([1, 2, 3])

    assert result == BroadcastResult(sent=1, blocked=1, failed=1)
    assert await is_blocked(env, 1) is True
    assert await is_blocked(env, 2) is False  # an ordinary error is only a failure
    assert await is_blocked(env, 3) is False
    saved = await env.row(bid)
    assert (saved.sent, saved.blocked, saved.failed) == (1, 1, 1)


async def test_broadcast_other_telegram_errors_are_failures(env):
    env.bot.copy_errors[1] = [TelegramNetworkError(method=METHOD, message="timeout")]

    result, _ = await env.run([1, 2])

    assert result == BroadcastResult(sent=1, blocked=0, failed=1)


@pytest.mark.parametrize(
    ("total", "points"),
    [(250, [(100, 250), (200, 250), (250, 250)]), (200, [(100, 200), (200, 200)]), (3, [(3, 3)])],
)
async def test_broadcast_progress_points(env, total, points):
    reported: list[tuple[int, int]] = []

    async def on_progress(done: int, count: int) -> None:
        reported.append((done, count))

    await env.run(list(range(1, total + 1)), on_progress=on_progress)

    assert reported == points


async def test_broadcast_progress_and_row(env):
    for user_id in (1, 2):
        await register(env.deps, tg_user(user_id))
    env.bot.copy_errors[1] = [forbidden()]
    env.bot.copy_errors[2] = [bad_request()]
    bid = await env.new_row(250)
    seen: list[tuple[int, str, int, int, int, bool]] = []

    async def on_progress(done: int, total: int) -> None:
        saved = await env.row(bid)
        seen.append((done, saved.status, saved.sent, saved.blocked, saved.failed, saved.finished_at is not None))

    result, _ = await env.run(list(range(1, 251)), bid, on_progress=on_progress)

    running, done = BroadcastStatus.RUNNING, BroadcastStatus.DONE
    assert seen == [
        (100, running, 98, 1, 1, False),  # the counters are in the row at every point, not only at the end
        (200, running, 198, 1, 1, False),
        (250, done, 248, 1, 1, True),
    ]
    assert result == BroadcastResult(sent=248, blocked=1, failed=1)


async def test_broadcast_to_nobody_is_finished_at_once(env):
    result, bid = await env.run([])

    assert result == BroadcastResult()
    saved = await env.row(bid)
    assert (saved.status, saved.finished_at is not None) == (BroadcastStatus.DONE, True)
    assert env.sleeps == [] and env.bot.called("copy_message") == []
