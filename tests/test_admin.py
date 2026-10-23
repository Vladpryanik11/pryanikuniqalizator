import asyncio
import itertools
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import CopyMessage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
)
from fakes import ADMIN_IDS, FakeBot, FakeCallback, FakeMessage, make_deps, real_bot, register, tg_user
from sqlalchemy import select

from uniqbot import admin, intake, repo, texts
from uniqbot.admin import (
    BroadcastForm,
    build_router,
    is_admin,
    on_broadcast,
    on_broadcast_cancel,
    on_broadcast_go,
    on_broadcast_post,
    on_cancel,
    on_stats,
    on_users,
)
from uniqbot.db import Broadcast, BroadcastStatus, OrderStatus
from uniqbot.export import users_csv
from uniqbot.middleware import TrackUser

ADMIN = tg_user(ADMIN_IDS[0], "boss")
STRANGER = tg_user(5, "guest")
MARKUP = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Канал", url="https://t.me/test_channel")]])
POST_ID, CONFIRM_ID = 40, 7


@pytest.fixture
async def env(tmp_path):
    bot = FakeBot()
    deps, engine = await make_deps(tmp_path, bot=bot)
    state = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN.id, user_id=ADMIN.id)
    )
    yield SimpleNamespace(bot=bot, deps=deps, state=state)
    task = deps.broadcast_task
    if task is not None and not task.done():
        task.cancel()
    await engine.dispose()


def command(env, text: str, user=ADMIN) -> FakeMessage:
    return FakeMessage(env.bot, user, text=text)


def press(env, data: str, user=ADMIN) -> FakeCallback:
    return FakeCallback(env.bot, user, data=data, message=FakeMessage(env.bot, user, message_id=CONFIRM_ID))


def buttons(call: dict) -> list:
    return [b for row in call["reply_markup"].inline_keyboard for b in row]


async def broadcast_rows(env) -> list[Broadcast]:
    async with env.deps.sessions() as s:
        return list((await s.scalars(select(Broadcast).order_by(Broadcast.id))).all())


async def make_users(env, ids=(1, 2, 3), blocked=(2,)) -> None:
    """Users 1..3; the audience of a broadcast is the ones that did not block the bot."""
    for user_id in ids:
        await register(env.deps, tg_user(user_id))
    for user_id in blocked:
        await repo.set_blocked(env.deps.sessions, user_id, True)


async def ask_and_post(env, **post) -> None:
    await on_broadcast(command(env, "/broadcast"), env.bot, env.deps, env.state)
    fields = dict(text="Новость", message_id=POST_ID, reply_markup=MARKUP) | post
    await on_broadcast_post(FakeMessage(env.bot, ADMIN, **fields), env.bot, env.deps, env.state)
    env.bot.calls.clear()


def test_is_admin_follows_the_settings():
    deps = SimpleNamespace(settings=SimpleNamespace(admin_id_list=[11, 22]))
    assert is_admin(11, deps) and is_admin(22, deps)
    assert not is_admin(5, deps)


# --- /stats and /users ---


async def test_stats_command(env):
    await make_users(env)
    await repo.set_subscription(env.deps.sessions, 1, True)
    order_id, _ = await repo.create_order(
        env.deps.sessions, tg_id=1, file_id="f", file_size=1, duration=1, mime_type=None, file_name=None
    )
    await repo.claim_next(env.deps.sessions)
    await repo.finish_order(env.deps.sessions, order_id, OrderStatus.DONE)
    await repo.create_order(
        env.deps.sessions, tg_id=3, file_id="g", file_size=1, duration=1, mime_type=None, file_name=None
    )

    await on_stats(command(env, "/stats"), env.bot, env.deps)

    assert env.bot.texts(ADMIN.id) == [
        "👥 Пользователей: 3 (новых за сутки 3, за 7 дней 3)\n"
        "Активных за 7 дней: 3\nПодписаны: 1\nЗаблокировали бота: 1\n"
        "🎬 Видео: за сутки 1, всего 1; ошибок за сутки 0\nОчередь: 1"
    ]

    await repo.claim_next(env.deps.sessions)  # the next one starts rendering
    env.bot.calls.clear()
    await on_stats(command(env, "/stats"), env.bot, env.deps)
    assert env.bot.texts(ADMIN.id)[0].endswith("Очередь: 0 + 1 в работе")


