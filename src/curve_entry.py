"""Pure directional curve-breakout signals from closed 1m candles."""
from __future__ import annotations

import math
import pandas as pd
import session_bars as sb
import trendline as tl


def curve_price_at(hourly, points, stamp):
    bucket = sb.hour_start_of(pd.Timestamp(stamp))
    if bucket is None:
        return None
    matches = hourly.index[pd.to_datetime(hourly["hour_start"]) == bucket]
    if len(matches) == 0:
        return None
    fraction = (pd.Timestamp(stamp) - bucket).total_seconds() / 3600
    return tl.value_at(points, int(matches[0]) + fraction, "curve")


def crossed_curve(minutes: pd.DataFrame, anchors: list[dict], now, action: str) -> bool:
    if action not in {"Buy", "Sell"}:
        raise ValueError("action must be Buy or Sell")
    if minutes.empty:
        return False
    closed = minutes[minutes["minute_start"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
    if len(closed) < 2:
        return False
    previous, current = closed.iloc[-2], closed.iloc[-1]
    if (pd.Timestamp(now) - (pd.Timestamp(current["minute_start"]) + pd.Timedelta(minutes=1))).total_seconds() > 180:
        return False
    # Session breaks/missing candles cannot manufacture an apparent crossing.
    if pd.Timestamp(current["minute_start"]) - pd.Timestamp(previous["minute_start"]) != pd.Timedelta(minutes=1):
        return False
    hourly = sb.minute_bars_to_hourly(closed).reset_index(drop=True)
    points = tl.anchor_indices(hourly, anchors, "curve")
    if not points:
        return False
    values = [curve_price_at(hourly, points, row["minute_start"]) for row in (previous, current)]
    if any(value is None or not math.isfinite(value) for value in values):
        return False
    before, after = float(previous["close"]), float(current["close"])
    if not math.isfinite(before) or not math.isfinite(after):
        return False
    if action == "Buy":
        return before <= values[0] and after > values[1]
    return before >= values[0] and after < values[1]


def crossed_above_curve(minutes: pd.DataFrame, anchors: list[dict], now) -> bool:
    """Compatibility entry point for the original upward-only signal."""
    return crossed_curve(minutes, anchors, now, "Buy")
