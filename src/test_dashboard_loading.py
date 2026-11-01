"""Ensure fast hourly reads preserve the exact full-minute reference output."""
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard
from dashboard_config import DashboardConfig
import db
import monitor
import session_bars as sb
from test_dashboard import minute_frame

pytestmark = pytest.mark.integration


@pytest.fixture
def chart(tmp_path, monkeypatch):
    path = tmp_path / 'hourly.db'
    bars = minute_frame('2026-10-01 08:45', 60 * 24 * 10)
    with db.session(path) as conn:
        db.upsert_minute_bars(conn, 'TMFR1', bars)
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', path)
    monkeypatch.setitem(dashboard.CONFIG, 'code', 'TMFR1')
    monkeypatch.setitem(dashboard.CONFIG, 'days', 30)
    monkeypatch.setitem(dashboard.CONFIG, 'dashboard_settings', DashboardConfig())
    dashboard._LOAD_CACHE.clear()
    yield path, bars, TestClient(dashboard.app)
    dashboard._LOAD_CACHE.clear()


def derive(path, bars):
    with db.session(path) as conn:
        revision = db.chart_versions(conn, 'TMFR1')[0]
        hourly = sb.minute_bars_to_hourly(bars)
        db.upsert_hourly_bars(conn, 'TMFR1', hourly, len(hourly) - 1)
        db.mark_dashboard_derived(conn, 'TMFR1', revision, bars, hourly)


def test_hourly_fast_path_matches_reference_without_loading_minutes(chart, monkeypatch):
    path, bars, client = chart
    reference = client.get('/api/chart?timeframe=1h').json()
    summary = client.get('/api/state').json()
    derive(path, bars)
    dashboard._LOAD_CACHE.clear()
    monkeypatch.setattr(db, 'load_minute_bars', lambda *args, **kwargs: pytest.fail('1h should not read all minutes'))
    result = client.get('/api/chart?timeframe=1h').json()
    assert result['bars'] == reference['bars']
    assert result['series'] == reference['series']
    state = client.get('/api/state').json()
    assert state['minute_bars'] == summary['minute_bars']
    assert state['hourly_atr'] == summary['hourly_atr']
    assert client.get('/api/trendlines').json() == []


def test_same_timestamp_revision_invalidates_cache_and_hourly_view(chart):
    path, bars, client = chart
    derive(path, bars)
    original = client.get('/api/chart').json()['bars'][-1]['close']
    with db.session(path) as conn:
        conn.execute('UPDATE minute_bars SET close=close+100, high=high+100 WHERE code=? AND minute_start=?',
                     ('TMFR1', db._text(bars.iloc[-1]['minute_start'])))
        conn.commit()
    result = client.get('/api/chart').json()
    assert result['bars'][-1]['close'] == original + 100


def test_hourly_curve_values_match_after_switching_to_fast_path(chart):
    path, bars, client = chart
    hourly = sb.minute_bars_to_hourly(bars)
    anchors = [dict(time=dashboard._epoch(row.timestamp), price=float(row.close))
               for row in hourly.iloc[[3, 12, 25]].itertuples()]
    with db.session(path) as conn:
        curve = db.insert_trendline(conn, 'TMFR1', anchors, kind='curve', timeframe='1h', label='測試曲線')
    before = client.get('/api/chart').json()['series']
    derive(path, bars)
    dashboard._LOAD_CACHE.clear()
    after = client.get('/api/chart').json()['series']
    assert before == after
    # A curve edit from another process also invalidates cached labels/geometry.
    with db.session(path) as conn:
        conn.execute('UPDATE trendlines SET label=? WHERE id=?', ('新名稱', curve))
        conn.commit()
    assert '新名稱' in client.get('/api/chart').json()['series'][-1]['label']


def test_smaller_timeframes_load_minutes_on_demand(chart, monkeypatch):
    path, bars, client = chart
    derive(path, bars)
    original = db.load_minute_bars
    calls = []
    def load(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(db, 'load_minute_bars', load)
    client.get('/api/chart?timeframe=1h')
    assert not calls
    assert client.get('/api/chart?timeframe=5m').status_code == 200
    assert len(calls) == 1


def test_public_dashboard_config_is_safe_and_defaults_apply(chart, monkeypatch):
    _, _, client = chart
    monkeypatch.setitem(dashboard.CONFIG, 'dashboard_settings', DashboardConfig(default_timeframe='5m', initial_bars=80))
    public = client.get('/api/dashboard-config').json()
    assert public['initial_bars'] == 80 and public['history_days'] == 30
    assert not {'host', 'port', 'db_path', 'trade_url', 'cache_seconds'} & public.keys()
    assert client.get('/api/chart').json()['timeframe'] == '5m'
    assert client.get('/api/chart?timeframe=1h').json()['timeframe'] == '1h'
