"""Fakes for the aiogram layer: a recording bot and session, messages, callbacks and a deps builder."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import Document, FSInputFile, Video
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncEngine

from uniqbot import repo
from uniqbot.access import AdminAlerts
from uniqbot.config import Settings
from uniqbot.db import init_db, make_engine, make_sessions
from uniqbot.deps import Deps
from uniqbot.profile import load_ranges

CHANNEL_ID = "-1001000000000"
CHANNEL_URL = "https://t.me/test_channel"
ADMIN_IDS = (11, 22)
MIB = 1024 * 1024


class FakeBot:
    """Records the API calls of the handlers as (method, kwargs) in `calls`.

    get_chat_member answers from `member_status` (and `member_is_member` for "restricted") or raises `member_error`;
    `send_errors` maps a chat id to the error send_message raises for it, `edit_error` is raised by edit_message_text
    and `document_errors` (by chat id, like `send_errors`) by send_document. `copy_errors` maps a chat id to the
    errors copy_message raises for it, one per call in order, and then it succeeds. send_document reads the file it
    is given into `sent_files`, as the real one would; download() writes `file_bytes` to the destination.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.member_status = "member"
        self.member_is_member = True
        self.member_error: Exception | None = None
        self.send_errors: dict[int, Exception] = {}
        self.edit_error: Exception | None = None
        self.document_errors: dict[int, Exception] = {}
        self.copy_errors: dict[int, list[Exception]] = {}
        self.sent_files: list[bytes] = []
        self.file_path = "videos/file_0.mp4"
        self.file_bytes = b"video"
        self.last_message_id = 100

    def _record(self, method: str, **kwargs) -> None:
        self.calls.append((method, kwargs))

    def called(self, method: str) -> list[dict]:
        return [kw for name, kw in self.calls if name == method]

    def texts(self, chat_id: int | None = None) -> list[str]:
        """Texts of the messages sent, optionally only those to one chat."""
        return [kw["text"] for kw in self.called("send_message") if chat_id in (None, kw["chat_id"])]

    async def send_message(self, chat_id, text, **kwargs):
        self._record("send_message", chat_id=chat_id, text=text, **kwargs)
        if chat_id in self.send_errors:
            raise self.send_errors[chat_id]
        self.last_message_id += 1
        return SimpleNamespace(message_id=self.last_message_id)

    async def send_document(self, chat_id, document, **kwargs):
        self._record("send_document", chat_id=chat_id, document=document, **kwargs)
        if chat_id in self.document_errors:
            raise self.document_errors[chat_id]
        if isinstance(document, FSInputFile):
            self.sent_files.append(Path(document.path).read_bytes())
        self.last_message_id += 1
        return SimpleNamespace(message_id=self.last_message_id)

    async def edit_message_text(self, text, **kwargs):
        self._record("edit_message_text", text=text, **kwargs)
        if self.edit_error is not None:
            raise self.edit_error
        return True

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self._record("copy_message", chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kwargs)
        if self.copy_errors.get(chat_id):
            raise self.copy_errors[chat_id].pop(0)
        self.last_message_id += 1
        return SimpleNamespace(message_id=self.last_message_id)

    async def answer_callback_query(self, callback_query_id, **kwargs):
        self._record("answer_callback_query", callback_query_id=callback_query_id, **kwargs)
        return True

    async def get_chat_member(self, chat_id, user_id):
        self._record("get_chat_member", chat_id=chat_id, user_id=user_id)
        if self.member_error is not None:
            raise self.member_error
        return SimpleNamespace(status=self.member_status, is_member=self.member_is_member)

    async def set_my_commands(self, commands, **kwargs):
        self._record("set_my_commands", commands=commands, **kwargs)
        return True

    async def get_file(self, file_id):
        self._record("get_file", file_id=file_id)
        return SimpleNamespace(file_path=self.file_path)

    async def download(self, file, destination=None, **kwargs):
        self._record("download", file=file, destination=destination, **kwargs)
        Path(destination).write_bytes(self.file_bytes)


