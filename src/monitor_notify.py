"""Bounded market-alert delivery, isolated from monitor's market-data loop.

Only freshly claimed alert IDs enter this in-memory queue. Restart never scans
or replays history; SQLite remains the owner of cooldown/deduplication claims.
The worker opens its own connection *after* HTTP completes, never shares the
monitor connection, and never holds a database transaction across network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full, Queue
import threading

from alert_rules import AlertConfig
import db
import telegram_notify as tg


@dataclass(frozen=True)
class _Notification:
    alert_id: int
    message: str
    epoch: int


class MonitorNotifier:
    def __init__(self, db_path, alerts_path, *, enabled=True, capacity=256,
                 send=None, logger=print):
        if capacity < 1:
            raise ValueError('notification capacity must be positive')
        self.db_path = db_path
        self.config = AlertConfig(alerts_path)  # belongs to this worker only
        self.send = send or tg.send_message
        self.log = logger
        self._queue = Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._epoch = 0
        self._in_flight = None
        self._sent = self._failed = self._skipped = self._rejected = 0
        self._last_error = None
        self._thread = threading.Thread(target=self._run, name='monitor-telegram', daemon=True)
        self._thread.start()

    def set_enabled(self, enabled):
        with self._lock:
            if self._enabled and not enabled:
                self._epoch += 1  # queued alerts cannot reappear after a pause
            self._enabled = bool(enabled)

    def record_error(self, reason):
        with self._lock:
            self._last_error = reason

    def submit(self, alert_id, message):
        """Nonblocking enqueue; caller records rejected claims as unsuccessful."""
        with self._lock:
            if self._stop.is_set() or not self._enabled:
                self._rejected += 1
                self._last_error = 'stopped_or_paused'
                return False
            try:
                self._queue.put_nowait(_Notification(alert_id, message, self._epoch))
            except Full:
                self._rejected += 1
                self._last_error = 'queue_full'
                return False
        return True

    def snapshot(self):
        with self._lock:
            return dict(worker_alive=self._thread.is_alive(), stopping=self._stop.is_set(),
                        enabled=self._enabled, queued=self._queue.qsize(),
                        in_flight=self._in_flight, sent=self._sent, failed=self._failed,
                        skipped=self._skipped, rejected=self._rejected, last_error=self._last_error)

    def _finish(self, job, ok, reason=None):
        try:
            with db.session(self.db_path) as conn:
                db.mark_alert_sent(conn, job.alert_id, ok)
        except Exception as error:
            reason = f'delivery_status_{type(error).__name__}'
        with self._lock:
            if reason in {'paused', 'stopped'}:
                self._skipped += 1
            elif ok:
                self._sent += 1
            else:
                self._failed += 1
            if reason:
                self._last_error = reason
        # Fixed classifications only: exception messages can contain credentials.
        self.log(f'telegram alert={job.alert_id}: {"sent" if ok else "not_sent"}'
                 + (f' ({reason})' if reason else ''))

    def _deliver(self, job):
        self.config.reload()
        if not self.config.telegram_enabled:
            self.set_enabled(False)
        with self._lock:
            reason = ('stopped' if self._stop.is_set() else
                      'paused' if not self._enabled or job.epoch != self._epoch else None)
            if reason is None:
                # Once marked in-flight, an HTTP request may complete after pause/stop.
                self._in_flight = job.alert_id
        if reason:
            self._finish(job, False, reason)
            return
        ok, reason = False, None
        try:
            ok = bool(self.send(job.message))
            if not ok:
                reason = 'delivery_failed'
        except Exception as error:
            reason = f'delivery_{type(error).__name__}'
        finally:
            self._finish(job, ok, reason)
            with self._lock:
                self._in_flight = None

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    job = self._queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    self._deliver(job)
                except Exception as error:
                    self._finish(job, False, f'worker_{type(error).__name__}')
                finally:
                    self._queue.task_done()
        finally:
            while True:
                try:
                    job = self._queue.get_nowait()
                except Empty:
                    break
                try:
                    self._finish(job, False, 'stopped')
                finally:
                    self._queue.task_done()

    def close(self, timeout=1.0):
        """Bound shutdown; a stuck HTTP request stays unconfirmed, never replayed."""
        self._stop.set()
        self.set_enabled(False)
        self._thread.join(timeout=max(0, timeout))
        if self._thread.is_alive():
            self.record_error('shutdown_delivery_unconfirmed')
            self.log('telegram shutdown: delivery still in flight; no history replay')
        return not self._thread.is_alive()
