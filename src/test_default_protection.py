"""New-order defaults and protective ticks without a risk-settings save."""
from types import SimpleNamespace

import pandas as pd
import pytest

import trading
from test_trading import fake_service
from test_independent_trade import rig, request


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
@pytest.mark.parametrize('price_type', ['LMT', 'MKT'])
@pytest.mark.parametrize('quantity', [1, 3])
def test_new_orders_enable_all_protections_and_reprice_from_fill(fake_service, action, direction, price_type, quantity):
    service, api, _ = fake_service
    service._now = lambda: pd.Timestamp('2026-10-05 11:00:00')
    order = service.place_order(dict(contract_code='TMF202611', action=action, quantity=quantity,
        price=46270 if price_type == 'LMT' else 0, price_type=price_type,
        order_type='ROD' if price_type == 'LMT' else 'IOC', simulation=True, confirmed=True))
    assert order['auto_exit'] == order['breakeven_enabled'] == order['profit_lock_enabled'] == 1
    service._now = lambda: pd.Timestamp('2026-10-05 11:05:00')
    entry = api.trades[-1]
    entry.status.status = 'Filled'
    entry.status.deal_quantity = quantity
    entry.status.deals = [SimpleNamespace(quantity=quantity, price=46280)]
    service._persist_trade_state(order['id'], entry=entry)
    row = service._active_order(order['id'])
    assert row['stop_price'] == 46280 - direction * 60
    assert row['target_price'] == 46280 + direction * 240
    assert pd.Timestamp(row['profit_lock_started_at']) == service._now()
    service._now = lambda: pd.Timestamp('2026-10-05 11:05:01')
    service._persist_trade_state(order['id'], entry=entry)
    assert service._active_order(order['id'])['profit_lock_started_at'] == row['profit_lock_started_at']
    restarted = trading.TradingService(service.db_path, service.code, simulation=True, start_worker=False)
    try:
        restored = restarted.orders()[0]
        assert restored['auto_exit'] == restored['breakeven_enabled'] == restored['profit_lock_enabled'] == 1
        assert restored['target_price'] == row['target_price']
    finally:
        restarted.close()


@pytest.mark.integration
@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
def test_default_profit_lock_tracks_and_exits_without_manual_enable(rig, action, direction):
    r = rig
    r.service._risk_context = lambda code: (500., pd.DataFrame())
    assert request(r, payload=dict(contract_code='TMF209912', action=action, quantity=3,
        price=0, price_type='MKT', order_type='IOC', simulation=True, confirmed=True)).status_code == 202
    r.runtime.step()
    r.runtime.step()
    order = r.service.orders()[0]
    assert order['auto_exit'] == order['breakeven_enabled'] == order['profit_lock_enabled'] == 1
    assert order['target_price'] == 20000 + direction * 1000
    r.update('2026-10-30 09:02:05', tick=20000 + direction * 400)
    r.runtime.step()
    protected = r.service.orders()[0]
    assert protected['breakeven_triggered_at'] and protected['profit_lock_triggered_at']
    assert protected['profit_lock_peak_points'] == 400
    assert protected['stop_price'] == 20000 + direction * 200
    r.update('2026-10-30 09:02:06', tick=20000 + direction * 150)
    r.runtime.step()
    r.runtime.step()
    closed = r.service.orders()[0]
    assert closed['trigger_reason'] == 'stop' and closed['exit_status'] == 'Filled'
    assert closed['exit_filled_quantity'] == 3 and not closed['active']
    assert len(r.broker.trades) == 2
