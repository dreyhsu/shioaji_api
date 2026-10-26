from types import SimpleNamespace as NS
from unittest.mock import Mock
import pandas as pd
import pytest
from mock_broker import MockBroker
from mock_config import BrokerConfig


@pytest.fixture
def broker():
    result = MockBroker(clock=lambda: pd.Timestamp("2026-10-05 09:00"), settings=BrokerConfig())
    result.publish(20000)
    return result


def order(**changes):
    return NS(**({"action": "Buy", "quantity": 2, "price": 0, "price_type": "MKT", "order_type": "IOC", "octype": "Auto"} | changes))


def test_market_fill_quote_callback_and_account(broker):
    callback = Mock()
    broker.set_on_tick_fop_v1_callback(callback)
    broker.quote.subscribe(broker.contract)
    callback.assert_called_once()
    assert callback.call_args.args[0].close == 20000
    trade = broker.place_order(broker.contract, order())
    assert trade.status.status == "Filled"
    assert trade.status.deals[0].price == 20000
    assert broker.position == 2
    assert broker.margin(broker.futopt_account).initial_margin == 44000
    close = broker.place_order(broker.contract, order(action="Sell", octype="Cover"))
    assert close.status.status == "Filled" and broker.position == 0


def test_limit_waits_for_marketable_price_and_can_be_cancelled(broker):
    pending = broker.place_order(broker.contract, order(price_type="LMT", price=19900, order_type="ROD"))
    assert pending.status.status == "Submitted"
    broker.publish(19950)
    assert pending.status.status == "Submitted"
    broker.publish(19890)
    assert pending.status.status == "Filled" and pending.status.average_price == 19890
    pending = broker.place_order(broker.contract, order(price_type="LMT", price=19000, order_type="ROD"))
    broker.cancel_order(pending)
    broker.publish(18000)
    assert pending.status.status == "Cancelled" and broker.position == 2


@pytest.mark.parametrize("condition", ["IOC", "FOK"])
def test_unmarketable_immediate_limit_cancels(broker, condition):
    trade = broker.place_order(broker.contract, order(price_type="LMT", price=19900, order_type=condition))
    assert trade.status.status == "Cancelled" and broker.position == 0


def test_cover_cannot_create_opposite_position(broker):
    trade = broker.place_order(broker.contract, order(action="Sell", octype="Cover"))
    assert trade.status.status == "Rejected" and broker.position == 0


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_invalid_quote_is_rejected(broker, price):
    with pytest.raises(ValueError):
        broker.publish(price)


def test_custom_account_margin():
    broker = MockBroker(clock=lambda: pd.Timestamp("2026-10-05 09:00"), settings=BrokerConfig(
        equity=500000, initial_margin_per_contract=10000, maintenance_margin_per_contract=8000))
    broker.publish(20000)
    broker.place_order(broker.contract, order())
    margin = broker.margin(None)
    assert margin.equity == 500000
    assert margin.initial_margin == 20000
    assert margin.maintenance_margin == 16000
    assert margin.available_margin == 480000
