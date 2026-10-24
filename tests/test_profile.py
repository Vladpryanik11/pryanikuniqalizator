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


def ranges_with(tmp_path, **changes):
    """load_ranges over the repo config with some keys replaced."""
    data = yaml.safe_load((PROJECT_ROOT / "config" / "uniq.yaml").read_text(encoding="utf-8")) | changes
    path = tmp_path / "uniq.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_ranges(path)


def rejected(tmp_path, field, **changes):
    with pytest.raises(ValidationError) as e:
        ranges_with(tmp_path, **changes)
    assert field in str(e.value)  # the error says which field is wrong


@pytest.mark.parametrize("field", ["brightness", "contrast", "gamma", "saturation"])
def test_span_low_must_not_exceed_high(tmp_path, field):
    rejected(tmp_path, field, **{field: {"low": 1.05, "high": 1.01}})
    assert ranges_with(tmp_path, **{field: {"low": 1.02, "high": 1.02}})  # a single value is fine


@pytest.mark.parametrize("field", ["rates", "noise"])
def test_rates_and_noise_must_not_be_empty(tmp_path, field):
    rejected(tmp_path, field, **{field: []})


@pytest.mark.parametrize("bad", [0.5, 0.3, 0, -1.04, 2.01, 1.0])
def test_every_rate_must_be_in_the_engines_range_and_move_the_clip(tmp_path, bad):
    # (0.5, 2.0] is what the engine's video.speed accepts; 1.0 would change nothing
    rejected(tmp_path, "rates", rates=[0.96, bad])


def test_rates_at_the_edges_of_the_range_are_fine(tmp_path):
    assert ranges_with(tmp_path, rates=[0.51, 2.0]).rates == [0.51, 2.0]


@pytest.mark.parametrize("bad", [-1, 101, 1000])
def test_noise_values_are_0_to_100(tmp_path, bad):
    rejected(tmp_path, "noise", noise=[4, bad])
    assert ranges_with(tmp_path, noise=[0, 100]).noise == [0, 100]


@pytest.mark.parametrize("bad", [0, -0.05, 0.11, 1])
def test_crop_strength_is_above_0_and_at_most_a_tenth(tmp_path, bad):
    rejected(tmp_path, "crop_max_strength", crop_max_strength=bad)
    assert ranges_with(tmp_path, crop_max_strength=0.001).crop_max_strength == 0.001


@pytest.mark.parametrize("bad", [0, -180])
def test_max_output_must_be_positive(tmp_path, bad):
    rejected(tmp_path, "max_output_sec", max_output_sec=bad)


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