class FakeMessage:
    """The part of aiogram's Message the handlers use; answer() goes through the bot like the real one."""

    def __init__(
        self, bot, user, *, text=None, video=None, document=None, animation=None, message_id=1, reply_markup=None
    ) -> None:
        self.bot = bot
        self.from_user = user
        self.chat = SimpleNamespace(id=user.id, type="private")
        self.message_id = message_id
        self.text = text
        self.video = video
        self.document = document
        self.animation = animation
        self.reply_markup = reply_markup

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)


class FakeCallback:
    def __init__(self, bot, user, data="check_sub", message=None) -> None:
        self.id = "cb1"
        self.bot = bot
        self.from_user = user
        self.data = data
        self.message = message or FakeMessage(bot, user, message_id=7)

    async def answer(self, text=None, show_alert=None, **kwargs):
        await self.bot.answer_callback_query(self.id, text=text, show_alert=show_alert, **kwargs)


_API_METHODS = {
    "sendMessage": "send_message",
    "sendDocument": "send_document",
    "editMessageText": "edit_message_text",
    "copyMessage": "copy_message",
    "answerCallbackQuery": "answer_callback_query",
    "getChatMember": "get_chat_member",
    "getFile": "get_file",
    "setMyCommands": "set_my_commands",
}


class FakeSession(BaseSession):
    """Lets a real aiogram Bot (and so a Dispatcher) run on a FakeBot: every API call is passed to it."""

    def __init__(self, fake: FakeBot) -> None:
        super().__init__()
        self.fake = fake
        self.closed = False

    async def make_request(self, bot, method, timeout=None):
        handler = getattr(self.fake, _API_METHODS[method.__api_method__])
        return await handler(**method.model_dump(exclude_none=True))

    async def close(self) -> None:
        self.closed = True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError
        yield  # makes this an async generator like the real one


def real_bot(fake: FakeBot) -> Bot:
    return Bot("123456:TEST", session=FakeSession(fake))


def tg_user(user_id: int = 1, username: str | None = "vlad", **kwargs) -> TgUser:
    return TgUser(id=user_id, is_bot=False, first_name="Vlad", username=username, **kwargs)


def video(file_id: str = "vid1", *, size: int | None = 5 * MIB, duration: int = 30) -> Video:
    return Video(
        file_id=file_id, file_unique_id=f"u-{file_id}", width=720, height=1280, duration=duration, file_size=size,
        mime_type="video/mp4",
    )


def document(mime_type: str | None, file_id: str = "doc1", *, name: str | None = None, size: int = 5 * MIB) -> Document:
    return Document(file_id=file_id, file_unique_id=f"u-{file_id}", file_name=name, mime_type=mime_type, file_size=size)


async def register(deps: Deps, user: TgUser) -> None:
    """The user row the TrackUser middleware creates in production (orders need it)."""
    await repo.touch_user(deps.sessions, repo.TgProfile(tg_id=user.id, username=user.username, first_name=user.first_name))


async def make_deps(tmp_path, *, bot: FakeBot | None = None, **settings) -> tuple[Deps, AsyncEngine]:
    """Deps over a temp SQLite file; the caller disposes the engine. Settings overrides go in by keyword."""
    config = Settings(
        **{
            "channel_id": CHANNEL_ID,
            "channel_url": CHANNEL_URL,
            "admin_ids": ",".join(map(str, ADMIN_IDS)),
            "max_file_mb": 200,
            "max_duration_sec": 180,
            "render_timeout_sec": 900,
            "data_dir": tmp_path / "data",
            **settings,
        }
    )
    engine = make_engine(config.db_path)
    await init_db(engine)
    deps = Deps(
        settings=config,
        sessions=make_sessions(engine),
        ranges=load_ranges(config.uniq_config),
        wake=asyncio.Event(),
        alerts=AdminAlerts(bot or FakeBot(), config.admin_id_list),
    )
    return deps, engine

