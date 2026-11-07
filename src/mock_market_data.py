"""Deterministic session-aligned decline, V rebound and pullback minute bars.

No broker, database or wall-clock dependency. Only the replay controller decides
how much of this frame to expose, so future candles never enter ATR or signals.
"""
from __future__ import annotations

import math
import pandas as pd

from session_bars import add_sessions
from mock_config import MarketConfig, load_mock_config


def v_shape_bars(settings: MarketConfig | None = None) -> pd.DataFrame:
    settings = settings if settings is not None else load_mock_config().market
    count = settings.decline_minutes + settings.rebound_minutes + settings.pullback_minutes
    # Generate enough session minutes even when the configured scenario exceeds 12 days.
    chunks, available = [], 0
    start = pd.Timestamp(settings.start_time)
    while available < count:
        candidates = pd.DataFrame({"minute_start": pd.date_range(start, periods=7 * 1440, freq="min")})
        chunk = add_sessions(candidates)
        # Keep the existing business-session-date mapping.
        chunk = chunk[chunk["session_date"].dt.dayofweek < 5]
        chunks.append(chunk)
        available += len(chunk)
        start += pd.Timedelta(days=7)
    frame = pd.concat(chunks, ignore_index=True).iloc[:count].copy()
    bottom = settings.start_price - settings.decline_minutes * settings.decline_points_per_minute
    peak = bottom + settings.rebound_minutes * settings.rebound_points_per_minute

    def distance(elapsed, duration, rate):
        # A bounded quadratic easing preserves phase endpoints and direction.
        # Zero keeps the original deterministic linear scenario exactly intact.
        return rate * elapsed * (1 + settings.trend_curvature * (1 - elapsed / duration))

    closes = []
    for index in range(count):
        if index < settings.decline_minutes:
            value = settings.start_price - distance(index, settings.decline_minutes, settings.decline_points_per_minute)
        elif index < settings.decline_minutes + settings.rebound_minutes:
            value = bottom + distance(index - settings.decline_minutes, settings.rebound_minutes, settings.rebound_points_per_minute)
        else:
            value = peak - distance(index - settings.decline_minutes - settings.rebound_minutes,
                                    settings.pullback_minutes, settings.pullback_points_per_minute)
        closes.append(round(value + settings.wave_amplitude * math.sin(index / settings.wave_scale_minutes)))
    frame["close"] = closes
    frame["open"] = frame["close"].shift(1).fillna(frame["close"].iloc[0])
    frame["high"] = frame[["open", "close"]].max(axis=1) + settings.wick_points
    frame["low"] = frame[["open", "close"]].min(axis=1) - settings.wick_points
    frame["volume"] = [settings.volume_base + i % settings.volume_cycle_minutes for i in range(count)]
    frame["amount"] = frame["close"] * frame["volume"]
    return frame
