from uniqbot.config import PROJECT_ROOT, Settings


def test_defaults_and_admin_list(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "1, 22,333")
    s = Settings()
    assert s.admin_id_list == [1, 22, 333]
    assert (s.max_file_mb, s.max_duration_sec, s.render_timeout_sec) == (200, 180, 900)
    assert s.db_path == s.data_dir / "bot.db" and s.work_dir == s.data_dir / "work"


def test_relative_data_dir_is_anchored(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "data")
    assert Settings().data_dir == PROJECT_ROOT / "data"


def test_empty_values_from_env_example_fall_back_to_defaults(monkeypatch):
    for key in ("BOT_TOKEN", "BOT_API_URL", "ADMIN_IDS", "MAX_FILE_MB", "MAX_DURATION_SEC", "RENDER_TIMEOUT_SEC"):
        monkeypatch.setenv(key, "")
    s = Settings()
    assert (s.bot_token, s.bot_api_url, s.admin_id_list) == (None, None, [])
    assert (s.max_file_mb, s.max_duration_sec, s.render_timeout_sec) == (200, 180, 900)


def test_ensure_dirs_creates_data_and_work(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    s = Settings()
    s.ensure_dirs()
    assert s.data_dir.is_dir() and s.work_dir.is_dir()
