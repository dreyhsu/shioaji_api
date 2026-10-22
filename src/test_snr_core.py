"""Tests for the Pine indicator maths."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import snr_core as snr


def bars(closes, opens=None, highs=None, lows=None, volumes=None, start="2026-01-01 09:00"):
    """Hourly OHLCV from a close series, with sensible defaults for the rest."""
    closes = list(closes)
    count = len(closes)
    opens = list(opens) if opens is not None else [c - 1 for c in closes]
    highs = list(highs) if highs is not None else [max(o, c) + 1 for o, c in zip(opens, closes)]
    lows = list(lows) if lows is not None else [min(o, c) - 1 for o, c in zip(opens, closes)]
    volumes = list(volumes) if volumes is not None else [100.0] * count
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=count, freq="h"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


# --------------------------------------------------------------------------------------
# pine_rma / pine_atr
# --------------------------------------------------------------------------------------
def test_pine_rma_seeds_with_sma_then_wilder():
    values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = snr.pine_rma(values, 3)
    assert pd.isna(result.iloc[0]) and pd.isna(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)  # SMA of 1,2,3
    assert result.iloc[3] == pytest.approx((2.0 * 2 + 4.0) / 3)
    assert result.iloc[4] == pytest.approx((result.iloc[3] * 2 + 5.0) / 3)


def test_pine_rma_differs_from_ewm():
    """The whole reason pine_rma is a loop: pandas seeds the recursion differently."""
    values = pd.Series(np.arange(1.0, 21.0))
    pine = snr.pine_rma(values, 5)
    pandas_style = values.ewm(alpha=1 / 5, adjust=False, min_periods=5).mean()
    assert pine.iloc[-1] != pytest.approx(pandas_style.iloc[-1])


def test_pine_rma_too_short_is_all_nan():
    assert snr.pine_rma(pd.Series([1.0, 2.0]), 5).isna().all()


def test_pine_rma_rejects_bad_length():
    with pytest.raises(ValueError):
        snr.pine_rma(pd.Series([1.0]), 0)


def test_pine_atr_uses_true_range():
    frame = bars([10, 12], opens=[10, 10], highs=[11, 20], lows=[9, 5])
    atr = snr.pine_atr(frame, 2)
    # TR[0] = 11-9 = 2 (no previous close); TR[1] = max(15, |20-10|, |5-10|) = 15.
    assert atr.iloc[1] == pytest.approx((2 + 15) / 2)


# --------------------------------------------------------------------------------------
# delta_volume
# --------------------------------------------------------------------------------------
def test_delta_volume_signs_by_direction():
    frame = bars([11, 9], opens=[10, 10], volumes=[5, 7])
    assert list(snr.delta_volume(frame)) == [5.0, -7.0]


def test_delta_volume_holds_direction_through_doji():
    """Pine keeps the last direction when close == open, rather than zeroing."""
    frame = bars([9, 10, 12], opens=[10, 10, 10], volumes=[3, 4, 5])
    assert list(snr.delta_volume(frame)) == [-3.0, -4.0, 5.0]


def test_delta_volume_first_bar_doji_defaults_to_buy():
    frame = bars([10], opens=[10], volumes=[8])
    assert list(snr.delta_volume(frame)) == [8.0]


# --------------------------------------------------------------------------------------
# collect_snr_boxes
# --------------------------------------------------------------------------------------
@pytest.fixture
def one_support_box():
    """A series engineered to create exactly one support box, at index 4.

    With lookback=2 the pivot for bar 4 is bar 2, whose close (5) is the minimum of
    closes[0:5].  Bar 4's delta volume (+1000) clears ta.highest(delta/2.5, 2) = 400,
    so the support condition fires.  No other index satisfies either condition.
    """
    return bars(
        closes=[10, 9, 5, 9, 10, 11, 12],
        opens=[11, 10, 6, 5, 5, 10, 11],
        volumes=[100, 100, 100, 100, 1000, 100, 100],
    )


def test_collect_creates_exactly_one_support_box(one_support_box):
    boxes = snr.collect_snr_boxes(
        one_support_box, lookback=2, volume_length=2, atr_length=2, box_width=1.0
    )
    assert len(boxes) == 1
    box = boxes.iloc[0]
    assert box["box_type"] == "support"
    assert box["top"] == pytest.approx(5.0)  # the pivot close
    assert box["pivot_index"] == 2
    assert box["created_index"] == 4
    assert box["bottom"] < box["top"]


def test_box_bottom_is_pivot_minus_atr(one_support_box):
    boxes = snr.collect_snr_boxes(
        one_support_box, lookback=2, volume_length=2, atr_length=2, box_width=1.0
    )
    atr = snr.pine_atr(one_support_box, 2).iloc[4]
    assert boxes.iloc[0]["bottom"] == pytest.approx(5.0 - atr)


def test_box_width_scales_the_box(one_support_box):
    narrow = snr.collect_snr_boxes(
        one_support_box, lookback=2, volume_length=2, atr_length=2, box_width=0.5
    )
    wide = snr.collect_snr_boxes(
        one_support_box, lookback=2, volume_length=2, atr_length=2, box_width=1.0
    )
    assert narrow.iloc[0]["atr_width"] == pytest.approx(wide.iloc[0]["atr_width"] / 2)


def test_creation_limit_suppresses_the_forming_bar(one_support_box):
    """The box is born on bar 4, so a limit of 3 must prevent it."""
    boxes = snr.collect_snr_boxes(
        one_support_box, lookback=2, volume_length=2, atr_length=2, creation_limit=3
    )
    assert boxes.empty


def test_creation_limit_at_the_creating_bar_still_allows_it(one_support_box):
    boxes = snr.collect_snr_boxes(
        one_support_box, lookback=2, volume_length=2, atr_length=2, creation_limit=4
    )
    assert len(boxes) == 1


def test_collect_requires_ohlcv_columns():
    frame = bars([1, 2, 3]).drop(columns=["volume"])
    with pytest.raises(ValueError, match="Missing hourly columns"):
        snr.collect_snr_boxes(frame)


def test_collect_rejects_bad_parameters():
    frame = bars([1, 2, 3])
    with pytest.raises(ValueError):
        snr.collect_snr_boxes(frame, lookback=0)
    with pytest.raises(ValueError):
        snr.collect_snr_boxes(frame, box_width=-1)


def test_short_series_makes_no_boxes():
    assert snr.collect_snr_boxes(bars([1, 2, 3]), lookback=2).empty


# --------------------------------------------------------------------------------------
# crosses
# --------------------------------------------------------------------------------------
def test_cross_helpers():
    assert snr._crossed_over(5, 3, 4, 4) is True
    assert snr._crossed_over(5, 5, 4, 4) is False  # was already above
    assert snr._crossed_under(3, 5, 4, 4) is True
    assert snr._crossed_under(3, 3, 4, 4) is False


def test_cross_helpers_reject_nan():
    assert snr._crossed_over(5, 3, np.nan, 4) is False
    assert snr._crossed_under(np.nan, 5, 4, 4) is False


def test_cross_treats_equality_as_not_yet_crossed():
    """Pine's crossover needs a strict move past the level, not a touch."""
    assert snr._crossed_over(4, 3, 4, 4) is False


