"""Durable external-position adoption and valid-tick protective exits."""
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import db
from test_independent_trade import rig
from test_position_reconciliation import rows
from test_external_reconciliation import install

pytestmark = pytest.mark.integration


def external(r, action='Buy', *, automatic=False):
    if not automatic:
        r.service._reconcile_external_positions = lambda *args: None
        r.service._prepare_broker_lots = lambda *args: False
    r.broker.position = 2 if action == 'Buy' else -2
    r.broker.list_positions = lambda *a, **k: ([] if not r.broker.position else [NS(
        id=0, code=r.broker.contract.code, direction='Buy' if r.broker.position > 0 else 'Sell',
        quantity=abs(r.broker.position))])
    details = [NS(code=r.broker.contract.code, direction=action, date='20261030', quantity=1,
        entry_quantity=1, price=20000+index, currency='TWD', dseq='app-new-1') for index in range(2)]
    r.broker.list_position_detail = lambda *a, **k: details
    r.runtime.step()
    return details


@pytest.mark.parametrize('action', ['Buy', 'Sell'])
def test_automatic_broker_lot_import_preserves_existing_risk_and_never_sends(rig, action):
    r = rig
    external(r, action, automatic=True)
    assert r.service._broker_ready, r.service._reconcile_error
    first = list(rows(r).values())[0]
    assert first['filled_quantity'] == 2 and first['active'] == 1
    assert first['auto_exit'] == first['breakeven_enabled'] == first['profit_lock_enabled'] == 1
    r.service.update_risk_settings(first['id'], curve_id=None, stop_price=19000 if action == 'Buy' else 21000,
        target_price=21000 if action == 'Buy' else 19000, breakeven_enabled=True,
        risk_revision=first['risk_revision'])
    existing = rows(r)[first['id']]
    details = [NS(code=r.broker.contract.code, direction=action, date='20261030', quantity=2,
        entry_quantity=2, price=20000.5, currency='TWD', dseq='app-new-1'),
        NS(code=r.broker.contract.code, direction=action, date='20261030', quantity=1,
        entry_quantity=1, price=20020, currency='TWD', dseq='app-new-2')]
    r.broker.list_position_detail = lambda *a, **k: details
    r.broker.position = 3 if action == 'Buy' else -3
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and len(rows(r)) == 2, r.service._reconcile_error
    assert all(rows(r)[first['id']][key] == existing[key] for key in ('stop_price', 'target_price', 'auto_exit', 'breakeven_enabled'))
    for _ in range(3):
        r.service._sync_managed_orders(force=True)
    assert len(rows(r)) == 2 and not r.broker.trades


def test_automatic_import_requires_stable_lot_identity_and_rolls_back(rig):
    r = rig
    details = external(r, automatic=False)
    del r.service._reconcile_external_positions
    del r.service._prepare_broker_lots
    count = [0]
    def changed(*args, **kwargs):
        count[0] += 1
        if count[0] == 2:
            details[0].dseq = 'reopened'
        return details
    r.broker.list_position_detail = changed
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and not rows(r)
    assert '變動' in r.service._reconcile_error


def test_automatic_import_does_not_manage_an_unconfigured_month(rig):
    r = rig
    r.service.position_sync_codes = {'TMFR1'}
    external(r, automatic=True)
    assert not r.service._broker_ready and not rows(r) and not r.broker.trades


def test_pending_app_order_allows_read_only_lot_diagnostics_but_no_adoption(rig):
    r = rig
    pending = NS(contract=r.broker.contract, order=NS(id='app-pending'), status=NS(status='Submitted'))
    r.broker.trades.append(pending)
    external(r, automatic=True)
    assert not r.service._broker_ready and not rows(r)
    assert r.broker.trades == [pending]
    assert r.service._broker_positions[0]['quantity'] == 2
    assert '帳戶' in r.service._reconcile_error
    r.broker.list_position_detail = lambda *a, **k: [NS(code=r.broker.contract.code,
        direction='Buy', date='invalid-private-date', quantity=2, entry_quantity=2,
        price=20000, currency='TWD', dseq='app-new')]
    r.mono[0] += 61
    r.service._sync_managed_orders(force=True)
    diagnostic = r.service._reconcile_diagnostic
    assert not r.service._broker_ready and not rows(r) and r.broker.trades == [pending]
    assert diagnostic is None  # malformed App identity remains gated before lot adoption
    assert 'invalid-private-date' not in json.dumps(r.service.snapshot())


