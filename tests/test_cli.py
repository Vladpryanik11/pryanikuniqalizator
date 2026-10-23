import json
import subprocess
import sys

import pytest

from uniqbot.config import PROJECT_ROOT
from uniqbot.profile import load_ranges

pytestmark = pytest.mark.engine

RANGES = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")


def test_render_command_prints_params_and_writes_output(clips, tmp_path):
    out = tmp_path / "out.mp4"
    done = subprocess.run(
        [sys.executable, "-m", "uniqbot", "render", str(clips["vertical"]), str(out), "--seed", "5"],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert done.returncode == 0, done.stderr
    assert out.exists()
    assert json.loads(done.stdout)["rate"] in RANGES.rates


def test_render_command_fails_on_a_non_video(clips, tmp_path):
    done = subprocess.run(
        [sys.executable, "-m", "uniqbot", "render", str(clips["audio_only"]), str(tmp_path / "out.mp4")],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    assert done.returncode != 0 and not (tmp_path / "out.mp4").exists()
