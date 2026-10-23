import random

import pytest
import yaml
from pydantic import ValidationError

from uniqbot import profile as profile_mod
from uniqbot.config import PROJECT_ROOT
from uniqbot.profile import (
    OrderParams,
    base_profile_path,
    build_profile,
    load_ranges,
    pick_params,
    write_profile,
)

RANGES = load_ranges(PROJECT_ROOT / "config" / "uniq.yaml")
ORDER = ["video.fit_aspect", "video.crop_resize", "video.color_eq", "video.noise", "video.speed",
         "audio.pitch_tempo", "audio.eq", "audio.loudnorm"]


def test_repo_config_values():
    assert RANGES.rates == [0.96, 0.97, 1.03, 1.04]
    assert (RANGES.crop_max_strength, RANGES.noise, RANGES.max_output_sec) == (0.10, [4, 5], 180)


def test_unknown_key_in_ranges_is_rejected(tmp_path):
    path = tmp_path / "uniq.yaml"
    path.write_text(
        (PROJECT_ROOT / "config" / "uniq.yaml").read_text(encoding="utf-8") + "extra_key: 1\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        load_ranges(path)


def test_pick_params_stay_in_ranges():
    for seed in range(200):
        p = pick_params(random.Random(seed), RANGES, duration_sec=10)
        assert RANGES.brightness.low <= p.brightness <= RANGES.brightness.high
        assert RANGES.contrast.low <= p.contrast <= RANGES.contrast.high
        assert RANGES.gamma.low <= p.gamma <= RANGES.gamma.high
        assert RANGES.saturation.low <= p.saturation <= RANGES.saturation.high
        assert p.noise in (4, 5) and p.rate in RANGES.rates and p.crop_max_strength == 0.10


def test_same_seed_gives_same_params_and_seeds_differ():
    a = pick_params(random.Random(7), RANGES, 10)
    assert a == pick_params(random.Random(7), RANGES, 10)
    assert len({pick_params(random.Random(s), RANGES, 10) for s in range(20)}) > 1


def test_all_rates_used_for_short_clips():
    assert {pick_params(random.Random(s), RANGES, 10).rate for s in range(200)} == set(RANGES.rates)


def test_slow_rates_never_push_output_over_reels_limit():
    rates_174 = {pick_params(random.Random(s), RANGES, 174).rate for s in range(300)}
    assert 0.96 not in rates_174 and 0.97 in rates_174          # 174/0.96 > 180, 174/0.97 <= 180
    assert {pick_params(random.Random(s), RANGES, 180).rate for s in range(200)} <= {1.03, 1.04}
    assert {pick_params(random.Random(s), RANGES, 200).rate for s in range(50)} == {1.04}  # none fits -> max


def test_order_params_to_dict():
    p = OrderParams(0.1, 0.01, 1.02, 0.99, 1.03, 5, 1.03)
    assert p.to_dict() == {
        "crop_max_strength": 0.1, "brightness": 0.01, "contrast": 1.02, "gamma": 0.99,
        "saturation": 1.03, "noise": 5, "rate": 1.03,
    }


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
    assert prof["name"] == "uniqbot_order"
    assert [t["id"] for t in prof["transforms"]] == ORDER
    assert by_id["video.crop_resize"] == {"max_strength": 0.1}
    assert by_id["video.color_eq"] == {"brightness": 0.01, "contrast": 1.02, "gamma": 0.99, "saturation": 1.03}
    assert by_id["video.noise"] == {"strength": 5}
    assert by_id["video.speed"] == {"rate": 1.03}
    assert by_id["audio.pitch_tempo"] == {"pitch": 1.03, "tempo": 1.03}
    assert "seed" not in prof and prof.get("seed_strategy", "per_run") == "per_run"


def test_build_profile_names_the_missing_transform(monkeypatch, tmp_path):
    base = yaml.safe_load(base_profile_path().read_text(encoding="utf-8"))
    base["transforms"] = [t for t in base["transforms"] if t["id"] != "video.noise"]
    broken = tmp_path / "base.yaml"
    broken.write_text(yaml.safe_dump(base), encoding="utf-8")
    monkeypatch.setattr(profile_mod, "base_profile_path", lambda: broken)
    with pytest.raises(ValueError, match="video.noise"):
        build_profile(pick_params(random.Random(1), RANGES, 10))


def test_written_profile_loads_in_engine(tmp_path):
    from video_uniquifier.core.profile_loader import load_profile
    path = tmp_path / "profile.yaml"
    write_profile(pick_params(random.Random(3), RANGES, 10), path)
    assert [t.id for t in load_profile(path).transforms] == ORDER
