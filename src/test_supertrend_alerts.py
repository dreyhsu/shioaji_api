"""Closed-candle AI disagreement and actual 2026-11-19 notification replay.

The CSV contains real TMFR1 unified-DB minutes from 2026-11-18 08:45 through
2026-11-19 12:29 (Asia/Taipei), exported read-only on 2026-11-19. Its bounded
warmup reproduces the direction transition; full-history values are verified
separately against the local DB. No broker or Telegram network calls are made.
"""
from pathlib import Path
import threading

import pandas as pd
import pytest

import alert_rules as ar
import db
import monitor
from monitor_notify import MonitorNotifier
import session_bars as sb
import supertrend_alerts as alerts
from supertrend import confirmed_supertrend
from supertrend_ai import confirmed_supertrend_ai


@pytest.fixture(scope='module')
def recorded_minutes():
    path = Path(__file__).with_name('testdata') / 'supertrend_tmfr1_20261119.csv'
    return sb.add_sessions(pd.read_csv(path, parse_dates=['minute_start']))


@pytest.fixture
def config(tmp_path):
    return ar.AlertConfig(tmp_path / 'defaults.json')


def candidates(minutes, config, end='2026-11-19 12:00', *, now=None):
    end = pd.Timestamp(end)
    bars = minutes[minutes.minute_start < end]
    return alerts.alert_candidates(
        bars, sb.minute_bars_to_hourly(bars), 'TMFR1',
        end - pd.Timedelta(minutes=1), now if now is not None else end, config)


def test_real_1145_candle_alerts_at_1200_using_ai_not_standard(recorded_minutes, config):
    end = pd.Timestamp('2026-11-19 12:00')
    bars = recorded_minutes[recorded_minutes.minute_start < end]
    quarters = sb.minute_bars_to_interval(bars, 15)
    ai = confirmed_supertrend_ai(quarters, end)
    standard = confirmed_supertrend(quarters, end, 15)
    hourly = confirmed_supertrend(sb.minute_bars_to_hourly(bars), end)
    assert list(ai.supertrend_direction.tail(2)) == [-1, 1]
    assert list(standard.supertrend_direction.tail(2)) == [-1, -1]
    assert hourly.iloc[-1].supertrend_direction == -1
    hits = candidates(recorded_minutes, config)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == 'supertrend_opposite' and hit.once
    assert hit.key == 'supertrend:1h:15m-ai:2026-11-19T12:00:00:-1:1'
    assert '1h 標準 Supertrend：空頭' in hit.message
    assert '15m LuxAlgo SuperTrend AI：多頭' in hit.message
    assert '15m K 棒 2026-11-19 11:45～12:00' in hit.message
    assert '15m 收盤價 39,301.00' in hit.message
    assert '確認時間 2026-11-19 12:00 (Asia/Taipei)' in hit.message


@pytest.mark.parametrize('end', ['11:30', '11:45', '11:59', '12:15', '12:30'])
def test_no_alert_before_flip_or_during_same_disagreement(recorded_minutes, config, end):
    assert candidates(recorded_minutes, config, f'2026-11-19 {end}') == []


@pytest.mark.parametrize('now', ['11:59:59.999', '12:01:00', '13:00:00'])
def test_no_forming_or_late_replay_notifications(recorded_minutes, config, now):
    assert candidates(recorded_minutes, config, now=pd.Timestamp(f'2026-11-19 {now}')) == []


def test_normal_sync_delay_inside_close_minute_is_allowed(recorded_minutes, config):
    assert len(candidates(recorded_minutes, config, now=pd.Timestamp('2026-11-19 12:00:59'))) == 1


@pytest.mark.parametrize('missing', ['10:50', '11:35', '11:50'])
def test_incomplete_hour_previous_quarter_or_current_quarter_suppresses_alert(
        recorded_minutes, config, missing):
    bars = recorded_minutes[recorded_minutes.minute_start != pd.Timestamp(f'2026-11-19 {missing}')]
    assert candidates(bars, config) == []


def test_missing_whole_previous_quarter_does_not_bridge_gap(recorded_minutes, config):
    bars = recorded_minutes[~recorded_minutes.minute_start.between(
        '2026-11-19 11:30', '2026-11-19 11:44')]
    assert candidates(bars, config) == []


