"""Shioaji order entry and persistent tick/1m-close exit monitoring."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from functools import wraps

import pandas as pd
import shioaji as sj
from dotenv import load_dotenv

import db
import market_clock
import session_bars as sb
import snr_core as snr
import shioaji_client as sc
import trendline as tl
from place_order import OrderRequest, OrderContext, validate_request, place_order
from shioaji_order_adapter import ShioajiOrderAdapter
from curve_entry_controller import CurveEntryController
from entry_notify import EntryNotifier
from alert_rules import DEFAULT_CONFIG_PATH as DEFAULT_ALERTS_PATH

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "trading.json"
TICK_STALE_SECONDS = 10
MONITOR_INTERVAL_SECONDS = 0.5


class RiskConflict(ValueError):
    """The editor's snapshot is no longer current."""


class RiskConfirmation(ValueError):
    """Saving these settings can immediately trigger an exit."""


def risk_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


def align_tick(price: float, tick: float, direction: int = 0) -> float:
    units = price / tick
    units = math.ceil(units - 1e-9) if direction > 0 else math.floor(units + 1e-9) if direction < 0 else round(units)
    return round(units * tick, 10)


def stop_target_prices(action: str, entry_price: float, atr: float) -> tuple[float, float]:
    """Return 2-ATR stop and target for the side of an entry."""
    if action not in {"Buy", "Sell"}:
        raise ValueError("action must be Buy or Sell")
    if not math.isfinite(entry_price) or not math.isfinite(atr) or atr <= 0:
        raise ValueError("entry price and ATR must be finite; ATR must be positive")
    direction = 1 if action == "Buy" else -1
    return entry_price - direction * 2 * atr, entry_price + direction * 2 * atr


def crossed_exit(action: str, price: float, stop: float, target: float | None) -> str | None:
    """Return the first applicable fixed exit reason for the latest quote."""
    if action == "Buy":
        if price <= stop:
            return "stop"
        if target is not None and price >= target:
            return "target"
    else:
        if price >= stop:
            return "stop"
        if target is not None and price <= target:
            return "target"
    return None


def crossed_curve_close(
    action: str,
    previous_close: float,
    previous_curve: float,
    current_close: float,
    current_curve: float,
) -> bool:
    """True only when two consecutive 1m closes cross the 1h curve."""
    if action == "Buy":
        return previous_close > previous_curve and current_close <= current_curve
    if action == "Sell":
        return previous_close < previous_curve and current_close >= current_curve
    raise ValueError("action must be Buy or Sell")


def _value(obj: Any, name: str, default=None):
    try:
        result = getattr(obj, name)
    except Exception:  # noqa: BLE001 - Shioaji fields can be lazy wrappers
        return default
    if result is None:
        return default
    return result


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _trade_status(trade: Any) -> str:
    return _enum_text(_value(_value(trade, "status"), "status", "Unknown"))


def _trade_id(trade: Any) -> str | None:
    return _value(_value(trade, "order"), "id") or _value(_value(trade, "status"), "id")


def _fill_summary(trade: Any) -> tuple[int, float | None]:
    status = _value(trade, "status")
    filled = int(_value(status, "deal_quantity", 0) or 0)
    deals = _value(status, "deals", []) or []
    if isinstance(deals, dict):
        deals = list(deals.values())
    total_quantity = 0
    total_value = 0.0
    for deal in deals:
        quantity = int(_value(deal, "quantity", 0) or 0)
        price = float(_value(deal, "price", 0) or 0)
        total_quantity += quantity
        total_value += quantity * price
    if total_quantity:
        return max(filled, total_quantity), total_value / total_quantity
    average = _value(status, "average_price") or _value(status, "deal_price")
    order = _value(trade, "order")
    if average is None and filled:
        average = _value(status, "modified_price") or _value(order, "price")
    return filled, None if average is None else float(average)