# --------------------------------------------------------------------------------------
# zone helpers
# --------------------------------------------------------------------------------------
@pytest.fixture
def box():
    return pd.Series({"box_id": 1, "box_type": "support", "top": 110.0, "bottom": 100.0})


def test_zone_pct_maps_edges_to_zero_and_one(box):
    assert snr.zone_pct(100.0, box) == pytest.approx(0.0)
    assert snr.zone_pct(110.0, box) == pytest.approx(1.0)
    assert snr.zone_pct(105.0, box) == pytest.approx(0.5)


def test_zone_pct_is_unclamped_outside_the_box(box):
    """Values outside [0, 1] are the point: they say how far out price went."""
    assert snr.zone_pct(120.0, box) == pytest.approx(2.0)
    assert snr.zone_pct(95.0, box) == pytest.approx(-0.5)


def test_zone_pct_of_degenerate_box_is_nan():
    flat = pd.Series({"top": 100.0, "bottom": 100.0})
    assert pd.isna(snr.zone_pct(100.0, flat))


def test_bar_in_box_uses_the_range_not_the_close(box):
    # Closes well above the box, but the low dips in -- this must still count,
    # because a wick into the zone is exactly what we want to capture.
    assert snr.bar_in_box({"low": 108.0, "high": 130.0}, box) is True
    assert snr.bar_in_box({"low": 111.0, "high": 130.0}, box) is False


def test_bar_in_box_counts_a_touch(box):
    assert snr.bar_in_box({"low": 110.0, "high": 130.0}, box) is True
    assert snr.bar_in_box({"low": 80.0, "high": 100.0}, box) is True


def test_current_box_picks_newest_unbroken():
    boxes = pd.DataFrame(
        {
            "box_id": [1, 2, 3],
            "box_type": ["support", "support", "support"],
            "state": ["holds", "broken", "fresh"],
            "top": [10.0, 20.0, 30.0],
            "bottom": [5.0, 15.0, 25.0],
        }
    )
    assert int(snr.current_box(boxes, "support")["box_id"]) == 3


def test_current_box_skips_broken_newest():
    boxes = pd.DataFrame(
        {
            "box_id": [1, 2],
            "box_type": ["support", "support"],
            "state": ["holds", "broken"],
            "top": [10.0, 20.0],
            "bottom": [5.0, 15.0],
        }
    )
    assert int(snr.current_box(boxes, "support")["box_id"]) == 1


def test_current_box_returns_none_when_all_broken_or_empty():
    boxes = pd.DataFrame(
        {"box_id": [1], "box_type": ["support"], "state": ["broken"], "top": [1.0], "bottom": [0.0]}
    )
    assert snr.current_box(boxes, "support") is None
    assert snr.current_box(boxes, "resistance") is None
    assert snr.current_box(pd.DataFrame(), "support") is None


# --------------------------------------------------------------------------------------
# rangebreaks
# --------------------------------------------------------------------------------------
def test_session_rangebreaks_finds_the_session_gap():
    """Real TMF hour starts: the day session's last bucket opens 12:45, the night's
    first opens 15:00, so the gap is 135 minutes -- 75 of them dead once the bar's
    own hour is accounted for."""
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-10-08 11:45",
                    "2026-10-08 12:45",  # last day bucket (covers 12:45-13:45)
                    "2026-10-08 15:00",  # night session opens
                    "2026-10-08 16:00",
                ]
            )
        }
    )
    breaks = snr.session_rangebreaks(frame)
    assert len(breaks) == 1
    assert breaks[0]["dvalue"] == 75 * 60 * 1000


def test_session_rangebreaks_ignores_a_sub_bar_gap():
    """A gap under 1.5 bars is a missing quiet minute, not a session boundary."""
    frame = pd.DataFrame(
        {"timestamp": pd.to_datetime(["2026-10-08 01:00", "2026-10-08 02:00", "2026-10-08 03:15"])}
    )
    assert snr.session_rangebreaks(frame) == []


def test_session_rangebreaks_empty_for_continuous_data():
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-10-08", periods=5, freq="h")})
    assert snr.session_rangebreaks(frame) == []
    assert snr.session_rangebreaks(pd.DataFrame()) == []
