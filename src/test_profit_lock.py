"""Tiered profit protection, durable settings and tick execution."""
import json

import pandas as pd

import pytest
from fastapi.testclient import TestClient

import dashboard
import db
import trading
from trade_service import PROFIT_LOCK_TICK_BATCH
from test_risk_settings import filled, save, tick
from test_trading import fake_service
from test_independent_trade import rig, request


def enable_tick_lock(r, action='Buy'):
    direction = 1 if action == 'Buy' else -1
    assert request(r, payload=dict(contract_code='TMF209912', action=action, quantity=1,
        price=0, price_type='MKT', order_type='IOC', simulation=True, confirmed=True)).status_code == 202
    r.runtime.step()
    r.runtime.step()
    order = r.service.orders()[0]
    payload = dict(curve_id=None, stop_price=order['stop_price'], target_price=20000 + direction * 2000,
        breakeven_enabled=False, profit_lock_enabled=True, risk_revision=order['risk_revision'])
    assert r.client.post('/api/commands', json=dict(id='enable-tick-lock', name='update_risk',
        target=order['id'], payload=payload)).status_code == 202
    r.runtime.step()
    assert r.client.get('/api/commands/enable-tick-lock').json()['state'] == 'succeeded'
    return order


@pytest.mark.integration
def test_large_tick_batch_yields_without_skipping_peak_or_retrace(rig):
    r = rig
    order = enable_tick_lock(r)
    r.update('2026-10-30 09:02:05', tick=20700)
    points = [100] * PROFIT_LOCK_TICK_BATCH + [800, 600, 700]
    ticks = [dict(tick_id=f'bounded:{i}', code='TMF209912', mode='simulation', price=20000 + point,
        quote_at=str(r.now[0] - pd.Timedelta(seconds=1) + pd.Timedelta(microseconds=i)),
        received_at=str(r.now[0]), instance='offline-monitor', generation=1)
        for i, point in enumerate(points)]
    r.store.quotes([], ticks)
    r.service._wake.clear()
    r.runtime.step()
    row = r.service.orders()[0]
    assert row['profit_lock_peak_points'] == 100  # the newest quote cannot jump ahead of the tape
    assert not row['profit_lock_triggered_at'] and len(r.broker.trades) == 1
    assert r.service._wake.is_set()
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT tick_seq FROM managed_profit_lock_ticks WHERE order_id=?', (order['id'],)).fetchone()[0] == PROFIT_LOCK_TICK_BATCH
    r.runtime.step()
    r.runtime.step()
    row = r.service.orders()[0]
    assert row['profit_lock_peak_points'] == 800 and row['profit_lock_price'] == 20600
    assert row['exit_status'] == 'Filled' and len(r.broker.trades) == 2


@pytest.mark.integration
def test_tick_calculation_holds_no_writer_lock_and_does_not_initialize_schema(rig, monkeypatch):
    r = rig
    enable_tick_lock(r)
    r.update('2026-10-30 09:02:05', tick=20800)
    r.service.load_quotes()
    order = r.service.orders()[0]
    called = []
    original = trading.TradingService._profit_lock_updates
    def check_lock(service, row, price, **kwargs):
        conn = db.connect(r.path, timeout=0)
        try:
            conn.execute('BEGIN IMMEDIATE')  # a writer must remain available during calculation
            conn.rollback()
        finally:
            conn.close()
        called.append(price)
        return original(service, row, price, **kwargs)
    monkeypatch.setattr(trading.TradingService, '_profit_lock_updates', check_lock)
    def no_schema(*args, **kwargs):
        raise AssertionError('tick hot path must use the existing schema')
    monkeypatch.setattr(db, 'init_schema', no_schema)
    updated = r.service._apply_tick_profit_lock(order)
    assert called == [20800] and updated['profit_lock_price'] == 20600


