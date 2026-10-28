"""Shioaji session handling and the raw-data fetches, with the quirks baked in.

Three API behaviours are easy to get wrong and are handled here once so nothing
downstream has to remember them (all verified live -- see ``doc/probe_findings.md``):

1. **``kbars`` labels bars by their CLOSING minute.**  The 2026-10-08 day session
   came back as 08:46..13:46, not 08:45..13:44.  :func:`fetch_minute_bars` shifts
   every timestamp by -1 minute so the rest of the project can treat
   ``minute_start`` as the bar's opening minute.  The leftover 13:46 bar becomes
   13:45, which ``session_bars`` then drops -- that is the 13:45:00 closing
   single-price auction, and dropping it is what keeps box parity with the
   tick-based backtests.
2. **``kbars`` slices by CALENDAR day but ``ticks`` slices by TRADING day.**
   ``ticks(date="2026-10-08")`` starts at 2026-10-07 15:00 because that night
   session belongs to trading day 10-08.  The two take different date windows.
3. **``ticks(AllDay)`` costs roughly 10 MB per trading day** against a 500 MiB
   daily quota, so it must never run inside a per-minute loop.  :class:`UsageGuard`
   watches the budget, and the order-book backfill uses ``RangeTime`` windows.

``api.usage()`` is also reported with a lag -- bracketing a call with two reads
returns 0 bytes for the call -- so the guard tracks a trend rather than trying to
price individual requests.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pandas as pd
import shioaji as sj
from dotenv import load_dotenv

import market_clock

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# kbars requests are chunked so one failure does not cost the whole backfill, and
# so a single response never gets enormous.
KBARS_CHUNK_DAYS = 7
FETCH_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

MINUTE_COLUMNS = ["minute_start", "open", "high", "low", "close", "volume", "amount"]
TICK_COLUMNS = [
    "ts",
    "close",
    "volume",
    "bid_price",
    "bid_volume",
    "ask_price",
    "ask_volume",
    "tick_type",
]


class DataUnavailable(RuntimeError):
    """A fetch the caller depends on came back empty or failed outright."""


def is_session_unavailable(error: BaseException) -> bool:
    """Recognize session failures even when wrapped by DataUnavailable."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if "sessionnotestablished" in message or "notready" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------
@contextmanager
def connect(simulation: bool | None = None) -> Iterator[sj.Shioaji]:
    """Log in from ``.env`` and always log out again."""
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        api_key = os.environ["SHIOAJI_API_KEY"]
        secret_key = os.environ["SHIOAJI_SECRET_KEY"]
    except KeyError as error:
        raise DataUnavailable(
            f"missing {error.args[0]} in {PROJECT_ROOT / '.env'}"
        ) from error

    use_simulation = (
        os.getenv("SHIOAJI_SIMULATION", "1") == "1" if simulation is None else simulation
    )
    api = sj.Shioaji(simulation=use_simulation)
    api.login(api_key=api_key, secret_key=secret_key)
    try:
        yield api
    finally:
        try:
            api.logout()
        except Exception:  # noqa: BLE001 - logout failures must not mask real errors
            pass


def resolve_contract(api: sj.Shioaji, code: str):
    """``api.Contracts.Futures.TMF["TMFR1"]`` from a bare code like ``TMFR1``."""
    root = code[:3].upper()
    futures = api.Contracts.Futures
    if not hasattr(futures, root):
        available = sorted(
            name for name in dir(futures) if name.isupper() and not name.startswith("_")
        )
        raise DataUnavailable(
            f"no futures root {root!r} in api.Contracts.Futures; available: {available}"
        )
    contract = getattr(futures, root).get(code)
    if contract is None:
        raise DataUnavailable(f"no contract {code!r} under futures root {root}")
    return contract


class UsageGuard:
    """Watch the daily byte budget and say when to back off.

    ``api.usage()`` lags, so this samples it every ``every`` checks and only ever
    reasons about the cumulative total, never about one request's cost.
    """

    def __init__(self, api: sj.Shioaji, every: int = 10, warn_at: float = 0.8) -> None:
        self.api = api
        self.every = max(1, every)
        self.warn_at = warn_at
        self.checks = 0
        self.last_bytes = 0
        self.last_limit = 0
        self.last_fraction = 0.0

    def sample(self, force: bool = False) -> float:
        """Fraction of the daily quota consumed, refreshed every ``every`` calls."""
        self.checks += 1
        if not force and self.checks % self.every != 1:
            return self.last_fraction
        try:
            usage = self.api.usage()
        except Exception:  # noqa: BLE001 - never let telemetry break the loop
            return self.last_fraction
        self.last_bytes = usage.bytes
        self.last_limit = usage.limit_bytes
        self.last_fraction = (usage.bytes / usage.limit_bytes) if usage.limit_bytes else 0.0
        return self.last_fraction

    @property
    def over_budget(self) -> bool:
        return self.last_fraction >= self.warn_at

    def describe(self) -> str:
        if not self.last_limit:
            return "usage unknown"
        return (
            f"usage {self.last_bytes:,}/{self.last_limit:,} bytes "
            f"({self.last_fraction * 100:.1f}%)"
        )


def _with_retries(label: str, call):
    last: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            return call()
        except Exception as error:  # noqa: BLE001 - retry any transport hiccup
            last = error
            if attempt < FETCH_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise DataUnavailable(f"{label} failed after {FETCH_RETRIES} attempts: {last}") from last


