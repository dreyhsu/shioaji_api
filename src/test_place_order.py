from dataclasses import replace
from unittest.mock import Mock
import subprocess
import sys

import pytest
from place_order import OrderContext, OrderRequest, OrderResult, OrderSubmissionError, place_order


@pytest.fixture
def order():
    return OrderRequest("TMF202611", "Buy", 1, 20001, confirmed=True)


@pytest.mark.parametrize("action,price_type,order_type,expected", [
    ("Buy", "LMT", "ROD", 20000), ("Sell", "LMT", "IOC", 20000),
    ("Buy", "MKT", "IOC", 0), ("Sell", "MKP", "FOK", 0),
])
def test_order_parameters_and_exactly_one_submission(order, action, price_type, order_type, expected):
    order = replace(order, action=action, price_type=price_type, order_type=order_type)
    context = OrderContext("TMFX6", 5, True)
    broker = Mock()
    broker.submit_order.return_value = OrderResult("paper-1", "Submitted", expected)
    result = place_order(order, context=context, broker=broker)
    assert result.trade_id == "paper-1"
    broker.submit_order.assert_called_once_with(order, context, expected)


@pytest.mark.parametrize("changes", [
    {"confirmed": False}, {"simulation": False}, {"quantity": 0}, {"quantity": 101},
    {"quantity": 1.5}, {"quantity": True}, {"action": "hold"}, {"price": float("nan")},
    {"price": float("inf")}, {"price": -1}, {"price": 0}, {"contract_code": ""},
    {"price_type": "bad"}, {"order_type": "bad"}, {"price": "20000"},
])
def test_invalid_inputs_never_submit(order, changes):
    broker = Mock()
    with pytest.raises(ValueError):
        place_order(replace(order, **changes), context=OrderContext("TMFX6", 1, True), broker=broker)
    broker.submit_order.assert_not_called()


@pytest.mark.parametrize("tick", [0, -1, float("nan"), float("inf")])
def test_invalid_tick_never_submits(order, tick):
    broker = Mock()
    with pytest.raises(ValueError):
        place_order(order, context=OrderContext("TMFX6", tick, True), broker=broker)
    broker.submit_order.assert_not_called()


@pytest.mark.parametrize("failure", [TimeoutError("timeout"), OrderResult("", "Unknown", 20001)])
def test_uncertain_order_is_not_retried(order, failure):
    broker = Mock()
    if isinstance(failure, Exception):
        broker.submit_order.side_effect = failure
    else:
        broker.submit_order.return_value = failure
    with pytest.raises(OrderSubmissionError):
        place_order(order, context=OrderContext("TMFX6", 1, True), broker=broker)
    assert broker.submit_order.call_count == 1


def test_import_has_no_application_or_broker_dependencies():
    code = "import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError('network')); import place_order, sys; assert not {'dashboard','trading','shioaji','db','dotenv'} & sys.modules.keys()"
    from pathlib import Path
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