@pytest.mark.integration
def test_staged_ticks_cannot_trigger_before_monitor_publishes_the_head(rig):
    r = rig
    enable_tick_lock(r)
    r.update('2026-10-30 09:02:04', tick=20100)
    r.runtime.step()
    # Leave the published quote at :04 while the monitor stages newer ticks.
    r.now[0] += pd.Timedelta(seconds=1)
    r.mono[0] += 1
    ticks = [dict(tick_id=f'staged:{i}', code='TMF209912', mode='simulation', price=price,
        quote_at=str(r.now[0] - pd.Timedelta(milliseconds=500) + pd.Timedelta(microseconds=i)),
        received_at=str(r.now[0]), instance='offline-monitor', generation=1)
        for i, price in enumerate((20800, 20600, 20700))]
    r.store.quotes([], ticks)
    with db.session(r.path) as conn:
        conn.execute('UPDATE market_risk_tick_heads SET seq=0')
        conn.commit()
    r.runtime.step()
    assert r.service.orders()[0]['profit_lock_peak_points'] == 100 and len(r.broker.trades) == 1
    with db.session(r.path) as conn:
        conn.execute('UPDATE market_risk_tick_heads SET seq=3')
        conn.commit()
    r.update('2026-10-30 09:02:06', tick=20700)
    r.runtime.step()
    r.runtime.step()
    assert r.service.orders()[0]['profit_lock_peak_points'] == 800
    assert len(r.broker.trades) == 2 and r.service.orders()[0]['exit_status'] == 'Filled'


@pytest.mark.integration
@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_tick_tape_remembers_extreme_and_crossing_even_after_rebound(rig, action, direction):
    r = rig
    order = enable_tick_lock(r, action)
    r.update('2026-10-30 09:02:05', tick=20000 + direction * 700)
    ticks = [dict(tick_id=f'test:{i}', code='TMF209912', mode='simulation', price=20000 + direction * points,
        quote_at=str(r.now[0] - pd.Timedelta(seconds=1) + pd.Timedelta(milliseconds=i)),
        received_at=str(r.now[0]), instance='offline-monitor', generation=1)
        for i, points in enumerate((800, 600, 700))]
    r.store.quotes([], ticks)
    r.runtime.step()
    r.runtime.step()
    closed = r.service.orders()[0]
    assert closed['profit_lock_peak_points'] == 800
    assert closed['profit_lock_price'] == 20000 + direction * 600
    assert closed['trigger_reason'] == 'stop' and closed['exit_status'] == 'Filled'
    assert len(r.broker.trades) == 2
    with db.read_session(r.path) as conn:
        state = conn.execute('SELECT * FROM managed_profit_lock_ticks WHERE order_id=?', (order['id'],)).fetchone()
        assert state['hit_at'] == ticks[1]['quote_at']
    r.runtime.step()
    assert len(r.broker.trades) == 2


@pytest.mark.integration
@pytest.mark.parametrize('bad', ['stale', 'generation', 'instance', 'before_enable', 'late'])
def test_tick_peak_rejects_unusable_tape(rig, bad):
    r = rig
    order = enable_tick_lock(r)
    r.update('2026-10-30 09:02:05', tick=20100)
    r.runtime.step()
    tick_row = dict(tick_id='bad', code='TMF209912', mode='simulation', price=20800,
        quote_at=str(r.now[0]), received_at=str(r.now[0]), instance='offline-monitor', generation=1)
    if bad == 'stale':
        tick_row['quote_at'] = '2026-10-30 09:01:00'
    elif bad in ('generation', 'instance'):
        tick_row[bad] = 99 if bad == 'generation' else 'previous-owner'
    elif bad == 'before_enable':
        tick_row.update(quote_at='2026-10-30 09:02:02', received_at='2026-10-30 09:02:02')
    else:
        tick_row['quote_at'] = '2026-10-30 09:02:04'
    r.store.quotes([], [tick_row])
    r.runtime.step()
    row = r.service.orders()[0]
    assert row['profit_lock_peak_points'] == 100 and not row['profit_lock_triggered_at']
    assert len(r.broker.trades) == 1


