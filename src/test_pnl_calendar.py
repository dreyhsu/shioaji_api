"""P&L uses actual dated fills, never order timestamps or limit prices."""
from datetime import datetime
from types import SimpleNamespace as NS
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import db
import pnl_calendar as pnl
from test_trading import fake_service  # reusable offline broker fixture
from test_mock_runtime import replay


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / 'pnl.db'
    with db.session(path):
        pass
    return path


def order(conn, identity='o1', **changes):
    row = dict(id=identity, code='TMFR1', contract_code='TMFX6', action='Buy', quantity=2,
               filled_quantity=2, avg_fill_price=100, price=999, price_type='LMT', order_type='ROD',
               atr=5, simulation=1, entry_trade_id='entry-' + identity, entry_status='Filled',
               exit_trade_id='exit-' + identity, exit_filled_quantity=2, avg_exit_price=999,
               created_at='2026-01-01 00:00:00', updated_at='2026-12-31 23:00:00', active=0)
    row.update(changes)
    conn.execute(f"INSERT INTO managed_orders ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
    return row


def fill(at='2026-11-08T10:00:00+08:00', price=110, quantity=1, seq=None):
    return NS(datetime=at, price=price, quantity=quantity, seq=seq)


def trade(*fills):
    return NS(status=NS(deals=list(fills)))


def save(conn, row, exits=None, entries=None, **kwargs):
    pnl.save_fills(conn, row,
        entry=entries or trade(fill('2026-11-07T10:00:00+08:00', price=100, quantity=2)),
        exit_trade=exits or trade(fill(quantity=2)), multiplier=kwargs.get('multiplier', 10),
        currency=kwargs.get('currency', 'TWD'), observed_at='2026-11-08T11:00:00+08:00')


def client_for(path, mode='simulation'):
    app = FastAPI()
    app.include_router(pnl.create_router(lambda: dict(db_path=path, mode=mode,
        now=lambda: datetime(2026, 11, 8))))
    return TestClient(app)


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_partial_exits_cross_midnight_and_month(ledger, action, direction):
    with db.session(ledger) as conn:
        row = order(conn, action=action)
        exits = trade(fill('2026-10-31T15:59:59+00:00', price=110),
                      fill('2026-10-31T16:00:00+00:00', price=90))
        save(conn, row, exits)
        oct_rows, _, pending, _, _ = pnl.report(conn, 'simulation', '2026-10')
        nov_rows, _, _, _, _ = pnl.report(conn, 'simulation', '2026-11')
        assert pending == 0
        assert oct_rows[0]['date'] == '2026-10-31'
        assert nov_rows[0]['date'] == '2026-11-01'
        assert oct_rows[0]['gross_pnl'] == 100 * direction
        assert nov_rows[0]['gross_pnl'] == -100 * direction


def test_duplicate_sync_identical_anonymous_fills_and_seq_enrichment(ledger):
    with db.session(ledger) as conn:
        row = order(conn)
        exits = trade(fill(), fill())
        for _ in range(3):
            save(conn, row, exits)
        assert conn.execute("SELECT COUNT(*) FROM pnl_fills WHERE phase='exit'").fetchone()[0] == 2
        rows, _, pending, _, _ = pnl.report(conn, 'simulation', '2026-11')
        assert pending == 0 and pnl.aggregate(rows) == dict(gross_pnl=200, order_count=1, quantity=2)
        conn.commit()
    # Restart and later arrival of native identities must replace, not append.
    with db.session(ledger) as conn:
        save(conn, row, trade(fill(seq='a'), fill(seq='b')))
        assert conn.execute("SELECT COUNT(*) FROM pnl_fills WHERE phase='exit'").fetchone()[0] == 2
        assert len(pnl.report(conn, 'simulation', '2026-11')[0]) == 2


def test_daily_and_monthly_counts_are_distinct_orders(ledger):
    with db.session(ledger) as conn:
        row = order(conn)
        save(conn, row, trade(fill('2026-11-08T10:00:00'), fill('2026-11-09T10:00:00')))
        conn.commit()
    client = client_for(ledger)
    data = client.get('/api/pnl-calendar?month=2026-11').json()
    assert data['summary'] == dict(gross_pnl=200, order_count=1, quantity=2)
    assert sum(day['order_count'] for day in data['days']) == 2
    day = client.get('/api/pnl-calendar/day?date=2026-11-08').json()
    assert len(day['fills']) == 1 and day['summary']['gross_pnl'] == 100
    assert day['fills'][0]['entry_price'] == 100  # ignores saved limit/exit trigger prices


@pytest.mark.parametrize('problem', ['time', 'price', 'multiplier', 'currency', 'quantity', 'entry', 'duplicate_seq'])
def test_unreliable_records_are_excluded_and_recover(ledger, problem):
    with db.session(ledger) as conn:
        row = order(conn)
        exits = trade(fill(quantity=2))
        kwargs = {}
        if problem == 'time': exits.status.deals[0].datetime = None
        if problem == 'price': exits.status.deals[0].price = float('nan')
        if problem == 'multiplier': kwargs['multiplier'] = None
        if problem == 'currency': kwargs['currency'] = 'USD'
        if problem == 'quantity': exits.status.deals[0].quantity = 1
        if problem == 'entry': kwargs['entries'] = trade(fill(price=100, quantity=1))
        if problem == 'duplicate_seq': exits = trade(fill(seq='same'), fill(seq='same'))
        save(conn, row, exits, **kwargs)
        rows, _, pending, _, _ = pnl.report(conn, 'simulation', '2026-11')
        assert not rows and pending == 1
        # Currency changes are not silently allowed for a persisted contract.
        if problem != 'currency':
            save(conn, row)
            assert pnl.report(conn, 'simulation', '2026-11')[2] == 0


def test_empty_month_filters_modes_validation_and_legacy_read_only(ledger):
    with db.session(ledger) as conn:
        save(conn, order(conn))
        save(conn, order(conn, 'production', simulation=0, contract_code='TMFZ6'))
        order(conn, 'legacy')
        conn.commit()
    client = client_for(ledger)
    result = client.get('/api/pnl-calendar?month=2026-11').json()
    assert result['summary']['quantity'] == 2 and result['incomplete_order_count'] == 1
    assert result['contracts'] == ['TMFX6']
    assert client.get('/api/pnl-calendar?month=2026-11&contract_code=unknown').json()['summary']['quantity'] == 0
    assert client_for(ledger, 'production').get('/api/pnl-calendar?month=2026-11').json()['contracts'] == ['TMFZ6']
    assert client.get('/api/pnl-calendar?month=2024-02').json()['days'][-1]['date'] == '2024-02-29'
    assert client.get('/api/pnl-calendar?month=2026-13').status_code == 422
    assert client.get('/api/pnl-calendar/day?date=2026-02-30').status_code == 422
    assert client.get('/pnl-calendar').status_code == 200
    with db.session(ledger) as conn:
        conn.execute('DROP TABLE pnl_fills')
        conn.execute('DROP TABLE pnl_order_metadata')
        conn.commit()
    result = client.get('/api/pnl-calendar?month=2026-11').json()
    assert not result['ledger_available'] and result['incomplete_order_count'] == 2
    with db.read_session(ledger) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='pnl_fills'").fetchone() is None
    missing = client_for(ledger.parent / 'absent.db')
    assert missing.get('/api/pnl-calendar?month=2026-11').status_code == 503
    assert not (ledger.parent / 'absent.db').exists()


def test_epoch_timestamp_is_converted_to_taipei():
    stamp = datetime.fromisoformat('2026-11-07T16:00:00+00:00').timestamp()
    assert pnl.fill_time(NS(ts=stamp)).startswith('2026-11-08T00:00:00')


def test_duplicate_native_fill_is_counted_once():
    rows = pnl.normalized_fills(trade(fill(seq='same'), fill(seq='same')))
    assert len(rows) == 1 and rows[0][3] == 1


def test_trade_hook_persists_then_recovers_late_details_without_new_queries(fake_service, monkeypatch):
    service, api, _ = fake_service
    api.Contracts.Futures.TMF[0].multiplier = 10
    api.Contracts.Futures.TMF[0].currency = 'TWD'
    result = service.place_order(dict(confirmed=True, simulation=True, contract_code='TMF202611',
        action='Buy', quantity=2, price=46270, price_type='LMT', order_type='ROD'))
    entry = api.trades[0]
    entry.status.status = 'Filled'
    entry.status.deal_quantity = 2
    entry.status.deals = [fill(price=46270, quantity=2)]
    service._persist_trade_state(result['id'], entry=entry)
    exit_trade = NS(status=NS(status='Filled', deal_quantity=2, deals=[NS(price=46280, quantity=2)]))
    with db.session(service.db_path) as conn:
        conn.execute("UPDATE managed_orders SET exit_trade_id='exit1' WHERE id=?", (result['id'],))
        conn.commit()
    service._persist_trade_state(result['id'], exit_trade=exit_trade)
    client = client_for(service.db_path)
    assert client.get('/api/pnl-calendar?month=2026-11').json()['incomplete_order_count'] == 1
    exit_trade.status.deals = [fill(price=46280, quantity=2)]
    # Closed orders are no longer in the active monitor loop; same-cache recovery is essential.
    service._sync_pnl_history({entry.order.id: entry, 'exit1': exit_trade})
    monkeypatch.setattr(service, 'ensure_connected', lambda: pytest.fail('calendar must never connect'))
    data = client.get('/api/pnl-calendar?month=2026-11').json()
    assert data['summary']['gross_pnl'] == 200 and data['incomplete_order_count'] == 0


def test_ledger_failure_cannot_rollback_confirmed_order(fake_service, monkeypatch):
    service, api, _ = fake_service
    result = service.place_order(dict(confirmed=True, simulation=True, contract_code='TMF202611',
        action='Buy', quantity=2, price=46270, price_type='LMT', order_type='ROD'))
    entry = api.trades[0]
    entry.status.status, entry.status.deal_quantity = 'Filled', 2
    entry.status.deals = [fill(price=46270, quantity=2)]
    monkeypatch.setattr(pnl, 'save_fills', lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError('offline test')))
    service._persist_trade_state(result['id'], entry=entry)
    assert service._active_order(result['id'])['filled_quantity'] == 2


