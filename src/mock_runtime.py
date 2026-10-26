"""Offline market-data/clock driver for the shared trading service.

This module alone owns replay orchestration. MockBroker owns matching;
mock_market_data owns the scenario; TradingService owns all trading decisions. A fresh,
dedicated SQLite path is mandatory. No production credentials are consulted.
"""
from __future__ import annotations

import threading
import pandas as pd

import db
from mock_broker import MockBroker
from mock_market_data import v_shape_bars
from mock_config import MockConfig, load_mock_config
from trading import TradingService
from pathlib import Path


class MockRuntime:
    def __init__(self, db_path, code="TMFR1", *, config: MockConfig | None = None, settings_path=None):
        self.db_path = Path(db_path)
        if self.db_path.exists():
            raise ValueError("Mock 模式必須使用新的獨立資料庫路徑")
        self.code = code
        self.config = config if config is not None else load_mock_config()
        self.settings_path = settings_path
        self.lock = threading.RLock()
        self.bars = v_shape_bars(self.config.market)
        self.playing = False
        self._stop = threading.Event()
        self._thread = None
        self._initialize()

    def _initialize(self):
        self.cursor = self.config.warmup_minutes
        self.playing = False
        self.broker = MockBroker(clock=self.now, root=self.code[:3], settings=self.config.broker)
        self.service = TradingService(
            self.db_path, self.code, api_factory=lambda simulation: self.broker,
            simulation=True, clock=self.now, start_worker=False, sdk=self.broker.sdk,
            **({"settings_path": self.settings_path} if self.settings_path is not None else {}),
        )
        with db.session(self.db_path) as conn:
            db.upsert_minute_bars(conn, self.code, self.bars.iloc[:self.cursor])
        self.service.ensure_connected()
        self.service.subscribe_contract(self.broker.contract.symbol)
        if self.cursor:
            self.broker.publish(float(self.bars.iloc[self.cursor - 1]["close"]))

    def now(self):
        if self.cursor == 0:
            return pd.Timestamp(self.bars.iloc[0]["minute_start"])
        return pd.Timestamp(self.bars.iloc[self.cursor - 1]["minute_start"]) + pd.Timedelta(minutes=1)

    def snapshot(self):
        with self.lock:
            market = self.config.market
            phase = ("長時間下跌" if self.cursor <= market.decline_minutes else
                     "V 型反彈" if self.cursor <= market.decline_minutes + market.rebound_minutes else "反彈後回檔")
            return {"enabled": True, "mode": "離線 Mock API", "playing": self.playing,
                    "cursor": self.cursor, "total": len(self.bars), "remaining": len(self.bars) - self.cursor,
                    "time": self.now().isoformat(sep=" "), "phase": phase,
                    "price": self.broker.price, "entry": None if self.entry is None else dict(self.entry),
                    "replay": self.config.replay.model_dump()}

    @property
    def entry(self):
        return self.service.curve_entry.snapshot()

    def sync(self):
        """Reconcile fills deterministically without a background trading worker."""
        with self.lock:
            self.service.process_market_update()

    def advance(self, steps=1):
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 600:
            raise ValueError("推進分鐘數必須介於 1 到 600")
        with self.lock:
            for _ in range(min(steps, len(self.bars) - self.cursor)):
                row = self.bars.iloc[self.cursor:self.cursor + 1]
                self.cursor += 1
                with db.session(self.db_path) as conn:
                    db.upsert_minute_bars(conn, self.code, row)
                self.broker.publish(float(row.iloc[0]["close"]))
                self.sync()
            if self.cursor == len(self.bars):
                self.playing = False
            return self.snapshot()

    def arm_entry(self, curve_id, contract_code, quantity):
        with self.lock:
            if self.cursor == len(self.bars):
                raise ValueError("行情已播完，請重設情境")
            return self.service.curve_entry.arm(curve_id, contract_code, quantity, simulation=True, confirmed=True)

    def cancel_entry(self):
        with self.lock:
            self.service.curve_entry.cancel()
            return self.snapshot()

    def reset(self):
        with self.lock:
            self.service.close()
            # This path was created by this runtime and cannot be a user database.
            with db.session(self.db_path) as conn:
                for table in ("curve_entries", "managed_orders", "trendlines", "minute_bars"):
                    conn.execute(f"DELETE FROM {table}")
                conn.commit()
            self._initialize()
            return self.snapshot()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mock-replay", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(self.config.replay.interval_seconds):
            with self.lock:
                if self.playing:
                    try:
                        self.advance()
                    except Exception as error:
                        self.playing = False
                        self.service._connect_error = f"Mock 回放停止：{error}"

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.service.close()
