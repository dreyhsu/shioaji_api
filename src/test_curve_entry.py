import pandas as pd
import pytest
from curve_entry import crossed_above_curve, crossed_curve


def frame(closes, stamps=None):
    stamps = stamps or list(pd.date_range("2026-10-05 08:45", periods=len(closes), freq="h"))
    return pd.DataFrame({"minute_start": pd.to_datetime(stamps), "open": closes, "high": closes, "low": closes, "close": closes, "volume": 1})


def anchors():
    return [{"time": int(pd.Timestamp(stamp).timestamp()), "price": 100}
            for stamp in ("2026-10-05 08:45", "2026-10-05 09:45", "2026-10-05 10:45")]


@pytest.mark.parametrize("before,after,expected", [(99, 101, True), (100, 101, True), (99, 100, False), (101, 102, False), (101, 99, False)])
def test_closed_minute_crossing(before, after, expected):
    bars = frame([100, 100, 100, before, after], ["2026-10-05 08:45", "2026-10-05 09:45", "2026-10-05 10:45", "2026-10-05 11:00", "2026-10-05 11:01"])
    assert crossed_above_curve(bars, anchors(), pd.Timestamp("2026-10-05 11:02")) is expected


@pytest.mark.parametrize("action,closes", [("Buy", [100, 100, 99, 101]), ("Sell", [100, 100, 101, 99])])
def test_forming_stale_missing_and_unresolvable_bars_do_not_trigger(action, closes):
    bars = frame(closes, ["2026-10-05 08:45", "2026-10-05 09:45", "2026-10-05 10:45", "2026-10-05 10:46"])
    assert not crossed_curve(bars, anchors(), pd.Timestamp("2026-10-05 10:46:30"), action)
    assert not crossed_curve(bars, anchors(), pd.Timestamp("2026-10-05 11:30"), action)
    assert not crossed_curve(bars, anchors()[:2], pd.Timestamp("2026-10-05 10:47"), action)
    bars.loc[3, "minute_start"] += pd.Timedelta(minutes=1)
    assert not crossed_curve(bars, anchors(), pd.Timestamp("2026-10-05 10:48"), action)


@pytest.mark.parametrize("action,before,after,expected", [
    ("Buy", 99, 101, True), ("Buy", 100, 101, True), ("Buy", 99, 100, False),
    ("Buy", 101, 99, False), ("Sell", 101, 99, True), ("Sell", 100, 99, True),
    ("Sell", 101, 100, False), ("Sell", 99, 98, False), ("Sell", 99, 101, False),
])
def test_directional_crossing(action, before, after, expected):
    bars = frame([100, 100, 100, before, after], ["2026-10-05 08:45", "2026-10-05 09:45", "2026-10-05 10:45", "2026-10-05 11:00", "2026-10-05 11:01"])
    assert crossed_curve(bars, anchors(), pd.Timestamp("2026-10-05 11:02"), action) is expected
