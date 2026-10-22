"""Tests for the hourly candlestick reversal detectors."""

from __future__ import annotations

import pytest

import patterns as pt

# Small enough that the default min_range_atr (0.3) needs only a 6-point range.
ATR = 20.0


def bar(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close}


def fired(bar_, prev=None, *, box_type, **kwargs):
    return [hit["pattern"] for hit in pt.detect(bar_, prev, box_type=box_type, hourly_atr=ATR, **kwargs)]


# --------------------------------------------------------------------------------------
# BarShape
# --------------------------------------------------------------------------------------
def test_bar_shape_measures_the_pieces():
    shape = pt.BarShape.of(bar(100, 112, 90, 104))
    assert shape.body == 4
    assert shape.range == 22
    assert shape.upper_wick == 8     # 112 - max(100, 104)
    assert shape.lower_wick == 10    # min(100, 104) - 90
    assert shape.close_pos == pytest.approx(14 / 22)
    assert shape.bullish and not shape.bearish


def test_zero_range_bar_gets_a_neutral_close_position():
    shape = pt.BarShape.of(bar(100, 100, 100, 100))
    assert shape.close_pos == 0.5


# --------------------------------------------------------------------------------------
# Tails
# --------------------------------------------------------------------------------------
def test_hammer_fires_at_support():
    assert "long_lower_tail" in fired(bar(100, 102, 90, 101), box_type="support")


def test_hammer_is_ignored_at_resistance():
    """Direction has to match the side -- a hammer says nothing about a ceiling."""
    assert fired(bar(100, 102, 90, 101), box_type="resistance") == []


def test_shooting_star_fires_at_resistance():
    assert "long_upper_tail" in fired(bar(100, 112, 99, 100.5), box_type="resistance")


def test_tail_needs_the_wick_to_beat_the_body():
    # Lower wick 6 vs body 4 = 1.5x, under the 2.0 threshold.
    assert fired(bar(100, 105, 94, 104), box_type="support") == []


def test_tail_needs_the_close_near_the_high():
    # Big lower wick, but the close sits low in the range -- no rejection.
    assert fired(bar(101, 108, 92, 96.8), box_type="support") == []


def test_doji_with_a_long_wick_is_the_strongest_tail():
    """Zero body must not divide by zero; it scores a large finite ratio."""
    hits = pt.detect(bar(100, 100, 90, 100), None, box_type="support", hourly_atr=ATR)
    assert hits[0]["pattern"] == "long_lower_tail"
    assert hits[0]["detail"]["wick_body_ratio"] == 999.0  # finite, so it serialises


# --------------------------------------------------------------------------------------
# Engulfing
# --------------------------------------------------------------------------------------
PREV_BEAR = bar(110, 111, 99, 100)
PREV_BULL = bar(100, 111, 99, 110)


def test_bullish_engulfing_fires_at_support():
    assert "bullish_engulfing" in fired(bar(99, 115, 98, 112), PREV_BEAR, box_type="support")


def test_bearish_engulfing_fires_at_resistance():
    assert "bearish_engulfing" in fired(bar(111, 112, 96, 98), PREV_BULL, box_type="resistance")


def test_engulfing_needs_a_previous_bar():
    assert "bullish_engulfing" not in fired(bar(99, 115, 98, 112), None, box_type="support")


def test_engulfing_needs_the_opposite_previous_direction():
    assert "bullish_engulfing" not in fired(bar(99, 115, 98, 112), PREV_BULL, box_type="support")


def test_engulfing_needs_to_cover_the_previous_body():
    # Closes at 105, short of the previous open at 110.
    assert "bullish_engulfing" not in fired(bar(99, 115, 98, 105), PREV_BEAR, box_type="support")


def test_engulfing_needs_a_bigger_body():
    # Opens inside the previous body, so it does not engulf it.
    assert "bullish_engulfing" not in fired(bar(105, 115, 104, 111), PREV_BEAR, box_type="support")


# --------------------------------------------------------------------------------------
# Piercing / dark cloud
# --------------------------------------------------------------------------------------
def test_piercing_line_fires_at_support():
    assert "piercing_line" in fired(bar(98, 108, 97, 106), PREV_BEAR, box_type="support")


def test_dark_cloud_cover_fires_at_resistance():
    assert "dark_cloud_cover" in fired(bar(112, 113, 102, 104), PREV_BULL, box_type="resistance")


def test_piercing_stops_short_of_the_previous_open():
    """Past the previous open it is an engulfing, not a piercing line."""
    assert "piercing_line" not in fired(bar(98, 115, 97, 112), PREV_BEAR, box_type="support")


def test_piercing_needs_to_clear_the_midpoint():
    # Previous body 100-110, midpoint 105; closing at 102 is not a piercing.
    assert "piercing_line" not in fired(bar(98, 108, 97, 102), PREV_BEAR, box_type="support")


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------
def test_a_tiny_bar_is_filtered_out():
    """Without this, a flat overnight hour trivially satisfies every wick ratio."""
    assert fired(bar(100, 100.5, 99.5, 100.4), box_type="support") == []


def test_the_filter_is_relative_to_atr():
    tiny = bar(100, 102, 90, 101)
    assert fired(tiny, box_type="support") != []          # ATR 20 -> needs 6
    assert pt.detect(tiny, None, box_type="support", hourly_atr=200.0) == []  # needs 60


def test_without_an_atr_the_size_filter_is_skipped():
    """A correctly shaped but tiny hammer: blocked by the ATR filter, allowed without it."""
    small_hammer = bar(100, 100.2, 99, 100.1)
    assert pt.detect(small_hammer, None, box_type="support", hourly_atr=ATR) == []
    assert pt.detect(small_hammer, None, box_type="support", hourly_atr=None)


def test_zero_range_bar_never_fires():
    assert pt.detect(bar(100, 100, 100, 100), None, box_type="support", hourly_atr=ATR) == []


def test_unknown_box_type_fires_nothing():
    assert pt.detect(bar(100, 102, 90, 101), None, box_type="nonsense", hourly_atr=ATR) == []


def test_config_thresholds_are_honoured():
    strict = pt.PatternConfig(wick_body_ratio=50.0)
    assert pt.detect(bar(100, 102, 90, 101), None, box_type="support", hourly_atr=ATR, config=strict) == []


def test_every_registered_pattern_has_a_direction():
    assert set(pt.PATTERN_NAMES) == set(pt.REGISTRY)
    for name, (_, direction) in pt.REGISTRY.items():
        assert direction in (pt.LONG, pt.SHORT), name


def test_hits_carry_the_geometry_for_the_database():
    hit = pt.detect(bar(100, 102, 90, 101), None, box_type="support", hourly_atr=ATR)[0]
    assert set(hit) >= {"pattern", "direction", "detail", "upper_wick", "lower_wick", "body", "close_pos", "range_atr"}
