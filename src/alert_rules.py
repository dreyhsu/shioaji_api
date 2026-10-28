"""Which Telegram alerts the latest 1-minute bar has earned.

Six rules, each checked on the newest minute's close and each switched on and
tuned from ``config/alerts.json``:

* ``support_bottom_third``   -- close inside the live support box, in its bottom
  ``zone_max`` (default 1/3).
* ``resistance_top_third``   -- close inside the live resistance box, in its top
  part (``zone_pct >= zone_min``, default 2/3).
* ``box_in_out``             -- the previous minute closed inside a live box and
  this minute closed outside it, either way: a support box held (up) or broke
  (down), a resistance box broke (up) or held (down).  Close-to-close only, no
  exit buffer, so it fires on the very minute the close leaves the box.
* ``trendline_near``         -- close within ``distance_points`` (or
  ``distance_atr`` x hourly ATR) of a drawn line.
* ``trendline_break_below_up``   -- close below a *rising* line.  Once per line.
* ``trendline_break_above_down`` -- close above a *falling* line.  Once per line.

A line's value and slope are read at the newest hourly bar (normally the forming
one), the same bar-index maths the chart draws with, so past its end point a
line carries on along its end tangent.

This module only decides; throttling (the cooldown and the once-per-line rule)
lives in ``monitor.send_rule`` because it needs the alert history in the DB.

Zero shioaji dependency, like ``patterns.py``.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

import snr_core as snr
import telegram_notify as tg
import trendline as tl

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "alerts.json"

RULES = (
    "support_bottom_third",
    "resistance_top_third",
    "box_in_out",
    "trendline_near",
    "trendline_break_below_up",
    "trendline_break_above_down",
)
ONCE_RULES = ("trendline_break_below_up", "trendline_break_above_down")

DEFAULTS: dict[str, Any] = {
    "telegram_enabled": True,
    "cooldown_minutes": 10,
    "support_bottom_third": {"enabled": True, "zone_max": 1 / 3},
    "resistance_top_third": {"enabled": True, "zone_min": 2 / 3},
    "box_in_out": {"enabled": True},
    "trendline_near": {
        "enabled": True,
        "kinds": ["curve", "line"],
        "distance_points": 30,
        "distance_atr": None,
    },
    "trendline_break_below_up": {"enabled": True, "kinds": ["curve", "line"], "buffer_points": 0},
    "trendline_break_above_down": {"enabled": True, "kinds": ["curve", "line"], "buffer_points": 0},
}


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------
def merge_config(user: dict | None) -> dict:
    """``user`` laid over :data:`DEFAULTS`; a missing key keeps its default."""
    merged = copy.deepcopy(DEFAULTS)
    for key, value in (user or {}).items():
        if key.startswith("_"):
            continue  # "_comment" style keys are documentation
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    if not isinstance(merged["telegram_enabled"], bool):
        raise ValueError("telegram_enabled must be a JSON boolean (true or false)")
    return merged


class AlertConfig:
    """The JSON config, re-read whenever the file's mtime changes.

    Tuning a threshold should not need a monitor restart, and a typo in the file
    should not take alerts down: a file that fails to parse keeps the last good
    config and reports the problem through ``last_error``.
    """

    def __init__(self, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        self.path = Path(path)
        self._mtime: float | None = None
        self.data: dict = merge_config(None)
        self.last_error: str | None = None
        self.reload()

    def reload(self) -> bool:
        """Re-read if the file changed; returns whether a new config was loaded."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._mtime is not None or self.last_error is None:
                self.last_error = f"{self.path} not found; using defaults"
            self._mtime = None
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            self.data = merge_config(json.loads(self.path.read_text(encoding="utf-8")))
            self.last_error = None
            return True
        except (OSError, ValueError) as error:
            self.last_error = f"{self.path}: {error}; keeping previous config"
            return False

    @property
    def telegram_enabled(self) -> bool:
        return self.data["telegram_enabled"]

    def rule(self, name: str) -> dict:
        return self.data.get(name) or {}

    def enabled(self, name: str) -> bool:
        return bool(self.rule(name).get("enabled", False))

    def cooldown_minutes(self, name: str) -> float:
        own = self.rule(name).get("cooldown_minutes")
        return float(own if own is not None else self.data.get("cooldown_minutes", 10))


# --------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------
@dataclass
class Candidate:
    rule: str
    key: str  # what the cooldown / once-only rule is counted against
    once: bool
    message: str


def _box_rules(code, close, minute_start, side, box, config: AlertConfig) -> list[Candidate]:
    if box is None:
        return []
    pct = snr.zone_pct(close, box)
    if pd.isna(pct):
        return []
    if side == "support":
        rule = "support_bottom_third"
        hit = config.enabled(rule) and 0.0 <= pct <= float(config.rule(rule)["zone_max"])
    else:
        rule = "resistance_top_third"
        hit = config.enabled(rule) and float(config.rule(rule)["zone_min"]) <= pct <= 1.0
    if not hit:
        return []
    box_id = int(box["box_id"])
    return [
        Candidate(
            rule=rule,
            key=f"{side}:{box_id}",
            once=False,
            message=tg.format_zone_alert(
                code=code,
                box_type=side,
                box_id=box_id,
                box_top=float(box["top"]),
                box_bottom=float(box["bottom"]),
                price=close,
                zone_pct=float(pct),
                minute_start=minute_start,
            ),
        )
    ]


