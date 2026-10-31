"""Independent futures 1m collector: tick aggregation plus bounded history repair.

Futures do not support the securities-only KBar subscription. This process owns
its own tick session; it never depends on tick_fetcher or monitor. Candidate bars
and broker history go to isolated acquisition tables, not strategy minute_bars.
"""
from __future__ import annotations

import pandas as pd

from market_fetcher import FetcherRuntime, run_cli
import shioaji_client as sc
import session_bars as sb


class MinuteAccumulator:
    """Order-aware OHLCV, with delayed close and explicit incomplete minutes."""
    def __init__(self, complete_from, close_delay=2):
        self.complete_from = pd.Timestamp(complete_from)
        self.close_delay = close_delay
        self.minutes = {}
        self.closed_through = None
        self.invalidated = set()

    def add(self, tick):
        if sb.session_fields(tick.at) is None:
            return
        minute = tick.at.floor('min')
        if self.closed_through is not None and minute <= self.closed_through:
            self.invalidated.add(minute)
            return
        row = self.minutes.get(minute)
        if row is None:
            row = {'minute_start': minute, 'open': tick.price, 'high': tick.price,
                   'low': tick.price, 'close': tick.price, 'volume': 0., 'amount': 0.,
                   'first': tick.at, 'last': tick.at, 'complete': minute >= self.complete_from}
            self.minutes[minute] = row
        if tick.at < row['first']:
            row['open'], row['first'] = tick.price, tick.at
        if tick.at >= row['last']:
            row['close'], row['last'] = tick.price, tick.at
        row['high'], row['low'] = max(row['high'], tick.price), min(row['low'], tick.price)
        row['volume'] += tick.volume
        row['amount'] += tick.amount

    def finish(self, now):
        cutoff = pd.Timestamp(now) - pd.Timedelta(seconds=self.close_delay)
        ready = [stamp for stamp in sorted(self.minutes) if stamp + pd.Timedelta(minutes=1) <= cutoff]
        result = [self.minutes.pop(stamp) for stamp in ready]
        # Remember empty closed minutes too: late arrivals must not invent a complete minute.
        watermark = cutoff.floor('min') - pd.Timedelta(minutes=1)
        self.closed_through = max(self.closed_through, watermark) if self.closed_through is not None else watermark
        return result


