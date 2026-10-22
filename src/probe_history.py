"""Probe what the Shioaji API will actually hand us before building on it.

Everything in this project depends on four answers that cannot be read off the
SDK's type stubs, so this script asks the live API and prints the verdicts:

1. **Quota** -- ``api.usage()`` before/after a one-day ``kbars`` call, so we can
   price the monitor's once-a-minute polling.  ``api.kbars`` only accepts *date*
   granularity, so the monitor has to re-pull the whole day every minute; if that
   is too expensive the fallback is aggregating ``api.ticks(LastCount)`` instead.
2. **kbars ``ts`` labelling** -- whether ``ts`` names the bar's opening or closing
   minute.  The day session ends at 13:45, so a last bar of 13:44 means ``ts`` is
   the open and 13:45 means it is the close (and every timestamp needs a -1min
   shift).  Guessing wrong shifts every bar into the wrong session bucket.
3. **Historical depth** -- how far back ``api.ticks`` still answers, and whether
   its ``bid_*``/``ask_*`` columns carry real values.  Those columns are the only
   historical order-book data that exists: Shioaji's whole Data API is
   ``snapshots``/``ticks``/``kbars``/``credit_enquires``/``short_stock_sources``/
   ``scanners``/``punish``/``notice``/``daily_quotes``/``index_components``/
   ``fetch_contracts`` -- there is **no** historical depth endpoint, so five-level
   books can only ever be accumulated going forward.
4. **Realtime quotes** -- whether this account/environment actually streams
   ``BidAskFOPv1``, and at how many updates per second.  ``SHIOAJI_SIMULATION=1``
   may or may not push real quotes; the answer decides whether the five-level
   capture is possible at all or has to degrade to one-level ``api.snapshots``.

Run inside the ``tss`` conda env::

    /opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python \
        src/probe_history.py --code TMFR1
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import shioaji as sj
from dotenv import load_dotenv
from shioaji.constant import QuoteType, QuoteVersion, TicksQueryType

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Trading days to step back when testing how deep api.ticks still answers.
DEPTH_PROBES = (1, 5, 20, 60, 120)

# The day session opens at 08:45; the first bar's label is the ruler for the
# ts-open-vs-close question (see probe_kbars).
DAY_SESSION_OPEN = "08:45"


def log(message: str = "") -> None:
    print(message, flush=True)


def section(title: str) -> None:
    log()
    log("=" * 78)
    log(title)
    log("=" * 78)


class ProbeFailure(RuntimeError):
    """A piece of data the project needs could not be fetched."""


# --------------------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------------------
def login(simulation_override: bool | None = None) -> tuple[sj.Shioaji, bool]:
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        api_key = os.environ["SHIOAJI_API_KEY"]
        secret_key = os.environ["SHIOAJI_SECRET_KEY"]
    except KeyError as error:
        raise ProbeFailure(f"missing {error.args[0]} in {PROJECT_ROOT / '.env'}") from error

    simulation = (
        os.getenv("SHIOAJI_SIMULATION", "1") == "1"
        if simulation_override is None
        else simulation_override
    )
    log(f"simulation = {simulation}")
    api = sj.Shioaji(simulation=simulation)
    accounts = api.login(api_key=api_key, secret_key=secret_key)
    log(f"logged in, {len(accounts)} account(s):")
    for account in accounts:
        log(f"  {type(account).__name__} id={account.account_id} signed={account.signed}")
    return api, simulation


def show_usage(api: sj.Shioaji, label: str) -> int:
    """Print the quota and return bytes used so far."""
    usage = api.usage()
    pct = (usage.bytes / usage.limit_bytes * 100) if usage.limit_bytes else float("nan")
    log(
        f"[usage {label}] connections={usage.connections} "
        f"bytes={usage.bytes:,} limit={usage.limit_bytes:,} "
        f"remaining={usage.remaining_bytes:,} ({pct:.2f}% used)"
    )
    return usage.bytes


# --------------------------------------------------------------------------------------
# 1. Contract
# --------------------------------------------------------------------------------------
def resolve_contract(api: sj.Shioaji, code: str):
    root = code[:3].upper()
    futures = api.Contracts.Futures
    if not hasattr(futures, root):
        available = sorted(
            name for name in dir(futures) if name.isupper() and not name.startswith("_")
        )
        raise ProbeFailure(
            f"no futures root {root!r} in api.Contracts.Futures; available: {available}"
        )
    family = getattr(futures, root)
    contract = family.get(code)
    if contract is None:
        codes = sorted(c.code for c in family)
        raise ProbeFailure(f"no contract {code!r} under root {root}; available: {codes}")
    return contract


# --------------------------------------------------------------------------------------
# 2. kbars: ts labelling + quota cost
# --------------------------------------------------------------------------------------
def kbars_frame(api: sj.Shioaji, contract, start: date, end: date) -> pd.DataFrame:
    kbars = api.kbars(contract, start=start.isoformat(), end=end.isoformat())
    frame = pd.DataFrame({**kbars})
    if frame.empty:
        return frame
    frame["ts"] = pd.to_datetime(frame["ts"])
    return frame.sort_values("ts").reset_index(drop=True)


def probe_kbars(api: sj.Shioaji, contract, lookback_days: int) -> dict:
    """Find a trading day with a full day session and read off the ts convention."""
    today = date.today()
    found: pd.DataFrame | None = None
    found_day: date | None = None

    bytes_before = show_usage(api, "before kbars")
    calls = 0
    for back in range(1, lookback_days + 1):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:  # Sat/Sun: TAIFEX is closed
            continue
        frame = kbars_frame(api, contract, day, day)
        calls += 1
        if frame.empty:
            log(f"  {day} -> no bars (holiday?)")
            continue
        day_session = frame[
            (frame["ts"].dt.time >= pd.Timestamp("08:45").time())
            & (frame["ts"].dt.time < pd.Timestamp("14:00").time())
        ]
        log(f"  {day} -> {len(frame)} bars, day-session bars={len(day_session)}")
        if len(day_session) >= 290:  # a full 08:45-13:45 session is 300 minutes
            found, found_day = frame, day
            break
    bytes_after = show_usage(api, "after kbars")

    if found is None or found_day is None:
        raise ProbeFailure(
            f"no trading day with a full day session found in the last {lookback_days} days; "
            "cannot determine the kbars ts convention"
        )

    day_session = found[
        (found["ts"].dt.time >= pd.Timestamp("08:45").time())
        & (found["ts"].dt.time < pd.Timestamp("14:00").time())
    ]
    first_ts, last_ts = day_session["ts"].iloc[0], day_session["ts"].iloc[-1]
    first_clock, last_clock = first_ts.strftime("%H:%M"), last_ts.strftime("%H:%M")

    # The session's FIRST bar is the unambiguous ruler: the day session opens at
    # 08:45, so a bar labelled 08:45 means ts is the bar's open and 08:46 means it
    # is the close.  The last bar is a worse test because 13:45:00's closing
    # single-price auction adds one extra bar beyond the continuous session.
    if first_clock == "08:45":
        convention = "open"
        verdict = "ts labels the bar's OPENING minute -> use timestamps as-is"
    elif first_clock == "08:46":
        convention = "close"
        verdict = "ts labels the bar's CLOSING minute -> shift every ts by -1 minute"
    else:
        convention = "unknown"
        verdict = (
            f"day session's first bar is {first_clock}, which is neither 08:45 nor 08:46 "
            "-- inspect manually before trusting the session bucketing"
        )

    if convention == "close" and last_clock == "13:46":
        verdict += (
            "; the 13:46 bar is the 13:45:00 closing auction, which session_fields "
            "drops once shifted (matching the tick-based backtests)"
        )

    log()
    log(f"sample day            : {found_day}")
    log(f"total bars that day   : {len(found)}")
    log(f"day session first/last: {first_ts}  ..  {last_ts}  ({len(day_session)} bars)")
    log(f"  first bar clock = {first_clock} (the ruler), last = {last_clock}")
    night = found[found["ts"].dt.time >= pd.Timestamp("15:00").time()]
    if not night.empty:
        log(f"night segment (>=15:00): {night['ts'].iloc[0]}  ..  {night['ts'].iloc[-1]}  ({len(night)} bars)")
    early = found[found["ts"].dt.time < pd.Timestamp("05:00").time()]
    if not early.empty:
        log(f"night segment (<05:00) : {early['ts'].iloc[0]}  ..  {early['ts'].iloc[-1]}  ({len(early)} bars)")
    log(f"columns               : {list(found.columns)}")
    log()
    log(f">>> ts convention: {convention.upper()} -- {verdict}")

    cost = bytes_after - bytes_before
    log()
    log(f"quota cost of this probe block: {cost:,} bytes")
    if cost > 0:
        # The monitor polls once a minute; TAIFEX trades ~19h/day = ~1140 minutes.
        per_call = cost / max(1, calls)
        log(f"  kbars(one day) calls issued: {calls}")
        log(f"  approx per kbars(one day) call: {per_call:,.0f} bytes")
        log(
            f"  once-a-minute polling for a full ~1140-minute trading day: "
            f"{per_call * 1140 / 1_048_576:,.1f} MiB/day"
        )

    return {
        "convention": convention,
        "sample_day": found_day,
        "bars": len(found),
        "day_session_bars": len(day_session),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "frame": found,
    }


# --------------------------------------------------------------------------------------
# 3. Historical ticks: depth + whether bid/ask columns are populated
# --------------------------------------------------------------------------------------
def trading_days_back(count: int) -> date:
    """``count`` weekdays before today (holidays just come back empty)."""
    day = date.today()
    stepped = 0
    while stepped < count:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            stepped += 1
    return day


def probe_ticks(api: sj.Shioaji, contract) -> dict:
    results: list[dict] = []
    deepest_ok: int | None = None

    for back in DEPTH_PROBES:
        day = trading_days_back(back)
        try:
            ticks = api.ticks(contract, date=day.isoformat(), query_type=TicksQueryType.AllDay)
            frame = pd.DataFrame({**ticks})
        except Exception as error:  # noqa: BLE001 - we are probing; report, don't crash
            log(f"  -{back:>3} trading days ({day}): FAILED -- {type(error).__name__}: {error}")
            results.append({"back": back, "date": day, "rows": 0, "error": str(error)})
            continue

        rows = len(frame)
        if rows == 0:
            log(f"  -{back:>3} trading days ({day}): 0 rows (holiday, or beyond retention)")
            results.append({"back": back, "date": day, "rows": 0, "error": None})
            continue

        has_bid = "bid_price" in frame.columns
        bid_nonzero = float((frame["bid_price"] > 0).mean()) if has_bid else 0.0
        ask_nonzero = float((frame["ask_price"] > 0).mean()) if "ask_price" in frame.columns else 0.0
        log(
            f"  -{back:>3} trading days ({day}): {rows:,} rows, "
            f"bid_price>0 in {bid_nonzero:.1%}, ask_price>0 in {ask_nonzero:.1%}"
        )
        results.append(
            {
                "back": back,
                "date": day,
                "rows": rows,
                "bid_nonzero": bid_nonzero,
                "ask_nonzero": ask_nonzero,
                "error": None,
            }
        )
        deepest_ok = back
        if back == DEPTH_PROBES[0]:
            log(f"      columns: {list(frame.columns)}")
            log("      first 3 rows (the only historical order-book data that exists):")
            for _, row in frame.head(3).iterrows():
                stamp = pd.to_datetime(row["ts"])
                log(
                    f"        {stamp}  close={row['close']:>10}  vol={row['volume']:>5}  "
                    f"bid={row['bid_price']}x{row['bid_volume']}  "
                    f"ask={row['ask_price']}x{row['ask_volume']}  tick_type={row['tick_type']}"
                )

    log()
    if deepest_ok is None:
        log(">>> api.ticks returned nothing usable at any depth")
    else:
        log(f">>> api.ticks answered at least {deepest_ok} trading days back")
    log(">>> five-level historical depth: NOT AVAILABLE from Shioaji at any depth")
    return {"results": results, "deepest_ok": deepest_ok}


# --------------------------------------------------------------------------------------
# 4. Realtime five-level book
# --------------------------------------------------------------------------------------
def probe_realtime(api: sj.Shioaji, contract, seconds: int) -> dict:
    books: list[dict] = []
    ticks: list[dict] = []
    lock = threading.Lock()

    def on_bidask(quote) -> None:
        with lock:
            books.append(quote.to_dict())

    def on_tick(quote) -> None:
        with lock:
            ticks.append(quote.to_dict())

    api.set_on_bidask_fop_v1_callback(on_bidask)
    api.set_on_tick_fop_v1_callback(on_tick)

    log(f"subscribing BidAsk + Tick for {contract.code}, listening {seconds}s ...")
    api.quote.subscribe(contract, quote_type=QuoteType.BidAsk, version=QuoteVersion.v1)
    api.quote.subscribe(contract, quote_type=QuoteType.Tick, version=QuoteVersion.v1)

    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            time.sleep(0.25)
    finally:
        elapsed = time.monotonic() - started
        try:
            api.quote.unsubscribe(contract, quote_type=QuoteType.BidAsk, version=QuoteVersion.v1)
            api.quote.unsubscribe(contract, quote_type=QuoteType.Tick, version=QuoteVersion.v1)
        except Exception as error:  # noqa: BLE001
            log(f"  (unsubscribe raised {type(error).__name__}: {error})")

    with lock:
        book_rows, tick_rows = list(books), list(ticks)

    log()
    log(f"bidask updates: {len(book_rows)}  ({len(book_rows) / elapsed:.2f}/s)")
    log(f"tick updates  : {len(tick_rows)}  ({len(tick_rows) / elapsed:.2f}/s)")

    if book_rows:
        simtrade = Counter(bool(row.get("simtrade")) for row in book_rows)
        log(f"bidask simtrade flags: {dict(simtrade)}")
        log("first 3 five-level books:")
        for row in book_rows[:3]:
            log(f"  {row.get('datetime')}  bid_total={row.get('bid_total_vol')} ask_total={row.get('ask_total_vol')}")
            log(f"    bid_price ={row.get('bid_price')}")
            log(f"    bid_volume={row.get('bid_volume')}  diff={row.get('diff_bid_vol')}")
            log(f"    ask_price ={row.get('ask_price')}")
            log(f"    ask_volume={row.get('ask_volume')}  diff={row.get('diff_ask_vol')}")
        levels = len(book_rows[0].get("bid_price") or [])
        log(f">>> realtime book available with {levels} level(s)")
    else:
        log(">>> NO bidask updates arrived")

    if tick_rows:
        tick_types = Counter(row.get("tick_type") for row in tick_rows)
        log(f"tick_type distribution: {dict(tick_types)}  (aggressor side)")
        log("first 3 ticks:")
        for row in tick_rows[:3]:
            log(
                f"  {row.get('datetime')}  close={row.get('close')} vol={row.get('volume')} "
                f"tick_type={row.get('tick_type')} "
                f"bid_side_total={row.get('bid_side_total_vol')} ask_side_total={row.get('ask_side_total_vol')} "
                f"simtrade={row.get('simtrade')}"
            )

    return {
        "bidask_count": len(book_rows),
        "tick_count": len(tick_rows),
        "elapsed": elapsed,
        "bidask_hz": len(book_rows) / elapsed if elapsed else 0.0,
        "levels": len(book_rows[0].get("bid_price") or []) if book_rows else 0,
    }


def probe_snapshot(api: sj.Shioaji, contract) -> dict:
    """The one-level fallback if the stream is unavailable."""
    snapshots = api.snapshots([contract])
    if not snapshots:
        raise ProbeFailure("api.snapshots returned an empty list")
    snap = snapshots[0]
    log(
        f"snapshot: ts={pd.to_datetime(snap.ts)} close={snap.close} "
        f"total_volume={snap.total_volume}"
    )
    log(
        f"  best bid {snap.buy_price} x {snap.buy_volume}   "
        f"best ask {snap.sell_price} x {snap.sell_volume}   (one level only)"
    )
    return {"close": snap.close, "ts": snap.ts}


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", default="TMFR1", help="futures contract code (default TMFR1)")
    parser.add_argument(
        "--seconds", type=int, default=15, help="how long to listen for realtime quotes"
    )
    parser.add_argument(
        "--kbars-lookback-days",
        type=int,
        default=10,
        help="how many calendar days back to search for a full day session",
    )
    parser.add_argument(
        "--simulation",
        choices=("0", "1"),
        default=None,
        help="override SHIOAJI_SIMULATION from .env",
    )
    parser.add_argument("--skip-realtime", action="store_true")
    parser.add_argument("--skip-ticks", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    override = None if args.simulation is None else args.simulation == "1"

    api: sj.Shioaji | None = None
    failures: list[str] = []
    try:
        section("0. LOGIN")
        api, simulation = login(override)

        section("1. QUOTA")
        show_usage(api, "baseline")

        section(f"2. CONTRACT {args.code}")
        contract = resolve_contract(api, args.code)
        log(str(contract))

        section("3. KBARS: ts labelling + quota cost")
        try:
            kbars_info = probe_kbars(api, contract, args.kbars_lookback_days)
        except ProbeFailure as error:
            failures.append(f"kbars: {error}")
            kbars_info = {"convention": "unknown"}

        if not args.skip_ticks:
            section("4. HISTORICAL TICKS: depth + one-level book")
            try:
                probe_ticks(api, contract)
            except ProbeFailure as error:
                failures.append(f"ticks: {error}")

        section("5. SNAPSHOT (one-level fallback)")
        try:
            probe_snapshot(api, contract)
        except Exception as error:  # noqa: BLE001
            failures.append(f"snapshots: {type(error).__name__}: {error}")
            log(f"FAILED -- {type(error).__name__}: {error}")

        realtime = {"bidask_count": 0, "tick_count": 0, "bidask_hz": 0.0, "levels": 0}
        if not args.skip_realtime:
            section("6. REALTIME QUOTES")
            try:
                realtime = probe_realtime(api, contract, args.seconds)
            except Exception as error:  # noqa: BLE001
                failures.append(f"realtime subscribe: {type(error).__name__}: {error}")
                log(f"FAILED -- {type(error).__name__}: {error}")

        section("7. FINAL USAGE")
        show_usage(api, "final")

        section("VERDICTS")
        log(f"simulation mode          : {simulation}")
        log(f"kbars ts convention      : {kbars_info.get('convention')}")
        log(f"realtime bidask updates  : {realtime['bidask_count']} ({realtime['bidask_hz']:.2f}/s)")
        log(f"realtime book levels     : {realtime['levels']}")
        log(f"realtime tick updates    : {realtime['tick_count']}")
        log("historical 5-level depth : NOT AVAILABLE (no such endpoint in shioaji)")
        log(f"probed at                : {datetime.now():%Y-%m-%d %H:%M:%S} (Friday night session if >=15:00)")

        if failures:
            log()
            log("!!! DATA THAT COULD NOT BE FETCHED:")
            for item in failures:
                log(f"  - {item}")
            return 2
        return 0

    except ProbeFailure as error:
        log()
        log(f"!!! PROBE FAILED: {error}")
        return 2
    except Exception as error:  # noqa: BLE001
        log()
        log(f"!!! UNEXPECTED ERROR: {type(error).__name__}: {error}")
        raise
    finally:
        if api is not None:
            try:
                api.logout()
                log()
                log("logged out")
            except Exception as error:  # noqa: BLE001
                log(f"logout raised {type(error).__name__}: {error}")


if __name__ == "__main__":
    sys.exit(main())
