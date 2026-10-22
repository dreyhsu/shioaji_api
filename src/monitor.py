"""The once-a-minute loop: update prices, track boxes, record books, notify.

One cycle per minute:

1. Pull today's and yesterday's minute bars (``kbars`` slices by calendar day, and
   a night session starts at 15:00 the previous day, so "today" alone would miss a
   running night session) and upsert them.
2. Rebuild session-anchored hourly bars and the Pine box table.  The last hourly
   bar is normally still forming, so ``creation_limit`` stops it creating boxes
   while still letting it break or hold existing ones -- what Pine does.
3. Step each side's :class:`box_watch.BoxWatcher` with the newest **1-minute** bar.
   Entering a box opens a visit and starts persisting the order book (plus the
   buffered run-up as ``pre_entry``); staying keeps flushing the book; leaving
   closes the visit and stops persisting.
4. While inside, look for hourly reversal patterns on the forming bar and log any
   hit together with the book snapshot at that moment.
5. Check the Telegram rules in ``alert_rules`` (configured by
   ``config/alerts.json``) against the newest minute close, and send what has
   not been sent within its cooldown.

Everything that could repeat is idempotent at the database level, so a retried or
restarted cycle cannot double-write books or double-send alerts.  A failing cycle
is logged and skipped -- the loop is meant to survive a bad night, not to be
correct about every single minute.

Run inside the ``tss`` conda env::

    /opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python \
        src/monitor.py --code TMFR1 --backfill-days 60
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alert_rules as ar
import db
import market_clock
import orderbook as ob
import patterns as pt
import session_bars as sb
import shioaji_client as sc
import snr_core as snr
import telegram_notify as tg
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
    poll_seconds: int = 60
    backfill_days: int = 60
    lookback: int = snr.DEFAULT_LOOKBACK
    volume_length: int = snr.DEFAULT_VOLUME_LENGTH
    atr_length: int = snr.DEFAULT_ATR_LENGTH
    box_width: float = snr.DEFAULT_BOX_WIDTH
    orderbook_max_hz: float = 5.0
    exit_buffer_atr: float = 0.1
    enable_telegram: bool = True
    enable_orderbook: bool = True
    once: bool = False
    idle_sleep_seconds: int = 300
    patterns: pt.PatternConfig = field(default_factory=pt.PatternConfig)
    alerts_config: Path = ar.DEFAULT_CONFIG_PATH


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
    """Decides what book rows to persist for an open visit."""

    def __init__(self, api, contract, config: MonitorConfig) -> None:
        self.config = config
        self.live = ob.LiveBook(api, contract) if config.enable_orderbook else None
        self.api = api
        self.contract = contract
        # Per-visit watermark so each flush only writes what is new.
        self._watermark: dict[int, pd.Timestamp] = {}

    def start(self) -> str:
        if self.live is None:
            return "disabled"
        return "subscribed" if self.live.start() else f"failed ({self.live.last_error})"

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()

    @property
    def source(self) -> str | None:
        if self.live is None:
            return None
        return "bidask_v1" if self.live.alive else "snapshot"

    @property
    def levels(self) -> int | None:
        if self.live is None:
            return None
        latest = self.live.buffer.latest()
        return latest["levels"] if latest else 1

    def latest_row(self) -> dict | None:
        """The freshest book we have, falling back to a one-level snapshot."""
        if self.live is not None and self.live.alive:
            return self.live.buffer.latest()
        try:
            return ob.snapshot_row(self.api, self.contract)
        except Exception as error:  # noqa: BLE001 - book capture is best-effort
            log(f"  snapshot fallback failed: {type(error).__name__}: {error}")
            return None

    def flush(self, conn, visit_id: int, phase: str = "in_box") -> int:
        """Persist new book rows for ``visit_id``; returns how many were new."""
        if self.live is not None and self.live.alive:
            rows = self.live.buffer.since(self._watermark.get(visit_id))
            if phase == "pre_entry" and not self._watermark.get(visit_id):
                rows = self.live.buffer.tail(ob.PRE_ENTRY_ROWS)
            rows = ob.throttle(rows, self.config.orderbook_max_hz)
        else:
            row = self.latest_row()
            rows = [row] if row else []

        if not rows:
            return 0
        self._watermark[visit_id] = pd.Timestamp(rows[-1]["event_ts"])
        for row in rows:
            row["visit_id"] = visit_id
            row.setdefault("phase", phase)
        written = db.insert_orderbook_rows(conn, self.config.code, rows)
        if written:
            db.bump_visit_counter(conn, visit_id, "orderbook_rows", written)
        return written

    def record_at_reversal(self, conn, visit_id: int, reversal_id: int) -> int:
        row = self.latest_row()
        if row is None:
            return 0
        row = dict(row)
        row.update({"visit_id": visit_id, "reversal_id": reversal_id, "phase": "at_reversal"})
        return db.insert_orderbook_rows(conn, self.config.code, [row])

    def forget(self, visit_id: int) -> None:
        self._watermark.pop(visit_id, None)


# --------------------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------------------
def send_rule(conn, config: MonitorConfig, alerts: ar.AlertConfig, candidate: ar.Candidate, minute_start) -> bool:
    """Send one rule hit unless it is inside its cooldown or already sent once.

    Both checks read the alert rows back, measured on the triggering minute
    rather than the wall clock, so a restart or a replay throttles the same way.
    """
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
    alerts: ar.AlertConfig,
) -> MarketState:
    now = market_clock.now()

    fresh = sc.fetch_latest_minute_bars(api, contract)
    stored = db.upsert_minute_bars(conn, config.code, fresh)

    state = build_state(conn, config, now)
    if state.hourly.empty:
        log(f"no bars yet (upserted {stored}); nothing to do")
        return state

    db.upsert_hourly_bars(conn, config.code, state.hourly, state.closed_through)
    db.replace_boxes(conn, config.code, state.boxes)

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

    for box_type in SIDES:
        handle_side(conn, config, state, watchers[box_type], recorder)
    detect_reversals(conn, config, state, watchers, recorder)
    check_alerts(conn, config, alerts, state)
    return state


# --------------------------------------------------------------------------------------
# Startup / loop
# --------------------------------------------------------------------------------------
def backfill(api, contract, conn, config: MonitorConfig) -> int:
    last = db.last_minute_start(conn, config.code)
    today = market_clock.today()
    if last is None:
        start = today - timedelta(days=config.backfill_days)
        log(f"empty store -> backfilling {config.backfill_days} days from {start}")
    else:
        # Re-pull the last day to absorb revisions and the night-session overlap.
        start = last.date() - timedelta(days=1)
        log(f"resuming from {last} -> refetching from {start}")
    bars = sc.fetch_minute_bars(api, contract, start, today, verbose=True)
    written = db.upsert_minute_bars(conn, config.code, bars)
    log(f"backfill upserted {written} minute bars")
    return written


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


def parse_args(argv: list[str] | None = None) -> MonitorConfig:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--poll-seconds", type=int, default=60)
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
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--no-orderbook", action="store_true")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--min-range-atr", type=float, default=pt.PatternConfig.min_range_atr)
    parser.add_argument("--wick-body-ratio", type=float, default=pt.PatternConfig.wick_body_ratio)
    parser.add_argument(
        "--alerts-config",
        type=Path,
        default=ar.DEFAULT_CONFIG_PATH,
        help="JSON file with the Telegram rule switches and thresholds",
    )
    args = parser.parse_args(argv)

    return MonitorConfig(
        code=args.code,
        db_path=args.db,
        poll_seconds=args.poll_seconds,
        backfill_days=args.backfill_days,
        lookback=args.lookback,
        volume_length=args.volume_length,
        atr_length=args.atr_length,
        box_width=args.box_width,
        orderbook_max_hz=args.orderbook_max_hz,
        exit_buffer_atr=args.exit_buffer_atr,
        enable_telegram=not args.no_telegram,
        enable_orderbook=not args.no_orderbook,
        once=args.once,
        patterns=pt.PatternConfig(
            min_range_atr=args.min_range_atr, wick_body_ratio=args.wick_body_ratio
        ),
        alerts_config=args.alerts_config,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    stopping = {"flag": False}

    def _handle_signal(signum, _frame):
        log(f"signal {signum} received, finishing current cycle")
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    with db.session(config.db_path) as conn, sc.connect() as api:
        contract = sc.resolve_contract(api, config.code)
        log(f"contract {contract.code} ({contract.name}) delivery {contract.delivery_month}")

        guard = sc.UsageGuard(api)
        guard.sample(force=True)
        log(guard.describe())

        backfill(api, contract, conn, config)
        watchers = restore_watchers(conn, config)
        recorder = BookRecorder(api, contract, config)
        log(f"order book: {recorder.start()}")
        alerts = ar.AlertConfig(config.alerts_config)
        log(f"alert config: {alerts.path}" + (f" ({alerts.last_error})" if alerts.last_error else ""))

        try:
            while True:
                started = time.monotonic()
                if not sb.is_trading_time():
                    log(f"outside session hours; sleeping {config.idle_sleep_seconds}s")
                    if config.once:
                        break
                    time.sleep(config.idle_sleep_seconds)
                    if stopping["flag"]:
                        break
                    continue

                try:
                    run_cycle(api, contract, conn, config, watchers, recorder, alerts)
                except Exception as error:  # noqa: BLE001 - a bad cycle must not end the run
                    log(f"cycle failed: {type(error).__name__}: {error}")
                    traceback.print_exc()

                if guard.sample() and guard.over_budget:
                    log(f"WARNING {guard.describe()} -- doubling poll interval")
                    config.poll_seconds = min(config.poll_seconds * 2, 600)

                if config.once or stopping["flag"]:
                    break
                elapsed = time.monotonic() - started
                time.sleep(max(1.0, config.poll_seconds - elapsed))
        finally:
            shutdown(conn, config, watchers, recorder)
            log(guard.describe())

    return 0


if __name__ == "__main__":
    sys.exit(main())