def test_weighted_cost_and_progressive_partial_exits(ledger):
    with db.session(ledger) as conn:
        row = order(conn, exit_filled_quantity=1, active=1)
        entries = trade(fill(price=100), fill(price=120))
        save(conn, row, trade(fill(price=130)), entries)
        rows = pnl.report(conn, 'simulation', '2026-11')[0]
        assert rows[0]['entry_price'] == 110 and pnl.aggregate(rows)['gross_pnl'] == 200
        row.update(exit_filled_quantity=2, active=0)
        conn.execute('UPDATE managed_orders SET exit_filled_quantity=2, active=0')
        save(conn, row, trade(fill(price=130), fill('2026-11-09T01:00:00+08:00', price=90)), entries)
        rows = pnl.report(conn, 'simulation', '2026-11')[0]
        assert pnl.aggregate(rows) == dict(gross_pnl=0, quantity=2, order_count=1)
        assert [r['gross_pnl'] for r in rows] == [200, -200]


def test_unknown_metadata_can_be_enriched_later(ledger):
    with db.session(ledger) as conn:
        row = order(conn)
        save(conn, row, multiplier=None, currency='')
        assert pnl.report(conn, 'simulation', '2026-11')[2] == 1
        save(conn, row)
        assert pnl.report(conn, 'simulation', '2026-11')[2] == 0


def test_mock_pnl_uses_replay_clock_and_reset_clears_ledger(replay):
    from test_mock_runtime import manual_order
    runtime, client = replay
    row = manual_order(client, 'Buy')
    runtime.advance(150)
    assert runtime.service._active_order(row['id'])['exit_filled_quantity'] == 1
    month = runtime.now().strftime('%Y-%m')
    data = client.get('/api/pnl-calendar', params=dict(month=month)).json()
    assert data['mode'] == 'mock' and data['today'] == runtime.now().date().isoformat()
    assert data['summary']['quantity'] == 1 and data['incomplete_order_count'] == 0
    runtime.reset()
    data = client.get('/api/pnl-calendar', params=dict(month=month)).json()
    assert data['summary']['quantity'] == 0 and data['incomplete_order_count'] == 0
    with db.read_session(runtime.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM pnl_fills').fetchone()[0] == 0
