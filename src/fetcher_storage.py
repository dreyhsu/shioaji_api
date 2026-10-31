"""Isolated acquisition tables: never overwrite the monitor's strategy bars.

Live aggregates are candidates until checked against broker historical bars.
No raw tick history or broker credentials are persisted here.
"""
from __future__ import annotations

import json
import math

import pandas as pd

import db


SCHEMA = """
CREATE TABLE IF NOT EXISTS market_latest_quotes (
    code TEXT NOT NULL, mode TEXT NOT NULL, price REAL NOT NULL,
    volume REAL NOT NULL, quote_at TEXT NOT NULL, received_at TEXT NOT NULL,
    instance TEXT NOT NULL, generation INTEGER NOT NULL,
    PRIMARY KEY (code, mode)
);
CREATE TABLE IF NOT EXISTS market_stream_bars (
    code TEXT NOT NULL, mode TEXT NOT NULL, minute_start TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('stream', 'broker')),
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL, amount REAL NOT NULL,
    complete INTEGER NOT NULL, instance TEXT NOT NULL, generation INTEGER NOT NULL,
    PRIMARY KEY (code, mode, minute_start, source)
);
CREATE TABLE IF NOT EXISTS market_bar_checks (
    code TEXT NOT NULL, mode TEXT NOT NULL, minute_start TEXT NOT NULL,
    result TEXT NOT NULL, differences TEXT NOT NULL,
    PRIMARY KEY (code, mode, minute_start)
);
CREATE TABLE IF NOT EXISTS market_fetcher_progress (
    role TEXT NOT NULL, code TEXT NOT NULL, mode TEXT NOT NULL,
    validation_since TEXT NOT NULL,
    PRIMARY KEY (role, code, mode)
);
"""


class FetcherStore:
    def __init__(self, path):
        self.conn = db.connect(path)
        try:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    def quotes(self, rows):
        with self.conn:
            self.conn.executemany(
                "INSERT INTO market_latest_quotes VALUES (:code,:mode,:price,:volume,:quote_at,"
                ":received_at,:instance,:generation) ON CONFLICT(code,mode) DO UPDATE SET "
                "price=excluded.price,volume=excluded.volume,quote_at=excluded.quote_at,"
                "received_at=excluded.received_at,instance=excluded.instance,generation=excluded.generation",
                rows,
            )

    def bars(self, code, mode, rows, source, instance, generation):
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT INTO market_stream_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code,mode,minute_start,source) DO UPDATE SET "
                "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,"
                "volume=excluded.volume,amount=excluded.amount,complete=excluded.complete,"
                "instance=excluded.instance,generation=excluded.generation",
                [(code, mode, str(row['minute_start']), source, *(float(row[key]) for key in
                  ('open', 'high', 'low', 'close', 'volume', 'amount')), int(row['complete']), instance,
                  row.get('generation', generation))
                 for row in rows],
            )
            for row in rows:
                stamp = str(row['minute_start'])
                pair = self.conn.execute(
                    "SELECT * FROM market_stream_bars WHERE code=? AND mode=? AND minute_start=? ORDER BY source",
                    (code, mode, stamp),
                ).fetchall()
                if len(pair) != 2:
                    continue
                broker, stream = map(dict, pair)
                differences = {key: {'broker': broker[key], 'stream': stream[key]}
                               for key in ('open', 'high', 'low', 'close', 'volume', 'amount')
                               if not math.isclose(broker[key], stream[key], rel_tol=0, abs_tol=1e-8)}
                result = 'incomplete' if not stream['complete'] else 'mismatch' if differences else 'match'
                self.conn.execute(
                    "INSERT OR REPLACE INTO market_bar_checks VALUES (?,?,?,?,?)",
                    (code, mode, stamp, result, json.dumps(differences, allow_nan=False)),
                )

    def invalidate(self, code, mode, stamps):
        with self.conn:
            for stamp in stamps:
                self.conn.execute(
                    "UPDATE market_stream_bars SET complete=0 WHERE code=? AND mode=? AND minute_start=? AND source='stream'",
                    (code, mode, str(stamp)),
                )
                self.conn.execute(
                    "UPDATE market_bar_checks SET result='incomplete' WHERE code=? AND mode=? AND minute_start=?",
                    (code, mode, str(stamp)),
                )

    def validation_since(self, code, mode, default):
        row = self.conn.execute(
            "SELECT validation_since FROM market_fetcher_progress WHERE role='kbar' AND code=? AND mode=?",
            (code, mode),
        ).fetchone()
        if row:
            return pd.Timestamp(row[0])
        self.conn.execute("INSERT INTO market_fetcher_progress VALUES ('kbar',?,?,?)", (code, mode, str(default)))
        self.conn.commit()
        return pd.Timestamp(default)

    def missing(self, code, mode, expected):
        rows = self.conn.execute(
            "SELECT minute_start FROM market_stream_bars WHERE code=? AND mode=? AND complete=1",
            (code, mode),
        ).fetchall()
        return sorted(expected - {pd.Timestamp(row[0]) for row in rows})

    def latest_bar(self, code, mode):
        row = self.conn.execute(
            'SELECT MAX(minute_start) FROM market_stream_bars WHERE code=? AND mode=?', (code, mode),
        ).fetchone()
        return None if row[0] is None else pd.Timestamp(row[0])

    def close(self):
        self.conn.close()
