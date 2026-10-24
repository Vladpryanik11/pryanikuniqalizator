import asyncio
import itertools
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandObject
from aiogram.methods import EditMessageText, GetChatMember
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberBanned,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)
from fakes import (
    ADMIN_IDS,
    CHANNEL_URL,
    MIB,
    FakeBot,
    FakeCallback,
    FakeMessage,
    document,
    make_deps,
    real_bot,
    register,
    tg_user,
    video,
)
from sqlalchemy import select

from uniqbot import repo, texts
from uniqbot.config import Settings
from uniqbot.db import Order, OrderStatus
from uniqbot.intake import (
    build_router,
    on_check,
    on_my_chat_member,
    on_other,
    on_privacy,
    on_start,
    on_video,
)
from uniqbot.middleware import TrackUser

USER = tg_user(1, "vlad")
BOT_USER = User(id=999, is_bot=True, first_name="uniqbot")


@pytest.fixture
async def env(tmp_path):
    bot = FakeBot()
    deps, engine = await make_deps(tmp_path, bot=bot)
    await register(deps, USER)
    yield SimpleNamespace(bot=bot, deps=deps, settings=deps.settings)
    await engine.dispose()


def msg(env, **kwargs) -> FakeMessage:
    return FakeMessage(env.bot, USER, **kwargs)


def start_command(label: str | None = None) -> CommandObject:
    return CommandObject(command="start", args=label)


async def load_user(env, tg_id: int = USER.id):
    return next(u for u in await repo.all_users(env.deps.sessions) if u.tg_id == tg_id)


async def all_orders(env) -> list[Order]:
    async with env.deps.sessions() as s:
        return list((await s.scalars(select(Order).order_by(Order.id))).all())


async def send_video(env, vid) -> list[str]:
    """Send a video as USER and return only what the bot wrote back for it."""
    env.bot.calls.clear()
    await on_video(msg(env, video=vid), env.bot, env.deps)
    return env.bot.texts(USER.id)


def buttons(call: dict) -> list:
    return [b for row in call["reply_markup"].inline_keyboard for b in row]


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=EditMessageText(text="x", chat_id=1, message_id=7), message=text)


def check_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=GetChatMember(chat_id=1, user_id=1), message="timeout")


# --- texts ---


def test_text_formatters_follow_the_limits():
    default = Settings(max_file_mb=200, max_duration_sec=180)
    assert texts.welcome(default) == "Пришли видео до 200 МБ и не длиннее 3 мин — верну уникальную копию файлом."
    assert texts.too_big(default) == "Файл больше 200 МБ."
    assert texts.too_long(default) == "Видео длиннее 3 мин."
    assert texts.accepted(2) == "Принял! Ты в очереди: 2-й. Результат пришлю сюда."
    small = Settings(max_file_mb=50, max_duration_sec=120)
    assert "50 МБ" in texts.welcome(small) and "2 мин" in texts.welcome(small)
    assert texts.too_big(small) == "Файл больше 50 МБ." and texts.too_long(small) == "Видео длиннее 2 мин."


# --- /start and the subscription button ---


async def test_start_records_first_label(env):
    await on_start(msg(env, text="/start reels"), env.bot, env.deps, start_command("reels"))
    await on_start(msg(env, text="/start stories"), env.bot, env.deps, start_command("stories"))
    assert (await load_user(env)).source == "reels"


async def test_start_without_a_label_or_with_a_broken_one_records_nothing(env):
    await on_start(msg(env, text="/start"), env.bot, env.deps, start_command())
    await on_start(msg(env, text="/start a b!"), env.bot, env.deps, start_command("a b!"))
    assert (await load_user(env)).source is None


async def test_start_not_subscribed_shows_buttons(env):
    env.bot.member_status = "left"
    await on_start(msg(env, text="/start"), env.bot, env.deps, start_command())

    [call] = env.bot.called("send_message")
    assert call["text"] == texts.NOT_SUBSCRIBED
    subscribe, check = buttons(call)
    assert (subscribe.text, subscribe.url, subscribe.callback_data) == (texts.BTN_SUBSCRIBE, CHANNEL_URL, None)
    assert (check.text, check.callback_data, check.url) == (texts.BTN_CHECK, "check_sub", None)
    assert (await load_user(env)).is_subscribed is False


