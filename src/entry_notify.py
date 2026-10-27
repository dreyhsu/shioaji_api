"""Persistently deduplicate entry-fill notifications and send off the trading thread.

Input is a persisted managed-order snapshot after its filled quantity increases.
Each cumulative quantity claims one alert, including across service restarts.
Disabled notifications are skipped without consuming an alert; the trading service
still saves fills, so enabling later does not replay skipped fills. Transport or
storage failures are best effort and must never affect orders or exit monitoring.
"""
from __future__ import annotations

import threading

import db
from alert_rules import AlertConfig, DEFAULT_CONFIG_PATH
import telegram_notify as tg


class EntryNotifier:
    def __init__(self, db_path, *, enabled=True, alerts_path=DEFAULT_CONFIG_PATH, dispatch=None):
        self.db_path = db_path
        self.enabled = enabled
        self.config = AlertConfig(alerts_path)
        self.dispatch = dispatch or self._background

    @staticmethod
    def _background(task):
        threading.Thread(target=task, name="telegram-entry", daemon=True).start()

    def notify(self, row):
        try:
            if not self.enabled:
                return
            self.config.reload()
            if not self.config.telegram_enabled:
                return
            message = tg.format_entry_fill(row)
            with db.session(self.db_path) as conn:
                alert_id = db.record_alert(
                    conn, row["code"], hour_start=row["created_at"],
                    box_type=f"entry:{row['id']}:{row['filled_quantity']}",
                    box_id=None, visit_id=None, kind="entry_fill", message=message,
                )
            if alert_id is not None:
                self.dispatch(lambda: self._deliver(alert_id, message))
        except Exception as error:  # notification failures cannot interrupt trading
            print(f"[Telegram] Entry notification failed: {type(error).__name__}: {error}")

    def _deliver(self, alert_id, message):
        ok = False
        try:
            ok = tg.send_message(message)
        except Exception as error:
            print(f"[Telegram] Entry delivery failed: {type(error).__name__}: {error}")
        try:
            with db.session(self.db_path) as conn:
                db.mark_alert_sent(conn, alert_id, ok)
        except Exception as error:
            print(f"[Telegram] Entry delivery status failed: {type(error).__name__}: {error}")
