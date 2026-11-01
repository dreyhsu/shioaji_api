"""Price gaps between closed session-anchored hourly candles.

Shared by the chart and monitor. Original edges stay fixed; a filled gap stops
extending. Wick overlap counts as entry, reaching the far edge as full fill.
All timestamps are naive Asia/Taipei, like the unified database.
"""
from __future__ import annotations

import pandas as pd


def collect(hourly: pd.DataFrame, now) -> list[dict]:
    if len(hourly) < 2:
        return []
    bars = hourly.copy()
    if 'timestamp' not in bars:
        bars['timestamp'] = bars['hour_start']
    bars = bars.sort_values('timestamp')
    bars['end'] = pd.to_datetime(bars['timestamp']) + pd.Timedelta(hours=1)
    bars = bars[bars['end'] <= pd.Timestamp(now)]
    zones = []
    previous = None
    for bar in bars.itertuples(index=False):
        stamp = pd.Timestamp(bar.timestamp)
        if getattr(bar, 'minutes', 60) != 60:
            previous = bar
            continue
        # Advance existing gaps before creating this candle's gap: its own
        # boundary is not a return into the newly formed gap.
        for zone in zones:
            if zone['filled_at'] is not None:
                continue
            zone['right_time'] = stamp
            entered = bar.low <= zone['top'] if zone['direction'] == 'up' else bar.high >= zone['bottom']
            filled = bar.low <= zone['bottom'] if zone['direction'] == 'up' else bar.high >= zone['top']
            if entered and zone['entered_at'] is None:
                zone['entered_at'] = bar.end
            if filled:
                zone['filled_at'] = bar.end
        if previous is not None:
            # Do not turn missing intra-session bars into price gaps.
            same_session = getattr(bar, 'session_id', None) == getattr(previous, 'session_id', None)
            adjacent = not same_session or stamp - pd.Timestamp(previous.timestamp) == pd.Timedelta(hours=1)
            complete = getattr(bar, 'minutes', 60) == 60 and getattr(previous, 'minutes', 60) == 60
            direction = 'up' if bar.low > previous.high else 'down' if bar.high < previous.low else None
            if direction and adjacent and complete:
                zones.append(dict(
                    gap_id=stamp.isoformat(), direction=direction,
                    top=float(bar.low if direction == 'up' else previous.low),
                    bottom=float(previous.high if direction == 'up' else bar.high),
                    left_time=stamp, right_time=stamp, created_at=bar.end,
                    entered_at=None, filled_at=None,
                ))
        previous = bar
    return zones


def alert_candidates(zones, code, minute_start, config):
    """Only current confirmations; do not send a backlog on monitor startup."""
    from alert_rules import Candidate
    latest = pd.Timestamp(minute_start)
    labels = {'gap_formed': '形成', 'gap_entered': '首次進入', 'gap_filled': '完全回補'}
    for zone in zones:
        for rule, field in [('gap_formed', 'created_at'), ('gap_entered', 'entered_at'), ('gap_filled', 'filled_at')]:
            stamp = zone[field]
            # The final closed minute is end-1min; a poll just after the next
            # minute is also allowed. send_rule persists the once-only claim.
            if stamp is None or not config.enabled(rule):
                continue
            if not stamp - pd.Timedelta(minutes=1) <= latest <= stamp:
                continue
            direction = '向上' if zone['direction'] == 'up' else '向下'
            yield Candidate(rule=rule, key=f"gap:1h:{zone['gap_id']}", once=True,
                message=(f"{code}｜1h {direction}跳空缺口{labels[rule]}\n"
                         f"區間 {zone['bottom']:,.0f}–{zone['top']:,.0f}\n"
                         f"缺口時間 {zone['left_time']:%Y-%m-%d %H:%M}\n"
                         f"確認時間 {stamp:%Y-%m-%d %H:%M} (Asia/Taipei)"))