async def test_start_subscribed_shows_welcome(env):
    await on_start(msg(env, text="/start"), env.bot, env.deps, start_command())

    assert env.bot.texts(USER.id) == [texts.welcome(env.settings)]
    user = await load_user(env)
    assert user.is_subscribed is True and user.subscription_checked_at is not None


async def test_check_button_subscribed_replaces_the_prompt_with_the_welcome(env):
    await on_check(FakeCallback(env.bot, USER), env.bot, env.deps)

    [edit] = env.bot.called("edit_message_text")
    assert (edit["text"], edit["chat_id"], edit["message_id"]) == (texts.welcome(env.settings), USER.id, 7)
    assert "reply_markup" not in edit  # the buttons go away with the edit
    assert env.bot.called("answer_callback_query") == [
        {"callback_query_id": "cb1", "text": None, "show_alert": None}
    ]
    assert (await load_user(env)).is_subscribed is True


async def test_check_button_not_subscribed_answers_with_an_alert(env):
    env.bot.member_status = "kicked"
    await on_check(FakeCallback(env.bot, USER), env.bot, env.deps)

    assert env.bot.called("answer_callback_query") == [
        {"callback_query_id": "cb1", "text": texts.STILL_NOT_SUBSCRIBED, "show_alert": True}
    ]
    assert env.bot.called("edit_message_text") == []
    assert (await load_user(env)).is_subscribed is False


async def test_check_button_sends_the_welcome_when_the_prompt_is_gone(env):
    env.bot.edit_error = bad_request("Bad Request: message to edit not found")
    await on_check(FakeCallback(env.bot, USER), env.bot, env.deps)
    assert env.bot.texts(USER.id) == [texts.welcome(env.settings)]


async def test_check_button_pressed_twice_does_not_repeat_the_welcome(env):
    env.bot.edit_error = bad_request("Bad Request: message is not modified: specified new message content ...")
    await on_check(FakeCallback(env.bot, USER), env.bot, env.deps)
    assert env.bot.texts(USER.id) == []


async def test_check_failure_tells_user_and_alerts_admins(env):
    env.bot.member_error = check_error()

    await on_video(msg(env, video=video()), env.bot, env.deps)

    assert env.bot.texts(USER.id) == [texts.CHECK_FAILED]
    alerts = [kw for kw in env.bot.called("send_message") if kw["chat_id"] in ADMIN_IDS]
    assert sorted(kw["chat_id"] for kw in alerts) == sorted(ADMIN_IDS)  # one message each
    assert all("timeout" in kw["text"] for kw in alerts)
    assert await all_orders(env) == []
    assert (await load_user(env)).is_subscribed is None  # a failed check says nothing about the subscription

    await on_video(msg(env, video=video()), env.bot, env.deps)
    assert env.bot.texts(USER.id) == [texts.CHECK_FAILED, texts.CHECK_FAILED]
    alerts = [kw for kw in env.bot.called("send_message") if kw["chat_id"] in ADMIN_IDS]
    assert len(alerts) == 2  # the admins are not bothered again within 10 minutes


async def test_check_failure_alert_never_holds_the_token(env):
    token = "123456:" + "A" * 35
    env.bot.member_error = TelegramNetworkError(
        method=GetChatMember(chat_id=1, user_id=1), message=f"cannot reach https://api.telegram.org/bot{token}/getChatMember"
    )

    await on_video(msg(env, video=video()), env.bot, env.deps)

    alerts = [kw["text"] for kw in env.bot.called("send_message") if kw["chat_id"] in ADMIN_IDS]
    assert len(alerts) == 2
    assert all("<token>" in text and token not in text for text in alerts)


