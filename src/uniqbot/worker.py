"""The render queue: one order at a time, oldest first. Spec 2.3."""

import asyncio
import logging
import random
import secrets
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import FSInputFile

from . import repo, texts
from .config import Settings
from .db import Order, OrderStatus
from .deps import Deps
from .engine import EngineError, NotAVideo, RenderResult, TooLong, render
from .redact import redact

log = logging.getLogger(__name__)

POLL_SEC = 30  # how long an idle worker waits for a wake-up before it looks at the queue anyway
ERROR_PAUSE_SEC = 5  # after an unexpected error, so a broken database does not turn the loop into a spin
# The local Bot API server downloads the file from Telegram before it answers getFile and uploads the result before it
# answers sendDocument; aiogram's default of 60 s fails on a 150-200 MB video.
LONG_REQUEST_SEC = 600
# The extension of the sender's file name is used only if it names one of these containers; ffprobe guesses the
# demuxer from the name too, so anything else (.m3u8, .sdp, .ffconcat...) must not reach it.
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".ts", ".3gp", ".mpg"}

Renderer = Callable[..., Awaitable[RenderResult]]


class FileFetcher(Protocol):
    async def fetch(self, bot: Bot, file_id: str, dest_dir: Path, suffix: str) -> Path:
        """Put the Telegram file into dest_dir as input<suffix> and return its path."""
        ...


class LocalFetcher:
    """Local Bot API server: getFile gives a path on the volume shared with it, so the file is moved, not downloaded."""

    async def fetch(self, bot: Bot, file_id: str, dest_dir: Path, suffix: str) -> Path:
        file = await bot.get_file(file_id, request_timeout=LONG_REQUEST_SEC)
        dest = dest_dir / f"input{suffix}"
        # the move can be a copy across volumes; keep it off the event loop
        await asyncio.to_thread(shutil.move, Path(file.file_path), dest)
        return dest


class CloudFetcher:
    """Cloud Bot API (development only, files up to 20 MB): an ordinary download."""

    async def fetch(self, bot: Bot, file_id: str, dest_dir: Path, suffix: str) -> Path:
        dest = dest_dir / f"input{suffix}"
        # Bot.download has no request_timeout: its `timeout` bounds the whole download (getFile on the cloud is quick)
        await bot.download(file_id, destination=dest, timeout=LONG_REQUEST_SEC)
        return dest


def make_fetcher(settings: Settings) -> FileFetcher:
    return LocalFetcher() if settings.bot_api_url else CloudFetcher()


def input_suffix(mime_type: str | None, file_name: str | None) -> str:
    """The lower-cased extension of the original file name if it is a known video container, otherwise .mp4.

    The MIME type is not needed: ffprobe sniffs the container.
    """
    suffix = Path(file_name).suffix.lower() if file_name else ""
    return suffix if suffix in _VIDEO_SUFFIXES else ".mp4"  # the name is the sender's


def _describe(error: BaseException, token: str | None) -> str:
    """What is stored on the order and sent to the admins, without the bot token (a file path or URL can hold it).

    Too long means the start is cut, the end explains the failure.
    """
    return redact(f"{type(error).__name__}: {error}", token)[-repo.ERROR_MAX :]