@pytest.mark.integration
def test_tick_crossing_survives_restart_without_duplicate_exit(rig):
    from trade import TradeRuntime
    r = rig
    order = enable_tick_lock(r)
    r.update('2026-10-30 09:02:05', tick=20700)
    ticks = [dict(tick_id=f'restart:{i}', code='TMF209912', mode='simulation', price=price,
        quote_at=str(r.now[0] - pd.Timedelta(seconds=1) + pd.Timedelta(milliseconds=i)),
        received_at=str(r.now[0]), instance='offline-monitor', generation=1)
        for i, price in enumerate((20800, 20600, 20700))]
    r.store.quotes([], ticks)
    r.service.settings_path.write_text(json.dumps({'auto_exit_enabled': False}))
    r.runtime.step()
    assert len(r.broker.trades) == 1
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT hit_at FROM managed_profit_lock_ticks WHERE order_id=?', (order['id'],)).fetchone()[0]
    r.runtime.close()
    service = r.create()
    # MockBroker's canonical code contains an underscore; restore its local
    # monitor alias before the startup snapshot, which precedes broker login.
    service.bar_codes[order['contract_code']] = 'TMF209912'
    service.settings_path.write_text(json.dumps({'auto_exit_enabled': True}))
    runtime = TradeRuntime(service)
    try:
        r.update('2026-10-30 09:02:06', tick=20700)
        runtime.step()
        runtime.step()
        assert len(r.broker.trades) == 2 and not service.orders()[0]['active']
        runtime.step()
        assert len(r.broker.trades) == 2
    finally:
        runtime.close()


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
@pytest.mark.parametrize('quantity', [1, 3])
def test_tiered_profit_tracks_per_contract_peak_and_survives_restart(fake_service, action, direction, quantity):
    service, api, _ = fake_service
    order = filled(service, api, action, quantity=quantity)
    order = save(service, order, breakeven_enabled=False, profit_lock_enabled=True,
                 target_price=46270 + direction * 2000)
    tick(service, 46270 + direction * 299)
    row = service._active_order(order['id'])
    assert row['profit_lock_peak_points'] == 299 and not row['profit_lock_triggered_at']
    tick(service, 46270 + direction * 300)
    row = service._active_order(order['id'])
    assert row['stop_price'] == row['profit_lock_price'] == 46270 + direction * 150
    first_trigger = row['profit_lock_triggered_at']
    tick(service, 46270 + direction * 800)
    peak = service._active_order(order['id'])
    assert peak['profit_lock_peak_points'] == 800
    assert peak['stop_price'] == peak['profit_lock_price'] == 46270 + direction * 600
    assert peak['profit_lock_triggered_at'] == first_trigger
    tick(service, 46270 + direction * 601)
    row = service._active_order(order['id'])
    assert row['risk_revision'] == peak['risk_revision']
    assert service._exit_signal(row, 46270 + direction * 601) is None
    assert service._exit_signal(row, 46270 + direction * 600) == 'stop'
    assert service._exit_signal(row, 46270 + direction * 550) == 'stop'
    restarted = trading.TradingService(service.db_path, service.code, simulation=True, start_worker=False)
    try:
        restored = restarted.orders()[0]
        assert restored['profit_lock_enabled'] == 1
        assert restored['profit_lock_peak_points'] == 800
        assert restored['profit_lock_price'] == row['profit_lock_price']
        assert restored['stop_price'] == row['stop_price']
        assert restored['profit_lock_triggered_at'] == first_trigger
    finally:
        restarted.close()


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_profit_lock_respects_stronger_stop_and_requires_separate_disable(fake_service, action, direction):
    service, api, _ = fake_service
    order = filled(service, api, action)
    order = save(service, order, breakeven_enabled=False, profit_lock_enabled=True, target_price=46270 + direction * 2000,
                 stop_price=46270 + direction * 700)
    tick(service, 46270 + direction * 801)
    row = service._public_order(service._active_order(order['id']))
    assert row['profit_lock_price'] == 46270 + direction * 601  # round toward retained profit
    assert row['stop_price'] == 46270 + direction * 700
    with pytest.raises(ValueError, match='先關閉獲利回撤保本'):
        save(service, row, stop_price=46270, profit_lock_enabled=False)
    disabled = save(service, row, profit_lock_enabled=False)
    assert disabled['stop_price'] == row['stop_price']
    assert not disabled['profit_lock_triggered_at'] and disabled['profit_lock_price'] is None
    assert disabled['profit_lock_peak_points'] == 0
    loosened = save(service, disabled, stop_price=46270 - direction * 100)
    assert loosened['stop_price'] == 46270 - direction * 100


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_fixed_breakeven_and_profit_lock_use_the_stronger_protection(fake_service, action, direction):
    service, api, _ = fake_service
    order = filled(service, api, action)
    order = save(service, order, profit_lock_enabled=True, target_price=46270 + direction * 2000,
                 breakeven_trigger_points=100, breakeven_offset_points=450)
    tick(service, 46270 + direction * 500)
    row = service._active_order(order['id'])
    assert row['breakeven_triggered_at'] and row['profit_lock_triggered_at']
    assert row['stop_price'] == 46270 + direction * 450
    tick(service, 46270 + direction * 1000)
    row = service._active_order(order['id'])
    assert row['stop_price'] == 46270 + direction * 800