def _in_box(price: float, box) -> bool:
    return float(box["bottom"]) <= price <= float(box["top"])


def _exit_rules(code, close, prev_close, minute_start, boxes, config: AlertConfig) -> list[Candidate]:
    rule = "box_in_out"
    if prev_close is None or not config.enabled(rule):
        return []
    out = []
    for side in ("support", "resistance"):
        box = boxes.get(side)
        if box is None or not _in_box(prev_close, box) or _in_box(close, box):
            continue
        top, bottom = float(box["top"]), float(box["bottom"])
        box_id = int(box["box_id"])
        out.append(
            Candidate(
                rule=rule,
                key=f"{side}:{box_id}",
                once=False,
                message=tg.format_box_exit_alert(
                    code=code,
                    box_type=side,
                    box_id=box_id,
                    box_top=top,
                    box_bottom=bottom,
                    price=close,
                    direction="up" if close > top else "down",
                    minute_start=minute_start,
                ),
            )
        )
    return out


def line_now(hourly: pd.DataFrame, line: dict) -> tuple[float, float] | None:
    """``(value, slope_per_bar)`` of a drawn line at the newest hourly bar."""
    if hourly is None or len(hourly) < 2:
        return None
    kind = line.get("kind") or "line"
    points = tl.anchor_indices(hourly, line["anchors"], kind)
    if points is None:
        return None
    n = len(hourly) - 1
    value = tl.value_at(points, n, kind)
    previous = tl.value_at(points, n - 1, kind)
    if value is None:
        return None
    if previous is None:
        # The line starts on this very bar: slope from its own next step.
        following = tl.value_at(points, n + 1, kind)
        return float(value), float(following - value)
    return float(value), float(value - previous)


def _trendline_rules(code, close, minute_start, hourly, trendlines, hourly_atr, config) -> list[Candidate]:
    out = []
    for line in trendlines or []:
        if line.get("timeframe", "1h") != "1h":
            continue
        kind = line.get("kind") or "line"
        now = line_now(hourly, line)
        if now is None:
            continue
        value, slope = now
        line_id = int(line["id"])
        label = line.get("label") or tl.default_label(line)
        key = f"line:{line_id}"

        def emit(rule, event, once):
            out.append(
                Candidate(
                    rule=rule,
                    key=key,
                    once=once,
                    message=tg.format_line_alert(
                        code=code,
                        label=label,
                        event=event,
                        price=close,
                        line_value=value,
                        slope=slope,
                        minute_start=minute_start,
                    ),
                )
            )

        rule = "trendline_near"
        settings = config.rule(rule)
        if config.enabled(rule) and kind in settings.get("kinds", []):
            threshold = settings.get("distance_points")
            if settings.get("distance_atr") is not None and hourly_atr:
                threshold = float(settings["distance_atr"]) * float(hourly_atr)
            if threshold is not None and abs(close - value) <= float(threshold):
                emit(rule, "near", False)

        rule = "trendline_break_below_up"
        settings = config.rule(rule)
        if (
            config.enabled(rule)
            and kind in settings.get("kinds", [])
            and slope > 0
            and close < value - float(settings.get("buffer_points") or 0)
        ):
            emit(rule, "break_below_up", True)

        rule = "trendline_break_above_down"
        settings = config.rule(rule)
        if (
            config.enabled(rule)
            and kind in settings.get("kinds", [])
            and slope < 0
            and close > value + float(settings.get("buffer_points") or 0)
        ):
            emit(rule, "break_above_down", True)
    return out


def evaluate(
    *,
    code: str,
    minute_bar,
    boxes: dict,
    prev_minute_bar=None,
    hourly: pd.DataFrame,
    trendlines: list[dict],
    hourly_atr: float | None,
    config: AlertConfig,
) -> list[Candidate]:
    """Every rule the newest minute satisfies, before any throttling.

    ``boxes`` maps side -> live box (or None); ``prev_minute_bar`` is the minute
    before ``minute_bar`` (None when there is none), for the close-to-close
    ``box_in_out`` rule.
    """
    if minute_bar is None:
        return []
    close = float(minute_bar["close"])
    minute_start = minute_bar["minute_start"]
    out: list[Candidate] = []
    for side in ("support", "resistance"):
        out += _box_rules(code, close, minute_start, side, boxes.get(side), config)
    prev_close = None if prev_minute_bar is None else float(prev_minute_bar["close"])
    out += _exit_rules(code, close, prev_close, minute_start, boxes, config)
    out += _trendline_rules(code, close, minute_start, hourly, trendlines, hourly_atr, config)
    return out
