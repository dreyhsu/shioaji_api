"""Shared request validation for Dashboard and the independent trade API."""
from typing import Literal
from pydantic import BaseModel, Field, StrictBool, model_validator
from place_order import validate_futures_order_types

class OrderIn(BaseModel):
    contract_code: str = Field(min_length=1, max_length=24)
    action: Literal["Buy", "Sell"]
    quantity: int = Field(ge=1, le=100, strict=True)
    price: float = Field(ge=0, allow_inf_nan=False)
    price_type: Literal["LMT", "MKT", "MKP"] = "LMT"
    order_type: Literal["ROD", "IOC", "FOK"] = "ROD"
    curve_id: int | None = None
    simulation: StrictBool
    confirmed: StrictBool = False

    @model_validator(mode="after")
    def validate_order_types(self):
        validate_futures_order_types(self.price_type, self.order_type)
        return self


class TradeLevelsIn(BaseModel):
    stop_price: float
    target_price: float | None = None


class TradeCurveIn(BaseModel):
    target_price: float


class TradeCurveSelectionIn(BaseModel):
    curve_id: int | None = None


class TradeRiskSettingsIn(BaseModel):
    curve_id: int | None = Field(default=None, ge=1)
    stop_price: float = Field(gt=0, allow_inf_nan=False)
    target_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    breakeven_enabled: StrictBool
    breakeven_trigger_points: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    breakeven_offset_points: float | None = Field(default=None, ge=0, allow_inf_nan=False, strict=True)
    risk_revision: int = Field(ge=0, strict=True)
    confirmed: StrictBool = False


class ContractSubscriptionIn(BaseModel):
    contract_code: str = Field(min_length=1, max_length=24)


class ExternalPositionIn(BaseModel):
    contract_code: str = Field(min_length=1, max_length=24)
    action: Literal['Buy', 'Sell']
    quantity: int = Field(ge=1, le=100, strict=True)
    simulation: StrictBool
    confirmed: StrictBool = False


class CurveEntryIn(BaseModel):
    action: Literal["Buy", "Sell"] = "Buy"
    curve_id: int = Field(gt=0, strict=True)
    contract_code: str = Field(min_length=1, max_length=24)
    quantity: int = Field(default=1, ge=1, le=100, strict=True)
    simulation: StrictBool
    confirmed: StrictBool = False


