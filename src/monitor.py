"""Unify historical and realtime fetcher data, derive indicators and audit sessions.

No broker login or market-data network calls. Run in conda env tss.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alert_rules as ar
import db
import market_clock
from service_control import ServiceControl
from runtime_config import RuntimeConfig, DEFAULT_RUNTIME, skip_mock
import patterns as pt
import session_bars as sb
import snr_core as snr
import telegram_notify as tg
import market_health as mh
from connection_recovery import error_kind
from box_watch import BoxWatcher, BoxWatchConfig

SIDES = ("support", "resistance")
# How much minute history to feed the indicator each cycle.  The boxes need
# 2*lookback = 40 hourly bars minimum (~2.2 trading days); 90 calendar days keeps
# a useful box history without making the per-cycle recompute expensive.
INDICATOR_WINDOW_DAYS = 90


@dataclass
class MonitorConfig:
    code: str = "TMFR1"
    db_path: Path = db.DEFAULT_DB_PATH
    fetcher_db: Path = db.PROJECT_ROOT / 'data' / 'market_fetchers.db'
    mode: str = 'simulation'
    poll_seconds: int = 1
    backfill_days: int = 60
    lookback: int = snr.DEFAULT_LOOKBACK
    volume_length: int = snr.DEFAULT_VOLUME_LENGTH
    atr_length: int = snr.DEFAULT_ATR_LENGTH
    box_width: float = snr.DEFAULT_BOX_WIDTH
    orderbook_max_hz: float = 5.0
    exit_buffer_atr: float = 0.1
    enable_telegram: bool = True
    enable_orderbook: bool = False
    once: bool = False
    idle_sleep_seconds: int = 300
    patterns: pt.PatternConfig = field(default_factory=pt.PatternConfig)
    alerts_config: Path = ar.DEFAULT_CONFIG_PATH
    health_config: Path = mh.DEFAULT_CONFIG
    api_timeout_ms: int = 30000


def log(message: str) -> None:
    print(f"[{market_clock.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# --------------------------------------------------------------------------------------
# Indicator state for one cycle
# --------------------------------------------------------------------------------------
@dataclass
class MarketState:
    minute_bars: pd.DataFrame
    hourly: pd.DataFrame
    boxes: pd.DataFrame
    closed_through: int
    hourly_atr: float | None
    latest_minute: pd.Series | None
    forming_hour: pd.Series | None
    prev_closed_hour: pd.Series | None

    @property
    def hour_start(self):
        if self.forming_hour is not None:
            return self.forming_hour["hour_start"]
        if not self.hourly.empty:
            return self.hourly.iloc[-1]["hour_start"]
        return None


def last_closed_index(hourly: pd.DataFrame, now: pd.Timestamp) -> int:
    """Index of the newest *finished* hourly bar, or -1.

    The final row is only still forming when the clock agrees it is: outside a
    session (or on a holiday) every bar on file is done.
    """
    if hourly.empty:
        return -1
    current = sb.hour_start_of(now)
    last_hour = pd.Timestamp(hourly.iloc[-1]["hour_start"])
    if current is not None and last_hour == current:
        return len(hourly) - 2
    return len(hourly) - 1


def build_state(conn, config: MonitorConfig, now: pd.Timestamp) -> MarketState:
    since = (now - pd.Timedelta(days=INDICATOR_WINDOW_DAYS)).strftime("%Y-%m-%d 00:00:00")
    minute_bars = db.load_minute_bars(conn, config.code, since=since)
    if minute_bars.empty:
        return MarketState(minute_bars, pd.DataFrame(), pd.DataFrame(), -1, None, None, None, None)

    hourly = sb.minute_bars_to_hourly(minute_bars)
    closed_through = last_closed_index(hourly, now)

    boxes = pd.DataFrame(columns=snr.BOX_COLUMNS)
    atr_value: float | None = None
    if len(hourly) > 2 * config.lookback:
        boxes = snr.collect_snr_boxes(
            hourly,
            lookback=config.lookback,
            volume_length=config.volume_length,
            atr_length=config.atr_length,
            box_width=config.box_width,
            creation_limit=closed_through if closed_through >= 0 else None,
            overrides=db.load_box_overrides(conn, config.code),
        )
        atr_series = snr.pine_atr(hourly, config.atr_length)
        anchor = closed_through if closed_through >= 0 else len(hourly) - 1
        raw = atr_series.iloc[anchor]
        atr_value = float(raw) if pd.notna(raw) else None

    forming = hourly.iloc[-1] if closed_through == len(hourly) - 2 else None
    prev_closed = hourly.iloc[closed_through] if closed_through >= 0 else None

    return MarketState(
        minute_bars=minute_bars,
        hourly=hourly,
        boxes=boxes,
        closed_through=closed_through,
        hourly_atr=atr_value,
        latest_minute=minute_bars.iloc[-1],
        forming_hour=forming,
        prev_closed_hour=prev_closed,
    )


# --------------------------------------------------------------------------------------
# Order-book persistence
# --------------------------------------------------------------------------------------
class BookRecorder:
    """No broker fallback; bidask persistence awaits fetcher integration."""
    def __init__(self, api, contract, config):
        self.live = None
    def flush(self, conn, visit_id, phase='in_box'):
        return 0
    def forget(self, visit_id):
        pass
    def record_at_reversal(self, conn, visit_id, reversal_id):
        pass
    def stop(self):
        pass


def send_rule(conn, config: MonitorConfig, alerts: ar.AlertConfig, candidate: ar.Candidate, minute_start) -> bool:
    """Send one rule hit unless it is inside its cooldown or already sent once.

    Both checks read the alert rows back, measured on the triggering minute
    rather than the wall clock, so a restart or a replay throttles the same way.
    The config master switch pauses alerts without claiming their history.
    """
    if not alerts.telegram_enabled:
        return False
    last = db.last_alert_at(conn, config.code, candidate.key, candidate.rule)
    if last is not None:
        if candidate.once:
            return False
        cooldown = pd.Timedelta(minutes=alerts.cooldown_minutes(candidate.rule))
        if pd.Timestamp(minute_start) - last < cooldown:
            return False
    alert_id = db.record_alert(
        conn,
        config.code,
        hour_start=minute_start,
        box_type=candidate.key,
        box_id=None,
        visit_id=None,
        kind=candidate.rule,
        message=candidate.message,
    )
    if alert_id is None:
        return False  # this minute is already claimed -- a re-polled cycle
    if not config.enable_telegram:
        db.mark_alert_sent(conn, alert_id, False)
        log(f"  [telegram disabled] would send {candidate.rule} ({candidate.key})")
        return False
    ok = tg.send_message(candidate.message)
    db.mark_alert_sent(conn, alert_id, ok)
    log(f"  telegram {candidate.rule} ({candidate.key}): {'sent' if ok else 'FAILED'}")
    return ok


def check_alerts(conn, config: MonitorConfig, alerts: ar.AlertConfig, state: "MarketState") -> int:
    """Evaluate every rule on the newest minute and send what is owed."""
    if alerts.reload():
        log(f"  alert config loaded from {alerts.path}")
    if alerts.last_error:
        log(f"  WARNING alert config: {alerts.last_error}")
    if not alerts.telegram_enabled:
        return 0
    minute = state.latest_minute
    if minute is None:
        return 0
    bars = state.minute_bars
    candidates = ar.evaluate(
        code=config.code,
        minute_bar=minute,
        boxes={side: snr.current_box(state.boxes, side) for side in SIDES},
        prev_minute_bar=bars.iloc[-2] if len(bars) >= 2 else None,
        hourly=state.hourly,
        trendlines=db.load_trendlines(conn, config.code),
        hourly_atr=state.hourly_atr,
        config=alerts,
    )
    return sum(
        send_rule(conn, config, alerts, candidate, minute["minute_start"])
        for candidate in candidates
    )


# --------------------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------------------
def handle_side(
    conn,
    config: MonitorConfig,
    state: MarketState,
    watcher: BoxWatcher,
    recorder: BookRecorder,
) -> list:
    """Step one side; returns ``[(exit transition, box it left)]``."""
    box = snr.current_box(state.boxes, watcher.box_type)
    minute = state.latest_minute
    if minute is None:
        return []

    transition = watcher.step(minute, box, state.hourly_atr)
    if transition is None:
        return []

    exits = []
    for step in (transition, transition.follow_up):
        if step is None:
            continue
        if step.kind == "exit":
            _on_exit(conn, config, state, watcher, recorder, step)
            exits.append((step, box))
        elif step.kind == "enter":
            _on_enter(conn, config, state, watcher, recorder, step, box)
        elif step.kind == "stay":
            _on_stay(conn, config, state, watcher, recorder, step, box)
    return exits


def _on_enter(conn, config, state, watcher, recorder, step, box) -> None:
    visit_id = db.open_visit(
        conn,
        config.code,
        box_id=step.box_id,
        box_type=step.box_type,
        box_top=float(box["top"]),
        box_bottom=float(box["bottom"]),
        box_state=str(box["state"]),
        entered_at=market_clock.now(),
        entry_minute_start=step.minute_start,
        entry_price=step.price,
        entry_zone_pct=step.zone_pct,
    )
    watcher.visit_id = visit_id
    log(
        f"  ENTER {step.box_type} box #{step.box_id} at {step.price:,.0f} "
        f"(zone {step.zone_pct * 100:.0f}%) -> visit {visit_id}"
    )

    written = recorder.flush(conn, visit_id, phase="pre_entry")
    log(f"  book rows written (pre_entry): {written}")


def _on_stay(conn, config, state, watcher, recorder, step, box) -> None:
    if watcher.visit_id is None:
        return
    db.touch_visit(
        conn,
        watcher.visit_id,
        price=step.price,
        zone_pct_value=step.zone_pct,
        minutes_in_box=watcher.minutes_in_box,
    )
    recorder.flush(conn, watcher.visit_id, phase="in_box")


def _on_exit(conn, config, state, watcher, recorder, step) -> None:
    # The watcher has already reset itself, so the visit id rides on the transition.
    visit_id = step.visit_id
    if visit_id is None:
        return
    recorder.flush(conn, visit_id, phase="in_box")  # final flush before we stop
    db.close_visit(
        conn,
        visit_id,
        exited_at=market_clock.now(),
        exit_minute_start=step.minute_start,
        exit_price=step.price,
        exit_reason=step.exit_reason or "left_box",
    )
    recorder.forget(visit_id)
    log(f"  EXIT {step.box_type} visit {visit_id} ({step.exit_reason})")


def detect_reversals(
    conn, config: MonitorConfig, state: MarketState, watchers: dict, recorder: BookRecorder
) -> None:
    """Hourly reversal patterns on the forming bar, for whichever side is occupied."""
    bar = state.forming_hour
    if bar is None or state.prev_closed_hour is None:
        return

    minutes_into_bar = int(bar.get("minutes", 0) or 0)
    for box_type, watcher in watchers.items():
        if not watcher.inside or watcher.visit_id is None:
            continue
        box = snr.current_box(state.boxes, box_type)
        if box is None:
            continue

        hits = pt.detect(
            bar,
            state.prev_closed_hour,
            box_type=box_type,
            hourly_atr=state.hourly_atr,
            config=config.patterns,
        )
        for hit in hits:
            if db.reversal_exists(
                conn, config.code, bar["hour_start"], hit["pattern"], minutes_into_bar
            ):
                continue
            price = float(bar["close"])
            zone = snr.zone_pct(price, box)
            reversal_id = db.insert_reversal(
                conn,
                config.code,
                {
                    "visit_id": watcher.visit_id,
                    "detected_at": market_clock.now(),
                    "hour_start": bar["hour_start"],
                    "bar_closed": False,
                    "minutes_into_bar": minutes_into_bar,
                    "pattern": hit["pattern"],
                    "direction": hit["direction"],
                    "box_id": int(box["box_id"]),
                    "box_type": box_type,
                    "box_state": str(box["state"]),
                    "box_top": float(box["top"]),
                    "box_bottom": float(box["bottom"]),
                    "zone_pct": zone,
                    "bar_open": float(bar["open"]),
                    "bar_high": float(bar["high"]),
                    "bar_low": float(bar["low"]),
                    "bar_close": price,
                    "bar_volume": float(bar["volume"]),
                    "prev_open": float(state.prev_closed_hour["open"]),
                    "prev_high": float(state.prev_closed_hour["high"]),
                    "prev_low": float(state.prev_closed_hour["low"]),
                    "prev_close": float(state.prev_closed_hour["close"]),
                    "upper_wick": hit["upper_wick"],
                    "lower_wick": hit["lower_wick"],
                    "body": hit["body"],
                    "close_pos": hit["close_pos"],
                    "range_atr": hit["range_atr"],
                    "hourly_atr": state.hourly_atr,
                    "detail": hit["detail"],
                },
            )
            db.bump_visit_counter(conn, watcher.visit_id, "reversal_count", 1)
            recorder.record_at_reversal(conn, watcher.visit_id, reversal_id)
            log(f"  REVERSAL {hit['pattern']} ({box_type}) -> id {reversal_id}")


def run_cycle(
    api, contract, conn, config: MonitorConfig, watchers: dict, recorder: BookRecorder,
    alerts: ar.AlertConfig, *, fresh=None, allow_signals=True,
) -> MarketState:
    now = market_clock.now()

    if fresh is None:
        fresh = pd.DataFrame()
    stored = db.upsert_minute_bars(conn, config.code, fresh)

    source_revision = db.chart_versions(conn, config.code)[0]
    state = build_state(conn, config, now)
    if state.hourly.empty:
        log(f"no bars yet (upserted {stored}); nothing to do")
        return state

    db.upsert_hourly_bars(conn, config.code, state.hourly, state.closed_through)
    db.replace_boxes(conn, config.code, state.boxes)
    db.mark_dashboard_derived(conn, config.code, source_revision, state.minute_bars, state.hourly)

    support = snr.current_box(state.boxes, "support")
    resistance = snr.current_box(state.boxes, "resistance")
    last_price = float(state.latest_minute["close"])
    log(
        f"minutes+{stored} bars={len(state.hourly)} boxes={len(state.boxes)} "
        f"last={state.latest_minute['minute_start']} price={last_price:,.0f} "
        f"atr={state.hourly_atr if state.hourly_atr is None else round(state.hourly_atr, 1)} "
        f"sup={'none' if support is None else f'#{int(support.box_id)} {support.bottom:,.0f}-{support.top:,.0f}'} "
        f"res={'none' if resistance is None else f'#{int(resistance.box_id)} {resistance.bottom:,.0f}-{resistance.top:,.0f}'}"
    )

    if allow_signals:
        for box_type in SIDES:
            handle_side(conn, config, state, watchers[box_type], recorder)
        detect_reversals(conn, config, state, watchers, recorder)
        check_alerts(conn, config, alerts, state)
    return state


# --------------------------------------------------------------------------------------
# Startup / loop
# --------------------------------------------------------------------------------------
def restore_watchers(conn, config: MonitorConfig) -> dict:
    watchers = {
        side: BoxWatcher(side, BoxWatchConfig(exit_buffer_atr=config.exit_buffer_atr))
        for side in SIDES
    }
    for side, watcher in watchers.items():
        row = db.get_open_visit(conn, config.code, side)
        if row is not None:
            watcher.restore(row["box_id"], row["id"], row["minutes_in_box"])
            log(f"restored open {side} visit {row['id']} (box #{row['box_id']})")
    return watchers


def shutdown(conn, config: MonitorConfig, watchers: dict, recorder: BookRecorder) -> None:
    for watcher in watchers.values():
        if watcher.visit_id is not None:
            recorder.flush(conn, watcher.visit_id, phase="in_box")
            db.close_visit(
                conn,
                watcher.visit_id,
                exited_at=market_clock.now(),
                exit_minute_start=watcher.last_minute_start,
                exit_price=None,
                exit_reason="shutdown",
            )
            log(f"closed {watcher.box_type} visit {watcher.visit_id} on shutdown")
    recorder.stop()


class MonitorRuntime:
    """Unify fetcher data and audit events without a broker connection."""
    def __init__(self, conn, config, *, settings=None, calendar=None,
                 clock=market_clock.now, monotonic=time.monotonic, publisher=None):
        self.conn, self.config = conn, config
        self.settings = settings or mh.load_settings(config.health_config)
        self.calendar = calendar
        self.clock, self.monotonic = clock, monotonic
        self.publisher = publisher or mh.HealthPublisher(
            config.db_path, 'monitor', config.code, config.mode, clock=clock)
        self.recorder = BookRecorder(None, None, config)
        self.watchers = restore_watchers(conn, config)
        self.alerts = ar.AlertConfig(config.alerts_config)
        self.since = (db.last_minute_start(conn, config.code) or
                      pd.Timestamp(clock()).normalize() - pd.Timedelta(days=config.backfill_days))
        self.last_fetch = float('-inf')
        self.last_frame = None
        self.last_state = None
        self.ready_since = None
        self.audit_path = Path(config.db_path).resolve().parent / 'logs' / 'monitor.log'
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS monitor_fetch_events (
                id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, role TEXT NOT NULL,
                mode TEXT NOT NULL, event TEXT NOT NULL, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS monitor_event_offsets (path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS monitor_bar_sources (
                code TEXT NOT NULL, minute_start TEXT NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY(code, minute_start));
            CREATE TABLE IF NOT EXISTS monitor_validation_origins (
                code TEXT PRIMARY KEY, since TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS monitor_source_binding (
                code TEXT PRIMARY KEY, mode TEXT NOT NULL, source_db TEXT NOT NULL);
        ''')
        source_path = str(Path(config.fetcher_db).resolve())
        binding = conn.execute('SELECT mode, source_db FROM monitor_source_binding WHERE code=?', (config.code,)).fetchone()
        if binding and tuple(binding) != (config.mode, source_path):
            raise ValueError('monitor DB is bound to another mode/source; use a separate --db')
        conn.execute('INSERT OR IGNORE INTO monitor_source_binding VALUES (?,?,?)',
                     (config.code, config.mode, source_path))
        # The unified DB also exposes the latest per-contract quote snapshots.
        from fetcher_storage import SCHEMA
        conn.executescript(SCHEMA)
        self.origin_pending = (db.last_minute_start(conn, config.code) is None and
                               conn.execute('SELECT 1 FROM monitor_validation_origins WHERE code=?',
                                            (config.code,)).fetchone() is None)
        if not self.origin_pending:
            conn.execute('INSERT OR IGNORE INTO monitor_validation_origins VALUES (?,?)',
                         (config.code, mh.iso(self.since)))
            self.since = pd.Timestamp(conn.execute('SELECT since FROM monitor_validation_origins WHERE code=?',
                                                  (config.code,)).fetchone()[0])
        conn.commit()
        self.publisher.acquire()
        try:
            self.control = ServiceControl(config.db_path, f'monitor-{config.code}', config.mode).start()
        except BaseException:
            self.publisher.close()
            raise

    def _audit_events(self):
        import json
        folder = Path(self.config.fetcher_db).resolve().parent / 'logs'
        for role in ('tick', 'kbar', 'trade'):
            event_folder = Path(self.config.db_path).resolve().parent / 'logs' if role == 'trade' else folder
            path = event_folder / f'{role}_events.jsonl'
            if not path.exists():
                continue
            key = str(path)
            saved = self.conn.execute('SELECT offset FROM monitor_event_offsets WHERE path=?', (key,)).fetchone()
            offset = saved[0] if saved else 0
            records = []
            with path.open('rb') as stream:
                if path.stat().st_size < offset:
                    offset = 0
                stream.seek(offset)
                for _ in range(10000):
                    line = stream.readline()
                    if not line or not line.endswith(b'\n'):
                        break  # writer may be midway through appending this event
                    record = json.loads(line)
                    if record['mode'] == self.config.mode:
                        inserted = self.conn.execute('INSERT OR IGNORE INTO monitor_fetch_events VALUES (?,?,?,?,?,?)',
                            (record['id'], record['timestamp'], record['role'], record['mode'],
                             record['event'], json.dumps(record, ensure_ascii=False))).rowcount
                        if inserted:
                            records.append(record)
                    offset = stream.tell()
            self.conn.execute('INSERT OR REPLACE INTO monitor_event_offsets VALUES (?,?)', (key, offset))
            self.conn.commit()
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open('a', encoding='utf-8') as output:
                for record in records:
                    text = json.dumps(record, ensure_ascii=False)
                    output.write(text + '\n')
                    log(text)

    def _read_source(self, now):
        import sqlite3
        from contextlib import closing
        path = Path(self.config.fetcher_db).resolve()
        # No creation of a missing source and no broker fallback.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
            source.row_factory = sqlite3.Row
            source.execute('BEGIN')
            origin = source.execute("SELECT validation_since FROM market_fetcher_progress WHERE role='kbar' AND code=? AND mode=?",
                                    (self.config.code, self.config.mode)).fetchone()
            if self.origin_pending and origin:
                self.since = pd.Timestamp(origin[0])
                self.conn.execute('INSERT OR REPLACE INTO monitor_validation_origins VALUES (?,?)',
                                  (self.config.code, mh.iso(self.since)))
                self.conn.commit()
                self.origin_pending = False
            records = source.execute('''SELECT * FROM market_stream_bars
                WHERE code=? AND mode=? AND minute_start>=? ORDER BY minute_start, source''',
                (self.config.code, self.config.mode, mh.iso(self.since))).fetchall()
            quotes = source.execute('SELECT * FROM market_latest_quotes WHERE mode=?',
                                    (self.config.mode,)).fetchall()
        frame = pd.DataFrame([dict(row) for row in records])
        if not frame.empty:
            frame['minute_start'] = pd.to_datetime(frame['minute_start'])
            frame = frame[(frame['complete'] == 1) &
                          (frame['minute_start'] + pd.Timedelta(minutes=1) <= now)]
            # Broker revisions always supersede local tick aggregates.
            frame = frame.sort_values('source', ascending=False).drop_duplicates('minute_start', keep='last')
            frame = frame.sort_values('minute_start')
        return frame, quotes

    def step(self):
        now = pd.Timestamp(self.clock())
        try:
            self._audit_events()  # audit survives even when the acquisition DB is unavailable
            if self.monotonic() - self.last_fetch < self.config.poll_seconds:
                return
            calendar = self.calendar or mh.SessionCalendar.load(self.settings.calendar_path)
            fresh, quotes = self._read_source(now)
            self.publisher.publish(state='checking', operation='derive', operation_started=mh.iso(now),
                                   validation_pending=True)
            # Remove previously imported candidates invalidated by a late tick or reconnect.
            valid = set() if fresh.empty else set(fresh['minute_start'].map(mh.iso))
            imported = self.conn.execute('SELECT minute_start FROM monitor_bar_sources WHERE code=? AND source=\'stream\'',
                                         (self.config.code,)).fetchall()
            for row in imported:
                if row[0] not in valid:
                    self.conn.execute('DELETE FROM minute_bars WHERE code=? AND minute_start=?',
                                      (self.config.code, row[0]))
                    self.conn.execute('DELETE FROM monitor_bar_sources WHERE code=? AND minute_start=?',
                                      (self.config.code, row[0]))
            if not fresh.empty:
                db.upsert_minute_bars(self.conn, self.config.code, sb.add_sessions(fresh), commit=False)
                self.conn.executemany('INSERT OR REPLACE INTO monitor_bar_sources VALUES (?,?,?)',
                    [(self.config.code, mh.iso(row.minute_start), row.source) for row in fresh.itertuples()])
            self.conn.executemany('''INSERT OR REPLACE INTO market_latest_quotes
                VALUES (?,?,?,?,?,?,?,?)''', [tuple(row) for row in quotes])
            self.conn.commit()
            bars = db.load_minute_bars(self.conn, self.config.code, since=mh.iso(self.since))
            validation = mh.validate_bars(bars, now, calendar, since=self.since,
                                           lag_seconds=self.settings.bar_lag_seconds)
            unverified = self.conn.execute("SELECT COUNT(*) FROM monitor_bar_sources WHERE code=? AND source='stream'",
                                           (self.config.code,)).fetchone()[0]
            ready = validation['ready'] and unverified == 0
            if self.last_frame is None or not fresh.equals(self.last_frame):
                self.last_state = run_cycle(None, None, self.conn, self.config, self.watchers, self.recorder,
                                           self.alerts, fresh=pd.DataFrame(), allow_signals=ready)
                self.last_frame = fresh.copy()
            state = self.last_state
            self.last_fetch = self.monotonic()
            self.ready_since = (self.ready_since or mh.iso(now)) if ready else None
            self.publisher.publish(state='healthy' if ready else 'degraded', operation=None, error=None,
                bars=validation, validation_pending=not ready, staged_only=bool(unverified),
                unverified_stream_minutes=unverified, ready_since=self.ready_since,
                fetcher_db=str(Path(self.config.fetcher_db).resolve()), cycle_completed_at=mh.iso(self.clock()),
                book={'required': False, 'fresh': None, 'error': 'fetcher bidask integration pending'})
            return state
        except Exception as error:
            self.conn.rollback()
            self.publisher.publish(state='failed', operation=None, validation_pending=True,
                                   error={'kind': error_kind(error), 'type': type(error).__name__})
            log(f'fetch monitor failed: {error_kind(error)}: {type(error).__name__}')

    def close(self):
        try:
            self._audit_events()
        finally:
            try:
                self.control.close()
            finally:
                self.publisher.close()

def parse_args(argv: list[str] | None = None) -> MonitorConfig:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--code", default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=int, default=1)
    parser.add_argument("--fetcher-db", type=Path, default=None)
    parser.add_argument("--backfill-days", type=int, default=60)
    parser.add_argument("--lookback", type=int, default=snr.DEFAULT_LOOKBACK)
    parser.add_argument("--volume-length", type=int, default=snr.DEFAULT_VOLUME_LENGTH)
    parser.add_argument("--atr-length", type=int, default=snr.DEFAULT_ATR_LENGTH)
    parser.add_argument("--box-width", type=float, default=snr.DEFAULT_BOX_WIDTH)
    parser.add_argument(
        "--orderbook-max-hz",
        type=float,
        default=5.0,
        help="cap book rows per second (0 = keep every update)",
    )
    parser.add_argument(
        "--exit-buffer-atr",
        type=float,
        default=0.1,
        help=(
            "how far past a box edge price must go to end a visit, in hourly ATR; "
            "0 ends it the instant the bar clears the edge, which flaps"
        ),
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="disable Telegram sending regardless of telegram_enabled in the alerts config",
    )
    parser.add_argument("--no-orderbook", action="store_true")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--health-config", type=Path, default=None)
    parser.add_argument("--min-range-atr", type=float, default=pt.PatternConfig.min_range_atr)
    parser.add_argument("--wick-body-ratio", type=float, default=pt.PatternConfig.wick_body_ratio)
    parser.add_argument(
        "--alerts-config",
        type=Path,
        default=ar.DEFAULT_CONFIG_PATH,
        help="JSON file with the Telegram rule switches and thresholds",
    )
    args = parser.parse_args(argv)
    runtime_config = RuntimeConfig.load(args.runtime_config)

    return MonitorConfig(
        code=args.code or runtime_config.signal_code,
        db_path=args.db or runtime_config.unified_db,
        fetcher_db=args.fetcher_db or runtime_config.acquisition_db,
        mode=runtime_config.mode,
        poll_seconds=args.poll_seconds,
        backfill_days=args.backfill_days,
        lookback=args.lookback,
        volume_length=args.volume_length,
        atr_length=args.atr_length,
        box_width=args.box_width,
        orderbook_max_hz=args.orderbook_max_hz,
        exit_buffer_atr=args.exit_buffer_atr,
        enable_telegram=not args.no_telegram,
        enable_orderbook=False,
        once=args.once,
        patterns=pt.PatternConfig(
            min_range_atr=args.min_range_atr, wick_body_ratio=args.wick_body_ratio
        ),
        alerts_config=args.alerts_config,
        health_config=args.health_config or runtime_config.health_config,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    if skip_mock(config.mode):
        return 0
    stopping = {"flag": False}

    def _handle_signal(signum, _frame):
        log(f"signal {signum} received, finishing current cycle")
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    stop_event = threading.Event()
    with db.session(config.db_path) as conn:
        runtime = MonitorRuntime(conn, config)
        try:
            while True:
                started = time.monotonic()
                runtime.step()
                if config.once or stopping["flag"] or runtime.control.stop_requested:
                    break
                elapsed = time.monotonic() - started
                # Short waits keep termination responsive even during backoff.
                until = time.monotonic() + max(1, min(config.poll_seconds, 60) - elapsed)
                while not stopping["flag"] and not runtime.control.stop_requested and time.monotonic() < until:
                    stop_event.wait(min(1, until - time.monotonic()))
        finally:
            runtime.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
