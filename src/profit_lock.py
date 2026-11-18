"""Validated, immutable policy for per-contract tick profit protection."""
from bisect import bisect_right
from dataclasses import dataclass
import math


DEFAULT_TIERS = ((0., .5), (500., .4), (600., 1 / 3), (800., .25),
                 (1000., .2), (1500., 2 / 15))


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('profit lock settings require finite numbers')
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('profit lock settings require nonnegative finite numbers')
    return value


@dataclass(frozen=True)
class ProfitLockPolicy:
    trigger_points: float = 300.
    tiers: tuple[tuple[float, float], ...] = DEFAULT_TIERS

    @classmethod
    def from_config(cls, config):
        if not isinstance(config, dict):
            raise ValueError('profit lock settings require an object')
        trigger = _number(config.get('profit_lock_trigger_points', 300.))
        rows = config.get('profit_lock_tiers')
        if 'profit_lock_tiers' not in config:
            return cls(trigger_points=trigger)
        if not isinstance(rows, list) or not rows:
            raise ValueError('profit lock tiers require a nonempty list')
        tiers = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'from_points', 'giveback_ratio'}:
                raise ValueError('invalid profit lock tier fields')
            start, ratio = _number(row['from_points']), _number(row['giveback_ratio'])
            if ratio > 1:
                raise ValueError('profit lock giveback ratio must be between zero and one')
            if not tiers and start != 0:
                raise ValueError('the base profit lock tier must start at zero')
            if tiers and (start <= tiers[-1][0] or ratio > tiers[-1][1]):
                raise ValueError('tier starts must increase and giveback ratios must not increase')
            tiers.append((start, ratio))
        return cls(trigger, tuple(tiers))

    def tier(self, peak):
        index = bisect_right(self.tiers, (max(0., peak), math.inf)) - 1
        start, ratio = self.tiers[index]
        end = self.tiers[index + 1][0] if index + 1 < len(self.tiers) else None
        return start, end, ratio

    def retained_points(self, peak):
        return peak * (1 - self.tier(peak)[2])

    def public(self):
        return dict(profit_lock_trigger_points=self.trigger_points,
                    profit_lock_tiers=[dict(from_points=start, giveback_ratio=ratio)
                                       for start, ratio in self.tiers])

    def order_settings(self, peak):
        start, end, ratio = self.tier(peak)
        return dict(profit_lock_tier_from_points=start, profit_lock_tier_to_points=end,
                    profit_lock_giveback_ratio=ratio,
                    profit_lock_trigger_retained_points=self.retained_points(self.trigger_points))
