"""New risk API follows one engine in Mock, simulation and production doubles."""
import pytest

from test_trading_parity import market, save_curve
from trading import align_tick

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('action,direction', [('Buy', 1), ('Sell', -1)])
@pytest.mark.parametrize('curve_mode', [False, True])
def test_risk_api_breakeven_and_exit_share_all_transports(market, action, direction, curve_mode):
    driver = market(start=3600)
    response = driver.client.post('/api/trading/orders', json={
        'contract_code': driver.broker.contract.symbol, 'action': action, 'quantity': 2,
        'price': 0, 'price_type': 'MKT', 'order_type': 'IOC',
        'confirmed': True, 'simulation': driver.simulation,
    })
    assert response.status_code == 200, response.text
    driver.service.process_market_update()
    order = driver.client.get('/api/trading/orders').json()[0]
    entry = order['avg_fill_price']
    curve = save_curve(driver, entry + direction * 800) if curve_mode else None
    payload = dict(curve_id=curve, stop_price=align_tick(entry - direction * 500, 1),
                   target_price=None if curve_mode else align_tick(entry + direction * 1000, 1),
                   breakeven_enabled=True, risk_revision=order['risk_revision'])
    response = driver.client.put(f"/api/trading/orders/{order['id']}/risk-settings", json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()
    driver.broker.publish(entry + direction * 100)
    assert not driver.service._active_order(order['id'])['breakeven_triggered_at']
    driver.broker.publish(entry + direction * 101)
    protected = driver.client.get('/api/trading/orders').json()[0]
    assert protected['stop_price'] == align_tick(entry + direction * 5, 1, direction)
    assert protected['breakeven_triggered_at'] and protected['breakeven_enabled']
    assert protected['target_price'] == saved['target_price']
    assert protected['curve_id'] == curve
    stale = driver.client.put(f"/api/trading/orders/{order['id']}/risk-settings", json={**payload, 'risk_revision': saved['risk_revision']})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'risk_conflict'
    driver.broker.publish(protected['stop_price'] - direction)
    driver.service.process_market_update()
    closed = driver.client.get('/api/trading/orders').json()[0]
    assert closed['trigger_reason'] == 'stop' and closed['exit_status'] == 'Filled'
    assert closed['exit_filled_quantity'] == 2 and not closed['active']
    assert driver.broker.position == 0 and len(driver.broker.trades) == 2
