"""Confirmed 1h / 15m Supertrend disagreement alerts; no network or broker I/O."""
from __future__ import annotations

import pandas as pd

from alert_rules import Candidate
import session_bars as sb
from supertrend import confirmed_supertrend

RULE = 'supertrend_opposite'


def alert_candidates(minute_bars, hourly, code, minute_start, now, config):
    """One event on entry into each opposing pair, only at the live 15m close.

    Compare both indicators as they were known at each candle close, including
    a simultaneous hourly update. Require valid previous readings: warming up
    or recovering missing bars must not invent a reversal notification.
    """
    if not config.telegram_enabled or not config.enabled(RULE) or minute_bars.empty:
        return []
    latest = pd.Timestamp(minute_start)
    end = latest + pd.Timedelta(minutes=1)
    hour_start = sb.hour_start_of(latest)
    if hour_start is None or (end - hour_start) % pd.Timedelta(minutes=15):
        return []
    # Late historical repairs, startup history and closed sessions do not send
    # a backlog. Normal synchronization may arrive during the following minute.
    if not end <= pd.Timestamp(now) < end + pd.Timedelta(minutes=1):
        return []
    quarters = confirmed_supertrend(sb.minute_bars_to_interval(minute_bars, 15), end, 15)
    hours = confirmed_supertrend(hourly, end)
    if len(quarters) < 2 or hours.empty or quarters.iloc[-1]['available_at'] != end:
        return []
    previous, current = quarters.iloc[-2], quarters.iloc[-1]
    if (previous['session_id'] == current['session_id']
            and current['timestamp'] - previous['timestamp'] != pd.Timedelta(minutes=15)):
        return []
    pairs = pd.merge_asof(
        quarters.tail(2)[['available_at', 'supertrend', 'supertrend_direction', 'close']],
        hours[['available_at', 'supertrend', 'supertrend_direction']],
        on='available_at', direction='backward', suffixes=('_15m', '_1h'),
    )
    directions = pairs[['supertrend_direction_1h', 'supertrend_direction_15m']]
    if directions.isna().any().any():
        return []
    before = tuple(directions.iloc[0].astype(int))
    after = tuple(directions.iloc[1].astype(int))
    if after[0] == after[1] or before == after:
        return []
    current = pairs.iloc[-1]
    labels = {1: '多頭 ↑', -1: '空頭 ↓'}
    return [Candidate(
        rule=RULE, key=f'supertrend:1h:15m:{end.isoformat()}:{after[0]}:{after[1]}',
        once=True,
        message=(f'{code}｜Supertrend 週期方向相反\n'
                 f'1h：{labels[after[0]]}｜ST {current.supertrend_1h:,.2f}\n'
                 f'15m：{labels[after[1]]}｜ST {current.supertrend_15m:,.2f}\n'
                 f'15m 收盤價 {current.close:,.2f}\n'
                 f'參數 ATR 10 / 倍數 3（兩週期皆已收定）\n'
                 f'確認時間 {end:%Y-%m-%d %H:%M} (Asia/Taipei)'),
    )]
