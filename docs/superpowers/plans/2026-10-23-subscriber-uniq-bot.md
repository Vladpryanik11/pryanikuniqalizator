# Бот-уникализатор для подписчиков: план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** бесплатный Telegram-бот: подписчик канала присылает видео и получает одну уникализированную копию
(движок Hostlife22/video-uniquifier, усиленный пресет `instagram_reels`); бот ведёт базу пользователей,
отдаёт её CSV и делает рассылки.

**Architecture:** один процесс aiogram 3 (long polling через локальный сервер Bot API) + фоновый воркер,
который берёт заказы из SQLite по одному и запускает `python -m video_uniquifier run` отдельным процессом
на подготовленном входе. Профиль заказа строится из пресета движка со случайными значениями из
`config/uniq.yaml`. На VPS два контейнера в своём `docker-compose`: `bot-api` и `bot`.

**Tech Stack:** Python 3.12, aiogram ≥3.31,<4, SQLAlchemy 2 async + aiosqlite, pydantic-settings, PyYAML,
video-uniquifier (git, commit `e9138dd`), ffmpeg/ffprobe, pytest + pytest-asyncio, Docker Compose, PowerShell-скрипты деплоя.

**Spec:** `docs/superpowers/specs/2026-10-23-subscriber-uniq-bot-design.md`

## Global Constraints

- Python ≥ 3.12; пакет `uniqbot` в `src/uniqbot`, сборка hatchling (`[tool.hatch.metadata] allow-direct-references = true`).
- Движок: `video-uniquifier @ git+https://github.com/Hostlife22/video-uniquifier@e9138ddbc798cb23e28b44d9edf7959a712f0add`. Код движка не копируется и не правится. Основа профиля — `instagram_reels.yaml` из установленного пакета (`importlib.resources`). В репозитории `NOTICE` с MIT-текстом движка.
- Пределы по умолчанию: `MAX_FILE_MB=200`, `MAX_DURATION_SEC=180`, `RENDER_TIMEOUT_SEC=900`; итог не длиннее 180 с (лимит Reels). Один активный заказ на человека, один рендер за раз, суточного лимита нет.
- Все тексты для пользователя — по-русски и только в `src/uniqbot/texts.py`, ровно как в Task 4. Комментарии в коде — по-английски, коротко, как в Уникализаторе.
- Видео в базе не хранятся; `data/work/<order_id>/` удаляется после каждого заказа.
- Секреты только в `.env` (в `.gitignore`); тесты никогда не читают настоящий `.env` (фикстура как в Уникализаторе).
- Сервер: каталог `/opt/uniqbot`, свой compose; `bot` работает под uid/gid 101, `cpu_shares: 256`, `mem_limit: 1536m`; `bot-api` `mem_limit: 256m`. `/opt/reels` не трогать. SSH и деплой — только после явного «да» владельца.
- Коммиты заканчиваются строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Видео с iPhone в HDR (HLG), часто ещё и с пометкой поворота: ожидается обычная вертикальная SDR-копия, а не отказ preflight движка. Тесты: Task 2, случаи `hlg` и `hlg_rotated`.
2. Видео с телефона, записанное горизонтальным кадром с пометкой поворота на 90°: копия должна стоять вертикально, без размытых полос. Тест: Task 2, `test_normalize_bakes_rotation` и случай `rotated`.
3. MOV с дорожкой таймкода и PCM-звуком (камеры, монтажки): движок отклоняет служебные дорожки в MP4, ожидается обычная копия. Тест: Task 2, случай `mov_tmcd_pcm`.
4. Видео без звука (запись экрана): ожидается копия без звука, а не ошибка. Тест: Task 2, случай `silent`.
5. Человек заблокировал бота, пока его видео ждало в очереди: результат не доставить, пользователь помечается `blocked`, воркер идёт дальше. Тест: Task 5, `test_user_blocked_bot_before_result`.

---

### Task 1: Каркас, настройки, генератор профиля

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `NOTICE`, `config/uniq.yaml`
- Create: `src/uniqbot/__init__.py`, `src/uniqbot/config.py`, `src/uniqbot/profile.py`
- Test: `tests/conftest.py`, `tests/test_config.py`, `tests/test_profile.py`

**Interfaces:**
- Produces:
  - `uniqbot.config.PROJECT_ROOT: Path`; `Settings` (pydantic-settings, `env_file=PROJECT_ROOT/.env`, `extra="ignore"`) с полями `bot_token: SecretStr | None`, `bot_api_url: str | None` (None — облачный Bot API, только для разработки), `channel_id: str = ""` (`-100…` или `@username`), `channel_url: str = ""`, `admin_ids: str = ""`, `max_file_mb: int = 200`, `max_duration_sec: int = 180`, `render_timeout_sec: int = 900`, `data_dir: Path` (относительный путь якорится к `PROJECT_ROOT`), `uniq_config: Path = PROJECT_ROOT/"config"/"uniq.yaml"`, `tz: str = "Europe/Moscow"`; свойства `admin_id_list -> list[int]`, `db_path` (`data_dir/"bot.db"`), `work_dir` (`data_dir/"work"`); метод `ensure_dirs()`.
  - `uniqbot.profile`: `Span(low, high)`; `UniqRanges` (`crop_max_strength: float`, `brightness/contrast/gamma/saturation: Span`, `noise: list[int]`, `rates: list[float]`, `max_output_sec: float`, `extra="forbid"`); `load_ranges(path: Path) -> UniqRanges`; `OrderParams` (frozen dataclass: `crop_max_strength, brightness, contrast, gamma, saturation: float`, `noise: int`, `rate: float`; `to_dict() -> dict`); `pick_params(rng: random.Random, ranges: UniqRanges, duration_sec: float) -> OrderParams`; `base_profile_path() -> Path`; `build_profile(params: OrderParams) -> dict`; `write_profile(params: OrderParams, path: Path) -> None`.

- [ ] **Step 1: Окружение и каркас**

