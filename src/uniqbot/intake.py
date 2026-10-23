"""The subscriber side: /start, the subscription gate, video intake, /privacy and the fallback hint.

Handlers share one signature, (event, bot, deps); aiogram passes what each one declares.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Video,
)

from . import repo, texts
from .access import SubscriptionCheckFailed, check_subscription
from .config import Settings
from .deps import Deps
from .redact import redact

log = logging.getLogger(__name__)

CHECK_CALLBACK = "check_sub"
MIB = 1024 * 1024


def _subscribe_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BTN_SUBSCRIBE, url=settings.channel_url),
                InlineKeyboardButton(text=texts.BTN_CHECK, callback_data=CHECK_CALLBACK),
            ]
        ]
    )


async def _check(bot: Bot, deps: Deps, user_id: int) -> bool | None:
    """Subscribed or not, and the answer is recorded; None if Telegram could not tell (the admins are told)."""
    try:
        subscribed = await check_subscription(bot, deps.settings.channel_id, user_id)
    except SubscriptionCheckFailed as e:
        log.warning("subscription check failed for %s: %s", user_id, e)
        await deps.alerts.alert("subscription-check", texts.sub_check_alert(redact(str(e), deps.settings.token_value)))
        return None
    await repo.set_subscription(deps.sessions, user_id, subscribed)
    return subscribed


async def _gate(message: Message, bot: Bot, deps: Deps) -> bool:
    """True if the sender is a subscriber; otherwise they have just been told why not."""
    subscribed = await _check(bot, deps, message.from_user.id)
    if subscribed is None:
        await message.answer(texts.CHECK_FAILED)
    elif not subscribed:
        await message.answer(texts.NOT_SUBSCRIBED, reply_markup=_subscribe_keyboard(deps.settings))
    return bool(subscribed)


async def on_start(message: Message, bot: Bot, deps: Deps, command: CommandObject | None = None) -> None:
    label = repo.parse_source(command.args if command else None)
    await repo.set_source_if_empty(deps.sessions, message.from_user.id, label)
    if await _gate(message, bot, deps):
        await message.answer(texts.welcome(deps.settings))


async def on_check(callback: CallbackQuery, bot: Bot, deps: Deps) -> None:
    subscribed = await _check(bot, deps, callback.from_user.id)
    if subscribed is None:
        await callback.answer(texts.CHECK_FAILED, show_alert=True)
    elif not subscribed:
        await callback.answer(texts.STILL_NOT_SUBSCRIBED, show_alert=True)
    else:
        await callback.answer()
        await _replace_prompt(bot, callback, texts.welcome(deps.settings))


async def _replace_prompt(bot: Bot, callback: CallbackQuery, text: str) -> None:
    """Put `text` in place of the message with the button; if that cannot be edited any more, send it as a new one."""
    prompt = callback.message
    try:
        await bot.edit_message_text(text, chat_id=prompt.chat.id, message_id=prompt.message_id)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):  # pressed twice: the welcome is already there
            await bot.send_message(callback.from_user.id, text)


def _video_of(message: Message) -> Video | Document | None:
    """What to process: a video, or a document with a video MIME type. A GIF is not one (it also comes as a document)."""
    if message.animation is not None:
        return None
    if message.video is not None:
        return message.video
    if message.document is not None and (message.document.mime_type or "").startswith("video/"):
        return message.document
    return None


async def _enqueue(deps: Deps, user_id: int, media: Video | Document, duration: float | None) -> tuple[int, int] | None:
    """(order id, place in the line), or None if the user already has a video in work.

    The look beforehand is only the quick answer; the database refuses a second open order even when
    two videos of an album get here at the same moment.
    """
    if await repo.active_order(deps.sessions, user_id) is not None:
        return None
    try:
        return await repo.create_order(
            deps.sessions,
            tg_id=user_id,
            file_id=media.file_id,
            file_size=media.file_size,
            duration=duration,
            mime_type=media.mime_type,
            file_name=media.file_name,
        )
    except repo.ActiveOrderExists:
        return None


async def on_video(message: Message, bot: Bot, deps: Deps) -> None:
    media = _video_of(message)
    if media is None:
        await message.answer(texts.SEND_VIDEO_HINT)
        return
    if not await _gate(message, bot, deps):
        return

    settings = deps.settings
    if media.file_size is not None and media.file_size > settings.max_file_mb * MIB:
        await message.answer(texts.too_big(settings))
        return
    duration = message.video.duration if message.video is not None else None  # a document has none; ffprobe checks it later
    if duration is not None and duration > settings.max_duration_sec:
        await message.answer(texts.too_long(settings))
        return

    queued = await _enqueue(deps, message.from_user.id, media, duration)
    if queued is None:
        await message.answer(texts.HAS_ACTIVE)
        return
    order_id, position = queued
    log.info("order %s queued for %s", order_id, message.from_user.id)
    try:
        reply = await message.answer(texts.accepted(position))
        await repo.set_status_message(deps.sessions, order_id, reply.message_id)
    finally:
        deps.wake.set()  # the order is in the line whether or not the acknowledgement got through


async def on_privacy(message: Message, bot: Bot, deps: Deps) -> None:
    await message.answer(texts.PRIVACY)


async def on_my_chat_member(event: ChatMemberUpdated, bot: Bot, deps: Deps) -> None:
    """In a private chat the bot becomes `kicked` when the user blocks it and `member` when they come back."""
    status = event.new_chat_member.status
    if status == ChatMemberStatus.KICKED:
        await repo.set_blocked(deps.sessions, event.chat.id, True)
    elif status == ChatMemberStatus.MEMBER:
        await repo.set_blocked(deps.sessions, event.chat.id, False)


async def on_other(message: Message, bot: Bot, deps: Deps) -> None:
    if message.text is not None and message.text.startswith("/"):
        return  # a command nobody took (the admin ones, for a stranger) gets no answer
    await message.answer(texts.SEND_VIDEO_HINT)


def build_router() -> Router:
    router = Router(name="intake")
    router.message.filter(F.chat.type == "private")
    router.message.register(on_start, CommandStart())
    router.callback_query.register(on_check, F.data == CHECK_CALLBACK)
    router.message.register(on_video, F.video | F.document)
    router.message.register(on_privacy, Command("privacy"))
    router.my_chat_member.register(on_my_chat_member, F.chat.type == "private")
    router.message.register(on_other)  # last: whatever the handlers above did not take
    return router