def test_disabled_default_and_legacy_updates_preserve_individual_choice(fake_service):
    service, api, _ = fake_service
    first = filled(service, api)
    second = filled(service, api)
    assert not first['profit_lock_enabled'] and not second['profit_lock_enabled']
    enabled = save(service, first, profit_lock_enabled=True, target_price=49000)
    # Old callers omit the new field: an unrelated save must retain the choice.
    assert save(service, enabled)['profit_lock_enabled'] == 1
    assert not service._active_order(second['id'])['profit_lock_enabled']
    tick(service, 47070)
    assert service._active_order(first['id'])['profit_lock_peak_points'] == 800
    assert service._active_order(second['id'])['profit_lock_peak_points'] == 0
    with pytest.raises(trading.RiskConflict):
        save(service, enabled, profit_lock_enabled=False)


def test_enabling_on_a_fresh_profitable_price_immediately_installs_protection(fake_service):
    service, api, _ = fake_service
    order = filled(service, api)
    order = save(service, order, breakeven_enabled=False, target_price=49000)
    tick(service, 47070)
    assert service._active_order(order['id'])['profit_lock_peak_points'] == 0
    enabled = save(service, order, profit_lock_enabled=True)
    assert enabled['profit_lock_peak_points'] == 800
    assert enabled['profit_lock_price'] == enabled['stop_price'] == 46870


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_configured_profit_lock_threshold_reloads_without_releasing_latched_protection(fake_service, action, direction):
    service, api, _ = fake_service
    service.settings_path.write_text(json.dumps({'profit_lock_trigger_points': 600}))
    order = filled(service, api, action)
    order = save(service, order, breakeven_enabled=False, profit_lock_enabled=True,
                 target_price=46270 + direction * 2000)
    assert order['profit_lock_trigger_points'] == 600
    for points in (500, 599):
        tick(service, 46270 + direction * points)
        assert not service._active_order(order['id'])['profit_lock_triggered_at']
    tick(service, 46270 + direction * 600)
    row = service._active_order(order['id'])
    assert row['stop_price'] == row['profit_lock_price'] == 46270 + direction * 400
    first_trigger = row['profit_lock_triggered_at']
    service.settings_path.write_text(json.dumps({'profit_lock_trigger_points': 900}))
    tick(service, 46270 + direction * 800)
    row = service._active_order(order['id'])
    assert row['profit_lock_price'] == row['stop_price'] == 46270 + direction * 600
    assert row['profit_lock_triggered_at'] == first_trigger
    assert service._public_order(row)['profit_lock_trigger_points'] == 900


