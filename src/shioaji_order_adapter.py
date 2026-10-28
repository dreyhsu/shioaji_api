"""Translate a broker-neutral order using an already-connected Shioaji API."""
from __future__ import annotations

from place_order import OrderResult


class ShioajiOrderAdapter:
    def __init__(self, api, contract, *, sdk=None, timeout_ms=None):
        if sdk is None:
            import shioaji as sdk
        self.api, self.contract, self.sdk = api, contract, sdk
        self.timeout_ms = timeout_ms

    def submit_order(self, request, context, price):
        sj = self.sdk
        order = sj.Order(
            action=getattr(sj.Action, request.action), price=price, quantity=request.quantity,
            price_type=getattr(sj.FuturesPriceType, request.price_type),
            order_type=getattr(sj.OrderType, request.order_type),
            octype=sj.FuturesOCType.Auto, account=self.api.futopt_account,
        )
        kwargs = {} if self.timeout_ms is None else {"timeout": self.timeout_ms}
        trade = self.api.place_order(self.contract, order, **kwargs)
        trade_id = getattr(trade.order, "id", None) or getattr(trade.status, "id", None)
        status = getattr(trade.status.status, "value", trade.status.status)
        return OrderResult(str(trade_id) if trade_id else "", str(status), price)
