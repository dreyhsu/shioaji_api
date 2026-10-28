"""Five-level order-book capture, with a one-level fallback.

Shioaji has no historical depth endpoint -- the whole Data API is
``snapshots``/``ticks``/``kbars``/``credit_enquires``/``short_stock_sources``/
``scanners``/``punish``/``notice``/``daily_quotes``/``index_components``/
``fetch_contracts``.  Five-level books therefore exist only in the live
``BidAskFOPv1`` stream and **cannot be backfilled**, which is why the subscription
runs continuously rather than being switched on when price enters a zone: by the
time a visit opens, the interesting seconds are already in the past.  The buffer
keeps them so the first flush can write the run-up as ``pre_entry`` context.

Only *persistence* is gated by the visit.  A visit that never happens costs
nothing but a bounded deque.

The callback runs on Shioaji's Rust thread, so it does exactly one thing -- append
to a deque under a lock -- and never touches sqlite or any shared mutable state.

If the stream turns out to be unavailable (no market-data entitlement, or simply a
holiday), :func:`snapshot_row` degrades to ``api.snapshots``, which carries only
the best bid/ask.  Rows record ``levels`` and ``source`` so later analysis can
tell one-level data from five-level rather than silently mixing them.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import pandas as pd
import shioaji as sj
import market_clock

# ~1200 updates is a few minutes of an active session: enough to hold the run-up
# into a zone, bounded enough to never matter for memory.
BUFFER_SIZE = 1200
# How many buffered updates to write as pre-entry context when a visit opens.
PRE_ENTRY_ROWS = 30


def _floats(values: Any) -> list[float]:
    """BidAskFOPv1 prices arrive as decimal strings."""
    if not values:
        return []
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            out.append(float("nan"))
    return out


def _ints(values: Any) -> list[int]:
    if not values:
        return []
    out: list[int] = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            out.append(0)
    return out


@dataclass
class BookBuffer:
    """Thread-safe ring buffer of normalised five-level snapshots."""

    maxlen: int = BUFFER_SIZE

    def __post_init__(self) -> None:
        self._rows: deque[dict] = deque(maxlen=self.maxlen)
        self._lock = threading.Lock()
        self.received = 0

    def append(self, row: dict) -> None:
        with self._lock:
            self._rows.append(row)
            self.received += 1

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._rows)

    def latest(self) -> dict | None:
        with self._lock:
            return self._rows[-1] if self._rows else None

    def since(self, after: Any | None, limit: int | None = None) -> list[dict]:
        """Rows strictly newer than ``after`` (an event_ts), oldest first."""
        rows = self.snapshot()
        if after is not None:
            cutoff = pd.Timestamp(after)
            rows = [row for row in rows if pd.Timestamp(row["event_ts"]) > cutoff]
        if limit is not None and len(rows) > limit:
            rows = rows[-limit:]
        return rows

    def tail(self, count: int) -> list[dict]:
        rows = self.snapshot()
        return rows[-count:] if count < len(rows) else rows


def throttle(rows: list[dict], max_hz: float) -> list[dict]:
    """Keep at most ``max_hz`` rows per second, preferring the latest in each bucket.

    ``max_hz <= 0`` keeps everything.  Downsampling costs microstructure detail,
    so the knob exists to trade that against database growth -- a five-level row
    is roughly 300 bytes, so an unthrottled busy session can run to several MB per
    visit.
    """
    if max_hz <= 0 or not rows:
        return rows
    kept: dict[int, dict] = {}
    for row in rows:
        stamp = pd.Timestamp(row["event_ts"])
        bucket = int(stamp.value // int(1e9 / max_hz))
        kept[bucket] = row  # later row in the same bucket wins
    return [kept[key] for key in sorted(kept)]


class LiveBook:
    """Continuous ``BidAskFOPv1`` subscription feeding a :class:`BookBuffer`."""

    def __init__(self, api: sj.Shioaji, contract, buffer: BookBuffer | None = None, *, stale_seconds=60,
                 clock=market_clock.now, monotonic=time.monotonic) -> None:
        self.api = api
        self.contract = contract
        self.buffer = buffer or BookBuffer()
        self.subscribed = False
        self.last_error: str | None = None
        self.stale_seconds = stale_seconds
        self.clock, self.monotonic = clock, monotonic
        self.received_at = None
        self._accepting = False
        self._generation = 0
        self._callback_lock = threading.RLock()

    # -- Rust callback thread: append only, nothing else ------------------------------
    def _on_bidask(self, quote) -> None:
        if not self._accepting:
            return
        try:
            stamp = pd.Timestamp(quote.datetime)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert("Asia/Taipei").tz_localize(None)
            if pd.isna(stamp):
                raise ValueError("missing book timestamp")
            self.buffer.append(
                {
                    "event_ts": stamp,
                    "source": "bidask_v1",
                    "levels": len(quote.bid_price or []),
                    "bid_price": _floats(quote.bid_price),
                    "bid_volume": _ints(quote.bid_volume),
                    "diff_bid_vol": _ints(quote.diff_bid_vol),
                    "ask_price": _floats(quote.ask_price),
                    "ask_volume": _ints(quote.ask_volume),
                    "diff_ask_vol": _ints(quote.diff_ask_vol),
                    "bid_total_vol": int(quote.bid_total_vol or 0),
                    "ask_total_vol": int(quote.ask_total_vol or 0),
                    "underlying_price": float(quote.underlying_price or 0) or None,
                    "simtrade": bool(quote.simtrade),
                }
            )
            self.received_at = self.monotonic()
        except Exception as error:  # noqa: BLE001 - a bad quote must not kill the stream
            self.last_error = f"{type(error).__name__}: {error}"

    def start(self) -> bool:
        if self.subscribed:
            return True
        with self._callback_lock:
            self._generation += 1
            generation = self._generation
            self._accepting = True
            self.received_at = None
        try:
            def callback(quote):
                with self._callback_lock:
                    if generation == self._generation:
                        self._on_bidask(quote)
            self.api.set_on_bidask_fop_v1_callback(callback)
            self.api.quote.subscribe(
                self.contract, quote_type=sj.QuoteType.BidAsk, version=sj.QuoteVersion.v1
            )
            self.subscribed = True
        except Exception as error:  # noqa: BLE001
            self.last_error = f"{type(error).__name__}: {error}"
            self.subscribed = False
            self._accepting = False
        return self.subscribed

    def stop(self) -> None:
        with self._callback_lock:
            self._accepting = False
            self._generation += 1
        if not self.subscribed:
            return
        try:
            self.api.quote.unsubscribe(
                self.contract, quote_type=sj.QuoteType.BidAsk, version=sj.QuoteVersion.v1
            )
        except Exception as error:  # noqa: BLE001
            self.last_error = f"{type(error).__name__}: {error}"
        finally:
            self.subscribed = False

    @property
    def alive(self) -> bool:
        """Subscribed *and* actually receiving -- a holiday satisfies only the first."""
        latest = self.buffer.latest()
        if not self.subscribed or self.received_at is None or latest is None:
            return False
        age = (self.clock() - pd.Timestamp(latest["event_ts"])).total_seconds()
        return (-5 <= age <= self.stale_seconds
                and self.monotonic() - self.received_at <= self.stale_seconds)


def snapshot_row(api: sj.Shioaji, contract) -> dict:
    """One-level book from ``api.snapshots``, shaped like a buffered five-level row."""
    snapshots = api.snapshots([contract])
    if not snapshots:
        raise RuntimeError(f"snapshots({contract.code}) returned an empty list")
    snap = snapshots[0]
    return {
        "event_ts": pd.Timestamp(snap.ts),
        "source": "snapshot",
        "levels": 1,
        "bid_price": [float(snap.buy_price)],
        "bid_volume": [int(snap.buy_volume)],
        "diff_bid_vol": [0],
        "ask_price": [float(snap.sell_price)],
        "ask_volume": [int(snap.sell_volume)],
        "diff_ask_vol": [0],
        # api.snapshots has no resting-depth totals; these are the day's traded
        # aggressor volumes, so they are deliberately left out rather than faked.
        "bid_total_vol": None,
        "ask_total_vol": None,
        "underlying_price": None,
        "simtrade": False,
    }


def imbalance(row: dict) -> float | None:
    """Resting bid size over ask size: >1 means more size wants to buy.

    Uses the five-level totals when present, else sums whatever levels there are,
    so a one-level fallback row still yields a usable (if cruder) number.
    """
    if row is None:
        return None
    bid_total, ask_total = row.get("bid_total_vol"), row.get("ask_total_vol")
    if not bid_total or not ask_total:
        bid_total = sum(row.get("bid_volume") or [])
        ask_total = sum(row.get("ask_volume") or [])
    if not ask_total:
        return None
    return float(bid_total) / float(ask_total)


def ticks_to_book_rows(ticks: pd.DataFrame) -> list[dict]:
    """Historical ``api.ticks`` rows as one-level book snapshots.

    This is the only way to get order-book data for the past.  ``source`` is
    ``"ticks"`` so these never get confused with live five-level rows.
    """
    if ticks is None or ticks.empty:
        return []
    rows: list[dict] = []
    for tick in ticks.itertuples(index=False):
        bid, ask = float(tick.bid_price), float(tick.ask_price)
        # Auction ticks (e.g. 15:00:00.02x) legitimately have no book.
        if bid <= 0 and ask <= 0:
            continue
        rows.append(
            {
                "event_ts": pd.Timestamp(tick.ts),
                "source": "ticks",
                "levels": 1,
                "bid_price": [bid],
                "bid_volume": [int(tick.bid_volume)],
                "diff_bid_vol": [0],
                "ask_price": [ask],
                "ask_volume": [int(tick.ask_volume)],
                "diff_ask_vol": [0],
                "bid_total_vol": None,
                "ask_total_vol": None,
                "underlying_price": None,
                "simtrade": False,
            }
        )
    return rows
