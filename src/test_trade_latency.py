import json
from queue import Queue
import threading
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient
import pytest

import dashboard
import db
import telegram_notify as tg
from trade_client import TradeClient
from test_independent_trade import rig, request
from test_monitor_recovery import runtime as market_runtime


def events(r):
    r.service.latency.flush()
    with db.read_session(r.path) as conn:
        return [{**dict(row), 'document': json.loads(row['document'])}
                for row in conn.execute('SELECT * FROM latency_events ORDER BY rowid')]


def callback(r, trade):
    r.service._capture_order_event(r.service._generation, 'FuturesDeal',
        {'order': {'id': trade.order.id}, 'account': {'secret': 'never-record-this'}})


def test_command_sdk_fill_snapshot_and_browser_keep_one_trace(rig, monkeypatch):
    r = rig
    assert request(r, identity='trace-order').status_code == 202
    r.runtime.step()
    r.runtime.step()
    order = r.service.orders()[0]
    rows = events(r)
    required = {'command_accepted', 'command_processing', 'command_completed', 'sdk_order_started',
                'sdk_order_completed', 'broker_cache_observed', 'fill_persisted', 'order_snapshot_published'}
    assert required <= {row['event'] for row in rows if row['trace_id'] == 'trace-order'}
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', r.path)
    monkeypatch.setitem(dashboard.CONFIG, 'code', 'TMFR1')
    monkeypatch.setitem(dashboard.CONFIG, 'mode', 'simulation')
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    client = TestClient(dashboard.app)
    before = len(r.apis)
    body = dict(event='browser_orders_rendered', order_id=order['id'], snapshot_id='test-snapshot',
                client_elapsed_ms=25, client_observed_at='2026-10-30 09:02:03+08:00')
    assert client.post('/api/latency/browser', json=body).status_code == 204
    rows = client.get('/api/latency', params={'order_id': order['id']}).json()
    assert any(row['event'] == 'browser_orders_rendered' and row['trace_id'] == 'trace-order' for row in rows)
    assert len(r.apis) == before
    assert client.post('/api/latency/browser', json={**body, 'client_elapsed_ms': -1}).status_code == 422


def test_callback_only_enqueues_then_owner_records_and_persists(rig):
    r = rig
    request(r)
    r.runtime.step()
    trade = r.broker.trades[0]
    r.service._wake.clear()
    called = []
    original = r.broker.update_status
    r.broker.update_status = lambda *args, **kwargs: called.append(threading.get_ident()) or original(*args, **kwargs)
    worker = threading.Thread(target=lambda: callback(r, trade))
    worker.start(); worker.join(1)
    assert not worker.is_alive()
    assert not called
    assert r.service._wake.is_set()
    assert r.service._order_events.qsize() == 1
    r.runtime.step()
    rows = events(r)
    received = next(row for row in rows if row['event'] == 'broker_callback_received')
    consumed = next(row for row in rows if row['event'] == 'broker_callback_consumed')
    assert received['trace_id'] == consumed['trace_id'] == 'order-1'
    assert consumed['elapsed_ms'] is not None
    assert 'never-record-this' not in json.dumps(rows)
    assert len(r.apis) == len(r.broker.trades) == 1


def test_dashboard_proxy_command_trace_reaches_sdk_without_network(rig, monkeypatch):
    r = rig
    proxy = TradeClient(r.path, 'TMFR1', mode='simulation')
    def transport(path, payload=None):
        response = r.client.get(path) if payload is None else r.client.post(path, json=payload)
        if payload is not None:
            r.runtime.step()
        return response.json()
    monkeypatch.setattr(proxy, '_request', transport)
    monkeypatch.setattr(dashboard, '_TRADING_SERVICE', proxy)
    monkeypatch.setattr(dashboard.app.state, 'mock_runtime', None, raising=False)
    monkeypatch.setattr(dashboard, '_dashboard_now', lambda: r.now[0])
    monkeypatch.setitem(dashboard.CONFIG, 'db_path', r.path)
    monkeypatch.setitem(dashboard.CONFIG, 'mode', 'simulation')
    monkeypatch.setitem(dashboard.CONFIG, 'code', 'TMFR1')
    body = dict(contract_code='TMF209912', action='Buy', quantity=1, price=0,
                price_type='MKT', order_type='IOC', simulation=True, confirmed=True)
    client = TestClient(dashboard.app)
    response = client.post('/api/trading/orders', headers={'Idempotency-Key': 'browser-order'}, json=body)
    assert response.status_code == 200
    rows = client.get('/api/latency', params={'trace_id': 'browser-order'}).json()
    assert {'dashboard_request_received', 'dashboard_response_ready', 'sdk_order_started'} <= {row['event'] for row in rows}
    assert {'trade', 'dashboard'} <= {row['role'] for row in rows}
    assert len(r.apis) == len(r.broker.trades) == 1


