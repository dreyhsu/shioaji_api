"""Offline regressions for broker Cover executions and reconciliation gates."""
import json
from types import SimpleNamespace as NS

import pytest
import pandas as pd

import db
from test_independent_trade import rig, request
from mock_broker import MockBroker
from trading import TradingService

pytestmark = pytest.mark.integration


def enter(r, identity, action='Buy', quantity=2):
    payload = dict(contract_code='TMF209912', action=action, quantity=quantity,
                   price=0, price_type='MKT', order_type='IOC', simulation=True, confirmed=True)
    assert request(r, identity=identity, payload=payload).status_code == 202
    r.runtime.step()
    command = r.client.get('/api/commands/' + identity).json()
    assert command['state'] == 'succeeded', command
    return command['result']['id']


def rows(r):
    with db.read_session(r.path) as conn:
        return {row['id']: dict(row) for row in conn.execute('SELECT * FROM managed_orders')}


@pytest.mark.parametrize('action', ['Buy', 'Sell'])
def test_auto_cover_closes_original_position_without_creating_a_ghost(rig, action):
    r = rig
    opened = enter(r, 'open', action)
    r.runtime.step()
    r.now[0] += pd.Timedelta(seconds=1)
    covered = enter(r, 'cover', 'Sell' if action == 'Buy' else 'Buy')
    r.runtime.step()
    records = rows(r)
    original, closing = records[opened], records[covered]
    assert r.broker.position == 0 and len(r.broker.trades) == 2
    assert original['active'] == closing['active'] == 0
    assert original['exit_trade_id'] == closing['entry_trade_id']
    assert original['exit_filled_quantity'] == 2 and original['avg_exit_price'] == 20000
    assert closing['broker_octype'] == 'Cover' and closing['reconciled_order_id'] == opened
    assert closing['exit_filled_quantity'] == 0  # no fabricated round trip
    assert r.service._broker_ready and not r.service._reconcile_error
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT count(*) FROM order_reconciliations').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM pnl_fills WHERE order_id=? AND phase='entry'", (covered,)).fetchone()[0] == 0
        assert conn.execute("SELECT sum(quantity) FROM pnl_fills WHERE order_id=? AND phase='exit'", (opened,)).fetchone()[0] == 2
    for _ in range(3):
        r.runtime.step()
    assert len(r.broker.trades) == 2 and r.service._broker_ready
    assert r.service._public_order(closing)['broker_octype'] == 'Cover'


def test_periodic_position_check_detects_external_flat_and_keeps_gate_closed(rig):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    r.broker.position = 0
    r.mono[0] += 61
    r.runtime.step()
    assert not r.service._broker_ready and '不一致' in r.service._reconcile_error
    assert rows(r)[opened]['active'] == 1  # flat alone does not invent an exit
    r.mono[0] += 61
    r.runtime.step()
    assert not r.service._broker_ready
    assert r.service._last_position_sync_at and r.service._broker_positions == []


def test_manual_entry_forces_position_check_before_sending(rig):
    r = rig
    enter(r, 'open')
    r.runtime.step()
    r.broker.position = 0
    assert request(r, identity='blocked').status_code == 202
    r.runtime.step()
    assert r.client.get('/api/commands/blocked').json()['state'] == 'failed'
    assert len(r.broker.trades) == 1 and not r.service._broker_ready


def test_auto_exit_forces_position_check_before_sending(rig):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    r.broker.position = 0
    r.service._try_auto_exit(rows(r)[opened], 'stop')
    assert len(r.broker.trades) == 1 and not r.service._broker_ready


def test_position_query_failure_cannot_be_cleared_by_order_sync(rig):
    r = rig
    enter(r, 'open')
    r.runtime.step()
    def unavailable(*args, **kwargs):
        raise TimeoutError('private exception content must not appear')
    r.broker.list_positions = unavailable
    for _ in range(2):
        r.mono[0] += 61
        r.runtime.step()
        assert not r.service._broker_ready
        assert r.service._reconcile_error == '券商部位核對失敗：TimeoutError'


