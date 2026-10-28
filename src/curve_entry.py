"""Pure directional curve-breakout signals from closed 1m candles."""
from __future__ import annotations

import math
import pandas as pd
import trendline as tl


def crossed_curve(minutes: pd.DataFrame, anchors: list[dict], now, action: str, timeframe="1h") -> bool:
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
    if timeframe not in tl.TIMEFRAME_MINUTES:
        return False
    bars = tl.source_bars(closed, timeframe)
    values = tl.values_at_times(bars, anchors, [previous["minute_start"], current["minute_start"]],
                               timeframe=timeframe).tolist()
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
