"""Tests for hand-drawn trendlines: bar-index geometry."""

from __future__ import annotations

import pandas as pd
import pytest

import dashboard
import trendline as tl

# Two day-session bars, then the night session: 12:45 -> 15:00 is a 2h15m wall
# gap but only one bar step, which is the whole point of bar-index maths.
HOURS = [
    "2026-10-08 10:45", "2026-10-08 11:45", "2026-10-08 12:45",
    "2026-10-08 15:00", "2026-10-08 16:00", "2026-10-08 17:00",
]


def hourly(closes):
    stamps = pd.to_datetime(HOURS[: len(closes)])
    return pd.DataFrame({"hour_start": stamps, "timestamp": stamps, "close": closes})


def anchor(stamp, price):
    return {"time": dashboard._epoch(stamp), "price": price}


def test_anchor_indices_map_bar_times_across_a_session_gap():
    frame = hourly([1, 2, 3, 4, 5, 6])
    points = tl.anchor_indices(frame, [anchor(HOURS[4], 120), anchor(HOURS[1], 100)])
    assert points == [(1, 100.0), (4, 120.0)]


def test_anchor_off_the_bar_grid_or_out_of_window_is_rejected():
    frame = hourly([1, 2, 3, 4, 5, 6])
    assert tl.anchor_indices(frame, [anchor("2026-10-08 13:45", 1), anchor(HOURS[2], 2)]) is None
    assert tl.anchor_indices(frame, [anchor("2026-10-01 08:45", 1), anchor(HOURS[2], 2)]) is None
    assert tl.anchor_indices(frame, [anchor(HOURS[2], 1), anchor(HOURS[2], 5)]) is None


def test_value_interpolates_extends_right_and_is_none_before_start():
    points = [(1, 100.0), (3, 120.0), (4, 110.0)]
    assert tl.value_at(points, 0) is None
    assert tl.value_at(points, 2) == pytest.approx(110.0)
    assert tl.value_at(points, 4) == pytest.approx(110.0)
    # Past the last anchor the last segment's slope (-10/bar) carries on.
    assert tl.value_at(points, 6) == pytest.approx(90.0)


def test_line_values_are_nan_before_the_first_anchor():
    frame = hourly([1, 2, 3, 4, 5, 6])
    values = tl.line_values(frame, [anchor(HOURS[1], 100), anchor(HOURS[2], 110)])
    assert values.isna().tolist() == [True, False, False, False, False, False]
    assert values.iloc[5] == pytest.approx(140.0)


# ------------------------------------------------------------------ curve (Bezier)
CURVE_POINTS = [(0, 100.0), (2, 100.0), (4, 140.0)]  # start, control, end


def test_curve_passes_through_start_and_end_but_not_the_control():
    assert tl.value_at(CURVE_POINTS, 0, "curve") == pytest.approx(100.0)
    assert tl.value_at(CURVE_POINTS, 4, "curve") == pytest.approx(140.0)
    # Symmetric control -> t = 0.5 at the midpoint: 0.25*100 + 0.5*100 + 0.25*140.
    assert tl.value_at(CURVE_POINTS, 2, "curve") == pytest.approx(110.0)
    assert tl.value_at(CURVE_POINTS, 1, "curve") < tl.value_at(CURVE_POINTS, 3, "curve")


def test_curve_extends_along_its_end_tangent():
    # Tangent at the end is control -> end: +40 over 2 bars = +20/bar.
    assert tl.value_at(CURVE_POINTS, 5, "curve") == pytest.approx(160.0)
    assert tl.value_at(CURVE_POINTS, -1, "curve") is None


def test_curve_with_uneven_control_is_still_one_value_per_bar():
    points = [(0, 100.0), (1, 200.0), (6, 100.0)]
    values = [tl.value_at(points, i, "curve") for i in range(7)]
    assert values[0] == pytest.approx(100.0) and values[6] == pytest.approx(100.0)
    assert all(v >= 100.0 - 1e-9 for v in values)


def test_curve_needs_exactly_three_anchors():
    frame = hourly([1, 2, 3, 4, 5, 6])
    two = [anchor(HOURS[0], 1), anchor(HOURS[2], 2)]
    assert tl.anchor_indices(frame, two, "curve") is None
    three = two + [anchor(HOURS[4], 3)]
    assert tl.anchor_indices(frame, three, "curve") == [(0, 1.0), (2, 2.0), (4, 3.0)]