async def test_check_failure_on_start_and_on_the_button(env):
    env.bot.member_error = check_error()

    await on_start(msg(env, text="/start"), env.bot, env.deps, start_command())
    await on_check(FakeCallback(env.bot, USER), env.bot, env.deps)

    assert env.bot.texts(USER.id) == [texts.CHECK_FAILED]
    assert env.bot.called("answer_callback_query") == [
        {"callback_query_id": "cb1", "text": texts.CHECK_FAILED, "show_alert": True}
    ]
    assert env.bot.called("edit_message_text") == []


# --- video intake ---


async def test_video_checks_in_order(env):
    settings = env.settings
    too_big_and_long = video("v0", size=201 * MIB, duration=181)

    env.bot.member_status = "left"
    assert await send_video(env, too_big_and_long) == [texts.NOT_SUBSCRIBED]
    assert buttons(env.bot.called("send_message")[0])[1].callback_data == "check_sub"

    env.bot.member_status = "member"
    assert await send_video(env, too_big_and_long) == [texts.too_big(settings)]
    assert await send_video(env, video("v0", duration=181)) == [texts.too_long(settings)]

    first_id, _ = await repo.create_order(
        env.deps.sessions, tg_id=USER.id, file_id="old", file_size=1, duration=1, mime_type=None, file_name=None
    )
    assert await send_video(env, video("v0", duration=181)) == [texts.too_long(settings)]  # limits before the queue
    assert await send_video(env, video("v1")) == [texts.HAS_ACTIVE]
    assert len(await all_orders(env)) == 1 and not env.deps.wake.is_set()

    await repo.finish_order(env.deps.sessions, first_id, OrderStatus.FAILED)
    assert await send_video(env, video("v2", size=7 * MIB, duration=60)) == [texts.accepted(1)]

    order = (await all_orders(env))[-1]
    assert (order.file_id, order.file_size, order.duration, order.mime_type) == ("v2", 7 * MIB, 60, "video/mp4")
    assert order.status == OrderStatus.QUEUED
    assert order.status_message_id == env.bot.last_message_id
    assert env.deps.wake.is_set()


@pytest.mark.parametrize(
    "vid", [video(size=200 * MIB, duration=180), video(size=None)], ids=["at-the-limits", "size-unknown"]
)
async def test_limits_are_inclusive_and_an_unknown_size_passes(env, vid):
    assert await send_video(env, vid) == [texts.accepted(1)]


@pytest.mark.parametrize("concurrent", [False, True], ids=["sequential", "concurrent"])
async def test_second_video_of_an_album_is_refused(env, monkeypatch, concurrent):
    first = msg(env, video=video("a1"), message_id=1)
    second = msg(env, video=video("a2"), message_id=2)

    if concurrent:
        # both pre-checks must see "no order yet" before either video is queued: only the database can refuse one
        gate = asyncio.Barrier(2)
        seen = []
        real = repo.active_order

        async def pre_check(sessions, tg_id):
            found = await real(sessions, tg_id)
            seen.append(found)
            await gate.wait()
            return found

        monkeypatch.setattr(repo, "active_order", pre_check)
        await asyncio.gather(on_video(first, env.bot, env.deps), on_video(second, env.bot, env.deps))
        assert seen == [None, None]
    else:
        await on_video(first, env.bot, env.deps)
        await on_video(second, env.bot, env.deps)

    assert sorted(env.bot.texts(USER.id)) == sorted([texts.accepted(1), texts.HAS_ACTIVE])
    [order] = await all_orders(env)
    assert order.status == OrderStatus.QUEUED
    assert order.status_message_id is not None and env.deps.wake.is_set()


async def test_the_line_is_woken_even_if_the_acknowledgement_cannot_be_sent(env):
    env.bot.send_errors[USER.id] = RuntimeError("bot was blocked by the user")
    with pytest.raises(RuntimeError):
        await on_video(msg(env, video=video()), env.bot, env.deps)
    assert len(await all_orders(env)) == 1 and env.deps.wake.is_set()


