"""Puts the bot together: the Bot, the Dispatcher with its routers, the render worker and the process lifecycle."""

import asyncio
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.utils.token import TokenValidationError, validate_token

from . import admin, intake, repo, texts
from .access import AdminAlerts
from .config import Settings
from .db import init_db, make_engine, make_sessions
from .deps import Deps
from .middleware import TrackUser
from .profile import load_ranges
from .worker import Worker, make_fetcher, recover

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]


class StartupError(Exception):
    """The bot cannot start; the message is for whoever runs it and never holds the token."""


def _token_problem(settings: Settings) -> str | None:
    if settings.bot_token is None:
        return "BOT_TOKEN is not set (the token from @BotFather)"
    try:
        validate_token(settings.bot_token.get_secret_value())
    except TokenValidationError:
        return "BOT_TOKEN is not a valid bot token"
    return None


def startup_problems(settings: Settings) -> list[str]:
    """Everything wrong with the settings, one line each, so that a broken .env is fixed in one go."""
    problems = []
    if token_problem := _token_problem(settings):
        problems.append(token_problem)
    if not settings.channel_id:
        problems.append("CHANNEL_ID is not set (the channel subscribers must be in)")
    if not settings.channel_url:
        problems.append("CHANNEL_URL is not set (the link of the Subscribe button)")
    try:
        if not settings.admin_id_list:
            problems.append("ADMIN_IDS is not set (Telegram ids of the admins, separated by commas)")
    except ValueError:
        problems.append("ADMIN_IDS must be Telegram ids separated by commas")
    try:
        ZoneInfo(settings.tz)  # /users needs it; better to learn here than on the first call
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"TZ is not a known time zone: {settings.tz}")
    return problems


def build_bot(settings: Settings) -> Bot:
    """No default parse mode: admin alerts carry raw ffmpeg output, which HTML parsing would reject."""
    session = None  # the cloud Bot API
    if settings.bot_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True))
    return Bot(settings.bot_token.get_secret_value(), session=session)


def build_dispatcher(deps: Deps) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), deps=deps)
    # outer middleware on `update`: the user row has to exist before any handler creates an order for it
    dp.update.outer_middleware(TrackUser(deps.sessions))
    # admin before intake: its state handlers must take the admin's post ahead of intake's fallback hint
    dp.include_router(admin.build_router())
    dp.include_router(intake.build_router())
    return dp


def _log_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("the render worker stopped", exc_info=task.exception())


async def _stop(*tasks: asyncio.Task | None) -> None:
    running = [task for task in tasks if task is not None and not task.done()]
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)  # a cancelled render kills its ffmpeg before we go on


async def run_bot(settings: Settings) -> None:
    """Runs until the polling stops (SIGINT/SIGTERM), then stops what it started."""
    if problems := startup_problems(settings):
        raise StartupError("\n".join(problems))
    settings.ensure_dirs()
    engine = make_engine(settings.db_path)
    sessions = make_sessions(engine)
    ranges = load_ranges(settings.uniq_config)
    bot = build_bot(settings)
    deps = Deps(
        settings=settings,
        sessions=sessions,
        ranges=ranges,
        wake=asyncio.Event(),
        alerts=AdminAlerts(bot, settings.admin_id_list),
    )
    worker_task: asyncio.Task | None = None
    try:
        await init_db(engine)
        await recover(deps)
        await repo.interrupt_running_broadcasts(sessions)
        worker = Worker(bot, deps, fetcher=make_fetcher(settings))
        worker_task = asyncio.create_task(worker.run_forever(), name="worker")
        worker_task.add_done_callback(_log_crash)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description=texts.CMD_START),
                BotCommand(command="privacy", description=texts.CMD_PRIVACY),
            ]
        )
        await build_dispatcher(deps).start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        await _stop(worker_task, deps.broadcast_task)
        await bot.session.close()
        await engine.dispose()


async def logout_cloud(settings: Settings) -> bool:
    """Leaves the cloud Bot API. Done once, before the bot starts on the local server (always talks to the cloud)."""
    if problem := _token_problem(settings):
        raise StartupError(problem)
    bot = build_bot(settings.model_copy(update={"bot_api_url": None}))
    try:
        return await bot.log_out()
    finally:
        await bot.session.close()
