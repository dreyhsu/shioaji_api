"""Integration tests for the monitor's per-cycle decisions.

These drive the real state machine, the real database and the real pattern
detectors; only Shioaji and Telegram are stubbed, since the point is the wiring
between them -- entering a box opens a visit and persists a book, and a rule hit
claims an alert row only outside its cooldown.
"""

from __future__ import annotations

import json
import os

import pandas as pd
import pytest

import alert_rules as ar
import db
import monitor
import patterns as pt
import snr_core as snr
import session_bars as sb
from box_watch import BoxWatcher, BoxWatchConfig

SUPPORT_BOX = {
    "box_id": 1, "box_type": "support", "state": "fresh",
    "top": 110.0, "bottom": 100.0, "pivot_price": 110.0, "atr_width": 10.0,
    "delta_volume": 1.0,
    "left_time": pd.Timestamp("2026-10-08 10:00"), "right_time": pd.Timestamp("2026-10-09 10:00"),
    "created_time": pd.Timestamp("2026-10-08 10:00"), "state_time": pd.NaT,
    "pivot_index": 0, "created_index": 1, "holds": 0, "breakouts": 0,
}


class StubRecorder:
    """Stands in for BookRecorder: counts calls, invents no data."""

    def __init__(self):
        self.flushes = []
        self.reversal_captures = []
        self.source = "bidask_v1"
        self.levels = 5

    def flush(self, conn, visit_id, phase="in_box"):
        self.flushes.append((visit_id, phase))
        return 3

    def record_at_reversal(self, conn, visit_id, reversal_id):
        self.reversal_captures.append((visit_id, reversal_id))
        return 1

    def forget(self, visit_id):
        pass


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "monitor.db")
    db.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def config():
    # Telegram off: these tests assert on the alert rows, not on the network.
    return monitor.MonitorConfig(code="TMFR1", enable_telegram=False, exit_buffer_atr=0.0)


def make_state(low, high, close, *, boxes=None, minute="2026-10-08 15:00"):
    minute_bar = pd.Series(
        {
            "minute_start": pd.Timestamp(minute),
            "open": close, "high": high, "low": low, "close": close, "volume": 10.0,
        }
    )
    box_table = pd.DataFrame([SUPPORT_BOX] if boxes is None else boxes)
    return monitor.MarketState(
        minute_bars=pd.DataFrame([minute_bar]),
        hourly=pd.DataFrame(
            [{"hour_start": pd.Timestamp("2026-10-08 15:00"), "open": 105.0, "high": high,
              "low": low, "close": close, "volume": 10.0, "minutes": 5,
              "session_id": "2026-10-09 night"}]
        ),
        boxes=box_table,
        closed_through=-1,
        hourly_atr=10.0,
        latest_minute=minute_bar,
        forming_hour=None,
        prev_closed_hour=None,
    )


def watchers(config):
    return {
        side: BoxWatcher(side, BoxWatchConfig(exit_buffer_atr=config.exit_buffer_atr))
        for side in monitor.SIDES
    }


