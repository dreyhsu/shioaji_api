"""Tests for the order-book buffer, throttle and historical conversion."""

from __future__ import annotations

import threading

import pandas as pd
import pytest

import orderbook as ob


def row(offset_ms, **overrides):
    data = {
        "event_ts": pd.Timestamp("2026-10-08 15:00:00") + pd.Timedelta(milliseconds=offset_ms),
        "source": "bidask_v1",
        "levels": 5,
        "bid_volume": [1, 2, 3, 4, 5],
        "ask_volume": [1, 1, 1, 1, 1],
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------------------
# BookBuffer
# --------------------------------------------------------------------------------------
def test_buffer_is_bounded_but_counts_everything():
    buffer = ob.BookBuffer(maxlen=5)
    for index in range(12):
        buffer.append(row(index * 100))
    assert len(buffer.snapshot()) == 5
    assert buffer.received == 12  # the counter is how we know the stream is alive


def test_latest_returns_the_newest():
    buffer = ob.BookBuffer(maxlen=5)
    assert buffer.latest() is None
    buffer.append(row(0))
    buffer.append(row(500))
    assert buffer.latest()["event_ts"] == pd.Timestamp("2026-10-08 15:00:00.500")


def test_since_is_exclusive():
    buffer = ob.BookBuffer()
    for index in range(5):
        buffer.append(row(index * 100))
    newer = buffer.since(pd.Timestamp("2026-10-08 15:00:00.200"))
    assert len(newer) == 2  # .300 and .400, not .200 itself


def test_since_with_none_returns_everything():
    buffer = ob.BookBuffer()
    buffer.append(row(0))
    assert len(buffer.since(None)) == 1


def test_tail_takes_the_most_recent():
    buffer = ob.BookBuffer()
    for index in range(10):
        buffer.append(row(index * 100))
    tail = buffer.tail(3)
    assert len(tail) == 3
    assert tail[-1]["event_ts"] == pd.Timestamp("2026-10-08 15:00:00.900")


def test_tail_longer_than_the_buffer_is_fine():
    buffer = ob.BookBuffer()
    buffer.append(row(0))
    assert len(buffer.tail(50)) == 1


def test_concurrent_appends_are_not_lost():
    """The Rust quote thread appends while the monitor thread drains."""
    buffer = ob.BookBuffer(maxlen=10_000)

    def worker(base):
        for index in range(200):
            buffer.append(row(base + index))

    threads = [threading.Thread(target=worker, args=(base * 1000,)) for base in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert buffer.received == 800
    assert len(buffer.snapshot()) == 800


# --------------------------------------------------------------------------------------
# throttle
# --------------------------------------------------------------------------------------
def test_throttle_keeps_one_row_per_bucket():
    rows = [row(index * 100) for index in range(10)]  # 1 second of 10 Hz data
    assert len(ob.throttle(rows, 5.0)) == 5


def test_throttle_keeps_the_latest_in_each_bucket():
    """Within a bucket the freshest book is the truthful one."""
    # 5 Hz = 200 ms buckets, aligned to the epoch: 0 and 100 share one, 250 starts the next.
    rows = [row(0, bid_volume=[1]), row(100, bid_volume=[2]), row(250, bid_volume=[3])]
    kept = ob.throttle(rows, 5.0)
    assert len(kept) == 2
    assert kept[0]["bid_volume"] == [2]  # 100 supersedes 0
    assert kept[1]["bid_volume"] == [3]


def test_throttle_zero_keeps_everything():
    rows = [row(index * 10) for index in range(50)]
    assert len(ob.throttle(rows, 0)) == 50


def test_throttle_preserves_order():
    rows = [row(index * 500) for index in range(6)]
    kept = ob.throttle(rows, 1.0)
    stamps = [item["event_ts"] for item in kept]
    assert stamps == sorted(stamps)


def test_throttle_of_nothing_is_nothing():
    assert ob.throttle([], 5.0) == []


# --------------------------------------------------------------------------------------
# imbalance
# --------------------------------------------------------------------------------------
def test_imbalance_prefers_the_five_level_totals():
    assert ob.imbalance({"bid_total_vol": 30, "ask_total_vol": 10}) == pytest.approx(3.0)


def test_imbalance_falls_back_to_summing_levels():
    """A one-level fallback row still yields a usable, if cruder, number."""
    value = ob.imbalance(
        {"bid_total_vol": None, "ask_total_vol": None, "bid_volume": [6, 4], "ask_volume": [2, 3]}
    )
    assert value == pytest.approx(2.0)


def test_imbalance_is_none_without_an_ask_side():
    assert ob.imbalance({"bid_volume": [5], "ask_volume": [0]}) is None
    assert ob.imbalance(None) is None


# --------------------------------------------------------------------------------------
# historical ticks -> book rows
# --------------------------------------------------------------------------------------
def ticks_frame(rows):
    return pd.DataFrame(
        [
            {
                "ts": pd.Timestamp(stamp),
                "close": 100.0,
                "volume": 1,
                "bid_price": bid,
                "bid_volume": 5,
                "ask_price": ask,
                "ask_volume": 6,
                "tick_type": 1,
            }
            for stamp, bid, ask in rows
        ]
    )


def test_ticks_become_one_level_book_rows():
    rows = ob.ticks_to_book_rows(ticks_frame([("2026-10-08 15:00:01", 99.0, 101.0)]))
    assert len(rows) == 1
    assert rows[0]["source"] == "ticks"
    assert rows[0]["levels"] == 1
    assert rows[0]["bid_price"] == [99.0]
    assert rows[0]["ask_volume"] == [6]


def test_auction_ticks_without_a_book_are_dropped():
    """The 15:00:00.02x opening auction legitimately has no bid or ask."""
    rows = ob.ticks_to_book_rows(
        ticks_frame([("2026-10-08 15:00:00", 0.0, 0.0), ("2026-10-08 15:00:01", 99.0, 101.0)])
    )
    assert len(rows) == 1


def test_empty_ticks_give_no_rows():
    assert ob.ticks_to_book_rows(pd.DataFrame()) == []
    assert ob.ticks_to_book_rows(None) == []


# --------------------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------------------
def test_prices_arrive_as_strings_and_are_parsed():
    assert ob._floats(["49922.0", "49921.5"]) == [49922.0, 49921.5]
    assert ob._ints(["3", "4"]) == [3, 4]


def test_unparsable_values_do_not_raise():
    assert pd.isna(ob._floats(["bogus"])[0])
    assert ob._ints(["bogus"]) == [0]
    assert ob._floats(None) == []
