"""Render the S/R box history over a 1h candlestick chart to a standalone HTML file.

Reads the minute bars that ``monitor.py``/``backfill`` already stored in sqlite,
folds them into session-anchored hourly bars and draws every box the Pine
indicator would have drawn, plus any logged reversal markers.  Use it to eyeball
the boxes against the same indicator on TradingView before trusting the alerts.

This file used to be a verbatim copy of ``snr_strategy/src/pine_snr_boxes_1h_chart.py``,
which could not run here: it imported three ``backtest_tmf_*`` modules and read
``data/txf``/``data/btcusdt_bars``, none of which exist in this project.  The box
algorithm moved to ``snr_core`` and the data source is now the database, so the
original remains available in the sibling repo if a side-by-side is ever needed.

::

    /opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python \
        src/snr_1_hour_chart.py --code TMFR1 --boxes-csv out/boxes.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import session_bars as sb
import snr_core as snr

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "out"


def parse_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid date {text!r}; expected YYYY-MM-DD") from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--start-date", type=parse_date, default=None, help="first visible day")
    parser.add_argument("--end-date", type=parse_date, default=None, help="last visible day")
    parser.add_argument(
        "--warmup-days",
        type=int,
        default=30,
        help=(
            "earlier days loaded for ATR/pivot warm-up but hidden from the chart "
            "(the first 2*lookback bars can never create a box)"
        ),
    )
    parser.add_argument("--lookback", type=int, default=snr.DEFAULT_LOOKBACK)
    parser.add_argument("--volume-length", type=int, default=snr.DEFAULT_VOLUME_LENGTH)
    parser.add_argument("--atr-length", type=int, default=snr.DEFAULT_ATR_LENGTH)
    parser.add_argument("--box-width", type=float, default=snr.DEFAULT_BOX_WIDTH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--boxes-csv", type=Path, default=None)
    parser.add_argument(
        "--no-reversals", action="store_true", help="omit logged reversal markers"
    )
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    for name in ("lookback", "volume_length", "atr_length"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.box_width < 0:
        parser.error("--box-width must be non-negative")
    if args.warmup_days < 0:
        parser.error("--warmup-days must be non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with db.session(args.db) as conn:
        minute_bars = db.load_minute_bars(conn, args.code)
        reversals = (
            pd.DataFrame() if args.no_reversals else db.load_reversals(conn, args.code, limit=500)
        )

    if minute_bars.empty:
        print(
            f"no minute bars for {args.code} in {args.db}\n"
            "run: conda run -n tss python src/monitor.py --code "
            f"{args.code} --once --no-telegram"
        )
        return 2

    hourly = sb.minute_bars_to_hourly(minute_bars)
    if len(hourly) <= 2 * args.lookback:
        print(
            f"only {len(hourly)} hourly bars; boxes need more than "
            f"2*lookback = {2 * args.lookback}"
        )
        return 2

    # Boxes are computed over everything available so the visible window is fully
    # populated, then clipped to what the user asked to see.
    boxes = snr.collect_snr_boxes(
        hourly,
        lookback=args.lookback,
        volume_length=args.volume_length,
        atr_length=args.atr_length,
        box_width=args.box_width,
    )

    visible = hourly
    if args.start_date is not None:
        visible = visible[visible["hour_start"] >= pd.Timestamp(args.start_date)]
    if args.end_date is not None:
        end = pd.Timestamp(args.end_date) + pd.Timedelta(days=1)
        visible = visible[visible["hour_start"] < end]
    if visible.empty:
        print("the requested window contains no bars")
        return 2
    visible = visible.reset_index(drop=True)

    visible_boxes = snr.clip_boxes(
        boxes, visible["timestamp"].iloc[0], visible["timestamp"].iloc[-1]
    )

    span = f"{visible['hour_start'].iloc[0]:%Y-%m-%d} to {visible['hour_start'].iloc[-1]:%Y-%m-%d}"
    title = (
        f"{args.code} 1h Support &amp; Resistance high-volume boxes ({span})<br>"
        f"<sub>lookback={args.lookback} vol_len={args.volume_length} "
        f"atr={args.atr_length} box_width={args.box_width:g}</sub>"
    )
    figure = snr.build_figure(
        visible,
        visible_boxes,
        title=title,
        volume_length=args.volume_length,
        instrument=args.code,
        reversals=reversals if not reversals.empty else None,
    )

    output = args.output or DEFAULT_OUTPUT_DIR / f"{args.code}_snr_boxes_1h.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(str(output), include_plotlyjs="cdn")

    supports = int((visible_boxes["box_type"] == "support").sum()) if not visible_boxes.empty else 0
    broken = int((visible_boxes["state"] == "broken").sum()) if not visible_boxes.empty else 0
    print(
        f"wrote {output}\n"
        f"  hourly bars={len(visible)} (warm-up hidden={len(hourly) - len(visible)})\n"
        f"  boxes={len(visible_boxes)} support={supports} "
        f"resistance={len(visible_boxes) - supports} broken={broken}\n"
        f"  reversal markers={0 if reversals.empty else len(reversals)}"
    )

    if args.boxes_csv is not None:
        args.boxes_csv.parent.mkdir(parents=True, exist_ok=True)
        visible_boxes.to_csv(args.boxes_csv, index=False, encoding="utf-8-sig")
        print(f"  box table -> {args.boxes_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
