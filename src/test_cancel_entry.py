"""Cancel requests reconcile delayed broker results without closing filled positions."""
from types import SimpleNamespace

from test_trading import fake_service


def pending(service, quantity=1):
    return service.place_order({
        "confirmed": True, "simulation": True, "contract_code": "TMF202611",
        "action": "Buy", "quantity": quantity, "price": 45000,
        "price_type": "LMT", "order_type": "ROD",
    })


def test_cancel_reconciles_confirmation_after_sdk_returns_pending(fake_service):
    service, api, _ = fake_service
    order = pending(service)
    requested = []

    def cancel(trade):
        requested.append(trade)
        return trade  # The immediate SDK response has not confirmed cancellation.

    def sync(account):
        assert requested, "Status refresh must not delay sending the cancel"
        requested[0].status.status = "Cancelled"

    api.cancel_order, api.update_status = cancel, sync
    result = service.cancel_entry(order["id"])
    assert len(requested) == 1
    assert result["entry_status"] == "Cancelled"
    assert result["filled_quantity"] == 0 and result["active"] == 0


def test_unconfirmed_cancel_is_never_reported_as_cancelled(fake_service):
    service, api, _ = fake_service
    order = pending(service)
    api.trades[0].status.status = "Submitted"
    requests = []
    api.cancel_order = lambda trade: requests.append(trade) or trade
    result = service.cancel_entry(order["id"])
    assert result["entry_status"] == "Submitted" and result["active"] == 1
    assert len(requests) == 1
    api.trades[0].status.status = "Cancelled"
    service.process_market_update()
    assert service._active_order(order["id"])["active"] == 0
    assert len(requests) == 1


def test_cancel_retains_partial_fill_and_does_not_submit_exit(fake_service):
    service, api, _ = fake_service
    order = pending(service, quantity=3)
    trade = api.trades[0]
    trade.status.status = "PartFilled"
    trade.status.deal_quantity = 1
    trade.status.deals = [SimpleNamespace(quantity=1, price=45000)]
    result = service.cancel_entry(order["id"])
    assert result["entry_status"] == "Cancelled"
    assert result["filled_quantity"] == 1 and result["active"] == 1
    assert result["exit_trade_id"] is None and len(api.sent_orders) == 1


def test_fill_after_ui_snapshot_is_reconciled_without_cancel_request(fake_service):
    service, api, _ = fake_service
    order = pending(service)
    trade = api.trades[0]
    trade.status.status = "Filled"
    trade.status.deal_quantity = 1
    trade.status.deals = [SimpleNamespace(quantity=1, price=45000)]
    result = service.cancel_entry(order["id"])
    assert result["entry_status"] == "Filled" and result["filled_quantity"] == 1
    assert result["active"] == 1 and not api.cancelled


def test_external_cancel_after_ui_snapshot_is_not_submitted_again(fake_service):
    service, api, _ = fake_service
    order = pending(service)
    api.trades[0].status.status = "Cancelled"
    result = service.cancel_entry(order["id"])
    assert result["entry_status"] == "Cancelled" and result["active"] == 0
    assert not api.cancelled