@pytest.mark.parametrize('switch', ['telegram_enabled', 'supertrend_opposite'])
def test_notification_switches(recorded_minutes, config, switch):
    if switch == 'telegram_enabled':
        config.data[switch] = False
    else:
        config.data[switch]['enabled'] = False
    assert candidates(recorded_minutes, config) == []


@pytest.mark.parametrize('hour_before,hour_after,ai_before,ai_after,expected', [
    (1, -1, 1, 1, True),   # The simultaneously closed hour creates divergence.
    (-1, 1, -1, -1, True),
    (-1, 1, -1, 1, False), # Both indicators flip together, remaining aligned.
    (-1, -1, 1, 1, False), # Existing divergence does not repeat.
    (float('nan'), -1, -1, 1, False), # Warmup is not a known regime transition.
])
def test_hourly_close_alignment_and_opposing_pair_transitions(
        recorded_minutes, config, monkeypatch,
        hour_before, hour_after, ai_before, ai_after, expected):
    quarter_starts = pd.to_datetime(['2026-11-19 11:15', '2026-11-19 11:30'])
    quarters = pd.DataFrame(dict(timestamp=quarter_starts,
        session_id=['2026-11-19 day'] * 2, close=[39000, 39155],
        supertrend=[38000, 38000], supertrend_direction=[ai_before, ai_after]))
    hours = pd.DataFrame(dict(
        available_at=pd.to_datetime(['2026-11-19 10:45', '2026-11-19 11:45']),
        supertrend=[40000, 40000], supertrend_direction=[hour_before, hour_after]))
    monkeypatch.setattr(alerts, 'confirmed_supertrend_ai', lambda *a: quarters.copy())
    monkeypatch.setattr(alerts, 'confirmed_supertrend', lambda *a: hours.copy())
    assert bool(candidates(recorded_minutes, config, '2026-11-19 11:45')) is expected


def test_real_candle_runs_through_monitor_claim_queue_delivery_and_restart_dedup(
        recorded_minutes, tmp_path, monkeypatch):
    path = tmp_path / 'replay.db'
    settings = tmp_path / 'alerts.json'
    settings.write_text('{"telegram_enabled": true, "supertrend_opposite": {"enabled": true}}',
                        encoding='utf-8')
    config = ar.AlertConfig(settings)
    cfg = monitor.MonitorConfig(code='TMFR1', db_path=path, alerts_config=settings)
    end = pd.Timestamp('2026-11-19 12:00')
    bars = recorded_minutes[recorded_minutes.minute_start < end]
    state = monitor.MarketState(
        minute_bars=bars, hourly=sb.minute_bars_to_hourly(bars), boxes=pd.DataFrame(),
        closed_through=-1, hourly_atr=None, latest_minute=bars.iloc[-1],
        forming_hour=None, prev_closed_hour=None)
    monkeypatch.setattr(monitor.market_clock, 'now', lambda: end + pd.Timedelta(seconds=6))
    delivered = threading.Event()
    calls = []

    def send(message):
        calls.append(message)
        delivered.set()
        return True

    with db.session(path) as conn:
        worker = MonitorNotifier(path, settings, send=send, logger=lambda _: None)
        try:
            assert monitor.check_alerts(conn, cfg, config, state, notifier=worker) == 1
            assert delivered.wait(3)
            # Join waits for the worker's persisted delivery status, not just HTTP.
            worker._queue.join()
            row = conn.execute('SELECT * FROM telegram_alerts').fetchone()
            assert row['kind'] == 'supertrend_opposite' and row['ok'] == 1
            assert row['hour_start'] == '2026-11-19 11:59:00'
            assert row['message'] == calls[0]
            assert monitor.check_alerts(conn, cfg, config, state, notifier=worker) == 0
        finally:
            assert worker.close(timeout=3)
    # A new DB connection and notifier must honor the same persistent claim.
    with db.session(path) as conn:
        worker = MonitorNotifier(path, settings, send=send, logger=lambda _: None)
        try:
            assert monitor.check_alerts(conn, cfg, config, state, notifier=worker) == 0
            assert conn.execute('SELECT COUNT(*) FROM telegram_alerts').fetchone()[0] == 1
        finally:
            assert worker.close(timeout=3)
    assert len(calls) == 1
