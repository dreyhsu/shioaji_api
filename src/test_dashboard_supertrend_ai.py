"""Closed-candle alignment, shared source calculations and cache corrections."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard
import session_bars as sb
from supertrend_ai import calculate_supertrend_ai, confirmed_supertrend_ai
from test_dashboard import minute_frame


@pytest.fixture
def source(monkeypatch):
    minutes = minute_frame('2026-11-02 15:00', 720)
    prices = 20000 + 600 * np.sin(np.arange(len(minutes)) / 80)
    minutes['open'], minutes['close'] = prices, prices + 1
    minutes['high'], minutes['low'] = prices + 4, prices - 4
    hourly = sb.minute_bars_to_hourly(minutes)
    bars = sb.minute_bars_to_interval(minutes, 15)
    data = dict(code='TEST', minute_bars=minutes, last_minute=minutes.iloc[-1].to_dict(),
                hourly=hourly, chart_bars=bars, visible=bars.copy(),
                supertrend_cache={}, boxes=pd.DataFrame(), box_overrides={}, trendlines=[])
    monkeypatch.setattr(dashboard, '_dashboard_now', lambda: pd.Timestamp('2026-11-03 03:00'))
    dashboard._AI_CACHE.clear()
    yield data
    dashboard._AI_CACHE.clear()


def test_payload_matches_pine_port_and_has_no_reversal_scores(source):
    payload = dashboard._supertrend_ai_payload(source, '15m')
    expected = calculate_supertrend_ai(source['chart_bars'])
    valid = expected['supertrend'].notna()
    points = [p for p in payload['data'] if 'value' in p]
    assert payload['parameters']['cluster'] == 'Best'
    assert payload['confirmed_only']
    assert [p['time'] for p in payload['data']] == dashboard._epochs(source['visible']['timestamp'])
    np.testing.assert_allclose([p['value'] for p in points], expected.loc[valid, 'supertrend'])
    assert {p['direction'] for p in points} == {-1, 1}
    assert all(set(p) <= {'time', 'value', 'direction'} for p in payload['data'])


@pytest.mark.parametrize('timeframe', ['1m', '5m', '1h'])
def test_other_timeframes_do_not_compute_ai(source, monkeypatch, timeframe):
    monkeypatch.setattr(dashboard, 'confirmed_supertrend_ai', lambda *a: pytest.fail('wrong timeframe'))
    assert dashboard._supertrend_ai_payload(source, timeframe) is None


def test_forming_bar_mutations_do_not_change_confirmed_values(source, monkeypatch):
    monkeypatch.setattr(dashboard, '_dashboard_now', lambda: pd.Timestamp('2026-11-03 02:53'))
    source['last_minute']['minute_start'] = pd.Timestamp('2026-11-03 02:52')
    before = dashboard._supertrend_ai_payload(source, '15m')
    assert before['data'][-1] == {'time': dashboard._epoch('2026-11-03 02:45')}
    source['chart_bars'].loc[source['chart_bars'].index[-1], ['high', 'close']] += 10000
    assert dashboard._supertrend_ai_payload(source, '15m') == before


def test_cache_reuses_closed_inputs_but_invalidates_historical_corrections(source, monkeypatch):
    original = dashboard.confirmed_supertrend_ai
    calls = []
    def calculate(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(dashboard, 'confirmed_supertrend_ai', calculate)
    before = dashboard._supertrend_ai_payload(source, '15m')
    dashboard._supertrend_ai_payload(source, '15m')
    assert len(calls) == 1
    source['chart_bars'].loc[source['chart_bars'].index[-1], ['high', 'close']] += 1000
    after = dashboard._supertrend_ai_payload(source, '15m')
    assert len(calls) == 2
    assert before['data'][-1] != after['data'][-1]


def test_incomplete_and_missing_buckets_reseed_instead_of_bridging(source):
    bars = source['chart_bars'].copy()
    bars.loc[20, 'minutes'] = 14
    actual = confirmed_supertrend_ai(bars, '2026-11-03 03:00')
    assert pd.isna(actual.loc[20, 'supertrend'])
    np.testing.assert_allclose(actual.loc[21:, 'supertrend'], calculate_supertrend_ai(bars.loc[21:])['supertrend'], equal_nan=True)
    missing = source['chart_bars'].drop(index=20)
    actual = confirmed_supertrend_ai(missing, '2026-11-03 03:00')
    np.testing.assert_allclose(actual.loc[21:, 'supertrend'], calculate_supertrend_ai(missing.loc[21:])['supertrend'], equal_nan=True)


def test_normal_session_break_keeps_indicator_history(source):
    bars = source['chart_bars'].copy()
    bars.loc[24:, 'timestamp'] += pd.Timedelta(hours=12)
    bars.loc[24:, 'session_id'] = '2026-11-03 day'
    actual = confirmed_supertrend_ai(bars, '2026-11-04')
    np.testing.assert_allclose(actual['supertrend'], calculate_supertrend_ai(bars)['supertrend'], equal_nan=True)


def test_api_attaches_ai_only_to_15m_and_preserves_1h_overlay(source, monkeypatch):
    base = deepcopy(source)
    base['visible'] = base['hourly'].copy()
    monkeypatch.setattr(dashboard, '_cached_load', lambda *a, **kw: base)
    client = TestClient(dashboard.app)
    for timeframe in ('15m', '1h', '5m', '1m'):
        payload = client.get('/api/chart', params={'code': 'TEST', 'timeframe': timeframe}).json()
        assert payload['supertrend']['timeframe'] == '1h'
        if timeframe == '15m':
            assert payload['supertrend_ai']['data']
            assert [p['time'] for p in payload['supertrend_ai']['data']] == [p['time'] for p in payload['bars']]
        else:
            assert payload['supertrend_ai'] is None
