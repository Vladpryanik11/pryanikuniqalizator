"""Input preparation and the video-uniquifier runner."""

import asyncio
import json
import os
import random
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from uniqbot.profile import OrderParams, UniqRanges, pick_params, write_profile

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ and HLG
# What the engine's video.tonemap_sdr builds with its defaults (hable, peak 1000, desat 0);
# see video_uniquifier/core/transforms/video_tonemap.py. The code is not imported on purpose.
TONEMAP_SDR = (
    "zscale=t=linear:npl=1000,tonemap=hable:desat=0.0000:peak=10.0000,"
    "zscale=t=bt709:m=bt709:p=bt709:r=tv,format=yuv420p"
)
# The engine outputs 1080x1920 anyway, so a clip that has to be re-encoded is first cut down to at most 1920 on a side
# (a 4K HDR phone clip would otherwise take most of the render budget on 2 vCPU); even sides for yuv420p.
DOWNSCALE = "scale=w='min(iw,1920)':h='min(ih,1920)':force_original_aspect_ratio=decrease:force_divisible_by=2"
NORMALIZE_TIMEOUT = 600  # when normalize is called on its own; render passes what is left of its budget


class EngineError(RuntimeError):
    pass


class NotAVideo(EngineError):
    pass


class TooLong(EngineError):
    pass


@dataclass(frozen=True)
class SourceInfo:
    duration: float
    width: int
    height: int
    has_audio: bool
    rotated: bool
    hdr: bool


@dataclass(frozen=True)
class RenderResult:
    output: Path
    params: OrderParams
    source: SourceInfo


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)  # the engine's ffmpeg children share the group
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _run(cmd: list[str], timeout: float, label: str, env: dict[str, str] | None = None) -> bytes:
    group = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **group
        )
    except FileNotFoundError:
        raise EngineError(f"{label}: {cmd[0]} not found") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill(proc)
        raise EngineError(f"{label} timed out after {timeout:.0f}s") from None
    except BaseException:
        await _kill(proc)
        raise
    if proc.returncode != 0:
        # some tools report failures on stdout
        tail = (err.strip() or out.strip()).decode("utf-8", "replace")[-2000:]
        raise EngineError(f"{label} exited {proc.returncode}: {tail}")
    return out


async def probe_source(path: Path) -> SourceInfo:
    try:
        raw = await _run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            60, "ffprobe",
        )
    except EngineError as e:
        raise NotAVideo(str(e)) from e
    data = json.loads(raw.decode("utf-8", "replace"))
    streams = data.get("streams", [])
    # the first real video stream; cover art is a video stream flagged attached_pic
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise NotAVideo("no video stream")
    try:
        duration = float(data.get("format", {}).get("duration") or video.get("duration"))
    except (TypeError, ValueError):
        raise NotAVideo("no duration") from None
    if duration <= 0:
        raise NotAVideo("no duration")
    return SourceInfo(
        duration=duration,
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        rotated=any(float(d.get("rotation") or 0) != 0 for d in video.get("side_data_list", [])),
        hdr=video.get("color_transfer") in HDR_TRANSFERS,
    )


async def normalize(src: Path, dest: Path, info: SourceInfo, timeout: float = NORMALIZE_TIMEOUT) -> None:
    """Write dest (.mkv): the first real video stream and the first audio stream, no metadata, SDR, upright."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(src),
        "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
    ]
    if info.rotated or info.hdr:
        # ffmpeg turns the frame upright while decoding; the engine rejects HDR and rotation flags
        cmd += ["-vf", f"{DOWNSCALE},{TONEMAP_SDR if info.hdr else 'format=yuv420p'}"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-c:a", "copy"]
    else:
        cmd += ["-c", "copy"]
    await _run([*cmd, str(dest)], timeout, "ffmpeg")


def _nice_prefix() -> list[str]:
    return ["nice", "-n", "10"] if os.name == "posix" and shutil.which("nice") else []


async def run_engine(clean: Path, profile: Path, out: Path, work: Path, timeout: float) -> None:
    cmd = [
        *_nice_prefix(), sys.executable, "-m", "video_uniquifier", "run", str(clean),
        "--profile", str(profile), "--out", str(out), "--work-dir", str(work),
        "--workers", "1", "--no-qa", "--no-progress",
    ]
    # the engine prints emoji; a legacy console codepage would crash it
    await _run(cmd, timeout, "video-uniq", {**os.environ, "PYTHONUTF8": "1"})


async def render(
    src: Path, workdir: Path, *, ranges: UniqRanges, max_duration: float, timeout: float, rng: random.Random
) -> RenderResult:
    """Uniquify src into workdir/output.mp4; timeout covers input preparation and the engine together."""
    started = time.monotonic()
    workdir.mkdir(parents=True, exist_ok=True)
    info = await probe_source(src)
    if info.duration > max_duration:
        raise TooLong(f"{info.duration:.0f}s is longer than {max_duration:.0f}s")
    clean = workdir / "clean.mkv"
    await normalize(src, clean, info, timeout=timeout)
    params = pick_params(rng, ranges, info.duration)
    profile = workdir / "profile.yaml"
    write_profile(params, profile)
    left = timeout - (time.monotonic() - started)
    if left <= 0:
        raise EngineError(f"render timed out after {timeout:.0f}s")
    output = workdir / "output.mp4"
    await run_engine(clean, profile, output, workdir / "tmp", timeout=left)
    return RenderResult(output=output, params=params, source=info)