async def test_admin_handlers_are_silent_for_a_stranger(env):
    await make_users(env)
    await on_stats(command(env, "/stats", STRANGER), env.bot, env.deps)
    await on_users(command(env, "/users", STRANGER), env.bot, env.deps)
    await on_broadcast(command(env, "/broadcast", STRANGER), env.bot, env.deps, env.state)
    await on_broadcast_post(
        FakeMessage(env.bot, STRANGER, text="spam", message_id=POST_ID), env.bot, env.deps, env.state
    )
    await on_cancel(command(env, "/cancel", STRANGER), env.bot, env.deps, env.state)
    await on_broadcast_go(press(env, "bc:go", STRANGER), env.bot, env.deps, env.state)
    await on_broadcast_cancel(press(env, "bc:cancel", STRANGER), env.bot, env.deps, env.state)

    assert env.bot.calls == []
    assert await env.state.get_state() is None
    assert await broadcast_rows(env) == []

    # not even a stranger who got hold of a pending post can send it
    await ask_and_post(env)
    await on_broadcast_go(press(env, "bc:go", STRANGER), env.bot, env.deps, env.state)
    assert env.bot.calls == [] and env.deps.broadcast_task is None and await broadcast_rows(env) == []


async def test_users_command_sends_csv(env, monkeypatch):
    await make_users(env)
    # 22:30 UTC on the 23rd is already the 24th in Moscow: the file is named by the bot's local date
    monkeypatch.setattr(admin, "utcnow", lambda: datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc))

    await on_users(command(env, "/users"), env.bot, env.deps)

    [sent] = env.bot.called("send_document")
    assert sent["chat_id"] == ADMIN.id
    assert isinstance(sent["document"], BufferedInputFile)
    assert sent["document"].filename == "users_2026-10-24.csv"
    users = await repo.all_users(env.deps.sessions)
    assert sent["document"].data == users_csv(users, ZoneInfo(env.deps.settings.tz))
    assert sent["document"].data.startswith("﻿".encode("utf-8")) and b"tg_id;username" in sent["document"].data

    env.deps.settings.tz = "America/New_York"  # the date comes from the TZ setting: still the 23rd there
    await on_users(command(env, "/users"), env.bot, env.deps)
    assert env.bot.called("send_document")[1]["document"].filename == "users_2026-10-23.csv"


# --- /broadcast ---


async def test_broadcast_flow(env):
    await make_users(env)  # 3 users, one of them blocked the bot

    await on_broadcast(command(env, "/broadcast"), env.bot, env.deps, env.state)
    assert env.bot.texts(ADMIN.id) == [texts.BC_ASK_POST]
    assert await env.state.get_state() == BroadcastForm.waiting_post.state

    env.bot.calls.clear()
    post = FakeMessage(env.bot, ADMIN, text="Новость", message_id=POST_ID, reply_markup=MARKUP)
    await on_broadcast_post(post, env.bot, env.deps, env.state)
    assert env.bot.called("copy_message") == [
        dict(chat_id=ADMIN.id, from_chat_id=ADMIN.id, message_id=POST_ID, reply_markup=MARKUP)
    ]  # the preview is the post itself, with its buttons
    [confirm] = env.bot.called("send_message")
    assert confirm["text"] == texts.bc_confirm(2) == "Отправить 2 пользователям?"
    assert [(b.text, b.callback_data) for b in buttons(confirm)] == [("Отправить", "bc:go"), ("Отмена", "bc:cancel")]
    assert await env.state.get_state() is None  # the next message of the admin is no longer taken as a post
    assert await broadcast_rows(env) == []  # nothing is recorded until the admin decides

    env.bot.calls.clear()
    await on_broadcast_cancel(press(env, "bc:cancel"), env.bot, env.deps, env.state)
    [row] = await broadcast_rows(env)
    assert (row.status, row.total, row.sent, row.source_message_id) == (BroadcastStatus.CANCELLED, 2, 0, POST_ID)
    assert row.finished_at is not None
    assert [(e["text"], e["message_id"]) for e in env.bot.called("edit_message_text")] == [
        (texts.BC_CANCELLED, CONFIRM_ID)
    ]  # the question with the buttons becomes the answer
    assert env.bot.called("copy_message") == []

    env.bot.calls.clear()
    await on_broadcast_cancel(press(env, "bc:cancel"), env.bot, env.deps, env.state)  # the same button again
    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)  # an old button of a cancelled post
    assert len(await broadcast_rows(env)) == 1 and env.deps.broadcast_task is None
    assert [c[0] for c in env.bot.calls] == ["answer_callback_query"] * 2