def test_lowering_configured_profit_lock_threshold_uses_tracked_peak(fake_service):
    service, api, _ = fake_service
    service.settings_path.write_text(json.dumps({'profit_lock_trigger_points': 600}))
    order = filled(service, api)
    order = save(service, order, breakeven_enabled=False, profit_lock_enabled=True, target_price=49000)
    tick(service, 46270 + 550)
    assert not service._active_order(order['id'])['profit_lock_triggered_at']
    service.settings_path.write_text(json.dumps({'profit_lock_trigger_points': 500}))
    tick(service, 46270 + 540)
    row = service._active_order(order['id'])
    assert row['profit_lock_peak_points'] == 550
    assert row['profit_lock_price'] == 46270 + 330


@pytest.mark.parametrize('contents', ['{}', '[]', 'broken json',
    '{"profit_lock_trigger_points": true}', '{"profit_lock_trigger_points": -1}',
    '{"profit_lock_trigger_points": "600"}', '{"profit_lock_trigger_points": null}',
    '{"profit_lock_trigger_points": NaN}', '{"profit_lock_trigger_points": Infinity}'])
def test_invalid_or_missing_profit_lock_config_uses_default_tiered_policy(fake_service, contents):
    service, _, _ = fake_service
    service.settings_path.write_text(contents)
    settings = service.profit_lock_settings()
    assert settings['profit_lock_trigger_points'] == 300
    assert settings['profit_lock_tiers'][0] == dict(from_points=0, giveback_ratio=.5)
    assert len(settings['profit_lock_tiers']) == 6
    assert settings['profit_lock_config_error'] == (None if contents == '{}' else 'invalid_config')


@pytest.mark.integration
def test_trade_snapshot_reports_configured_profit_lock_threshold_without_login(rig):
    r = rig
    r.service.settings_path.write_text(json.dumps({'profit_lock_trigger_points': 600}))
    r.runtime.save_snapshot()
    snapshot = r.client.get('/api/snapshot').json()
    assert snapshot['status']['profit_lock_trigger_points'] == 600
    assert not r.apis


@pytest.mark.parametrize('value', [1, 'true', 'false', 0])
def test_dashboard_rejects_non_boolean_profit_lock(fake_service, monkeypatch, value):
    service, api, _ = fake_service
    order = filled(service, api)
    monkeypatch.setattr(dashboard, '_trading_service', lambda: service)
    monkeypatch.setattr(dashboard, '_TRADING_SERVICE', service)
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', service.db_path)
    monkeypatch.setitem(dashboard.CONFIG, 'code', service.code)
    with TestClient(dashboard.app) as client:
        response = client.put(f"/api/trading/orders/{order['id']}/risk-settings", json=dict(
            curve_id=None, stop_price=order['stop_price'], target_price=order['target_price'],
            risk_revision=order['risk_revision'], breakeven_enabled=False, profit_lock_enabled=value))
    assert response.status_code == 422
    assert not service._active_order(order['id'])['profit_lock_enabled']


