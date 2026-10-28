"""Independent read-only supervision. No Shioaji login or order API dependency.

Run separately from monitor/dashboard. Notification delivery is opt-in on the
CLI; incidents and delivery failures are always persisted outside the market DB.
"""
from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import signal
import sqlite3
import threading

import pandas as pd
import market_clock
import market_health as mh
from file_lease import FileLease


def age_seconds(value, now):
    try:
        age = (pd.Timestamp(now) - pd.Timestamp(value)).total_seconds()
        return age if pd.notna(age) and age >= -5 else float("inf")
    except (TypeError, ValueError):
        return float("inf")


def inspect_health(db_path, code, *, now, settings, calendar, mode="production", read=mh.read_health):
    """Return stable incident keys, not a single misleading 'connected' flag."""
    issues = {}
    active = calendar.covered(now) and calendar.active(now)
    if not calendar.covered(now):
        issues["calendar"] = "交易日曆未設定、無效或已超出驗證範圍；曲線交易暫停"
    for role in ("monitor", "trading"):
        document = read(mh.health_path(db_path, role, code))
        if (document.get("role") != role or document.get("code") != code
                or document.get("db_path") != str(Path(db_path).resolve()) or document.get("mode") != mode):
            issues[f"{role}.identity"] = f"{role} 健康紀錄缺失或環境不符"
            continue
        heartbeat_age = age_seconds(document.get("heartbeat_at"), now)
        if heartbeat_age > settings.heartbeat_seconds:
            issues[f"{role}.heartbeat"] = f"{role} 心跳逾時，程序可能終止或卡住"
        if document.get("operation") and age_seconds(document.get("operation_started"), now) > settings.operation_timeout_seconds:
            issues[f"{role}.operation"] = f"{role} 操作逾時：{document['operation']}"
        if document.get("state") in {"failed", "stopped", "retry_wait", "recovering", "connecting"}:
            issues[f"{role}.state"] = f"{role} 狀態：{document['state']}"
        if role == "monitor":
            if document.get("state") not in {"healthy", "checking", "waiting_market"}:
                issues["monitor.recovery"] = "行情連線／回補尚未完成驗證"
            bars = document.get("bars", {})
            if not bars.get("ready") and not bars.get("opening_grace"):
                issues["monitor.bars"] = (f"1 分 K 尚未驗證恢復；缺口 {bars.get('missing_count', '未知')}；"
                                          f"最後資料 {bars.get('last_minute_start', '未知')}")
            book = document.get("book", {})
            if active and book.get("required") and not book.get("fresh"):
                issues["monitor.book"] = "委託簿未收到新鮮資料"
        else:
            if not document.get("broker_ready"):
                issues["trading.reconciliation"] = document.get("reconciliation_error") or "券商委託／部位尚未核對"
            quotes = document.get("quotes", {})
            for contract, quote in quotes.items():
                if active and (not quote.get("fresh") or age_seconds(quote.get("last_quote_at"), now) > settings.tick_stale_seconds):
                    issues[f"trading.tick.{contract}"] = f"{contract} tick 過期；依賴該報價的本機自動保護受影響"
            if quotes and age_seconds(document.get("last_order_sync_at"), now) > settings.order_sync_seconds:
                issues["trading.order_sync"] = "委託狀態同步逾時；沒有成交回報本身不代表故障"
            if document.get("recovery", {}).get("pending"):
                issues["trading.recovery"] = "交易連線待恢復／核對，請檢查重連開關及券商端"
    # Independent DB check catches an otherwise fresh file describing old bars.
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1) as connection:
            row = connection.execute("SELECT MAX(minute_start) FROM minute_bars WHERE code = ?", (code,)).fetchone()
        latest = None if row is None or row[0] is None else pd.Timestamp(row[0])
        if latest is not None and latest > pd.Timestamp(now):
            issues["database.clock"] = "資料庫含未來時間的 K 線，請核對來源與時區"
        if calendar.covered(now):
            expected = calendar.expected_minutes(pd.Timestamp(now) - pd.Timedelta(days=7), now)
            last_expected = max(expected) if expected else None
            start = next((a for a, b in calendar.sessions if a <= pd.Timestamp(now) < b), None)
            opening = (start is not None and (latest is None or latest < start)
                       and (pd.Timestamp(now) - start).total_seconds() <= settings.bar_lag_seconds + 60)
            target = pd.Timestamp(now) - pd.Timedelta(minutes=1) if active else last_expected
            tolerance = settings.bar_lag_seconds if active else 0
            if not opening and last_expected is not None and (latest is None or
                    (target - latest).total_seconds() > tolerance):
                issues["database.bars"] = f"資料庫 K 線停止前進；最後資料 {mh.iso(latest)}"
    except (sqlite3.Error, OSError, ValueError):
        issues["database.read"] = "無法唯讀檢查行情資料庫；未執行任何 migration 或寫入"
    return issues


