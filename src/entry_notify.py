"""Persistently deduplicate position-fill notifications off the trading thread.

Input is a persisted managed-order snapshot after entry/exit quantity increases.
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
from latency_trace import order_link


class EntryNotifier:
    def __init__(self, db_path, *, enabled=True, alerts_path=DEFAULT_CONFIG_PATH, dispatch=None):
        self.db_path = db_path
        self.enabled = enabled
        self.config = AlertConfig(alerts_path)
        self.dispatch = dispatch or self._background
        self.latency = None

    @staticmethod
    def _background(task):
        threading.Thread(target=task, name="telegram-fill", daemon=True).start()

    def notify(self, row):
        self._notify(row, 'entry')

    def notify_exit(self, row):
        self._notify(row, 'exit')

    def _notify(self, row, phase):
        try:
            if not self.enabled:
                return
            self.config.reload()
            if not self.config.telegram_enabled:
                return
            quantity = row['filled_quantity'] if phase == 'entry' else row['exit_filled_quantity']
            if quantity <= 0:
                return
            with db.session(self.db_path) as conn:
                if row.get('curve_id') is not None:
                    drawing = conn.execute('SELECT kind FROM trendlines WHERE id=? AND code=?',
                                           (row['curve_id'], row['code'])).fetchone()
                    if drawing is not None:
                        row = {**row, 'curve_kind': drawing['kind']}
                message = tg.format_entry_fill(row) if phase == 'entry' else tg.format_exit_fill(row)
                alert_id = db.record_alert(
                    conn, row["code"], hour_start=row["created_at"],
                    box_type=f"{phase}:{row['id']}:{quantity}",
                    box_id=None, visit_id=None, kind=f"{phase}_fill", message=message,
                )
            if alert_id is not None:
                timing = dict(order_id=row['id'], phase=phase, alert_id=alert_id,
                              cumulative_quantity=quantity, **order_link(self.db_path, row['id']))
                if self.latency:
                    self.latency.record('notification_queued', **timing)
                self.dispatch(lambda: self._deliver(alert_id, message, timing))
        except Exception as error:  # notification failures cannot interrupt trading
            print(f"[Telegram] {phase} notification failed: {type(error).__name__}")

    def _deliver(self, alert_id, message, timing=None):
        ok = False
        began = self.latency.record('notification_started', **(timing or {})) if self.latency else None
        try:
            ok = tg.send_message(message)
        except Exception as error:
            print(f"[Telegram] Fill delivery failed: {type(error).__name__}")
        try:
            with db.session(self.db_path) as conn:
                db.mark_alert_sent(conn, alert_id, ok)
        except Exception as error:
            print(f"[Telegram] Fill delivery status failed: {type(error).__name__}")
        if self.latency:
            self.latency.record('notification_completed', started=began,
                                state='succeeded' if ok else 'failed', **(timing or {}))
            self.latency.flush()
