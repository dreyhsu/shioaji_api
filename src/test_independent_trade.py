"""Real command ledger, unified DB and order engine with an offline broker."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import db
import dashboard
from fetcher_storage import FetcherStore
import market_health as mh
from market_fetcher import FetcherSettings
from mock_broker import MockBroker
import session_bars as sb
from test_market_fetchers import calendar
from trade import create_app, TradeRuntime, Command
from trade_client import TradeClient, command_id
from trade_service import IndependentTradingService

pytestmark = pytest.mark.integration


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / 'unified.db'
    now, mono, apis = [pd.Timestamp('2026-10-30 09:02:03')], [0.], []
    cal = tmp_path / 'calendar.json'
    cal.write_text(json.dumps(dict(source='offline verified exchange calendar fixture',
        coverage_start='2026-10-26', coverage_end='2026-11-03', sessions=[
            dict(start='2026-10-30 08:45', end='2026-10-30 13:45'),
            dict(start='2026-10-30 15:00', end='2026-10-31 05:00')])) )
    settings = mh.HealthSettings(calendar_path=str(cal))
    fetcher = replace(FetcherSettings(), calendar_path=str(cal))
    store = FetcherStore(path)
    health = {}
    for code in ('TMFR1', 'TMF209912'):
        publisher = mh.HealthPublisher(path, 'monitor', code, 'simulation', clock=lambda: now[0])
        publisher.acquire()
        health[code] = publisher
    broker = MockBroker(clock=lambda: now[0])
    broker.price = 20000
    def forbidden(*args, **kwargs):
        raise AssertionError('trade must never subscribe to or fetch market data')
    broker.quote.subscribe = forbidden
    broker.set_on_tick_fop_v1_callback = forbidden
    def factory(simulation):
        apis.append(simulation)
        return broker
    services = []
    def create():
        service = IndependentTradingService(path, 'TMFR1', api_factory=factory, sdk=broker.sdk, simulation=True,
            health_settings=settings, fetcher_settings=fetcher, calendar=calendar(), clock=lambda: now[0],
            monotonic=lambda: mono[0], enable_telegram=False, settings_path=tmp_path / 'trading.json')
        service._risk_context = lambda code: (120., pd.DataFrame())
        services.append(service)
        return service
    def update(stamp=None, close=20000, signal_close=None, tick=None):
        if stamp:
            now[0] = pd.Timestamp(stamp)
            mono[0] += 61
        latest = now[0].floor('min') - pd.Timedelta(minutes=1)
        for code in ('TMFR1', 'TMF209912'):
            price = close if code != 'TMFR1' or signal_close is None else signal_close
            bars = pd.DataFrame([dict(minute_start=latest, open=price, high=price, low=price,
                close=price, volume=1, amount=price)])
            with db.session(path) as conn:
                db.upsert_minute_bars(conn, code, sb.add_sessions(bars))
            health[code].publish(state='healthy', ready_since='2026-10-30 08:45',
                                 bars=dict(ready=True, last_minute_start=mh.iso(latest)))
            store.quotes([dict(code=code, mode='simulation', price=price if tick is None else tick,
                volume=1, quote_at=mh.iso(now[0]), received_at=mh.iso(now[0]), instance='offline-monitor', generation=1)])
        broker.price = close if tick is None else tick
    # Stable geometry and enough adjacent minutes for arming.
    for code in ('TMFR1', 'TMF209912'):
        frame = pd.DataFrame([dict(minute_start=at, open=19999, high=19999, low=19999,
            close=19999, volume=1, amount=19999) for at in pd.date_range('2026-10-30 08:55', periods=7, freq='min')])
        with db.session(path) as conn:
            db.upsert_minute_bars(conn, code, sb.add_sessions(frame))
    update()
    service = create()
    app = create_app(service, start_loop=False)
    with TestClient(app) as client:
        yield NS(service=service, client=client, runtime=app.state.runtime, path=path, now=now, mono=mono,
                 update=update, broker=broker, apis=apis, health=health, create=create, store=store)
    for item in services:
        item.close()
    for item in health.values():
        item.close()
    store.close()


def request(r, identity='order-1', **changes):
    body = dict(id=identity, name='place_order', payload=dict(contract_code='TMF209912', action='Buy', quantity=1,
        price=0, price_type='MKT', order_type='IOC', simulation=True, confirmed=True))
    body.update(changes)
    return r.client.post('/api/commands', json=body)


def test_reads_enqueue_and_duplicate_ids_do_not_login_or_submit(rig):
    r = rig
    assert r.client.get('/api/snapshot').status_code == 200 and not r.apis
    assert request(r).status_code == 202
    assert request(r).status_code == 202 and not r.apis
    r.runtime.step()
    assert len(r.apis) == 1 and len(r.broker.trades) == 1
    assert r.client.get('/api/commands/order-1').json()['state'] == 'succeeded'
    assert request(r).json()['state'] == 'succeeded'
    r.runtime.step()
    assert len(r.broker.trades) == 1
    body = request(r).json()
    changed = json.loads(body['request'])['payload']
    changed['quantity'] = 2
    assert request(r, payload=changed).status_code == 409


def test_stale_monitor_data_blocks_entry_without_quote_subscription(rig):
    r = rig
    r.health['TMFR1'].publish(state='degraded', bars={'ready': False})
    request(r)
    r.runtime.step()
    assert not r.broker.trades
    assert r.client.get('/api/commands/order-1').json()['state'] == 'failed'


def test_pending_incomplete_minute_does_not_disable_manual_order(rig):
    r = rig
    # A historical incomplete minute is repair work; current closed bars and
    # fresh quotes are available for both the signal and actual contract.
    for code in ('TMFR1', 'TMF209912'):
        with db.session(r.path) as conn:
            conn.execute("DELETE FROM minute_bars WHERE code=? AND minute_start='2026-10-30 08:59:00'", (code,))
            conn.commit()
        r.health[code].publish(state='healthy', incomplete_tick_minutes=1,
            bars=dict(ready=True, last_minute_start='2026-10-30 09:01:00',
                      missing_count=1, blocking_missing_count=0, pending_repair_count=1))
    request(r)
    r.runtime.step()
    assert r.client.get('/api/commands/order-1').json()['state'] == 'succeeded'
    assert len(r.broker.trades) == 1


def test_per_order_breakeven_points_pass_through_durable_trade_command(rig):
    r = rig
    request(r)
    r.runtime.step()
    r.runtime.step()
    order = r.service.orders()[0]
    payload = dict(curve_id=None, stop_price=order['stop_price'], target_price=order['target_price'],
        breakeven_enabled=True, risk_revision=order['risk_revision'],
        breakeven_trigger_points=60, breakeven_offset_points=8)
    response = r.client.post('/api/commands', json=dict(id='risk-points', name='update_risk',
        target=order['id'], payload=payload))
    assert response.status_code == 202
    r.runtime.step()
    command = r.client.get('/api/commands/risk-points').json()
    assert command['state'] == 'succeeded'
    assert command['result']['breakeven_trigger_points'] == 60
    assert command['result']['breakeven_offset_points'] == 8
    # Retrying this exact ID reads the original outcome, without another edit.
    repeated = r.client.post('/api/commands', json=dict(id='risk-points', name='update_risk',
        target=order['id'], payload=payload)).json()
    assert repeated['result']['risk_revision'] == command['result']['risk_revision']


def test_tick_crossing_does_not_exit_until_verified_minute_closes(rig):
    r = rig
    request(r)
    r.runtime.step()
    r.runtime.step()  # persist the entry fill
    r.update(tick=21000)
    r.runtime.step()
    assert len(r.broker.trades) == 1
    r.update('2026-10-30 09:03:03', close=21000)
    r.runtime.step()
    r.runtime.step()
    assert len(r.broker.trades) == 2
    assert r.service.orders()[0]['trigger_reason'] == 'target'


def test_independent_trade_notifies_confirmed_open_and_close(rig, monkeypatch):
    r = rig
    import telegram_notify as tg
    messages = []
    monkeypatch.setattr(tg, 'send_message', lambda message: messages.append(message) or True)
    notifier = r.service.entry_notifier
    notifier.enabled = True
    notifier.config.path = r.path.parent / 'fill-alerts.json'
    notifier.config.path.write_text('{"telegram_enabled": true}')
    notifier.dispatch = lambda task: task()
    request(r)
    r.runtime.step()
    r.runtime.step()
    assert len(messages) == 1 and '進場成交' in messages[0]
    r.update('2026-10-30 09:03:03', close=21000)
    r.runtime.step()
    r.runtime.step()
    assert len(messages) == 2 and '平倉成交' in messages[1]
    order = r.service.orders()[0]
    assert not order['active'] and order['avg_exit_price'] is not None
    r.runtime.step()
    assert len(messages) == 2


def test_accepted_order_with_lost_response_is_never_resubmitted(rig):
    r = rig
    original = r.broker.place_order
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError('accepted but response lost')
    r.broker.place_order = lost
    request(r)
    r.runtime.step()
    assert len(r.broker.trades) == 1
    assert r.client.get('/api/commands/order-1').json()['state'] == 'unknown'
    for _ in range(3):
        request(r)
        r.runtime.step()
    assert len(r.broker.trades) == 1
    assert r.service.orders()[0]['entry_status'] == 'unknown'


def test_scheduled_logout_does_not_flatten_and_cannot_login_on_holiday(rig):
    r = rig
    request(r)
    r.runtime.step()
    r.runtime.step()
    r.update('2026-10-30 13:46:00')
    r.runtime.step()
    assert r.service._api is None and r.broker.position == 1 and len(r.broker.trades) == 1
    r.update('2026-11-01 09:00:00')
    r.runtime.step()
    assert len(r.apis) == 1
    events = [json.loads(line) for line in (r.path.parent / 'logs' / 'trade_events.jsonl').read_text().splitlines()]
    assert {'login_succeeded', 'logout_succeeded'} <= {row['event'] for row in events}
    assert next(row['generation'] for row in events if row['event'] == 'login_succeeded') == next(
        row['generation'] for row in events if row['event'] == 'logout_succeeded')


def test_snapshot_requests_never_trigger_broker_synchronization(rig):
    r = rig
    r.runtime.step()
    def forbidden(*args, **kwargs):
        raise AssertionError('GET must not query the broker')
    r.broker.update_status = forbidden
    r.broker.margin = forbidden
    for _ in range(3):
        assert r.client.get('/api/snapshot').status_code == 200


def test_restart_preserves_unknown_processing_command(rig):
    r = rig
    request(r)
    with db.session(r.path) as conn:
        conn.execute("UPDATE trade_commands SET state='processing' WHERE id='order-1'")
        conn.commit()
    r.runtime.close()
    service = r.create()
    runtime = TradeRuntime(service)
    try:
        assert runtime.command('order-1')['state'] == 'unknown'
        runtime.step()
        assert not r.broker.trades
    finally:
        runtime.close()


def test_duplicate_trade_process_cannot_take_ownership(rig):
    with pytest.raises(RuntimeError, match='another trading'):
        TradeRuntime(rig.create())


def test_command_validation_rejects_invalid_mode_or_quantity_before_submission(rig):
    r = rig
    assert r.client.post('/api/commands', json={'id': 'bad', 'name': 'place_order', 'payload': {'quantity': True}}).status_code == 422
    request(r, payload=dict(contract_code='TMF209912', action='Buy', quantity=1, price=0,
        price_type='MKT', simulation=False, confirmed=True))
    r.runtime.step()
    assert not r.broker.trades


def test_dashboard_uses_remote_client_without_instantiating_trading_service(rig, monkeypatch):
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    monkeypatch.setattr(dashboard, '_TRADING_SERVICE', None)
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', rig.path)
    monkeypatch.setitem(dashboard.CONFIG, 'code', 'TMFR1')
    monkeypatch.setattr(dashboard.trading, 'TradingService', lambda *args, **kwargs: pytest.fail('Dashboard must not own a broker'))
    client = dashboard._trading_service()
    assert isinstance(client, TradeClient)
    def transport(path, payload=None):
        response = rig.client.get(path) if payload is None else rig.client.post(path, json=payload)
        assert response.status_code < 400, response.text
        if payload:
            rig.runtime.step()
            return rig.client.get('/api/commands/' + payload['id']).json()
        return response.json()
    monkeypatch.setattr(client, '_request', transport)
    with TestClient(dashboard.app) as browser:
        response = browser.get('/api/trading/status')
        assert response.status_code == 200 and not rig.apis
        body = dict(contract_code='TMF209912', action='Buy', quantity=1, price=0,
                    price_type='MKT', order_type='IOC', simulation=True, confirmed=True)
        response = browser.post('/api/trading/orders', json=body, headers={'Idempotency-Key': 'browser-1'})
        assert response.status_code == 200, response.text
        assert len(rig.broker.trades) == 1


def arm_curve(r, price=20000):
    with db.session(r.path) as conn:
        bars = db.load_minute_bars(conn, 'TMFR1')
        anchors = [{'time': int(at.timestamp()), 'price': price} for at in bars['minute_start'].iloc[[-5, -3, -1]]]
        curve = db.insert_trendline(conn, 'TMFR1', anchors, kind='curve', timeframe='1m')
    response = request(r, identity='arm-1', name='arm_curve', payload=dict(curve_id=curve,
                       contract_code='TMF209912', quantity=1, simulation=True, confirmed=True, action='Buy'))
    assert response.status_code == 202
    r.runtime.step()
    assert r.client.get('/api/commands/arm-1').json()['state'] == 'succeeded'
    return curve


def test_curve_entry_uses_adjacent_closes_and_survives_monitor_checking(rig):
    r = rig
    arm_curve(r)
    r.update('2026-10-30 09:03:03', close=19999, tick=21000)
    r.runtime.step()
    assert not r.broker.trades  # tick above the curve is not an entry signal
    r.update('2026-10-30 09:04:03', close=20001)
    r.health['TMFR1'].publish(state='checking')
    r.runtime.step()
    assert not r.broker.trades
    baseline = r.service.curve_entry.snapshot()['check_from']
    r.health['TMFR1'].publish(state='healthy')
    r.runtime.step()
    assert len(r.broker.trades) == 1
    entry = r.service.curve_entry.snapshot()
    assert entry['state'] == 'triggered' and entry['check_from'] == baseline
    r.runtime.step()
    assert len(r.broker.trades) == 1


def test_curve_modification_cancels_old_armed_version(rig):
    r = rig
    curve = arm_curve(r)
    with db.session(r.path) as conn:
        anchors = db.load_trendlines(conn, 'TMFR1')[0]['anchors']
        anchors[0]['price'] += 1
        db.update_trendline_anchors(conn, curve, anchors)
    r.update('2026-10-30 09:04:03', close=20001)
    r.runtime.step()
    assert r.service.curve_entry.snapshot()['state'] == 'cancelled'
    assert not r.broker.trades


def test_stale_tick_snapshot_does_not_reset_its_receipt_age_on_reload(rig):
    r = rig
    r.runtime.step()
    r.now[0] += pd.Timedelta(seconds=11)
    r.mono[0] += 11
    request(r)
    r.runtime.step()
    assert not r.broker.trades
    assert r.service._quote_health()['TMF_MOCK']['fresh'] is False


def test_healthy_orders_use_callback_cache_without_per_cycle_broker_poll(rig):
    r = rig
    calls = []
    original = r.broker.update_status
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    r.broker.update_status = counted
    r.runtime.step()
    assert len(calls) == 1
    for _ in range(5):
        r.runtime.step()
        r.client.get('/api/snapshot')
    assert len(calls) == 1
    r.mono[0] += 61
    r.runtime.step()
    assert len(calls) == 2


def test_remote_command_timeout_can_be_queried_with_same_id(rig, monkeypatch):
    r = rig
    client = TradeClient(r.path, 'TMFR1', timeout=0)
    def transport(path, payload=None):
        response = r.client.get(path) if payload is None else r.client.post(path, json=payload)
        return response.json()
    monkeypatch.setattr(client, '_request', transport)
    token = command_id.set('timeout-1')
    payload = dict(contract_code='TMF209912', action='Buy', quantity=1, price=0, price_type='MKT',
                   order_type='IOC', simulation=True, confirmed=True)
    from fastapi import HTTPException
    try:
        with pytest.raises(HTTPException) as caught:
            client.place_order(payload)
        assert caught.value.status_code == 504
        r.runtime.step()
        result = client.place_order(payload)
        assert result['id'] and len(r.broker.trades) == 1
    finally:
        command_id.reset(token)


def test_dashboard_settings_mismatch_rejects_command_before_enqueue(rig, monkeypatch):
    client = TradeClient(rig.path, 'OTHER')
    calls = []
    def transport(path, payload=None):
        calls.append(path)
        return rig.client.get(path).json()
    monkeypatch.setattr(client, '_request', transport)
    token = command_id.set('wrong-db')
    from fastapi import HTTPException
    try:
        with pytest.raises(HTTPException):
            client.place_order({})
        assert calls == ['/api/snapshot']
    finally:
        command_id.reset(token)


def test_login_crossing_cutoff_logs_out_before_accepting_any_order(rig):
    r = rig
    r.now[0] = pd.Timestamp('2026-10-30 13:45:59')
    original = r.service.api_factory
    def delayed(simulation):
        api = original(simulation)
        r.now[0] = pd.Timestamp('2026-10-30 13:46:01')
        return api
    r.service.api_factory = delayed
    request(r)
    r.runtime.step()
    assert r.service._api is None and not r.broker.trades


def test_logout_failure_does_not_block_next_scheduled_login(rig):
    r = rig
    r.runtime.step()
    r.broker.logout = lambda: False
    r.update('2026-10-30 13:46')
    r.runtime.step()
    assert r.service.logout_unconfirmed
    r.update('2026-10-30 15:00:03')
    r.runtime.step()
    assert len(r.apis) == 2


def test_actual_broker_call_rechecks_cutoff_after_validation(rig):
    r = rig
    r.runtime.step()
    r.now[0] = pd.Timestamp('2026-10-30 13:46')
    with pytest.raises(ValueError, match='交易窗口'):
        r.service._api.place_order(None, None)
    assert not r.broker.trades


def test_snapshot_prices_remain_live_while_risk_checks_use_closed_candles(rig):
    r = rig
    request(r)
    r.runtime.step()
    r.runtime.step()
    r.update('2026-10-30 09:03:03', close=20010, tick=20020)
    r.runtime.step()
    snapshot = r.client.get('/api/snapshot').json()
    assert snapshot['status']['market_quotes']['TMF209912']['price'] == 20020
    assert snapshot['orders'][0]['last_quote'] == 20020
    assert r.service.closed_price({'contract_code': 'TMF_MOCK'}) == 20010
