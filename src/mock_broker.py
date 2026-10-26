"""In-memory broker: quotes, full fills, resting limits, cancellation and margin.

Market/MKP orders fill at the latest replay close, limits at that close only
when marketable. IOC/FOK cancel otherwise. Infinite liquidity, no fees, latency
or partial fills. No sockets, credentials or Shioaji import.
"""
from __future__ import annotations

from types import SimpleNamespace as NS
import math
from mock_config import BrokerConfig, load_mock_config


def _text(value):
    return str(getattr(value, "value", value))


class MockBroker:
    sdk = NS(
        Action=NS(Buy="Buy", Sell="Sell"),
        FuturesPriceType=NS(LMT="LMT", MKT="MKT", MKP="MKP"),
        OrderType=NS(ROD="ROD", IOC="IOC", FOK="FOK"),
        FuturesOCType=NS(Auto="Auto", Cover="Cover"),
        QuoteType=NS(Tick="Tick"), QuoteVersion=NS(v1="v1"), Order=NS,
    )

    def __init__(self, *, clock, root="TMF", settings: BrokerConfig | None = None):
        self.clock = clock
        self.settings = settings if settings is not None else load_mock_config().broker
        self.contract = NS(code=f"{root}_MOCK", symbol=f"{root}209912", target_code=f"{root}_MOCK",
                           delivery_month="209912", delivery_date="2099/12/31", name="離線 Mock 期貨", tick=1)
        self.Contracts = NS(Futures=NS(**{root: [self.contract]}))
        self.futopt_account = NS(account_id="OFFLINE-MOCK")
        self.trades = []
        self.price = None
        self.position = 0
        self._callback = lambda quote: None
        self.quote = NS(subscribe=self._subscribe, unsubscribe=lambda *a, **k: None)

    def set_on_tick_fop_v1_callback(self, callback):
        self._callback = callback

    def _subscribe(self, *args, **kwargs):
        if self.price is not None:
            self.publish(self.price)

    def publish(self, price):
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Mock price must be finite and positive")
        self.price = float(price)
        for trade in self.trades:
            if trade.status.status == "Submitted":
                self._match(trade)
        self._callback(NS(code=self.contract.code, close=self.price, datetime=self.clock()))

    def _match(self, trade):
        order, status = trade.order, trade.status
        action = _text(order.action)
        marketable = (_text(order.price_type) != "LMT" or
                      (self.price <= order.price if action == "Buy" else self.price >= order.price))
        if marketable:
            if _text(order.octype) == "Cover":
                available = max(0, self.position if action == "Sell" else -self.position)
                if order.quantity > available:
                    status.status = "Rejected"
                    return
            status.status = "Filled"
            status.deal_quantity = order.quantity
            status.deals = [NS(quantity=order.quantity, price=self.price)]
            status.average_price = self.price
            self.position += order.quantity * (1 if action == "Buy" else -1)
        elif _text(order.order_type) in {"IOC", "FOK"}:
            status.status = "Cancelled"

    def place_order(self, contract, order):
        if contract.code != self.contract.code or self.price is None:
            raise ValueError("Mock contract or quote unavailable")
        if _text(order.action) not in {"Buy", "Sell"} or not isinstance(order.quantity, int) or order.quantity <= 0:
            raise ValueError("Invalid mock order")
        order.id = f"mock-{len(self.trades) + 1}"
        trade = NS(order=order, status=NS(id=order.id, status="Submitted", deal_quantity=0, deals=[]))
        self.trades.append(trade)
        self._match(trade)
        return trade

    def cancel_order(self, trade):
        if trade.status.status == "Submitted":
            trade.status.status = "Cancelled"
        return trade

    def update_status(self, account):
        pass

    def list_trades(self):
        return list(self.trades)

    def margin(self, account):
        initial = abs(self.position) * self.settings.initial_margin_per_contract
        return NS(status="Mock", risk_indicator=999.0,
                  maintenance_margin=abs(self.position) * self.settings.maintenance_margin_per_contract,
                  initial_margin=initial, available_margin=self.settings.equity - initial, equity=self.settings.equity)

    def logout(self):
        pass
