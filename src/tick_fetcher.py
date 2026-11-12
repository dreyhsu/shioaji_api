"""Independent tick subscriber; publish latest quotes once per second.

Example: conda run -n tss python src/tick_fetcher.py --codes TMFR1
This process owns its session and never imports or starts monitor/trading.
"""
from __future__ import annotations

import pandas as pd

from market_fetcher import FetcherRuntime, run_cli
from minute_accumulator import MinuteAccumulator


class TickFetcher(FetcherRuntime):
    """Subscribe to ticks independently and persist latest per-contract quotes."""
    role = 'tick'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accumulators = {}
        self.pending_rows = []
        self.pending_invalidated = {}
        self.latest = {}
        self.dirty = set()
        self.last_publish = float('-inf')

    def on_connected(self):
        self.accumulators = {code: MinuteAccumulator(self.connected_at.ceil('min'),
                             self.settings.bar_close_delay_seconds) for code in self.codes}
        self.stream_generation = self.generation

    def on_disconnected(self):
        for code, acc in self.accumulators.items():
            for row in acc.minutes.values():
                self.pending_rows.append((code, {**row, 'complete': False,
                                                'generation': self.stream_generation}))
            self.pending_invalidated.setdefault(code, set()).update(acc.invalidated)
        self.accumulators.clear()
        self.latest.clear()
        self.dirty.clear()
        self.last_publish = float('-inf')

    def on_tick(self, tick):
        if tick.code in self.accumulators:
            self.accumulators[tick.code].add(tick)
        previous = self.latest.get(tick.code)
        if previous is None or tick.at >= previous.at:
            self.latest[tick.code] = tick
            self.dirty.add(tick.code)

    def flush(self, now, calendar, *, force=False):
        for code, acc in self.accumulators.items():
            self.pending_rows.extend((code, {**row, 'generation': self.stream_generation})
                                     for row in acc.finish(now))
            self.pending_invalidated.setdefault(code, set()).update(acc.invalidated)
            acc.invalidated.clear()
        ready = [(code, row) for code, row in self.pending_rows
                 if row['minute_start'] + pd.Timedelta(
                     minutes=1, seconds=self.settings.bar_close_delay_seconds) <= now]
        for code in self.codes:
            output = [{**row, 'complete': bool(row['complete'] and calendar.covered(row['minute_start'])
                       and calendar.active(row['minute_start']))} for item_code, row in ready if item_code == code]
            self.store.bars(code, self.mode, output, 'stream', self.instance, self.generation)
            self.store.invalidate(code, self.mode, self.pending_invalidated.get(code, set()))
        ready_ids = {id(row) for _, row in ready}
        self.pending_rows = [(code, row) for code, row in self.pending_rows if id(row) not in ready_ids]
        self.pending_invalidated.clear()
        if not self.dirty or (not force and self.monotonic() - self.last_publish < self.settings.tick_publish_seconds):
            return
        rows = [{'code': code, 'mode': self.mode, 'price': self.latest[code].price,
                 'volume': self.latest[code].volume, 'quote_at': str(self.latest[code].at),
                 'received_at': str(self.latest[code].received_at), 'instance': self.instance,
                 'generation': self.generation} for code in sorted(self.dirty)]
        self.store.quotes(rows)
        published_at = pd.Timestamp(self.clock())
        for row in rows:
            self.health['quote_publish_latency'][row['code']] = {
                'quote_at': row['quote_at'], 'received_at': row['received_at'],
                'published_at': str(published_at),
                'receive_to_publish_ms': (published_at - pd.Timestamp(row['received_at'])).total_seconds() * 1000,
                'quote_age_ms': (published_at - pd.Timestamp(row['quote_at'])).total_seconds() * 1000,
            }
        self.dirty.clear()  # only acknowledge after a successful DB commit
        self.last_publish = self.monotonic()


def main(argv=None):
    return run_cli(TickFetcher, argv)


if __name__ == '__main__':
    raise SystemExit(main())
