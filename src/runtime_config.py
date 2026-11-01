"""Shared, explicit runtime selection. Loading settings never logs into a broker."""
from pathlib import Path
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / 'config' / 'runtime.json'


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    mode_comment: str = ''  # Human-readable guidance in the JSON configuration.
    mode: Literal['mock', 'simulation', 'production'] = 'mock'
    signal_code: str = Field(default='TMFR1', pattern=r'^[A-Za-z0-9]{1,24}$')
    contracts: list[str] = Field(default_factory=lambda: ['TMFR1'], min_length=1)
    acquisition_db: Path = Path('../data/market_fetchers.db')
    unified_db: Path = Path('../data/tmf.db')
    market_config: Path = Path('market_services.json')
    health_config: Path = Path('market_health.json')
    trading_config: Path = Path('trading.json')
    dashboard_config: Path = Path('dashboard.json')
    mock_config: Path = Path('mock.json')
    trade_host: Literal['127.0.0.1', '::1'] = '127.0.0.1'
    trade_port: int = Field(default=8766, ge=1, le=65535, strict=True)

    @field_validator('contracts')
    @classmethod
    def codes(cls, values):
        if len(set(values)) != len(values) or any(not code.isascii() or not code.isalnum() or len(code) > 24 for code in values):
            raise ValueError('contracts 須為不重複的合約代碼')
        return values

    @model_validator(mode='after')
    def live_contracts(self):
        if self.mode != 'mock' and (self.signal_code not in self.contracts or not any(
                code != self.signal_code and not code.endswith(('R1', 'R2')) for code in self.contracts)):
            raise ValueError('非 Mock 模式須在 contracts 明確設定訊號商品與實際月份合約')
        return self

    @property
    def trade_url(self):
        host = f'[{self.trade_host}]' if ':' in self.trade_host else self.trade_host
        return f'http://{host}:{self.trade_port}'

    @classmethod
    def load(cls, path=DEFAULT_RUNTIME):
        path = Path(path).resolve()
        values = json.loads(path.read_text(encoding='utf-8-sig'))
        result = cls.model_validate(values)
        return result.model_copy(update={name: (path.parent / getattr(result, name)).resolve()
            for name in ('acquisition_db', 'unified_db', 'market_config', 'health_config',
                         'trading_config', 'dashboard_config', 'mock_config')})


def skip_mock(mode):
    if mode == 'mock':
        print('Mock 模式由 Dashboard 的離線回放提供；請執行 start_all.command 或 start_all.sh。', flush=True)
        return True
    return False
