import asyncio
import io
import itertools
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import LogOut
from aiogram.types import Chat, Message, MessageEntity, Update
from fakes import ADMIN_IDS, CHANNEL_ID, CHANNEL_URL, FakeBot, make_deps, real_bot, tg_user, video
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from uniqbot import __main__ as cli
from uniqbot import app, repo, texts
from uniqbot.app import (
    ALLOWED_UPDATES,
    StartupError,
    build_bot,
    build_dispatcher,
    logout_cloud,
    run_bot,
    startup_problems,
)
from uniqbot.config import Settings
from uniqbot.db import Broadcast, BroadcastStatus, OrderStatus, init_db, make_engine, make_sessions

TOKEN = "123456:SECRET-token_abc"
ADMIN = tg_user(ADMIN_IDS[0], "boss")


def make_settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        **{
            "bot_token": TOKEN,
            "channel_id": CHANNEL_ID,
            "channel_url": CHANNEL_URL,
            "admin_ids": ",".join(map(str, ADMIN_IDS)),
            "data_dir": tmp_path / "data",
            **overrides,
        }
    )


# --- the bot and the dispatcher ---


def test_build_bot_local_mode(tmp_path):
    local = build_bot(make_settings(tmp_path, bot_api_url="http://bot-api:8081/"))
    assert local.session.api.is_local is True
    assert local.session.api.api_url("T", "getMe") == "http://bot-api:8081/botT/getMe"
    assert local.session.api.file_url("T", "a/b.mp4") == "http://bot-api:8081/file/botT/a/b.mp4"

    cloud = build_bot(make_settings(tmp_path))
    assert cloud.session.api.is_local is False
    assert cloud.session.api.api_url("T", "getMe").startswith("https://api.telegram.org/")


def test_build_bot_sends_plain_text(tmp_path):
    # admin alerts carry raw ffmpeg stderr full of < and >, which HTML parsing would reject
    assert build_bot(make_settings(tmp_path)).default.parse_mode is None


def test_allowed_updates():
    assert ALLOWED_UPDATES == ["message", "callback_query", "my_chat_member"]


@pytest.fixture
async def env(tmp_path):
    fake = FakeBot()
    deps, engine = await make_deps(tmp_path, bot=fake)
    yield SimpleNamespace(fake=fake, deps=deps)
    await engine.dispose()


async def test_dispatcher_builds_twice(env):
    assert build_dispatcher(env.deps) is not build_dispatcher(env.deps)


@pytest.fixture
async def wired(env):
    """The application's dispatcher on a real aiogram Bot that talks to FakeBot. Nobody is registered in advance."""
    bot = real_bot(env.fake)
    dp = build_dispatcher(env.deps)
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
        env.fake.calls.clear()
        await dp.feed_update(bot, Update(update_id=next(ids), **fields))

    yield SimpleNamespace(feed=feed, message=message)
    await bot.session.close()


async def test_a_new_user_with_a_video_gets_a_user_row_and_a_queued_order(env, wired):
    newbie = tg_user(2, "newbie")

    await wired.feed(message=wired.message(newbie, video=video("w1")))

    assert env.fake.texts(newbie.id) == [texts.accepted(1)]
    assert [u.tg_id for u in await repo.all_users(env.deps.sessions)] == [newbie.id]  # TrackUser ran first
    order = await repo.active_order(env.deps.sessions, newbie.id)
    assert (order.file_id, order.status) == ("w1", OrderStatus.QUEUED)


async def test_the_admins_post_goes_to_the_admin_router_not_to_the_fallback_hint(env, wired):
    await wired.feed(message=wired.message(ADMIN, "/broadcast"))
    assert env.fake.texts(ADMIN.id) == [texts.BC_ASK_POST]

    post = wired.message(ADMIN, "Новость")
    await wired.feed(message=post)

    assert [c["message_id"] for c in env.fake.called("copy_message")] == [post.message_id]
    assert env.fake.texts(ADMIN.id) == [texts.bc_confirm(1)]  # the admin is the only user so far
    assert texts.SEND_VIDEO_HINT not in env.fake.texts()


# --- run_bot ---


