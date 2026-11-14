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
CREATE TABLE IF NOT EXISTS market_storage_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL
);
INSERT OR IGNORE INTO market_storage_identity VALUES (1, hex(randomblob(16)));
CREATE TABLE IF NOT EXISTS market_bar_versions (
    code TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL,
    PRIMARY KEY (code, mode)
);
CREATE TRIGGER IF NOT EXISTS market_bar_version_insert AFTER INSERT ON market_stream_bars BEGIN
    INSERT INTO market_bar_versions VALUES (NEW.code, NEW.mode, 1)
    ON CONFLICT(code,mode) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER IF NOT EXISTS market_bar_version_delete AFTER DELETE ON market_stream_bars BEGIN
    INSERT INTO market_bar_versions VALUES (OLD.code, OLD.mode, 1)
    ON CONFLICT(code,mode) DO UPDATE SET revision=revision+1;
END;
CREATE TRIGGER IF NOT EXISTS market_bar_version_update AFTER UPDATE ON market_stream_bars
WHEN OLD.open IS NOT NEW.open OR OLD.high IS NOT NEW.high OR OLD.low IS NOT NEW.low
    OR OLD.close IS NOT NEW.close OR OLD.volume IS NOT NEW.volume OR OLD.amount IS NOT NEW.amount
    OR OLD.complete IS NOT NEW.complete OR OLD.instance IS NOT NEW.instance
    OR OLD.generation IS NOT NEW.generation
    OR OLD.code IS NOT NEW.code OR OLD.mode IS NOT NEW.mode
    OR OLD.minute_start IS NOT NEW.minute_start OR OLD.source IS NOT NEW.source
BEGIN
    INSERT INTO market_bar_versions SELECT OLD.code, OLD.mode, 1
    WHERE OLD.code IS NOT NEW.code OR OLD.mode IS NOT NEW.mode
    ON CONFLICT(code,mode) DO UPDATE SET revision=revision+1;
    INSERT INTO market_bar_versions VALUES (NEW.code, NEW.mode, 1)
    ON CONFLICT(code,mode) DO UPDATE SET revision=revision+1;
END;
"""


class FetcherStore:
    def __init__(self, path, *, clock=None, role='acquisition'):
        from latency_trace import LatencyRecorder
        self.latency = LatencyRecorder(path, role, '', '', clock=clock)
        self._published = {}
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
        complete = [row for row in rows if row['complete']]
        if complete:
            row = max(complete, key=lambda row: pd.Timestamp(row['minute_start']))
            stamp = pd.Timestamp(row['minute_start'])
            actual_generation = row.get('generation', generation)
            signature = (str(stamp), instance, actual_generation, *(row[key] for key in ('open','high','low','close','volume','amount')))
            key = (code, mode, source)
            if self._published.get(key) != signature:
                self._published[key] = signature
                self.latency.record('bar_published', code=code, mode=mode,
                    trace_id=f'market:{code}:{stamp}', minute_start=str(stamp), source=source,
                    source_instance=instance, generation=actual_generation,
                    closed_at=str(stamp + pd.Timedelta(minutes=1)))
                self.latency.flush()

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

    def missing(self, code, mode, expected, source=None):
        rows = self.conn.execute(
            "SELECT minute_start FROM market_stream_bars WHERE code=? AND mode=? AND complete=1 AND (? IS NULL OR source=?)",
            (code, mode, source, source),
        ).fetchall()
        return sorted(expected - {pd.Timestamp(row[0]) for row in rows})

    def latest_bar(self, code, mode):
        row = self.conn.execute(
            'SELECT MAX(minute_start) FROM market_stream_bars WHERE code=? AND mode=?', (code, mode),
        ).fetchone()
        return None if row[0] is None else pd.Timestamp(row[0])

    def close(self):
        self.conn.close()
