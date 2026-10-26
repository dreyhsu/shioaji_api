"""Validate and submit one order through an injected broker; no I/O on import.

Input: OrderRequest, resolved OrderContext and BrokerPort.
Output: OrderResult. Invalid input raises ValueError before submission; an
uncertain submission raises OrderSubmissionError and is never retried here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol


class OrderSubmissionError(RuntimeError):
    """The broker may have received an order. Reconcile before attempting again."""


@dataclass(frozen=True)
class OrderRequest:
    contract_code: str
    action: str
    quantity: int
    price: float = 0
    price_type: str = "LMT"
    order_type: str = "ROD"
    simulation: bool = True
    confirmed: bool = False


@dataclass(frozen=True)
class OrderContext:
    contract_code: str
    tick: float
    simulation: bool


@dataclass(frozen=True)
class OrderResult:
    trade_id: str
    status: str
    price: float


class BrokerPort(Protocol):
    def submit_order(self, request: OrderRequest, context: OrderContext, price: float) -> OrderResult: ...


def validate_request(request: OrderRequest, simulation: bool) -> None:
    if request.confirmed is not True:
        raise ValueError("送單前必須確認委託摘要")
    if not isinstance(request.simulation, bool) or request.simulation != simulation:
        raise ValueError("下單環境已改變，請重新確認目前環境後再送單")
    if not isinstance(request.contract_code, str) or not request.contract_code.strip():
        raise ValueError("請選擇有效合約")
    if request.action not in {"Buy", "Sell"}:
        raise ValueError("請選擇買進或賣出")
    if isinstance(request.quantity, bool) or not isinstance(request.quantity, int) or not 1 <= request.quantity <= 100:
        raise ValueError("口數必須為 1 到 100 的整數")
    if request.price_type not in {"LMT", "MKT", "MKP"} or request.order_type not in {"ROD", "IOC", "FOK"}:
        raise ValueError("不支援的價格類別或委託條件")
    try:
        valid = not isinstance(request.price, bool) and math.isfinite(request.price) and request.price >= 0
    except TypeError:
        valid = False
    if not valid or (request.price_type == "LMT" and request.price <= 0):
        raise ValueError("委託價格必須是有效數字，限價必須大於 0")


def place_order(request: OrderRequest, *, context: OrderContext, broker: BrokerPort) -> OrderResult:
    validate_request(request, context.simulation)
    if not context.contract_code or not math.isfinite(context.tick) or context.tick <= 0:
        raise ValueError("合約與最小跳動單位無效")
    price = round(round(request.price / context.tick) * context.tick, 8) if request.price_type == "LMT" else 0
    if request.price_type == "LMT" and price <= 0:
        raise ValueError("依最小跳動單位調整後限價必須大於 0")
    try:
        result = broker.submit_order(request, context, price)
        if not result.trade_id:
            raise OrderSubmissionError("券商沒有回傳委託編號，請先核對委託狀態")
        return result
    except OrderSubmissionError:
        raise
    except Exception as error:
        raise OrderSubmissionError(f"委託狀態不明，請先核對券商端：{error}") from error
