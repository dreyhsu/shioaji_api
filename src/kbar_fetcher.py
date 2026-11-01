"""Independent historical K-bar backfill and reconciliation; no live subscription."""
from __future__ import annotations

import pandas as pd

from market_fetcher import FetcherRuntime, run_cli
import shioaji_client as sc



class KbarFetcher(FetcherRuntime):
    """Own a separate session exclusively for historical backfill and checks."""
    stream_required = False
    role = 'kbar'

    def __init__(self, *args, backfill_enabled=True, fetch_history=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.code = self.codes[0]
        self.history_states = {}
        self.backfill_enabled = backfill_enabled
        self.fetch_history = fetch_history or sc.fetch_minute_bars
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

        for code in self.codes[1:]:
            if code not in self.history_states:
                initial = pd.Timestamp(self.clock()).normalize() - pd.Timedelta(days=self.settings.backfill_days)
                self.history_states[code] = dict(since=self.store.validation_since(code, self.mode, initial),
                    history_pending=True, last_history=float('-inf'), history_completed_at=None, final_attempted_end=None)

    def on_connected(self):
        self.history_pending = True
        for state in self.history_states.values():
            state['history_pending'] = True

    def flush(self, now, calendar, *, force=False):
        pass

    def work(self, now, calendar):
        # Independent cursors, one shared broker session and no extra login.
        names = ('since', 'history_pending', 'last_history', 'history_completed_at', 'final_attempted_end')
        primary = {name: getattr(self, name) for name in names}
        try:
            for code in self.codes:
                if self.schedule.window(self.clock(), calendar) != self._window:
                    break
                self.code = code
                if code != self.codes[0]:
                    state = self.history_states.get(code)
                    if state is None:
                        initial = pd.Timestamp(now).normalize() - pd.Timedelta(days=self.settings.backfill_days)
                        state = dict(since=self.store.validation_since(code, self.mode, initial), history_pending=True,
                                     last_history=float('-inf'), history_completed_at=None, final_attempted_end=None)
                    for name, value in state.items():
                        setattr(self, name, value)
                try:
                    self._work_contract(pd.Timestamp(self.clock()), calendar)
                finally:
                    state = {name: getattr(self, name) for name in names}
                    self.history_states[code] = state
                    if code == self.codes[0]:
                        primary = state
        finally:
            self.code = self.codes[0]
            for name, value in primary.items():
                setattr(self, name, value)

    def _work_contract(self, now, calendar):
        mature_now = now - pd.Timedelta(seconds=self.settings.bar_close_delay_seconds + 180)
        expected = calendar.expected_minutes(self.since, mature_now)
        missing = self.store.missing(self.code, self.mode, expected, source='broker')
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
        candidates = (self.store.missing(self.code, self.mode, calendar.expected_minutes(self.since, now), source='broker')
                      if self.history_pending else missing)
        latest = self.store.latest_bar(self.code, self.mode)
        left = candidates[0] if candidates else max(
            self.since, latest - pd.Timedelta(minutes=1)) if latest is not None else self.since
        requested = {'contract_code': self.code, 'from_timestamp': str(left.normalize()),
                     'to_timestamp': str(now.normalize() + pd.Timedelta(days=1)),
                     'to_exclusive': True, 'closed_before': str(now)}
        self.health['history_range'] = requested
        self.event('backfill_started', **requested)
        frame = self.fetch_history(self.api, self.contracts[self.code], left.date(), now.date(),
                                   timeout_ms=self.settings.api_timeout_ms)
        finished = pd.Timestamp(self.clock())
        frame = frame[(frame['minute_start'] >= left) &
                      (frame['minute_start'] + pd.Timedelta(minutes=1) <= min(finished, now))]
        rows = [{**row, 'complete': True} for row in frame.to_dict('records')]
        self.store.bars(self.code, self.mode, rows, 'broker', self.instance, self.generation)
        self.event('backfill_completed', **requested, rows=len(rows),
                   data_from=str(frame['minute_start'].min()) if len(rows) else None,
                   data_to=str(frame['minute_start'].max()) if len(rows) else None)
        self.history_pending = False
        self.history_completed_at = finished
        self.health['last_history_at'] = str(finished)
        self.health['history_rows'] = len(rows)
        self.health['history_error'] = None
        self.health['missing_count'] = len(self.store.missing(self.code, self.mode, expected, source='broker'))


def main(argv=None):
    return run_cli(KbarFetcher, argv)


if __name__ == '__main__':
    raise SystemExit(main())
