"""Integration tests for the monitor's per-cycle decisions.

These drive the real state machine, the real database and the real pattern
detectors; only Shioaji and Telegram are stubbed, since the point is the wiring
between them -- entering a box opens a visit and persists a book, and a rule hit
claims an alert row only outside its cooldown.
"""

from __future__ import annotations

import pandas as pd
import pytest

import alert_rules as ar
import db
import monitor
import patterns as pt
import snr_core as snr
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
