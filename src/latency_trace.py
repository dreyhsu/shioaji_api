"""Best-effort, bounded local timing records. No network or broker dependency.

Wall clocks correlate processes; monotonic durations are valid only within one
recorder instance. Callbacks only enqueue into memory; owners explicitly flush.
"""
from collections import deque
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS latency_events (
 id TEXT PRIMARY KEY, trace_id TEXT, command_id TEXT, order_id TEXT, broker_trade_id TEXT,
 role TEXT NOT NULL, code TEXT NOT NULL, mode TEXT NOT NULL, instance TEXT NOT NULL,
 generation INTEGER, event TEXT NOT NULL, occurred_at TEXT NOT NULL,
 monotonic_ns INTEGER NOT NULL, elapsed_ms REAL, document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_latency_trace ON latency_events(trace_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_latency_order ON latency_events(order_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_latency_market ON latency_events(code,mode,event);
CREATE TABLE IF NOT EXISTS latency_order_links (
 order_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, command_id TEXT, signal_minute TEXT);
CREATE TABLE IF NOT EXISTS latency_entry_links (
 entry_id INTEGER PRIMARY KEY, trace_id TEXT NOT NULL, command_id TEXT);
'''
_scope = ContextVar('latency_scope', default={})
_details = {'signal_minute', 'minute_start', 'closed_at', 'published_at', 'adopted_at', 'ready_at',
            'source', 'source_instance', 'source_generation', 'source_revision', 'phase', 'quantity',
            'cumulative_quantity', 'state', 'reason', 'error_type', 'snapshot_id', 'callback_id',
            'seqno', 'ordno', 'reported_trade_id', 'alert_id', 'client_elapsed_ms', 'client_observed_at', 'broker_filled_at'}
TAIPEI = timezone(timedelta(hours=8))


def timestamp(value):
    stamp = datetime.fromisoformat(str(value))
    stamp = stamp.replace(tzinfo=TAIPEI) if stamp.tzinfo is None else stamp.astimezone(TAIPEI)
    return stamp.isoformat(sep=' ', timespec='microseconds')


def identity(value):
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= 160 and re.fullmatch(r'[A-Za-z0-9_:./+ -]+', value) else None


@contextmanager
def trace_scope(**values):
    token = _scope.set({**_scope.get(), **values})
    try:
        yield
    finally:
        _scope.reset(token)


def trace_values():
    return dict(_scope.get())


class LatencyRecorder:
    def __init__(self, path, role, code, mode, *, clock=None, monotonic=time.monotonic, capacity=4096):
        self.path, self.role, self.code, self.mode = Path(path), role, code, mode
        self.clock = clock or (lambda: datetime.now(TAIPEI))
        self.monotonic = monotonic
        self.instance = uuid.uuid4().hex
        self._events = deque()
        self._lock = threading.Lock()
        self.capacity = capacity
        self.dropped = 0
        self.error = None

    def record(self, event, *, started=None, at=None, mono=None, **values):
        try:
            moment = self.monotonic() if mono is None else mono
            fields = {**_scope.get(), **values}
            details = {key: value for key, value in fields.items() if key in _details and
                       (value is None or isinstance(value, (str, int, float, bool)))}
            details = {key: value[:256] if isinstance(value, str) else value for key, value in details.items()}
            row = (uuid.uuid4().hex, *(identity(fields.get(key)) for key in
                   ('trace_id', 'command_id', 'order_id', 'broker_trade_id')),
                   self.role, fields.get('code', self.code), fields.get('mode', self.mode), self.instance, fields.get('generation'), event,
                   timestamp(self.clock() if at is None else at), int(moment * 1_000_000_000),
                   None if started is None else max(0, (moment - started) * 1000),
                   json.dumps(details, ensure_ascii=False, allow_nan=False))
            with self._lock:
                if len(self._events) >= self.capacity:
                    self.dropped += 1
                else:
                    self._events.append(row)
            return moment
        except Exception as error:
            self.error = type(error).__name__
            return self.monotonic()

    def flush(self):
        with self._lock:
            rows = list(self._events)
            self._events.clear()
        if not rows:
            return
        try:
            # A tracing failure or busy database must not hold the trading loop.
            import db
            with closing(db.connect(self.path, timeout=0.05)) as conn:
                conn.executescript(SCHEMA)
                conn.executemany('INSERT OR IGNORE INTO latency_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
                conn.commit()
            self.error = None
        except Exception as error:
            self.error = type(error).__name__
            with self._lock:
                for row in reversed(rows):
                    if len(self._events) < self.capacity:
                        self._events.appendleft(row)
                    else:
                        self.dropped += 1

    def health(self):
        with self._lock:
            return dict(instance=self.instance, pending=len(self._events), dropped=self.dropped, error=self.error)


def link_order(conn, order_id):
    fields = _scope.get()
    trace_id = identity(fields.get('trace_id')) or uuid.uuid4().hex
    try:
        conn.execute('INSERT OR IGNORE INTO latency_order_links VALUES (?,?,?,?)',
                     (order_id, trace_id, identity(fields.get('command_id')), fields.get('signal_minute')))
        return True
    except sqlite3.Error:
        return False


def order_link(path, order_id):
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.05)) as conn:
            row = conn.execute('SELECT trace_id,command_id,signal_minute FROM latency_order_links WHERE order_id=?',
                               (order_id,)).fetchone()
        return dict(zip(('trace_id', 'command_id', 'signal_minute'), row)) if row else {}
    except sqlite3.Error:
        return {}
