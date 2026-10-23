from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# The image's test stage has no Dockerfile/compose: these files are checked in the repo only.
pytestmark = pytest.mark.skipif(not (ROOT / "Dockerfile").exists(), reason="deploy files live in the repo, not the image")

SCRIPTS = ("server-common.ps1", "setup-server.ps1", "deploy.ps1")


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_read("docker-compose.yml"))


def _stage(dockerfile: str, name: str) -> str:
    """The text of the build stage `FROM ... AS <name>`, up to the next FROM."""
    return dockerfile.split(f" AS {name}\n", 1)[1].split("\nFROM ", 1)[0]


def test_secrets_and_data_never_go_into_the_image():
    ignored = {line.strip() for line in _read(".dockerignore").splitlines()}
    assert {".env", "data", ".venv", ".git", "scripts/server.env"} <= ignored


def test_compose_limits_and_volumes():
    c = _compose()
    bot, api = c["services"]["bot"], c["services"]["bot-api"]
    assert bot["cpu_shares"] == 256 and bot["mem_limit"] == "1536m" and api["mem_limit"] == "256m"
    assert bot["environment"]["BOT_API_URL"] == "http://bot-api:8081"
    assert api["environment"]["TELEGRAM_LOCAL"] == "1"
    assert "bot-api-data:/var/lib/telegram-bot-api" in bot["volumes"] and "bot-api-data:/var/lib/telegram-bot-api" in api["volumes"]
    assert bot["env_file"] == ["../.env"] and "../data:/app/data" in bot["volumes"]


def test_compose_is_its_own_project_next_to_the_reels_bot():
    c = _compose()
    # the file lives in <dir>/app, so without a name the project would be called "app" like the reels bot's:
    # `up --remove-orphans` would then remove that bot's containers
    assert c["name"] == "uniqbot"
    bot, api = c["services"]["bot"], c["services"]["bot-api"]
    assert bot["container_name"] == "uniqbot" and bot["build"] == {"context": ".", "target": "runtime"}
    assert bot["environment"] == {"TZ": "Europe/Moscow", "DATA_DIR": "/app/data", "BOT_API_URL": "http://bot-api:8081"}
    assert bot["depends_on"] == ["bot-api"] and bot["init"] is True and bot["stop_grace_period"] == "30s"
    assert api["image"].startswith("aiogram/telegram-bot-api") and api["env_file"] == ["../.env"]
    for service in (bot, api):
        assert service["restart"] == "unless-stopped"
        assert service["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
        assert "ports" not in service  # the Bot API server is reachable from the bot only
    assert c["volumes"] == {"bot-api-data": {}}


def test_dockerfile_runs_as_uid_101_and_checks_ffmpeg():
    d = _read("Dockerfile")
    assert "--uid 101" in d and "USER 101:101" in d
    assert "zscale" in d and "libx264" in d and "libx265" in d


def test_dockerfile_has_a_test_stage_and_a_runtime_stage():
    d = _read("Dockerfile")
    assert "FROM python:3.12-slim-trixie AS base" in d
    # ffmpeg comes from Debian here: the build fails if it lacks what the engine needs
    assert "ffmpeg" in _stage(d, "base").split("apt-get install", 1)[1].split("\n")[0]
    test, runtime = _stage(d, "test"), _stage(d, "runtime")
    assert "python -m pytest" in test and "USER 101:101" in test and "COPY tests" in test
    assert 'CMD ["python", "-m", "uniqbot", "bot"]' in runtime and "USER 101:101" in runtime
    # dependencies get their own layer, ahead of the sources
    base = _stage(d, "base")
    assert base.index("pip install -r") < base.index("COPY src") and "pip install --no-deps -e ." in base
    assert "COPY config" in base  # uniq_config defaults to PROJECT_ROOT/config/uniq.yaml


def test_env_example_lists_every_key():
    keys = {l.split("=")[0] for l in _read(".env.example").splitlines() if "=" in l and not l.startswith("#")}
    assert keys == {"BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "CHANNEL_ID", "CHANNEL_URL", "ADMIN_IDS",
                    "MAX_FILE_MB", "MAX_DURATION_SEC", "RENDER_TIMEOUT_SEC"}


def test_deploy_ships_only_committed_code():
    s = _read("scripts", "deploy.ps1")
    assert "git -C $ProjectRoot status --porcelain" in s and "git archive" in s and "/opt/reels" not in s


def test_deploy_checks_the_container_did_not_restart():
    s = _read("scripts", "deploy.ps1")
    assert "docker compose up -d --build --remove-orphans" in s and "RestartCount" in s and "Start-Sleep -Seconds 20" in s
    assert "docker build --target test" in s and "app.new" in s and "docker compose logs --tail" in s


def test_scripts_use_uniqbot_settings_and_never_touch_the_reels_bot():
    example = _read("scripts", "server.env.example")
    assert "UNIQBOT_HOST=root@203.0.113.10" in example and "UNIQBOT_KEY=~/.ssh/reels_vps" in example
    assert "UNIQBOT_DIR=/opt/uniqbot" in example
    common = _read("scripts", "server-common.ps1")
    assert '$cfg = @{ UNIQBOT_DIR = "/opt/uniqbot" }' in common
    for name in SCRIPTS:
        text = _read("scripts", name)
        assert "REELS_" not in text and "/opt/reels" not in text, name
    assert "/opt/reels" not in example and "/opt/reels" not in _read("docker-compose.yml")


def test_setup_creates_data_owned_by_101_and_a_private_env():
    s = _read("scripts", "setup-server.ps1")
    assert "chown 101:101 $dir/data" in s and "chmod 600" in s and 'Join-Path $ProjectRoot ".env"' in s


def test_powershell_scripts_carry_a_utf8_bom():
    # Windows PowerShell 5.1 reads a file without the BOM as ANSI, and the Russian messages turn to garbage
    for name in SCRIPTS:
        assert (ROOT / "scripts" / name).read_bytes().startswith(b"\xef\xbb\xbf"), name


def test_text_files_that_go_into_the_image_keep_lf():
    rules = {line.split()[0]: line.split()[1:] for line in _read(".gitattributes").splitlines() if line.strip()}
    for pattern in ("*.sh", "Dockerfile", "*.yml", "*.yaml", "*.py", "*.toml", "*.md", ".dockerignore"):
        assert rules[pattern] == ["text", "eol=lf"], pattern
    assert rules["*.ps1"] == ["text"]


def test_server_settings_file_is_not_committed():
    assert "scripts/server.env" in _read(".gitignore").splitlines()
