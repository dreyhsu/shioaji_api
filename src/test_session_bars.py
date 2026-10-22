"""Tests for the TAIFEX session mapping and the minute -> hourly fold."""

from __future__ import annotations

import pandas as pd
import pytest

import session_bars as sb


def minutes(start, end, **columns):
    index = pd.date_range(start, end, freq="min")
    frame = pd.DataFrame({"minute_start": index})
    defaults = {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0, "amount": 10.0}
    defaults.update(columns)
    for name, value in defaults.items():
        frame[name] = value
    return frame


# --------------------------------------------------------------------------------------
# session_fields
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "clock,expected",
    [
        ("08:44:59", None),           # before the day session opens
        ("08:45:00", "day"),          # inclusive open
        ("13:44:59", "day"),
        ("13:45:00", None),           # exclusive close: drops the closing auction
        ("14:30:00", None),           # the afternoon break
        ("14:59:59", None),
        ("15:00:00", "night"),        # inclusive night open
        ("23:59:59", "night"),
        ("00:00:00", "night"),        # still the night session, next calendar day
        ("04:59:59", "night"),
        ("05:00:00", None),           # exclusive night close
        ("07:00:00", None),
    ],
)
def test_session_fields_boundaries(clock, expected):
    stamp = pd.Timestamp(f"2026-10-08 {clock}")
    fields = sb.session_fields(stamp)
    assert (None if fields is None else fields[0]) == expected


def test_day_session_dates_itself():
    _, session_date = sb.session_fields(pd.Timestamp("2026-10-08 10:00"))
    assert session_date == pd.Timestamp("2026-10-08")


def test_evening_night_belongs_to_the_next_trading_day():
    """15:00 on the 7th is part of trading day the 8th, matching api.ticks."""
    _, session_date = sb.session_fields(pd.Timestamp("2026-10-07 15:00"))
    assert session_date == pd.Timestamp("2026-10-08")


def test_after_midnight_night_keeps_its_trading_day():
    _, session_date = sb.session_fields(pd.Timestamp("2026-10-08 02:00"))
    assert session_date == pd.Timestamp("2026-10-08")


def test_night_session_spanning_midnight_is_one_session_id():
    frame = minutes("2026-10-07 23:58", "2026-10-08 00:02")
    tagged = sb.add_sessions(frame)
    assert tagged["session_id"].nunique() == 1
    assert tagged["session_id"].iloc[0] == "2026-10-08 night"


def test_add_sessions_drops_non_session_minutes():
    frame = minutes("2026-10-08 13:40", "2026-10-08 15:05")
    tagged = sb.add_sessions(frame)
    clocks = pd.to_datetime(tagged["minute_start"]).dt.strftime("%H:%M")
    assert "13:44" in set(clocks)
    assert "13:45" not in set(clocks)
    assert "14:30" not in set(clocks)
    assert "15:00" in set(clocks)


def test_add_sessions_on_empty_frame_keeps_the_columns():
    tagged = sb.add_sessions(pd.DataFrame(columns=["minute_start", "close"]))
    assert {"session_type", "session_date", "session_id"} <= set(tagged.columns)


# --------------------------------------------------------------------------------------
# anchors and hour buckets
# --------------------------------------------------------------------------------------
def test_session_anchor_inverts_session_fields():
    assert sb.session_anchor("2026-10-08 day") == pd.Timestamp("2026-10-08 08:45")
    assert sb.session_anchor("2026-10-08 night") == pd.Timestamp("2026-10-07 15:00")


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-10-08 08:45", "2026-10-08 08:45"),   # buckets are anchored to :45
        ("2026-10-08 09:44", "2026-10-08 08:45"),
        ("2026-10-08 09:45", "2026-10-08 09:45"),
        ("2026-10-08 13:44", "2026-10-08 12:45"),   # last day bucket
        ("2026-10-07 15:00", "2026-10-07 15:00"),   # night buckets land on :00
        ("2026-10-08 04:59", "2026-10-08 04:00"),
    ],
)
def test_hour_start_of(stamp, expected):
    assert sb.hour_start_of(pd.Timestamp(stamp)) == pd.Timestamp(expected)


def test_hour_start_of_outside_a_session_is_none():
    assert sb.hour_start_of(pd.Timestamp("2026-10-08 14:00")) is None


def test_is_trading_time_follows_the_clock():
    assert sb.is_trading_time(pd.Timestamp("2026-10-08 10:00")) is True
    assert sb.is_trading_time(pd.Timestamp("2026-10-08 14:00")) is False


# --------------------------------------------------------------------------------------
# minute -> hourly
# --------------------------------------------------------------------------------------
@pytest.fixture
def full_trading_day():
    """One complete trading day: 300 day minutes + 840 night minutes."""
    day = minutes("2026-10-08 08:45", "2026-10-08 13:44")
    night = minutes("2026-10-08 15:00", "2026-10-09 04:59")
    return pd.concat([day, night], ignore_index=True)


def test_full_day_is_1140_minutes(full_trading_day):
    assert len(full_trading_day) == sb.MINUTES_PER_TRADING_DAY


def test_full_day_folds_into_19_hourly_bars(full_trading_day):
    hourly = sb.minute_bars_to_hourly(full_trading_day)
    assert len(hourly) == sb.HOURS_PER_TRADING_DAY
    assert (hourly["minutes"] == 60).all()


def test_day_and_night_buckets_sit_on_different_phases(full_trading_day):
    hourly = sb.minute_bars_to_hourly(full_trading_day)
    day = hourly[hourly["session_id"].str.endswith("day")]["hour_start"]
    night = hourly[hourly["session_id"].str.endswith("night")]["hour_start"]
    assert len(day) == 5 and (day.dt.minute == 45).all()
    assert len(night) == 14 and (night.dt.minute == 0).all()


def test_hourly_ohlcv_aggregates_correctly():
    frame = minutes("2026-10-08 08:45", "2026-10-08 09:44")
    frame["open"] = range(60)
    frame["close"] = [value * 2 for value in range(60)]
    frame["high"] = 999.0
    frame["low"] = -5.0
    frame["volume"] = 2.0
    hourly = sb.minute_bars_to_hourly(frame)
    assert len(hourly) == 1
    bar = hourly.iloc[0]
    assert bar["open"] == 0          # first minute's open
    assert bar["close"] == 118       # last minute's close
    assert bar["high"] == 999.0
    assert bar["low"] == -5.0
    assert bar["volume"] == 120.0    # summed, not averaged


def test_timestamp_mirrors_hour_start_for_the_indicator(full_trading_day):
    """collect_snr_boxes keys off `timestamp`, so the fold must provide it."""
    hourly = sb.minute_bars_to_hourly(full_trading_day)
    assert (hourly["timestamp"] == hourly["hour_start"]).all()


def test_minute_bars_to_hourly_requires_columns():
    with pytest.raises(ValueError, match="Missing minute columns"):
        sb.minute_bars_to_hourly(pd.DataFrame({"minute_start": []}))


def test_empty_input_gives_empty_output():
    frame = minutes("2026-10-08 14:00", "2026-10-08 14:30")  # all outside sessions
    assert sb.minute_bars_to_hourly(frame).empty


def test_missing_minutes_make_a_short_bucket():
    """Quiet night minutes simply have no kbar, so a bucket can hold fewer than 60."""
    frame = minutes("2026-10-08 15:00", "2026-10-08 15:29")
    hourly = sb.minute_bars_to_hourly(frame)
    assert len(hourly) == 1
    assert hourly.iloc[0]["minutes"] == 30