async def test_cancel_while_waiting_for_the_post(env):
    await on_broadcast(command(env, "/broadcast"), env.bot, env.deps, env.state)
    env.bot.calls.clear()

    await on_cancel(command(env, "/cancel"), env.bot, env.deps, env.state)

    assert await env.state.get_state() is None
    assert env.bot.texts(ADMIN.id) == [texts.BC_CANCELLED]
    assert await broadcast_rows(env) == []


async def test_go_while_a_broadcast_is_running(env):
    await make_users(env)
    await ask_and_post(env)
    running = asyncio.create_task(asyncio.sleep(3600))
    env.deps.broadcast_task = running

    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)

    [answer] = env.bot.called("answer_callback_query")
    assert (answer["text"], answer["show_alert"]) == (texts.BC_BUSY, True)
    assert env.deps.broadcast_task is running
    assert await broadcast_rows(env) == [] and env.bot.called("copy_message") == []

    running.cancel()  # a finished broadcast no longer blocks: the same button now sends the post
    await asyncio.gather(running, return_exceptions=True)
    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)
    await env.deps.broadcast_task
    assert [c["chat_id"] for c in env.bot.called("copy_message")] == [1, 3]


async def test_go_sends_the_post_and_reports(env):
    await make_users(env)
    await ask_and_post(env)

    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)
    await env.deps.broadcast_task

    assert env.bot.called("copy_message") == [
        dict(chat_id=u, from_chat_id=ADMIN.id, message_id=POST_ID, reply_markup=MARKUP) for u in (1, 3)
    ]  # the blocked one is not written to
    [row] = await broadcast_rows(env)
    assert (row.status, row.total, row.sent, row.blocked, row.failed) == (BroadcastStatus.DONE, 2, 2, 0, 0)
    assert row.finished_at is not None
    # the message with the buttons turns into the progress; the report comes as a new message
    assert [(e["text"], e["message_id"]) for e in env.bot.called("edit_message_text")] == [
        (texts.bc_progress(0, 2), CONFIRM_ID),
        (texts.bc_progress(2, 2), CONFIRM_ID),
    ]
    assert env.bot.texts(ADMIN.id) == [texts.BC_REPORT.format(sent=2, blocked=0, failed=0)]
    assert await env.state.get_state() is None and await env.state.get_data() == {}

    env.bot.calls.clear()
    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)  # pressed again: the post is gone
    assert len(await broadcast_rows(env)) == 1 and env.bot.called("copy_message") == []


async def test_go_counts_blocked_and_failed_in_the_report(env):
    await make_users(env, blocked=())
    env.bot.copy_errors[1] = [
        TelegramForbiddenError(
            method=CopyMessage(chat_id=1, from_chat_id=2, message_id=3), message="Forbidden: bot was blocked by the user"
        )
    ]
    await ask_and_post(env, reply_markup=None)

    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)
    await env.deps.broadcast_task

    assert env.bot.texts(ADMIN.id) == [texts.BC_REPORT.format(sent=2, blocked=1, failed=0)]
    assert next(u for u in await repo.all_users(env.deps.sessions) if u.tg_id == 1).blocked is True
    assert env.bot.called("copy_message")[0]["reply_markup"] is None


async def test_a_second_broadcast_command_drops_the_first_post(env):
    await make_users(env)
    await ask_and_post(env)
    await on_broadcast(command(env, "/broadcast"), env.bot, env.deps, env.state)
    env.bot.calls.clear()

    await on_broadcast_go(press(env, "bc:go"), env.bot, env.deps, env.state)  # the button of the first post

    assert env.deps.broadcast_task is None and env.bot.called("copy_message") == []


