"""Candlestick reversal patterns on the hourly bar, scoped to a box side.

These enrich a box visit rather than gating it: the visit (and therefore the book
recording and the alerts) starts when the 1-minute bar enters the zone, and these
patterns say whether the hourly bar forming inside that zone *looks* like a
reversal.

Two rules apply to every detector:

* **Direction must match the side.**  A hammer means something at support and
  nothing at resistance, so ``detect`` filters by ``box_type`` rather than
  reporting every shape it finds.
* **The bar must be big enough to mean anything.**  ``min_range_atr`` throws away
  bars whose whole range is a fraction of the hourly ATR; without it a flat
  overnight hour trivially satisfies the wick ratios.

The hourly bar is normally *unfinished* when these run, so a result is provisional
-- a long lower tail at minute 10 can be gone by minute 50.  Callers record every
hit with ``minutes_into_bar`` and ``bar_closed`` instead of trying to decide when
the shape is final.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

LONG, SHORT = "long", "short"

# A support box is where longs are looked for, resistance is where shorts are.
SIDE_DIRECTION = {"support": LONG, "resistance": SHORT}


@dataclass(frozen=True)
class PatternConfig:
    # Reject bars whose entire range is smaller than this multiple of hourly ATR.
    min_range_atr: float = 0.3
    # Tail patterns: wick must be this many times the body ...
    wick_body_ratio: float = 2.0
    # ... and this fraction of the whole range.
    wick_range_ratio: float = 0.5
    # Where the close has to sit within the bar's range (0 = low, 1 = high).
    close_pos_long: float = 0.6
    close_pos_short: float = 0.4
    # Engulfing: the body must exceed the previous body by this factor.
    engulf_body_ratio: float = 1.0


@dataclass(frozen=True)
class BarShape:
    """The geometry every detector shares, computed once per call."""

    open: float
    high: float
    low: float
    close: float
    body: float
    range: float
    upper_wick: float
    lower_wick: float
    close_pos: float
    bullish: bool
    bearish: bool

    @classmethod
    def of(cls, bar) -> "BarShape":
        o, h, l, c = (
            float(bar["open"]),
            float(bar["high"]),
            float(bar["low"]),
            float(bar["close"]),
        )
        rng = h - l
        body = abs(c - o)
        return cls(
            open=o,
            high=h,
            low=l,
            close=c,
            body=body,
            range=rng,
            upper_wick=h - max(o, c),
            lower_wick=min(o, c) - l,
            # A zero-range bar has no meaningful close position; 0.5 keeps it
            # neutral so it fails both the long and short thresholds.
            close_pos=((c - l) / rng) if rng > 0 else 0.5,
            bullish=c > o,
            bearish=c < o,
        )


def _ratio(numerator: float, denominator: float) -> float:
    """Guard the doji case: a zero body would otherwise divide by zero.

    A bar with no body and a real wick is the *strongest* tail, not an error, so
    it gets a large finite score rather than inf (which would not serialise to
    JSON for the DB).
    """
    if denominator > 0:
        return numerator / denominator
    return 999.0 if numerator > 0 else 0.0


# --------------------------------------------------------------------------------------
# Detectors: (shape, prev_shape, cfg) -> detail dict | None
# --------------------------------------------------------------------------------------
def long_lower_tail(shape: BarShape, prev: BarShape | None, cfg: PatternConfig) -> dict | None:
    """Hammer / pin bar: sellers pushed down and got rejected."""
    wick_body = _ratio(shape.lower_wick, shape.body)
    wick_range = _ratio(shape.lower_wick, shape.range)
    if (
        wick_body >= cfg.wick_body_ratio
        and wick_range >= cfg.wick_range_ratio
        and shape.close_pos >= cfg.close_pos_long
    ):
        return {
            "lower_wick": shape.lower_wick,
            "body": shape.body,
            "wick_body_ratio": round(wick_body, 3),
            "wick_range_ratio": round(wick_range, 3),
            "close_pos": round(shape.close_pos, 3),
        }
    return None


def long_upper_tail(shape: BarShape, prev: BarShape | None, cfg: PatternConfig) -> dict | None:
    """Shooting star / gravestone: buyers pushed up and got rejected."""
    wick_body = _ratio(shape.upper_wick, shape.body)
    wick_range = _ratio(shape.upper_wick, shape.range)
    if (
        wick_body >= cfg.wick_body_ratio
        and wick_range >= cfg.wick_range_ratio
        and shape.close_pos <= cfg.close_pos_short
    ):
        return {
            "upper_wick": shape.upper_wick,
            "body": shape.body,
            "wick_body_ratio": round(wick_body, 3),
            "wick_range_ratio": round(wick_range, 3),
            "close_pos": round(shape.close_pos, 3),
        }
    return None


def bullish_engulfing(shape: BarShape, prev: BarShape | None, cfg: PatternConfig) -> dict | None:
    if prev is None or not prev.bearish or not shape.bullish:
        return None
    if not (shape.close > prev.open and shape.open < prev.close):
        return None
    if shape.body < prev.body * cfg.engulf_body_ratio:
        return None
    return {
        "prev_body": prev.body,
        "body": shape.body,
        "body_ratio": round(_ratio(shape.body, prev.body), 3),
        "engulfed_from": prev.close,
        "engulfed_to": prev.open,
    }


def bearish_engulfing(shape: BarShape, prev: BarShape | None, cfg: PatternConfig) -> dict | None:
    if prev is None or not prev.bullish or not shape.bearish:
        return None
    if not (shape.close < prev.open and shape.open > prev.close):
        return None
    if shape.body < prev.body * cfg.engulf_body_ratio:
        return None
    return {
        "prev_body": prev.body,
        "body": shape.body,
        "body_ratio": round(_ratio(shape.body, prev.body), 3),
        "engulfed_from": prev.close,
        "engulfed_to": prev.open,
    }


def piercing_line(shape: BarShape, prev: BarShape | None, cfg: PatternConfig) -> dict | None:
    """Opens below a down bar's close, closes back above its midpoint (not past its open)."""
    if prev is None or not prev.bearish or not shape.bullish:
        return None
    midpoint = (prev.open + prev.close) / 2
    if shape.open < prev.close and midpoint <= shape.close < prev.open:
        return {
            "prev_midpoint": midpoint,
            "penetration": round(_ratio(shape.close - prev.close, prev.body), 3),
        }
    return None


