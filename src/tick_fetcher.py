"""Independent tick subscriber; publish latest quotes once per second.

Example: conda run -n tss python src/tick_fetcher.py --mode simulation --codes TMFR1
This process owns its session and never imports or starts monitor/trading.
"""
from __future__ import annotations

from market_fetcher import FetcherRuntime, run_cli


class TickFetcher(FetcherRuntime):
    """Subscribe to ticks independently and persist latest per-contract quotes."""
    role = 'tick'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.latest = {}
        self.dirty = set()
        self.last_publish = float('-inf')

    def on_disconnected(self):
        self.latest.clear()
        self.dirty.clear()
        self.last_publish = float('-inf')

    def on_tick(self, tick):
        previous = self.latest.get(tick.code)
        if previous is None or tick.at >= previous.at:
            self.latest[tick.code] = tick
            self.dirty.add(tick.code)

    def flush(self, now, calendar, *, force=False):
        if not self.dirty or (not force and self.monotonic() - self.last_publish < self.settings.tick_publish_seconds):
            return
        rows = [{'code': code, 'mode': self.mode, 'price': self.latest[code].price,
                 'volume': self.latest[code].volume, 'quote_at': str(self.latest[code].at),
                 'received_at': str(self.latest[code].received_at), 'instance': self.instance,
                 'generation': self.generation} for code in sorted(self.dirty)]
        self.store.quotes(rows)
        self.dirty.clear()  # only acknowledge after a successful DB commit
        self.last_publish = self.monotonic()


def main(argv=None):
    return run_cli(TickFetcher, argv)


if __name__ == '__main__':
    raise SystemExit(main())
