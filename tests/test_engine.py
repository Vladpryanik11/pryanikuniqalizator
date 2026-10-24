import json
import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from media import make_clip

from uniqbot import engine
from uniqbot.config import PROJECT_ROOT
from uniqbot.engine import EngineError, NotAVideo, TooLong, normalize, probe_source, render, run_engine
from uniqbot.profile import load_ranges

pytestmark = pytest.mark.engine

RANGES = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)],
        check=True, capture_output=True,
    )
    return json.loads(out.stdout)


def stream_kinds(path: Path) -> list[tuple[str, str]]:
    return [(s["codec_type"], s["codec_name"]) for s in ffprobe_json(path)["streams"]]


def video_packet_sizes(path: Path) -> list[int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return [int(line) for line in out.stdout.split()]


async def test_probe_reads_vertical_clip(clips):
    info = await probe_source(clips["vertical"])
    assert abs(info.duration - 2) < 0.1 and (info.width, info.height) == (180, 320)
    assert info.has_audio and not info.rotated and not info.hdr


async def test_probe_flags_rotation_and_hdr(clips):
    assert (await probe_source(clips["rotated"])).rotated
    assert (await probe_source(clips["hlg"])).hdr


async def test_probe_reads_silent_clip(clips):
    assert not (await probe_source(clips["silent"])).has_audio


async def test_probe_rejects_non_video(tmp_path, clips):
    fake = tmp_path / "fake.mp4"
    fake.write_text("hello", encoding="utf-8")
    for path in (clips["audio_only"], fake):
        with pytest.raises(NotAVideo):
            await probe_source(path)


async def test_probe_ignores_cover_art(clips):
    with pytest.raises(NotAVideo):
        await probe_source(clips["audio_cover"])


async def test_normalize_copies_a_plain_clip(clips, tmp_path):
    src, dest = clips["vertical"], tmp_path / "clean.mkv"
    await normalize(src, dest, await probe_source(src))
    probe = ffprobe_json(dest)
    assert stream_kinds(dest) == [("video", "h264"), ("audio", "aac")]
    assert not probe["chapters"]
    assert video_packet_sizes(dest) == video_packet_sizes(src)  # streams were copied, not re-encoded


async def test_normalize_strips_tags_chapters_and_extra_audio(clips, tmp_path):
    src, dest = clips["messy"], tmp_path / "clean.mkv"
    before = ffprobe_json(src)  # the fixture really carries what has to go
    assert before["chapters"] and "secret" in json.dumps(before["format"]["tags"])
    assert [s["codec_type"] for s in before["streams"]].count("audio") == 2
    await normalize(src, dest, await probe_source(src))
    after = ffprobe_json(dest)
    assert [s["codec_type"] for s in after["streams"]] == ["video", "audio"]
    assert not after["chapters"]
    # the muxer may add its own tags (ENCODER, DURATION); only ours must be gone
    all_tags = [after["format"].get("tags", {}), *(s.get("tags", {}) for s in after["streams"])]
    assert "secret" not in json.dumps(all_tags)


async def test_normalize_keeps_a_silent_clip_silent(clips, tmp_path):
    src, dest = clips["silent"], tmp_path / "clean.mkv"
    await normalize(src, dest, await probe_source(src))
    assert stream_kinds(dest) == [("video", "h264")]


async def test_normalize_bakes_rotation(clips, tmp_path):
    src, dest = clips["rotated"], tmp_path / "clean.mkv"
    await normalize(src, dest, await probe_source(src))
    out = await probe_source(dest)
    assert (out.width, out.height) == (180, 320) and not out.rotated
    assert ffprobe_json(dest)["streams"][0]["pix_fmt"] == "yuv420p"


async def test_normalize_tonemaps_hdr(clips, tmp_path):
    src, dest = clips["hlg"], tmp_path / "clean.mkv"
    await normalize(src, dest, await probe_source(src))
    video = ffprobe_json(dest)["streams"][0]
    assert video.get("color_transfer") not in HDR_TRANSFERS and video["pix_fmt"] == "yuv420p"
    assert not (await probe_source(dest)).hdr


@pytest.mark.parametrize(("kind", "shown"), [("big_rotated", (1200, 2400)), ("big_hlg", (1100, 2000))])
async def test_normalize_downscales_before_it_re_encodes(kind, shown, tmp_path):
    # the engine outputs 1080x1920 anyway; re-encoding a 4K phone clip at full size would eat the time budget
    src, dest = make_clip(kind, tmp_path / "src"), tmp_path / "clean.mkv"
    info = await probe_source(src)
    assert info.rotated or info.hdr  # it takes the re-encode path
    await normalize(src, dest, info)
    out = await probe_source(dest)
    assert not out.rotated and not out.hdr
    assert max(out.width, out.height) <= 1920 and out.width % 2 == 0 and out.height % 2 == 0
    assert abs(out.width * shown[1] - out.height * shown[0]) <= shown[1]  # the aspect ratio is kept to a pixel


async def test_normalize_copy_path_keeps_the_size(clips, tmp_path):
    src, dest = clips["horizontal"], tmp_path / "clean.mkv"
    await normalize(src, dest, await probe_source(src))
    assert (await probe_source(dest)).width == 320  # nothing is scaled when the streams are copied


async def test_normalize_drops_timecode_and_keeps_pcm(clips, tmp_path):
    src, dest = clips["mov_tmcd_pcm"], tmp_path / "clean.mkv"
    await normalize(src, dest, await probe_source(src))
    assert stream_kinds(dest) == [("video", "h264"), ("audio", "pcm_s16le")]


@pytest.mark.parametrize("kind", ["vertical", "horizontal", "silent", "hlg", "rotated", "hlg_rotated", "mov_tmcd_pcm"])
async def test_render_end_to_end(kind, clips, tmp_path):
    res = await render(clips[kind], tmp_path / "w", ranges=RANGES, max_duration=180, timeout=300, rng=random.Random(7))
    out = await probe_source(res.output)
    assert (out.width, out.height) == (1080, 1920) and not out.hdr and not out.rotated
    assert out.has_audio == (kind != "silent")
    assert abs(out.duration - 2 / res.params.rate) < 0.15
    assert res.source.duration == pytest.approx(2, abs=0.1)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(res.output), "-f", "null", "-"], check=True, capture_output=True)


