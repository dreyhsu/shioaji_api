"""Hand-drawn trendlines: geometry in the source chart's bar-index space.

A drawing is a list of anchors ``{"time": epoch, "price": float}`` clicked on
the chart, plus a ``kind``:

* ``"line"`` -- two anchors make a straight line; three or more a polyline.
  Past the last anchor the last segment's slope carries on, like a TradingView
  trendline with "extend right".
* ``"curve"`` -- TradingView's *Curve* tool: exactly three anchors, a quadratic
  Bezier from start to end whose middle anchor is the *control point*.  The
  curve passes through start and end but only bends toward the control point.
  The control must sit strictly between start and end in time, which keeps the
  curve a function of bar index (one value per bar), so it can be drawn as a
  line series and compared against price.  Past the end it carries on along its end
  tangent (control -> end), so a curve drawn up to the current bar keeps
  projecting forward for the alert.

The maths runs in *bar-index* space, not wall-clock time.  lightweight-charts'
horizontal axis is a logical index -- the session breaks take up no space -- so
a line that looks straight on screen is linear in bar index.  Interpolating in
wall time would bend it across every 13:45-15:00 and 05:00-08:45 gap, and the
alert would disagree with what the chart shows.

Anchor ``time`` follows the chart's convention (``dashboard._epoch``): a naive
Taipei stamp read as UTC, so ``pd.to_datetime(epoch, unit="s")`` gives back the
original naive ``hour_start``.

Zero shioaji dependency, like ``patterns.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import session_bars as sb


def _anchor_stamp(epoch) -> pd.Timestamp:
    return pd.to_datetime(int(epoch), unit="s")


KINDS = ("line", "curve")
TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "1h": 60}


def source_bars(minutes: pd.DataFrame, timeframe: str = "1h") -> pd.DataFrame:
    interval = TIMEFRAME_MINUTES[timeframe]
    return (sb.minute_bars_to_hourly(minutes) if interval == 60
            else sb.minute_bars_to_interval(minutes, interval))


def _starts(bars: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(bars["hour_start" if "hour_start" in bars else "timestamp"])


def values_at_times(bars, anchors, stamps, kind="curve", timeframe="1h") -> pd.Series:
    """Project source geometry onto timestamps without bridging missing buckets.

    A minute close is compared at its minute-open timestamp, matching the chart.
    Session breaks consume no source indices; incomplete source buckets are
    usable geometry, while the signal itself still requires closed 1m candles.
    """
    stamps = pd.DatetimeIndex(stamps)
    out = np.full(len(stamps), np.nan)
    points = anchor_indices(bars, anchors, kind)
    if not points:
        return pd.Series(out)
    starts = _starts(bars)
    indices = starts.get_indexer(stamps, method="pad")
    duration = pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    for position, (stamp, index) in enumerate(zip(stamps, indices)):
        if index < 0:
            continue
        fraction = (stamp - starts[index]) / duration
        if 0 <= fraction < 1:
            value = value_at(points, index + fraction, kind)
            out[position] = np.nan if value is None else value
    return pd.Series(out)


def anchor_indices(
    hourly: pd.DataFrame, anchors: list[dict], kind: str = "line"
) -> list[tuple[int, float]] | None:
    """``[(bar_index, price), ...]`` sorted by index, or None if unresolvable.

    An anchor must land exactly on a bar of ``hourly`` -- the chart only lets you
    click on bars -- so one that has scrolled out of the loaded window (older than
    the first bar) or names a bar that never existed makes the whole line None
    rather than silently snapping to a neighbour and tilting it.
    """
    if hourly.empty or len(anchors) < 2:
        return None
    if kind == "curve" and len(anchors) != 3:
        return None
    starts = _starts(hourly).to_numpy()
    points = []
    for anchor in anchors:
        stamp = np.datetime64(_anchor_stamp(anchor["time"]))
        index = int(np.searchsorted(starts, stamp, side="left"))
        if index >= len(starts) or starts[index] != stamp:
            return None
        points.append((index, float(anchor["price"])))
    points.sort(key=lambda point: point[0])
    # Two anchors on the same bar give a vertical segment with no slope.
    if len({index for index, _ in points}) != len(points):
        return None
    return points


def default_label(line: dict) -> str:
    noun = "曲線" if line.get("kind") == "curve" else "趨勢線"
    return f"{noun} #{line['id']}"


def _bezier_at(points: list[tuple[int, float]], index: float) -> float:
    """Quadratic Bezier value at ``index`` within [start, end].

    x(t) = (1-t)^2 x0 + 2(1-t)t x1 + t^2 x2 is monotonic because x0 < x1 < x2, so
    there is exactly one t in [0, 1] per index: solve a t^2 + b t + c = 0.
    """
    (x0, y0), (x1, y1), (x2, y2) = points
    a = x0 - 2 * x1 + x2
    b = 2 * (x1 - x0)
    c = x0 - index
    if abs(a) < 1e-12:
        t = -c / b
    else:
        # The "+" root is the one in [0, 1] whenever b > 0, i.e. x1 > x0.
        t = (-b + np.sqrt(max(b * b - 4 * a * c, 0.0))) / (2 * a)
    t = min(max(t, 0.0), 1.0)
    return (1 - t) ** 2 * y0 + 2 * (1 - t) * t * y1 + t**2 * y2


def value_at(
    points: list[tuple[int, float]], index: int, kind: str = "line"
) -> float | None:
    """Value at a bar index: interpolated, extended right, None before start."""
    if not points or index < points[0][0]:
        return None
    if kind == "curve":
        (_, _), (x1, y1), (x2, y2) = points
        if index <= x2:
            return float(_bezier_at(points, index))
        # A Bezier's end tangent points from the control point to the end.
        return y2 + (y2 - y1) * (index - x2) / (x2 - x1)
    for (i0, p0), (i1, p1) in zip(points, points[1:]):
        if index <= i1:
            return p0 + (p1 - p0) * (index - i0) / (i1 - i0)
    # Past the last anchor: keep going along the last segment.
    (i0, p0), (i1, p1) = points[-2], points[-1]
    return p1 + (p1 - p0) * (index - i1) / (i1 - i0)


def line_values(hourly: pd.DataFrame, anchors: list[dict], kind: str = "line") -> pd.Series:
    """The drawing at every bar of ``hourly`` (NaN before the first anchor)."""
    out = pd.Series(np.nan, index=hourly.index, dtype=float)
    points = anchor_indices(hourly, anchors, kind)
    if points is None:
        return out
    for position in range(points[0][0], len(hourly)):
        out.iloc[position] = value_at(points, position, kind)
    return out

