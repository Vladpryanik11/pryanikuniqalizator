"""Tiny synthetic clips for the engine tests (2 s of testsrc2 + a 440 Hz sine)."""

import subprocess
from pathlib import Path

KINDS = ("vertical", "horizontal", "silent", "hlg", "rotated", "hlg_rotated", "mov_tmcd_pcm", "audio_only", "audio_cover", "messy")

_VIDEO_H264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
# ffmpeg 9 drops -color_trc/-color_primaries after the implicit pix_fmt conversion, so the frames are tagged by setparams
_VIDEO_HLG = [
    "-vf", "format=yuv420p10le,setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
    "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
    "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
    "-x265-params", "log-level=error",
]


def _ffmpeg(*args: object) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", *map(str, args)]
    subprocess.run(cmd, check=True, capture_output=True)


def _generate(size: str | None, video_args: list[str], audio_args: list[str], dest: Path, *extra: str) -> None:
    inputs: list[str] = []
    if size:
        inputs += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration=2"]
    if audio_args:
        inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    _ffmpeg(*inputs, *(video_args if size else []), *audio_args, *extra, dest)


def _rotate(src: Path, dest: Path) -> None:
    # a phone-style clip: frames stored 320x180, shown turned by 90 degrees (display matrix)
    _ffmpeg("-display_rotation", 90, "-i", src, "-c", "copy", dest)


def make_clip(kind: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    aac = ["-c:a", "aac"]
    out = dest_dir / f"{kind}.mp4"
    if kind == "vertical":
        _generate("180x320", _VIDEO_H264, aac, out)
    elif kind == "horizontal":
        _generate("320x180", _VIDEO_H264, aac, out)
    elif kind == "silent":
        _generate("180x320", _VIDEO_H264, [], out)
    elif kind == "hlg":
        _generate("180x320", _VIDEO_HLG, aac, out)
    elif kind in ("big_rotated", "big_hlg"):
        # larger than the engine's 1080x1920 output; not in KINDS: only the tests that need it pay for it.
        # big_rotated is stored 2400x1200 and shown turned to 1200x2400, big_hlg is 1100x2000 HLG
        if kind == "big_hlg":
            _generate("1100x2000", _VIDEO_HLG, aac, out, "-preset", "ultrafast")
        else:
            tmp = dest_dir / f"{kind}_unrotated.mp4"
            _generate("2400x1200", _VIDEO_H264, aac, tmp, "-preset", "ultrafast")
            _rotate(tmp, out)
            tmp.unlink()
    elif kind in ("rotated", "hlg_rotated"):
        tmp = dest_dir / f"{kind}_unrotated.mp4"
        _generate("320x180", _VIDEO_HLG if kind == "hlg_rotated" else _VIDEO_H264, aac, tmp)
        _rotate(tmp, out)
        tmp.unlink()
    elif kind == "mov_tmcd_pcm":
        out = dest_dir / f"{kind}.mov"
        _generate("180x320", _VIDEO_H264, ["-c:a", "pcm_s16le"], out, "-timecode", "00:00:00:00")
    elif kind == "audio_only":
        out = dest_dir / f"{kind}.m4a"
        _generate(None, [], aac, out)
    elif kind == "audio_cover":
        # a song with cover art: its only "video" stream is an attached picture
        cover, song = dest_dir / "cover.png", dest_dir / "song.m4a"
        _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x64", "-frames:v", 1, cover)
        _generate(None, [], aac, song)
        out = dest_dir / f"{kind}.m4a"
        _ffmpeg("-i", song, "-i", cover, "-map", 0, "-map", 1, "-c", "copy", "-disposition:v:0", "attached_pic", out)
    elif kind == "messy":
        # what a camera app leaves behind: global and stream tags, a chapter, a second audio track
        meta = dest_dir / "messy_meta.txt"
        meta.write_text(
            ";FFMETADATA1\ntitle=secret-global-title\n"
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=secret-chapter\n",
            encoding="utf-8",
        )
        _ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            "-i", meta,
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map_metadata", 3, "-map_chapters", 3,
            "-metadata:s:v:0", "comment=secret-video-tag", "-metadata:s:a:0", "title=secret-audio-tag",
            *_VIDEO_H264, *aac, out,
        )
    else:
        raise ValueError(f"unknown clip kind: {kind}")
    return out