class KbarFetcher(FetcherRuntime):
    """Own a separate tick session and emit closed 1m candidates, with history checks."""
    role = 'kbar'

    def __init__(self, *args, backfill_enabled=True, fetch_history=None, **kwargs):
        super().__init__(*args, **kwargs)
        if len(self.codes) != 1:
            raise ValueError('kbar_fetcher requires exactly one --codes contract')
        self.code = self.codes[0]
        self.backfill_enabled = backfill_enabled
        self.fetch_history = fetch_history or sc.fetch_minute_bars
        self.accumulator = None
        self.pending_rows = []
        self.pending_invalidated = set()
        self.since = None
        self.history_pending = True
        self.last_history = float('-inf')
        self.history_completed_at = None
        self.final_attempted_end = None

    def start(self):
        super().start()
        if self.since is None:
            # Resume from the actual canonical DB tail when available. Keep the
            # validation origin durable, even if newer stream rows are written.
            initial = pd.Timestamp(self.clock()).normalize() - pd.Timedelta(days=self.settings.backfill_days)
            if self.store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='minute_bars'").fetchone():
                row = self.store.conn.execute('SELECT MAX(minute_start) FROM minute_bars WHERE code=?', (self.code,)).fetchone()
                if row[0]:
                    initial = pd.Timestamp(row[0]) - pd.Timedelta(minutes=1)
            self.since = self.store.validation_since(self.code, self.mode, initial)

    def on_connected(self):
        self.accumulator = MinuteAccumulator(self.connected_at.ceil('min'), self.settings.bar_close_delay_seconds)
        self.stream_generation = self.generation
        self.history_pending = True

    def on_disconnected(self):
        # No bar spanning a lost connection can be certified from this stream.
        if self.accumulator is not None:
            for row in self.accumulator.minutes.values():
                row['complete'] = False
                row['generation'] = self.stream_generation
                self.pending_rows.append(row)
            self.pending_invalidated.update(self.accumulator.invalidated)
        self.accumulator = None

    def on_tick(self, tick):
        if self.accumulator is not None:
            self.accumulator.add(tick)

    def flush(self, now, calendar, *, force=False):
        if self.accumulator is not None:
            self.pending_rows.extend(self.accumulator.finish(now))
            self.pending_invalidated.update(self.accumulator.invalidated)
            self.accumulator.invalidated.clear()
        # Do not publish a partial minute on a disconnect; keep it pending until closed.
        ready = [row for row in self.pending_rows
                 if row['minute_start'] + pd.Timedelta(minutes=1, seconds=self.settings.bar_close_delay_seconds) <= now]
        if ready:
            # A calendar is required even for declaring a live candidate complete.
            output = [{**row, 'complete': bool(row['complete'] and calendar.covered(row['minute_start'])
                       and calendar.active(row['minute_start']))} for row in ready]
            self.store.bars(self.code, self.mode, output, 'stream', self.instance, self.generation)
            ready_ids = {id(row) for row in ready}
            self.pending_rows = [row for row in self.pending_rows if id(row) not in ready_ids]
        if self.pending_invalidated:
            self.store.invalidate(self.code, self.mode, self.pending_invalidated)
            self.pending_invalidated.clear()

    def work(self, now, calendar):
        mature_now = now - pd.Timedelta(seconds=self.settings.bar_close_delay_seconds + 180)
        expected = calendar.expected_minutes(self.since, mature_now)
        missing = self.store.missing(self.code, self.mode, expected)
        self.health['missing_count'] = len(missing)
        self.health['validation_since'] = str(self.since)
        self.health['history_enabled'] = self.backfill_enabled
        self.health['calendar_covers_resume'] = calendar.covered(self.since)
        self.health['history_retry_in_seconds'] = max(
            0, self.settings.history_repair_seconds - (self.monotonic() - self.last_history))
        # One final reconciliation in the minute between market close and logout.
        ended = [end for start, end in calendar.sessions if self.connected_at <= end <= now
                 and now >= end + pd.Timedelta(seconds=self.settings.bar_close_delay_seconds)]
        final_due = bool(ended and (self.history_completed_at is None or self.history_completed_at < max(ended))
                         and (self.final_attempted_end is None or self.final_attempted_end < max(ended)))
        repair_due = bool(missing and self.monotonic() - self.last_history >= self.settings.history_repair_seconds)
        if not self.backfill_enabled or not (self.history_pending or final_due or repair_due):
            return
        if self.monotonic() - self.last_history < self.settings.history_repair_seconds and not final_due:
            return
        self.publish(state='backfilling', operation='kbars', operation_started=str(now))
        self.last_history = self.monotonic()  # limit failures too; never poll history each minute
        if final_due:
            self.final_attempted_end = max(ended)
        # Do not download the entire retained range on every restart. Check all
        # closed gaps on reconnect, otherwise overlap only the most recent bar.
        candidates = (self.store.missing(self.code, self.mode, calendar.expected_minutes(self.since, now))
                      if self.history_pending else missing)
        latest = self.store.latest_bar(self.code, self.mode)
        left = candidates[0] if candidates else max(
            self.since, latest - pd.Timedelta(minutes=1)) if latest is not None else self.since
        frame = self.fetch_history(self.api, self.contracts[self.code], left.date(), now.date(),
                                   timeout_ms=self.settings.api_timeout_ms)
        finished = pd.Timestamp(self.clock())
        frame = frame[frame['minute_start'] + pd.Timedelta(minutes=1) <= finished]
        rows = [{**row, 'complete': True} for row in frame.to_dict('records')]
        self.store.bars(self.code, self.mode, rows, 'broker', self.instance, self.generation)
        self.history_pending = False
        self.history_completed_at = finished
        self.health['last_history_at'] = str(finished)
        self.health['history_rows'] = len(rows)
        self.health['history_error'] = None
        self.health['missing_count'] = len(self.store.missing(self.code, self.mode, expected))


def main(argv=None):
    return run_cli(KbarFetcher, argv)


if __name__ == '__main__':
    raise SystemExit(main())