def test_texts_of_the_admin_side():
    assert texts.BC_ASK_POST == "Пришли пост для рассылки. /cancel — отмена."
    assert texts.BC_GO == "Отправить" and texts.BC_CANCEL == "Отмена"
    assert texts.BC_CANCELLED == "Рассылка отменена."
    assert texts.BC_BUSY == "Рассылка уже идёт, дождись отчёта."
    assert texts.bc_progress(100, 250) == "Рассылка: 100 из 250…"
    assert texts.bc_report(5, 2, 1) == "Рассылка завершена. Доставлено: 5, заблокировали бота: 2, ошибок: 1."


# --- the router, run by a real dispatcher ---


@pytest.fixture
async def wired(env):
    """The application's dispatcher: the admin router before the intake one, on a bot that talks to FakeBot."""
    bot = real_bot(env.bot)
    dp = Dispatcher(deps=env.deps)
    dp.update.outer_middleware(TrackUser(env.deps.sessions))
    dp.include_router(build_router())
    dp.include_router(intake.build_router())
    ids = itertools.count(1)

    def message(user, text=None, **fields) -> Message:
        entities = None
        if text and text.startswith("/"):
            entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        return Message(
            message_id=next(ids) + 100, date=datetime.now(timezone.utc), chat=Chat(id=user.id, type="private"),
            from_user=user, text=text, entities=entities, **fields,
        )

    async def feed(**fields) -> None:
        env.bot.calls.clear()
        await dp.feed_update(bot, Update(update_id=next(ids), **fields))

    yield SimpleNamespace(feed=feed, message=message)
    await bot.session.close()


def test_the_router_can_be_built_again():
    assert build_router() is not build_router()


async def test_router_commands_are_silent_for_a_stranger(env, wired):
    for text in ("/stats", "/users", "/broadcast", "/cancel"):
        await wired.feed(message=wired.message(STRANGER, text))
        assert env.bot.calls == []

    await wired.feed(message=wired.message(STRANGER, "привет"))  # an ordinary message still gets the hint
    assert env.bot.texts(STRANGER.id) == [texts.SEND_VIDEO_HINT]


async def test_router_broadcast_from_the_command_to_the_report(env, wired):
    await make_users(env, blocked=())

    await wired.feed(message=wired.message(ADMIN, "/broadcast"))
    assert env.bot.texts(ADMIN.id) == [texts.BC_ASK_POST]

    # while a post is awaited, a command is still a command and does not become the post
    await wired.feed(message=wired.message(ADMIN, "/privacy"))
    assert env.bot.texts(ADMIN.id) == [texts.PRIVACY]
    await wired.feed(message=wired.message(ADMIN, "/stats"))
    assert env.bot.texts(ADMIN.id)[0].startswith("👥 Пользователей: 4")  # 3 users and the admin

    post = wired.message(ADMIN, "Новость")
    await wired.feed(message=post)
    assert [c["message_id"] for c in env.bot.called("copy_message")] == [post.message_id]
    assert env.bot.texts(ADMIN.id) == [texts.bc_confirm(4)]
    confirm = wired.message(ADMIN, texts.bc_confirm(4))

    await wired.feed(
        callback_query=CallbackQuery(id="c1", from_user=ADMIN, chat_instance="ci", data="bc:go", message=confirm)
    )
    await env.deps.broadcast_task

    assert sorted(c["chat_id"] for c in env.bot.called("copy_message")) == [1, 2, 3, ADMIN.id]
    assert env.bot.texts(ADMIN.id) == [texts.BC_REPORT.format(sent=4, blocked=0, failed=0)]
    assert [r.status for r in await broadcast_rows(env)] == [BroadcastStatus.DONE]


async def test_router_cancel_while_waiting_for_the_post(env, wired):
    await wired.feed(message=wired.message(ADMIN, "/broadcast"))
    await wired.feed(message=wired.message(ADMIN, "/cancel"))
    assert env.bot.texts(ADMIN.id) == [texts.BC_CANCELLED]

    await wired.feed(message=wired.message(ADMIN, "привет"))  # no longer a post: the usual hint
    assert env.bot.texts(ADMIN.id) == [texts.SEND_VIDEO_HINT]
