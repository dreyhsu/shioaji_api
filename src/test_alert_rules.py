"""Tests for the Telegram rule evaluation and its JSON config (no DB, no network)."""

from __future__ import annotations

import json
import os

import pandas as pd
import pytest

import alert_rules as ar
import dashboard

HOURS = pd.to_datetime(
    ["2026-10-08 15:00", "2026-10-08 16:00", "2026-10-08 17:00", "2026-10-08 18:00"]
)
HOURLY = pd.DataFrame({"hour_start": HOURS, "timestamp": HOURS, "close": [100.0] * 4})
SUPPORT = {"box_id": 1, "box_type": "support", "top": 130.0, "bottom": 100.0}
RESISTANCE = {"box_id": 2, "box_type": "resistance", "top": 230.0, "bottom": 200.0}
MINUTE = pd.Timestamp("2026-10-08 18:05")


def anchor(index, price):
    return {"time": dashboard._epoch(HOURS[index]), "price": price}


def line(line_id, prices, kind="line"):
    """A straight line through bars 0 and 1 at ``prices``, extended right."""
    return {"id": line_id, "kind": kind, "label": None,
            "anchors": [anchor(0, prices[0]), anchor(1, prices[1])]}


@pytest.fixture
def config(tmp_path):
    return ar.AlertConfig(tmp_path / "missing.json")  # all defaults


def run(config, close, *, boxes=None, prev=None, trendlines=(), atr=None):
    return ar.evaluate(
        code="TMFR1",
        minute_bar={"minute_start": MINUTE, "close": close},
        boxes=boxes or {},
        prev_minute_bar=None if prev is None else {"close": prev},
        hourly=HOURLY,
        trendlines=list(trendlines),
        hourly_atr=atr,
        config=config,
    )


def rules(candidates):
    return sorted(c.rule for c in candidates)


# ------------------------------------------------------------------ boxes
@pytest.mark.parametrize(
    "close, expected",
    [(100, True), (109.9, True), (110.1, False), (125, False), (99, False)],
)
def test_support_bottom_third(config, close, expected):
    hits = run(config, close, boxes={"support": SUPPORT})
    assert (rules(hits) == ["support_bottom_third"]) is expected
    if expected:
        assert hits[0].key == "support:1"


@pytest.mark.parametrize(
    "close, expected",
    [(230, True), (220.1, True), (219.9, False), (205, False), (231, False)],
)
def test_resistance_top_third(config, close, expected):
    hits = run(config, close, boxes={"resistance": RESISTANCE})
    assert (rules(hits) == ["resistance_top_third"]) is expected


@pytest.mark.parametrize(
    "side, box, prev, close, head",
    [
        ("support", SUPPORT, 101, 99, "跌破"),
        ("support", SUPPORT, 129, 131, "守住（向上離開）"),
        ("resistance", RESISTANCE, 229, 231, "突破"),
        ("resistance", RESISTANCE, 201, 199, "守住（向下離開）"),
    ],
)
def test_box_in_out_when_the_close_leaves_the_box(config, side, box, prev, close, head):
    hits = run(config, close, boxes={side: box}, prev=prev)
    assert rules(hits) == ["box_in_out"]
    assert hits[0].key == f"{side}:{box['box_id']}"
    assert head in hits[0].message


@pytest.mark.parametrize(
    "prev, close",
    [(None, 95), (95, 90), (95, 105), (105, 110), (100, 130)],
)
def test_box_in_out_needs_previous_close_in_and_latest_out(config, prev, close):
    # no previous bar / out->out / out->in / in->in (edges count as inside)
    hits = run(config, close, boxes={"support": SUPPORT}, prev=prev)
    assert "box_in_out" not in rules(hits)


# ------------------------------------------------------------------ trendlines
def test_line_value_and_slope_at_the_newest_bar():
    # 100 -> 110 over bars 0..1, so bar 3 sits at 130 with slope +10/bar.
    assert ar.line_now(HOURLY, line(1, (100, 110))) == (pytest.approx(130.0), pytest.approx(10.0))


def test_near_uses_points_or_atr(config):
    up = line(1, (100, 110))  # 130 at the newest bar
    assert rules(run(config, 155, trendlines=[up])) == ["trendline_near"]  # 25 <= 30
    assert rules(run(config, 165, trendlines=[up])) == []  # 35 > 30, above a rising line

    config.data["trendline_near"]["distance_atr"] = 0.5
    assert rules(run(config, 165, trendlines=[up], atr=100)) == ["trendline_near"]  # 35 <= 50
    assert rules(run(config, 165, trendlines=[up], atr=40)) == []  # 35 > 20


def test_break_below_a_rising_line_is_once_only(config):
    up = line(1, (100, 110))
    hits = run(config, 90, trendlines=[up])
    assert rules(hits) == ["trendline_break_below_up"]
    assert hits[0].once and hits[0].key == "line:1"
    # Above a rising line is not a break.
    assert rules(run(config, 200, trendlines=[up])) == []


def test_break_above_a_falling_line(config):
    down = line(2, (200, 190))  # 170 at the newest bar
    assert rules(run(config, 250, trendlines=[down])) == ["trendline_break_above_down"]
    assert rules(run(config, 100, trendlines=[down])) == []


def test_break_buffer(config):
    up = line(1, (100, 110))
    config.data["trendline_break_below_up"]["buffer_points"] = 20
    assert rules(run(config, 120, trendlines=[up])) == ["trendline_near"]  # only 10 below
    assert rules(run(config, 95, trendlines=[up])) == ["trendline_break_below_up"]


def test_kinds_filter(config):
    curve_like = line(1, (100, 110), kind="line")
    config.data["trendline_break_below_up"]["kinds"] = ["curve"]
    assert rules(run(config, 50, trendlines=[curve_like])) == []


def test_disabled_rules_do_not_fire(config):
    for name in ar.RULES:
        config.data[name]["enabled"] = False
    assert run(config, 105, boxes={"support": SUPPORT}, trendlines=[line(1, (100, 110))]) == []


# ------------------------------------------------------------------ config file
def test_config_merges_over_defaults_and_reloads(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"cooldown_minutes": 5, "trendline_near": {"distance_points": 12}}))
    config = ar.AlertConfig(path)
    assert config.cooldown_minutes("box_in_out") == 5
    assert config.rule("trendline_near")["distance_points"] == 12
    assert config.rule("trendline_near")["kinds"] == ["curve", "line"]  # default kept

    path.write_text(json.dumps({"box_in_out": {"enabled": False, "cooldown_minutes": 30}}))
    os.utime(path, (1, 1))  # force a different mtime
    assert config.reload()
    assert not config.enabled("box_in_out")
    assert config.cooldown_minutes("box_in_out") == 30
    assert config.cooldown_minutes("trendline_near") == 10


def test_bad_json_keeps_the_last_good_config(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"cooldown_minutes": 7}))
    config = ar.AlertConfig(path)
    path.write_text("{ not json")
    os.utime(path, (2, 2))
    assert not config.reload()
    assert config.last_error
    assert config.cooldown_minutes("box_in_out") == 7


def test_shipped_config_parses():
    config = ar.AlertConfig(ar.DEFAULT_CONFIG_PATH)
    assert config.last_error is None
    assert all(config.enabled(name) for name in ar.RULES)
    assert config.cooldown_minutes("support_bottom_third") == 10