def test_partial_cover_is_not_guessed_or_retried(rig):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    covered = enter(r, 'cover', 'Sell', 1)
    r.runtime.step()
    assert not r.service._broker_ready and '部分成交' in r.service._reconcile_error
    assert rows(r)[opened]['exit_filled_quantity'] == 0
    assert rows(r)[covered]['reconciled_order_id'] is None
    for _ in range(3):
        r.runtime.step()
    assert len(r.broker.trades) == 2


def test_multiple_candidates_do_not_allocate_by_guessing_fifo(rig):
    r = rig
    enter(r, 'open-1', quantity=1)
    r.runtime.step()
    enter(r, 'open-2', quantity=1)
    r.runtime.step()
    enter(r, 'cover', 'Sell', 2)
    r.runtime.step()
    assert not r.service._broker_ready and '唯一' in r.service._reconcile_error
    assert all(row['exit_filled_quantity'] == 0 for row in rows(r).values())


def test_repair_preserves_rejected_exit_audit_and_does_not_notify_new_entry(rig):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    covered = enter(r, 'cover', 'Sell')
    rejected = NS(contract=r.broker.contract,
        order=NS(id='rejected-exit', action='Sell', octype='Cover'),
        status=NS(status='Failed', status_code='99Q1', deal_quantity=0, deals=[]))
    r.broker.trades.append(rejected)
    with db.session(r.path) as conn:
        conn.execute("UPDATE managed_orders SET exit_trade_id='rejected-exit',exit_status='Failed' WHERE id=?", (opened,))
        conn.commit()
    entries, exits = [], []
    r.service.entry_notifier.notify = lambda row: entries.append(row['id'])
    r.service.entry_notifier.notify_exit = lambda row: exits.append(row['id'])
    r.runtime.step()
    assert entries == [] and exits == [opened]
    with db.read_session(r.path) as conn:
        document = json.loads(conn.execute('SELECT document FROM order_reconciliations WHERE source_order_id=?', (covered,)).fetchone()[0])
    assert document['target_before']['exit_trade_id'] == 'rejected-exit'
    assert document['target_before']['exit_status_code'] == '99Q1'
    assert rows(r)[opened]['exit_trade_id'] != 'rejected-exit'


def test_mock_uses_the_same_cover_reconciliation_without_health_publishers(tmp_path):
    stamp = [pd.Timestamp('2026-10-30 09:00')]
    broker = MockBroker(clock=lambda: stamp[0])
    broker.price = 20000
    path = tmp_path / 'mock.db'
    with db.session(path):
        pass
    service = TradingService(path, 'TMFR1', simulation=True, api_factory=lambda _: broker, sdk=broker.sdk,
        clock=lambda: stamp[0], start_worker=False, enable_telegram=False, settings_path=tmp_path / 'settings.json')
    service._risk_context = lambda _: (120., pd.DataFrame())
    payload = dict(contract_code='TMF209912', action='Buy', quantity=2, price=0,
                   price_type='MKT', order_type='IOC', simulation=True, confirmed=True)
    try:
        opened = service.place_order(payload)
        service._sync_managed_orders(force=True)
        stamp[0] += pd.Timedelta(seconds=1)
        service.place_order(dict(payload, action='Sell'))
        service._sync_managed_orders(force=True)
        assert not service._active_orders() and broker.position == 0
        assert service._active_order(opened['id'])['exit_filled_quantity'] == 2
    finally:
        service.close()


