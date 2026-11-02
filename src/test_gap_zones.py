"""Gap geometry, closed-candle confirmation and persisted notification claims."""
import pandas as pd

import alert_rules as ar
import dashboard
import db
import gap_zones as gaps
import monitor
import session_bars as sb


def candles(prices):
    stamps = pd.date_range('2026-10-30 15:00', periods=len(prices), freq='h')
    return pd.DataFrame([dict(timestamp=stamp, hour_start=stamp,
        session_id='2026-10-31 night', minutes=60,
        low=low, high=high, open=low, close=high, volume=10)
        for stamp, (low, high) in zip(stamps, prices)])


def test_up_gap_entry_fill_and_fixed_edges():
    bars = candles([(100, 110), (120, 130), (115, 125), (109, 122), (90, 105)])
    zone = gaps.collect(bars, '2026-10-30 20:00')[0]
    assert (zone['bottom'], zone['top']) == (110, 120)
    assert zone['entered_at'] == pd.Timestamp('2026-10-30 18:00')
    assert zone['filled_at'] == pd.Timestamp('2026-10-30 19:00')
    assert zone['right_time'] == pd.Timestamp('2026-10-30 18:00')


def test_down_gap_and_exact_far_edge_fills():
    bars = candles([(120, 130), (100, 110), (105, 119), (110, 120)])
    zone = gaps.collect(bars, '2026-10-30 19:00')[0]
    assert zone['direction'] == 'down'
    assert (zone['bottom'], zone['top']) == (110, 120)
    assert zone['filled_at'] == pd.Timestamp('2026-10-30 19:00')


def test_oct30_open_gap_across_night_and_day_and_no_historical_notification(tmp_path, monkeypatch):
    # Regression values inspected read-only from TMFR1: the 08:45 gap is still
    # open when later candles enter it without reaching the prior low.
    bars = candles([(45856, 46522), (44910, 45579), (45400, 45700)])
    bars['timestamp'] = pd.to_datetime(['2026-10-30 04:00', '2026-10-30 08:45', '2026-10-30 12:45'])
    bars['hour_start'] = bars['timestamp']
    bars['session_id'] = ['2026-10-30 night', '2026-10-30 day', '2026-10-30 day']
    zone, = gaps.collect(bars, '2026-11-02 09:30')
    assert (zone['bottom'], zone['top'], zone['filled_at']) == (45579, 45856, None)
    assert zone['created_at'] == pd.Timestamp('2026-10-30 09:45')
    alerts = ar.AlertConfig(tmp_path / 'alerts.json')
    assert len(list(gaps.alert_candidates([zone], 'TMFR1', '2026-10-30 09:44', alerts))) == 1
    assert not list(gaps.alert_candidates([zone], 'TMFR1', '2026-11-02 09:29', alerts))
    monkeypatch.setattr(dashboard, '_dashboard_now', lambda: pd.Timestamp('2026-11-02 09:30'))
    payload, = dashboard._gaps_payload(dict(hourly=bars, visible=bars))
    assert payload['box_id'] == '2026-10-30T08:45:00' and payload['open']


def test_forming_candles_cannot_create_or_fill():
    bars = candles([(100, 110), (120, 130), (100, 135)])
    assert gaps.collect(bars, '2026-10-30 16:59') == []
    zone = gaps.collect(bars, '2026-10-30 17:59')[0]
    assert zone['filled_at'] is None
    assert zone['entered_at'] is None


def test_touching_ranges_and_missing_bars_are_not_gaps():
    assert gaps.collect(candles([(100, 110), (110, 120)]), '2026-10-30 18:00') == []
    bars = candles([(100, 110), (120, 130)])
    bars.loc[1, 'timestamp'] += pd.Timedelta(hours=1)
    assert gaps.collect(bars, '2026-10-30 19:00') == []
    bars = candles([(100, 110), (120, 130)])
    bars.loc[1, 'minutes'] = 59
    assert gaps.collect(bars, '2026-10-30 19:00') == []