@pytest.fixture
def stub(monkeypatch):
    """What run_bot talks to instead of Telegram: a recording bot, a worker that only waits, a polling that returns.

    `during` is awaited in place of polling, with the dispatcher.
    """
    fake = FakeBot()
    bot = real_bot(fake)
    seen = SimpleNamespace(fake=fake, bot=bot, polls=[], workers=[], during=None)

    class WaitingWorker:
        def __init__(self, bot, deps, *, fetcher):
            self.cancelled = False
            seen.workers.append(self)

        async def run_forever(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def polling(dp, *bots, **kwargs):
        seen.polls.append((bots, kwargs))
        if seen.during is not None:
            await seen.during(dp)

    monkeypatch.setattr(app, "build_bot", lambda settings: bot)
    monkeypatch.setattr(app, "Worker", WaitingWorker)
    monkeypatch.setattr(Dispatcher, "start_polling", polling)
    return seen


async def broadcasts(deps) -> list[Broadcast]:
    async with deps.sessions() as s:
        return list((await s.scalars(select(Broadcast))).all())


async def seed_unfinished_work(settings: Settings, user_id: int) -> None:
    """What a killed process leaves behind: an order in work, a running broadcast, a work dir nobody needs."""
    engine = make_engine(settings.db_path)
    await init_db(engine)
    sessions = make_sessions(engine)
    await repo.touch_user(sessions, repo.TgProfile(tg_id=user_id, username="vlad", first_name="Vlad"))
    await repo.create_order(
        sessions, tg_id=user_id, file_id="f", file_size=1, duration=1, mime_type="video/mp4", file_name=None
    )
    await repo.claim_next(sessions)
    await repo.create_broadcast(
        sessions, admin_id=ADMIN.id, source_chat_id=1, source_message_id=1, total=1, status=BroadcastStatus.RUNNING
    )
    (settings.work_dir / "999").mkdir(parents=True)
    await engine.dispose()


async def test_run_bot_recovers_then_serves_and_cleans_up_after_itself(stub, tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    await seed_unfinished_work(settings, 1)
    disposed = []
    real_dispose = AsyncEngine.dispose

    async def spy(self, *args, **kwargs):
        disposed.append(self)
        await real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)
    during = {}

    async def while_polling(dp):
        deps = dp["deps"]
        during["order"] = (await repo.active_order(deps.sessions, 1)).status
        during["broadcast"] = [r.status for r in await broadcasts(deps)]
        during["work_dirs"] = [p.name for p in settings.work_dir.glob("*")]
        during["commands"] = stub.fake.called("set_my_commands")
        during["worker_running"] = [not w.cancelled for w in stub.workers]
        stub.running_broadcast = deps.broadcast_task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)  # let the task start

    stub.during = while_polling
    await run_bot(settings)

    # at the start the previous run's leftovers were dealt with, and the menu was set before the polling
    assert during["order"] == OrderStatus.QUEUED
    assert during["broadcast"] == [BroadcastStatus.INTERRUPTED]
    assert during["work_dirs"] == []
    [call] = during["commands"]
    assert call["commands"] == [
        {"command": "start", "description": texts.CMD_START},
        {"command": "privacy", "description": texts.CMD_PRIVACY},
    ]
    assert during["worker_running"] == [True]
    # one polling, of our bot, for the updates the handlers take
    assert stub.polls == [((stub.bot,), {"allowed_updates": ALLOWED_UPDATES})]
    # at the end everything is stopped
    assert [w.cancelled for w in stub.workers] == [True]
    assert stub.running_broadcast.cancelled()
    assert stub.bot.session.closed is True
    assert len(disposed) == 1


async def test_run_bot_cleans_up_when_polling_fails(stub, tmp_path):
    async def failing(dp):
        await asyncio.sleep(0)  # the worker has started
        raise RuntimeError("network is gone")

    stub.during = failing
    with pytest.raises(RuntimeError, match="network is gone"):
        await run_bot(make_settings(tmp_path))

    assert [w.cancelled for w in stub.workers] == [True]
    assert stub.bot.session.closed is True


async def test_run_bot_logs_a_crashed_worker_with_its_stack(stub, tmp_path, monkeypatch, caplog):
    class CrashingWorker:
        def __init__(self, bot, deps, *, fetcher):
            pass

        async def run_forever(self):
            raise RuntimeError("boom")

    async def a_moment(dp):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(app, "Worker", CrashingWorker)
    stub.during = a_moment
    with caplog.at_level(logging.ERROR, logger="uniqbot.app"):
        await run_bot(make_settings(tmp_path))

    [record] = [r for r in caplog.records if r.name == "uniqbot.app"]
    assert record.exc_info is not None and record.exc_info[1].args == ("boom",)