def test_closed_adoption_reappearing_is_corrected_to_the_verified_broker_position(rig):
    r = rig
    details = external(r, automatic=True)
    original = next(iter(rows(r).values()))
    with db.session(r.path) as conn:
        conn.execute("UPDATE managed_orders SET active=0,auto_exit=0,exit_filled_quantity=filled_quantity,"
                     "exit_status='Filled' WHERE id=?", (original['id'],))
        conn.commit()
    for _ in range(2):
        r.service._sync_managed_orders(force=True)
        assert r.service._broker_ready and r.service._reconcile_error is None
        assert len(rows(r)) == 1 and rows(r)[original['id']]['active'] == 1
        assert not r.broker.trades
    # Once accounting catches up, the gate recovers through a fresh broker check.
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and r.service._reconcile_error is None
    # A genuine new batch still receives its own management record.
    for detail in details:
        detail.dseq = 'app-new-after-close'
    r.broker.position = 2
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and len(rows(r)) == 2
    assert rows(r)[original['id']]['active'] == 0
    assert len(r.service._active_orders()) == 1 and not r.broker.trades


def test_close_one_and_reopen_one_same_total_splits_original_and_new_lot(rig):
    r = rig
    r.service.position_sync_codes = {'TMFR1', r.broker.contract.code}
    original_details = external(r, 'Sell', automatic=True)
    original = list(rows(r).values())[0]
    r.service.update_risk_settings(original['id'], curve_id=None, stop_price=21000,
        target_price=19000, breakeven_enabled=True, risk_revision=original['risk_revision'])
    previous = rows(r)[original['id']]
    remaining = original_details[1]
    new = NS(code=r.broker.contract.code, direction='Sell', date='20261030', quantity=1,
        entry_quantity=1, price=20020, currency='TWD', dseq='app-new-2')
    r.broker.list_position_detail = lambda *args, **kwargs: [remaining, new]
    closed = original_details[0]
    install(r, [NS(code=closed.code, direction=closed.direction, date=closed.date,
        entry_date=closed.date, entry_seqno=closed.dseq, entry_quantity=1, quantity=1,
        entry_price=closed.price, cover_price=19900, dseq='app-cover', currency='TWD', pnl=1000, fee=30, tax=10)])
    r.mono[0] += 61
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and not r.service._reconcile_error, r.service._reconcile_error
    managed = rows(r)
    assert len(managed) == 2 and not r.broker.trades
    old = managed.pop(original['id'])
    assert old['filled_quantity'] == 2 and old['external_exit_quantity'] == old['exit_filled_quantity'] == 1
    assert all(old[key] == previous[key] for key in ('auto_exit','breakeven_enabled','stop_price','target_price'))
    added = list(managed.values())[0]
    assert added['filled_quantity'] == 1 and added['avg_fill_price'] == 20020 and added['auto_exit'] == 1
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and len(rows(r)) == 2 and not r.broker.trades


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
    assert row['auto_exit'] == row['breakeven_enabled'] == row['profit_lock_enabled'] == 0
    assert row['atr'] == 120 and row['triggered_at'] is None
    assert row['stop_price'] == (19940 if action == 'Buy' else 20060)
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
    # Saving risk enables valid-tick exits without waiting for a new closed bar.
    r.update('2026-10-30 09:02:20', close=20000, tick=stop-10 if action == 'Buy' else stop+10)
    r.runtime.step()
    assert len(r.broker.trades) == 1
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
            if fault == 'changed_lots':
                details[0].dseq = 'another-entry' if count[0] % 2 == 0 else 'app-new-1'
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