async def test_document_video_mime_accepted(env):
    doc = document("video/quicktime", "d1", name="IMG_1.MOV")
    await on_video(msg(env, document=doc), env.bot, env.deps)

    assert env.bot.texts(USER.id) == [texts.accepted(1)]
    [order] = await all_orders(env)
    assert (order.file_id, order.duration, order.mime_type, order.file_name) == ("d1", None, "video/quicktime", "IMG_1.MOV")


@pytest.mark.parametrize(
    "fields",
    [
        {"document": document("application/pdf")},
        {"document": document(None)},
        {"document": document("video/mp4"), "animation": SimpleNamespace(file_id="gif")},  # a GIF also comes as a document
    ],
    ids=["pdf", "no-mime", "gif"],
)
async def test_other_documents_and_gifs_get_the_hint(env, fields):
    await on_video(msg(env, **fields), env.bot, env.deps)

    assert env.bot.texts(USER.id) == [texts.SEND_VIDEO_HINT]
    assert await all_orders(env) == []
    assert env.bot.called("get_chat_member") == []


# --- the rest ---


async def test_other_messages_get_hint_but_commands_are_silent(env):
    await on_other(msg(env, text="привет"), env.bot, env.deps)
    await on_other(msg(env, text=None), env.bot, env.deps)  # a photo, a sticker, a video note
    assert env.bot.texts(USER.id) == [texts.SEND_VIDEO_HINT, texts.SEND_VIDEO_HINT]

    env.bot.calls.clear()
    await on_other(msg(env, text="/stats"), env.bot, env.deps)
    assert env.bot.calls == []


async def test_privacy(env):
    await on_privacy(msg(env, text="/privacy"), env.bot, env.deps)
    assert env.bot.texts(USER.id) == [texts.PRIVACY]


def test_privacy_text_lists_every_stored_field():
    assert texts.PRIVACY == (
        "Бот хранит твой Telegram ID, имя, @username, язык, отметку Telegram Premium, дату первого и последнего "
        "визита, метку ссылки, по которой ты пришёл, статус подписки на канал, число обработанных видео и то, "
        "заблокирован ли бот. Это нужно, чтобы пускать подписчиков, считать статистику и присылать новости бота. "
        "Видео удаляются сразу после обработки."
    )


def member_update(chat_type: str, new_status_member) -> ChatMemberUpdated:
    return ChatMemberUpdated(
        chat=Chat(id=USER.id if chat_type == "private" else -5, type=chat_type),
        from_user=USER,
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberMember(user=BOT_USER),
        new_chat_member=new_status_member,
    )


async def test_my_chat_member_tracks_blocking(env):
    kicked = ChatMemberBanned(user=BOT_USER, until_date=datetime.fromtimestamp(0, timezone.utc))
    await on_my_chat_member(member_update("private", kicked), env.bot, env.deps)
    user = await load_user(env)
    assert user.blocked is True and user.blocked_at is not None

    await on_my_chat_member(member_update("private", ChatMemberMember(user=BOT_USER)), env.bot, env.deps)
    user = await load_user(env)
    assert user.blocked is False and user.blocked_at is None


# --- middleware ---


async def test_track_user_middleware_refreshes_profile(env):
    seen = []

    async def handler(event, data):
        seen.append(event)
        return "handled"

    middleware = TrackUser(env.deps.sessions)
    assert await middleware(handler, "first", {"event_from_user": tg_user(7, "old", language_code="ru")}) == "handled"
    await middleware(handler, "second", {"event_from_user": tg_user(7, "new")})

    user = await load_user(env, 7)
    assert (user.username, user.first_name, user.language_code) == ("new", "Vlad", None)
    assert seen == ["first", "second"]


