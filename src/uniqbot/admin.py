"""The admin side: /stats, /users and /broadcast. Spec 3.

Every handler returns silently for a user who is not in ADMIN_IDS. The router goes before the intake one, so the
handlers of the "waiting for the post" state take the admin's message ahead of the fallback hint.
"""

import asyncio
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import repo, texts
from .broadcast import send_broadcast
from .db import BroadcastStatus, utcnow
from .deps import Deps
from .export import users_csv

log = logging.getLogger(__name__)

GO_CALLBACK = "bc:go"
CANCEL_CALLBACK = "bc:cancel"


class BroadcastForm(StatesGroup):
    waiting_post = State()


def is_admin(user_id: int, deps: Deps) -> bool:
    return user_id in deps.settings.admin_id_list


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.BC_GO, callback_data=GO_CALLBACK),
                InlineKeyboardButton(text=texts.BC_CANCEL, callback_data=CANCEL_CALLBACK),
            ]
        ]
    )


async def _edit(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
    """Put `text` into a message of ours; the admin may have deleted it, which must not stop anything."""
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as e:
        log.warning("could not edit message %s: %s", message_id, e)


async def on_stats(message: Message, bot: Bot, deps: Deps) -> None:
    if not is_admin(message.from_user.id, deps):
        return
    await message.answer(texts.stats(await repo.stats(deps.sessions)))


async def on_users(message: Message, bot: Bot, deps: Deps) -> None:
    if not is_admin(message.from_user.id, deps):
        return
    tz = ZoneInfo(deps.settings.tz)
    data = users_csv(await repo.all_users(deps.sessions), tz)
    name = f"users_{utcnow().astimezone(tz):%Y-%m-%d}.csv"
    await bot.send_document(message.chat.id, BufferedInputFile(data, filename=name))


async def on_broadcast(message: Message, bot: Bot, deps: Deps, state: FSMContext) -> None:
    if not is_admin(message.from_user.id, deps):
        return
    await state.clear()  # a post still waiting for its confirmation is dropped, and its old buttons stop working
    await state.set_state(BroadcastForm.waiting_post)
    await message.answer(texts.BC_ASK_POST)


async def on_cancel(message: Message, bot: Bot, deps: Deps, state: FSMContext) -> None:
    """/cancel while the post is awaited."""
    if not is_admin(message.from_user.id, deps):
        return
    await state.clear()
    await message.answer(texts.BC_CANCELLED)


async def on_broadcast_post(message: Message, bot: Bot, deps: Deps, state: FSMContext) -> None:
    """The post itself: shown to the admin as it will look, then confirmed with the buttons."""
    if not is_admin(message.from_user.id, deps):
        return
    markup = message.reply_markup
    await bot.copy_message(
        chat_id=message.chat.id, from_chat_id=message.chat.id, message_id=message.message_id, reply_markup=markup
    )
    audience = await repo.broadcast_audience(deps.sessions)
    await state.set_state(None)  # the data stays until the admin presses a button
    await state.update_data(
        chat_id=message.chat.id,
        message_id=message.message_id,
        # as plain data, so that any FSM storage can keep it
        reply_markup=markup.model_dump(mode="json", exclude_none=True) if markup is not None else None,
        total=len(audience),
    )
    await message.answer(texts.bc_confirm(len(audience)), reply_markup=_confirm_keyboard())


async def on_broadcast_cancel(callback: CallbackQuery, bot: Bot, deps: Deps, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id, deps):
        return
    post = await state.get_data()
    if "message_id" not in post:  # an old button: its post was sent, cancelled or replaced
        await callback.answer()
        return
    bid = await repo.create_broadcast(
        deps.sessions,
        admin_id=callback.from_user.id,
        source_chat_id=post["chat_id"],
        source_message_id=post["message_id"],
        total=post["total"],
        status=BroadcastStatus.CANCELLED,
    )
    await repo.update_broadcast(deps.sessions, bid, sent=0, blocked=0, failed=0, finished=True)
    await state.clear()
    await callback.answer()
    await _edit(bot, callback.from_user.id, callback.message.message_id, texts.BC_CANCELLED)


async def on_broadcast_go(callback: CallbackQuery, bot: Bot, deps: Deps, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id, deps):
        return
    post = await state.get_data()
    if "message_id" not in post:
        await callback.answer()
        return
    audience = await repo.broadcast_audience(deps.sessions)
    running = deps.broadcast_task
    if running is not None and not running.done():
        await callback.answer(texts.BC_BUSY, show_alert=True)  # the post stays: the button works once the report is in
        return
    # no await between the check above and this line: two presses cannot both start a broadcast
    deps.broadcast_task = asyncio.create_task(
        _run_broadcast(bot, deps, callback.from_user.id, callback.message.message_id, post, audience)
    )
    await state.clear()
    await callback.answer()


async def _run_broadcast(
    bot: Bot, deps: Deps, admin_id: int, progress_id: int, post: dict, audience: list[int]
) -> None:
    """The background part: the question with the buttons turns into the progress, the report is a new message."""
    try:
        bid = await repo.create_broadcast(
            deps.sessions,
            admin_id=admin_id,
            source_chat_id=post["chat_id"],
            source_message_id=post["message_id"],
            total=len(audience),
            status=BroadcastStatus.RUNNING,
        )

        async def on_progress(done: int, total: int) -> None:
            await _edit(bot, admin_id, progress_id, texts.bc_progress(done, total))

        await on_progress(0, len(audience))
        markup = InlineKeyboardMarkup.model_validate(post["reply_markup"]) if post["reply_markup"] else None
        result = await send_broadcast(
            bot,
            deps.sessions,
            bid,
            chat_id=post["chat_id"],
            message_id=post["message_id"],
            reply_markup=markup,
            audience=audience,
            on_progress=on_progress,
        )
        await bot.send_message(admin_id, texts.bc_report(result.sent, result.blocked, result.failed))
    except Exception:
        log.exception("broadcast failed")  # the row stays `running` until the next start marks it interrupted


def _is_post(message: Message) -> bool:
    """Anything but a command: a command sent while the post is awaited is still a command."""
    return not (message.text or "").startswith("/")


def build_router() -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.type == "private")
    router.message.register(on_stats, Command("stats"))
    router.message.register(on_users, Command("users"))
    router.message.register(on_broadcast, Command("broadcast"))
    router.message.register(on_cancel, Command("cancel"), BroadcastForm.waiting_post)
    router.message.register(on_broadcast_post, BroadcastForm.waiting_post, _is_post)
    router.callback_query.register(on_broadcast_go, F.data == GO_CALLBACK)
    router.callback_query.register(on_broadcast_cancel, F.data == CANCEL_CALLBACK)
    return router
