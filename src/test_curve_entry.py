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


@pytest.mark.parametrize("timeframe,step", [("1m", 1), ("5m", 5), ("1h", 60)])
@pytest.mark.parametrize("action,before,after", [("Buy", 99, 101), ("Sell", 101, 99)])
def test_source_timeframe_uses_closed_one_minute_crossing(timeframe, step, action, before, after):
    start = pd.Timestamp("2026-10-05 08:45")
    end = start + pd.Timedelta(minutes=step * 3 + 2)
    stamps = pd.date_range(start, end, freq="min")
    bars = frame([100] * (len(stamps) - 2) + [before, after], list(stamps))
    curve = [{"time": int((start + pd.Timedelta(minutes=step * index)).timestamp()), "price": 100}
             for index in range(3)]
    # 5m/1h candles are still forming, but the latest 1m candle has closed.
    assert crossed_curve(bars, curve, end + pd.Timedelta(minutes=1), action, timeframe)
    assert not crossed_curve(bars, curve, end + pd.Timedelta(seconds=30), action, timeframe)
    assert not crossed_curve(bars, curve, end + pd.Timedelta(minutes=5), action, timeframe)
    assert not crossed_curve(bars, curve[:2], end + pd.Timedelta(minutes=1), action, timeframe)
    missing = bars.drop(bars.index[-2])
    assert not crossed_curve(missing, curve, end + pd.Timedelta(minutes=1), action, timeframe)


@pytest.mark.parametrize("timeframe", ["1m", "5m"])
def test_session_break_does_not_create_minute_curve_crossing(timeframe):
    bars = frame([100, 100, 100, 99, 101], ["2026-10-05 08:45", "2026-10-05 08:50",
        "2026-10-05 08:55", "2026-10-05 13:44", "2026-10-05 15:00"])
    curve = [{"time": int(pd.Timestamp(stamp).timestamp()), "price": 100}
             for stamp in ("2026-10-05 08:45", "2026-10-05 08:50", "2026-10-05 08:55")]
    assert not crossed_curve(bars, curve, pd.Timestamp("2026-10-05 15:01"), "Buy", timeframe)


@pytest.mark.parametrize("timeframe,step", [("1m", 1), ("5m", 5)])
@pytest.mark.parametrize("action,sign", [("Buy", 1), ("Sell", -1)])
def test_sloping_curve_uses_source_bar_fraction_at_each_minute(timeframe, step, action, sign):
    start = pd.Timestamp("2026-10-05 08:45")
    stamps = list(pd.date_range(start, periods=4 * step + 3, freq="min"))
    curve = [{"time": int((start + pd.Timedelta(minutes=index * step)).timestamp()), "price": price}
             for index, price in [(0, 100), (2, 100), (4, 120)]]
    # The final tangent rises 10 points per source bar, or 2/minute for 5m.
    previous_value, current_value = 120 + 10 / step, 120 + 20 / step
    bars = frame([100] * (len(stamps) - 2) + [previous_value - sign * 0.5, current_value + sign * 0.5], stamps)
    now = stamps[-1] + pd.Timedelta(minutes=1)
    assert crossed_curve(bars, curve, now, action, timeframe)
    bars.loc[bars.index[-1], "close"] = current_value - sign * 0.5
    assert not crossed_curve(bars, curve, now, action, timeframe)
