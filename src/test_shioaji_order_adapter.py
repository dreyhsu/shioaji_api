from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from place_order import OrderRequest, OrderContext, place_order
from shioaji_order_adapter import ShioajiOrderAdapter
from mock_broker import MockBroker


@pytest.mark.parametrize("action,price_type,order_type", [("Buy", "LMT", "ROD"), ("Sell", "MKT", "IOC"), ("Buy", "MKP", "FOK")])
def test_sdk_translation_uses_injected_api(action, price_type, order_type):
    api = Mock()
    api.place_order.return_value = NS(order=NS(id="adapter-1"), status=NS(status=NS(value="Submitted")))
    contract = NS(code="TMFX6")
    request = OrderRequest("TMF202611", action, 2, 20000, price_type, order_type, confirmed=True)
    result = place_order(request, context=OrderContext(contract.code, 1, True),
                         broker=ShioajiOrderAdapter(api, contract, sdk=MockBroker.sdk))
    api.place_order.assert_called_once()
    actual_contract, order = api.place_order.call_args.args
    assert actual_contract is contract
    assert (order.action, order.quantity, order.price_type, order.order_type, order.octype) == (action, 2, price_type, order_type, "Auto")
    assert order.account is api.futopt_account
    assert order.price == (20000 if price_type == "LMT" else 0)
    assert (result.trade_id, result.status) == ("adapter-1", "Submitted")
