"""The Pine "Support and Resistance (High Volume Boxes)" indicator, as pure math.

Ported from ``src/snr_1_hour_chart.py`` (itself a copy of
``snr_strategy/src/pine_snr_boxes_1h_chart.py``) with the three sibling-repo
imports inlined, so this module depends on nothing but pandas/numpy/plotly and
can be unit-tested offline.  Box geometry follows ``doc/sr_breaks_resistance_pine_script.txt``:

* A support box is born on the bar that confirms a ``ta.pivotlow(close, L, L)``
  whose delta volume clears ``ta.highest(delta/2.5, vol_len)``; its top is the
  pivot close and its bottom is ``pivot - atr * box_width``.  Resistance mirrors
  it (``ta.pivothigh`` + delta volume under ``ta.lowest(delta/2.5, vol_len)``) and
  extends *upward*.
* The left edge sits ``lookback`` bars back from the confirming bar, matching
  ``chart.point.from_index(bar_index-lookbackPeriod, ...)``.
* Pine holds a single ``var box`` per side and only ever calls ``set_right`` on it,
  so a box's right edge freezes the moment the next box of the same side is born.
* ``brekout_sup``/``sup_holds`` only restyle the newest box of a side, so the
  recorded state belongs to whichever box was current when the cross happened.

The one addition over the Pine source is :func:`collect_snr_boxes`'s
``creation_limit``, which lets a live caller feed in an unfinished bar: Pine only
creates boxes on confirmed bars, but the crosses and the right edge still need to
advance across the forming one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# --------------------------------------------------------------------------------------
# Indicator defaults
# --------------------------------------------------------------------------------------
DEFAULT_LOOKBACK = 20
DEFAULT_VOLUME_LENGTH = 2
DEFAULT_BOX_WIDTH = 1.0
PINE_ATR_LENGTH = 200  # the indicator's own ta.atr(200)
# TMF's session-anchored hourly bars are only ~19 per trading day, where ATR(200)
# would need half a quarter of warm-up, so the TMF backtests use 20 and we follow.
DEFAULT_ATR_LENGTH = 20

SUPPORT_COLOR = "#20ca26"  # Pine's green
RESISTANCE_COLOR = "#e92929"  # Pine's red
SUPPORT_FILL = "rgba(32, 202, 38, 0.18)"
RESISTANCE_FILL = "rgba(233, 41, 41, 0.18)"
BROKEN_SUPPORT_FILL = "rgba(233, 41, 41, 0.10)"  # Pine repaints a broken support red
BROKEN_RESISTANCE_FILL = "rgba(32, 202, 38, 0.10)"  # ... and a broken resistance green

BOX_COLUMNS = [
    "box_id",
    "box_type",
    "state",
    "top",
    "bottom",
    "pivot_price",
    "atr_width",
    "delta_volume",
    "left_time",
    "right_time",
    "created_time",
    "state_time",
    "pivot_index",
    "created_index",
    "holds",
    "breakouts",
]

OHLCV_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


# --------------------------------------------------------------------------------------
# Pine-compatible primitives
# --------------------------------------------------------------------------------------
def pine_rma(values: pd.Series, length: int) -> pd.Series:
    """Pine-compatible RMA: SMA seed followed by Wilder's recursive average.

    Deliberately *not* ``ewm(alpha=1/length)``: pandas seeds the recursion from the
    first observation, while Pine seeds it from an SMA of the first ``length``
    values.  The two diverge for hundreds of bars, so the loop stays.
    """
    if length < 1:
        raise ValueError("length must be at least 1")
    source = pd.to_numeric(values, errors="coerce").astype(float)
    output = pd.Series(np.nan, index=source.index, dtype=float)
    if len(source) < length:
        return output
    for index in range(length - 1, len(source)):
        window = source.iloc[index - length + 1 : index + 1]
        if window.notna().all():
            output.iloc[index] = float(window.mean())
            start = index + 1
            break
    else:
        return output
    for index in range(start, len(source)):
        value = source.iloc[index]
        previous = output.iloc[index - 1]
        if pd.isna(value) or pd.isna(previous):
            continue
        output.iloc[index] = (previous * (length - 1) + value) / length
    return output


def pine_atr(bars: pd.DataFrame, length: int) -> pd.Series:
    previous_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - previous_close).abs(),
            (bars["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return pine_rma(true_range, length)


def delta_volume(bars: pd.DataFrame) -> pd.Series:
    """Pine's ``upAndDownVolume()``: signed volume, direction held on doji bars."""
    is_buy = True
    values: list[float] = []
    for bar in bars.itertuples(index=False):
        if bar.close > bar.open:
            is_buy = True
        elif bar.close < bar.open:
            is_buy = False
        values.append(float(bar.volume) if is_buy else -float(bar.volume))
    return pd.Series(values, index=bars.index, dtype=float)