class Worker:
    def __init__(
        self,
        bot: Bot,
        deps: Deps,
        *,
        fetcher: FileFetcher,
        renderer: Renderer = render,
        rng_factory: Callable[[], random.Random] = random.Random,
    ) -> None:
        self._bot = bot
        self._deps = deps
        self._fetcher = fetcher
        self._renderer = renderer
        self._rng_factory = rng_factory

    async def run_forever(self) -> None:
        while True:
            try:
                order = await repo.claim_next(self._deps.sessions)
                if order is None:
                    await self._wait_for_work()
                    continue
                await self.process(order)
            except Exception:
                log.exception("worker loop failed")
                await asyncio.sleep(ERROR_PAUSE_SEC)

    async def _wait_for_work(self) -> None:
        try:
            await asyncio.wait_for(self._deps.wake.wait(), POLL_SEC)
        except TimeoutError:
            pass
        self._deps.wake.clear()  # before the next look at the queue, so an order queued from now on is seen

    async def process(self, order: Order) -> None:
        settings = self._deps.settings
        workdir = settings.work_dir / str(order.id)
        log.info("order %s of %s: processing", order.id, order.tg_id)
        status_id = await self._set_status(order, order.status_message_id, texts.PROCESSING)
        try:
            shutil.rmtree(workdir, ignore_errors=True)  # a restart can leave the half-done work of this very order
            workdir.mkdir(parents=True)
            suffix = input_suffix(order.mime_type, order.file_name)
            src = await self._fetcher.fetch(self._bot, order.file_id, workdir, suffix)
            result = await self._renderer(
                src,
                workdir,
                ranges=self._deps.ranges,
                max_duration=settings.max_duration_sec,
                timeout=settings.render_timeout_sec,
                rng=self._rng_factory(),
            )
            document = FSInputFile(result.output, filename=f"uniq_{secrets.token_hex(4)}.mp4")
            await self._bot.send_document(
                order.tg_id, document, caption=texts.DONE_CAPTION, request_timeout=LONG_REQUEST_SEC
            )
        except NotAVideo as e:
            await self._fail(order, texts.NOT_A_VIDEO, e, alert=False)
        except TooLong as e:
            await self._fail(order, texts.too_long(settings), e, alert=False)
        except TelegramForbiddenError:
            # the user blocked the bot while the video waited: nobody to deliver to, and not our error
            await repo.set_blocked(self._deps.sessions, order.tg_id, True)
            await repo.finish_order(self._deps.sessions, order.id, OrderStatus.FAILED, error="blocked")
            log.info("order %s: %s blocked the bot", order.id, order.tg_id)
        except EngineError as e:
            log.error("order %s failed: %s", order.id, e)
            await self._fail(order, texts.FAILED, e, alert=True)
        except Exception as e:
            log.exception("order %s failed unexpectedly", order.id)
            await self._fail(order, texts.FAILED, e, alert=True)
        else:
            await repo.finish_order(self._deps.sessions, order.id, OrderStatus.DONE, params=result.params.to_dict())
            if status_id is not None:
                await self._set_status(order, status_id, texts.DONE_STATUS)
            log.info("order %s: done", order.id)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _set_status(self, order: Order, message_id: int | None, text: str) -> int | None:
        """Show `text` in the status message, or in a new one if there is none yet; returns the message's id.

        The status is a courtesy: Telegram errors (message deleted, user gone) are logged and do not touch the order.
        """
        try:
            if message_id is None:
                return (await self._bot.send_message(order.tg_id, text)).message_id
            await self._bot.edit_message_text(text, chat_id=order.tg_id, message_id=message_id)
        except TelegramAPIError as e:
            log.warning("order %s: status message not updated: %s", order.id, e)
        return message_id

    async def _fail(self, order: Order, user_text: str, error: BaseException, *, alert: bool) -> None:
        reason = _describe(error, self._deps.settings.token_value)
        await repo.finish_order(self._deps.sessions, order.id, OrderStatus.FAILED, error=reason)
        try:
            await self._bot.send_message(order.tg_id, user_text)
        except TelegramForbiddenError:
            await repo.set_blocked(self._deps.sessions, order.tg_id, True)
        except TelegramAPIError as e:
            log.warning("order %s: could not tell %s about the failure: %s", order.id, order.tg_id, e)
        if alert:
            await self._deps.alerts.notify(texts.order_failed_alert(order.id, order.tg_id, reason))


async def recover(deps: Deps) -> None:
    """At start: orders left in processing go back to the line, work directories of no open order are removed."""
    requeued = await repo.requeue_processing(deps.sessions)
    active = {str(order_id) for order_id in await repo.active_order_ids(deps.sessions)}
    removed = 0
    for entry in deps.settings.work_dir.glob("*"):
        if entry.is_dir() and entry.name not in active:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    log.info("recover: %s order(s) back in the line, %s stale work dir(s) removed", requeued, removed)