@pytest.mark.integration
@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_trade_locks_tick_extreme_and_exits_before_minute_closes(rig, action, direction):
    r = rig
    assert request(r, payload=dict(contract_code='TMF209912', action=action, quantity=1,
        price=0, price_type='MKT', order_type='IOC', simulation=True, confirmed=True)).status_code == 202
    r.runtime.step()
    r.runtime.step()
    order = r.service.orders()[0]
    payload = dict(curve_id=None, stop_price=order['stop_price'], target_price=20000 + direction * 2000,
                   breakeven_enabled=False, profit_lock_enabled=True, risk_revision=order['risk_revision'])
    command = dict(id='tiered-profit', name='update_risk', target=order['id'], payload=payload)
    assert r.client.post('/api/commands', json=command).status_code == 202
    r.runtime.step()
    result = r.client.get('/api/commands/tiered-profit').json()
    assert result['state'] == 'succeeded', result
    assert result['result']['profit_lock_enabled'] == 1
    assert r.client.post('/api/commands', json=command).json()['result'] == result['result']
    r.update(tick=20000 + direction * 299)
    r.runtime.step()
    assert not r.service.orders()[0]['profit_lock_triggered_at']
    r.update(tick=20000 + direction * 300)
    r.runtime.step()
    assert r.service.orders()[0]['profit_lock_price'] == 20000 + direction * 150
    r.update(tick=20000 + direction * 800)
    r.runtime.step()
    assert r.service.orders()[0]['stop_price'] == 20000 + direction * 600
    r.health['TMF209912'].publish(state='degraded', bars={'ready': False})
    r.store.quotes([dict(code='TMF209912',mode='simulation',price=20000+direction*300,volume=1,
        quote_at=str(r.now[0]),received_at=str(r.now[0]),instance='offline-monitor',generation=1)])
    r.runtime.step()
    assert not r.service.orders()[0]['protection']['profit_lock']
    assert len(r.broker.trades) == 1
    r.update(tick=20000 + direction * 600)
    r.runtime.step()
    r.runtime.step()
    closed = r.service.orders()[0]
    assert len(r.broker.trades) == 2 and r.broker.position == 0
    assert closed['trigger_reason'] == 'stop'
    assert closed['exit_status'] == 'Filled' and not closed['active']


def test_schema_migrates_profit_lock_without_changing_existing_orders(tmp_path):
    conn = db.connect(tmp_path / 'old.db')
    try:
        conn.execute('CREATE TABLE managed_orders (id TEXT PRIMARY KEY, code TEXT, contract_code TEXT, '
                     'active INTEGER, simulation INTEGER, entry_trade_id TEXT, exit_trade_id TEXT)')
        conn.execute("INSERT INTO managed_orders (id,code,active,simulation) VALUES ('old','TMFR1',1,1)")
        conn.commit()
        db.init_schema(conn)
        db.init_schema(conn)
        row = dict(conn.execute('SELECT * FROM managed_orders').fetchone())
        assert row['profit_lock_enabled'] == row['profit_lock_peak_points'] == 0
        assert row['profit_lock_price'] is None and row['profit_lock_triggered_at'] is None
    finally:
        conn.close()


@pytest.mark.integration
def test_pre_upgrade_risk_command_retry_returns_original_result_without_execution(rig):
    r = rig
    legacy_payload = dict(curve_id=None, stop_price=19760., target_price=20240., breakeven_enabled=True,
        breakeven_trigger_points=None, breakeven_offset_points=None, risk_revision=3, confirmed=False)
    document = json.dumps(dict(name='update_risk', target='old-order', payload=legacy_payload), sort_keys=True)
    result = dict(id='old-order', risk_revision=4)
    with db.session(r.path) as conn:
        conn.execute('INSERT INTO trade_commands VALUES (?,?,?,?,?,?,?,?,?)',
            ('legacy-risk', 'TMFR1', 'simulation', document, 'succeeded', str(r.now[0]), str(r.now[0]),
             json.dumps(result), 200))
        conn.commit()
    command = dict(id='legacy-risk', name='update_risk', target='old-order', payload=legacy_payload)
    assert r.client.post('/api/commands', json=command).json()['result'] == result
    command['payload']['profit_lock_enabled'] = None
    assert r.client.post('/api/commands', json=command).json()['result'] == result
    command['payload']['profit_lock_enabled'] = False
    assert r.client.post('/api/commands', json=command).status_code == 409
    assert not r.apis and not r.broker.trades
