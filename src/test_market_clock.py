"""The market clock is UTC + the configured offset, whatever the machine's timezone."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

import market_clock


def test_now_is_utc_plus_the_offset(monkeypatch):
    monkeypatch.setattr(market_clock, "UTC_OFFSET_HOURS", 8.0)
    utc = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    delta = market_clock.now() - utc
    assert abs(delta - pd.Timedelta(hours=8)) < pd.Timedelta(seconds=5)
    assert market_clock.now().tzinfo is None  # comparable with the naive DB stamps


def test_offset_is_read_from_settings(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"utc_offset_hours": 7}))
    assert market_clock._load_offset(path) == 7.0
    path.write_text("{ broken")
    assert market_clock._load_offset(path) == 8.0
    assert market_clock._load_offset(tmp_path / "missing.json") == 8.0


def test_shipped_config_defaults_to_taiwan():
    assert market_clock._load_offset() == 8.0
