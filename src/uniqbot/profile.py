"""Per-order engine profile: the instagram_reels preset with values drawn from config/uniq.yaml."""

import random
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

PROFILE_NAME = "uniqbot_order"


class Span(BaseModel):
    low: float
    high: float


class UniqRanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crop_max_strength: float
    brightness: Span
    contrast: Span
    gamma: Span
    saturation: Span
    noise: list[int]
    rates: list[float]
    max_output_sec: float


@dataclass(frozen=True)
class OrderParams:
    crop_max_strength: float
    brightness: float
    contrast: float
    gamma: float
    saturation: float
    noise: int
    rate: float

    def to_dict(self) -> dict:
        return asdict(self)


def load_ranges(path: Path) -> UniqRanges:
    return UniqRanges.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def pick_params(rng: random.Random, ranges: UniqRanges, duration_sec: float) -> OrderParams:
    def draw(span: Span) -> float:
        return round(rng.uniform(span.low, span.high), 4)

    brightness, contrast, gamma, saturation = (
        draw(s) for s in (ranges.brightness, ranges.contrast, ranges.gamma, ranges.saturation)
    )
    noise = rng.choice(ranges.noise)
    # a slow rate must not stretch the result past the Reels limit; if none fits, take the fastest
    fitting = [r for r in ranges.rates if duration_sec / r <= ranges.max_output_sec]
    rate = rng.choice(fitting or [max(ranges.rates)])
    return OrderParams(ranges.crop_max_strength, brightness, contrast, gamma, saturation, noise, rate)


def base_profile_path() -> Path:
    return Path(str(resources.files("video_uniquifier") / "profiles" / "instagram_reels.yaml"))


def build_profile(params: OrderParams) -> dict:
    profile = yaml.safe_load(base_profile_path().read_text(encoding="utf-8"))
    profile["name"] = PROFILE_NAME
    profile.pop("seed", None)  # per_run strategy draws a fresh seed
    transforms = profile["transforms"]
    new_params = {
        "video.crop_resize": {"max_strength": params.crop_max_strength},
        "video.color_eq": {
            "brightness": params.brightness,
            "contrast": params.contrast,
            "gamma": params.gamma,
            "saturation": params.saturation,
        },
        "video.noise": {"strength": params.noise},
        "audio.pitch_tempo": {"pitch": params.rate, "tempo": params.rate},
    }
    by_id = {t["id"]: t for t in transforms}
    for transform_id, value in new_params.items():
        if transform_id not in by_id:
            raise ValueError(f"base profile has no transform {transform_id}")
        by_id[transform_id]["params"] = value
    # time goes last among the video transforms (geometry -> colour -> noise -> time)
    last_video = max(i for i, t in enumerate(transforms) if t["id"].startswith("video."))
    transforms.insert(last_video + 1, {"id": "video.speed", "enabled": True, "params": {"rate": params.rate}})
    return profile


def write_profile(params: OrderParams, path: Path) -> None:
    text = yaml.safe_dump(build_profile(params), sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
