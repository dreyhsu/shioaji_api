"""The enter/exit state machine that gates order-book recording and alerts.

One watcher per side (support, resistance).  It is fed the newest *1-minute* bar
plus whichever box is currently live for that side, and it answers "did we just
walk into the zone, are we still in it, or did we just leave".  Everything with a
side effect -- opening a visit, flushing the book buffer, sending Telegram --
hangs off those transitions, so this module stays pure and testable.

Why the minute bar and not the hourly one: a 1-minute bar is the finest grain the
once-a-minute poll can see, and using its ``[low, high]`` range (rather than its
close) means a spike that pokes into the zone and snaps straight back out still
counts.  That moment is precisely the one worth having a book recording for.

``exit_buffer_atr`` exists because price loitering on a box edge would otherwise
churn visits open and closed every minute.  Replaying 30 days of TMF minute bars
showed exactly that: a buffer of 0 produced 146 visits covering 2,502 minutes,
while 0.1 produced 84 visits covering 2,745 minutes -- 42% fewer visits and *more*
total coverage, because the fragments merge into continuous windows instead of
being torn apart by single-minute pokes back over the edge.  Hence the 0.1
default.  It is expressed in hourly ATR so it scales with volatility rather than
being a fixed number of points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd

from snr_core import bar_in_box, box_height, zone_pct

TransitionKind = Literal["enter", "stay", "exit"]
ExitReason = Literal["left_box", "box_broken", "box_changed", "session_end", "shutdown"]


@dataclass(frozen=True)
class BoxWatchConfig:
    # How far outside the box price must go before a visit is considered over,
    # as a multiple of the hourly ATR.  0 = leave the instant the range clears;
    # 0.1 measured best on TMF (see the module docstring).
    exit_buffer_atr: float = 0.1


@dataclass
class Transition:
    kind: TransitionKind
    box_id: int | None = None
    # Carried on the transition itself because _leave() clears the watcher's
    # visit_id as it returns -- the caller still needs to know which visit ended.
    visit_id: int | None = None
    box_type: str | None = None
    price: float | None = None
    zone_pct: float | None = None
    minute_start: Any = None
    exit_reason: ExitReason | None = None
    # On a box change we leave one visit and join another in the same step.
    follow_up: "Transition | None" = None


@dataclass
class BoxWatcher:
    """Tracks whether price is inside this side's live box."""

    box_type: str
    config: BoxWatchConfig = field(default_factory=BoxWatchConfig)
    inside: bool = False
    box_id: int | None = None
    visit_id: int | None = None
    minutes_in_box: int = 0
    last_minute_start: Any = None

    def restore(self, box_id: int | None, visit_id: int | None, minutes_in_box: int) -> None:
        """Adopt an unfinished visit from the database after a restart."""
        self.inside = visit_id is not None
        self.box_id = box_id
        self.visit_id = visit_id
        self.minutes_in_box = int(minutes_in_box or 0)

    def _leave(self, reason: ExitReason, price: float | None, minute_start: Any) -> Transition:
        transition = Transition(
            kind="exit",
            box_id=self.box_id,
            visit_id=self.visit_id,
            box_type=self.box_type,
            price=price,
            minute_start=minute_start,
            exit_reason=reason,
        )
        self.inside = False
        self.box_id = None
        self.visit_id = None
        self.minutes_in_box = 0
        return transition

    def _join(self, box, price: float, minute_start: Any) -> Transition:
        self.inside = True
        self.box_id = int(box["box_id"])
        self.visit_id = None  # the caller fills this in once the row exists
        self.minutes_in_box = 1
        return Transition(
            kind="enter",
            box_id=int(box["box_id"]),
            box_type=self.box_type,
            price=price,
            zone_pct=zone_pct(price, box),
            minute_start=minute_start,
        )

    def step(self, minute_bar, box, hourly_atr: float | None = None) -> Transition | None:
        """Advance one minute.  Returns the transition, or None when nothing changed.

        ``minute_bar`` needs ``minute_start``/``high``/``low``/``close``; ``box`` is
        a row from ``snr_core.current_box`` (or None when this side has no live box).
        """
        minute_start = minute_bar["minute_start"]
        # The same minute can be re-polled; only let the clock advance once.
        repeated = (
            self.last_minute_start is not None
            and pd.Timestamp(minute_start) == pd.Timestamp(self.last_minute_start)
        )
        self.last_minute_start = minute_start
        price = float(minute_bar["close"])

        if box is None:
            # The box was broken or aged out from under us.
            if self.inside:
                return self._leave("box_broken", price, minute_start)
            return None

        incoming_id = int(box["box_id"])
        if self.inside and self.box_id != incoming_id:
            # A newer box took over this side: close the old visit, then decide
            # whether this same minute also lands inside the new one.
            exited = self._leave("box_changed", price, minute_start)
            if bar_in_box(minute_bar, box):
                exited.follow_up = self._join(box, price, minute_start)
            return exited

        touching = bar_in_box(minute_bar, box)

        if not self.inside:
            return self._join(box, price, minute_start) if touching else None

        if touching:
            if not repeated:
                self.minutes_in_box += 1
            return Transition(
                kind="stay",
                box_id=incoming_id,
                box_type=self.box_type,
                price=price,
                zone_pct=zone_pct(price, box),
                minute_start=minute_start,
            )

        # Outside the box proper -- honour the buffer before calling it an exit.
        if self.config.exit_buffer_atr > 0:
            buffer = self.config.exit_buffer_atr * float(
                hourly_atr if hourly_atr and hourly_atr > 0 else box_height(box)
            )
            padded = {
                "box_id": incoming_id,
                "box_type": self.box_type,
                "top": float(box["top"]) + buffer,
                "bottom": float(box["bottom"]) - buffer,
            }
            if bar_in_box(minute_bar, padded):
                return Transition(
                    kind="stay",
                    box_id=incoming_id,
                    box_type=self.box_type,
                    price=price,
                    zone_pct=zone_pct(price, box),
                    minute_start=minute_start,
                )

        return self._leave("left_box", price, minute_start)
