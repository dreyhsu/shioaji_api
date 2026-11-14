"""External App closes reconcile without sending orders or fabricating fill times."""
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import db
import pnl_calendar
from test_independent_trade import rig
from test_position_reconciliation import enter, rows

pytestmark = pytest.mark.integration


def evidence(r, opened, *, seqno='entry-1', legacy=False, entry_prices=None, cover_price=19900):
    row = rows(r)[opened]
    trade = next(item for item in r.broker.trades if item.order.id == row['entry_trade_id'])
    if not legacy:
        trade.order.seqno = 'platform-' + seqno
        trade.order.ordno = seqno
    r.service._sync_managed_orders(force=True)
    row = rows(r)[opened]
    prices = entry_prices or [20000] * row['filled_quantity']
    details = [NS(code=r.broker.contract.code, direction=row['action'], date='20261030',
        entry_date='20261030', entry_seqno=seqno, entry_quantity=row['filled_quantity'], quantity=1,
        entry_price=price, cover_price=cover_price, dseq='app-close-1', currency='TWD',
        pnl=(cover_price-price)*10, fee=15, tax=9) for price in prices]
    return details


def install(r, details):
    calls = []
    def summaries(account, **kwargs):
        calls.append(('summary', kwargs))
        return [NS(id=0, code=r.broker.contract.code)]
    def detail(account, **kwargs):
        calls.append(('detail', kwargs))
        return details
    r.broker.list_profit_loss = summaries
    r.broker.list_profit_loss_detail = detail
    return calls


@pytest.mark.parametrize('action', ['Buy', 'Sell'])
@pytest.mark.parametrize('legacy', [False, True])
def test_full_external_close_reconciles_persists_and_never_resubmits(rig, action, legacy):
    r = rig
    opened = enter(r, 'open', action)
    records = evidence(r, opened, legacy=legacy)
    calls = install(r, records)
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    closed = rows(r)[opened]
    assert r.service._broker_ready and not r.service._reconcile_error, (r.service._reconcile_error, calls, r.service._quote_aliases)
    assert closed['active'] == closed['auto_exit'] == closed['breakeven_enabled'] == 0
    assert closed['avg_exit_price'] == 19900 and closed['exit_filled_quantity'] == 2
    assert closed['trigger_reason'] == 'external_close' and closed['exit_trade_id'] is None
    assert closed['triggered_at'] is None  # a business date is not a fill timestamp
    assert not r.service._public_order(closed)['protection']['fixed_stop']
    with db.read_session(r.path) as conn:
        audit = json.loads(conn.execute('SELECT document FROM external_close_reconciliations').fetchone()[0])
        assert audit['match_method'] == ('unique_entry_fills' if legacy else 'entry_seqno')
        if not legacy:
            identity = conn.execute('SELECT * FROM broker_order_identities WHERE order_id=?', (opened,)).fetchone()
            assert identity['seqno'] == 'platform-entry-1' and identity['ordno'] == 'entry-1'
        saved = conn.execute("SELECT * FROM pnl_fills WHERE order_id=? AND phase='exit'", (opened,)).fetchall()
        assert len(saved) == 2 and all(row['filled_at'] == '2026-10-30' for row in saved)
        report, _, pending, _, _ = pnl_calendar.report(conn, 'simulation', '2026-10')
        assert pending == 0 and sum(row['gross_pnl'] for row in report) == (-2000 if action == 'Buy' else 2000)
    for _ in range(3):
        r.runtime.step()
    assert len(calls) == 2 and len(r.broker.trades) == 1
    # Recreate the service: neither accounting nor a second exit is replayed.
    r.service.close()
    restarted = r.create()
    restarted.ensure_connected()
    restarted._sync_managed_orders(force=True)
    assert restarted._broker_ready and len(calls) == 2 and len(r.broker.trades) == 1


@pytest.mark.parametrize('corruption', ['quantity', 'same_average', 'wrong_seqno', 'wrong_date', 'wrong_account',
                                       'incomplete_ledger', 'no_evidence', 'bad_price', 'wrong_direction', 'wrong_contract'])
def test_incomplete_or_conflicting_evidence_keeps_gate_closed(rig, corruption):
    r = rig
    opened = enter(r, 'open')
    records = evidence(r, opened)
    if corruption == 'quantity':
        records.pop()
    elif corruption == 'same_average':
        records[0].entry_price, records[1].entry_price = 19999, 20001
    elif corruption == 'wrong_seqno':
        for record in records:
            record.entry_seqno = 'another-order'
    elif corruption == 'wrong_date':
        for record in records:
            record.entry_date = '20261029'
    elif corruption == 'wrong_account':
        with db.session(r.path) as conn:
            conn.execute("UPDATE broker_order_identities SET account_key='another-account'")
            conn.commit()
        r.broker.trades.clear()  # a relogin need not return the original order
    elif corruption == 'incomplete_ledger':
        with db.session(r.path) as conn:
            conn.execute("DELETE FROM pnl_fills WHERE order_id=?", (opened,))
            conn.commit()
        r.broker.trades.clear()
    elif corruption == 'no_evidence':
        records.clear()
    elif corruption == 'bad_price':
        records[0].cover_price = 0
    elif corruption == 'wrong_direction':
        for record in records:
            record.direction = 'Sell'
    else:
        for record in records:
            record.code = 'OTHER'
    install(r, records)
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and rows(r)[opened]['active'] == 1
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM external_close_reconciliations').fetchone()[0] == 0


def test_partial_external_close_is_not_falsely_finalized(rig):
    r = rig
    opened = enter(r, 'open')
    install(r, evidence(r, opened)[:1])
    r.broker.position = 1
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and rows(r)[opened]['exit_filled_quantity'] == 0
    assert len(r.broker.trades) == 1


