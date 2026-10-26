"""TAIFEX session mapping and 1-minute -> session-anchored 60-minute bars.

The session rules are copied verbatim from ``snr_strategy/src/backtest_tmf_dense_long.py``
and ``backtest_tmf_hourly_support_long.py`` so the boxes this project draws line up
with the existing TMF backtests.  Do not "tidy" the boundaries: the backtests treat
13:45 as exclusive, which drops the closing single-price auction, and that exclusion
is what keeps the two code bases agreeing.

    08:45 <= t < 13:45  ->  day,   session_date = t.normalize()
    t >= 15:00          ->  night, session_date = t.normalize() + 1 day
    t <  05:00          ->  night, session_date = t.normalize()
    otherwise           ->  outside any session, dropped

Hour buckets are anchored to the session open rather than to wall-clock hours, so
day bars start at 08:45/09:45/... and night bars at 15:00/16:00/....  One full
trading day is 300 day minutes + 840 night minutes = 1140 minute bars = 19 hourly
bars (5 day + 14 night); ``minute_bars_to_hourly`` is expected to reproduce that.

Note the input must already carry *minute-open* timestamps.  Shioaji's ``kbars``
labels bars by their closing minute (verified in ``doc/probe_findings.md``), so
``shioaji_client.fetch_minute_bars`` shifts them by -1 minute before anything here
sees them.
"""

from __future__ import annotations

from datetime import time

import pandas as pd

import market_clock

DAY_START = time(8, 45)
DAY_END = time(13, 45)  # exclusive: drops the 13:45:00 closing auction
NIGHT_START = time(15, 0)
NIGHT_END = time(5, 0)  # exclusive

MINUTES_PER_TRADING_DAY = 1140  # 300 day + 840 night
HOURS_PER_TRADING_DAY = 19  # 5 day + 14 night


def session_fields(timestamp: pd.Timestamp) -> tuple[str, pd.Timestamp] | None:
    """``(session_type, session_date)`` for a minute-open timestamp, or None."""
    clock = timestamp.time()
    if DAY_START <= clock < DAY_END:
        return "day", timestamp.normalize()
    if clock >= NIGHT_START:
        return "night", timestamp.normalize() + pd.Timedelta(days=1)
    if clock < NIGHT_END:
        return "night", timestamp.normalize()
    return None


def is_trading_time(timestamp: pd.Timestamp | None = None) -> bool:
    """Whether the clock is inside a session, so the monitor can idle otherwise.

    Only a clock test -- it does not know about holidays, so a holiday still looks
    like trading time.  The monitor notices a holiday by getting no fresh bars.
    """
    stamp = market_clock.now() if timestamp is None else timestamp
    return session_fields(stamp) is not None


def add_sessions(bars: pd.DataFrame, column: str = "minute_start") -> pd.DataFrame:
    """Attach ``session_type``/``session_date``/``session_id``, dropping non-session rows."""
    if bars.empty:
        out = bars.copy()
        for name in ("session_type", "session_date", "session_id"):
            out[name] = pd.Series(dtype="object")
        return out

    stamps = pd.to_datetime(bars[column])
    fields = stamps.map(session_fields)
    keep = fields.notna()
    out = bars.loc[keep].copy()
    if out.empty:
        # Every row fell outside a session -- a plausible input (a holiday pull, or
        # a window that only covers the 13:45-15:00 break).  Build the columns with
        # real dtypes so callers can keep using .dt without a special case.
        out["session_type"] = pd.Series(dtype="object")
        out["session_date"] = pd.Series(dtype="datetime64[ns]")
        out["session_id"] = pd.Series(dtype="object")
        return out

    kept = fields[keep]
    out["session_type"] = kept.map(lambda value: value[0])
    out["session_date"] = kept.map(lambda value: value[1])
    out["session_id"] = (
        out["session_date"].dt.strftime("%Y-%m-%d") + " " + out["session_type"]
    )
    return out


def session_anchor(session_id: str) -> pd.Timestamp:
    """The session's opening timestamp -- the inverse of :func:`session_fields`."""
    date_text, session_type = session_id.split()
    session_date = pd.Timestamp(date_text)
    if session_type == "day":
        return session_date + pd.Timedelta(hours=8, minutes=45)
    return session_date - pd.Timedelta(days=1) + pd.Timedelta(hours=15)


def hour_start_of(timestamp: pd.Timestamp) -> pd.Timestamp | None:
    """Which session-anchored hour bucket a timestamp falls in, or None if outside.

    The O(1) single-point version of the bucketing inside
    :func:`minute_bars_to_hourly`, for the live path that only has "now".
    """
    fields = session_fields(timestamp)
    if fields is None:
        return None
    session_type, session_date = fields
    session_id = f"{session_date:%Y-%m-%d} {session_type}"
    anchor = session_anchor(session_id)
    elapsed_hours = int((timestamp - anchor) // pd.Timedelta(hours=1))
    return anchor + pd.Timedelta(hours=elapsed_hours)


def minute_bars_to_hourly(minute_bars: pd.DataFrame) -> pd.DataFrame:
    """Aggregate minute bars into session-anchored 60-minute bars.

    Expects ``minute_start`` plus OHLCV (``amount`` optional) and, if already
    present, ``session_id``; otherwise sessions are derived here.  Emits
    ``timestamp`` = ``hour_start`` so the result drops straight into
    :func:`snr_core.collect_snr_boxes`, which keys off ``timestamp``.
    """
    return minute_bars_to_interval(minute_bars, 60).rename(columns={"bar_start": "hour_start"})


def minute_bars_to_interval(minute_bars: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate OHLCV in buckets anchored to each session's opening minute.

    Keep gaps empty and include the final partial bucket. ``timestamp`` and
    ``bar_start`` both identify the opening minute of the resulting candle.
    """
    if minutes not in (1, 5, 15, 60):
        raise ValueError("Unsupported candle interval")
    required = {"minute_start", "open", "high", "low", "close", "volume"}
    missing = required.difference(minute_bars.columns)
    if missing:
        raise ValueError(f"Missing minute columns: {sorted(missing)}")

    frame = minute_bars if "session_id" in minute_bars.columns else add_sessions(minute_bars)
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "timestamp",
                "bar_start",
                "session_id",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
            ]
        )

    frame = frame.copy()
    frame["minute_start"] = pd.to_datetime(frame["minute_start"])
    frame = frame.sort_values("minute_start", kind="stable")
    if "amount" not in frame.columns:
        frame["amount"] = 0.0

    anchors = {sid: session_anchor(sid) for sid in frame["session_id"].unique()}
    anchor_series = frame["session_id"].map(anchors)
    elapsed = ((frame["minute_start"] - anchor_series) // pd.Timedelta(minutes=minutes)).astype(int)
    frame["bar_start"] = anchor_series + pd.to_timedelta(elapsed * minutes, unit="min")

    grouped = (
        frame.groupby(["session_id", "bar_start"], sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            amount=("amount", "sum"),
            minutes=("minute_start", "count"),
        )
        .reset_index()
    )
    grouped["timestamp"] = grouped["bar_start"]
    return (
        grouped.sort_values("bar_start", kind="stable")
        .reset_index(drop=True)[
            [
                "timestamp",
                "bar_start",
                "session_id",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "minutes",
            ]
        ]
    )