def test_alert_claim_survives_restart_and_switches(tmp_path, monkeypatch):
    alerts = ar.AlertConfig(tmp_path / 'alerts.json')
    config = monitor.MonitorConfig(db_path=tmp_path / 'market.db')
    zones = gaps.collect(candles([(100, 110), (120, 130)]), '2026-10-30 17:00')
    sent = []
    monkeypatch.setattr(monitor.tg, 'send_message', lambda message: sent.append(message) or True)
    with db.session(config.db_path) as conn:
        candidates = list(gaps.alert_candidates(zones, config.code, '2026-10-30 16:59', alerts))
        assert len(candidates) == 1
        assert monitor.send_rule(conn, config, alerts, candidates[0], '2026-10-30 16:59')
    with db.session(config.db_path) as conn:
        assert not monitor.send_rule(conn, config, alerts, candidates[0], '2026-10-30 17:00')
    assert len(sent) == 1
    assert not list(gaps.alert_candidates(zones, config.code, '2026-10-30 18:00', alerts))
    alerts.data['gap_formed']['enabled'] = False
    assert not list(gaps.alert_candidates(zones, config.code, '2026-10-30 16:59', alerts))


def test_chart_clips_old_open_gap_without_changing_prices(monkeypatch):
    bars = candles([(100, 110), (120, 130), (125, 140)])
    monkeypatch.setattr(dashboard, '_dashboard_now', lambda: pd.Timestamp('2026-10-30 18:00'))
    payload = dashboard._gaps_payload(dict(hourly=bars, visible=bars.iloc[-1:]))
    assert len(payload) == 1
    assert payload[0]['box_type'] == 'gap'
    assert payload[0]['open']
    assert payload[0]['left_time'] == dashboard._epoch(bars.iloc[-1].timestamp)
    assert (payload[0]['bottom'], payload[0]['top']) == (110, 120)


def test_incomplete_closed_hour_does_not_fill():
    bars = candles([(100, 110), (120, 130), (100, 135)])
    bars.loc[2, 'minutes'] = 59
    assert gaps.collect(bars, '2026-10-30 18:00')[0]['filled_at'] is None


def test_monitor_wires_gap_events_and_deduplicates(tmp_path, monkeypatch):
    bars = candles([(100, 110), (120, 130)])
    minute = pd.Series(dict(minute_start=pd.Timestamp('2026-10-30 16:59'),
        open=125, high=130, low=120, close=125, volume=10))
    state = monitor.MarketState(pd.DataFrame([minute]), bars, pd.DataFrame(), 1,
        None, minute, None, bars.iloc[-1])
    alerts = ar.AlertConfig(tmp_path / 'alerts.json')
    config = monitor.MonitorConfig(db_path=tmp_path / 'market.db')
    sent = []
    monkeypatch.setattr(monitor.market_clock, 'now', lambda: pd.Timestamp('2026-10-30 17:00'))
    monkeypatch.setattr(monitor.tg, 'send_message', lambda message: sent.append(message) or True)
    with db.session(config.db_path) as conn:
        assert monitor.check_alerts(conn, config, alerts, state) == 1
        assert monitor.check_alerts(conn, config, alerts, state) == 0
    assert '向上跳空缺口形成' in sent[0]


def test_chart_timeframes_share_hourly_gaps(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    prices = [(100, 110)] * 60 + [(120, 130)] * 60 + [(125, 140)] * 30
    stamps = pd.date_range('2026-10-30 15:00', periods=len(prices), freq='min')
    minutes = pd.DataFrame([dict(minute_start=stamp, open=low, close=high,
        low=low, high=high, volume=1, amount=0)
        for stamp, (low, high) in zip(stamps, prices)])
    path = tmp_path / 'market.db'
    with db.session(path) as conn:
        db.upsert_minute_bars(conn, 'TMFR1', sb.add_sessions(minutes))
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', path)
    monkeypatch.setattr(dashboard, '_dashboard_now', lambda: pd.Timestamp('2026-10-30 17:30'))
    dashboard._LOAD_CACHE.clear()
    client = TestClient(dashboard.app)
    try:
        for timeframe in ['1m', '5m', '15m', '1h']:
            response = client.get(f'/api/chart?code=TMFR1&days=30&timeframe={timeframe}')
            assert response.status_code == 200
            zone, = response.json()['gaps']
            assert zone['box_id'] == '2026-10-30T16:00:00'
            assert (zone['bottom'], zone['top'], zone['open']) == (110, 120, True)
    finally:
        dashboard._LOAD_CACHE.clear()
