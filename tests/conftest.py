import pytest


@pytest.fixture(autouse=True)
def _never_read_project_env(monkeypatch):
    """Tests must never load the real .env (it holds the bot token)."""
    from uniqbot.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