`pyproject.toml`: имя `uniqbot`, `requires-python = ">=3.12"`, зависимости `aiogram>=3.31,<4`, `sqlalchemy[asyncio]>=2.0.30`, `aiosqlite>=0.20`, `pydantic-settings>=2.4`, `pyyaml>=6.0`, движок по ссылке из Global Constraints; extra `dev = ["pytest>=8", "pytest-asyncio>=0.24"]`; `[tool.pytest.ini_options]` `testpaths=["tests"]`, `asyncio_mode="auto"`, маркер `engine: runs ffmpeg and video-uniquifier`. `.gitignore`: `.venv/`, `.env`, `data/`, `__pycache__/`, `scripts/server.env`. `.env.example` — все ключи из спеки §7 с пустыми значениями и комментарием по-русски. `NOTICE` — «Использует video-uniquifier (https://github.com/Hostlife22/video-uniquifier), MIT» + полный текст MIT с `Copyright (c) 2026 Hostlife22`.

`config/uniq.yaml` (шапка по-русски: «Диапазоны профиля заказа, спека §4; основа — instagram_reels.yaml движка video-uniquifier (MIT, см. NOTICE)»):

```yaml
crop_max_strength: 0.10
brightness: {low: -0.02, high: 0.02}
contrast:   {low: 1.01, high: 1.04}
gamma:      {low: 0.98, high: 1.02}
saturation: {low: 0.97, high: 1.06}
noise: [4, 5]
rates: [0.96, 0.97, 1.03, 1.04]
max_output_sec: 180
```

Run: `py -3.12 -m venv .venv` → `.venv\Scripts\python -m pip install -e .[dev]` → `.venv\Scripts\python -m pip check`
Expected: установка без ошибок, `No broken requirements found.`

- [ ] **Step 2: Тесты**

`tests/conftest.py`: autouse-фикстура `monkeypatch.setitem(Settings.model_config, "env_file", None)`.

```python
# tests/test_config.py
def test_defaults_and_admin_list(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "1, 22,333")
    s = Settings()
    assert s.admin_id_list == [1, 22, 333]
    assert (s.max_file_mb, s.max_duration_sec, s.render_timeout_sec) == (200, 180, 900)
    assert s.db_path == s.data_dir / "bot.db" and s.work_dir == s.data_dir / "work"

def test_relative_data_dir_is_anchored(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "data")
    assert Settings().data_dir == PROJECT_ROOT / "data"
```

```python
# tests/test_profile.py
RANGES = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")
ORDER = ["video.fit_aspect", "video.crop_resize", "video.color_eq", "video.noise", "video.speed",
         "audio.pitch_tempo", "audio.eq", "audio.loudnorm"]

def test_repo_config_values():
    assert RANGES.rates == [0.96, 0.97, 1.03, 1.04]
    assert (RANGES.crop_max_strength, RANGES.noise, RANGES.max_output_sec) == (0.10, [4, 5], 180)

def test_pick_params_stay_in_ranges():
    for seed in range(200):
        p = pick_params(random.Random(seed), RANGES, duration_sec=10)
        assert RANGES.brightness.low <= p.brightness <= RANGES.brightness.high
        # same for contrast, gamma, saturation
        assert p.noise in (4, 5) and p.rate in RANGES.rates and p.crop_max_strength == 0.10

def test_all_rates_used_for_short_clips():
    assert {pick_params(random.Random(s), RANGES, 10).rate for s in range(200)} == set(RANGES.rates)

def test_slow_rates_never_push_output_over_reels_limit():
    rates_174 = {pick_params(random.Random(s), RANGES, 174).rate for s in range(300)}
    assert 0.96 not in rates_174 and 0.97 in rates_174          # 174/0.96 > 180, 174/0.97 <= 180
    assert {pick_params(random.Random(s), RANGES, 180).rate for s in range(200)} <= {1.03, 1.04}
    assert {pick_params(random.Random(s), RANGES, 200).rate for s in range(50)} == {1.04}  # none fits -> max

def test_build_profile_keeps_instagram_reels_base():
    base = yaml.safe_load(base_profile_path().read_text(encoding="utf-8"))
    prof = build_profile(pick_params(random.Random(1), RANGES, 10))
    by_id = {t["id"]: t for t in prof["transforms"]}
    assert by_id["video.fit_aspect"] == next(t for t in base["transforms"] if t["id"] == "video.fit_aspect")
    assert prof["target_loudness_lufs"] == -16.0 and "audio.eq" in by_id

def test_build_profile_applies_params():
    p = OrderParams(0.1, 0.01, 1.02, 0.99, 1.03, 5, 1.03)
    prof = build_profile(p)
    by_id = {t["id"]: t["params"] for t in prof["transforms"] if "params" in t}
    assert [t["id"] for t in prof["transforms"]] == ORDER
    assert by_id["video.crop_resize"] == {"max_strength": 0.1}
    assert by_id["video.color_eq"] == {"brightness": 0.01, "contrast": 1.02, "gamma": 0.99, "saturation": 1.03}
    assert by_id["video.noise"] == {"strength": 5}
    assert by_id["video.speed"] == {"rate": 1.03}
    assert by_id["audio.pitch_tempo"] == {"pitch": 1.03, "tempo": 1.03}
    assert "seed" not in prof and prof.get("seed_strategy", "per_run") == "per_run"

def test_written_profile_loads_in_engine(tmp_path):
    from video_uniquifier.core.profile_loader import load_profile
    path = tmp_path / "profile.yaml"
    write_profile(pick_params(random.Random(3), RANGES, 10), path)
    assert [t.id for t in load_profile(path).transforms] == ORDER
```

- [ ] **Step 3: Run tests — FAIL** (`ModuleNotFoundError: uniqbot.config` / `uniqbot.profile`)

Run: `.venv\Scripts\python -m pytest tests/test_config.py tests/test_profile.py -v`

- [ ] **Step 4: Реализация `config.py` и `profile.py`**

`pick_params`: значения `round(rng.uniform(low, high), 4)`, `noise = rng.choice(...)`; `rate = rng.choice([r for r in rates if duration_sec / r <= max_output_sec] or [max(rates)])`. `build_profile`: загрузить базовый YAML, `name = "uniqbot_order"`, убрать `seed`, заменить `params` у `video.crop_resize` (без `rng_seed`), `video.color_eq`, `video.noise`, `audio.pitch_tempo`; вставить `{"id": "video.speed", "enabled": True, "params": {"rate": r}}` сразу после последнего `video.*`. Если в базовом профиле нет ожидаемого transform — `ValueError` с его id.

- [ ] **Step 5: Run tests — PASS** (та же команда)

- [ ] **Step 6: Commit** — `feat: project skeleton, settings and order profile generator`

---

### Task 2: Подготовка входа и запуск движка

**Files:**
- Create: `src/uniqbot/engine.py`, `src/uniqbot/__main__.py` (пока только `render`)
- Test: `tests/media.py` (генерация роликов), `tests/test_engine.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `UniqRanges`, `OrderParams`, `pick_params`, `write_profile` (Task 1).
- Produces (`uniqbot.engine`):
  - `EngineError(RuntimeError)`; `NotAVideo(EngineError)`; `TooLong(EngineError)`.
  - `SourceInfo` (frozen dataclass): `duration: float, width: int, height: int, has_audio: bool, rotated: bool, hdr: bool`.
  - `async probe_source(path: Path) -> SourceInfo` — ffprobe JSON; видеопоток = первый `codec_type == "video"` без `disposition.attached_pic`; `rotated` = в `side_data_list` есть ненулевой `rotation`; `hdr` = `color_transfer in {"smpte2084", "arib-std-b67"}`; нет видеопотока или ffprobe упал → `NotAVideo`.
  - `async normalize(src: Path, dest: Path, info: SourceInfo) -> None` (dest — `.mkv`).
  - `async run_engine(clean: Path, profile: Path, out: Path, work: Path, timeout: float) -> None`.
  - `RenderResult` (frozen dataclass): `output: Path, params: OrderParams, source: SourceInfo`.
  - `async render(src: Path, workdir: Path, *, ranges: UniqRanges, max_duration: float, timeout: float, rng: random.Random) -> RenderResult`.
  - CLI: `python -m uniqbot render IN OUT [--seed N]` — печатает `params.to_dict()` как JSON.

- [ ] **Step 1: Генератор роликов `tests/media.py`**

`make_clip(kind: str, dest_dir: Path) -> Path` — 2 с, `testsrc2` + `sine=frequency=440`, через ffmpeg:
`vertical` 180×320 h264+aac `.mp4`; `horizontal` 320×180; `silent` 180×320 без звука; `hlg` 180×320 `libx265 -pix_fmt yuv420p10le -color_primaries bt2020 -color_trc arib-std-b67 -colorspace bt2020nc -x265-params log-level=error`; `rotated` — сначала 320×180, затем `ffmpeg -display_rotation 90 -i tmp.mp4 -c copy out.mp4`; `hlg_rotated` — то же от `hlg`-кадра 320×180; `mov_tmcd_pcm` 180×320 h264 + `pcm_s16le` + `-timecode 00:00:00:00` `.mov`; `audio_only` — только `sine`, aac `.m4a`. В `conftest.py` фикстура `clips` (session, словарь `kind -> Path`) и скип: нет `ffmpeg` в PATH или не импортируется `video_uniquifier` → `pytest.skip`. В тестах `RANGES = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")`.

- [ ] **Step 2: Тесты (все с `@pytest.mark.engine`)**

```python
async def test_probe_reads_vertical_clip(clips):
    info = await probe_source(clips["vertical"])
    assert abs(info.duration - 2) < 0.1 and (info.width, info.height) == (180, 320)
    assert info.has_audio and not info.rotated and not info.hdr

async def test_probe_flags_rotation_and_hdr(clips):
    assert (await probe_source(clips["rotated"])).rotated
    assert (await probe_source(clips["hlg"])).hdr

async def test_probe_rejects_non_video(tmp_path, clips):
    fake = tmp_path / "fake.mp4"
    fake.write_text("hello", encoding="utf-8")
    for path in (clips["audio_only"], fake):
        with pytest.raises(NotAVideo):
            await probe_source(path)

async def test_normalize_copies_a_plain_clip(clips, tmp_path):        # 1 video h264 + 1 audio, nothing else
async def test_normalize_bakes_rotation(clips, tmp_path):             # out: width 180, height 320, rotated False
async def test_normalize_tonemaps_hdr(clips, tmp_path):               # out: color_transfer not HDR, pix_fmt yuv420p
async def test_normalize_drops_timecode_and_keeps_pcm(clips, tmp_path):  # streams == video + pcm_s16le audio only

@pytest.mark.parametrize("kind", ["vertical", "horizontal", "silent", "hlg", "rotated", "hlg_rotated", "mov_tmcd_pcm"])
async def test_render_end_to_end(kind, clips, tmp_path):
    res = await render(clips[kind], tmp_path / "w", ranges=RANGES, max_duration=180, timeout=300, rng=random.Random(7))
    out = await probe_source(res.output)
    assert (out.width, out.height) == (1080, 1920) and not out.hdr and not out.rotated
    assert out.has_audio == (kind != "silent")
    assert abs(out.duration - 2 / res.params.rate) < 0.15

async def test_render_rejects_too_long(clips, tmp_path):
    with pytest.raises(TooLong):
        await render(clips["vertical"], tmp_path / "w", ranges=RANGES, max_duration=1, timeout=300, rng=random.Random(1))

async def test_run_engine_failure_carries_stderr(clips, tmp_path):
    bad = tmp_path / "bad.yaml"; bad.write_text("name: x\ntransforms: [{id: video.nope}]\n", encoding="utf-8")
    with pytest.raises(EngineError) as e:
        await run_engine(clips["vertical"], bad, tmp_path / "o.mp4", tmp_path / "t", timeout=120)
    assert str(e.value).strip()
```

`tests/test_cli.py` (`engine`): `python -m uniqbot render <vertical> <tmp>/out.mp4 --seed 5` через `subprocess.run([sys.executable, ...])` → код 0, файл есть, `json.loads(stdout)["rate"] in RANGES.rates`.

- [ ] **Step 3: Run tests — FAIL** — `.venv\Scripts\python -m pytest tests/test_engine.py tests/test_cli.py -v`

- [ ] **Step 4: Реализация**

Общий запуск процессов `_run(cmd, timeout) -> bytes` как в `reels/ffmpeg.py` Уникализатора (kill по таймауту, `EngineError` с последними 2000 символами stderr).

`normalize`: общие аргументы `-map 0:V:0 -map 0:a:0? -sn -dn -map_metadata -1 -map_chapters -1`. Если `not (info.rotated or info.hdr)` → `-c copy`. Иначе `-c:v libx264 -preset veryfast -crf 14 -c:a copy` и `-vf`: для HDR — фильтр `video.tonemap_sdr` движка с его значениями по умолчанию (код не импортировать, строку повторить с комментарием-ссылкой на `video_uniquifier/core/transforms/video_tonemap.py`):

```
zscale=t=linear:npl=1000,tonemap=hable:desat=0.0000:peak=10.0000,zscale=t=bt709:m=bt709:p=bt709:r=tv,format=yuv420p
```

для не-HDR — `format=yuv420p`. Поворот применяет автоповорот ffmpeg при декодировании.

`run_engine`: `[*nice, sys.executable, "-m", "video_uniquifier", "run", clean, "--profile", profile, "--out", out, "--work-dir", work, "--workers", "1", "--no-qa", "--no-progress"]`, `nice = ["nice", "-n", "10"]` если `shutil.which("nice")`, иначе пусто.

`render`: `probe_source` → `TooLong`, если `duration > max_duration` → `normalize` в `workdir/"clean.mkv"` → `pick_params(rng, ranges, info.duration)` → `write_profile(..., workdir/"profile.yaml")` → `run_engine(..., out=workdir/"output.mp4", work=workdir/"tmp")`.

Если preflight движка отклоняет какой-то синтетический случай — разобраться по его сообщению и чинить подготовку входа, а не отключать preflight (`--no-preflight` запрещён).

- [ ] **Step 5: Run tests — PASS** (та же команда)
- [ ] **Step 6: Commit** — `feat: input normalization and video-uniquifier runner`

---

### Task 3: База данных

**Files:**
- Create: `src/uniqbot/db.py`, `src/uniqbot/repo.py`
- Test: `tests/test_repo.py`

**Interfaces:**
- Produces (`uniqbot.db`): `utcnow()`, `UTCDateTime` (как в Уникализаторе: в SQLite naive UTC, наружу aware UTC), `Base`; `OrderStatus(StrEnum)`: `queued/processing/done/failed`; `BroadcastStatus(StrEnum)`: `running/done/cancelled/interrupted`; модели `User`, `Order`, `Broadcast` с полями из спеки §5 (у `Order` дополнительно `mime_type: str | None`, `file_name: str | None` — для расширения входного файла); `make_engine(db_path: Path) -> AsyncEngine` (WAL), `async init_db(engine)` (`create_all`), `make_sessions(engine) -> async_sessionmaker`.
- Produces (`uniqbot.repo`, все функции первым аргументом берут `sessions`, время — необязательный `now`):
  - `TgProfile` (dataclass): `tg_id, username, first_name, last_name, language_code, is_premium`.
  - `parse_source(payload: str | None) -> str | None` — `[A-Za-z0-9_-]{1,64}`, иначе `None`.
  - `touch_user(sessions, p: TgProfile, now=None)` — upsert, обновляет профиль и `last_seen`, ставит `blocked=False`.
  - `set_source_if_empty(sessions, tg_id, source: str | None)`.
  - `set_subscription(sessions, tg_id, subscribed: bool, now=None)`; `set_blocked(sessions, tg_id, blocked: bool, now=None)` (неизвестный пользователь — ничего не делает).
  - `active_order(sessions, tg_id) -> Order | None`.
  - `create_order(sessions, *, tg_id, file_id, file_size, duration, mime_type, file_name) -> tuple[int, int]` — `(order_id, position)`, `position` = число `queued` с меньшим id + 1, плюс 1, если есть `processing`.
  - `set_status_message(sessions, order_id, message_id)`; `claim_next(sessions, now=None) -> Order | None` (старейший `queued` → `processing`, `started_at`); `finish_order(sessions, order_id, status, *, error=None, params: dict | None = None, now=None)` (`done` → `users.videos_done += 1`; `error` обрезается до 2000); `requeue_processing(sessions) -> int`; `active_order_ids(sessions) -> set[int]`.
  - `Stats` (dataclass): `users_total, users_new_day, users_new_week, users_active_week, users_subscribed, users_blocked, done_day, done_total, failed_day, queued: int, processing: bool`; `stats(sessions, now) -> Stats` (сутки и неделя считаются от `now` назад).
  - `all_users(sessions) -> list[User]` (по `first_seen`); `broadcast_audience(sessions) -> list[int]` (`blocked = false`).
  - `create_broadcast(sessions, *, admin_id, source_chat_id, source_message_id, total, status) -> int`; `update_broadcast(sessions, bid, *, sent, blocked, failed, status=None, finished=False)`; `interrupt_running_broadcasts(sessions) -> int`.

- [ ] **Step 1: Тесты** (фикстура `sessions` на `tmp_path/"t.db"`)

- `test_touch_user_upserts_and_refreshes_profile` — второй `touch_user` с новым username меняет его и `last_seen`, `first_seen` прежний.
- `test_source_only_first_label` — `set_source_if_empty(..., "reels")`, затем `"stories"` → `"reels"`; `parse_source("bad label!") is None`, `parse_source("a"*65) is None`, `parse_source("ig_reels-1") == "ig_reels-1"`.
- `test_blocked_flag` — `set_blocked(True)` ставит `blocked_at`; `touch_user` снимает флаг; `set_blocked` для неизвестного id не падает.
- `test_queue_positions` — заказы A, B → позиции 1, 2; `claim_next` → A `processing`; новый C → позиция 3 (B + обрабатываемый A + 1).
- `test_one_active_order` — `active_order` видит `queued` и `processing`, не видит `done`/`failed`.
- `test_finish_done_counts_video_and_failed_keeps_error` — `done` → `videos_done == 1`, `params_json` сохранён; `failed` с ошибкой 3000 символов → длина 2000.
- `test_requeue_processing` — `processing` → `queued`, возвращает 1.
- `test_stats_windows` — пользователи с `first_seen` 2 ч, 3 дня, 10 дней назад: `users_new_day == 1`, `users_new_week == 2`; плюс подписанные, заблокированные, `done_day`, `failed_day`, `queued`, `processing`.
- `test_broadcast_rows` — `create_broadcast(status=running)` → `interrupt_running_broadcasts == 1` и статус `interrupted`; `broadcast_audience` без заблокированных.

- [ ] **Step 2: Run — FAIL** — `.venv\Scripts\python -m pytest tests/test_repo.py -v`
- [ ] **Step 3: Реализация `db.py`, `repo.py`**
- [ ] **Step 4: Run — PASS**
- [ ] **Step 5: Commit** — `feat: users, orders and broadcasts storage`

---

### Task 4: Подписка и приём видео

**Files:**
- Create: `src/uniqbot/deps.py`, `src/uniqbot/texts.py`, `src/uniqbot/access.py`, `src/uniqbot/intake.py`, `src/uniqbot/middleware.py`
- Test: `tests/fakes.py`, `tests/test_access.py`, `tests/test_intake.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `repo.*` (Task 3).
- Produces:
  - `uniqbot.deps.Deps` (dataclass): `settings: Settings, sessions, ranges: UniqRanges, wake: asyncio.Event, alerts: AdminAlerts, broadcast_task: asyncio.Task | None = None`.
  - `uniqbot.access`: `is_member(member: ChatMember) -> bool`; `SubscriptionCheckFailed(Exception)`; `async check_subscription(bot, channel_id: str, user_id: int) -> bool` (`TelegramAPIError` → `SubscriptionCheckFailed`); `AdminAlerts(bot, admin_ids: list[int], *, clock=time.monotonic)` с `async alert(key: str, text: str, *, every: float = 600)` (не чаще раза в `every` секунд на ключ; ошибки отправки глотает и логирует) и `async notify(text: str)` (без ограничения).
  - `uniqbot.intake.build_router() -> Router`; обработчики — обычные async-функции `(message|callback, bot, deps)`, их тесты вызывают напрямую: `on_start`, `on_check`, `on_video`, `on_privacy`, `on_other`, `on_my_chat_member`.
  - `uniqbot.middleware.TrackUser` — outer-middleware на `update`: для `event_from_user` (не бот) вызывает `touch_user`.
  - `uniqbot.texts` — строки ниже; `welcome(settings)`, `too_big(settings)`, `too_long(settings)`, `accepted(position)` — форматтеры.

Тексты (`texts.py`, ровно так):

```python
WELCOME = "Пришли видео до {max_mb} МБ и не длиннее {max_min} мин — верну уникальную копию файлом."
NOT_SUBSCRIBED = "Бот бесплатный для подписчиков канала. Подпишись и нажми «Я подписался»."
BTN_SUBSCRIBE = "Подписаться"
BTN_CHECK = "Я подписался"
STILL_NOT_SUBSCRIBED = "Подписка пока не видна. Подпишись и нажми ещё раз."
CHECK_FAILED = "Временная ошибка, попробуй через пару минут."
SEND_VIDEO_HINT = "Пришли обычное видео файлом или как видео."
TOO_BIG = "Файл больше {max_mb} МБ."
TOO_LONG = "Видео длиннее {max_min} мин."
HAS_ACTIVE = "У тебя уже есть видео в работе, дождись результата."
ACCEPTED = "Принял! Ты в очереди: {position}-й. Результат пришлю сюда."
PROCESSING = "Обрабатываю…"
DONE_STATUS = "Готово ✅"
DONE_CAPTION = "Готово ✅ Можно выкладывать"
FAILED = "Не получилось обработать это видео, пришли другой файл."
NOT_A_VIDEO = "В файле не нашлось видео. Пришли обычное видео."
PRIVACY = ("Бот хранит твой Telegram ID, имя, @username, язык, дату первого и последнего визита, метку ссылки, "
           "по которой ты пришёл, статус подписки на канал и число обработанных видео. Это нужно, чтобы "
           "пускать подписчиков, считать статистику и присылать новости бота. Видео удаляются сразу после "
           "обработки.")
```

`max_min = max_duration_sec // 60`.

- [ ] **Step 1: Фейки `tests/fakes.py`**

`FakeBot` записывает вызовы `send_message`, `send_document`, `edit_message_text`, `copy_message`, `answer_callback_query` в `self.calls: list[tuple[str, dict]]`; `get_chat_member` возвращает объект со статусом из `self.member_status` (или бросает `self.member_error`); `get_file` возвращает `SimpleNamespace(file_path=...)`. `FakeMessage`/`FakeCallback` — минимальные объекты с `from_user`, `chat.id`, `video`/`document`/`text`, `message_id`, `answer()` (пишет в bot.calls через `send_message`). Builder `make_deps(tmp_path, **settings)`.

- [ ] **Step 2: Тесты**

- `test_member_statuses` — `creator/administrator/member` → True; `restricted` с `is_member=True` → True; `restricted` с `is_member=False`, `left`, `kicked` → False.
- `test_check_subscription_wraps_api_errors` — `TelegramBadRequest` → `SubscriptionCheckFailed`.
- `test_alerts_rate_limited` — два `alert("sub", ...)` подряд → по одному сообщению каждому админу; через 601 с (поддельные часы) — снова.
- `test_start_records_first_label` — `/start reels`, потом `/start stories` → `source == "reels"`.
- `test_start_not_subscribed_shows_buttons` — ответ `NOT_SUBSCRIBED`; клавиатура: url-кнопка `BTN_SUBSCRIBE` на `CHANNEL_URL` и callback-кнопка `BTN_CHECK` с `callback_data == "check_sub"`; `is_subscribed is False` в базе.
- `test_start_subscribed_shows_welcome` — текст `welcome(settings)`.
- `test_check_button` — подписан → сообщение отредактировано в `welcome`; не подписан → `answer_callback_query(text=STILL_NOT_SUBSCRIBED, show_alert=True)`.
- `test_check_failure_tells_user_and_alerts_admins` — `get_chat_member` бросает → пользователю `CHECK_FAILED`, админам одно сообщение, заказ не создан.
- `test_video_checks_in_order` — по очереди: не подписан → `NOT_SUBSCRIBED`; `file_size = 201 MiB` → `too_big`; `duration = 181` → `too_long`; уже есть `queued` → `HAS_ACTIVE`; нормальное видео → `accepted(1)`, заказ с `file_id`, `status_message_id` сохранён, `deps.wake.is_set()`.
- `test_second_video_of_an_album_is_refused` — два видео подряд от одного человека → второе `HAS_ACTIVE`, заказ один.
- `test_document_video_mime_accepted_other_documents_hint` — `video/quicktime` принят (`duration is None`); `application/pdf` → `SEND_VIDEO_HINT`.
- `test_other_messages_get_hint_but_commands_are_silent` — текст «привет» → `SEND_VIDEO_HINT`; `/stats` от не-админа → никаких вызовов.
- `test_my_chat_member_tracks_blocking` — статус `kicked` → `blocked`; `member` → не `blocked`.
- `test_track_user_middleware_refreshes_profile` — апдейт с новым username обновляет запись.
- `test_privacy` — ответ `PRIVACY`.

- [ ] **Step 3: Run — FAIL** — `.venv\Scripts\python -m pytest tests/test_access.py tests/test_intake.py -v`
- [ ] **Step 4: Реализация**

Порядок проверок в `on_video` — как в спеке §2.2. `file_size` в МиБ: `> max_file_mb * 1024 * 1024`. Роутер: `CommandStart` (с `command.args`), `F.data == "check_sub"`, `F.video | F.document`, `Command("privacy")`, `my_chat_member` в личке, последним — `on_other` для любых личных сообщений, который молчит, если текст начинается с `/`. Подписка проверяется в `on_start`, `on_check`, `on_video` и пишется через `set_subscription`.

- [ ] **Step 5: Run — PASS**
- [ ] **Step 6: Commit** — `feat: subscription gate and video intake`

---

### Task 5: Воркер очереди

**Files:**
- Create: `src/uniqbot/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `render`, `EngineError`, `NotAVideo`, `TooLong`, `RenderResult` (Task 2); `repo.claim_next/finish_order/set_blocked/requeue_processing/active_order_ids` (Task 3); `Deps`, `texts` (Task 4).
- Produces:
  - `FileFetcher` (Protocol): `async fetch(bot, file_id: str, dest_dir: Path, suffix: str) -> Path`.
  - `LocalFetcher` — `bot.get_file` → `Path(file.file_path)` на общем томе → `shutil.move` в `dest_dir/f"input{suffix}"`; `CloudFetcher` — `bot.download(file_id, destination=...)`. `make_fetcher(settings) -> FileFetcher` (по `bot_api_url`).
  - `input_suffix(mime_type: str | None, file_name: str | None) -> str` — расширение из имени файла, иначе `.mp4`.
  - `Worker(bot, deps, *, fetcher: FileFetcher, renderer=render, rng_factory=random.Random)` с `async run_forever()` (берёт `claim_next`; если пусто — ждёт `deps.wake` до 30 с, потом `clear()`) и `async process(order: Order) -> None`.
  - `async recover(deps) -> None` — `requeue_processing`, удаление `work_dir/*`, чьё имя не в `active_order_ids`.

- [ ] **Step 1: Тесты** (поддельные `renderer` и `fetcher`; `renderer` создаёт `workdir/"output.mp4"` и возвращает `RenderResult`)

- `test_worker_processes_queue_in_order` — два заказа разных людей → обработаны по возрастанию id; каждому: правка статуса на `PROCESSING`, `send_document` с подписью `DONE_CAPTION` и именем файла по `r"uniq_[0-9a-f]{8}\.mp4"`, правка статуса на `DONE_STATUS`; заказы `done`, `params_json` = `params.to_dict()`, `videos_done == 1`; каталоги `work/<id>` удалены.
- `test_engine_failure_marks_failed_and_alerts` — `EngineError("boom")` → пользователю `FAILED`, заказ `failed`, в `error` есть «boom», админам сообщение с номером заказа; следующий заказ обработан.
- `test_user_errors_do_not_alert_admins` — `NotAVideo` → `NOT_A_VIDEO`, `TooLong` → `too_long(settings)`; админам ничего.
- `test_user_blocked_bot_before_result` — `send_document` бросает `TelegramForbiddenError` → `users.blocked`, заказ `failed` с `error == "blocked"`, следующий заказ обработан.
- `test_status_message_gone_does_not_fail_order` — `edit_message_text` бросает `TelegramBadRequest` → заказ всё равно `done`.
- `test_recover_requeues_and_removes_orphans` — заказ `processing` → `queued`; `work/999` удалён, `work/<id активного>` на месте.
- `test_local_fetcher_moves_file` — файл «сервера» в `tmp/server/videos/file_0.mp4` перенесён в `dest/input.mp4`, исходника нет.
- `test_input_suffix` — `("video/quicktime", "IMG_1.MOV") -> ".MOV"`, `(None, None) -> ".mp4"`.

- [ ] **Step 2: Run — FAIL** — `.venv\Scripts\python -m pytest tests/test_worker.py -v`
- [ ] **Step 3: Реализация**

`process` по шагам спеки §2.3. `renderer(src, workdir, ranges=deps.ranges, max_duration=settings.max_duration_sec, timeout=settings.render_timeout_sec, rng=rng_factory())`. Отправка — `FSInputFile(output, filename=f"uniq_{secrets.token_hex(4)}.mp4")`. Ошибки правки статус-сообщения логируются и игнорируются. `finally: shutil.rmtree(workdir, ignore_errors=True)`. Исключение не из `EngineError` — тоже `FAILED` + уведомление админам (`deps.alerts.notify`) и запись в лог со стеком; воркер не падает.

- [ ] **Step 4: Run — PASS**
- [ ] **Step 5: Commit** — `feat: render queue worker`

---

### Task 6: Админ-команды и рассылка

**Files:**
- Create: `src/uniqbot/export.py`, `src/uniqbot/broadcast.py`, `src/uniqbot/admin.py`
- Modify: `src/uniqbot/texts.py` (тексты админки)
- Test: `tests/test_export.py`, `tests/test_broadcast.py`, `tests/test_admin.py`

**Interfaces:**
- Consumes: `repo.stats/all_users/broadcast_audience/create_broadcast/update_broadcast/set_blocked` (Task 3); `Deps`, фейки (Task 4).
- Produces:
  - `export.users_csv(users: list[User], tz: ZoneInfo) -> bytes` — UTF-8 с BOM, разделитель `;`, колонки в порядке полей `users` из спеки §5; булевы `да`/`нет`; `None` → пусто; время в `tz` как `%Y-%m-%d %H:%M`.
  - `broadcast.BroadcastResult(sent, blocked, failed)`; `async send_broadcast(bot, sessions, broadcast_id: int, *, chat_id: int, message_id: int, reply_markup, audience: list[int], rate_per_sec: float = 20, on_progress=None, sleep=asyncio.sleep) -> BroadcastResult`.
  - `admin.build_router() -> Router`; `is_admin(user_id, deps) -> bool`; обработчики `on_stats`, `on_users`, `on_broadcast`, `on_broadcast_post`, `on_broadcast_go`, `on_broadcast_cancel`, `on_cancel` (не-админам ничего не отвечают).

Тексты админки (в `texts.py`):

```python
STATS = ("👥 Пользователей: {users_total} (новых за сутки {users_new_day}, за 7 дней {users_new_week})\n"
         "Активных за 7 дней: {users_active_week}\nПодписаны: {users_subscribed}\n"
         "Заблокировали бота: {users_blocked}\n🎬 Видео: за сутки {done_day}, всего {done_total}; "
         "ошибок за сутки {failed_day}\nОчередь: {queued}{processing_mark}")   # processing_mark = " + 1 в работе" | ""
BC_ASK_POST = "Пришли пост для рассылки. /cancel — отмена."
BC_CONFIRM = "Отправить {n} пользователям?"
BC_GO, BC_CANCEL = "Отправить", "Отмена"
BC_CANCELLED = "Рассылка отменена."
BC_BUSY = "Рассылка уже идёт, дождись отчёта."
BC_PROGRESS = "Рассылка: {done} из {total}…"
BC_REPORT = "Рассылка завершена. Доставлено: {sent}, заблокировали бота: {blocked}, ошибок: {failed}."
```

- [ ] **Step 1: Тесты**

- `test_users_csv_format` — начинается с `"﻿"`; заголовок `tg_id;username;first_name;last_name;language_code;is_premium;source;first_seen;last_seen;is_subscribed;subscription_checked_at;videos_done;blocked;blocked_at`; булевы `да`/`нет`; `first_seen` 2026-10-23 09:00 UTC → `2026-10-23 12:00` в Europe/Moscow; `None` → пустая ячейка; `;` внутри имени экранируется кавычками (модуль `csv`).
- `test_broadcast_paces_and_uses_copy_message` — 2 адресата → `copy_message(chat_id=u, from_chat_id=chat_id, message_id=message_id, reply_markup=markup)` для каждого; `sleep` вызывается с `0.05` между отправками.
- `test_broadcast_retry_after` — первый вызов бросает `TelegramRetryAfter(retry_after=3)` → `sleep(3)` и повтор того же адресата → `sent == 2`.
- `test_broadcast_forbidden_marks_blocked` — `TelegramForbiddenError` → `blocked == 1`, пользователь `blocked` в базе; прочая `TelegramBadRequest` → `failed == 1`.
- `test_broadcast_progress_and_row` — 250 адресатов → `on_progress` вызван с `(100, 250)`, `(200, 250)`, `(250, 250)`; строка `broadcasts` после 100 и 200 содержит текущие счётчики, в конце `status == done`, `finished_at` есть.
- `test_stats_command` — админ получает `STATS` с числами из `repo.stats`; не-админ — ничего.
- `test_users_command_sends_csv` — `send_document` с именем `users_<YYYY-MM-DD>.csv`.
- `test_broadcast_flow` — `/broadcast` → `BC_ASK_POST`, состояние ожидания; пост → `copy_message` админу (превью) и `BC_CONFIRM` с n = числом незаблокированных и кнопками `bc:go` / `bc:cancel`; `bc:cancel` → строка `cancelled`, `BC_CANCELLED`; `/cancel` в ожидании поста → состояние сброшено; `bc:go` при живой `deps.broadcast_task` → `BC_BUSY`.

- [ ] **Step 2: Run — FAIL** — `.venv\Scripts\python -m pytest tests/test_export.py tests/test_broadcast.py tests/test_admin.py -v`
- [ ] **Step 3: Реализация**

FSM — `StatesGroup` `BroadcastForm.waiting_post`; в данных состояния — `chat_id`, `message_id`, `reply_markup` поста. `on_broadcast_go`: `create_broadcast(status=running)`, сообщение-прогресс админу, `deps.broadcast_task = asyncio.create_task(...)`; прогресс правит это сообщение (`BC_PROGRESS`), в конце — `BC_REPORT`. Счётчики пишутся в `broadcasts` при каждом вызове `on_progress`.

- [ ] **Step 4: Run — PASS**
- [ ] **Step 5: Commit** — `feat: admin stats, users export and broadcasts`

---

### Task 7: Сборка приложения и команды запуска

**Files:**
- Create: `src/uniqbot/app.py`
- Modify: `src/uniqbot/__main__.py` (команды `bot`, `logout-cloud`)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: всё из Task 1–6.
- Produces:
  - `build_bot(settings) -> Bot` — при `bot_api_url` сессия `AiohttpSession(api=TelegramAPIServer.from_base(settings.bot_api_url, is_local=True))`, иначе облачная.
  - `build_dispatcher(deps) -> Dispatcher` — `MemoryStorage`, `deps=deps` в данных диспетчера, `TrackUser` на `update`, роутеры: `admin`, затем `intake` (в нём `on_other` последним).
  - `ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]`.
  - `async run_bot(settings) -> None` — `ensure_dirs`, `init_db`, `load_ranges`, `recover(deps)`, `interrupt_running_broadcasts`, задача `Worker.run_forever()`, `set_my_commands` (`/start`, `/privacy`), `start_polling(allowed_updates=ALLOWED_UPDATES)`; падение задачи воркера пишется в лог со стеком.
  - CLI: `python -m uniqbot bot | render IN OUT [--seed N] | logout-cloud` (последняя — один раз перед переходом на локальный сервер: облачный `Bot(token).log_out()`, печатает результат).

- [ ] **Step 1: Тесты**

- `test_dispatcher_builds_twice` — два вызова `build_dispatcher` не падают (роутеры создаются заново).
- `test_build_bot_local_mode` — `bot_api_url="http://bot-api:8081"` → `bot.session.api.is_local is True` и базовый URL указывает на `bot-api:8081`; без него — `is_local is False`.
- `test_allowed_updates` — `ALLOWED_UPDATES == ["message", "callback_query", "my_chat_member"]`.
- `test_main_dispatches_commands` — `monkeypatch` подменяет `run_bot`, `render`, `logout_cloud`; `main(["bot"])`, `main(["render", "a.mp4", "b.mp4", "--seed", "3"])`, `main(["logout-cloud"])` вызывают их с нужными аргументами.

- [ ] **Step 2: Run — FAIL** — `.venv\Scripts\python -m pytest tests/test_app.py -v`
- [ ] **Step 3: Реализация** (логирование — `logging.basicConfig` в stdout, уровень INFO; токен в логи не попадает)
- [ ] **Step 4: Полный прогон** — `.venv\Scripts\python -m pytest -v` → всё PASS (engine-тесты тоже, ffmpeg есть локально)
- [ ] **Step 5: Commit** — `feat: bot wiring and command line`

---

### Task 8: Docker, compose, скрипты деплоя, README

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `README.md`
- Create: `scripts/server-common.ps1`, `scripts/setup-server.ps1`, `scripts/deploy.ps1`, `scripts/server.env.example`
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- Consumes: CLI `python -m uniqbot bot` (Task 7); `config/uniq.yaml`.
- Produces: образ с целями `test` и `runtime`; compose со службами `bot-api` и `bot`; скрипты `setup-server.ps1` (один раз) и `deploy.ps1 [-Test]`.

- [ ] **Step 1: Тест статических файлов**

```python
def test_compose_limits_and_volumes():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    bot, api = c["services"]["bot"], c["services"]["bot-api"]
    assert bot["cpu_shares"] == 256 and bot["mem_limit"] == "1536m" and api["mem_limit"] == "256m"
    assert bot["environment"]["BOT_API_URL"] == "http://bot-api:8081"
    assert api["environment"]["TELEGRAM_LOCAL"] == "1"
    assert "bot-api-data:/var/lib/telegram-bot-api" in bot["volumes"] and "bot-api-data:/var/lib/telegram-bot-api" in api["volumes"]
    assert bot["env_file"] == ["../.env"] and "../data:/app/data" in bot["volumes"]

def test_dockerfile_runs_as_uid_101_and_checks_ffmpeg():
    d = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--uid 101" in d and "USER 101:101" in d
    assert "zscale" in d and "libx264" in d and "libx265" in d

def test_env_example_lists_every_key():
    keys = {l.split("=")[0] for l in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines() if "=" in l and not l.startswith("#")}
    assert keys == {"BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "CHANNEL_ID", "CHANNEL_URL", "ADMIN_IDS",
                    "MAX_FILE_MB", "MAX_DURATION_SEC", "RENDER_TIMEOUT_SEC"}

def test_deploy_ships_only_committed_code():
    s = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    assert "git -C $ProjectRoot status --porcelain" in s and "git archive" in s and "/opt/reels" not in s
```

- [ ] **Step 2: Run — FAIL** — `.venv\Scripts\python -m pytest tests/test_deploy_files.py -v`

- [ ] **Step 3: Файлы**

`Dockerfile`: `FROM python:3.12-slim-trixie AS base`; `apt-get install ffmpeg git ca-certificates tzdata`; проверки сборки `ffmpeg -hide_banner -filters | grep -q zscale`, `ffmpeg -hide_banner -encoders | grep -q libx264`, `... | grep -q libx265`; зависимости из `pyproject` отдельным слоем (как в Уникализаторе), затем `COPY config src`, `pip install --no-deps -e .`; `groupadd --gid 101 uniqbot && useradd --uid 101 --gid 101 --create-home --shell /usr/sbin/nologin uniqbot` (если id 101 в базовом образе занят, сборка упадёт здесь — тогда `--non-unique`); `mkdir -p /app/data && chown 101:101 /app/data`. Цель `test`: `pip install -e .[dev]`, `COPY tests`, `RUN python -m pytest -q` (под пользователем 101). Цель `runtime`: `USER 101:101`, `CMD ["python", "-m", "uniqbot", "bot"]`.

`docker-compose.yml`: как проверяет тест; `bot-api` — `image: aiogram/telegram-bot-api:latest` (в Task 10 заменяется на digest), `env_file: ["../.env"]`, `restart: unless-stopped`; `bot` — `build: {context: ., target: runtime}`, `container_name: uniqbot`, `init: true`, `depends_on: [bot-api]`, `environment: {TZ: Europe/Moscow, DATA_DIR: /app/data, BOT_API_URL: http://bot-api:8081}`, `stop_grace_period: 30s`; у обеих логи `json-file` 10m × 3; `volumes: {bot-api-data: {}}`.

Скрипты — по образцу `scripts/` Уникализатора (`server-common.ps1`: `Read-ServerEnv`, `Invoke-Remote`, `Send-File`, `Protect-Key`) с ключами `UNIQBOT_HOST`, `UNIQBOT_KEY`, `UNIQBOT_DIR` (по умолчанию `/opt/uniqbot`); `server.env.example` — `UNIQBOT_HOST=root@203.0.113.10`, `UNIQBOT_KEY=~/.ssh/reels_vps`, `UNIQBOT_DIR=/opt/uniqbot`. `setup-server.ps1`: создаёт `$dir/data`, `chown 101:101 $dir/data`, загружает локальный `.env` в `$dir/.env` с `chmod 600`. `deploy.ps1 [-Test]`: отказ при грязном дереве, `git archive HEAD` → scp → распаковка в `app.new` → при `-Test` `docker build --target test` → подмена `app` → `docker compose up -d --build --remove-orphans` → через 20 с проверка, что `uniqbot` запущен и `RestartCount` не вырос → хвост логов.

`README.md` по-русски: что это, локальная разработка и тесты, `python -m uniqbot render` для подбора, `.env`, первый запуск на сервере (порядок из Task 10), деплой.

- [ ] **Step 4: Run — PASS**, затем `.venv\Scripts\python -m pytest -v` — всё PASS
- [ ] **Step 5: Commit** — `build: docker image, compose and deploy scripts`

---

### Task 9: Подбор диапазонов на роликах владельца

Нужен владелец: 5–10 его роликов (разные: вертикальные с речью, с музыкой, горизонтальный, с iPhone).

**Files:**
- Modify: `config/uniq.yaml` (только если владелец попросит поменять)

- [ ] **Step 1:** Ролики владельца лежат в `data/tuning/src/` (каталог в `.gitignore`). Для каждого две копии с разными зёрнами: `.venv\Scripts\python -m uniqbot render data/tuning/src/<имя> data/tuning/out/<имя>_s1.mp4 --seed 1` и `--seed 2`; параметры из stdout сохранить.
- [ ] **Step 2:** Похожесть каждой копии на исходник: `.venv\Scripts\python -m video_uniquifier qa data/tuning/src/<имя> data/tuning/out/<имя>_s1.mp4 --no-vmaf --json data/tuning/out/<имя>_s1.qa.json`. Без `fpcalc` в PATH поле Chromaprint будет пустым: поставить `fpcalc` только с разрешения владельца, иначе опираться на замер этапа 7 Уникализатора (Chromaprint расходится при разнице тона ≥ 3%).
- [ ] **Step 3:** Владельцу — таблица: ролик, `rate`, кроп, pHash-похожесть, Chromaprint (если есть), время рендера; копии он смотрит и слушает сам.
- [ ] **Step 4:** Правки диапазонов по его словам → `config/uniq.yaml`; `.venv\Scripts\python -m pytest tests/test_profile.py -v` (тест `test_repo_config_values` обновить под новые значения) → PASS.
- [ ] **Step 5: Commit** — `tune: order profile ranges after the owner's review`

---

### Task 10: Первый запуск на VPS и приёмка (только после «да» владельца)

- [ ] **Step 1:** От владельца: токен нового бота (@BotFather), `api_id`/`api_hash` (my.telegram.org), канал (`CHANNEL_ID` — `@username` или `-100…`, `CHANNEL_URL`), бот добавлен админом канала без прав на публикацию, его Telegram ID для `ADMIN_IDS`. Он сам заполняет локальный `.env` по `.env.example`; значения в чат и в коммиты не попадают.
- [ ] **Step 2:** `.venv\Scripts\python -m uniqbot logout-cloud` → печатает `True` (бот вышел из облачного Bot API; после этого он работает только через локальный сервер).
- [ ] **Step 3:** Заполнить `scripts/server.env` по примеру; `powershell -ExecutionPolicy Bypass -File scripts\setup-server.ps1` → на сервере есть `/opt/uniqbot/.env` (600) и `/opt/uniqbot/data` (101:101).
- [ ] **Step 4:** `scripts\deploy.ps1 -Test` из инструмента PowerShell без `*>`/`2>&1` (как в Уникализаторе) → тестовая цель собрана, `uniqbot` запущен, рестартов нет, в логах «Start polling».
- [ ] **Step 5:** Зафиксировать digest образа: на сервере `docker inspect --format '{{index .RepoDigests 0}}' aiogram/telegram-bot-api:latest` → подставить `aiogram/telegram-bot-api@sha256:…` в `docker-compose.yml`, commit `build: pin telegram-bot-api image`, `scripts\deploy.ps1`.
- [ ] **Step 6:** Приёмка по спеке §9 с владельцем: два аккаунта (подписан / нет), видео как видео и как файл, файл > 20 МБ, видео с iPhone, второй заказ во время первого, `/stats`, `/users`, `/broadcast` на свои аккаунты, блокировка бота одним аккаунтом → `blocked` в `/users`. Во время теста бот рилсов (`docker ps` на сервере) продолжает работать, его `RestartCount` не меняется.
