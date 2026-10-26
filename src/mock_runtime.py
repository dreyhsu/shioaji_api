"""Offline composition root: replay -> storage -> curve signal -> real trading service.

This module alone owns replay orchestration. MockBroker owns matching;
mock_market_data owns the scenario; curve_entry owns signal math. A fresh,
dedicated SQLite path is mandatory. No production credentials are consulted.
"""
from __future__ import annotations

import threading
import pandas as pd

import db
import session_bars as sb
import trendline as tl
from curve_entry import crossed_above_curve
from mock_broker import MockBroker
from mock_market_data import v_shape_bars
from mock_config import MockConfig, load_mock_config
from place_order import OrderRequest, validate_request
from trading import TradingService
from pathlib import Path


class MockRuntime:
    def __init__(self, db_path, code="TMFR1", *, config: MockConfig | None = None):
        self.db_path = Path(db_path)
        if self.db_path.exists():
            raise ValueError("Mock 模式必須使用新的獨立資料庫路徑")
        self.code = code
        self.config = config if config is not None else load_mock_config()
        self.lock = threading.RLock()
        self.bars = v_shape_bars(self.config.market)
        self.playing = False
        self._stop = threading.Event()
        self._thread = None
        self._sequence = 0
        self.entry = None
        self._initialize()

    def _initialize(self):
        self.cursor = self.config.warmup_minutes
        self.playing = False
        self.entry = None
        self.broker = MockBroker(clock=self.now, root=self.code[:3], settings=self.config.broker)
        self.service = TradingService(
            self.db_path, self.code, api_factory=lambda simulation: self.broker,
            simulation=True, clock=self.now, start_worker=False, sdk=self.broker.sdk,
        )
        # Mock exits always run, independent of the user's production settings.
        self.service.auto_exit_enabled = lambda: True
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

    def sync(self):
        """Reconcile fills deterministically without a background trading worker."""
        with self.lock:
            self.service._last_sync = 0
            self.service._curve_break_cache.clear()
            self.service._curve_cache.clear()
            self.service._monitor_once()
            # A close submitted during the first pass is filled synchronously.
            self.service._last_sync = 0
            self.service._monitor_once()

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
                self._check_entry()
                self.sync()
            if self.cursor == len(self.bars):
                self.playing = False
            return self.snapshot()

    def arm_entry(self, curve_id, contract_code, quantity):
        with self.lock:
            if self.entry and self.entry["state"] in {"armed", "submitting"}:
                raise ValueError("已有啟用中的曲線進場，請先取消")
            if self.cursor == len(self.bars):
                raise ValueError("行情已播完，請重設情境")
            validate_request(OrderRequest(contract_code, "Buy", quantity, price_type="MKT", order_type="IOC",
                                          confirmed=True), True)
            if contract_code not in {self.broker.contract.code, self.broker.contract.symbol}:
                raise ValueError("無效的 Mock 合約")
            curve = self._curve(curve_id)
            hourly = sb.minute_bars_to_hourly(self.bars.iloc[:self.cursor])
            if curve is None or not tl.anchor_indices(hourly, curve["anchors"], "curve"):
                raise ValueError("請選擇已保存、有效的 1 小時曲線")
            self._sequence += 1
            self.entry = {"id": self._sequence, "curve_id": curve_id, "contract_code": contract_code,
                          "quantity": quantity, "state": "armed", "armed_at": str(self.now()),
                          "order_id": None, "error": None}
            return dict(self.entry)

    def _curve(self, curve_id):
        with db.session(self.db_path) as conn:
            return next((curve for curve in db.load_trendlines(conn, self.code)
                         if curve["id"] == curve_id and curve["kind"] == "curve"), None)

    def _check_entry(self):
        if self.entry is None or self.entry["state"] != "armed":
            return
        curve = self._curve(self.entry["curve_id"])
        if curve is None:
            self.entry.update(state="failed", error="曲線已刪除，進場已停用")
            return
        if not crossed_above_curve(self.bars.iloc[:self.cursor], curve["anchors"], self.now()):
            return
        # Claim before sending. Errors must not re-arm a possibly received order.
        self.entry["state"] = "submitting"
        try:
            result = self.service.place_order({
                "contract_code": self.entry["contract_code"], "action": "Buy", "quantity": self.entry["quantity"],
                "price_type": "MKT", "order_type": "IOC", "price": 0, "confirmed": True, "simulation": True,
            })
            self.entry.update(state="triggered", order_id=result["id"], triggered_at=str(self.now()))
        except Exception as error:
            self.entry.update(state="failed", error=str(error))

    def cancel_entry(self):
        with self.lock:
            if self.entry and self.entry["state"] == "armed":
                self.entry["state"] = "cancelled"
            return self.snapshot()

    def reset(self):
        with self.lock:
            self.service.close()
            # This path was created by this runtime and cannot be a user database.
            with db.session(self.db_path) as conn:
                for table in ("managed_orders", "trendlines", "minute_bars"):
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