async def test_render_rejects_too_long(clips, tmp_path):
    with pytest.raises(TooLong):
        await render(clips["vertical"], tmp_path / "w", ranges=RANGES, max_duration=1, timeout=300, rng=random.Random(1))


async def test_render_rejects_non_video(tmp_path, clips):
    with pytest.raises(NotAVideo):
        await render(clips["audio_only"], tmp_path / "w", ranges=RANGES, max_duration=180, timeout=300, rng=random.Random(1))


async def test_run_engine_failure_carries_stderr(clips, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\ntransforms: [{id: video.nope}]\n", encoding="utf-8")
    with pytest.raises(EngineError) as e:
        await run_engine(clips["vertical"], bad, tmp_path / "o.mp4", tmp_path / "t", timeout=120)
    assert "video.nope" in str(e.value)  # the engine's own stderr tail, not just our prefix


async def test_run_engine_timeout_is_an_engine_error(clips, tmp_path):
    bad = tmp_path / "any.yaml"
    bad.write_text("name: x\ntransforms: []\n", encoding="utf-8")
    with pytest.raises(EngineError, match="timed out"):
        await run_engine(clips["vertical"], bad, tmp_path / "o.mp4", tmp_path / "t", timeout=0.05)


async def test_run_engine_command_line(monkeypatch):
    seen = {}

    async def fake_run(cmd, timeout, label, env=None):
        seen.update(cmd=cmd, timeout=timeout, env=env)
        return b""

    monkeypatch.setattr(engine, "_run", fake_run)
    monkeypatch.setattr(engine, "_nice_prefix", lambda: [])
    await run_engine(Path("c.mkv"), Path("p.yaml"), Path("o.mp4"), Path("w"), timeout=12)
    cmd = seen["cmd"]
    assert cmd[1:5] == ["-m", "video_uniquifier", "run", "c.mkv"]
    assert cmd[cmd.index("--workers") + 1] == "1" and "--no-qa" in cmd and "--no-progress" in cmd
    assert "--no-preflight" not in cmd
    assert seen["timeout"] == 12 and seen["env"]["PYTHONUTF8"] == "1"


@pytest.mark.parametrize(
    ("os_name", "nice_path", "expected"),
    [("posix", "/usr/bin/nice", ["nice", "-n", "10"]), ("posix", None, []), ("nt", "C:/tools/nice.exe", [])],
)
def test_nice_prefix_only_on_posix_with_nice(monkeypatch, os_name, nice_path, expected):
    monkeypatch.setattr(engine, "os", SimpleNamespace(name=os_name))
    monkeypatch.setattr(engine.shutil, "which", lambda name: nice_path)
    assert engine._nice_prefix() == expected
