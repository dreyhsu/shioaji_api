"""Tier boundaries, editable policy and protection that never moves backwards."""
import json

import pandas as pd
import pytest

from test_trading import fake_service
from test_profit_lock import enable_tick_lock
from test_independent_trade import rig


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_default_policy_matches_profit_table_and_intermediate_tier_prices(fake_service, action, direction):
    service, _, _ = fake_service
    row = dict(action=action, avg_fill_price=20000., contract_tick=1., profit_lock_enabled=True,
               profit_lock_peak_points=0., stop_price=20000 - direction * 1000)
    # 700 uses the 600 tier (2/3 retained), rather than interpolating a 200-point trail.
    cases = [(299, None), (300, 150), (400, 200), (499, 250), (500, 300),
             (599, 360), (600, 400), (700, 467), (799, 533), (800, 600),
             (999, 750), (1000, 800), (1499, 1200), (1500, 1300), (1600, 1387)]
    for peak, retained in cases:
        row.update(service._profit_lock_updates(row, 20000 + direction * peak))
        assert row['profit_lock_peak_points'] == peak
        if retained is None:
            assert not row.get('profit_lock_triggered_at') and not row.get('profit_lock_price')
        else:
            assert row['profit_lock_price'] == row['stop_price'] == 20000 + direction * retained
    frozen = dict(row)
    assert service._profit_lock_updates(row, 20000 + direction * 1500) == {}
    assert row == frozen


@pytest.mark.parametrize('tiers', [None, {}, [], [None], [dict(from_points=300, giveback_ratio=.5)],
    [dict(from_points=0, giveback_ratio=True)], [dict(from_points=0, giveback_ratio='0.5')],
    [dict(from_points=0, giveback_ratio=-.1)], [dict(from_points=0, giveback_ratio=1.1)],
    [dict(from_points=0, giveback_ratio=float('nan'))], [dict(from_points=0, giveback_ratio=float('inf'))],
    [dict(from_points=0, giveback_ratio=.5), dict(from_points=0, giveback_ratio=.4)],
    [dict(from_points=0, giveback_ratio=.5), dict(from_points=800, giveback_ratio=.25),
     dict(from_points=500, giveback_ratio=.4)],
    [dict(from_points=0, giveback_ratio=.5), dict(from_points=500, giveback_ratio=.6)],
    [dict(from_points=0, giveback_ratio=.5), dict(from_points=True, giveback_ratio=.4)],
    [dict(from_points=0, giveback_ratio=.5), dict(from_points=float('inf'), giveback_ratio=.4)],
    [dict(from_points=0, giveback_ratio=.5, misspelled=True)]])
def test_invalid_tier_edit_retains_whole_previous_policy_and_reports_error(fake_service, tiers):
    service, _, _ = fake_service
    valid = dict(profit_lock_trigger_points=450,
                 profit_lock_tiers=[dict(from_points=0, giveback_ratio=.45),
                                    dict(from_points=900, giveback_ratio=.2)])
    service.settings_path.write_text(json.dumps(valid))
    previous = service.profit_lock_settings()
    assert previous['profit_lock_config_error'] is None
    service.settings_path.write_text(json.dumps(dict(profit_lock_trigger_points=50, profit_lock_tiers=tiers)))
    rejected = service.profit_lock_settings()
    assert rejected == {**previous, 'profit_lock_config_error': 'invalid_config'}
    service.settings_path.write_text(json.dumps(valid))
    assert service.profit_lock_settings() == previous


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_policy_reloads_and_looser_edits_never_release_the_latched_floor(fake_service, action, direction):
    service, _, _ = fake_service
    row = dict(action=action, avg_fill_price=20000., contract_tick=1., profit_lock_enabled=True,
               profit_lock_peak_points=0., stop_price=20000 - direction * 1000)
    row.update(service._profit_lock_updates(row, 20000 + direction * 800))
    assert row['profit_lock_price'] == 20000 + direction * 600
    triggered = row['profit_lock_triggered_at']
    config = dict(profit_lock_trigger_points=2000,
                  profit_lock_tiers=[dict(from_points=0, giveback_ratio=.5)])
    service.settings_path.write_text(json.dumps(config))
    assert service._profit_lock_updates(row, 20000 + direction * 900) == {'profit_lock_peak_points': 900}
    row['profit_lock_peak_points'] = 900
    # Tightening applies to the tracked tick peak, not just the latest price.
    config['profit_lock_tiers'][0]['giveback_ratio'] = .1
    service.settings_path.write_text(json.dumps(config))
    row.update(service._profit_lock_updates(row, 20000 + direction * 850))
    assert row['profit_lock_price'] == row['stop_price'] == 20000 + direction * 810
    assert row['profit_lock_triggered_at'] == triggered
    assert service.profit_lock_settings()['profit_lock_trigger_points'] == 2000
    assert service._profit_lock_policy().order_settings(row['profit_lock_peak_points'])['profit_lock_giveback_ratio'] == .1