# --------------------------------------------------------------------------------------
# Entering a box
# --------------------------------------------------------------------------------------
def test_entering_a_box_opens_a_visit_and_records_the_book(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    monitor.handle_side(conn, config, make_state(102, 108, 105), watch["support"], recorder)

    visit = db.get_open_visit(conn, "TMFR1", "support")
    assert visit is not None
    assert visit["box_id"] == 1
    assert visit["entry_price"] == 105.0
    assert visit["entry_zone_pct"] == pytest.approx(0.5)
    assert visit["source"] == "live"
    # The buffered run-up is written as context before anything else.
    assert recorder.flushes == [(visit["id"], "pre_entry")]


def test_box_visits_send_no_alerts_of_their_own(conn, config):
    """Entering and staying only record; Telegram is the rule engine's job now."""
    recorder = StubRecorder()
    watch = watchers(config)
    for minute in ["15:00", "15:01", "15:02"]:
        monitor.handle_side(
            conn, config,
            make_state(102, 108, 105, minute=f"2026-10-08 {minute}"),
            watch["support"], recorder,
        )
    assert db.load_alerts(conn, "TMFR1").empty
    assert len(recorder.flushes) == 3


def test_price_outside_the_box_does_nothing(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    monitor.handle_side(conn, config, make_state(130, 140, 135), watch["support"], recorder)
    assert db.get_open_visit(conn, "TMFR1", "support") is None
    assert db.load_alerts(conn, "TMFR1").empty


def test_a_wick_into_the_box_still_opens_a_visit(conn, config):
    """Close is well above the box; only the low reaches in."""
    recorder = StubRecorder()
    watch = watchers(config)
    monitor.handle_side(conn, config, make_state(109, 140, 138), watch["support"], recorder)
    assert db.get_open_visit(conn, "TMFR1", "support") is not None


# --------------------------------------------------------------------------------------
# Leaving a box
# --------------------------------------------------------------------------------------
def test_leaving_closes_the_visit_after_a_final_flush(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    monitor.handle_side(conn, config, make_state(102, 108, 105), watch["support"], recorder)
    visit_id = db.get_open_visit(conn, "TMFR1", "support")["id"]

    monitor.handle_side(
        conn, config,
        make_state(130, 140, 135, minute="2026-10-08 15:01"),
        watch["support"], recorder,
    )
    assert db.get_open_visit(conn, "TMFR1", "support") is None
    row = conn.execute("SELECT * FROM box_visits WHERE id = ?", (visit_id,)).fetchone()
    assert row["exit_reason"] == "left_box"
    assert row["exit_price"] == 135.0
    assert recorder.flushes[-1] == (visit_id, "in_box")  # flushed before closing


def test_losing_the_box_closes_the_visit(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    monitor.handle_side(conn, config, make_state(102, 108, 105), watch["support"], recorder)
    visit_id = db.get_open_visit(conn, "TMFR1", "support")["id"]

    broken = dict(SUPPORT_BOX, state="broken")
    monitor.handle_side(
        conn, config,
        make_state(102, 108, 105, boxes=[broken], minute="2026-10-08 15:01"),
        watch["support"], recorder,
    )
    row = conn.execute("SELECT * FROM box_visits WHERE id = ?", (visit_id,)).fetchone()
    assert row["is_open"] == 0
    assert row["exit_reason"] == "box_broken"


# --------------------------------------------------------------------------------------
# Reversal detection while inside
# --------------------------------------------------------------------------------------
def test_a_hammer_inside_the_box_is_logged_with_its_book(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    state = make_state(100, 110, 105)
    monitor.handle_side(conn, config, state, watch["support"], recorder)

    # A hammer forming inside the support box, with a previous closed bar to compare.
    state.forming_hour = pd.Series(
        {"hour_start": pd.Timestamp("2026-10-08 15:00"), "open": 105.0, "high": 106.0,
         "low": 95.0, "close": 104.5, "volume": 10.0, "minutes": 14}
    )
    state.prev_closed_hour = pd.Series(
        {"hour_start": pd.Timestamp("2026-10-08 14:00"), "open": 112.0, "high": 113.0,
         "low": 104.0, "close": 105.0, "volume": 10.0}
    )
    monitor.detect_reversals(conn, config, state, watch, recorder)

    reversals = db.load_reversals(conn, "TMFR1")
    assert len(reversals) == 1
    assert reversals.iloc[0]["pattern"] == "long_lower_tail"
    assert reversals.iloc[0]["direction"] == "long"
    assert recorder.reversal_captures  # the book at that instant was captured


def test_the_same_pattern_in_the_same_minute_is_not_duplicated(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    state = make_state(100, 110, 105)
    monitor.handle_side(conn, config, state, watch["support"], recorder)
    state.forming_hour = pd.Series(
        {"hour_start": pd.Timestamp("2026-10-08 15:00"), "open": 105.0, "high": 106.0,
         "low": 95.0, "close": 104.5, "volume": 10.0, "minutes": 14}
    )
    state.prev_closed_hour = pd.Series(
        {"hour_start": pd.Timestamp("2026-10-08 14:00"), "open": 112.0, "high": 113.0,
         "low": 104.0, "close": 105.0, "volume": 10.0}
    )
    monitor.detect_reversals(conn, config, state, watch, recorder)
    monitor.detect_reversals(conn, config, state, watch, recorder)  # retried cycle
    assert len(db.load_reversals(conn, "TMFR1")) == 1


def test_no_reversals_are_logged_when_price_is_outside(conn, config):
    recorder = StubRecorder()
    watch = watchers(config)
    state = make_state(130, 140, 135)
    monitor.handle_side(conn, config, state, watch["support"], recorder)
    state.forming_hour = pd.Series(
        {"hour_start": pd.Timestamp("2026-10-08 15:00"), "open": 105.0, "high": 106.0,
         "low": 95.0, "close": 104.5, "volume": 10.0, "minutes": 14}
    )
    state.prev_closed_hour = pd.Series(
        {"hour_start": pd.Timestamp("2026-10-08 14:00"), "open": 112.0, "high": 113.0,
         "low": 104.0, "close": 105.0, "volume": 10.0}
    )
    monitor.detect_reversals(conn, config, state, watch, recorder)
    assert db.load_reversals(conn, "TMFR1").empty


# --------------------------------------------------------------------------------------
# Closed-bar bookkeeping
# --------------------------------------------------------------------------------------
def test_last_closed_index_marks_the_current_hour_as_forming():
    hourly = pd.DataFrame(
        {"hour_start": pd.to_datetime(["2026-10-08 15:00", "2026-10-08 16:00"])}
    )
    now = pd.Timestamp("2026-10-08 16:30")  # inside the 16:00 bucket
    assert monitor.last_closed_index(hourly, now) == 0


def test_outside_a_session_every_bar_is_closed():
    hourly = pd.DataFrame(
        {"hour_start": pd.to_datetime(["2026-10-08 15:00", "2026-10-08 16:00"])}
    )
    now = pd.Timestamp("2026-10-08 14:00")  # the afternoon break
    assert monitor.last_closed_index(hourly, now) == 1


def test_no_bars_means_no_closed_index():
    assert monitor.last_closed_index(pd.DataFrame(), pd.Timestamp.now()) == -1


@pytest.mark.parametrize("now,expected_boxes", [("09:50", 0), ("10:00", 1)])
def test_snr_uses_15m_bars_and_waits_for_confirmation(conn, config, now, expected_boxes):
    stamps = pd.date_range("2026-10-08 08:45", periods=75, freq="min")
    closes, opens = [10, 9, 5, 9, 10], [11, 10, 6, 5, 5]
    bars = pd.DataFrame([
        dict(minute_start=stamp, open=opens[i // 15], close=closes[i // 15],
             high=max(opens[i // 15], closes[i // 15]) + 1,
             low=min(opens[i // 15], closes[i // 15]) - 1,
             volume=1000 if i // 15 == 4 else 100, amount=0)
        for i, stamp in enumerate(stamps)
    ])
    now = pd.Timestamp(f"2026-10-08 {now}")
    db.upsert_minute_bars(conn, config.code, sb.add_sessions(bars[bars.minute_start < now]))
    config.lookback = config.atr_length = 2
    state = monitor.build_state(conn, config, now)
    assert len(state.boxes) == expected_boxes
    if expected_boxes:
        box = state.boxes.iloc[0]
        assert box.created_time == pd.Timestamp("2026-10-08 09:45")
        assert box.left_time == pd.Timestamp("2026-10-08 09:15")
        assert box.top == 5
        assert box.atr_width == pytest.approx(
            snr.pine_atr(sb.minute_bars_to_interval(state.minute_bars, 15), 2).iloc[-1])


def test_switching_box_interval_preserves_hourly_risk_atr(conn, config):
    from test_dashboard import minute_frame

    bars = minute_frame("2026-10-01 08:45", 60 * 24 * 3)
    db.upsert_minute_bars(conn, config.code, bars)
    now = bars.minute_start.iloc[-1] + pd.Timedelta(minutes=1)
    state = monitor.build_state(conn, config, now)
    hourly = sb.minute_bars_to_hourly(bars)
    pd.testing.assert_frame_equal(state.hourly, hourly)
    assert state.hourly_atr == pytest.approx(
        snr.pine_atr(hourly, config.atr_length).iloc[state.closed_through])
    fifteen = sb.minute_bars_to_interval(bars, 15)
    expected = snr.collect_snr_boxes(
        fifteen, creation_limit=monitor.last_closed_index(fifteen, now, minutes=15))
    assert not expected.empty
    pd.testing.assert_frame_equal(state.boxes, expected)


# --------------------------------------------------------------------------------------
# Rule alerts: cooldown, once-only, wiring
# --------------------------------------------------------------------------------------
@pytest.fixture
def alerts(tmp_path):
    return ar.AlertConfig(tmp_path / "defaults.json")  # missing file = defaults


def candidate(rule="support_bottom_third", key="support:1", once=False):
    return ar.Candidate(rule=rule, key=key, once=once, message="m")


def at(minute):
    return pd.Timestamp(f"2026-10-08 {minute}")


@pytest.mark.parametrize("once", [False, True])
def test_config_pause_does_not_consume_alert_history(conn, config, alerts, monkeypatch, once):
    config.enable_telegram = True
    messages = []
    monkeypatch.setattr(monitor.tg, "send_message", lambda message: messages.append(message) or True)
    hit = candidate(
        rule="trendline_break_below_up" if once else "support_bottom_third",
        key="line:3" if once else "support:1",
        once=once,
    )
    alerts.data["telegram_enabled"] = False
    assert not monitor.send_rule(conn, config, alerts, hit, at("15:00"))
    assert db.load_alerts(conn, config.code).empty
    assert messages == []

    alerts.data["telegram_enabled"] = True
    assert monitor.send_rule(conn, config, alerts, hit, at("15:01"))
    assert messages == ["m"]
    assert len(db.load_alerts(conn, config.code)) == 1
    assert not monitor.send_rule(conn, config, alerts, hit, at("15:02"))
    assert messages == ["m"]


def test_check_alerts_reloads_master_switch(conn, config, tmp_path, monkeypatch):
    config.enable_telegram = True
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({"telegram_enabled": True}))
    alerts = ar.AlertConfig(path)
    messages = []
    monkeypatch.setattr(monitor.tg, "send_message", lambda message: messages.append(message) or True)
    state = make_state(101, 103, 102)
    db.replace_boxes(conn, config.code, state.boxes)

    path.write_text(json.dumps({"telegram_enabled": False}))
    os.utime(path, (1, 1))
    assert monitor.check_alerts(conn, config, alerts, state) == 0
    assert messages == []
    assert db.load_alerts(conn, config.code).empty

    path.write_text(json.dumps({"telegram_enabled": True}))
    os.utime(path, (2, 2))
    state = make_state(101, 103, 102, minute="2026-10-08 15:01")
    assert monitor.check_alerts(conn, config, alerts, state) == 1
    assert len(messages) == 1
    assert list(db.load_alerts(conn, config.code)["hour_start"]) == ["2026-10-08 15:01:00"]


def test_no_telegram_overrides_enabled_config(conn, alerts, monkeypatch, tmp_path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"mode": "mock"}))
    config = monitor.parse_args(["--no-telegram", "--runtime-config", str(runtime)])
    messages = []
    monkeypatch.setattr(monitor.tg, "send_message", lambda message: messages.append(message) or True)
    assert alerts.telegram_enabled
    assert not monitor.send_rule(conn, config, alerts, candidate(), at("15:00"))
    assert messages == []


def test_cooldown_suppresses_repeats_for_ten_minutes(conn, config, alerts):
    sent = [
        monitor.send_rule(conn, config, alerts, candidate(), at(minute)) or None
        for minute in ["15:00", "15:00", "15:05", "15:09", "15:10", "15:15", "15:21"]
    ]
    claimed = list(db.load_alerts(conn, "TMFR1")["hour_start"])[::-1]
    assert claimed == ["2026-10-08 15:00:00", "2026-10-08 15:10:00", "2026-10-08 15:21:00"]
    assert sent == [None] * 7  # telegram disabled in the fixture: nothing "sent"


def test_cooldown_is_per_key_and_rule(conn, config, alerts):
    monitor.send_rule(conn, config, alerts, candidate(), at("15:00"))
    monitor.send_rule(conn, config, alerts, candidate(key="support:2"), at("15:01"))
    monitor.send_rule(conn, config, alerts, candidate(rule="box_in_out"), at("15:01"))
    assert len(db.load_alerts(conn, "TMFR1")) == 3


def test_per_rule_cooldown_override(conn, config, alerts):
    alerts.data["support_bottom_third"]["cooldown_minutes"] = 2
    for minute in ["15:00", "15:01", "15:02"]:
        monitor.send_rule(conn, config, alerts, candidate(), at(minute))
    assert len(db.load_alerts(conn, "TMFR1")) == 2


def test_break_rules_send_once_per_line(conn, config, alerts):
    hit = candidate(rule="trendline_break_below_up", key="line:3", once=True)
    for minute in ["15:00", "15:30", "16:30", "18:00"]:
        monitor.send_rule(conn, config, alerts, hit, at(minute))
    assert len(db.load_alerts(conn, "TMFR1")) == 1


def test_check_alerts_wires_box_rules(conn, config, alerts):
    recorder = StubRecorder()
    watch = watchers(config)
    # 102 is in the bottom third of the 100-110 support box.
    state = make_state(101, 103, 102)
    monitor.handle_side(conn, config, state, watch["support"], recorder)
    monitor.check_alerts(conn, config, alerts, state)
    assert list(db.load_alerts(conn, "TMFR1")["kind"]) == ["support_bottom_third"]

    # Next minute: closes out of the box (previous close was in), and still in
    # cooldown for the zone rule.
    later = make_state(130, 140, 135, minute="2026-10-08 15:01")
    later.minute_bars = pd.concat([state.minute_bars, later.minute_bars], ignore_index=True)
    monitor.check_alerts(conn, config, alerts, later)
    kinds = sorted(db.load_alerts(conn, "TMFR1")["kind"])
    assert kinds == ["box_in_out", "support_bottom_third"]


def test_latest_broken_box_sends_each_entry_exit_and_deduplicates_repolls(conn, config, monkeypatch):
    alerts = ar.AlertConfig(ar.DEFAULT_CONFIG_PATH)
    config.enable_telegram = True
    messages = []
    monkeypatch.setattr(monitor.tg, "send_message", lambda message: messages.append(message) or True)
    # Older intact box is elsewhere; the broken newest box still extends on the chart.
    old = dict(SUPPORT_BOX, box_id=1, top=210, bottom=200)
    latest = dict(SUPPORT_BOX, box_id=2, top=110, bottom=100, state="broken")
    previous = make_state(120, 120, 120, minute="2026-10-08 15:00").minute_bars
    for stamp, close in [("15:01", 108), ("15:02", 120), ("15:03", 108), ("15:04", 99)]:
        state = make_state(close, close, close, boxes=[old, latest], minute=f"2026-10-08 {stamp}")
        state.minute_bars = pd.concat([previous, state.minute_bars], ignore_index=True)
        assert monitor.check_alerts(conn, config, alerts, state) == 1
        assert monitor.check_alerts(conn, config, alerts, state) == 0
        previous = state.minute_bars
    assert len(messages) == 4
    assert all("[15m] #2" in message for message in messages)
    assert sorted(db.load_alerts(conn, config.code)["kind"]) == [
        "box_enter", "box_enter", "box_in_out", "box_in_out"]


@pytest.mark.parametrize("timeframe, interval", [("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60)])
def test_check_alerts_sends_saved_curves_once_in_each_timeframe(
    conn, config, alerts, monkeypatch, timeframe, interval,
):
    config.enable_telegram = True
    messages = []
    monkeypatch.setattr(monitor.tg, "send_message", lambda message: messages.append(message) or True)
    start = at("15:00")
    line_id = db.insert_trendline(conn, config.code, [
        {"time": int((start + pd.Timedelta(minutes=i * interval)).timestamp()), "price": 100 + i * 10}
        for i in range(3)
    ], kind="curve", timeframe=timeframe)
    for minute, expected in [("17:00", 1), ("17:00", 0), ("17:01", 0)]:
        state = make_state(50, 50, 50, boxes=[], minute=f"2026-10-08 {minute}")
        state.minute_bars = pd.DataFrame({
            "minute_start": pd.date_range(start, at(minute), freq="min"),
        })
        for column in ("open", "high", "low", "close", "volume"):
            state.minute_bars[column] = 50.0
        state.hourly = monitor.sb.minute_bars_to_hourly(state.minute_bars)
        assert monitor.check_alerts(conn, config, alerts, state) == expected
    assert len(messages) == 1
    assert f"[{timeframe}]" in messages[0]
    history = db.load_alerts(conn, config.code)
    assert list(history["kind"]) == ["trendline_break_below_up"]
    assert list(history["box_type"]) == [f"line:{line_id}"]