def _crossed_over(current_a, previous_a, current_b, previous_b) -> bool:
    values = (current_a, previous_a, current_b, previous_b)
    return bool(
        all(pd.notna(value) for value in values)
        and current_a > current_b
        and previous_a <= previous_b
    )


def _crossed_under(current_a, previous_a, current_b, previous_b) -> bool:
    values = (current_a, previous_a, current_b, previous_b)
    return bool(
        all(pd.notna(value) for value in values)
        and current_a < current_b
        and previous_a >= previous_b
    )


def _right_edge(timestamps: pd.Series, index: int) -> pd.Timestamp:
    """Pine's ``set_right(bar_index+1)``; one bar past the end is extrapolated."""
    if index < len(timestamps):
        return timestamps.iloc[index]
    last = timestamps.iloc[-1]
    step = last - timestamps.iloc[-2] if len(timestamps) > 1 else pd.Timedelta(hours=1)
    return last + step


# --------------------------------------------------------------------------------------
# Box history
# --------------------------------------------------------------------------------------
def collect_snr_boxes(
    bars: pd.DataFrame,
    lookback: int = DEFAULT_LOOKBACK,
    volume_length: int = DEFAULT_VOLUME_LENGTH,
    atr_length: int = DEFAULT_ATR_LENGTH,
    box_width: float = DEFAULT_BOX_WIDTH,
    creation_limit: int | None = None,
) -> pd.DataFrame:
    """Every support/resistance box the Pine indicator would draw over ``bars``.

    Returns one row per box with :data:`BOX_COLUMNS`.  ``state`` is the box's final
    styling (``fresh``/``holds``/``broken``), and ``holds``/``breakouts`` count how
    many times each cross fired while the box was the current one for its side.

    ``creation_limit`` caps the bar index that may *create* a box.  Pass
    ``len(closed_bars) - 1`` when the last row is an unfinished bar: Pine confirms
    boxes only on closed bars, but the break/hold crosses and the right edge still
    have to advance across the forming one.  ``None`` means no cap.
    """
    if lookback < 1 or volume_length < 1 or atr_length < 1:
        raise ValueError("lookback, volume_length, and atr_length must be positive")
    if box_width < 0:
        raise ValueError("box_width must be non-negative")

    frame = bars.sort_values("timestamp", kind="stable").reset_index(drop=True)
    missing = set(OHLCV_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError(f"Missing hourly columns: {sorted(missing)}")

    timestamps = frame["timestamp"]
    delta = delta_volume(frame)
    atr = pine_atr(frame, atr_length)
    scaled = delta / 2.5
    volume_high = scaled.rolling(volume_length, min_periods=volume_length).max()
    volume_low = scaled.rolling(volume_length, min_periods=volume_length).min()
    closes = frame["close"]

    boxes: list[dict] = []
    support: dict | None = None
    resistance: dict | None = None
    # Pine's ``supportLevel``/``supportLevel_1`` persist across bars, and the crosses
    # compare today's high/low against yesterday's level, so the levels are tracked
    # separately from the box records.
    support_top = support_bottom = np.nan
    resistance_bottom = resistance_top = np.nan
    previous_support_top = previous_support_bottom = np.nan
    previous_resistance_bottom = previous_resistance_top = np.nan

    def _open_box(box_type: str, index: int, pivot_index: int, top: float, bottom: float) -> dict:
        record = {
            "box_id": len(boxes) + 1,
            "box_type": box_type,
            "state": "fresh",
            "top": top,
            "bottom": bottom,
            "pivot_price": top if box_type == "support" else bottom,
            "atr_width": top - bottom,
            "delta_volume": float(delta.iloc[index]),
            "left_time": timestamps.iloc[pivot_index],
            "right_time": _right_edge(timestamps, index + 1),
            "created_time": timestamps.iloc[index],
            "state_time": pd.NaT,
            "pivot_index": pivot_index,
            "created_index": index,
            "holds": 0,
            "breakouts": 0,
        }
        boxes.append(record)
        return record

    may_create_until = len(frame) - 1 if creation_limit is None else creation_limit

    for index in range(len(frame)):
        # --- box creation (Pine's two ``if`` blocks) ---------------------------------
        current_atr = atr.iloc[index]
        if index >= 2 * lookback and index <= may_create_until and pd.notna(current_atr):
            pivot_index = index - lookback
            window = closes.iloc[pivot_index - lookback : pivot_index + lookback + 1]
            pivot_close = float(closes.iloc[pivot_index])
            current_delta = float(delta.iloc[index])
            width = float(current_atr) * box_width
            high_filter = volume_high.iloc[index]
            low_filter = volume_low.iloc[index]

            if (
                pd.notna(high_filter)
                and pivot_close == float(window.min())
                and current_delta > float(high_filter)
            ):
                support_top = pivot_close
                support_bottom = pivot_close - width
                support = _open_box("support", index, pivot_index, support_top, support_bottom)

            if (
                pd.notna(low_filter)
                and pivot_close == float(window.max())
                and current_delta < float(low_filter)
            ):
                resistance_bottom = pivot_close
                resistance_top = pivot_close + width
                resistance = _open_box(
                    "resistance", index, pivot_index, resistance_top, resistance_bottom
                )

        # --- adaptive right edge (Pine's ``set_right(bar_index+1)``) -----------------
        if support is not None:
            support["right_time"] = _right_edge(timestamps, index + 1)
        if resistance is not None:
            resistance["right_time"] = _right_edge(timestamps, index + 1)

        # --- break / hold crosses ---------------------------------------------------
        if index > 0:
            current_low = float(frame.at[index, "low"])
            current_high = float(frame.at[index, "high"])
            previous_low = float(frame.at[index - 1, "low"])
            previous_high = float(frame.at[index - 1, "high"])

            breakout_resistance = _crossed_over(
                current_low, previous_low, resistance_top, previous_resistance_top
            )
            resistance_holds = _crossed_under(
                current_high, previous_high, resistance_bottom, previous_resistance_bottom
            )
            support_holds = _crossed_over(
                current_low, previous_low, support_top, previous_support_top
            )
            breakout_support = _crossed_under(
                current_high, previous_high, support_bottom, previous_support_bottom
            )

            if support is not None:
                if breakout_support:
                    support["state"] = "broken"
                    support["state_time"] = timestamps.iloc[index]
                    support["breakouts"] += 1
                if support_holds:
                    support["state"] = "holds"
                    support["state_time"] = timestamps.iloc[index]
                    support["holds"] += 1
            if resistance is not None:
                if breakout_resistance:
                    resistance["state"] = "broken"
                    resistance["state_time"] = timestamps.iloc[index]
                    resistance["breakouts"] += 1
                if resistance_holds:
                    resistance["state"] = "holds"
                    resistance["state_time"] = timestamps.iloc[index]
                    resistance["holds"] += 1

        previous_support_top, previous_support_bottom = support_top, support_bottom
        previous_resistance_bottom = resistance_bottom
        previous_resistance_top = resistance_top

    return pd.DataFrame(boxes, columns=BOX_COLUMNS)


def clip_boxes(boxes: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Keep boxes overlapping ``[start, end]`` and clamp their edges to that window."""
    if boxes.empty:
        return boxes
    kept = boxes[(boxes["right_time"] >= start) & (boxes["left_time"] <= end)].copy()
    kept["left_time"] = kept["left_time"].clip(lower=start)
    kept["right_time"] = kept["right_time"].clip(upper=end)
    return kept.reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Live helpers: where is price relative to a box?
# --------------------------------------------------------------------------------------
def box_height(box) -> float:
    return float(box["top"]) - float(box["bottom"])


def zone_pct(price: float, box) -> float:
    """Where ``price`` sits inside ``box``: 0.0 at the bottom edge, 1.0 at the top.

    Not clamped -- values outside [0, 1] mean price is outside the box, and the
    sign tells you which way, which is useful for the exit buffer.
    """
    height = box_height(box)
    if height <= 0:
        return float("nan")
    return (float(price) - float(box["bottom"])) / height


def bar_in_box(bar, box) -> bool:
    """True when the bar's [low, high] range overlaps the box.

    Deliberately a range test rather than a close test: a 1-minute bar that only
    wicks into the zone and snaps back out is exactly the moment worth recording,
    and a close-only test would miss it entirely.  Touching an edge counts.
    """
    return bool(float(bar["low"]) <= float(box["top"]) and float(bar["high"]) >= float(box["bottom"]))


def current_box(boxes: pd.DataFrame, box_type: str):
    """The newest not-yet-broken box of ``box_type``, or None.

    Pine itself keeps only one live box per side; a box whose level has been
    crossed (``state == "broken"``) is no longer a zone worth watching, so it is
    skipped and the next-newest intact box takes over.
    """
    if boxes is None or boxes.empty:
        return None
    subset = boxes[(boxes["box_type"] == box_type) & (boxes["state"] != "broken")]
    if subset.empty:
        return None
    return subset.loc[subset["box_id"].idxmax()]


# --------------------------------------------------------------------------------------
# Plotly figure
# --------------------------------------------------------------------------------------
def session_rangebreaks(
    frame: pd.DataFrame, bar_span: pd.Timedelta = pd.Timedelta(hours=1)
) -> list[dict]:
    """Plotly ``rangebreaks`` covering the stretches between sessions.

    Derived from the gaps between consecutive *plotted* timestamps rather than from
    a fixed hourly grid, because TMF's two sessions sit on different phases: the day
    session is anchored at 08:45 so its bars land on :45, while the night session is
    anchored at 15:00 and lands on :00.  A grid built off either phase would hide the
    other session's bars.  Each break is inset by half a bar so no candle is clipped,
    and breaks are grouped by duration since ``dvalue`` is per rangebreak.
    """
    if frame.empty or "timestamp" not in frame.columns:
        return []
    stamps = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"]).unique()).sort_values()
    if len(stamps) < 2:
        return []

    grouped: dict[int, list[pd.Timestamp]] = {}
    for previous, following in zip(stamps[:-1], stamps[1:]):
        delta = following - previous
        if delta <= bar_span * 1.5:  # a normal bar-to-bar step, not a session gap
            continue
        width = int((delta - bar_span) / pd.Timedelta(milliseconds=1))
        grouped.setdefault(width, []).append(previous + bar_span / 2)
    return [dict(values=values, dvalue=width) for width, values in sorted(grouped.items())]


def _box_style(box: pd.Series) -> tuple[str, str, str]:
    """``(border_color, fill_color, dash)`` following the Pine recolour rules."""
    broken = box["state"] == "broken"
    if box["box_type"] == "support":
        border = RESISTANCE_COLOR if broken else SUPPORT_COLOR
        fill = BROKEN_SUPPORT_FILL if broken else SUPPORT_FILL
    else:
        border = SUPPORT_COLOR if broken else RESISTANCE_COLOR
        fill = BROKEN_RESISTANCE_FILL if broken else RESISTANCE_FILL
    return border, fill, "dash" if broken else "solid"


def _add_boxes(fig: go.Figure, boxes: pd.DataFrame, box_type: str) -> int:
    subset = boxes[boxes["box_type"] == box_type]
    legend_name = f"{box_type} boxes"
    first = True
    for _, box in subset.iterrows():
        border, fill, dash = _box_style(box)
        left, right = box["left_time"], box["right_time"]
        top, bottom = float(box["top"]), float(box["bottom"])
        hover = (
            f"<b>{box_type} #{int(box['box_id'])}</b> ({box['state']})<br>"
            f"top={top:,.2f}<br>bottom={bottom:,.2f}<br>"
            f"width={top - bottom:,.2f}<br>"
            f"Vol: {box['delta_volume']:,.2f}<br>"
            f"created={box['created_time']:%Y-%m-%d %H:%M}"
            "<extra></extra>"
        )
        fig.add_trace(
            go.Scatter(
                x=[left, right, right, left, left],
                y=[top, top, bottom, bottom, top],
                mode="lines",
                fill="toself",
                fillcolor=fill,
                line=dict(color=border, width=1, dash=dash),
                legendgroup=box_type,
                name=legend_name,
                showlegend=first,
                hoveron="fills",
                hoverlabel=dict(bgcolor=border),
                hovertemplate=hover,
                text=legend_name,
            ),
            row=1,
            col=1,
        )
        first = False
    return len(subset)


def _add_reversal_markers(fig: go.Figure, reversals: pd.DataFrame) -> None:
    """Triangles where a reversal pattern fired, split by direction."""
    if reversals is None or reversals.empty:
        return
    for direction, symbol, color, offset in (
        ("long", "triangle-up", SUPPORT_COLOR, -1),
        ("short", "triangle-down", RESISTANCE_COLOR, 1),
    ):
        subset = reversals[reversals["direction"] == direction]
        if subset.empty:
            continue
        # Park the marker just outside the bar so it never hides the candle body.
        anchor = subset["bar_low"] if direction == "long" else subset["bar_high"]
        nudge = (subset["bar_high"] - subset["bar_low"]).abs().fillna(0.0) * 0.25
        fig.add_trace(
            go.Scatter(
                x=pd.to_datetime(subset["hour_start"]),
                y=anchor + offset * nudge,
                mode="markers",
                marker=dict(symbol=symbol, size=12, color=color, line=dict(width=1, color="#ffffff")),
                name=f"{direction} reversal",
                customdata=subset[["pattern", "box_type", "zone_pct"]].to_numpy(),
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>box=%{customdata[1]}<br>"
                    "zone_pct=%{customdata[2]:.2f}<br>%{x}<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )


def build_figure(
    frame: pd.DataFrame,
    boxes: pd.DataFrame,
    *,
    title: str,
    volume_length: int = DEFAULT_VOLUME_LENGTH,
    instrument: str = "price",
    reversals: pd.DataFrame | None = None,
) -> go.Figure:
    """Candles + every box, with the delta-volume filter that gates box creation."""
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.76, 0.24],
        vertical_spacing=0.04,
        subplot_titles=("", "delta volume / 2.5 vs filters"),
    )

    _add_boxes(fig, boxes, "support")
    _add_boxes(fig, boxes, "resistance")

    fig.add_trace(
        go.Candlestick(
            x=frame["timestamp"],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name=f"{instrument} 1h",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1,
        col=1,
    )

    _add_reversal_markers(fig, reversals)

    scaled = delta_volume(frame) / 2.5
    fig.add_trace(
        go.Bar(
            x=frame["timestamp"],
            y=scaled,
            name="delta volume / 2.5",
            marker_color=np.where(scaled >= 0, "#26a69a", "#ef5350"),
            opacity=0.65,
            hovertemplate="delta/2.5=%{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=scaled.rolling(volume_length, min_periods=volume_length).max(),
            mode="lines",
            line=dict(color=SUPPORT_COLOR, width=1),
            name="support filter (highest)",
            hovertemplate="support filter=%{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=scaled.rolling(volume_length, min_periods=volume_length).min(),
            mode="lines",
            line=dict(color=RESISTANCE_COLOR, width=1),
            name="resistance filter (lowest)",
            hovertemplate="resistance filter=%{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    fig.update_layout(
        title=title,
        template="plotly_dark",
        hovermode="closest",
        title_y=0.97,
        title_yanchor="top",
        # The two-line title plus the horizontal legend both live above the plot area.
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(l=40, r=20, t=150, b=40),
        bargap=0.1,
    )
    rangebreaks = session_rangebreaks(frame)
    fig.update_xaxes(rangeslider_visible=False, rangebreaks=rangebreaks, row=1, col=1)
    fig.update_xaxes(rangeslider_visible=False, rangebreaks=rangebreaks, row=2, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="delta vol / 2.5", row=2, col=1)
    return fig
