"""Durable external-position adoption and the existing closed-minute risk flow."""
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import db
from test_independent_trade import rig
from test_position_reconciliation import rows
from test_external_reconciliation import install

pytestmark = pytest.mark.integration


def external(r, action='Buy'):
    r.broker.position = 2 if action == 'Buy' else -2
    r.broker.list_positions = lambda *a, **k: ([] if not r.broker.position else [NS(
        id=0, code=r.broker.contract.code, direction='Buy' if r.broker.position > 0 else 'Sell',
        quantity=abs(r.broker.position))])
    details = [NS(code=r.broker.contract.code, direction=action, date='20261030', quantity=1,
        entry_quantity=1, price=20000+index, currency='TWD', dseq='app-new-1') for index in range(2)]
    r.broker.list_position_detail = lambda *a, **k: details
    r.runtime.step()
    return details


def adopt(r, identity='adopt-1', action='Buy', **changes):
    payload = dict(contract_code=r.broker.contract.symbol, action=action, quantity=2,
                   simulation=True, confirmed=True)
    payload.update(changes)
    assert r.client.post('/api/commands', json=dict(id=identity, name='adopt_external', payload=payload)).status_code == 202
    r.runtime.step()
    return r.client.get('/api/commands/' + identity).json()


@pytest.mark.parametrize('action', ['Buy', 'Sell'])
def test_adoption_persists_full_lots_waits_for_risk_save_and_exits_once(rig, action):
    r = rig
    external(r, action)
    result = adopt(r, action=action)
    assert result['state'] == 'succeeded', result
    row_id = result['result']['order_ids'][0]
    row = rows(r)[row_id]
    assert row['order_origin'] == 'external_position' and row['active'] == 1
    assert row['filled_quantity'] == 2 and row['avg_fill_price'] == 20000.5
    assert row['auto_exit'] == row['breakeven_enabled'] == 0
    assert row['atr'] == 120 and row['triggered_at'] is None
    assert row['stop_price'] == (19760 if action == 'Buy' else 20240)
    assert row['target_price'] == (20240 if action == 'Buy' else 19760)
    assert r.service._broker_ready and not r.broker.trades
    snapshot = r.client.get('/api/snapshot').json()
    assert snapshot['orders'][0]['order_origin'] == 'external_position'
    assert snapshot['status']['pending_external_risk'] == 1
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT count(*) FROM external_position_adoptions').fetchone()[0] == 1
        assert all(len(fill[0]) == 10 for fill in conn.execute('SELECT filled_at FROM pnl_fills'))
        identity = conn.execute('SELECT * FROM broker_order_identities').fetchone()
        assert identity['seqno'] == '' and identity['ordno'] == 'app-new-1'
        audit = json.loads(conn.execute('SELECT document FROM external_position_adoptions').fetchone()[0])
        assert audit['risk_defaults']['basis'] == 'adoption' and audit['risk_defaults']['timeframe'] == '1h'
    for _ in range(3):
        r.runtime.step()
    assert not r.broker.trades and rows(r)[row_id]['triggered_at'] is None
    # A duplicate command and a fresh command cannot import the same position twice.
    assert adopt(r, action=action)['result'] == result['result']
    assert adopt(r, 'adopt-again', action)['state'] == 'failed'
    assert len(rows(r)) == 1
    stop, target = (19900, 20100) if action == 'Buy' else (20100, 19900)
    r.service.update_risk_settings(row_id, curve_id=None, stop_price=stop, target_price=target,
        breakeven_enabled=False, risk_revision=row['risk_revision'])
    assert rows(r)[row_id]['auto_exit'] == 1
    # Tick-only movement cannot exit before a new complete minute is imported.
    r.update('2026-10-30 09:02:20', close=20000, tick=stop-10 if action == 'Buy' else stop+10)
    r.runtime.step()
    assert not r.broker.trades
    r.update('2026-10-30 09:04:03', close=stop-10 if action == 'Buy' else stop+10)
    r.runtime.step()
    r.runtime.step()
    assert len(r.broker.trades) == 1 and r.broker.trades[0].order.octype == 'Cover'
    assert r.broker.trades[0].order.quantity == 2 and r.broker.position == 0
    assert rows(r)[row_id]['active'] == 0
    for _ in range(2):
        r.runtime.step()
    assert len(r.broker.trades) == 1


