"""One shared, persistent curve-entry lifecycle for every trading adapter.

The host trading service supplies storage, a clock, settings and order execution.
Claiming the signal in SQLite before submission prevents repeat orders, including
after an uncertain response or process restart. No broker or UI dependency.
"""
from __future__ import annotations

import sqlite3
import pandas as pd

import db
import trendline as tl
from curve_entry import crossed_curve
from place_order import OrderRequest, validate_request


class CurveEntryController:
    def __init__(self, service):
        self.service = service

    def snapshot(self):
        service = self.service
        with db.session(service.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM curve_entries WHERE code = ? AND simulation = ? ORDER BY id DESC LIMIT 1",
                (service.code, int(service.simulation)),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["simulation"] = bool(result["simulation"])
        return result

    def _curve(self, curve_id):
        with db.session(self.service.db_path) as conn:
            return next((curve for curve in db.load_trendlines(conn, self.service.code)
                         if curve["id"] == curve_id and curve["kind"] == "curve"), None)

    def _update(self, entry_id, **values):
        with db.session(self.service.db_path) as conn:
            conn.execute(f"UPDATE curve_entries SET {', '.join(key + ' = ?' for key in values)} WHERE id = ?",
                         (*values.values(), entry_id))
            conn.commit()

    def arm(self, curve_id, contract_code, quantity, *, simulation, confirmed, action="Buy"):
        service = self.service
        with service._lock:
            validate_request(OrderRequest(contract_code, action, quantity, price_type="MKT", order_type="IOC",
                                          simulation=simulation, confirmed=confirmed), service.simulation)
            if not service.auto_entry_enabled():
                raise ValueError("自動進場已停用，請檢查 config/trading.json")
            pending = self.snapshot()
            if pending and pending["state"] in {"armed", "submitting"}:
                raise ValueError("已有啟用或待核對的曲線進場，請先取消或核對")
            contracts = {item["code"] for item in service.contracts()}
            if contract_code not in contracts:
                raise ValueError("請選擇有效的月份合約")
            service._validate_connected_mode()
            _atr, hourly = service._risk_context(service.code)
            curve = self._curve(curve_id)
            if curve is None or not tl.anchor_indices(hourly, curve["anchors"], "curve"):
                raise ValueError("請選擇已保存、有效的 1 小時曲線")
            now = pd.Timestamp(service._now())
            service.subscribe_contract(contract_code)
            try:
                with db.session(service.db_path) as conn:
                    conn.execute(
                        "INSERT INTO curve_entries (code, simulation, curve_id, contract_code, quantity, action, "
                        "state, armed_at, check_from) VALUES (?, ?, ?, ?, ?, ?, 'armed', ?, ?)",
                        (service.code, int(simulation), curve_id, contract_code, quantity, action,
                         now.isoformat(sep=" "), now.ceil("min").isoformat(sep=" ")),
                    )
                    conn.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError("已有啟用或待核對的曲線進場") from error
            service._start_worker()
            return self.snapshot()

    def cancel(self):
        with self.service._lock:
            entry = self.snapshot()
            if entry and entry["state"] == "armed":
                with db.session(self.service.db_path) as conn:
                    conn.execute("UPDATE curve_entries SET state = 'cancelled' WHERE id = ? AND state = 'armed'",
                                 (entry["id"],))
                    conn.commit()
            return self.snapshot()

    def fail_pending(self, error):
        entry = self.snapshot()
        if entry and entry["state"] == "armed":
            self._update(entry["id"], state="failed", error=str(error))

    def check(self):
        service = self.service
        entry = self.snapshot()
        if not entry or entry["state"] != "armed":
            return
        if not service.auto_entry_enabled():
            self._update(entry["id"], state="cancelled", error="自動進場開關已停用，請重新啟用等待")
            return
        service._validate_connected_mode()
        curve = self._curve(entry["curve_id"])
        if curve is None:
            self._update(entry["id"], state="failed", error="曲線已刪除，進場已停用")
            return
        with db.session(service.db_path) as conn:
            minutes = db.load_minute_bars(conn, service.code)
        if minutes.empty:
            return
        now = pd.Timestamp(service._now())
        closed = minutes[minutes["minute_start"] + pd.Timedelta(minutes=1) <= now]
        if closed.empty:
            return
        latest = pd.Timestamp(closed.iloc[-1]["minute_start"])
        if latest < pd.Timestamp(entry["check_from"]):
            return
        if entry["last_checked_minute"] and latest <= pd.Timestamp(entry["last_checked_minute"]):
            return
        # Wait for the matching adapter's fresh quote before submitting a market order.
        contract = service._find_contract(entry["contract_code"])
        quote = service._quotes.get(str(contract.code))
        if not quote or not -5 <= (now - quote["at"]).total_seconds() <= 10:
            return
        self._update(entry["id"], last_checked_minute=latest.isoformat(sep=" "))
        if not crossed_curve(closed, curve["anchors"], now, entry["action"]):
            return
        with db.session(service.db_path) as conn:
            claimed = conn.execute("UPDATE curve_entries SET state = 'submitting' WHERE id = ? AND state = 'armed'",
                                   (entry["id"],)).rowcount
            conn.commit()
        if not claimed:
            return
        try:
            result = service.place_order({
                "contract_code": entry["contract_code"], "action": entry["action"], "quantity": entry["quantity"],
                "price_type": "MKT", "order_type": "IOC", "price": 0, "confirmed": True,
                "simulation": entry["simulation"],
            })
            self._update(entry["id"], state="triggered", order_id=result["id"], triggered_at=now.isoformat(sep=" "))
        except Exception as error:
            # Keep an uncertain submission locked: neither the worker nor a restart retries it.
            self._update(entry["id"], state="failed", error=str(error))