def test_copy_repair_updates_only_unchanged_local_records_and_keeps_audit(rig, tmp_path):
    import importlib.util
    from pathlib import Path
    import sqlite3
    spec = importlib.util.spec_from_file_location('repair_tool', Path(__file__).resolve().parents[1] / 'tools/reconcile_positions.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    covered = enter(r, 'cover', 'Sell')
    before = tool.active(r.path, 'TMFR1', True)
    saved = tmp_path / 'original.db'
    with sqlite3.connect(r.path) as source, sqlite3.connect(saved) as dest:
        source.backup(dest)
    r.runtime.step()
    tool.copy_updates(saved, r.path, before)
    assert tool.active(saved, 'TMFR1', True) == []
    with db.read_session(saved) as conn:
        assert conn.execute('SELECT target_order_id FROM order_reconciliations WHERE source_order_id=?', (covered,)).fetchone()[0] == opened
    with pytest.raises(RuntimeError, match='changed'):
        tool.copy_updates(saved, r.path, before)


@pytest.mark.parametrize('corruption', ['missing_price', 'wrong_contract', 'other_position', 'late_entry'])
def test_cover_repair_requires_complete_identity_fills_and_matching_account_positions(rig, corruption):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    covered = enter(r, 'cover', 'Sell')
    if corruption == 'missing_price':
        r.broker.trades[-1].status.deals[0].price = 0
    elif corruption == 'wrong_contract':
        r.broker.trades[-1].contract = NS(code='OTHER')
    elif corruption == 'other_position':
        r.broker.position = 1
    else:
        r.broker.trades[0].status.deals[0].datetime = r.now[0] + pd.Timedelta(minutes=1)
    r.runtime.step()
    assert not r.service._broker_ready
    assert rows(r)[opened]['active'] == 1 and rows(r)[covered]['reconciled_order_id'] is None
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT count(*) FROM order_reconciliations').fetchone()[0] == 0


def test_sparse_relogin_summary_preserves_confirmed_average_and_ledger(rig):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    trade = r.broker.trades[0]
    trade.status.deals = []
    del trade.status.average_price
    trade.status.modified_price = 99999
    r.service._sync_managed_orders(force=True)
    assert rows(r)[opened]['avg_fill_price'] == 20000
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT entry_error FROM pnl_order_metadata WHERE order_id=?', (opened,)).fetchone()[0] is None
        assert conn.execute("SELECT sum(quantity) FROM pnl_fills WHERE order_id=? AND phase='entry'", (opened,)).fetchone()[0] == 2


def test_persisted_cover_fills_can_repair_after_relogin_omits_deals(rig):
    r = rig
    opened = enter(r, 'open')
    r.runtime.step()
    covered = enter(r, 'cover', 'Sell')
    r.service._persist_trade_state(covered, entry=r.broker.trades[-1])
    for trade in r.broker.trades:
        trade.status.deals = []
        del trade.status.average_price
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and not r.service._active_orders()
    assert rows(r)[opened]['avg_exit_price'] == 20000
    with db.read_session(r.path) as conn:
        assert conn.execute("SELECT sum(quantity) FROM pnl_fills WHERE order_id=? AND phase='exit'", (opened,)).fetchone()[0] == 2
        assert conn.execute('SELECT exit_error FROM pnl_order_metadata WHERE order_id=?', (opened,)).fetchone()[0] is None


def test_repair_tool_reuses_only_exactly_matching_verified_fills(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('repair_tool_fills', Path(__file__).resolve().parents[1] / 'tools/reconcile_positions.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    saved = NS(order=NS(id='broker-1', action='Sell', octype='Cover'), contract=NS(code='TMFK6'),
        status=NS(status='Filled', deals=[NS(quantity=2, price=43797, ts=1794457831)]))
    current = NS(order=NS(id='broker-1', action='Sell', octype='Cover'), contract=NS(code='TMFK6'),
        status=NS(status='Filled', deal_quantity=2, deals=[]))
    assert tool.with_confirmed_fills(current, saved).status.deals[0].price == 43797
    current.status.deal_quantity = 3
    assert tool.with_confirmed_fills(current, saved) is current
    current.status.deal_quantity = 2
    current.order.action = 'Buy'
    assert tool.with_confirmed_fills(current, saved) is current


def test_persisted_fills_from_another_broker_order_cannot_prove_a_close(rig):
    r = rig
    enter(r, 'open')
    r.runtime.step()
    covered = enter(r, 'cover', 'Sell')
    r.service._persist_trade_state(covered, entry=r.broker.trades[-1])
    with db.session(r.path) as conn:
        conn.execute("UPDATE pnl_fills SET broker_trade_id='different' WHERE order_id=?", (covered,))
        conn.commit()
    r.broker.trades[-1].status.deals = []
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and rows(r)[covered]['reconciled_order_id'] is None