@pytest.mark.parametrize('fault', ['partial', 'missing', 'wrong_side', 'quantity', 'unconfirmed', 'mode', 'changed_lots', 'changed_position'])
def test_adoption_rejects_incomplete_or_changed_evidence_without_writes(rig, fault):
    r = rig
    details = external(r)
    changes = {}
    if fault == 'partial': details[0].entry_quantity = 2
    if fault == 'missing': details.clear()
    if fault == 'wrong_side': details[0].direction = 'Sell'
    if fault == 'quantity': changes['quantity'] = 1
    if fault == 'unconfirmed': changes['confirmed'] = False
    if fault == 'mode': changes['simulation'] = False
    if fault in {'changed_lots', 'changed_position'}:
        count = [0]
        def changed(*a, **k):
            count[0] += 1
            if fault == 'changed_lots' and count[0] == 2:
                details[0].dseq = 'another-entry'
            if fault == 'changed_position': r.broker.position = 1
            return details
        r.broker.list_position_detail = changed
    result = adopt(r, **changes)
    assert result['state'] == 'failed', result
    assert not rows(r) and not r.broker.trades
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT count(*) FROM external_position_adoptions').fetchone()[0] == 0


def test_adopted_date_only_entry_can_reconcile_a_later_external_close(rig):
    r = rig
    details = external(r)
    result = adopt(r)
    assert result['state'] == 'succeeded', result
    row_id = result['result']['order_ids'][0]
    r.broker.position = 0
    install(r, [NS(code=d.code, direction=d.direction, date=d.date, entry_date=d.date,
        entry_seqno=d.dseq, entry_quantity=d.entry_quantity, quantity=d.quantity,
        entry_price=d.price, cover_price=19900, dseq='app-cover', currency='TWD', pnl=-1000, fee=30, tax=10) for d in details])
    r.mono[0] += 61
    r.runtime.step()
    row = rows(r)[row_id]
    assert row['active'] == 0 and row['trigger_reason'] == 'external_close'
    assert r.service._broker_ready and not r.broker.trades


def test_adoption_storage_error_rolls_back_all_evidence(rig):
    r = rig
    external(r)
    with db.session(r.path) as conn:
        conn.execute("CREATE TRIGGER fail_adoption BEFORE INSERT ON external_position_adoptions BEGIN SELECT RAISE(ABORT,'offline'); END")
        conn.commit()
    assert adopt(r)['state'] == 'unknown'
    assert not rows(r) and not r.broker.trades
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT count(*) FROM pnl_fills').fetchone()[0] == 0


def test_restart_keeps_adopted_lots_and_disabled_protection(rig):
    r = rig
    external(r)
    result = adopt(r)
    assert result['state'] == 'succeeded', result
    row_id = result['result']['order_ids'][0]
    r.service.close()
    restarted = r.create()
    restarted.ensure_connected()
    restarted.process_market_update()
    assert restarted._broker_ready and not r.broker.trades
    assert restarted._active_order(row_id)['auto_exit'] == 0
    assert restarted.snapshot()['status']['pending_external_risk'] == 1


def test_unknown_command_prevents_adoption(rig):
    r = rig
    external(r)
    with db.session(r.path) as conn:
        conn.execute("INSERT INTO trade_commands VALUES ('uncertain','TMFR1','simulation','{}','unknown','t','t',NULL,NULL)")
        conn.commit()
    assert adopt(r)['state'] == 'failed'
    assert not rows(r) and not r.broker.trades


def test_dashboard_adoption_proxies_the_original_durable_id(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import dashboard
    from trade_client import TradeClient, command_id
    client = TradeClient(tmp_path / 'unified.db', 'TMFR1', mode='production')
    calls = []
    monkeypatch.setattr(client, '_snapshot', lambda: {})
    def transport(path, payload=None):
        calls.append((path, payload, command_id.get()))
        return {'state': 'succeeded', 'result': {'order_ids': ['adopted'], 'risk_enabled': False}}
    monkeypatch.setattr(client, '_request', transport)
    monkeypatch.setattr(dashboard, '_trading_service', lambda: client)
    body = dict(contract_code='TMFK6', action='Buy', quantity=2, simulation=False, confirmed=True)
    response = TestClient(dashboard.app).post('/api/trading/external-positions/adopt', json=body,
                                            headers={'Idempotency-Key': 'adopt-from-dashboard'})
    assert response.status_code == 200 and response.json()['order_ids'] == ['adopted']
    assert calls == [('/api/commands', dict(id='adopt-from-dashboard', name='adopt_external', target=None, payload=body), 'adopt-from-dashboard')]
