import json

import pytest

from mock_config import MockConfig, load_mock_config


def test_partial_config_defaults_and_path_independent_of_working_directory(tmp_path, monkeypatch):
    path = tmp_path / "mock.json"
    path.write_text(json.dumps({"replay": {"interval_seconds": 60}}))
    settings = load_mock_config(path)
    assert settings.replay.interval_seconds == 60
    assert settings.market == MockConfig().market
    monkeypatch.chdir(tmp_path)
    assert isinstance(load_mock_config(), MockConfig)


@pytest.mark.parametrize("data", [
    {"replay": {"interval_seconds": 0}},
    {"replay": {"browser_poll_seconds": -1}},
    {"replay": {"minutes_before_bottom": 3600}},
    {"replay": {"start_at": "unknown"}},
    {"replay": {"minutes_before_pullback": -1}},
    {"replay": {"start_at": "before_pullback", "minutes_before_pullback": 481}},
    {"market": {"decline_minutes": True}},
    {"market": {"rebound_minutes": 1.5}},
    {"market": {"wave_scale_minutes": 0}},
    {"market": {"volume_cycle_minutes": 0}},
    {"market": {"start_price": 10}},
    {"market": {"pullback_minutes": 100000}},
    {"market": {"start_time": "not a date"}},
    {"market": {"start_time": "2026-10-05T08:45:00+08:00"}},
    {"market": {"start_time": "NaT"}},
    {"market": {"start_time": "2026-10-05 08:45:01"}},
    {"broker": {"equity": float("nan")}},
    {"replay": {"interval_seconds": float("inf")}},
    {"replay": {"interval_seconds": "1"}},
    {"replay": {"interval_second": 1}},
    {"replay": {"start_bar": -1}},
    {"replay": {"start_bar": True}},
    {"replay": {"start_bar": 0.5}},
    {"replay": {"start_bar": 4201}},
])
def test_invalid_config_reports_source_path(tmp_path, data):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="bad.json"):
        load_mock_config(path)


def test_missing_or_malformed_config_reports_source_path(tmp_path):
    path = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="missing.json"):
        load_mock_config(path)
    path.write_text("{")
    with pytest.raises(ValueError, match="missing.json"):
        load_mock_config(path)


def test_comments_are_ignored_without_hiding_unknown_settings(tmp_path):
    path = tmp_path / "commented.json"
    data = {"_comment": "修改後重啟", "replay": {
        "_comment": "播放設定", "_comment_interval_seconds": "每根 K 棒間隔秒數",
        "interval_seconds": 0.7,
    }}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    settings = load_mock_config(path)
    assert settings.replay.interval_seconds == 0.7
    assert settings.market == MockConfig().market
    data["replay"]["interval_second"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="interval_second"):
        load_mock_config(path)


@pytest.mark.parametrize("offset", [0, 60, 480])
def test_pullback_start_keeps_all_previous_bars_as_history(offset):
    settings = MockConfig.model_validate({"replay": {
        "start_at": "before_pullback", "minutes_before_pullback": offset,
    }})
    assert settings.warmup_minutes == 4080 - offset


@pytest.mark.parametrize("start", [0, 1, 4020, 4200])
def test_explicit_start_bar_overrides_relative_start(start):
    settings = MockConfig.model_validate({"replay": {
        "start_bar": start, "start_at": "before_pullback", "minutes_before_pullback": 0,
    }})
    assert settings.warmup_minutes == start


def test_null_start_bar_uses_relative_start():
    settings = MockConfig.model_validate({"replay": {
        "start_bar": None, "start_at": "before_pullback", "minutes_before_pullback": 60,
    }})
    assert settings.warmup_minutes == 4020
