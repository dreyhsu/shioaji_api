"""Closed-minute proximity to confirmed hourly ST; no network/broker I/O."""
from __future__ import annotations

import math

import pandas as pd

from alert_rules import Candidate
import session_bars as sb
from supertrend import confirmed_supertrend

RULE = 'supertrend_near'
MINUTE = pd.Timedelta(minutes=1)
HOUR = pd.Timedelta(hours=1)


class SupertrendNearAlerts:
    def __init__(self):
        self._key = None
        self._hours = None

    def candidates(self, hourly, code, minute, now, config, *, mode):
        if (not config.telegram_enabled or not config.enabled(RULE)
                or minute is None or hourly.empty):
            return []
        start = pd.Timestamp(minute['minute_start'])
        end = start + MINUTE
        anchor = sb.hour_start_of(start)
        if anchor is None or not end <= pd.Timestamp(now) < end + MINUTE:
            return []
        # Only closed hourly OHLC contributes to the fingerprint: forming-hour
        # changes do not recompute the whole indicator every minute. Corrections
        # to older bars do invalidate it, as do missing/incomplete closed hours.
        source = hourly.copy() if 'timestamp' not in hourly else hourly
        if 'timestamp' not in source:
            source['timestamp'] = pd.to_datetime(source['hour_start'])
        closed = source.loc[source['timestamp'] + HOUR <= end]
        if closed.empty:
            return []
        key = pd.util.hash_pandas_object(
            closed[['timestamp', 'session_id', 'high', 'low', 'close', 'minutes']],
            index=True).to_numpy().tobytes()
        if key != self._key:
            self._hours = confirmed_supertrend(closed, end)
            self._key = key
        reading = self._hours.iloc[-1]
        # A missing entire hour must not carry an old regime into a later
        # completed bucket. First-hour session breaks use monitor calendar health.
        session_type, session_date = sb.session_fields(start)
        session_start = sb.session_anchor(f'{session_date:%Y-%m-%d} {session_type}')
        expected_end = anchor + HOUR if end == anchor + HOUR else anchor
        if expected_end == session_start:
            # At a new session's first hour, only the previous session's final
            # full hour is usable. Pending repairs can leave monitor healthy for
            # realtime purposes; they must not make a missing last hour vanish.
            prior_type = str(reading.session_id).split()[1]
            expected_end = sb.session_anchor(reading.session_id) + pd.Timedelta(
                hours=14 if prior_type == 'night' else 5)
        if reading.available_at != expected_end:
            return []
        close, line, direction = (float(minute['close']), float(reading.supertrend),
                                  float(reading.supertrend_direction))
        if (not all(math.isfinite(v) for v in (close, line, direction))
                or close <= 0 or line <= 0 or direction not in (-1, 1)):
            return []
        # Countertrend approach: below the bearish upper line, above the bullish
        # lower line. A touch counts; a close through the line does not.
        gap = (close - line) * direction
        threshold = config.rule(RULE)['distance_percent']
        if gap < 0 or gap > line * threshold / 100:
            return []
        percent = gap / line * 100
        label, side = ('空頭 ↓', '線下接近壓力') if direction == -1 else ('多頭 ↑', '線上接近支撐')
        return [Candidate(
            rule=RULE, key=f'supertrend-near:v1:{mode}:1h:{int(direction)}', once=False,
            message=(f'{code}｜1m 收盤接近 1h Supertrend\n'
                     f'1h Supertrend：{label}｜ST {line:,.2f}\n'
                     f'1m 收盤價：{close:,.2f}（{side}）\n'
                     f'距離：{gap:,.2f} 點／{percent:.4f}%（門檻 ≤ {threshold:g}%）\n'
                     f'1h：ATR 10／倍數 3，已收定\n'
                     f'指標收定時間：{reading.available_at:%Y-%m-%d %H:%M}\n'
                     f'確認時間：{end:%Y-%m-%d %H:%M}（Asia/Taipei）'),
        )]