class IncidentNotifier:
    """Persistent transitions, throttled repeats, retries, and local fallback log."""
    def __init__(self, path, settings, *, send=None):
        self.path = Path(path)
        self.settings = settings
        self.send = send
        self.state = mh.read_health(self.path)
        incidents = self.state.get("incidents", {})
        self.incidents = {key: item for key, item in incidents.items()
                          if isinstance(item, dict) and isinstance(item.get("active"), bool)
                          and isinstance(item.get("notified_active"), bool)} if isinstance(incidents, dict) else {}
        self.state["incidents"] = self.incidents

    def update(self, issues, now):
        now = pd.Timestamp(now)
        due_items = []
        for key in sorted(set(self.incidents) | set(issues)):
            item = self.incidents.setdefault(key, {"active": False, "notified_active": False, "attempts": 0})
            active = key in issues
            transition = active != item["active"]
            if transition:
                item.update(active=active, changed_at=mh.iso(now), attempts=0, next_attempt_at=mh.iso(now))
                event = {"at": mh.iso(now), "channel": key, "active": active,
                         "reason": issues.get(key, "恢復"), "delivery": "pending" if self.send else "log_only"}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.with_suffix(".events.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                print(json.dumps(event, ensure_ascii=False), flush=True)
            if active:
                item["reason"] = issues[key]
            if self.send is None:
                continue
            pending = transition or active != item["notified_active"] or item.get("attempts", 0) > 0
            repeating = active and age_seconds(item.get("last_sent_at"), now) >= self.settings.alert_repeat_seconds
            due = not item.get("next_attempt_at") or now >= pd.Timestamp(item["next_attempt_at"])
            if not due or not (pending or repeating):
                continue
            due_items.append((key, item))
        if due_items:
            # One bounded transport call per inspection, even if many channels
            # fail together. Never let N failed sends hide the supervisor.
            parts = [f"<b>{'行情／交易故障' if item['active'] else '行情／交易恢復'}</b>\n{escape(key)}\n{escape(item.get('reason', '')[:200])}"
                     for key, item in due_items[:8]]
            if len(due_items) > 8:
                parts.append(f"另有 {len(due_items) - 8} 個狀態變更，請查看本機事件紀錄")
            text = "\n\n".join(parts) + f"\n{escape(mh.iso(now))}"
            try:
                delivered = bool(self.send(text))
            except Exception:
                delivered = False
            for key, item in due_items:
                item["attempts"] += 1
                if delivered:
                    item.update(notified_active=item["active"], last_sent_at=mh.iso(now), attempts=0)
                else:
                    print(f"notification failed: {key}; retry scheduled; see local incident log", flush=True)
                item["next_attempt_at"] = mh.iso(now + pd.Timedelta(seconds=self.settings.notification_retry_seconds))
        mh.atomic_json(self.path, self.state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--health-config", type=Path, default=mh.DEFAULT_CONFIG)
    parser.add_argument("--mode", choices=("production", "simulation"), default="production")
    parser.add_argument("--notify", action="store_true", help="enable Telegram delivery; local incidents are always logged")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    if not 0 < args.interval <= 60:
        parser.error("interval must be in (0, 60]")
    settings = mh.load_settings(args.health_config)
    directory = mh.health_path(args.db, "monitor", args.code).parent
    directory.mkdir(parents=True, exist_ok=True)
    with FileLease(directory / f"watchdog-{args.code}.lock"):
        send = None
        if args.notify:
            import telegram_notify
            send = telegram_notify.send_message
        notifier = IncidentNotifier(directory / f"incidents-{args.code}.json", settings, send=send)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, lambda *_: stop.set())
        while not stop.is_set():
            calendar = mh.SessionCalendar.load(settings.calendar_path)
            now = market_clock.now()
            issues = inspect_health(args.db, args.code, now=now, settings=settings, calendar=calendar, mode=args.mode)
            mh.atomic_json(directory / f"watchdog-{args.code}.json",
                           {"heartbeat_at": mh.iso(market_clock.now()), "issues": issues,
                            "notification_enabled": args.notify})
            notifier.update(issues, now)
            if args.once:
                return 1 if issues else 0
            stop.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
