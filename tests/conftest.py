import shutil

import pytest


@pytest.fixture(autouse=True)
def _never_read_project_env(monkeypatch):
    """Tests must never load the real .env (it holds the bot token)."""
    from uniqbot.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def _engine_tools_present(request):
    """Engine tests need ffmpeg and the video-uniquifier package; skip them where those are missing."""
    if request.node.get_closest_marker("engine") is None:
        return
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not in PATH")
    try:
        import video_uniquifier  # noqa: F401
    except ImportError:
        pytest.skip("video_uniquifier is not installed")


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    """Synthetic 2 s clips by kind, built once per session."""
    from media import KINDS, make_clip

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not in PATH")
    root = tmp_path_factory.mktemp("clips")
    return {kind: make_clip(kind, root) for kind in KINDS}
