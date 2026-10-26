"""Validated offline mock settings, loaded once at runtime startup."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "mock.json"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)


class MarketConfig(Settings):
    start_time: str = "2026-10-05 08:45"
    decline_minutes: int = Field(default=3600, ge=1)
    rebound_minutes: int = Field(default=480, ge=1)
    pullback_minutes: int = Field(default=120, ge=1)
    start_price: float = Field(default=27000, gt=0)
    decline_points_per_minute: float = Field(default=0.8, gt=0)
    rebound_points_per_minute: float = Field(default=2, gt=0)
    pullback_points_per_minute: float = Field(default=2, gt=0)
    wave_amplitude: float = Field(default=0.6, ge=0)
    wave_scale_minutes: float = Field(default=7, gt=0)
    wick_points: float = Field(default=2, ge=0)
    volume_base: int = Field(default=10, ge=1)
    volume_cycle_minutes: int = Field(default=17, ge=1)

    @field_validator("start_time")
    @classmethod
    def valid_start_time(cls, value):
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is not None or stamp != stamp.floor("min"):
            raise ValueError("start_time 必須是無時區、精確到分鐘的日期時間")
        return value

    @model_validator(mode="after")
    def positive_prices(self):
        bottom = self.start_price - self.decline_minutes * self.decline_points_per_minute
        peak = bottom + self.rebound_minutes * self.rebound_points_per_minute
        end = peak - self.pullback_minutes * self.pullback_points_per_minute
        if min(bottom, end) - self.wave_amplitude - self.wick_points <= 0.5:
            raise ValueError("行情價格含波動、影線與取整後必須大於零")
        return self


class ReplayConfig(Settings):
    interval_seconds: float = Field(default=1, gt=0)
    browser_poll_seconds: float = Field(default=1.5, gt=0)
    start_bar: int | None = Field(default=None, ge=0)
    start_at: Literal["before_bottom", "before_pullback"] = "before_bottom"
    minutes_before_bottom: int = Field(default=60, ge=0)
    minutes_before_pullback: int = Field(default=60, ge=0)


class BrokerConfig(Settings):
    equity: float = Field(default=1000000, gt=0)
    initial_margin_per_contract: float = Field(default=22000, ge=0)
    maintenance_margin_per_contract: float = Field(default=18000, ge=0)


class MockConfig(Settings):
    market: MarketConfig = Field(default_factory=MarketConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)

    @model_validator(mode="after")
    def valid_warmup(self):
        if self.replay.start_bar is not None:
            total = self.market.decline_minutes + self.market.rebound_minutes + self.market.pullback_minutes
            if self.replay.start_bar > total:
                raise ValueError("start_bar 不得大於情境的總 K 棒數")
        elif self.replay.start_at == "before_bottom":
            if self.replay.minutes_before_bottom >= self.market.decline_minutes:
                raise ValueError("minutes_before_bottom 必須小於 decline_minutes，保留至少一根歷史 K 棒")
        elif self.replay.minutes_before_pullback > self.market.rebound_minutes:
            raise ValueError("minutes_before_pullback 不得大於 rebound_minutes，起點須位於反彈階段")
        return self

    @property
    def warmup_minutes(self):
        if self.replay.start_bar is not None:
            return self.replay.start_bar
        if self.replay.start_at == "before_pullback":
            return self.market.decline_minutes + self.market.rebound_minutes - self.replay.minutes_before_pullback
        return self.market.decline_minutes - self.replay.minutes_before_bottom


def _ignore_comments(fields: dict) -> dict:
    """Keep JSON valid while allowing explanatory fields at every level."""
    return {key: value for key, value in fields.items()
            if key != "_comment" and not key.startswith("_comment_")}


def load_mock_config(path: Path | str | None = None) -> MockConfig:
    path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_hook=_ignore_comments)
        return MockConfig.model_validate(data)
    except (OSError, ValueError) as error:
        raise ValueError(f"Mock 設定錯誤 {path}: {error}") from error