async def test_track_user_middleware_skips_bots_groups_and_updates_without_a_user(env):
    seen = []

    async def handler(event, data):
        seen.append(event)

    middleware = TrackUser(env.deps.sessions)
    await middleware(handler, "bot", {"event_from_user": BOT_USER})
    await middleware(handler, "group", {"event_from_user": tg_user(8), "event_chat": Chat(id=-5, type="supergroup")})
    await middleware(handler, "channel post", {})
    await middleware(handler, "private", {"event_from_user": tg_user(9), "event_chat": Chat(id=9, type="private")})

    assert [u.tg_id for u in await repo.all_users(env.deps.sessions)] == [USER.id, 9]
    assert len(seen) == 4  # every update is still handled


# --- the router, run by a real dispatcher ---


@pytest.fixture
async def wired(env):
    """A dispatcher like the application's (TrackUser, then the intake router) on a bot that talks to FakeBot."""
    bot = real_bot(env.bot)
    dp = Dispatcher(deps=env.deps)
    dp.update.outer_middleware(TrackUser(env.deps.sessions))
    dp.include_router(build_router())
    ids = itertools.count(1)

    def message(user, text=None, *, chat_type="private", **fields) -> Message:
        entities = None
        if text and text.startswith("/"):
            entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        chat = Chat(id=user.id if chat_type == "private" else -5, type=chat_type)
        return Message(
            message_id=next(ids), date=datetime.now(timezone.utc), chat=chat, from_user=user, text=text,
            entities=entities, **fields,
        )

    async def feed(**fields) -> None:
        env.bot.calls.clear()
        await dp.feed_update(bot, Update(update_id=next(ids), **fields))

    yield SimpleNamespace(feed=feed, message=message)
    await bot.session.close()


def test_the_router_can_be_built_again():
    assert build_router() is not build_router()


async def test_router_queues_the_video_of_a_user_the_bot_has_not_seen_yet(env, wired):
    newbie = tg_user(2, "newbie")
    await wired.feed(message=wired.message(newbie, video=video("w1")))

    assert env.bot.texts(2) == [texts.accepted(1)]
    [order] = await all_orders(env)
    assert (order.tg_id, order.file_id) == (2, "w1")
    assert (await load_user(env, 2)).username == "newbie"


async def test_router_commands_and_fallback(env, wired):
    await wired.feed(message=wired.message(USER, "/start reels"))
    assert env.bot.texts(USER.id) == [texts.welcome(env.settings)]
    assert (await load_user(env)).source == "reels"

    await wired.feed(message=wired.message(USER, "/privacy"))
    assert env.bot.texts(USER.id) == [texts.PRIVACY]

    await wired.feed(message=wired.message(USER, "привет"))
    assert env.bot.texts(USER.id) == [texts.SEND_VIDEO_HINT]

    await wired.feed(message=wired.message(USER, "/stats"))
    assert env.bot.calls == []

    await wired.feed(message=wired.message(USER, "привет", chat_type="supergroup"))
    assert env.bot.calls == []


async def test_router_subscription_button(env, wired):
    env.bot.member_status = "left"
    prompt = wired.message(USER, texts.NOT_SUBSCRIBED)
    callback = CallbackQuery(id="c1", from_user=USER, chat_instance="ci", data="check_sub", message=prompt)

    await wired.feed(callback_query=callback)
    assert env.bot.called("answer_callback_query")[0]["text"] == texts.STILL_NOT_SUBSCRIBED

    env.bot.member_status = "member"
    await wired.feed(callback_query=callback)
    assert [e["text"] for e in env.bot.called("edit_message_text")] == [texts.welcome(env.settings)]


async def test_router_blocking_is_kept_after_the_middleware_and_only_for_private_chats(env, wired):
    kicked = ChatMemberBanned(user=BOT_USER, until_date=datetime.fromtimestamp(0, timezone.utc))

    await wired.feed(my_chat_member=member_update("supergroup", kicked))
    assert (await load_user(env)).blocked is False

    await wired.feed(my_chat_member=member_update("private", kicked))
    assert (await load_user(env)).blocked is True

    await wired.feed(my_chat_member=member_update("private", ChatMemberMember(user=BOT_USER)))
    assert (await load_user(env)).blocked is False