class TradingService:
    """Own one Shioaji session and reconcile managed order state across restarts.

    ``api_factory`` is injectable so order logic can be exercised without network
    access.  Production callers use the installed Shioaji SDK from the ``tss``
    environment and the project's existing .env settings.
    """

    def __init__(
        self,
        db_path: Path | str,
        code: str,
        *,
        settings_path: Path | str = DEFAULT_SETTINGS_PATH,
        api_factory=None,
        simulation: bool | None = None,
        clock=None,
        start_worker: bool = True,
        sdk=None,
        enable_telegram: bool = True,
        alerts_path: Path | str = DEFAULT_ALERTS_PATH,
    ) -> None:
        self.db_path = Path(db_path)
        self.code = code
        self.settings_path = Path(settings_path)
        self.api_factory = api_factory
        self._simulation = simulation
        self._now = clock or (lambda: market_clock.now())
        self._worker_enabled = start_worker
        self._sdk = sdk or sj
        self._lock = threading.RLock()
        self._api = None
        self._api_context: AbstractContextManager | None = None
        self._connect_error: str | None = None
        self._subscribed: set[str] = set()
        self._quotes: dict[str, dict] = {}
        self._quote_aliases: dict[str, str] = {}
        self._curve_cache: dict[tuple[int, str], tuple[float, float | None]] = {}
        self._curve_break_cache: dict[tuple[str, int, str, str], tuple[float, str | None]] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_sync = 0.0
        self._connected_simulation = None
        self.curve_entry = CurveEntryController(self)
        self.entry_notifier = EntryNotifier(db_path, enabled=enable_telegram, alerts_path=alerts_path)

    @property
    def simulation(self) -> bool:
        if self._simulation is not None:
            return self._simulation
        load_dotenv(PROJECT_ROOT / ".env")
        return os.getenv("SHIOAJI_SIMULATION", "1") == "1"

    def auto_exit_enabled(self) -> bool:
        return self._setting_enabled("auto_exit_enabled")

    def auto_entry_enabled(self) -> bool:
        return self._setting_enabled("auto_entry_enabled")

    def _setting_enabled(self, key) -> bool:
        try:
            config = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return config.get(key, True) is True
        except (OSError, ValueError, AttributeError):
            return True

    def breakeven_settings(self) -> dict[str, float]:
        """Reload point settings like the existing trading switches; retain safe defaults."""
        defaults = {"breakeven_trigger_points": 100.0, "breakeven_offset_points": 5.0}
        try:
            config = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(config, dict):
            return defaults
        for key in defaults:
            value = config.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                value = float(value)
            except OverflowError:
                continue
            if math.isfinite(value) and value >= 0:
                defaults[key] = value
        return defaults

    def _open(self):
        if self._api is not None:
            return self._api
        mode = self.simulation
        if self.api_factory is not None:
            self._api = self.api_factory(self.simulation)
        else:
            self._api_context = sc.connect(simulation=self.simulation)
            self._api = self._api_context.__enter__()
            if not self.simulation:
                load_dotenv(PROJECT_ROOT / ".env")
                required = ("SHIOAJI_CA_PATH", "SHIOAJI_CA_PASSWD", "SHIOAJI_PERSON_ID")
                missing = [name for name in required if not os.getenv(name)]
                if missing:
                    self._close_api()
                    raise RuntimeError("正式環境缺少憑證設定：" + ", ".join(missing))
                self._api.activate_ca(
                    ca_path=os.environ["SHIOAJI_CA_PATH"],
                    ca_passwd=os.environ["SHIOAJI_CA_PASSWD"],
                    person_id=os.environ["SHIOAJI_PERSON_ID"],
                )
        self._api.set_on_tick_fop_v1_callback(self._on_tick)
        self._connected_simulation = mode
        self._connect_error = None
        self._start_worker()
        self._recover_subscriptions()
        return self._api

    def _close_api(self) -> None:
        if self._api_context is not None:
            try:
                self._api_context.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        elif self._api is not None:
            try:
                self._api.logout()
            except Exception:  # noqa: BLE001
                pass
        self._api = None
        self._api_context = None
        self._connected_simulation = None

    def _validate_connected_mode(self):
        if self._api is not None and self._connected_simulation != self.simulation:
            raise ValueError("交易環境已改變，請重啟服務並重新確認")

    def _active_orders(self) -> list[dict]:
        with db.session(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM managed_orders WHERE active = 1 AND code = ? AND simulation = ? ORDER BY created_at DESC",
                (self.code, int(self.simulation)),
            ).fetchall()
        return [dict(row) for row in rows]

    def _recover_subscriptions(self) -> None:
        for row in self._active_orders():
            try:
                contract = self._find_contract(row["contract_code"])
                tick = float(_value(contract, "tick", 1) or 1)
                if tick != float(row.get("contract_tick") or 1):
                    with db.session(self.db_path) as conn:
                        conn.execute("UPDATE managed_orders SET contract_tick = ?, risk_revision = risk_revision + 1 WHERE id = ?",
                                     (tick, row["id"]))
                        conn.commit()
                self._subscribe(contract)
            except Exception as error:  # noqa: BLE001
                self._connect_error = f"合約行情恢復失敗：{error}"
        entry = self.curve_entry.snapshot()
        if entry and entry["state"] == "armed":
            try:
                self._subscribe(self._find_contract(entry["contract_code"]))
            except Exception as error:
                self._connect_error = f"進場行情恢復失敗：{error}"

    def _start_worker(self) -> None:
        if not self._worker_enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._monitor_loop, name="shioaji-trading", daemon=True)
        self._thread.start()

    def _subscribe(self, contract) -> None:
        code = str(contract.code)
        symbol = str(_value(contract, "symbol", code))
        for alias in (code, symbol, _value(contract, "target_code"), _value(contract, "full_code")):
            if not alias:
                continue
            alias = str(alias)
            previous = self._quote_aliases.get(alias)
            self._quote_aliases[alias] = code if previous in {None, code} else ""
        if code in self._subscribed:
            return
        self._api.quote.subscribe(
            contract, quote_type=self._sdk.QuoteType.Tick, version=self._sdk.QuoteVersion.v1
        )
        self._subscribed.add(code)

    def _on_tick(self, quote) -> None:
        try:
            alias = str(_value(quote, "code", ""))
            code = self._quote_aliases.get(alias, alias)
            price = float(_value(quote, "close"))
            stamp = pd.Timestamp(_value(quote, "datetime", self._now()))
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert(None)
            if not math.isfinite(price) or price <= 0:
                return
            with self._lock:
                previous = self._quotes.get(code)
                if previous and stamp < previous["at"]:
                    return
                self._quotes[code] = {"price": price, "at": stamp}
                if -5 <= (self._now() - stamp).total_seconds() <= TICK_STALE_SECONDS:
                    for row in self._active_orders():
                        if row["contract_code"] == code:
                            self._apply_breakeven(row, price)
        except Exception:  # noqa: BLE001 - callback runs on Shioaji's quote thread
            return

    def _trades_by_id(self) -> dict[str, Any]:
        if self._api is None:
            return {}
        try:
            self._api.update_status(self._api.futopt_account)
            return {
                str(_trade_id(trade)): trade
                for trade in self._api.list_trades()
                if _trade_id(trade)
            }
        except Exception as error:  # noqa: BLE001
            self._connect_error = f"委託狀態同步失敗：{error}"
            return {}

    @risk_locked
    def _persist_trade_state(self, row_id: str, *, entry=None, exit_trade=None) -> None:
        original = self._active_order(row_id)
        now = self._now().isoformat(sep=" ")
        new_fill = False
        updates: dict[str, Any] = {"updated_at": now}
        if entry is not None:
            filled, avg = _fill_summary(entry)
            status = _trade_status(entry)
            current = self._active_order(row_id)
            new_fill = filled > int(current.get("filled_quantity", 0))
            updates.update(filled_quantity=filled, entry_status=status)
            terminal = status in {"Filled", "Cancelled", "Failed", "Rejected"}
            if terminal and current.get("exit_status") == "waiting_entry_cancel":
                # Once the remainder is confirmed terminal, the watchdog may
                # submit an exit for the final broker-confirmed filled quantity.
                updates["exit_status"] = None
            if (filled > 0 and current.get("curve_id") is not None
                    and int(current.get("filled_quantity", 0)) == 0):
                updates["curve_check_from"] = pd.Timestamp(self._now()).ceil("min").isoformat(sep=" ")
            if filled > 0 and avg is not None:
                updates["avg_fill_price"] = avg
                if not int(current.get("levels_customized", 0)):
                    stop, target = stop_target_prices(current["action"], avg, current["atr"])
                    tick = float(current.get("contract_tick") or 1)
                    updates.update(stop_price=align_tick(stop, tick), target_price=None if current.get("curve_id") else align_tick(target, tick))
                if current.get("breakeven_enabled") and current.get("breakeven_triggered_at"):
                    direction = 1 if current["action"] == "Buy" else -1
                    offset = self.breakeven_settings()["breakeven_offset_points"]
                    protection = align_tick(avg + direction * offset, float(current.get("contract_tick") or 1), direction)
                    old_floor = current.get("breakeven_price")
                    if old_floor is not None:
                        protection = max(protection, old_floor) if direction > 0 else min(protection, old_floor)
                    stop = updates.get("stop_price", current["stop_price"])
                    updates.update(breakeven_price=protection, stop_price=max(stop, protection) if direction > 0 else min(stop, protection), levels_customized=1)
            if terminal and filled == 0 and status in {"Cancelled", "Failed", "Rejected"}:
                updates.update(active=0, auto_exit=0)
        if exit_trade is not None:
            quantity, _ = _fill_summary(exit_trade)
            status = _trade_status(exit_trade)
            updates.update(exit_filled_quantity=quantity, exit_status=status)
            row = self._active_order(row_id)
            if quantity >= int(row["filled_quantity"]):
                updates["active"] = 0
            elif status in {"Cancelled", "Failed", "Rejected"}:
                # Do not retry an ambiguous exit automatically; it needs review.
                updates["error"] = f"平倉委託狀態：{status}，請確認剩餘部位"
        risk_keys = {"avg_fill_price", "filled_quantity", "exit_filled_quantity", "active", "stop_price", "target_price", "breakeven_price"}
        if any(key in risk_keys and value != original.get(key) for key, value in updates.items()):
            updates["risk_revision"] = int(original.get("risk_revision", 0)) + 1
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with db.session(self.db_path) as conn:
            conn.execute(
                f"UPDATE managed_orders SET {assignments} WHERE id = ?",
                (*updates.values(), row_id),
            )
            conn.commit()

        if new_fill:
            self.entry_notifier.notify(self._active_order(row_id))

    def _apply_breakeven(self, row: dict, price: float) -> dict:
        """Called under the shared risk lock for every fresh tick and after fill sync."""
        if (not row.get("active") or not row.get("breakeven_enabled") or row.get("triggered_at")
                or row.get("exit_trade_id") or row.get("exit_status")
                or int(row["filled_quantity"]) <= int(row["exit_filled_quantity"])):
            return row
        if row.get("breakeven_triggered_at"):
            return row
        entry = row.get("avg_fill_price")
        direction = 1 if row["action"] == "Buy" else -1
        settings = self.breakeven_settings()
        if entry is None or direction * (price - entry) <= settings["breakeven_trigger_points"]:
            return row
        protection = align_tick(entry + direction * settings["breakeven_offset_points"], float(row.get("contract_tick") or 1), direction)
        stop = row.get("stop_price")
        stop = protection if stop is None else max(stop, protection) if direction > 0 else min(stop, protection)
        now = self._now().isoformat(sep=" ")
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE managed_orders SET stop_price = ?, breakeven_price = ?, breakeven_triggered_at = ?, "
                         "levels_customized = 1, risk_revision = risk_revision + 1, updated_at = ? WHERE id = ?",
                         (stop, protection, now, now, row["id"]))
            conn.commit()
        return self._active_order(row["id"])

    def _active_order(self, row_id: str) -> dict:
        with db.session(self.db_path) as conn:
            row = conn.execute("SELECT * FROM managed_orders WHERE id = ? AND code = ? AND simulation = ?",
                               (row_id, self.code, int(self.simulation))).fetchone()
        return {} if row is None else dict(row)

    def _exit_signal(self, row: dict, price: float) -> str | None:
        stop_signal = crossed_exit(row["action"], price, float(row["stop_price"]), None)
        if stop_signal:
            return stop_signal
        target = row["target_price"]
        if row.get("curve_id") is not None:
            try:
                return self._curve_close_break(row)
            except Exception:  # noqa: BLE001
                return "curve_unavailable"
        return crossed_exit(row["action"], price, float(row["stop_price"]), target)

    def _curve_close_break(self, row: dict) -> str | None:
        """Evaluate completed 1m closes against the saved 1h curve."""
        cache_key = (
            str(row["id"]), int(row["curve_id"]), row["code"],
            str(row.get("curve_check_from") or ""),
        )
        cached = self._curve_break_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 0.75:
            return cached[1]
        with db.session(self.db_path) as conn:
            curve = conn.execute(
                "SELECT anchors FROM trendlines WHERE id = ? AND code = ? AND kind = 'curve' AND enabled = 1",
                (int(row["curve_id"]), row["code"]),
            ).fetchone()
            minutes = db.load_minute_bars(conn, row["code"])
        if curve is None or len(minutes) < 2:
            self._curve_break_cache[cache_key] = (time.monotonic(), "curve_unavailable")
            return "curve_unavailable"

        now = pd.Timestamp(self._now())
        minutes = minutes[minutes["minute_start"] + pd.Timedelta(minutes=1) <= now]
        if len(minutes) < 2:
            self._curve_break_cache[cache_key] = (time.monotonic(), "curve_unavailable")
            return "curve_unavailable"
        latest_start = pd.Timestamp(minutes.iloc[-1]["minute_start"])
        if (now - (latest_start + pd.Timedelta(minutes=1))).total_seconds() > 180:
            self._curve_break_cache[cache_key] = (time.monotonic(), "curve_unavailable")
            return "curve_unavailable"
        check_from = row.get("curve_check_from")
        if not check_from:
            baseline = now.ceil("min").isoformat(sep=" ")
            with db.session(self.db_path) as conn:
                conn.execute(
                    "UPDATE managed_orders SET curve_check_from = ? WHERE id = ? AND curve_check_from IS NULL",
                    (baseline, row["id"]),
                )
                conn.commit()
            self._curve_break_cache[cache_key] = (time.monotonic(), None)
            return None
        check_from = pd.Timestamp(check_from)
        if latest_start < check_from:
            self._curve_break_cache[cache_key] = (time.monotonic(), None)
            return None

        hourly = sb.minute_bars_to_hourly(minutes)
        points = tl.anchor_indices(hourly, json.loads(curve["anchors"]), "curve")
        if not points:
            self._curve_break_cache[cache_key] = (time.monotonic(), "curve_unavailable")
            return "curve_unavailable"

        def value_at_minute(stamp: pd.Timestamp) -> float | None:
            bucket = sb.hour_start_of(stamp)
            if bucket is None:
                return None
            starts = pd.to_datetime(hourly["hour_start"])
            indices = hourly.index[starts == bucket]
            if len(indices) == 0:
                return None
            index = int(indices[0])
            fraction = (stamp - bucket).total_seconds() / 3600.0
            return tl.value_at(points, index + fraction, "curve")

        eligible = [
            index for index, stamp in enumerate(minutes["minute_start"])
            if pd.Timestamp(stamp) >= check_from
        ]
        result = None
        for index in eligible:
            if index == 0:
                continue
            previous, current = minutes.iloc[index - 1], minutes.iloc[index]
            previous_curve = value_at_minute(pd.Timestamp(previous["minute_start"]))
            current_curve = value_at_minute(pd.Timestamp(current["minute_start"]))
            if previous_curve is None or current_curve is None:
                result = "curve_unavailable"
                continue
            if crossed_curve_close(
                row["action"], float(previous["close"]), previous_curve,
                float(current["close"]), current_curve,
            ):
                result = "curve"
                break
        self._curve_break_cache[cache_key] = (time.monotonic(), result)
        return result

    def _curve_value(self, row: dict) -> float | None:
        cache_key = (int(row["curve_id"]), row["code"])
        cached = self._curve_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 2:
            return cached[1]
        with db.session(self.db_path) as conn:
            curve = conn.execute(
                "SELECT anchors FROM trendlines WHERE id = ? AND code = ? AND kind = 'curve' AND enabled = 1",
                cache_key,
            ).fetchone()
            minutes = db.load_minute_bars(conn, row["code"])
        if curve is None or minutes.empty:
            self._curve_cache[cache_key] = (time.monotonic(), None)
            return None
        hourly = sb.minute_bars_to_hourly(minutes)
        points = tl.anchor_indices(hourly, json.loads(curve["anchors"]), "curve")
        # Match the fractional hour used by the 1m crossing evaluator.
        stamp = pd.Timestamp(minutes.iloc[-1]["minute_start"])
        bucket = sb.hour_start_of(stamp)
        fraction = 0 if bucket is None else (stamp - bucket).total_seconds() / 3600.0
        value = None if not points else tl.value_at(points, len(hourly) - 1 + fraction, "curve")
        result = None if value is None else float(value)
        self._curve_cache[cache_key] = (time.monotonic(), result)
        return result

    @risk_locked
    def _try_auto_exit(self, row: dict, reason: str) -> None:
        with db.session(self.db_path) as conn:
            conn.execute(
                "UPDATE managed_orders SET triggered_at = COALESCE(triggered_at, ?), "
                "trigger_reason = COALESCE(trigger_reason, ?), "
                "risk_revision = risk_revision + CASE WHEN triggered_at IS NULL THEN 1 ELSE 0 END, updated_at = ? WHERE id = ?",
                (self._now().isoformat(sep=" "), reason, self._now().isoformat(sep=" "), row["id"]),
            )
            conn.commit()
        if (not self.auto_exit_enabled() or row.get("exit_trade_id")
                or row.get("exit_status") in {"unknown", "waiting_entry_cancel", "cancelling_entry"}):
            return
        if row.get("entry_status") not in {"Filled", "Cancelled", "Failed", "Rejected"}:
            # A partially filled entry can still add contracts after the exit
            # trigger. Cancel its unfilled remainder first, then reconcile the
            # confirmed fill count before calculating the close quantity.
            trades = self._trades_by_id()
            entry_trade = trades.get(str(row["entry_trade_id"]))
            if entry_trade is None:
                with db.session(self.db_path) as conn:
                    conn.execute(
                        "UPDATE managed_orders SET exit_status = 'unknown', error = ?, updated_at = ? WHERE id = ?",
                        ("進場委託仍可能成交，但券商狀態無法查詢；已暫停自動平倉", self._now().isoformat(sep=" "), row["id"]),
                    )
                    conn.commit()
                return
            with db.session(self.db_path) as conn:
                cursor = conn.execute(
                    "UPDATE managed_orders SET exit_status = 'cancelling_entry', updated_at = ? "
                    "WHERE id = ? AND exit_trade_id IS NULL AND exit_status IS NULL",
                    (self._now().isoformat(sep=" "), row["id"]),
                )
                conn.commit()
            if not cursor.rowcount:
                return
            try:
                self._api.cancel_order(entry_trade)
                with db.session(self.db_path) as conn:
                    conn.execute(
                        "UPDATE managed_orders SET exit_status = 'waiting_entry_cancel', updated_at = ? WHERE id = ?",
                        (self._now().isoformat(sep=" "), row["id"]),
                    )
                    conn.commit()
            except Exception as error:  # noqa: BLE001 - uncertain cancellation must not race an exit
                with db.session(self.db_path) as conn:
                    conn.execute(
                        "UPDATE managed_orders SET exit_status = 'unknown', error = ?, updated_at = ? WHERE id = ?",
                        (f"取消未成交進場委託失敗，請先核對券商端：{error}", self._now().isoformat(sep=" "), row["id"]),
                    )
                    conn.commit()
            return
        quantity = max(0, int(row["filled_quantity"]) - int(row["exit_filled_quantity"]))
        if quantity <= 0:
            return
        # Claim the exit before talking to the broker so another monitor cycle cannot
        # submit a duplicate. An ambiguous network error remains locked for review.
        with db.session(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE managed_orders SET exit_status = 'submitting', triggered_at = COALESCE(triggered_at, ?), "
                "trigger_reason = COALESCE(trigger_reason, ?), updated_at = ? "
                "WHERE id = ? AND active = 1 AND exit_trade_id IS NULL AND exit_status IS NULL",
                (self._now().isoformat(sep=" "), reason, self._now().isoformat(sep=" "), row["id"]),
            )
            conn.commit()
        if not cursor.rowcount:
            return
        try:
            contract = self._find_contract(row["contract_code"])
            action = self._sdk.Action.Sell if row["action"] == "Buy" else self._sdk.Action.Buy
            order = self._sdk.Order(
                action=action,
                price=0,
                quantity=quantity,
                price_type=self._sdk.FuturesPriceType.MKT,
                order_type=self._sdk.OrderType.IOC,
                octype=self._sdk.FuturesOCType.Cover,
                account=self._api.futopt_account,
            )
            trade = self._api.place_order(contract, order)
            trade_id = _trade_id(trade)
            if not trade_id:
                raise RuntimeError("Shioaji 沒有回傳平倉委託編號")
            with db.session(self.db_path) as conn:
                conn.execute(
                    "UPDATE managed_orders SET exit_trade_id = ?, exit_status = ?, updated_at = ? WHERE id = ?",
                    (str(trade_id), _trade_status(trade), self._now().isoformat(sep=" "), row["id"]),
                )
                conn.commit()
        except Exception as error:  # noqa: BLE001 - don't blindly retry an uncertain order
            with db.session(self.db_path) as conn:
                conn.execute(
                    "UPDATE managed_orders SET exit_status = 'unknown', error = ?, updated_at = ? WHERE id = ?",
                    (str(error), self._now().isoformat(sep=" "), row["id"]),
                )
                conn.commit()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(MONITOR_INTERVAL_SECONDS):
            try:
                self.process_market_update()
            except Exception as error:  # noqa: BLE001 - keep the watchdog alive
                self._connect_error = f"交易監控錯誤：{error}"

    def process_market_update(self):
        """Run the same signal/fill/exit cycle for replay and wall-clock drivers."""
        with self._lock:
            self._last_sync = 0
            self._curve_break_cache.clear()
            self._curve_cache.clear()
            self._monitor_once()
            # Reconcile synchronous fills; asynchronous adapters settle on later cycles.
            self._last_sync = 0
            self._monitor_once()

    @risk_locked
    def _monitor_once(self) -> None:
        if not self._api:
            return
        with self._lock:
            self._validate_connected_mode()
            try:
                self.curve_entry.check()
            except Exception as error:
                self._connect_error = f"進場監控錯誤：{error}"
                self.curve_entry.fail_pending(error)
        active_rows = self._active_orders()
        if not active_rows:
            return
        now_mono = time.monotonic()
        if now_mono - self._last_sync >= 1.0:
            trades = self._trades_by_id()
            self._last_sync = now_mono
            for row in active_rows:
                entry = trades.get(str(row["entry_trade_id"]))
                exit_trade = trades.get(str(row["exit_trade_id"])) if row.get("exit_trade_id") else None
                if entry is not None or exit_trade is not None:
                    self._persist_trade_state(row["id"], entry=entry, exit_trade=exit_trade)
        for row in active_rows:
            # A fill/status sync above may have changed the managed row (including
            # confirming a remainder cancellation), so make exit decisions from
            # the refreshed persisted state rather than the stale snapshot.
            row = self._active_order(row["id"])
            if not row or not int(row["active"]):
                continue
            if int(row["filled_quantity"]) <= int(row["exit_filled_quantity"]):
                continue
            with self._lock:
                quote = self._quotes.get(row["contract_code"])
            if not quote:
                continue
            age = (self._now() - quote["at"]).total_seconds()
            if age < -5 or age > TICK_STALE_SECONDS:
                continue
            with db.session(self.db_path) as conn:
                conn.execute(
                    "UPDATE managed_orders SET last_quote = ?, last_quote_at = ?, updated_at = ? WHERE id = ?",
                    (quote["price"], quote["at"].isoformat(sep=" "), self._now().isoformat(sep=" "), row["id"]),
                )
                conn.commit()
            row = self._apply_breakeven(row, quote["price"])
            signal = self._exit_signal(row, quote["price"])
            if signal and signal != "curve_unavailable":
                self._try_auto_exit(row, signal)
            elif signal == "curve_unavailable":
                with db.session(self.db_path) as conn:
                    conn.execute(
                        "UPDATE managed_orders SET error = ?, updated_at = ? WHERE id = ?",
                        ("1 小時曲線或已收盤 1 分 K 資料暫不可用；曲線停利暫停，固定停損仍監控", self._now().isoformat(sep=" "), row["id"]),
                    )
                    conn.commit()

    def ensure_connected(self):
        with self._lock:
            if self._api is None:
                try:
                    return self._open()
                except Exception as error:  # noqa: BLE001
                    self._connect_error = f"{type(error).__name__}: {error}"
                    raise
            return self._api

    def status(self) -> dict:
        try:
            self.ensure_connected()
            connected = True
            error = self._connect_error
        except Exception:
            connected = False
            error = self._connect_error
        latest = None
        with self._lock:
            if self._quotes:
                latest = max(self._quotes.values(), key=lambda row: row["at"])
        age = None if latest is None else max(0.0, (self._now() - latest["at"]).total_seconds())
        return {
            "simulation": self.simulation,
            "mode": "模擬環境" if self.simulation else "正式環境",
            "connected": connected,
            "quote_connected": latest is not None and age is not None and age <= TICK_STALE_SECONDS,
            "last_quote": None if latest is None else latest["price"],
            "last_quote_at": None if latest is None else latest["at"].isoformat(sep=" "),
            "quote_age_seconds": age,
            "auto_exit_enabled": self.auto_exit_enabled(),
            "auto_entry_enabled": self.auto_entry_enabled(),
            **self.breakeven_settings(),
            "curve_entry": self.curve_entry.snapshot(),
            "error": error,
            "orders": self.orders(),
        }

    def contracts(self) -> list[dict]:
        api = self.ensure_connected()
        root_name = self.code[:3]
        root = getattr(api.Contracts.Futures, root_name, None)
        if root is None:
            raise RuntimeError(f"找不到期貨商品類別 {root_name}")
        contracts = []
        for contract in root:
            month = str(_value(contract, "delivery_month", ""))
            code = str(_value(contract, "symbol", _value(contract, "code", "")))
            delivery_date = str(_value(contract, "delivery_date", ""))
            if len(month) != 6 or not code or code.endswith(("R1", "R2")):
                continue
            if delivery_date:
                try:
                    if pd.Timestamp(delivery_date) < pd.Timestamp(self._now()).normalize():
                        continue
                except (TypeError, ValueError):
                    continue
            contracts.append({
                "code": code,
                "delivery_month": month,
                "name": str(_value(contract, "name", code)),
                "delivery_date": delivery_date,
                "tick": float(_value(contract, "tick", 1) or 1),
            })
        contracts = sorted(contracts, key=lambda item: item["delivery_month"])
        if contracts:
            try:
                self.subscribe_contract(contracts[0]["code"])
            except Exception:
                pass
        return contracts

    def _find_contract(self, contract_key: str):
        root = getattr(self._api.Contracts.Futures, self.code[:3], None)
        if root is None:
            raise RuntimeError(f"找不到期貨商品類別 {self.code[:3]}")
        for contract in root:
            if contract_key in {
                str(_value(contract, "code", "")),
                str(_value(contract, "symbol", "")),
                str(_value(contract, "target_code", "")),
                str(_value(contract, "full_code", "")),
            }:
                return contract
        raise ValueError(f"找不到合約 {contract_key}")

    def subscribe_contract(self, contract_key: str) -> dict:
        self.ensure_connected()
        contract = self._find_contract(contract_key)
        with self._lock:
            self._subscribe(contract)
        return {"subscribed": str(contract.code), "symbol": str(_value(contract, "symbol", contract.code))}

    def margin(self) -> dict:
        api = self.ensure_connected()
        result = api.margin(api.futopt_account)
        status = _value(result, "status", "Fetched")
        return {
            "status": _enum_text(status),
            "fetched_at": self._now().isoformat(sep=" "),
            "risk_indicator": _value(result, "risk_indicator"),
            "maintenance_margin": _value(result, "maintenance_margin"),
            "initial_margin": _value(result, "initial_margin"),
            "available_margin": _value(result, "available_margin"),
            "equity": _value(result, "equity"),
        }

    def _risk_context(self, code: str) -> tuple[float, pd.DataFrame]:
        with db.session(self.db_path) as conn:
            minutes = db.load_minute_bars(conn, code)
        if minutes.empty:
            raise RuntimeError("尚無可用行情，不能計算停損停利")
        minutes = minutes[minutes["minute_start"] + pd.Timedelta(minutes=1) <= pd.Timestamp(self._now())]
        if minutes.empty:
            raise RuntimeError("尚無已收盤行情，不能計算停損停利")
        hourly = sb.minute_bars_to_hourly(minutes)
        atr_series = snr.pine_atr(hourly, snr.DEFAULT_ATR_LENGTH)
        raw = atr_series.iloc[-1]
        if pd.isna(raw) or float(raw) <= 0:
            raise RuntimeError("1 小時 ATR 尚未就緒")
        return float(raw), hourly

    def place_order(self, request: dict) -> dict:
        order_request = OrderRequest(**{
            key: value for key, value in request.items() if key in OrderRequest.__dataclass_fields__
        })
        validate_request(order_request, self.simulation)
        action, quantity = order_request.action, order_request.quantity
        price_type, order_type = order_request.price_type, order_request.order_type
        price, contract_code = order_request.price, order_request.contract_code
        contracts = {item["code"]: item for item in self.contracts()}
        if contract_code not in contracts:
            raise ValueError("請選擇有效的微型臺指期貨月份合約")
        atr, _ = self._risk_context(self.code)
        curve_id = request.get("curve_id")
        if curve_id is not None:
            with db.session(self.db_path) as conn:
                curve = conn.execute(
                    "SELECT id FROM trendlines WHERE id = ? AND code = ? AND kind = 'curve' AND enabled = 1",
                    (int(curve_id), self.code),
                ).fetchone()
            if curve is None:
                raise ValueError("選用的停利曲線不存在或已刪除")
        api = self.ensure_connected()
        self._validate_connected_mode()
        contract = self._find_contract(contract_code)
        canonical_code = str(contract.code)
        tick = float(_value(contract, "tick", 1) or 1)
        with self._lock:
            result = place_order(
                order_request,
                context=OrderContext(canonical_code, tick, self.simulation),
                broker=ShioajiOrderAdapter(api, contract, sdk=self._sdk),
            )
            trade_id, price = result.trade_id, result.price
            self._subscribe(contract)
        preview_price = price if price_type == "LMT" else None
        stop, target = (stop_target_prices(action, preview_price, atr) if preview_price else (None, None))
        stop = None if stop is None else align_tick(stop, tick)
        target = None if target is None or curve_id is not None else align_tick(target, tick)
        row_id = str(uuid.uuid4())
        now = self._now().isoformat(sep=" ")
        with db.session(self.db_path) as conn:
            conn.execute(
                """INSERT INTO managed_orders (
                    id, code, contract_code, action, quantity, price, price_type, order_type,
                    atr, stop_price, target_price, curve_id, curve_check_from, auto_exit, simulation, contract_tick,
                    entry_trade_id, entry_status, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (row_id, self.code, canonical_code, action, quantity, price, price_type, order_type,
                 atr, stop, target, curve_id,
                 pd.Timestamp(self._now()).ceil("min").isoformat(sep=" ") if curve_id is not None else None,
                 int(self.auto_exit_enabled()), int(self.simulation), tick,
                 trade_id, result.status, now, now),
            )
            conn.commit()
        self._start_worker()
        return self._public_order(self._active_order(row_id))

    def _editable_risk_order(self, row_id: str) -> dict:
        row = self._active_order(row_id)
        if not row or not row["active"]:
            raise KeyError("找不到有效的委託部位")
        if int(row["filled_quantity"]) <= int(row["exit_filled_quantity"]):
            raise ValueError("需有已成交且未平倉的部位，才能變更出場設定")
        if row.get("triggered_at") or row.get("exit_trade_id") or row.get("exit_status"):
            raise RiskConflict("此部位已進入出場流程，不能變更出場設定")
        return row

    def _fresh_risk_quote(self, row: dict) -> float | None:
        quote = self._quotes.get(row["contract_code"])
        if quote and -5 <= (self._now() - quote["at"]).total_seconds() <= TICK_STALE_SECONDS:
            return float(quote["price"])
        return None

    @risk_locked
    def update_risk_settings(self, row_id: str, *, curve_id: int | None, stop_price: float,
                             target_price: float | None, breakeven_enabled: bool,
                             risk_revision: int, confirmed: bool = False) -> dict:
        row = self._editable_risk_order(row_id)
        quote = self._fresh_risk_quote(row)
        if quote is not None:
            row = self._apply_breakeven(row, quote)
        if risk_revision != int(row["risk_revision"]):
            raise RiskConflict("此單的出場設定或成交狀態已更新，請還原並重新核對後儲存")
        direction = 1 if row["action"] == "Buy" else -1
        tick = float(row.get("contract_tick") or 1)
        stop = float(stop_price)
        target = None if curve_id is not None or target_price is None else float(target_price)
        for value in (stop, target):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("停損停利價格必須是有效正數")
            if value is not None and not math.isclose(value, align_tick(value, tick), abs_tol=tick * 1e-7, rel_tol=0):
                raise ValueError(f"價格必須符合最小跳動單位 {tick:g}")
        stop = align_tick(stop, tick)
        target = None if target is None else align_tick(target, tick)
        if curve_id is None:
            if target is None:
                raise ValueError("固定停利模式必須提供 target")
            if direction * (target - stop) <= 0:
                raise ValueError("多單 stop 須低於 target；空單 stop 須高於 target")
        else:
            with db.session(self.db_path) as conn:
                curve = conn.execute("SELECT id FROM trendlines WHERE id = ? AND code = ? AND kind = 'curve' AND enabled = 1",
                                     (curve_id, row["code"])).fetchone()
            if curve is None:
                raise ValueError("選用的 1 小時曲線不存在或已刪除")
        # Disabling and loosening require separate saves. A stale editor can never
        # silently remove protection that was installed by a tick callback.
        if row.get("breakeven_enabled") and row.get("breakeven_triggered_at"):
            floor = row["stop_price"]
            if direction * (stop - floor) < -tick * 1e-7:
                raise ValueError("保本已觸發，請先關閉保本並儲存，再放寬 stop")
        triggered = row.get("breakeven_triggered_at") if breakeven_enabled else None
        protection = row.get("breakeven_price") if breakeven_enabled else None
        settings = self.breakeven_settings()
        if (breakeven_enabled and not triggered and quote is not None
                and direction * (quote - row["avg_fill_price"]) > settings["breakeven_trigger_points"]):
            protection = align_tick(row["avg_fill_price"] + direction * settings["breakeven_offset_points"], tick, direction)
            stop = max(stop, protection) if direction > 0 else min(stop, protection)
            triggered = self._now().isoformat(sep=" ")
        if curve_id is None and direction * (target - stop) <= 0:
            raise ValueError("套用保本後 stop 與 target 次序不符，請調整 target")
        if quote is not None:
            reason = crossed_exit(row["action"], quote, stop, target)
            if reason and not confirmed:
                raise RiskConfirmation(f"最新行情 {quote:g} 已達新設定的{'停損' if reason == 'stop' else '停利'}價，儲存後可能立即出場。確認儲存？")
        changed_curve = curve_id != row.get("curve_id")
        baseline = (pd.Timestamp(self._now()).ceil("min").isoformat(sep=" ")
                    if curve_id is not None and changed_curve else row.get("curve_check_from") if curve_id is not None else None)
        desired = dict(curve_id=curve_id, stop_price=stop, target_price=target, curve_check_from=baseline,
                       breakeven_enabled=int(breakeven_enabled), breakeven_triggered_at=triggered,
                       breakeven_price=protection, levels_customized=1)
        if all(row.get(key) == value for key, value in desired.items()):
            return self._public_order(row)
        now = self._now().isoformat(sep=" ")
        with db.session(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE managed_orders SET curve_id = ?, stop_price = ?, target_price = ?, curve_check_from = ?, "
                "breakeven_enabled = ?, breakeven_triggered_at = ?, breakeven_price = ?, levels_customized = 1, "
                "curve_armed = 0, risk_revision = risk_revision + 1, updated_at = ? WHERE id = ? AND risk_revision = ?",
                (curve_id, stop, target, baseline, int(breakeven_enabled), triggered, protection, now, row_id, risk_revision))
            if not cursor.rowcount:
                raise RiskConflict("出場設定已更新，請重新核對")
            conn.commit()
        self._curve_break_cache.clear()
        return self._public_order(self._active_order(row_id))

    @risk_locked
    def update_levels(self, row_id: str, stop: float, target: float | None) -> dict:
        row = self._editable_risk_order(row_id)
        return self.update_risk_settings(row_id, curve_id=row.get("curve_id"), stop_price=stop,
                                         target_price=target, breakeven_enabled=bool(row["breakeven_enabled"]),
                                         risk_revision=row["risk_revision"])

    @risk_locked
    def update_curve(self, row_id: str, target_price: float) -> dict:
        row = self._editable_risk_order(row_id)
        if row.get("curve_id") is None:
            raise KeyError("這筆委託沒有啟用曲線停利")
        target_price = float(target_price)
        if not math.isfinite(target_price):
            raise ValueError("曲線停利價格必須是有效數字")
        current = self._curve_value(row)
        if current is None:
            raise ValueError("選用曲線目前無法計算")
        with db.session(self.db_path) as conn:
            record = conn.execute(
                "SELECT anchors FROM trendlines WHERE id = ? AND code = ? AND kind = 'curve' AND enabled = 1",
                (row["curve_id"], row["code"]),
            ).fetchone()
            if record is None:
                raise ValueError("選用曲線已刪除")
            anchors = json.loads(record["anchors"])
            shift = target_price - current
            for anchor in anchors:
                anchor["price"] = float(anchor["price"]) + shift
            if not db.update_trendline_anchors(conn, row["curve_id"], anchors):
                raise ValueError("曲線更新失敗")
        self._curve_cache.pop((int(row["curve_id"]), row["code"]), None)
        self._curve_break_cache.clear()
        with db.session(self.db_path) as conn:
            conn.execute("UPDATE managed_orders SET risk_revision = risk_revision + 1 WHERE curve_id = ?", (row["curve_id"],))
            conn.commit()
        return self._public_order(self._active_order(row_id))

    @risk_locked
    def set_order_curve(self, row_id: str, curve_id: int | None) -> dict:
        """Legacy selection API retains the stop; the editor previews both ATR defaults."""
        row = self._editable_risk_order(row_id)
        target = None
        if curve_id is None:
            _, target = stop_target_prices(row["action"], row["avg_fill_price"], row["atr"])
            target = align_tick(target, float(row.get("contract_tick") or 1))
        return self.update_risk_settings(row_id, curve_id=curve_id, stop_price=row["stop_price"],
                                         target_price=target, breakeven_enabled=bool(row["breakeven_enabled"]),
                                         risk_revision=row["risk_revision"])

    def cancel_entry(self, row_id: str) -> dict:
        """Cancel the unfilled remainder of a managed entry order."""
        row = self._active_order(row_id)
        if not row or not int(row["active"]):
            raise KeyError("找不到可取消的有效委託")
        if row.get("exit_trade_id") or row.get("triggered_at"):
            raise ValueError("此委託已進入出場流程，請先核對委託狀態")
        if row.get("entry_status") in {"Filled", "Cancelled", "Failed", "Rejected"}:
            raise ValueError("進場委託已結束；若已有成交部位，請使用平倉功能")

        api = self.ensure_connected()
        # Use the SDK's existing trade object first. A synchronous status query
        # can wait up to 30 seconds before the cancellation is even submitted.
        trade = next((item for item in api.list_trades()
                      if str(_trade_id(item)) == str(row["entry_trade_id"])), None)
        if trade is None:
            trade = self._trades_by_id().get(str(row["entry_trade_id"]))
        if trade is None:
            raise RuntimeError("無法取得券商委託狀態，為避免誤取消，請稍後重試")
        with self._lock:
            cancelled = api.cancel_order(trade)
        self._persist_trade_state(row_id, entry=cancelled or trade)
        return self._public_order(self._active_order(row_id))

    def orders(self) -> list[dict]:
        with db.session(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM managed_orders WHERE code = ? AND simulation = ? ORDER BY created_at DESC LIMIT 30",
                                (self.code, int(self.simulation))).fetchall()
        result = []
        for row in rows:
            item = self._public_order(dict(row))
            if item.get("curve_id") is not None:
                item["curve_price"] = self._curve_value(dict(row))
            else:
                item["curve_price"] = None
            result.append(item)
        return result

    def _public_order(self, row: dict) -> dict:
        item = {key: row.get(key) for key in (
            "id", "code", "contract_code", "action", "quantity", "filled_quantity",
            "avg_fill_price", "price", "price_type", "order_type", "atr", "stop_price",
            "target_price", "curve_id", "auto_exit", "simulation", "entry_trade_id",
            "curve_check_from", "contract_tick", "breakeven_enabled", "breakeven_triggered_at", "breakeven_price", "risk_revision",
            "entry_status", "exit_trade_id", "exit_status", "exit_filled_quantity",
            "triggered_at", "trigger_reason", "last_quote", "last_quote_at", "active",
            "error", "created_at", "updated_at",
        )}
        item["curve_price"] = self._curve_value(row) if row.get("curve_id") is not None else None
        entry = row.get("avg_fill_price") or row.get("price")
        item["default_stop_price"] = item["default_target_price"] = None
        if entry and row.get("atr"):
            stop, target = stop_target_prices(row["action"], float(entry), float(row["atr"]))
            tick = float(row.get("contract_tick") or 1)
            item.update(default_stop_price=align_tick(stop, tick), default_target_price=align_tick(target, tick))
        quote = self._fresh_risk_quote(row)
        if quote is not None:
            item["last_quote"] = quote
        return item

    def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        with self._lock:
            if self._api is not None:
                for code in list(self._subscribed):
                    try:
                        contract = self._find_contract(code)
                        self._api.quote.unsubscribe(
                            contract, quote_type=self._sdk.QuoteType.Tick, version=self._sdk.QuoteVersion.v1
                        )
                    except Exception:  # noqa: BLE001
                        pass
                self._close_api()
            self._subscribed.clear()

