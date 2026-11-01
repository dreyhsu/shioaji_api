"""Dashboard display settings. Public settings contain no filesystem paths."""
import json
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class DashboardConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    host: Literal['127.0.0.1', '::1'] = '127.0.0.1'
    port: int = Field(default=8765, ge=1, le=65535, strict=True)
    default_timeframe: Literal['1m', '5m', '15m', '1h'] = '1h'
    history_days: int = Field(default=30, ge=1, le=365, strict=True)
    initial_bars: int = Field(default=120, ge=1, le=10000, strict=True)
    refresh_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    cache_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    curve_hover_pixels: float = Field(default=8, gt=0, le=30, allow_inf_nan=False)

    @classmethod
    def load(cls, path):
        return cls.model_validate(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def public(self):
        return self.model_dump(exclude={'host', 'port', 'cache_seconds'})