async def test_run_bot_gives_the_worker_the_fetcher_for_its_bot_api(stub, tmp_path, monkeypatch):
    fetchers = []

    class RecordingWorker:
        def __init__(self, bot, deps, *, fetcher):
            fetchers.append(type(fetcher).__name__)

        async def run_forever(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(app, "Worker", RecordingWorker)
    await run_bot(make_settings(tmp_path))
    await run_bot(make_settings(tmp_path, bot_api_url="http://bot-api:8081"))

    assert fetchers == ["CloudFetcher", "LocalFetcher"]


# --- start-up checks ---


def test_a_complete_setup_has_no_problems(tmp_path):
    assert startup_problems(make_settings(tmp_path)) == []


@pytest.mark.parametrize(
    ("override", "setting"),
    [
        ({"bot_token": None}, "BOT_TOKEN"),
        ({"bot_token": "not a token"}, "BOT_TOKEN"),
        ({"channel_id": ""}, "CHANNEL_ID"),
        ({"channel_url": ""}, "CHANNEL_URL"),
        ({"admin_ids": ""}, "ADMIN_IDS"),
        ({"admin_ids": "11,boss"}, "ADMIN_IDS"),
        ({"tz": "Mars/Base"}, "TZ"),
    ],
)
def test_startup_problem_names_the_setting(tmp_path, override, setting):
    [problem] = startup_problems(make_settings(tmp_path, **override))
    assert setting in problem
    assert "not a token" not in problem


async def test_run_bot_refuses_a_broken_setup_before_touching_anything(stub, tmp_path):
    settings = make_settings(tmp_path, bot_token=None, admin_ids="")

    with pytest.raises(StartupError) as e:
        await run_bot(settings)

    assert "BOT_TOKEN" in str(e.value) and "ADMIN_IDS" in str(e.value)
    assert not settings.data_dir.exists()
    assert stub.polls == []


# --- the command line ---


def test_main_dispatches_commands(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "setup_logging", lambda token: None)
    calls = []

    async def fake_run_bot(settings):
        calls.append(("bot", settings))

    async def fake_logout(settings):
        calls.append(("logout-cloud", settings))
        return True

    async def fake_render(src, workdir, *, ranges, max_duration, timeout, rng):
        calls.append(("render", src, rng.random()))
        output = workdir / "output.mp4"
        output.write_bytes(b"video")
        return SimpleNamespace(output=output, params=SimpleNamespace(to_dict=lambda: {"rate": 1.25}))

    monkeypatch.setattr(cli, "run_bot", fake_run_bot)
    monkeypatch.setattr(cli, "logout_cloud", fake_logout)
    monkeypatch.setattr(cli, "render", fake_render)

    assert cli.main(["bot"]) == 0
    assert cli.main(["render", "a.mp4", "b.mp4", "--seed", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"rate": 1.25}
    assert (tmp_path / "b.mp4").read_bytes() == b"video"
    assert cli.main(["logout-cloud"]) == 0
    assert "Logged out" in capsys.readouterr().out

    assert [c[0] for c in calls] == ["bot", "render", "logout-cloud"]
    assert isinstance(calls[0][1], Settings) and isinstance(calls[2][1], Settings)
    assert calls[1][1:] == (Path("a.mp4"), random.Random(3).random())


def test_main_bot_prints_what_is_missing_and_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup_logging", lambda token: None)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(bot_token=None, channel_id="", admin_ids=""))

    assert cli.main(["bot"]) == 2

    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err and "CHANNEL_ID" in err and "ADMIN_IDS" in err


def test_main_logout_cloud_reports_a_refusal(monkeypatch, capsys):
    async def refused(settings):
        raise TelegramUnauthorizedError(method=LogOut(), message="Unauthorized")

    monkeypatch.setattr(cli, "logout_cloud", refused)

    assert cli.main(["logout-cloud"]) == 1
    assert "Unauthorized" in capsys.readouterr().err


async def test_logout_cloud_goes_to_the_cloud_even_when_a_local_server_is_set(monkeypatch, tmp_path):
    seen = []

    async def log_out(self, request_timeout=None):
        seen.append(self.session.api.is_local)
        return True

    monkeypatch.setattr(Bot, "log_out", log_out)

    assert await logout_cloud(make_settings(tmp_path, bot_api_url="http://bot-api:8081")) is True
    assert seen == [False]


async def test_logout_cloud_needs_the_token(tmp_path):
    with pytest.raises(StartupError, match="BOT_TOKEN"):
        await logout_cloud(make_settings(tmp_path, bot_token=None))


def test_log_output_never_holds_the_token():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(cli.TokenRedactingFormatter("%(message)s", token=TOKEN))
    log = logging.getLogger("test_log_output_never_holds_the_token")
    log.propagate = False
    log.addHandler(handler)
    url = f"https://api.telegram.org/file/bot{TOKEN}/videos/file_1.mp4"

    log.error("download failed: %s", url)
    try:
        raise RuntimeError(f"404, url={url}")
    except RuntimeError:
        log.exception("order 7 failed")

    out = stream.getvalue()
    assert TOKEN not in out
    assert "download failed" in out and "404" in out and "RuntimeError" in out