def test_realized_query_failure_is_throttled_and_recovers(rig):
    r = rig
    opened = enter(r, 'open')
    records = evidence(r, opened)
    calls = install(r, records)
    def unavailable(*args, **kwargs):
        raise TimeoutError('private account details')
    r.broker.list_profit_loss_detail = unavailable
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and 'TimeoutError' in r.service._reconcile_error
    assert 'private' not in r.service._reconcile_error
    for _ in range(5):
        r.service._sync_managed_orders(force=True)
    assert len(calls) == 1 and rows(r)[opened]['active'] == 1
    install(r, records)
    r.mono[0] += 61
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and rows(r)[opened]['active'] == 0


def test_legacy_ambiguity_never_guesses_fifo_or_uses_one_group_twice(rig):
    r = rig
    first = enter(r, 'open-1', quantity=1)
    second = enter(r, 'open-2', quantity=1)
    install(r, evidence(r, first, legacy=True) + evidence(r, second, seqno='entry-2', legacy=True))
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and all(row['active'] for row in rows(r).values())


def test_two_explicit_entry_identities_reconcile_atomically(rig):
    r = rig
    first = enter(r, 'open-1', quantity=1)
    records = evidence(r, first)
    second = enter(r, 'open-2', quantity=1)
    records += evidence(r, second, seqno='entry-2')
    install(r, records)
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready and all(not row['active'] for row in rows(r).values())
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM external_close_reconciliations').fetchone()[0] == 2


def test_position_change_during_accounting_cannot_commit_a_stale_close(rig):
    r = rig
    opened = enter(r, 'open')
    records = evidence(r, opened)
    install(r, records)
    def changed(account, **kwargs):
        r.broker.position = 1
        return records
    r.broker.list_profit_loss_detail = changed
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and rows(r)[opened]['active'] == 1
    assert '部位已變動' in r.service._reconcile_error


def test_storage_failure_rolls_back_both_position_and_audit(rig):
    r = rig
    opened = enter(r, 'open')
    install(r, evidence(r, opened))
    with db.session(r.path) as conn:
        conn.execute("""CREATE TRIGGER fail_external BEFORE INSERT ON external_close_reconciliations
            BEGIN SELECT RAISE(ABORT, 'test disk failure'); END""")
        conn.commit()
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and rows(r)[opened]['active'] == 1
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM external_close_reconciliations').fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pnl_fills WHERE phase='exit'").fetchone()[0] == 0


def test_failed_local_exit_identity_is_preserved_only_in_audit(rig):
    r = rig
    opened = enter(r, 'open')
    install(r, evidence(r, opened))
    with db.session(r.path) as conn:
        conn.execute("""UPDATE managed_orders SET exit_trade_id='rejected-local-exit',
            exit_status='Failed',exit_status_code='99Q1',triggered_at='2026-10-30 09:02:10',
            trigger_reason='stop' WHERE id=?""", (opened,))
        conn.commit()
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    closed = rows(r)[opened]
    assert r.service._broker_ready and closed['exit_trade_id'] is None and closed['triggered_at'] is None
    with db.read_session(r.path) as conn:
        audit = json.loads(conn.execute('SELECT document FROM external_close_reconciliations').fetchone()[0])
        assert audit['before']['exit_trade_id'] == 'rejected-local-exit'
        assert audit['before']['exit_status_code'] == '99Q1'


def test_trading_day_respects_weekend_and_unknown_calendar(rig):
    from external_reconciliation import trading_day
    import market_health as mh
    assert trading_day('2026-10-30T20:00:00+08:00', rig.service._calendar) == '2026-11-02'
    assert trading_day('2026-10-31T03:00:00+08:00', rig.service._calendar) == '2026-11-02'
    assert trading_day('2026-10-30T09:00:00+08:00', rig.service._calendar) == '2026-10-30'
    assert trading_day('2026-10-30T09:00:00+08:00', mh.SessionCalendar()) is None


@pytest.mark.parametrize('future_empty', [True, False])
def test_night_session_query_keeps_calendar_history_without_double_counting(rig, future_empty):
    r = rig
    opened = enter(r, 'open')
    records = evidence(r, opened)
    calls = []
    def summaries(account, **kwargs):
        calls.append(kwargs['end_date'])
        return [] if future_empty and kwargs['end_date'] == '2026-11-02' else [NS(id=0, code=r.broker.contract.code)]
    r.broker.list_profit_loss = summaries
    r.broker.list_profit_loss_detail = lambda *args, **kwargs: records
    r.now[0] = pd.Timestamp('2026-10-30 16:00')
    r.broker.position = 0
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready, r.service._reconcile_error
    assert calls == ['2026-10-30', '2026-11-02']
    assert rows(r)[opened]['exit_filled_quantity'] == 2 and len(r.broker.trades) == 1
    with db.read_session(r.path) as conn:
        assert conn.execute("SELECT SUM(quantity) FROM pnl_fills WHERE phase='exit'").fetchone()[0] == 2


def test_identity_schema_migration_preserves_existing_rows(tmp_path):
    import sqlite3
    path = tmp_path / 'legacy-identities.db'
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE broker_order_identities(order_id TEXT PRIMARY KEY,
            broker_trade_id TEXT NOT NULL,account_key TEXT NOT NULL,seqno TEXT NOT NULL,
            entry_date TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        conn.execute('INSERT INTO broker_order_identities VALUES (?,?,?,?,?,?)',
            ('original', 'trade-1', 'account-hash', '123456', '2026-11-13', '2026-11-13 12:29'))
    with db.session(path) as conn:
        row = conn.execute('SELECT * FROM broker_order_identities').fetchone()
        assert row['ordno'] == '' and row['seqno'] == '123456' and row['broker_trade_id'] == 'trade-1'