def dark_cloud_cover(shape: BarShape, prev: BarShape | None, cfg: PatternConfig) -> dict | None:
    """Opens above an up bar's close, closes back below its midpoint (not past its open)."""
    if prev is None or not prev.bullish or not shape.bearish:
        return None
    midpoint = (prev.open + prev.close) / 2
    if shape.open > prev.close and prev.open < shape.close <= midpoint:
        return {
            "prev_midpoint": midpoint,
            "penetration": round(_ratio(prev.close - shape.close, prev.body), 3),
        }
    return None


Detector = Callable[[BarShape, "BarShape | None", PatternConfig], "dict | None"]

# name -> (detector, direction).  Add a row here and it shows up everywhere.
REGISTRY: dict[str, tuple[Detector, str]] = {
    "long_lower_tail": (long_lower_tail, LONG),
    "long_upper_tail": (long_upper_tail, SHORT),
    "bullish_engulfing": (bullish_engulfing, LONG),
    "bearish_engulfing": (bearish_engulfing, SHORT),
    "piercing_line": (piercing_line, LONG),
    "dark_cloud_cover": (dark_cloud_cover, SHORT),
}

PATTERN_NAMES = tuple(REGISTRY)


def detect(
    bar,
    prev_bar,
    *,
    box_type: str,
    hourly_atr: float | None = None,
    config: PatternConfig | None = None,
) -> list[dict]:
    """Every pattern matching ``box_type``'s direction that fires on ``bar``.

    Each hit is ``{pattern, direction, detail, ...geometry}``, ready to merge into
    a ``reversal_events`` row.  Returns ``[]`` when the bar is too small, when the
    side is unknown, or when nothing matches.
    """
    cfg = config or PatternConfig()
    wanted = SIDE_DIRECTION.get(box_type)
    if wanted is None:
        return []

    shape = BarShape.of(bar)
    if shape.range <= 0:
        return []

    range_atr = None
    if hourly_atr and hourly_atr > 0 and pd.notna(hourly_atr):
        range_atr = shape.range / float(hourly_atr)
        if range_atr < cfg.min_range_atr:
            return []

    prev_shape = BarShape.of(prev_bar) if prev_bar is not None else None

    hits: list[dict] = []
    for name, (detector, direction) in REGISTRY.items():
        if direction != wanted:
            continue
        detail = detector(shape, prev_shape, cfg)
        if detail is None:
            continue
        hits.append(
            {
                "pattern": name,
                "direction": direction,
                "detail": detail,
                "upper_wick": shape.upper_wick,
                "lower_wick": shape.lower_wick,
                "body": shape.body,
                "close_pos": shape.close_pos,
                "range_atr": range_atr,
            }
        )
    return hits