@pytest.mark.integration
@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_custom_policy_consumes_ticks_once_and_exits_at_300_point_floor_before_close(rig, action, direction, monkeypatch):
    r = rig
    config = dict(profit_lock_trigger_points=300,
                  profit_lock_tiers=[dict(from_points=0, giveback_ratio=.5),
                                     dict(from_points=500, giveback_ratio=.2)])
    r.service.settings_path.write_text(json.dumps(config))
    enable_tick_lock(r, action)
    # The worker consumes the persisted row, including the tick tracking start;
    # the public editor snapshot intentionally omits that internal timestamp.
    order = r.service._active_order(r.service.orders()[0]['id'])
    r.update('2026-10-30 09:02:05', tick=20000 + direction * 400)
    ticks = [dict(tick_id=f'custom:{i}', code='TMF209912', mode='simulation',
        price=20000 + direction * points,
        quote_at=str(r.now[0] - pd.Timedelta(milliseconds=500) + pd.Timedelta(milliseconds=i)),
        received_at=str(r.now[0]), instance='offline-monitor', generation=1)
        for i, points in enumerate((300, 150, 400))]
    r.store.quotes([], ticks)
    r.service.load_quotes()
    calls = []
    load_policy = r.service._profit_lock_policy
    def one_policy():
        calls.append(1)
        return load_policy()
    monkeypatch.setattr(r.service, '_profit_lock_policy', one_policy)
    updated = r.service._apply_tick_profit_lock(order)
    assert len(calls) == 1  # one policy per batch, no per-tick config reads
    assert updated['profit_lock_peak_points'] == 300
    assert updated['profit_lock_price'] == 20000 + direction * 150
    monkeypatch.setattr(r.service, '_profit_lock_policy', load_policy)
    r.runtime.step()
    r.runtime.step()
    closed = r.service.orders()[0]
    assert closed['exit_status'] == 'Filled' and len(r.broker.trades) == 2
    assert r.now[0].second == 5  # the current 09:02 minute has not closed
    assert closed['profit_lock_giveback_ratio'] == .5


@pytest.mark.integration
@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_custom_tier_tightening_can_exit_from_tick_without_new_candle(rig, action, direction):
    r = rig
    enable_tick_lock(r, action)
    r.update(tick=20000 + direction * 800)
    r.runtime.step()
    assert r.service.orders()[0]['profit_lock_price'] == 20000 + direction * 600
    r.service.settings_path.write_text(json.dumps(dict(profit_lock_trigger_points=300,
        profit_lock_tiers=[dict(from_points=0, giveback_ratio=.1)])))
    r.update(tick=20000 + direction * 700)
    r.runtime.step()
    r.runtime.step()
    row = r.service.orders()[0]
    assert row['profit_lock_price'] == 20000 + direction * 720
    assert row['exit_status'] == 'Filled' and len(r.broker.trades) == 2
