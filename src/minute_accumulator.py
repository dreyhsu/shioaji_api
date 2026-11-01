"""Closed-minute aggregation owned by the realtime tick fetcher."""
import pandas as pd
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


