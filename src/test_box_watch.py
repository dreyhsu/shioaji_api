"""Tests for the enter/stay/exit state machine."""

from __future__ import annotations

import pandas as pd
import pytest

from box_watch import BoxWatcher, BoxWatchConfig

BOX = pd.Series({"box_id": 1, "box_type": "support", "state": "fresh", "top": 110.0, "bottom": 100.0})
OTHER_BOX = pd.Series(
    {"box_id": 2, "box_type": "support", "state": "fresh", "top": 210.0, "bottom": 200.0}
)


def bar(low, high, close=None, minute="2026-10-08 15:00"):
    return {
        "minute_start": pd.Timestamp(minute),
        "open": close if close is not None else (low + high) / 2,
        "high": high,
        "low": low,
        "close": close if close is not None else (low + high) / 2,
    }


def walk(watcher, rows, box=BOX):
    """Feed bars in sequence and collect every transition that fired."""
    out = []
    for index, row in enumerate(rows):
        step = watcher.step(
            bar(*row, minute=f"2026-10-08 {15 + index // 60:02d}:{index % 60:02d}"), box
        )
        for part in (step, step.follow_up if step else None):
            if part is not None:
                out.append(part)
    return out


@pytest.fixture
def watcher():
    # Buffer off so these tests exercise the raw transitions.
    return BoxWatcher("support", BoxWatchConfig(exit_buffer_atr=0.0))


# --------------------------------------------------------------------------------------
# Basic lifecycle
# --------------------------------------------------------------------------------------
def test_outside_box_produces_nothing(watcher):
    assert walk(watcher, [(120, 130), (125, 135)]) == []


def test_one_visit_gives_one_enter_and_one_exit(watcher):
    steps = walk(watcher, [(120, 130), (105, 115), (102, 108), (120, 130)])
    kinds = [step.kind for step in steps]
    assert kinds == ["enter", "stay", "exit"]
    assert steps[-1].exit_reason == "left_box"


def test_entering_records_zone_position(watcher):
    steps = walk(watcher, [(100, 110, 105)])
    assert steps[0].kind == "enter"
    assert steps[0].zone_pct == pytest.approx(0.5)


def test_minutes_in_box_counts_only_distinct_minutes(watcher):
    walk(watcher, [(105, 115), (104, 114), (103, 113)])
    assert watcher.minutes_in_box == 3


def test_repolling_the_same_minute_does_not_advance_the_clock(watcher):
    same = bar(105, 115, minute="2026-10-08 15:00")
    watcher.step(same, BOX)
    watcher.step(same, BOX)
    watcher.step(same, BOX)
    assert watcher.minutes_in_box == 1


def test_a_wick_touching_the_edge_opens_a_visit(watcher):
    """The range test is the point: a spike that only grazes the zone still counts."""
    steps = walk(watcher, [(110, 150)])
    assert steps and steps[0].kind == "enter"


def test_re_entering_opens_a_second_visit(watcher):
    steps = walk(watcher, [(105, 115), (120, 130), (105, 115)])
    assert [s.kind for s in steps] == ["enter", "exit", "enter"]


# --------------------------------------------------------------------------------------
# Exit buffer
# --------------------------------------------------------------------------------------
def test_without_a_buffer_an_edge_poke_splits_the_visit():
    watcher = BoxWatcher("support", BoxWatchConfig(exit_buffer_atr=0.0))
    steps = walk(watcher, [(105, 115), (111, 112), (105, 115)])
    assert [s.kind for s in steps] == ["enter", "exit", "enter"]


def test_a_buffer_merges_the_poke_into_one_visit():
    """0.1 of a 10-point box tolerates a 1-point overshoot, so the visit survives."""
    watcher = BoxWatcher("support", BoxWatchConfig(exit_buffer_atr=0.1))
    steps = walk(watcher, [(105, 115), (110.5, 112), (105, 115)])
    assert [s.kind for s in steps] == ["enter", "stay", "stay"]


def test_a_buffer_still_lets_a_real_departure_end_the_visit():
    watcher = BoxWatcher("support", BoxWatchConfig(exit_buffer_atr=0.1))
    steps = walk(watcher, [(105, 115), (150, 160)])
    assert [s.kind for s in steps] == ["enter", "exit"]


def test_buffer_scales_with_the_supplied_atr():
    watcher = BoxWatcher("support", BoxWatchConfig(exit_buffer_atr=0.5))
    watcher.step(bar(105, 115), BOX, hourly_atr=100.0)  # buffer = 50 points
    step = watcher.step(bar(140, 150, minute="2026-10-08 15:01"), BOX, hourly_atr=100.0)
    assert step.kind == "stay"


# --------------------------------------------------------------------------------------
# Box lifecycle interactions
# --------------------------------------------------------------------------------------
def test_losing_the_box_closes_the_visit(watcher):
    watcher.step(bar(105, 115), BOX)
    step = watcher.step(bar(105, 115, minute="2026-10-08 15:01"), None)
    assert step.kind == "exit"
    assert step.exit_reason == "box_broken"


def test_no_box_and_outside_is_silent(watcher):
    assert watcher.step(bar(120, 130), None) is None


def test_a_new_box_closes_the_old_visit_and_may_open_a_new_one(watcher):
    watcher.step(bar(105, 115), BOX)
    step = watcher.step(bar(205, 215, minute="2026-10-08 15:01"), OTHER_BOX)
    assert step.kind == "exit"
    assert step.exit_reason == "box_changed"
    assert step.follow_up is not None
    assert step.follow_up.kind == "enter"
    assert step.follow_up.box_id == 2


def test_a_new_box_price_is_not_in_closes_without_reopening(watcher):
    watcher.step(bar(105, 115), BOX)
    step = watcher.step(bar(500, 510, minute="2026-10-08 15:01"), OTHER_BOX)
    assert step.kind == "exit"
    assert step.follow_up is None
    assert watcher.inside is False


# --------------------------------------------------------------------------------------
# Restart recovery
# --------------------------------------------------------------------------------------
def test_restore_adopts_an_unfinished_visit(watcher):
    watcher.restore(box_id=1, visit_id=42, minutes_in_box=17)
    assert watcher.inside is True
    assert watcher.visit_id == 42
    step = watcher.step(bar(105, 115), BOX)
    assert step.kind == "stay"  # continues the visit rather than re-entering
    assert watcher.minutes_in_box == 18


def test_restore_with_no_visit_leaves_the_watcher_outside(watcher):
    watcher.restore(box_id=None, visit_id=None, minutes_in_box=0)
    assert watcher.inside is False
    assert walk(watcher, [(105, 115)])[0].kind == "enter"


def test_exit_clears_the_visit_id(watcher):
    watcher.step(bar(105, 115), BOX)
    watcher.visit_id = 7
    watcher.step(bar(200, 210, minute="2026-10-08 15:01"), BOX)
    assert watcher.visit_id is None
    assert watcher.minutes_in_box == 0