# --------------------------------------------------------------------------------------
# Minute bars
# --------------------------------------------------------------------------------------
def _kbars_frame(api: sj.Shioaji, contract, start: date, end: date) -> pd.DataFrame:
    kbars = _with_retries(
        f"kbars({contract.code}, {start}..{end})",
        lambda: api.kbars(contract, start=start.isoformat(), end=end.isoformat()),
    )
    frame = pd.DataFrame({**kbars})
    if frame.empty:
        return pd.DataFrame(columns=MINUTE_COLUMNS)

    frame["ts"] = pd.to_datetime(frame["ts"])
    # Quirk 1: ts names the bar's CLOSING minute, so shift to the opening minute.
    frame["minute_start"] = frame["ts"] - pd.Timedelta(minutes=1)
    frame = frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
            "Amount": "amount",
        }
    )
    if "amount" not in frame.columns:
        frame["amount"] = 0.0
    return frame[MINUTE_COLUMNS].sort_values("minute_start", kind="stable").reset_index(drop=True)


def _chunk_days(start: date, end: date, size: int) -> Iterator[tuple[date, date]]:
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=size - 1), end)
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def fetch_minute_bars(
    api: sj.Shioaji,
    contract,
    start: date,
    end: date,
    *,
    chunk_days: int = KBARS_CHUNK_DAYS,
    verbose: bool = False,
) -> pd.DataFrame:
    """Session-tagged minute bars for the calendar range ``[start, end]``.

    Timestamps are already shifted to minute-open and non-session minutes are
    dropped, so the result is ready for ``session_bars.minute_bars_to_hourly``.
    Returns an empty frame rather than raising when a range has no trading days --
    holidays are normal; it is the caller that knows whether empty is a problem.
    """
    from session_bars import add_sessions  # local import keeps this module importable alone

    pieces: list[pd.DataFrame] = []
    for chunk_start, chunk_end in _chunk_days(start, end, max(1, chunk_days)):
        frame = _kbars_frame(api, contract, chunk_start, chunk_end)
        if verbose:
            print(f"  kbars {chunk_start}..{chunk_end}: {len(frame)} raw minute bars", flush=True)
        if not frame.empty:
            pieces.append(frame)

    if not pieces:
        return add_sessions(pd.DataFrame(columns=MINUTE_COLUMNS))

    combined = (
        pd.concat(pieces, ignore_index=True)
        .drop_duplicates(subset="minute_start", keep="last")
        .sort_values("minute_start", kind="stable")
        .reset_index(drop=True)
    )
    return add_sessions(combined)


def fetch_latest_minute_bars(api: sj.Shioaji, contract, *, verbose: bool = False) -> pd.DataFrame:
    """Today plus yesterday -- the smallest window that always covers the live bar.

    The night session starts at 15:00 on the previous calendar day, and ``kbars``
    slices by calendar day (quirk 2), so "today" alone would miss the first hours
    of a night session that is still running.
    """
    today = market_clock.today()
    return fetch_minute_bars(api, contract, today - timedelta(days=1), today, verbose=verbose)


# --------------------------------------------------------------------------------------
# Ticks (the only historical order-book data that exists -- one level)
# --------------------------------------------------------------------------------------
def _ticks_frame(ticks) -> pd.DataFrame:
    frame = pd.DataFrame({**ticks})
    if frame.empty:
        return pd.DataFrame(columns=TICK_COLUMNS)
    frame["ts"] = pd.to_datetime(frame["ts"])
    return frame.sort_values("ts", kind="stable").reset_index(drop=True)


def fetch_ticks_range(
    api: sj.Shioaji,
    contract,
    trading_day: date,
    time_start: str,
    time_end: str,
) -> pd.DataFrame:
    """Ticks for one window of one trading day, via ``TicksQueryType.RangeTime``.

    This is how the order-book backfill stays affordable: pulling only the minutes
    where price was actually inside a box costs a fraction of an ``AllDay`` call.
    ``trading_day`` is a *trading* day, so its night session starts on the previous
    calendar day (quirk 2).
    """
    ticks = _with_retries(
        f"ticks({contract.code}, {trading_day}, {time_start}-{time_end})",
        lambda: api.ticks(
            contract,
            date=trading_day.isoformat(),
            query_type=sj.TicksQueryType.RangeTime,
            time_start=time_start,
            time_end=time_end,
        ),
    )
    return _ticks_frame(ticks)


def fetch_ticks_all_day(api: sj.Shioaji, contract, trading_day: date) -> pd.DataFrame:
    """A whole trading day of ticks -- roughly 10 MB, so call this deliberately."""
    ticks = _with_retries(
        f"ticks({contract.code}, {trading_day}, AllDay)",
        lambda: api.ticks(
            contract, date=trading_day.isoformat(), query_type=sj.TicksQueryType.AllDay
        ),
    )
    return _ticks_frame(ticks)


def fetch_snapshot(api: sj.Shioaji, contract) -> dict:
    """Best bid/ask plus last trade -- the one-level fallback when no stream exists."""
    snapshots = _with_retries(
        f"snapshots({contract.code})", lambda: api.snapshots([contract])
    )
    if not snapshots:
        raise DataUnavailable(f"snapshots({contract.code}) returned an empty list")
    snap = snapshots[0]
    return {
        "ts": pd.to_datetime(snap.ts),
        "close": float(snap.close),
        "open": float(snap.open),
        "high": float(snap.high),
        "low": float(snap.low),
        "volume": float(snap.volume),
        "total_volume": float(snap.total_volume),
        "buy_price": float(snap.buy_price),
        "buy_volume": float(snap.buy_volume),
        "sell_price": float(snap.sell_price),
        "sell_volume": float(snap.sell_volume),
    }
