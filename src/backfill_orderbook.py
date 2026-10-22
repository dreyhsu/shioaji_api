"""Backfill one-level order-book history for the windows price spent inside a box.

Shioaji cannot give back five-level depth for the past -- that only exists in the
live stream.  What it *can* give back is ``api.ticks``, where every trade carries
the best bid and ask at that instant, and it answers at least 120 trading days
back.  That is a real order book, just one level deep, and it is the only way to
have anything to analyse before the live capture has had months to accumulate.

Fetching it wholesale is not affordable: ``TicksQueryType.AllDay`` costs roughly
10 MB per trading day against a 500 MiB daily quota, so two months would blow the
entire budget.  Instead this replays the stored minute bars through the same
:class:`box_watch.BoxWatcher` the monitor uses, finds every historical "price was
inside a box" window, and pulls **only those minutes** with
``TicksQueryType.RangeTime``.  Those are exactly the minutes worth analysing.

Reconstructing which box was live at a past moment is the fiddly part.
``snr_core.current_box`` answers for *now*; :func:`active_box_at` answers for an
arbitrary past timestamp by ignoring boxes created later and boxes already broken
by then (``state_time`` records when the break happened).

Progress is recorded per window, so a run interrupted by the quota guard resumes
where it stopped instead of re-buying data.

::

    /opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python \
        src/backfill_orderbook.py --code TMFR1 --days 30 --max-usage-pct 70
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import orderbook as ob
import session_bars as sb
import shioaji_client as sc
import snr_core as snr
from box_watch import BoxWatcher, BoxWatchConfig

TASK = "orderbook_ticks"
SIDES = ("support", "resistance")
# A night session runs past midnight; api.ticks takes a clock range within one
# trading day, so a window that wraps is split rather than trusted to wrap.
MIDNIGHT = pd.Timestamp("00:00:00").time()


def active_box_at(boxes: pd.DataFrame, box_type: str, when: pd.Timestamp):
    """What ``snr_core.current_box`` would have returned at ``when``.

    Boxes created after that moment did not exist yet, and a box that was already
    broken by then had stopped being a zone.  ``state``/``state_time`` hold only
    the *final* state and when it was reached, which is enough: a box marked
    broken at 14:00 was still intact at 13:00.
    """
    if boxes is None or boxes.empty:
        return None
    subset = boxes[(boxes["box_type"] == box_type) & (boxes["created_time"] <= when)]
    if subset.empty:
        return None
    broken_already = (subset["state"] == "broken") & (
        subset["state_time"].notna() & (subset["state_time"] <= when)
    )
    subset = subset[~broken_already]
    if subset.empty:
        return None
    return subset.loc[subset["box_id"].idxmax()]


@dataclass
class Window:
    """One contiguous stretch of minutes price spent inside a box."""

    box_type: str
    box_id: int
    box_top: float
    box_bottom: float
    box_state: str
    start: pd.Timestamp
    end: pd.Timestamp
    entry_price: float
    entry_zone_pct: float
    exit_price: float
    exit_reason: str
    minutes: int
    min_zone_pct: float
    max_zone_pct: float
    min_price: float
    max_price: float

    @property
    def trading_day(self) -> date | None:
        """``api.ticks`` keys on the trading day, which for a night session is the
        *next* calendar day -- exactly what ``session_fields`` already computes."""
        fields = sb.session_fields(self.start)
        return None if fields is None else fields[1].date()

    @property
    def key(self) -> str:
        return f"{self.box_type}:{self.box_id}:{self.start:%Y%m%d%H%M}"

    def clock_ranges(self) -> list[tuple[str, str]]:
        """``(time_start, time_end)`` pairs, split if the window crosses midnight."""
        start_clock = self.start.strftime("%H:%M:%S")
        end_clock = self.end.strftime("%H:%M:%S")
        if self.start.date() == self.end.date():
            return [(start_clock, end_clock)]
        return [(start_clock, "23:59:59"), ("00:00:00", end_clock)]


def find_windows(
    minute_bars: pd.DataFrame, boxes: pd.DataFrame, exit_buffer_atr: float = 0.0
) -> list[Window]:
    """Replay history through the live state machine and collect every box visit."""
    watchers = {
        side: BoxWatcher(side, BoxWatchConfig(exit_buffer_atr=exit_buffer_atr))
        for side in SIDES
    }
    open_windows: dict[str, dict] = {}
    windows: list[Window] = []

    def _close(side: str, end: pd.Timestamp, price: float, reason: str) -> None:
        state = open_windows.pop(side, None)
        if state is None or state["minutes"] < 1:
            return
        windows.append(
            Window(
                box_type=side,
                box_id=state["box_id"],
                box_top=state["box_top"],
                box_bottom=state["box_bottom"],
                box_state=state["box_state"],
                start=state["start"],
                end=end,
                entry_price=state["entry_price"],
                entry_zone_pct=state["entry_zone_pct"],
                exit_price=price,
                exit_reason=reason,
                minutes=state["minutes"],
                min_zone_pct=state["min_zone"],
                max_zone_pct=state["max_zone"],
                min_price=state["min_price"],
                max_price=state["max_price"],
            )
        )

    for bar in minute_bars.itertuples(index=False):
        row = {
            "minute_start": bar.minute_start,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
        }
        for side in SIDES:
            box = active_box_at(boxes, side, bar.minute_start)
            step = watchers[side].step(row, box)
            if step is None:
                continue
            for part in (step, step.follow_up):
                if part is None:
                    continue
                if part.kind == "exit":
                    _close(side, bar.minute_start, part.price, part.exit_reason or "left_box")
                elif part.kind == "enter":
                    open_windows[side] = {
                        "box_id": part.box_id,
                        "box_top": float(box["top"]),
                        "box_bottom": float(box["bottom"]),
                        "box_state": str(box["state"]),
                        "start": bar.minute_start,
                        "entry_price": part.price,
                        "entry_zone_pct": part.zone_pct,
                        "minutes": 1,
                        "min_zone": part.zone_pct,
                        "max_zone": part.zone_pct,
                        "min_price": float(bar.low),
                        "max_price": float(bar.high),
                    }
                elif part.kind == "stay" and side in open_windows:
                    state = open_windows[side]
                    state["minutes"] += 1
                    state["min_zone"] = min(state["min_zone"], part.zone_pct)
                    state["max_zone"] = max(state["max_zone"], part.zone_pct)
                    state["min_price"] = min(state["min_price"], float(bar.low))
                    state["max_price"] = max(state["max_price"], float(bar.high))

    # Whatever is still open at the end of history ran to the edge of the data.
    if not minute_bars.empty:
        final = minute_bars.iloc[-1]
        for side in list(open_windows):
            _close(side, final["minute_start"], float(final["close"]), "end_of_data")

    windows.sort(key=lambda w: w.start)
    return windows


def store_window(conn, code: str, window: Window, rows: list[dict]) -> int:
    """Record the historical visit and its book rows; returns new book rows."""
    visit_id = db.open_visit(
        conn,
        code,
        box_id=window.box_id,
        box_type=window.box_type,
        box_top=window.box_top,
        box_bottom=window.box_bottom,
        box_state=window.box_state,
        entered_at=window.start,
        entry_minute_start=window.start,
        entry_price=window.entry_price,
        entry_zone_pct=window.entry_zone_pct,
        source="backfill",
    )
    db.touch_visit(
        conn,
        visit_id,
        price=window.min_price,
        zone_pct_value=window.min_zone_pct,
        minutes_in_box=window.minutes,
    )
    db.touch_visit(
        conn,
        visit_id,
        price=window.max_price,
        zone_pct_value=window.max_zone_pct,
        minutes_in_box=window.minutes,
    )
    for row in rows:
        row["visit_id"] = visit_id
        row["phase"] = "in_box"
    written = db.insert_orderbook_rows(conn, code, rows)
    if written:
        db.bump_visit_counter(conn, visit_id, "orderbook_rows", written)
    db.close_visit(
        conn,
        visit_id,
        exited_at=window.end,
        exit_minute_start=window.end,
        exit_price=window.exit_price,
        exit_reason=window.exit_reason,
    )
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--days", type=int, default=30, help="how far back to look for visits")
    parser.add_argument("--max-windows", type=int, default=0, help="0 = no limit")
    parser.add_argument(
        "--max-usage-pct",
        type=float,
        default=70.0,
        help="stop once the daily API quota passes this percentage",
    )
    parser.add_argument(
        "--exit-buffer-atr",
        type=float,
        default=0.1,
        help="merge visits separated by a brief poke back over the box edge",
    )
    parser.add_argument(
        "--min-minutes",
        type=int,
        default=3,
        help=(
            "skip visits shorter than this; a one-minute wick touching the edge "
            "is not worth an API call"
        ),
    )
    parser.add_argument("--lookback", type=int, default=snr.DEFAULT_LOOKBACK)
    parser.add_argument("--volume-length", type=int, default=snr.DEFAULT_VOLUME_LENGTH)
    parser.add_argument("--atr-length", type=int, default=snr.DEFAULT_ATR_LENGTH)
    parser.add_argument("--box-width", type=float, default=snr.DEFAULT_BOX_WIDTH)
    parser.add_argument(
        "--dry-run", action="store_true", help="list the windows without fetching ticks"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with db.session(args.db) as conn:
        minute_bars = db.load_minute_bars(conn, args.code)
        if minute_bars.empty:
            print(f"no minute bars for {args.code}; run monitor.py first")
            return 2

        hourly = sb.minute_bars_to_hourly(minute_bars)
        if len(hourly) <= 2 * args.lookback:
            print(f"only {len(hourly)} hourly bars; need more than {2 * args.lookback}")
            return 2

        boxes = snr.collect_snr_boxes(
            hourly,
            lookback=args.lookback,
            volume_length=args.volume_length,
            atr_length=args.atr_length,
            box_width=args.box_width,
        )

        cutoff = minute_bars["minute_start"].max() - pd.Timedelta(days=args.days)
        recent = minute_bars[minute_bars["minute_start"] >= cutoff].reset_index(drop=True)
        found = find_windows(recent, boxes, args.exit_buffer_atr)
        windows = [w for w in found if w.minutes >= args.min_minutes]

        done = db.backfill_done_keys(conn, args.code, TASK)
        pending = [w for w in windows if w.key not in done]

        total_minutes = sum(w.minutes for w in windows)
        print(
            f"{len(found)} box visits in the last {args.days} days; "
            f"{len(windows)} are at least {args.min_minutes} minutes long "
            f"({total_minutes:,} minutes inside a box); "
            f"{len(pending)} not yet backfilled"
        )
        for window in windows[:40]:
            mark = " " if window.key in done else "*"
            print(
                f" {mark} {window.box_type:10s} #{window.box_id:<3d} "
                f"{window.start:%Y-%m-%d %H:%M} -> {window.end:%H:%M} "
                f"({window.minutes:>4d} min, zone "
                f"{window.min_zone_pct * 100:5.0f}~{window.max_zone_pct * 100:3.0f}%)"
            )
        if len(windows) > 40:
            print(f"   ... and {len(windows) - 40} more")

        if args.dry_run:
            print("\n--dry-run: nothing fetched")
            return 0
        if not pending:
            print("\nnothing to do")
            return 0

        if args.max_windows:
            pending = pending[: args.max_windows]

        with sc.connect() as api:
            contract = sc.resolve_contract(api, args.code)
            guard = sc.UsageGuard(api, every=1)
            start_pct = guard.sample(force=True) * 100
            print(f"\nstarting at {guard.describe()}")

            fetched = 0
            for index, window in enumerate(pending, start=1):
                trading_day = window.trading_day
                if trading_day is None:
                    print(f"  skip {window.key}: start is outside any session")
                    continue

                rows: list[dict] = []
                for time_start, time_end in window.clock_ranges():
                    try:
                        ticks = sc.fetch_ticks_range(
                            api, contract, trading_day, time_start, time_end
                        )
                    except sc.DataUnavailable as error:
                        print(f"  {window.key} {time_start}-{time_end}: {error}")
                        continue
                    rows.extend(ob.ticks_to_book_rows(ticks))

                written = store_window(conn, args.code, window, rows)
                db.mark_backfill_done(
                    conn, args.code, TASK, window.key, written, f"{len(rows)} ticks"
                )
                fetched += written
                pct = guard.sample(force=True) * 100
                print(
                    f"  [{index}/{len(pending)}] {window.box_type} #{window.box_id} "
                    f"{window.start:%m-%d %H:%M} -> {len(rows):,} ticks, "
                    f"{written:,} new book rows, quota {pct:.1f}%"
                )
                if pct >= args.max_usage_pct:
                    print(
                        f"\nstopping: quota {pct:.1f}% reached the "
                        f"--max-usage-pct {args.max_usage_pct} limit. "
                        "Re-run tomorrow; finished windows are recorded and will be skipped."
                    )
                    break

            print(
                f"\ndone: {fetched:,} book rows added, "
                f"quota {start_pct:.1f}% -> {guard.sample(force=True) * 100:.1f}%"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
