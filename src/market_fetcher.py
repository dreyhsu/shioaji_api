"""Shared, injectable lifecycle for independent market-data processes.

No monitor import, credentials, broker login, or database access at import time.
SDK callbacks only enqueue; the owning thread performs storage and recovery.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import signal
import threading
import time
import uuid

import pandas as pd

from connection_recovery import RecoveryGate, error_kind
from fetcher_storage import FetcherStore
from file_lease import FileLease
import market_health as mh
import shioaji_client as sc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'config' / 'market_services.json'
LOG = logging.getLogger('market_fetcher')


def taipei_now():
    # Fixed modern Taipei offset avoids OS timezone/tzdata differences on Windows.
    return pd.Timestamp(datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None))


@dataclass(frozen=True)
class FetcherSettings:
    timezone: str = 'Asia/Taipei'
    calendar_path: str = str(ROOT / 'config' / 'market_calendar.json')
    scheduled_login_times: tuple[str, ...] = ('08:44', '14:59')
    scheduled_logout_times: tuple[str, ...] = ('13:46', '05:01')
    tick_publish_seconds: float = 1
    kbar_publish_seconds: int = 60
    bar_close_delay_seconds: float = 2
    tick_stale_seconds: float = 10
    stream_recovery_seconds: float = 60
    queue_capacity: int = 20000
    api_timeout_ms: int = 30000
    backfill_days: int = 2
    history_repair_seconds: float = 300

    def __post_init__(self):
        if self.timezone != 'Asia/Taipei':
            raise ValueError('timezone must be Asia/Taipei')
        for name in ('scheduled_login_times', 'scheduled_logout_times'):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or not values or any(
                not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value)
                for value in values
            ):
                raise ValueError(f'{name} must contain HH:MM times')
        events = sorted([(value, 'login') for value in self.scheduled_login_times]
                        + [(value, 'logout') for value in self.scheduled_logout_times])
        if len({value for value, _ in events}) != len(events) or any(
            events[i][1] == events[(i + 1) % len(events)][1] for i in range(len(events))
        ):
            raise ValueError('login/logout times must be distinct and alternate across midnight')
        for name in ('tick_publish_seconds', 'bar_close_delay_seconds', 'tick_stale_seconds',
                     'stream_recovery_seconds', 'history_repair_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite')
        for name in ('queue_capacity', 'api_timeout_ms', 'backfill_days', 'kbar_publish_seconds'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.kbar_publish_seconds != 60 or self.bar_close_delay_seconds >= 60:
            raise ValueError('1m bars require kbar_publish_seconds=60 and close delay <60')
        if self.stream_recovery_seconds < self.tick_stale_seconds:
            raise ValueError('stream recovery must allow a freshness grace period')

    @classmethod
    def load(cls, path=DEFAULT_CONFIG):
        path = Path(path)
        values = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(values, dict) or set(values) - {item.name for item in fields(cls)}:
            raise ValueError('invalid market services configuration')
        if 'calendar_path' in values:
            values['calendar_path'] = str((path.parent / values['calendar_path']).resolve())
        return cls(**values)


class SessionSchedule:
    def __init__(self, settings):
        self.settings = settings

    def window(self, now, calendar):
        """Return the current configured login/logout window, only if verified open.

        Absolute intervals, rather than last-event flags, handle restarts and
        sleep/resume without accidentally logging back in after logout.
        """
        now = pd.Timestamp(now)
        if not calendar.covered(now):
            return None
        for day in (now.normalize() - pd.Timedelta(days=1), now.normalize()):
            for login in self.settings.scheduled_login_times:
                left = day + pd.Timedelta(hours=int(login[:2]), minutes=int(login[3:]))
                ends = [day + pd.Timedelta(hours=int(value[:2]), minutes=int(value[3:]))
                        for value in self.settings.scheduled_logout_times]
                right = min(end if end > left else end + pd.Timedelta(days=1) for end in ends)
                if left <= now < right and any(a < right and b > left for a, b in calendar.sessions):
                    return left, right
        return None


@dataclass(frozen=True)
class Tick:
    code: str
    at: pd.Timestamp
    received_at: pd.Timestamp
    price: float
    volume: float
    amount: float


def normalize_tick(quote, code, now):
    def value(name, default=None):
        return quote.get(name, default) if isinstance(quote, dict) else getattr(quote, name, default)
    if value('simtrade', 0):
        return None
    stamp = pd.Timestamp(value('datetime'))
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert('Asia/Taipei').tz_localize(None)
    price, volume, amount = float(value('close')), float(value('volume', 0)), float(value('amount', 0))
    if (not all(math.isfinite(number) for number in (price, volume, amount))
            or price <= 0 or volume < 0 or amount < 0 or stamp > now + pd.Timedelta(seconds=5)):
        return None
    return Tick(code, stamp, now, price, volume, amount)


class FetcherRuntime:
    role = ''

    def __init__(self, db_path, codes, *, mode, settings=None, calendar=None, connect_factory=None,
                 clock=taipei_now, monotonic=time.monotonic, sdk=None):
        if mode not in {'simulation', 'production'}:
            raise ValueError('mode must be simulation or production')
        self.codes = tuple(dict.fromkeys(codes))
        if not self.codes or any(not re.fullmatch(r'[A-Za-z0-9]{1,24}', code) for code in self.codes):
            raise ValueError('codes must be nonempty alphanumeric contract codes')
        self.db_path, self.mode = Path(db_path).resolve(), mode
        self.settings = settings or FetcherSettings.load()
        self.clock, self.monotonic = clock, monotonic
        self.calendar = calendar
        self.schedule = SessionSchedule(self.settings)
        self.connect_factory = connect_factory or (lambda: sc.connect(simulation=mode == 'simulation'))
        self.sdk = sdk or sc.sj
        self.instance = uuid.uuid4().hex
        self.generation = 0
        self.api = self.context = None
        self.contracts = {}
        self.aliases = {}
        self.queue = Queue(maxsize=self.settings.queue_capacity)
        self.lock = threading.RLock()
        self.down, self.overflow = threading.Event(), threading.Event()
        self.accepting = False
        self.connected_at = None
        self.connected_mono = 0
        self.last_fresh = {}
        self.last_received = {}
        self.gate = RecoveryGate(clock=monotonic)
        self.started = False
        self.store = None
        folder = self.db_path.parent / '.health' / self.db_path.name
        self.health_path = folder / f'{self.role}-fetcher.json'
        # One owner per role/DB, even for overlapping contract lists or modes.
        self.lease = FileLease(folder / f'{self.role}-fetcher.lock')
        self.health = {'role': f'{self.role}_fetcher', 'pid': os.getpid(), 'instance': self.instance,
                       'mode': mode, 'codes': list(self.codes), 'db_path': str(self.db_path),
                       'state': 'starting', 'started_at': str(clock()), 'invalid_ticks': 0,
                       'queue_overflows': 0, 'staged_only': True}
        self._last_state = None
        self._last_health = float('-inf')
        self._window = None

    def start(self):
        if self.started:
            return
        self.lease.acquire()
        try:
            self.store = FetcherStore(self.db_path)
            self.started = True
            self.publish(state='starting')
        except BaseException:
            if self.store is not None:
                self.store.close()
            self.store = None
            self.started = False
            self.lease.close()
            raise

    def publish(self, **values):
        urgent = any(key in values and values[key] != self.health.get(key)
                     for key in ('state', 'operation', 'error'))
        self.health.update(values, heartbeat_at=str(self.clock()), generation=self.generation,
                           recovery=self.gate.snapshot())
        if not urgent and self.monotonic() - self._last_health < 1:
            return
        mh.atomic_json(self.health_path, self.health)
        self._last_health = self.monotonic()
        if self._last_state != self.health['state']:
            LOG.info('role=%s pid=%s mode=%s codes=%s state=%s generation=%s', self.role,
                     os.getpid(), self.mode, ','.join(self.codes), self.health['state'], self.generation)
            self._last_state = self.health['state']

    def _callback(self, generation, quote):
        with self.lock:
            if not self.accepting or generation != self.generation:
                return
            try:
                alias = str(quote.get('code', '') if isinstance(quote, dict) else getattr(quote, 'code', ''))
                for code in self.aliases.get(alias, ()):
                    tick = normalize_tick(quote, code, pd.Timestamp(self.clock()))
                    if tick is None:
                        self.health['invalid_ticks'] += 1
                        continue
                    try:
                        self.queue.put_nowait(tick)
                    except Full:
                        self.overflow.set()
            except (ValueError, TypeError, OverflowError):
                self.health['invalid_ticks'] += 1

    def _open(self):
        self.publish(state='connecting', operation='login', operation_started=str(self.clock()))
        self.context = self.connect_factory()
        self.api = self.context.__enter__()
        self.generation += 1
        generation = self.generation
        self.aliases, self.contracts = {}, {}
        for code in self.codes:
            contract = sc.resolve_contract(self.api, code)
            self.contracts[code] = contract
            for attr in ('code', 'symbol', 'target_code', 'full_code'):
                alias = str(getattr(contract, attr, '') or '')
                if alias:
                    self.aliases.setdefault(alias, set()).add(code)
        self.api.set_on_tick_fop_v1_callback(lambda *args: self._callback(generation, args[-1]))
        def session_down():
            with self.lock:
                if self.accepting and generation == self.generation:
                    self.down.set()
        sc.bind_session_down(self.api, session_down)
        self.down.clear()
        self.accepting = True
        for contract in self.contracts.values():
            self.api.quote.subscribe(contract, quote_type=self.sdk.QuoteType.Tick, version=self.sdk.QuoteVersion.v1)
        self.connected_at, self.connected_mono = pd.Timestamp(self.clock()), self.monotonic()
        self.last_fresh.clear()
        self.last_received.clear()
        self.on_connected()
        self.gate.pending = False  # reset attempts only after fresh stream data
        self.publish(state='waiting_ticks', operation=None, error=None)

    def _disconnect(self):
        with self.lock:
            self.accepting = False
            self.generation += 1
        try:
            if self.context is not None:
                self.context.__exit__(None, None, None)
        finally:
            self.context = self.api = None
            self.contracts.clear()
            self.last_fresh.clear()
            self.last_received.clear()
            self.health.update(stream_fresh=False, quote_times={})
            while True:
                try:
                    self.queue.get_nowait()
                except Empty:
                    break
            self.down.clear()
            self.overflow.clear()
            self.on_disconnected()

    def on_connected(self):
        pass

    def on_disconnected(self):
        pass

    def on_tick(self, tick):
        raise NotImplementedError

    def flush(self, now, calendar, *, force=False):
        raise NotImplementedError

    def work(self, now, calendar):
        pass

    def step(self):
        self.start()
        now = pd.Timestamp(self.clock())
        calendar = self.calendar or mh.SessionCalendar.load(self.settings.calendar_path)
        window = self.schedule.window(now, calendar)
        try:
            if self.api is not None and window != self._window:
                # Flush a final closed minute before ending this session, including sleep/resume.
                self._drain()
                try:
                    self.flush(now, calendar, force=True)
                finally:
                    self._disconnect()
            if window is None:
                if self.gate.reason != 'rate_limit' or self.gate.due:
                    self.gate.success()
                self.publish(state='scheduled_closed' if calendar.covered(now) else 'calendar_unavailable',
                             operation=None, error=None, next_window=None)
                return
            if self.down.is_set() or self.overflow.is_set():
                reason = 'queue_overflow' if self.overflow.is_set() else 'session_down'
                if self.overflow.is_set():
                    self.health['queue_overflows'] += 1
                self._disconnect()
                self.gate.fail(reason, immediate=True)
            if self.gate.pending:
                if not self.gate.due:
                    self.publish(state='retry_wait', operation=None)
                    return
                self.gate.attempted()
                self._disconnect()
            if self.api is None:
                self._window = window
                try:
                    self._open()
                except Exception:
                    # A partial subscription setup is not a usable connection.
                    self._disconnect()
                    raise
            self._drain()
            self.flush(pd.Timestamp(self.clock()), calendar)
            # Do not begin backfill if login/subscription crossed the scheduled cutoff.
            if self.schedule.window(self.clock(), calendar) != window:
                self._disconnect()
                self.publish(state='scheduled_closed', operation=None)
                return
            self.work(pd.Timestamp(self.clock()), calendar)
            now = pd.Timestamp(self.clock())
            if self.schedule.window(now, calendar) != window:
                self._drain()
                self.flush(now, calendar, force=True)
                self._disconnect()
                self.publish(state='scheduled_closed', operation=None)
                return
            active = calendar.active(now)
            quiet = [code for code in self.codes
                     if self.monotonic() - self.last_fresh.get(code, self.connected_mono)
                     >= self.settings.stream_recovery_seconds]
            fresh = bool(self.last_fresh) and all(
                code in self.last_fresh and self.monotonic() - self.last_fresh[code] <= self.settings.tick_stale_seconds
                and -5 <= (now - self.last_received[code]).total_seconds() <= self.settings.tick_stale_seconds
                for code in self.codes)
            # No trade is not evidence of a broken session. Keep stale data
            # visible, but reconnect only on session-down or an actual API error.
            if fresh:
                self.gate.success()
            self.publish(state='running' if fresh else 'waiting_ticks' if not active else 'stale',
                         operation=None, error=None, stream_fresh=fresh,
                         quote_times={code: str(at) for code, at in self.last_received.items()},
                         quiet_codes=quiet,
                         window_end=str(window[1]))
        except Exception as error:
            kind = error_kind(error)
            if self.health.get('operation') == 'kbars':
                self.health['history_error'] = {'kind': kind, 'type': type(error).__name__}
            if kind == 'rate_limit':
                self.gate.fail(kind, immediate=True)
                # Stop all work on this session during the ban, including K-bar
                # repair queries in the other subclass of this runtime.
                self._disconnect()
            elif kind != 'database' and (kind in {'transport', 'session', 'authentication'} or self.api is None):
                self.gate.fail(kind, immediate=True)
            self.publish(state='failed', operation=None, error={'kind': kind, 'type': type(error).__name__})
            # Do not log exception text, which can contain authentication material.
            LOG.error('role=%s operation=step kind=%s error_type=%s', self.role, kind, type(error).__name__)

    def _drain(self):
        for _ in range(self.settings.queue_capacity):
            try:
                tick = self.queue.get_nowait()
            except Empty:
                break
            self.on_tick(tick)
            previous = self.last_received.get(tick.code)
            if previous is None or tick.at >= previous:
                self.last_received[tick.code] = tick.at
                if -5 <= (tick.received_at - tick.at).total_seconds() <= self.settings.tick_stale_seconds:
                    self.last_fresh[tick.code] = self.monotonic() - max(
                        0, (pd.Timestamp(self.clock()) - tick.received_at).total_seconds())

    def close(self):
        try:
            if self.started:
                # Persist pending output, but don't perform historical network I/O on shutdown.
                try:
                    self._drain()
                    self.flush(pd.Timestamp(self.clock()), self.calendar or mh.SessionCalendar.load(self.settings.calendar_path),
                               force=True)
                finally:
                    self._disconnect()
        finally:
            if self.started:
                try:
                    self.publish(state='stopped', operation=None)
                finally:
                    self.store.close()
                    self.store = None
                    self.started = False
                    self.lease.close()


def run_cli(runtime_class, argv=None):
    parser = argparse.ArgumentParser(description=runtime_class.__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data' / 'market_fetchers.db')
    parser.add_argument('--codes', nargs='+', default=['TMFR1'])
    parser.add_argument('--mode', choices=('simulation', 'production'), required=True)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--once', action='store_true', help='one lifecycle step, then logout')
    if runtime_class.role == 'kbar':
        parser.add_argument('--no-backfill', action='store_true', help='stream only; no historical repair queries')
    args = parser.parse_args(argv)
    settings = FetcherSettings.load(args.config)
    log_dir = args.db.resolve().parent / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(log_dir / f'{runtime_class.role}_fetcher.log', when='midnight',
                                       backupCount=14, encoding='utf-8', utc=True)
    formatter = logging.Formatter('%(asctime)sZ %(levelname)s %(message)s')
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    stop = threading.Event()
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())
    options = {'backfill_enabled': not args.no_backfill} if runtime_class.role == 'kbar' else {}
    runtime = runtime_class(args.db, args.codes, mode=args.mode, settings=settings, **options)
    try:
        while not stop.is_set():
            runtime.step()
            if args.once:
                break
            stop.wait(0.2)
    finally:
        try:
            runtime.close()
        finally:
            LOG.removeHandler(handler)
            handler.close()
    return 0
