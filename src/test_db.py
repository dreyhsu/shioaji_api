"""Tests for the sqlite layer, especially the constraints that carry logic."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_schema(connection)
    yield connection
    connection.close()


def minute_frame(rows):
    return pd.DataFrame(
        [
            {
                "minute_start": pd.Timestamp(stamp),
                "session_id": "2026-10-08 night",
                "open": value,
                "high": value + 1,
                "low": value - 1,
                "close": value,
                "volume": 10.0,
                "amount": 100.0,
            }
            for stamp, value in rows
        ]
    )


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------
def test_schema_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    for _ in range(2):
        connection = db.connect(path)
        db.init_schema(connection)
        connection.close()


def test_migration_adds_the_source_column(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(box_visits)")}
    assert "source" in columns


# --------------------------------------------------------------------------------------
# Bars
# --------------------------------------------------------------------------------------
def test_minute_bars_upsert_and_round_trip(conn):
    db.upsert_minute_bars(conn, "TMFR1", minute_frame([("2026-10-08 15:00", 100.0)]))
    loaded = db.load_minute_bars(conn, "TMFR1")
    assert len(loaded) == 1
    assert loaded.iloc[0]["close"] == 100.0
    assert isinstance(loaded.iloc[0]["minute_start"], pd.Timestamp)


def test_re_upserting_a_minute_overwrites_it(conn):
    """kbars revises recent bars, so the newer pull has to win."""
    db.upsert_minute_bars(conn, "TMFR1", minute_frame([("2026-10-08 15:00", 100.0)]))
    db.upsert_minute_bars(conn, "TMFR1", minute_frame([("2026-10-08 15:00", 123.0)]))
    loaded = db.load_minute_bars(conn, "TMFR1")
    assert len(loaded) == 1
    assert loaded.iloc[0]["close"] == 123.0


def test_minute_bars_are_scoped_per_code(conn):
    db.upsert_minute_bars(conn, "TMFR1", minute_frame([("2026-10-08 15:00", 1.0)]))
    db.upsert_minute_bars(conn, "TXFR1", minute_frame([("2026-10-08 15:00", 2.0)]))
    assert len(db.load_minute_bars(conn, "TMFR1")) == 1
    assert db.load_minute_bars(conn, "TXFR1").iloc[0]["close"] == 2.0


def test_last_minute_start(conn):
    db.upsert_minute_bars(
        conn, "TMFR1", minute_frame([("2026-10-08 15:00", 1.0), ("2026-10-08 15:05", 2.0)])
    )
    assert db.last_minute_start(conn, "TMFR1") == pd.Timestamp("2026-10-08 15:05")
    assert db.last_minute_start(conn, "NOPE") is None


def test_hourly_bars_mark_the_forming_bar(conn):
    hourly = pd.DataFrame(
        {
            "hour_start": pd.to_datetime(["2026-10-08 15:00", "2026-10-08 16:00"]),
            "session_id": ["2026-10-09 night"] * 2,
            "open": [1.0, 2.0],
            "high": [2.0, 3.0],
            "low": [0.5, 1.5],
            "close": [1.5, 2.5],
            "volume": [10.0, 20.0],
            "amount": [0.0, 0.0],
            "minutes": [60, 12],
        }
    )
    db.upsert_hourly_bars(conn, "TMFR1", hourly, closed_through=0)
    loaded = db.load_hourly_bars(conn, "TMFR1")
    assert list(loaded["bar_closed"]) == [1, 0]
    assert (loaded["timestamp"] == loaded["hour_start"]).all()


# --------------------------------------------------------------------------------------
# Boxes
# --------------------------------------------------------------------------------------
def test_replace_boxes_swaps_the_whole_table(conn):
    def table(ids):
        return pd.DataFrame(
            {
                "box_id": ids,
                "box_type": ["support"] * len(ids),
                "state": ["fresh"] * len(ids),
                "top": [10.0] * len(ids),
                "bottom": [5.0] * len(ids),
                "pivot_price": [10.0] * len(ids),
                "atr_width": [5.0] * len(ids),
                "delta_volume": [1.0] * len(ids),
                "left_time": [pd.Timestamp("2026-10-08")] * len(ids),
                "right_time": [pd.Timestamp("2026-10-09")] * len(ids),
                "created_time": [pd.Timestamp("2026-10-08")] * len(ids),
                "state_time": [pd.NaT] * len(ids),
                "holds": [0] * len(ids),
                "breakouts": [0] * len(ids),
            }
        )

    db.replace_boxes(conn, "TMFR1", table([1, 2, 3]))
    db.replace_boxes(conn, "TMFR1", table([1]))
    assert len(db.load_boxes(conn, "TMFR1")) == 1


def test_box_override_patches_and_resets_the_stored_box(conn):
    created = pd.Timestamp("2026-10-08 10:00")
    frame = pd.DataFrame(
        {
            "box_id": [1], "box_type": ["support"], "state": ["fresh"],
            "top": [10.0], "bottom": [5.0], "pivot_price": [10.0], "atr_width": [5.0],
            "delta_volume": [1.0], "left_time": [created], "right_time": [created],
            "created_time": [created], "state_time": [pd.NaT], "holds": [0], "breakouts": [0],
        }
    )
    db.replace_boxes(conn, "TMFR1", frame)

    assert not db.set_box_override(conn, "TMFR1", "resistance", created, 12.0, 6.0)
    assert db.set_box_override(conn, "TMFR1", "support", created, 12.0, 6.0)
    assert db.set_box_override(conn, "TMFR1", "support", created, 13.0, 7.0)
    assert db.load_box_overrides(conn, "TMFR1") == {("support", created): (13.0, 7.0)}
    row = db.load_boxes(conn, "TMFR1").iloc[0]
    assert (row["top"], row["bottom"]) == (13.0, 7.0)

    assert db.delete_box_override(conn, "TMFR1", "support", created)
    assert not db.delete_box_override(conn, "TMFR1", "support", created)
    assert db.load_box_overrides(conn, "TMFR1") == {}
    row = db.load_boxes(conn, "TMFR1").iloc[0]
    assert (row["top"], row["bottom"]) == (10.0, 5.0)  # the first edit's original


def test_nat_state_time_survives_the_round_trip(conn):
    frame = pd.DataFrame(
        {
            "box_id": [1], "box_type": ["support"], "state": ["fresh"],
            "top": [10.0], "bottom": [5.0], "pivot_price": [10.0], "atr_width": [5.0],
            "delta_volume": [1.0], "left_time": [pd.Timestamp("2026-10-08")],
            "right_time": [pd.Timestamp("2026-10-09")], "created_time": [pd.Timestamp("2026-10-08")],
            "state_time": [pd.NaT], "holds": [0], "breakouts": [0],
        }
    )
    db.replace_boxes(conn, "TMFR1", frame)
    assert pd.isna(db.load_boxes(conn, "TMFR1").iloc[0]["state_time"])


# --------------------------------------------------------------------------------------
# Visits
# --------------------------------------------------------------------------------------
def open_a_visit(conn, **overrides):
    kwargs = dict(
        box_id=1, box_type="support", box_top=110.0, box_bottom=100.0, box_state="fresh",
        entered_at=pd.Timestamp("2026-10-08 15:00"),
        entry_minute_start=pd.Timestamp("2026-10-08 15:00"),
        entry_price=105.0, entry_zone_pct=0.5,
    )
    kwargs.update(overrides)
    return db.open_visit(conn, "TMFR1", **kwargs)


def test_visit_lifecycle(conn):
    visit_id = open_a_visit(conn)
    assert db.get_open_visit(conn, "TMFR1", "support")["id"] == visit_id
    db.close_visit(
        conn, visit_id,
        exited_at=pd.Timestamp("2026-10-08 15:30"),
        exit_minute_start=pd.Timestamp("2026-10-08 15:30"),
        exit_price=112.0, exit_reason="left_box",
    )
    assert db.get_open_visit(conn, "TMFR1", "support") is None


def test_touch_visit_tracks_extremes(conn):
    visit_id = open_a_visit(conn)
    db.touch_visit(conn, visit_id, price=101.0, zone_pct_value=0.1, minutes_in_box=2)
    db.touch_visit(conn, visit_id, price=109.0, zone_pct_value=0.9, minutes_in_box=3)
    row = conn.execute("SELECT * FROM box_visits WHERE id = ?", (visit_id,)).fetchone()
    assert row["min_zone_pct"] == pytest.approx(0.1)
    assert row["max_zone_pct"] == pytest.approx(0.9)
    assert row["min_price"] == 101.0
    assert row["max_price"] == 109.0
    assert row["minutes_in_box"] == 3


def test_backfilled_visits_are_not_restored_as_live(conn):
    """A restart must not adopt a historical visit as if price were still in the box."""
    open_a_visit(conn, source="backfill")
    assert db.get_open_visit(conn, "TMFR1", "support") is None


def test_bump_visit_counter(conn):
    visit_id = open_a_visit(conn)
    db.bump_visit_counter(conn, visit_id, "orderbook_rows", 5)
    db.bump_visit_counter(conn, visit_id, "orderbook_rows", 3)
    row = conn.execute("SELECT * FROM box_visits WHERE id = ?", (visit_id,)).fetchone()
    assert row["orderbook_rows"] == 8


def test_bump_visit_counter_refuses_arbitrary_columns(conn):
    visit_id = open_a_visit(conn)
    with pytest.raises(ValueError):
        db.bump_visit_counter(conn, visit_id, "code; DROP TABLE box_visits", 1)


# --------------------------------------------------------------------------------------
# Order book
# --------------------------------------------------------------------------------------
def book_row(event_ts, **overrides):
    row = {
        "event_ts": pd.Timestamp(event_ts),
        "source": "bidask_v1",
        "levels": 5,
        "bid_price": [1.0, 2.0],
        "bid_volume": [3, 4],
        "diff_bid_vol": [0, 0],
        "ask_price": [5.0, 6.0],
        "ask_volume": [7, 8],
        "diff_ask_vol": [0, 0],
        "bid_total_vol": 7,
        "ask_total_vol": 15,
    }
    row.update(overrides)
    return row


def test_orderbook_insert_is_idempotent(conn):
    """The monitor re-flushes overlapping windows every minute; duplicates must no-op."""
    rows = [book_row("2026-10-08 15:00:01")]
    assert db.insert_orderbook_rows(conn, "TMFR1", rows) == 1
    assert db.insert_orderbook_rows(conn, "TMFR1", rows) == 0
    assert db.count_orderbook_rows(conn, "TMFR1") == 1


def test_same_timestamp_from_a_different_source_is_kept(conn):
    """Live five-level and backfilled one-level data must not collide."""
    db.insert_orderbook_rows(conn, "TMFR1", [book_row("2026-10-08 15:00:01")])
    db.insert_orderbook_rows(
        conn, "TMFR1", [book_row("2026-10-08 15:00:01", source="ticks", levels=1)]
    )
    assert db.count_orderbook_rows(conn, "TMFR1") == 2


def test_orderbook_levels_round_trip_as_json(conn):
    db.insert_orderbook_rows(conn, "TMFR1", [book_row("2026-10-08 15:00:01")])
    row = conn.execute("SELECT * FROM orderbook_snapshots LIMIT 1").fetchone()
    assert json.loads(row["bid_price"]) == [1.0, 2.0]
    assert json.loads(row["ask_volume"]) == [7, 8]


def test_count_orderbook_rows_can_scope_to_a_visit(conn):
    visit_id = open_a_visit(conn)
    db.insert_orderbook_rows(conn, "TMFR1", [book_row("2026-10-08 15:00:01", visit_id=visit_id)])
    db.insert_orderbook_rows(conn, "TMFR1", [book_row("2026-10-08 15:00:02")])
    assert db.count_orderbook_rows(conn, "TMFR1") == 2
    assert db.count_orderbook_rows(conn, "TMFR1", visit_id) == 1


def test_empty_insert_is_a_no_op(conn):
    assert db.insert_orderbook_rows(conn, "TMFR1", []) == 0


# --------------------------------------------------------------------------------------
# Reversals
# --------------------------------------------------------------------------------------
def test_insert_and_find_a_reversal(conn):
    db.insert_reversal(
        conn, "TMFR1",
        {
            "hour_start": pd.Timestamp("2026-10-08 15:00"),
            "pattern": "long_lower_tail", "direction": "long",
            "minutes_into_bar": 12, "detail": {"wick_body_ratio": 4.2},
        },
    )
    assert db.reversal_exists(conn, "TMFR1", pd.Timestamp("2026-10-08 15:00"), "long_lower_tail", 12)
    assert not db.reversal_exists(conn, "TMFR1", pd.Timestamp("2026-10-08 15:00"), "long_lower_tail", 13)
    row = db.load_reversals(conn, "TMFR1").iloc[0]
    assert json.loads(row["detail"])["wick_body_ratio"] == 4.2


def test_the_same_pattern_can_re_fire_on_a_later_minute(conn):
    """The hourly bar is unfinished, so the shape legitimately re-appears."""
    for minute in (10, 20, 30):
        db.insert_reversal(
            conn, "TMFR1",
            {"hour_start": pd.Timestamp("2026-10-08 15:00"), "pattern": "long_lower_tail",
             "direction": "long", "minutes_into_bar": minute, "detail": {}},
        )
    assert len(db.load_reversals(conn, "TMFR1")) == 3


# --------------------------------------------------------------------------------------
# Telegram rate limiting
# --------------------------------------------------------------------------------------
def alert(conn, **overrides):
    kwargs = dict(
        hour_start=pd.Timestamp("2026-10-08 15:00"), box_type="support",
        box_id=1, visit_id=1, kind="entry", message="hello",
    )
    kwargs.update(overrides)
    return db.record_alert(conn, "TMFR1", **kwargs)


def test_one_alert_per_hour_per_side_per_kind(conn):
    assert alert(conn) is not None
    assert alert(conn) is None  # the slot is taken, so nothing is owed


def test_a_different_kind_gets_its_own_slot(conn):
    assert alert(conn, kind="entry") is not None
    assert alert(conn, kind="hourly") is not None
    assert alert(conn, kind="reversal:long_lower_tail") is not None


def test_the_other_side_gets_its_own_slot(conn):
    assert alert(conn, box_type="support") is not None
    assert alert(conn, box_type="resistance") is not None


def test_the_next_hour_gets_its_own_slot(conn):
    assert alert(conn, hour_start=pd.Timestamp("2026-10-08 15:00")) is not None
    assert alert(conn, hour_start=pd.Timestamp("2026-10-08 16:00")) is not None


def test_mark_alert_sent_records_the_outcome(conn):
    alert_id = alert(conn)
    db.mark_alert_sent(conn, alert_id, False)
    assert db.load_alerts(conn, "TMFR1").iloc[0]["ok"] == 0


# --------------------------------------------------------------------------------------
# Backfill progress
# --------------------------------------------------------------------------------------
def test_backfill_progress_round_trip(conn):
    assert db.backfill_done_keys(conn, "TMFR1", "orderbook_ticks") == set()
    db.mark_backfill_done(conn, "TMFR1", "orderbook_ticks", "support:1:202610081500", 42)
    assert db.backfill_done_keys(conn, "TMFR1", "orderbook_ticks") == {"support:1:202610081500"}


def test_marking_the_same_key_twice_updates_it(conn):
    db.mark_backfill_done(conn, "TMFR1", "orderbook_ticks", "k", 1)
    db.mark_backfill_done(conn, "TMFR1", "orderbook_ticks", "k", 99)
    row = conn.execute("SELECT * FROM backfill_progress").fetchone()
    assert row["rows"] == 99