def test_callback_wakes_existing_worker_instead_of_waiting_for_poll(rig, monkeypatch):
    r = rig
    waiting, second = threading.Event(), threading.Event()
    class Gate(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(5)  # a poll-only worker cannot pass the one-second gate
    r.service._wake = Gate()
    calls = []
    def step():
        calls.append(threading.get_ident())
        if len(calls) >= 2:
            second.set()
    monkeypatch.setattr(r.runtime, 'step', step)
    r.runtime.start()
    try:
        assert waiting.wait(1)
        r.service._capture_order_event(r.service._generation, 'event', {})
        assert second.wait(1)
        assert len(set(calls)) == 1
    finally:
        r.runtime.stop.set()
        r.service._wake.set()
        r.runtime.thread.join(1)


def test_callback_without_sdk_id_matches_unique_seqno_and_ordno(rig):
    r = rig
    request(r); r.runtime.step()
    order = r.service.orders()[0]
    with db.session(r.path) as conn:
        conn.execute('INSERT OR REPLACE INTO broker_order_identities VALUES (?,?,?,?,?,?,?)',
                     (order['id'], order['entry_trade_id'], 'offline', 'seq-1', '2026-10-30',
                      str(r.now[0]), 'ord-1'))
        conn.commit()
    r.service._capture_order_event(r.service._generation, 'FuturesDeal', {'seqno': 'seq-1', 'ordno': 'ord-1'})
    r.runtime.step()
    row = next(row for row in events(r) if row['event'] == 'broker_callback_consumed')
    assert row['order_id'] == order['id']
    assert row['trace_id'] == 'order-1'


def test_partial_duplicate_and_regressing_reports_do_not_double_notify(rig, monkeypatch, tmp_path):
    r = rig
    messages = []
    alerts = tmp_path / 'alerts.json'
    alerts.write_text('{"telegram_enabled": true}')
    r.service.entry_notifier.enabled = True
    from alert_rules import AlertConfig
    r.service.entry_notifier.config = AlertConfig(alerts)
    r.service.entry_notifier.dispatch = lambda task: task()
    monkeypatch.setattr(tg, 'send_message', lambda message: messages.append(message) or True)
    payload = dict(contract_code='TMF209912', action='Buy', quantity=2, price=19000,
                   price_type='LMT', order_type='ROD', simulation=True, confirmed=True)
    request(r, payload=payload)
    r.runtime.step()
    trade = r.broker.trades[0]
    def fill(quantity, state):
        trade.status.deal_quantity = quantity
        trade.status.status = state
        trade.status.deals = [NS(seq='fill-1', quantity=quantity, price=20000, datetime=r.now[0])]
        r.broker.position = quantity
    fill(1, 'PartFilled')
    callback(r, trade); r.runtime.step()
    callback(r, trade); r.runtime.step()
    fill(2, 'Filled')
    callback(r, trade); r.runtime.step()
    fill(1, 'PartFilled')
    callback(r, trade); r.runtime.step()
    order = r.service.orders()[0]
    assert order['filled_quantity'] == 2 and order['entry_status'] == 'Filled'
    assert len(messages) == 2
    rows = events(r)
    assert [row['document']['cumulative_quantity'] for row in rows if row['event'] == 'fill_persisted'] == [1, 2]
    assert len([row for row in rows if row['event'] == 'notification_completed']) == 2


def test_missed_callback_repairs_pending_order_with_bounded_polling(rig):
    r = rig
    request(r, payload=dict(contract_code='TMF209912', action='Buy', quantity=1, price=19000,
                           price_type='LMT', order_type='ROD', simulation=True, confirmed=True))
    r.runtime.step()
    calls = []
    def repair(account):
        calls.append(1)
        trade = r.broker.trades[0]
        trade.status.status, trade.status.deal_quantity = 'Filled', 1
        trade.status.deals = [NS(seq='fill', quantity=1, price=20000, datetime=r.now[0])]
        r.broker.position = 1
    r.broker.update_status = repair
    r.mono[0] += 1.9
    r.runtime.step()
    assert not calls
    r.mono[0] += .2
    r.runtime.step()
    assert calls == [1]
    assert r.service.orders()[0]['filled_quantity'] == 1
    r.mono[0] += 2.1
    r.runtime.step()
    assert calls == [1]  # terminal order returns to the normal reconciliation interval


def test_old_generation_and_overflow_do_not_replay_orders(rig):
    r = rig
    request(r); r.runtime.step()
    trade = r.broker.trades[0]
    previous = r.service._generation - 1
    r.service._capture_order_event(previous, 'event', {'order': {'id': trade.order.id}})
    assert r.service._order_events.empty()
    r.service._order_events = Queue(maxsize=1)
    callback(r, trade); callback(r, trade)
    assert r.service._order_event_overflow.is_set()
    r.runtime.step()
    assert len(r.broker.trades) == len(r.apis) == 1
    assert any(row['event'] == 'broker_callback_overflow' for row in events(r))
    assert r.service._stale_order_events == 1


def test_market_publication_adoption_and_readiness_are_joinable(market_runtime):
    r = market_runtime
    r.store.latency.clock = lambda: r.now[0]
    r.put()
    r.item.step()
    with db.read_session(r.config.db_path) as conn:
        rows = conn.execute("SELECT * FROM latency_events WHERE role='monitor' ORDER BY rowid").fetchall()
    assert {row['event'] for row in rows} == {'monitor_adopted', 'monitor_ready'}
    details = json.loads(rows[0]['document'])
    assert details['minute_start'] == '2026-10-30 09:01:00'
    assert details['published_at'].endswith('+08:00')
    assert rows[0]['trace_id'] == 'market:TMFR1:2026-10-30 09:01:00'
