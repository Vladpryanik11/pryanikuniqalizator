"""Sends one post to many users at a steady pace. Spec 3, /broadcast."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from . import repo
from .db import BroadcastStatus

log = logging.getLogger(__name__)

PROGRESS_EVERY = 100  # recipients between two progress points

Progress = Callable[[int, int], Awaitable[None]]


@dataclass
class BroadcastResult:
    sent: int = 0
    blocked: int = 0
    failed: int = 0


async def send_broadcast(
    bot: Bot,
    sessions: repo.Sessions,
    broadcast_id: int,
    *,
    chat_id: int,
    message_id: int,
    reply_markup: InlineKeyboardMarkup | None,
    audience: list[int],
    rate_per_sec: float = 20,
    on_progress: Progress | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    result: BroadcastResult | None = None,
) -> BroadcastResult:
    """Copy the post (`chat_id`, `message_id`) to every user of `audience`, at most `rate_per_sec` a second.

    RetryAfter waits and tries the same user again, Forbidden marks the user as blocked, any other Telegram error
    is a failure. The counters go to the `broadcasts` row at every progress point (and `on_progress` is told how far
    it is); at the end the row is `done`. The counters live in `result` (the caller's, if given, updated in place), so
    after a crash the caller still knows how far the send got.
    """
    if result is None:
        result = BroadcastResult()
    total = len(audience)
    pause = 1 / rate_per_sec

    async def save(status: BroadcastStatus | None = None) -> None:
        await repo.update_broadcast(
            sessions,
            broadcast_id,
            sent=result.sent,
            blocked=result.blocked,
            failed=result.failed,
            status=status,
            finished=status is not None,
        )

    for done, user_id in enumerate(audience, 1):
        while True:
            try:
                await bot.copy_message(
                    chat_id=user_id, from_chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
                )
            except TelegramRetryAfter as e:
                log.warning("broadcast %s: flood control, waiting %s s", broadcast_id, e.retry_after)
                await sleep(e.retry_after)
                continue
            except TelegramForbiddenError:
                result.blocked += 1
                await repo.set_blocked(sessions, user_id, True)
            except TelegramAPIError as e:
                log.warning("broadcast %s: not delivered to %s: %s", broadcast_id, user_id, e)
                result.failed += 1
            else:
                result.sent += 1
            break
        if done < total:
            await sleep(pause)
        if done % PROGRESS_EVERY == 0 and done < total:
            await save()
            if on_progress is not None:
                await on_progress(done, total)

    await save(BroadcastStatus.DONE)
    if on_progress is not None and total:
        await on_progress(total, total)
    return result
