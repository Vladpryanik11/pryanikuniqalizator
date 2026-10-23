import asyncio
import json
import random
import re
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import EditMessageText, SendDocument
from fakes import ADMIN_IDS, FakeBot, make_deps, register, tg_user
from sqlalchemy import select

from uniqbot import repo, texts
from uniqbot import worker as worker_module
from uniqbot.config import Settings
from uniqbot.db import Order, OrderStatus
from uniqbot.engine import EngineError, NotAVideo, RenderResult, SourceInfo, TooLong
from uniqbot.profile import OrderParams
from uniqbot.worker import CloudFetcher, LocalFetcher, Worker, input_suffix, make_fetcher, recover

ALICE = tg_user(1, "alice")
BOB = tg_user(2, "bob")
PARAMS = OrderParams(0.1, 0.01, 1.02, 1.0, 1.03, 3, 1.05)
SOURCE = SourceInfo(duration=10.0, width=720, height=1280, has_audio=True, rotated=False, hdr=False)
OPEN = (OrderStatus.QUEUED, OrderStatus.PROCESSING)


class FakeFetcher:
    """Puts a stand-in input file into the order's directory, as the real fetchers do."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def fetch(self, bot, file_id, dest_dir, suffix):
        self.calls.append((file_id, dest_dir, suffix))
        dest = dest_dir / f"input{suffix}"
        dest.write_bytes(b"video")
        return dest


class FakeRenderer:
    """Stands in for engine.render: writes output.mp4 and returns its RenderResult.

    `outcomes` has one entry per call: an exception to raise, or None to succeed (an empty list succeeds).
    """

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.outcomes: list[Exception | None] = []

    async def __call__(self, src, workdir, **kwargs):
        files = sorted(p.name for p in workdir.iterdir())
        self.calls.append(SimpleNamespace(src=src, workdir=workdir, files=files, **kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        output = workdir / "output.mp4"
        output.write_bytes(b"rendered")
        return RenderResult(output=output, params=PARAMS, source=SOURCE)


@pytest.fixture
async def env(tmp_path):
    bot = FakeBot()
    deps, engine = await make_deps(tmp_path, bot=bot)
    fetcher, renderer = FakeFetcher(), FakeRenderer()
    worker = Worker(bot, deps, fetcher=fetcher, renderer=renderer)
    yield SimpleNamespace(bot=bot, deps=deps, settings=deps.settings, fetcher=fetcher, renderer=renderer, worker=worker)
    await engine.dispose()


def status_id(user) -> int:
    return 500 + user.id


async def queue(env, user, *, with_status: bool = True) -> int:
    """An order as intake leaves it: queued, with the id of the acknowledgement message."""
    await register(env.deps, user)
    order_id, _ = await repo.create_order(
        env.deps.sessions, tg_id=user.id, file_id=f"file-{user.id}", file_size=1000, duration=10.0,
        mime_type="video/mp4", file_name=None,
    )
    if with_status:
        await repo.set_status_message(env.deps.sessions, order_id, status_id(user))
    return order_id


async def order_row(env, order_id: int) -> Order:
    async with env.deps.sessions() as s:
        return await s.get(Order, order_id)


async def user_row(env, user):
    return next(u for u in await repo.all_users(env.deps.sessions) if u.tg_id == user.id)


async def drain(env) -> None:
    """Process everything that is queued, one order at a time, like the worker loop does."""
    while (order := await repo.claim_next(env.deps.sessions)) is not None:
        await env.worker.process(order)


async def wait_closed(env, *order_ids: int, timeout: float = 10) -> None:
    async with asyncio.timeout(timeout):
        while True:
            rows = [await order_row(env, i) for i in order_ids]
            if all(row.status not in OPEN for row in rows):
                return
            await asyncio.sleep(0.01)


async def start_idle_worker(env) -> asyncio.Task:
    """run_forever on an empty queue, given time to reach its wait for a wake-up."""
    task = asyncio.create_task(env.worker.run_forever())
    await asyncio.sleep(0.2)
    return task


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def edits(env, user) -> list[tuple[int, str]]:
    """(message id, text) of the status edits for one user, in order."""
    return [(kw["message_id"], kw["text"]) for kw in env.bot.called("edit_message_text") if kw["chat_id"] == user.id]


def admin_texts(env) -> list[str]:
    return env.bot.texts(ADMIN_IDS[0])


def work_path(env, order_id: int):
    return env.settings.work_dir / str(order_id)


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SendDocument(chat_id=1, document="x"), message="Forbidden: bot was blocked by the user"
    )


async def test_worker_processes_queue_in_order(env):
    first = await queue(env, ALICE)
    second = await queue(env, BOB)
    task = asyncio.create_task(env.worker.run_forever())
    try:
        await wait_closed(env, first, second)
    finally:
        await stop(task)

    assert [c.workdir.name for c in env.renderer.calls] == [str(first), str(second)]
    for order_id, user in ((first, ALICE), (second, BOB)):
        assert edits(env, user) == [(status_id(user), texts.PROCESSING), (status_id(user), texts.DONE_STATUS)]
        sent = [kw for kw in env.bot.called("send_document") if kw["chat_id"] == user.id]
        assert len(sent) == 1
        assert sent[0]["caption"] == texts.DONE_CAPTION
        # the local server uploads the file to Telegram before it answers: aiogram's 60 s would not do
        assert sent[0]["request_timeout"] == worker_module.LONG_REQUEST_SEC == 600
        assert re.fullmatch(r"uniq_[0-9a-f]{8}\.mp4", sent[0]["document"].filename)
        # processing, the result, then the "done" edit
        assert [m for m, kw in env.bot.calls if kw.get("chat_id") == user.id] == [
            "edit_message_text", "send_document", "edit_message_text",
        ]
        row = await order_row(env, order_id)
        assert row.status == OrderStatus.DONE
        assert json.loads(row.params_json) == PARAMS.to_dict()
        assert (await user_row(env, user)).videos_done == 1
        assert not work_path(env, order_id).exists()
    assert env.bot.sent_files == [b"rendered", b"rendered"]
    assert env.bot.texts() == []  # nothing to the users but the files, nothing to the admins


async def test_renderer_gets_the_input_the_limits_and_a_fresh_rng(env):
    order_id = await queue(env, ALICE)
    await drain(env)

    (call,) = env.renderer.calls
    assert env.fetcher.calls == [("file-1", work_path(env, order_id), ".mp4")]
    assert call.src == work_path(env, order_id) / "input.mp4"
    assert call.ranges is env.deps.ranges
    assert call.max_duration == env.settings.max_duration_sec == 180
    assert call.timeout == env.settings.render_timeout_sec == 900
    assert isinstance(call.rng, random.Random)


async def test_stale_work_dir_of_a_requeued_order_is_cleared(env):
    order_id = await queue(env, ALICE)
    stale = work_path(env, order_id)
    stale.mkdir(parents=True)
    (stale / "clean.mkv").write_bytes(b"left over from before the restart")
    await drain(env)

    assert env.renderer.calls[0].files == ["input.mp4"]
    assert (await order_row(env, order_id)).status == OrderStatus.DONE


async def test_engine_failure_marks_failed_and_alerts(env):
    first = await queue(env, ALICE)
    second = await queue(env, BOB)
    env.renderer.outcomes = [EngineError("boom")]
    await drain(env)

    assert env.bot.texts(ALICE.id) == [texts.FAILED]
    row = await order_row(env, first)
    assert row.status == OrderStatus.FAILED
    assert "boom" in row.error
    assert (await user_row(env, ALICE)).videos_done == 0
    for admin_id in ADMIN_IDS:
        (alert,) = env.bot.texts(admin_id)
        assert str(first) in alert and str(ALICE.id) in alert and "boom" in alert
    assert not work_path(env, first).exists()
    # the next order is not held up
    assert (await order_row(env, second)).status == OrderStatus.DONE
    assert len(env.bot.called("send_document")) == 1


async def test_unexpected_exception_fails_the_order_logs_and_alerts(env, caplog):
    first = await queue(env, ALICE)
    second = await queue(env, BOB)
    env.renderer.outcomes = [RuntimeError("kaboom")]
    with caplog.at_level("ERROR"):
        await drain(env)

    assert env.bot.texts(ALICE.id) == [texts.FAILED]
    row = await order_row(env, first)
    assert row.status == OrderStatus.FAILED
    assert "kaboom" in row.error
    (alert,) = admin_texts(env)
    assert str(first) in alert and "kaboom" in alert
    assert any(r.exc_info and "kaboom" in str(r.exc_info[1]) for r in caplog.records)  # logged with the stack
    assert (await order_row(env, second)).status == OrderStatus.DONE


@pytest.mark.parametrize(
    "error, expected",
    [(NotAVideo("no video stream"), lambda s: texts.NOT_A_VIDEO), (TooLong("200s is longer than 180s"), texts.too_long)],
    ids=["not-a-video", "too-long"],
)
async def test_user_errors_do_not_alert_admins(env, error, expected):
    order_id = await queue(env, ALICE)
    env.renderer.outcomes = [error]
    await drain(env)

    assert env.bot.texts(ALICE.id) == [expected(env.settings)]
    row = await order_row(env, order_id)
    assert row.status == OrderStatus.FAILED
    assert str(error) in row.error
    assert admin_texts(env) == []
    assert not work_path(env, order_id).exists()


async def test_long_error_keeps_its_tail(env):
    order_id = await queue(env, ALICE)
    env.renderer.outcomes = [EngineError("x" * 3000 + "the real reason")]
    await drain(env)

    row = await order_row(env, order_id)
    assert len(row.error) <= repo.ERROR_MAX
    assert row.error.endswith("the real reason")  # the end of a stderr tail is what explains the failure
    assert len(admin_texts(env)[0]) < 4096  # fits one Telegram message


@pytest.mark.parametrize(
    "configured, token",
    [
        ("123456:" + "A" * 35, "123456:" + "A" * 35),  # the token is known and has the usual shape
        (None, "123456:" + "A" * 35),  # not configured: it is recognised by its shape
        ("secret-without-a-token-shape", "secret-without-a-token-shape"),  # known, but shaped like nothing
    ],
    ids=["known", "by-shape", "exact-only"],
)
async def test_the_token_never_reaches_the_order_or_the_alert(tmp_path, configured, token):
    bot = FakeBot()
    deps, engine = await make_deps(tmp_path, bot=bot, bot_token=configured)
    env = SimpleNamespace(bot=bot, deps=deps, settings=deps.settings, renderer=FakeRenderer())

    class LeakyFetcher:
        async def fetch(self, bot, file_id, dest_dir, suffix):
            raise FileNotFoundError(f"/var/lib/telegram-bot-api/{token}/videos/file_0.mp4")

    env.worker = Worker(bot, deps, fetcher=LeakyFetcher(), renderer=env.renderer)
    try:
        order_id = await queue(env, ALICE)
        await drain(env)

        row = await order_row(env, order_id)
        assert row.status == OrderStatus.FAILED
        assert "<token>" in row.error and token not in row.error
        assert "file_0.mp4" in row.error  # only the token goes, the rest of the reason stays
        for admin_id in ADMIN_IDS:
            (alert,) = env.bot.texts(admin_id)
            assert "<token>" in alert and token not in alert
    finally:
        await engine.dispose()


async def test_user_blocked_bot_before_result(env):
    first = await queue(env, ALICE)
    second = await queue(env, BOB)
    env.bot.document_errors[ALICE.id] = forbidden()
    await drain(env)

    assert (await user_row(env, ALICE)).blocked is True
    row = await order_row(env, first)
    assert (row.status, row.error) == (OrderStatus.FAILED, "blocked")
    assert env.bot.texts(ALICE.id) == []  # nobody to tell
    assert admin_texts(env) == []  # and not an error of ours
    assert not work_path(env, first).exists()
    assert (await order_row(env, second)).status == OrderStatus.DONE
    assert (await user_row(env, BOB)).blocked is False


async def test_user_blocked_bot_before_the_failure_notice(env):
    order_id = await queue(env, ALICE)
    env.renderer.outcomes = [EngineError("boom")]
    env.bot.send_errors[ALICE.id] = forbidden()
    await drain(env)

    assert (await user_row(env, ALICE)).blocked is True
    assert "boom" in (await order_row(env, order_id)).error  # the real reason stays on the order
    assert len(admin_texts(env)) == 1


async def test_result_that_telegram_refuses_fails_the_order_and_alerts(env):
    order_id = await queue(env, ALICE)
    env.bot.document_errors[ALICE.id] = TelegramBadRequest(
        method=SendDocument(chat_id=1, document="x"), message="Bad Request: file is too big"
    )
    await drain(env)

    assert env.bot.texts(ALICE.id) == [texts.FAILED]
    row = await order_row(env, order_id)
    assert row.status == OrderStatus.FAILED
    assert "file is too big" in row.error
    assert len(admin_texts(env)) == 1
    assert (await user_row(env, ALICE)).blocked is False


async def test_status_message_gone_does_not_fail_order(env):
    order_id = await queue(env, ALICE)
    env.bot.edit_error = TelegramBadRequest(
        method=EditMessageText(text="x", chat_id=1, message_id=7), message="Bad Request: message to edit not found"
    )
    await drain(env)

    assert (await order_row(env, order_id)).status == OrderStatus.DONE
    assert len(env.bot.called("send_document")) == 1
    assert (await user_row(env, ALICE)).videos_done == 1
    assert [text for _, text in edits(env, ALICE)] == [texts.PROCESSING, texts.DONE_STATUS]  # both were tried
    assert env.bot.texts() == []


async def test_missing_status_message_gets_a_fresh_one(env):
    # the worker can claim an order between create_order and set_status_message
    order_id = await queue(env, ALICE, with_status=False)
    env.bot.last_message_id = 700
    await drain(env)

    assert env.bot.called("send_message") == [{"chat_id": ALICE.id, "text": texts.PROCESSING}]
    assert edits(env, ALICE) == [(701, texts.DONE_STATUS)]  # the finished status goes to that new message
    assert (await order_row(env, order_id)).status == OrderStatus.DONE


async def test_wake_cuts_the_idle_wait_short(env):
    task = await start_idle_worker(env)  # nothing queued: it waits up to 30 s for a wake-up
    try:
        order_id = await queue(env, ALICE)
        env.deps.wake.set()
        await wait_closed(env, order_id, timeout=5)
    finally:
        await stop(task)

    assert (await order_row(env, order_id)).status == OrderStatus.DONE
    assert not env.deps.wake.is_set()  # the worker took the wake-up


async def test_run_forever_survives_an_exception_from_process(env, monkeypatch, caplog):
    monkeypatch.setattr(worker_module, "ERROR_PAUSE_SEC", 0)
    real_process, seen = env.worker.process, []

    async def flaky(order):
        seen.append(order.id)
        if len(seen) == 1:
            raise RuntimeError("bug in process")
        await real_process(order)

    env.worker.process = flaky
    task = await start_idle_worker(env)
    try:
        first = await queue(env, ALICE)
        second = await queue(env, BOB)
        env.deps.wake.set()
        with caplog.at_level("ERROR"):
            await wait_closed(env, second)
    finally:
        await stop(task)

    assert seen == [first, second]
    assert (await order_row(env, second)).status == OrderStatus.DONE
    assert any(r.exc_info and "bug in process" in str(r.exc_info[1]) for r in caplog.records)


async def test_recover_requeues_and_removes_orphans(env):
    order_id = await queue(env, ALICE)
    assert (await repo.claim_next(env.deps.sessions)).id == order_id
    work = env.settings.work_dir
    (work / "999").mkdir(parents=True)
    (work / "999" / "clean.mkv").write_bytes(b"orphan")
    (work / str(order_id)).mkdir()

    await recover(env.deps)

    row = await order_row(env, order_id)
    assert (row.status, row.started_at) == (OrderStatus.QUEUED, None)
    assert not (work / "999").exists()
    assert (work / str(order_id)).exists()


async def test_recover_without_a_work_dir(env):
    await recover(env.deps)  # nothing to clean, nothing to requeue


async def test_local_fetcher_moves_file(tmp_path):
    server_file = tmp_path / "server" / "videos" / "file_0.mp4"
    server_file.parent.mkdir(parents=True)
    server_file.write_bytes(b"video")
    bot = FakeBot()
    bot.file_path = str(server_file)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    result = await LocalFetcher().fetch(bot, "fid", dest_dir, ".mp4")

    assert result == dest_dir / "input.mp4"
    assert result.read_bytes() == b"video"
    assert not server_file.exists()
    # the local server fetches the whole file from Telegram before it answers getFile: aiogram's 60 s would not do
    assert bot.called("get_file") == [{"file_id": "fid", "request_timeout": 600}]


async def test_cloud_fetcher_downloads_by_file_id(tmp_path):
    bot = FakeBot()
    bot.file_bytes = b"from the cloud"

    result = await CloudFetcher().fetch(bot, "fid", tmp_path, ".mov")

    assert result == tmp_path / "input.mov"
    assert result.read_bytes() == b"from the cloud"
    assert bot.called("download") == [{"file": "fid", "destination": tmp_path / "input.mov", "timeout": 600}]


def test_make_fetcher_follows_bot_api_url():
    assert isinstance(make_fetcher(Settings(bot_api_url="http://bot-api:8081")), LocalFetcher)
    assert isinstance(make_fetcher(Settings(bot_api_url=None)), CloudFetcher)


@pytest.mark.parametrize(
    "mime_type, file_name, expected",
    [
        ("video/quicktime", "IMG_1.MOV", ".MOV"),
        (None, None, ".mp4"),
        ("video/mp4", "clip", ".mp4"),
        ("video/webm", "a/../b.webm", ".webm"),
        ("video/mp4", "evil.mp4 && rm", ".mp4"),
        ("video/mp4", "name.toolongextension", ".mp4"),
    ],
)
def test_input_suffix(mime_type, file_name, expected):
    assert input_suffix(mime_type, file_name) == expected
