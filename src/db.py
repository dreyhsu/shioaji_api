"""SQLite storage for price bars, boxes, box visits, order books and alerts.

Plain stdlib ``sqlite3`` -- the monitor writes and the dashboard reads, so the
connection runs in WAL mode to let them overlap.

Two UNIQUE constraints carry real logic rather than just hygiene:

* ``orderbook_snapshots(code, event_ts, source)`` -- the monitor re-flushes its
  in-memory book buffer every minute and the windows overlap, so ``INSERT OR
  IGNORE`` makes repeated flushes idempotent instead of needing a watermark.
* ``telegram_alerts(code, hour_start, box_type, kind)`` -- rule alerts store the
  triggering *minute* in ``hour_start`` and their key (``support:7``,
  ``line:3``) in ``box_type``, so a re-polled minute cannot double-send.  The
  cooldown and once-per-line rules read the same rows back
  (:func:`last_alert_at`), so a restart forgets neither.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import pandas as pd

import market_clock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "tmf.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS minute_bars (
  code TEXT NOT NULL,
  minute_start TEXT NOT NULL,
  session_id TEXT,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  PRIMARY KEY (code, minute_start)
);

CREATE TABLE IF NOT EXISTS hourly_bars (
  code TEXT NOT NULL,
  hour_start TEXT NOT NULL,
  session_id TEXT,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  minutes INTEGER,
  bar_closed INTEGER,
  updated_at TEXT,
  PRIMARY KEY (code, hour_start)
);

CREATE TABLE IF NOT EXISTS snr_boxes (
  code TEXT NOT NULL,
  box_id INTEGER NOT NULL,
  box_type TEXT, state TEXT,
  top REAL, bottom REAL, pivot_price REAL, atr_width REAL, delta_volume REAL,
  left_time TEXT, right_time TEXT, created_time TEXT, state_time TEXT,
  holds INTEGER, breakouts INTEGER,
  updated_at TEXT,
  PRIMARY KEY (code, box_id)
);

CREATE TABLE IF NOT EXISTS box_overrides (
  code TEXT NOT NULL,
  box_type TEXT NOT NULL,
  created_time TEXT NOT NULL,
  top REAL NOT NULL, bottom REAL NOT NULL,
  orig_top REAL, orig_bottom REAL,
  updated_at TEXT,
  PRIMARY KEY (code, box_type, created_time)
);

CREATE TABLE IF NOT EXISTS box_visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  box_id INTEGER, box_type TEXT,
  box_top REAL, box_bottom REAL, box_state_at_entry TEXT,
  entered_at TEXT, entry_minute_start TEXT, entry_price REAL, entry_zone_pct REAL,
  exited_at TEXT, exit_minute_start TEXT, exit_price REAL, exit_reason TEXT,
  minutes_in_box INTEGER,
  min_zone_pct REAL, max_zone_pct REAL, min_price REAL, max_price REAL,
  reversal_count INTEGER DEFAULT 0,
  orderbook_rows INTEGER DEFAULT 0,
  is_open INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_visits_open ON box_visits (code, is_open);
CREATE INDEX IF NOT EXISTS idx_visits_entered ON box_visits (code, entered_at);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visit_id INTEGER,
  reversal_id INTEGER,
  code TEXT NOT NULL,
  captured_at TEXT,
  event_ts TEXT NOT NULL,
  source TEXT NOT NULL,
  levels INTEGER,
  phase TEXT,
  bid_total_vol REAL, ask_total_vol REAL,
  bid_price TEXT, bid_volume TEXT, diff_bid_vol TEXT,
  ask_price TEXT, ask_volume TEXT, diff_ask_vol TEXT,
  underlying_price REAL,
  simtrade INTEGER,
  UNIQUE (code, event_ts, source)
);
CREATE INDEX IF NOT EXISTS idx_ob_visit ON orderbook_snapshots (visit_id);
CREATE INDEX IF NOT EXISTS idx_ob_event ON orderbook_snapshots (code, event_ts);

CREATE TABLE IF NOT EXISTS reversal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visit_id INTEGER,
  code TEXT NOT NULL,
  detected_at TEXT, hour_start TEXT,
  bar_closed INTEGER, minutes_into_bar INTEGER,
  pattern TEXT, direction TEXT,
  box_id INTEGER, box_type TEXT, box_state TEXT,
  box_top REAL, box_bottom REAL, zone_pct REAL,
  bar_open REAL, bar_high REAL, bar_low REAL, bar_close REAL, bar_volume REAL,
  prev_open REAL, prev_high REAL, prev_low REAL, prev_close REAL,
  upper_wick REAL, lower_wick REAL, body REAL, close_pos REAL,
  range_atr REAL, hourly_atr REAL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_reversal_visit ON reversal_events (visit_id);
CREATE INDEX IF NOT EXISTS idx_reversal_detected ON reversal_events (code, detected_at);

CREATE TABLE IF NOT EXISTS telegram_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  hour_start TEXT, box_type TEXT, box_id INTEGER, visit_id INTEGER,
  kind TEXT NOT NULL,
  sent_at TEXT, ok INTEGER, message TEXT,
  UNIQUE (code, hour_start, box_type, kind)
);

CREATE TABLE IF NOT EXISTS trendlines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  label TEXT, color TEXT,
  anchors TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'line',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trendlines_code ON trendlines (code, enabled);

CREATE TABLE IF NOT EXISTS managed_orders (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  contract_code TEXT NOT NULL,
  action TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  filled_quantity INTEGER NOT NULL DEFAULT 0,
  avg_fill_price REAL,
  price REAL NOT NULL,
  price_type TEXT NOT NULL,
  order_type TEXT NOT NULL,
  atr REAL NOT NULL,
  stop_price REAL,
  target_price REAL,
  curve_id INTEGER,
  curve_armed INTEGER NOT NULL DEFAULT 0,
  curve_check_from TEXT,
  levels_customized INTEGER NOT NULL DEFAULT 0,
  auto_exit INTEGER NOT NULL DEFAULT 1,
  simulation INTEGER NOT NULL,
  entry_trade_id TEXT NOT NULL,
  entry_status TEXT NOT NULL,
  exit_trade_id TEXT,
  exit_status TEXT,
  exit_filled_quantity INTEGER NOT NULL DEFAULT 0,
  triggered_at TEXT,
  trigger_reason TEXT,
  last_quote REAL,
  last_quote_at TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_managed_orders_active
  ON managed_orders (active, code, contract_code);

CREATE TABLE IF NOT EXISTS curve_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL,
  simulation INTEGER NOT NULL,
  curve_id INTEGER NOT NULL,
  contract_code TEXT NOT NULL,
  quantity INTEGER NOT NULL,
  action TEXT NOT NULL DEFAULT 'Buy' CHECK (action IN ('Buy', 'Sell')),
  state TEXT NOT NULL,
  armed_at TEXT NOT NULL,
  check_from TEXT NOT NULL,
  last_checked_minute TEXT,
  triggered_at TEXT,
  order_id TEXT,
  error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_curve_entries_pending
  ON curve_entries (code, simulation) WHERE state IN ('armed', 'submitting');

CREATE TABLE IF NOT EXISTS backfill_progress (
  code TEXT NOT NULL,
  task TEXT NOT NULL,
  key TEXT NOT NULL,
  done_at TEXT, rows INTEGER, note TEXT,
  PRIMARY KEY (code, task, key)
);
"""


def _text(value: Any) -> str | None:
    """Timestamps go in as ISO text so sqlite comparisons stay lexicographic."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    return stamp.isoformat(sep=" ")


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while the monitor writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the first release.  sqlite has no "ADD COLUMN IF NOT
# EXISTS", so each one is applied only when missing.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("curve_entries", "action", "ALTER TABLE curve_entries ADD COLUMN action TEXT NOT NULL DEFAULT 'Buy' CHECK (action IN ('Buy', 'Sell'))"),
    ("box_visits", "source", "ALTER TABLE box_visits ADD COLUMN source TEXT NOT NULL DEFAULT 'live'"),
    ("trendlines", "kind", "ALTER TABLE trendlines ADD COLUMN kind TEXT NOT NULL DEFAULT 'line'"),
    ("managed_orders", "curve_check_from", "ALTER TABLE managed_orders ADD COLUMN curve_check_from TEXT"),
)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, statement in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(statement)
    conn.commit()


@contextmanager
def session(path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# Bars
# --------------------------------------------------------------------------------------
def upsert_minute_bars(conn: sqlite3.Connection, code: str, bars: pd.DataFrame) -> int:
    if bars.empty:
        return 0
    rows = [
        (
            code,
            _text(row.minute_start),
            row.session_id,
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            float(row.volume),
            float(getattr(row, "amount", 0.0) or 0.0),
        )
        for row in bars.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO minute_bars
            (code, minute_start, session_id, open, high, low, close, volume, amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, minute_start) DO UPDATE SET
            session_id=excluded.session_id, open=excluded.open, high=excluded.high,
            low=excluded.low, close=excluded.close, volume=excluded.volume,
            amount=excluded.amount
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_hourly_bars(
    conn: sqlite3.Connection, code: str, bars: pd.DataFrame, closed_through: int
) -> int:
    """Write hourly bars; rows at index > ``closed_through`` are marked unfinished."""
    if bars.empty:
        return 0
    now = _text(market_clock.now())
    rows = []
    for index, row in enumerate(bars.itertuples(index=False)):
        rows.append(
            (
                code,
                _text(row.hour_start),
                row.session_id,
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
                float(getattr(row, "amount", 0.0) or 0.0),
                int(getattr(row, "minutes", 0) or 0),
                1 if index <= closed_through else 0,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO hourly_bars
            (code, hour_start, session_id, open, high, low, close, volume, amount,
             minutes, bar_closed, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, hour_start) DO UPDATE SET
            session_id=excluded.session_id, open=excluded.open, high=excluded.high,
            low=excluded.low, close=excluded.close, volume=excluded.volume,
            amount=excluded.amount, minutes=excluded.minutes,
            bar_closed=excluded.bar_closed, updated_at=excluded.updated_at
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_minute_bars(
    conn: sqlite3.Connection, code: str, since: str | None = None
) -> pd.DataFrame:
    sql = "SELECT * FROM minute_bars WHERE code = ?"
    params: list[Any] = [code]
    if since:
        sql += " AND minute_start >= ?"
        params.append(since)
    sql += " ORDER BY minute_start"
    frame = pd.read_sql_query(sql, conn, params=params)
    if not frame.empty:
        frame["minute_start"] = pd.to_datetime(frame["minute_start"])
    return frame


def load_hourly_bars(
    conn: sqlite3.Connection, code: str, since: str | None = None
) -> pd.DataFrame:
    sql = "SELECT * FROM hourly_bars WHERE code = ?"
    params: list[Any] = [code]
    if since:
        sql += " AND hour_start >= ?"
        params.append(since)
    sql += " ORDER BY hour_start"
    frame = pd.read_sql_query(sql, conn, params=params)
    if not frame.empty:
        frame["hour_start"] = pd.to_datetime(frame["hour_start"])
        frame["timestamp"] = frame["hour_start"]
    return frame


def last_minute_start(conn: sqlite3.Connection, code: str) -> pd.Timestamp | None:
    row = conn.execute(
        "SELECT MAX(minute_start) AS m FROM minute_bars WHERE code = ?", (code,)
    ).fetchone()
    return pd.Timestamp(row["m"]) if row and row["m"] else None


# --------------------------------------------------------------------------------------
# Boxes
# --------------------------------------------------------------------------------------
def replace_boxes(conn: sqlite3.Connection, code: str, boxes: pd.DataFrame) -> int:
    """Rewrite the whole box table for a code -- box ids are recomputed each pass."""
    conn.execute("DELETE FROM snr_boxes WHERE code = ?", (code,))
    if boxes.empty:
        conn.commit()
        return 0
    now = _text(market_clock.now())
    rows = [
        (
            code,
            int(row.box_id),
            row.box_type,
            row.state,
            float(row.top),
            float(row.bottom),
            float(row.pivot_price),
            float(row.atr_width),
            float(row.delta_volume),
            _text(row.left_time),
            _text(row.right_time),
            _text(row.created_time),
            _text(row.state_time),
            int(row.holds),
            int(row.breakouts),
            now,
        )
        for row in boxes.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO snr_boxes
            (code, box_id, box_type, state, top, bottom, pivot_price, atr_width,
             delta_volume, left_time, right_time, created_time, state_time,
             holds, breakouts, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def load_boxes(conn: sqlite3.Connection, code: str) -> pd.DataFrame:
    frame = pd.read_sql_query(
        "SELECT * FROM snr_boxes WHERE code = ? ORDER BY box_id", conn, params=[code]
    )
    for column in ("left_time", "right_time", "created_time", "state_time"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column])
    return frame


def set_box_override(
    conn: sqlite3.Connection, code: str, box_type: str, created_time: Any, top: float, bottom: float
) -> bool:
    """Pin a box's edges by hand; returns False when no such box is stored.

    Keyed by ``created_time`` because ``box_id`` is renumbered on every pass.  The
    stored ``snr_boxes`` row is patched too, so the dashboard shows the new edges
    before the monitor's next cycle recomputes the box from the override.
    """
    created = _text(created_time)
    current = conn.execute(
        "SELECT top, bottom FROM snr_boxes WHERE code = ? AND box_type = ? AND created_time = ?",
        (code, box_type, created),
    ).fetchone()
    if current is None:
        return False
    # orig_* is the computed shape, kept from the first edit so a reset can put the
    # stored row back without waiting for the monitor to recompute it.
    conn.execute(
        """
        INSERT INTO box_overrides
            (code, box_type, created_time, top, bottom, orig_top, orig_bottom, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (code, box_type, created_time)
        DO UPDATE SET top = excluded.top, bottom = excluded.bottom, updated_at = excluded.updated_at
        """,
        (
            code, box_type, created, float(top), float(bottom),
            float(current["top"]), float(current["bottom"]), _text(market_clock.now()),
        ),
    )
    conn.execute(
        "UPDATE snr_boxes SET top = ?, bottom = ? WHERE code = ? AND box_type = ? AND created_time = ?",
        (float(top), float(bottom), code, box_type, created),
    )
    conn.commit()
    return True


def delete_box_override(conn: sqlite3.Connection, code: str, box_type: str, created_time: Any) -> bool:
    """Drop a hand edit and put the stored box back to its computed edges."""
    key = (code, box_type, _text(created_time))
    row = conn.execute(
        "SELECT orig_top, orig_bottom FROM box_overrides "
        "WHERE code = ? AND box_type = ? AND created_time = ?",
        key,
    ).fetchone()
    if row is None:
        return False
    if row["orig_top"] is not None:
        conn.execute(
            "UPDATE snr_boxes SET top = ?, bottom = ? "
            "WHERE code = ? AND box_type = ? AND created_time = ?",
            (row["orig_top"], row["orig_bottom"], *key),
        )
    conn.execute(
        "DELETE FROM box_overrides WHERE code = ? AND box_type = ? AND created_time = ?", key
    )
    conn.commit()
    return True


def load_box_overrides(
    conn: sqlite3.Connection, code: str
) -> dict[tuple[str, pd.Timestamp], tuple[float, float]]:
    """``{(box_type, created_time): (top, bottom)}``, the shape ``collect_snr_boxes`` takes."""
    rows = conn.execute(
        "SELECT box_type, created_time, top, bottom FROM box_overrides WHERE code = ?", (code,)
    ).fetchall()
    return {
        (row["box_type"], pd.Timestamp(row["created_time"])): (float(row["top"]), float(row["bottom"]))
        for row in rows
    }


# --------------------------------------------------------------------------------------
# Box visits
# --------------------------------------------------------------------------------------
def open_visit(
    conn: sqlite3.Connection,
    code: str,
    *,
    box_id: int,
    box_type: str,
    box_top: float,
    box_bottom: float,
    box_state: str,
    entered_at: Any,
    entry_minute_start: Any,
    entry_price: float,
    entry_zone_pct: float,
    source: str = "live",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO box_visits
            (code, box_id, box_type, box_top, box_bottom, box_state_at_entry,
             entered_at, entry_minute_start, entry_price, entry_zone_pct,
             minutes_in_box, min_zone_pct, max_zone_pct, min_price, max_price,
             is_open, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?)
        """,
        (
            code,
            int(box_id),
            box_type,
            float(box_top),
            float(box_bottom),
            box_state,
            _text(entered_at),
            _text(entry_minute_start),
            float(entry_price),
            float(entry_zone_pct),
            float(entry_zone_pct),
            float(entry_zone_pct),
            float(entry_price),
            float(entry_price),
            source,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def touch_visit(
    conn: sqlite3.Connection,
    visit_id: int,
    *,
    price: float,
    zone_pct_value: float,
    minutes_in_box: int,
) -> None:
    conn.execute(
        """
        UPDATE box_visits SET
            minutes_in_box = ?,
            min_zone_pct = MIN(min_zone_pct, ?),
            max_zone_pct = MAX(max_zone_pct, ?),
            min_price = MIN(min_price, ?),
            max_price = MAX(max_price, ?)
        WHERE id = ?
        """,
        (
            int(minutes_in_box),
            float(zone_pct_value),
            float(zone_pct_value),
            float(price),
            float(price),
            int(visit_id),
        ),
    )
    conn.commit()


def close_visit(
    conn: sqlite3.Connection,
    visit_id: int,
    *,
    exited_at: Any,
    exit_minute_start: Any,
    exit_price: float | None,
    exit_reason: str,
) -> None:
    conn.execute(
        """
        UPDATE box_visits SET
            exited_at = ?, exit_minute_start = ?, exit_price = ?, exit_reason = ?, is_open = 0
        WHERE id = ?
        """,
        (
            _text(exited_at),
            _text(exit_minute_start),
            None if exit_price is None else float(exit_price),
            exit_reason,
            int(visit_id),
        ),
    )
    conn.commit()


def get_open_visit(conn: sqlite3.Connection, code: str, box_type: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM box_visits
        WHERE code = ? AND box_type = ? AND is_open = 1 AND source = 'live'
        ORDER BY id DESC LIMIT 1
        """,
        (code, box_type),
    ).fetchone()


def load_visits(
    conn: sqlite3.Connection, code: str, limit: int = 50, open_only: bool = False
) -> pd.DataFrame:
    sql = "SELECT * FROM box_visits WHERE code = ?"
    if open_only:
        sql += " AND is_open = 1"
    sql += " ORDER BY id DESC LIMIT ?"
    return pd.read_sql_query(sql, conn, params=[code, int(limit)])


def bump_visit_counter(conn: sqlite3.Connection, visit_id: int, column: str, amount: int) -> None:
    if column not in {"reversal_count", "orderbook_rows"}:
        raise ValueError(f"refusing to bump unknown column {column!r}")
    conn.execute(
        f"UPDATE box_visits SET {column} = COALESCE({column}, 0) + ? WHERE id = ?",
        (int(amount), int(visit_id)),
    )
    conn.commit()


# --------------------------------------------------------------------------------------
# Order book
# --------------------------------------------------------------------------------------
def insert_orderbook_rows(
    conn: sqlite3.Connection, code: str, rows: Sequence[dict]
) -> int:
    """Append book snapshots, ignoring ones already stored.

    Returns how many rows were actually new, which is what the visit counter and
    the logs care about.
    """
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) AS n FROM orderbook_snapshots").fetchone()["n"]
    payload = [
        (
            row.get("visit_id"),
            row.get("reversal_id"),
            code,
            _text(row.get("captured_at") or market_clock.now()),
            _text(row["event_ts"]),
            row.get("source", "bidask_v1"),
            int(row.get("levels", 5)),
            row.get("phase", "in_box"),
            row.get("bid_total_vol"),
            row.get("ask_total_vol"),
            json.dumps(row.get("bid_price")),
            json.dumps(row.get("bid_volume")),
            json.dumps(row.get("diff_bid_vol")),
            json.dumps(row.get("ask_price")),
            json.dumps(row.get("ask_volume")),
            json.dumps(row.get("diff_ask_vol")),
            row.get("underlying_price"),
            1 if row.get("simtrade") else 0,
        )
        for row in rows
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO orderbook_snapshots
            (visit_id, reversal_id, code, captured_at, event_ts, source, levels, phase,
             bid_total_vol, ask_total_vol, bid_price, bid_volume, diff_bid_vol,
             ask_price, ask_volume, diff_ask_vol, underlying_price, simtrade)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) AS n FROM orderbook_snapshots").fetchone()["n"]
    return int(after - before)


def count_orderbook_rows(conn: sqlite3.Connection, code: str, visit_id: int | None = None) -> int:
    if visit_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orderbook_snapshots WHERE code = ?", (code,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orderbook_snapshots WHERE code = ? AND visit_id = ?",
            (code, int(visit_id)),
        ).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------------------
# Reversals
# --------------------------------------------------------------------------------------
def insert_reversal(conn: sqlite3.Connection, code: str, event: dict) -> int:
    cursor = conn.execute(
        """
        INSERT INTO reversal_events
            (visit_id, code, detected_at, hour_start, bar_closed, minutes_into_bar,
             pattern, direction, box_id, box_type, box_state, box_top, box_bottom,
             zone_pct, bar_open, bar_high, bar_low, bar_close, bar_volume,
             prev_open, prev_high, prev_low, prev_close,
             upper_wick, lower_wick, body, close_pos, range_atr, hourly_atr, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.get("visit_id"),
            code,
            _text(event.get("detected_at") or market_clock.now()),
            _text(event.get("hour_start")),
            1 if event.get("bar_closed") else 0,
            event.get("minutes_into_bar"),
            event.get("pattern"),
            event.get("direction"),
            event.get("box_id"),
            event.get("box_type"),
            event.get("box_state"),
            event.get("box_top"),
            event.get("box_bottom"),
            event.get("zone_pct"),
            event.get("bar_open"),
            event.get("bar_high"),
            event.get("bar_low"),
            event.get("bar_close"),
            event.get("bar_volume"),
            event.get("prev_open"),
            event.get("prev_high"),
            event.get("prev_low"),
            event.get("prev_close"),
            event.get("upper_wick"),
            event.get("lower_wick"),
            event.get("body"),
            event.get("close_pos"),
            event.get("range_atr"),
            event.get("hourly_atr"),
            json.dumps(event.get("detail") or {}, default=str),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def load_reversals(conn: sqlite3.Connection, code: str, limit: int = 50) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM reversal_events WHERE code = ? ORDER BY id DESC LIMIT ?",
        conn,
        params=[code, int(limit)],
    )


def reversal_exists(
    conn: sqlite3.Connection, code: str, hour_start: Any, pattern: str, minutes_into_bar: int
) -> bool:
    """Has this exact pattern already been logged for this minute of this bar?

    The hourly bar is unfinished, so the same pattern legitimately re-fires every
    minute; this only suppresses duplicates from a retried loop iteration.
    """
    row = conn.execute(
        """
        SELECT 1 FROM reversal_events
        WHERE code = ? AND hour_start = ? AND pattern = ? AND minutes_into_bar = ?
        LIMIT 1
        """,
        (code, _text(hour_start), pattern, int(minutes_into_bar)),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------------------
# Trendlines
# --------------------------------------------------------------------------------------
def insert_trendline(
    conn: sqlite3.Connection,
    code: str,
    anchors: Sequence[dict],
    *,
    label: str | None = None,
    color: str | None = None,
    kind: str = "line",
    created_at: Any = None,
) -> int:
    """Store a hand drawing; ``anchors`` is ``[{"time": epoch, "price": p}, ...]``.

    Anchors are kept in time order.  For a ``"curve"`` that puts the Bezier
    control point in the middle, which is where ``trendline`` expects it.
    """
    ordered = sorted(
        ({"time": int(a["time"]), "price": float(a["price"])} for a in anchors),
        key=lambda a: a["time"],
    )
    cursor = conn.execute(
        "INSERT INTO trendlines (code, label, color, anchors, kind, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (
            code,
            label,
            color,
            json.dumps(ordered),
            kind,
            _text(market_clock.now() if created_at is None else created_at),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def load_trendlines(conn: sqlite3.Connection, code: str, enabled_only: bool = True) -> list[dict]:
    query = "SELECT * FROM trendlines WHERE code = ?"
    if enabled_only:
        query += " AND enabled = 1"
    rows = conn.execute(query + " ORDER BY id", (code,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["anchors"] = json.loads(item["anchors"])
        out.append(item)
    return out


def delete_trendline(conn: sqlite3.Connection, line_id: int) -> bool:
    """Soft delete: past ``telegram_alerts`` rows keep pointing at a real line."""
    cursor = conn.execute(
        "UPDATE trendlines SET enabled = 0 WHERE id = ? AND enabled = 1", (int(line_id),)
    )
    conn.commit()
    return cursor.rowcount > 0


def update_trendline_anchors(conn: sqlite3.Connection, line_id: int, anchors: Sequence[dict]) -> bool:
    ordered = sorted(
        ({"time": int(anchor["time"]), "price": float(anchor["price"])} for anchor in anchors),
        key=lambda anchor: anchor["time"],
    )
    cursor = conn.execute(
        "UPDATE trendlines SET anchors = ? WHERE id = ? AND enabled = 1 AND kind = 'curve'",
        (json.dumps(ordered), int(line_id)),
    )
    conn.commit()
    return cursor.rowcount > 0


# --------------------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------------------
def record_alert(
    conn: sqlite3.Connection,
    code: str,
    *,
    hour_start: Any,
    box_type: str,
    box_id: int | None,
    visit_id: int | None,
    kind: str,
    message: str,
) -> int | None:
    """Claim the right to send one alert; returns its row id, or None if taken.

    The UNIQUE constraint is the rate limiter: whoever inserts first owns that
    (hour, side, kind) slot, so a restart cannot resend.  ``ok`` stays NULL until
    :func:`mark_alert_sent` records the outcome.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO telegram_alerts
            (code, hour_start, box_type, box_id, visit_id, kind, sent_at, ok, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            code,
            _text(hour_start),
            box_type,
            box_id,
            visit_id,
            kind,
            _text(market_clock.now()),
            message,
        ),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def mark_alert_sent(conn: sqlite3.Connection, alert_id: int, ok: bool) -> None:
    conn.execute(
        "UPDATE telegram_alerts SET ok = ?, sent_at = ? WHERE id = ?",
        (1 if ok else 0, _text(market_clock.now()), int(alert_id)),
    )
    conn.commit()


def last_alert_at(
    conn: sqlite3.Connection, code: str, box_type: str, kind: str
) -> pd.Timestamp | None:
    """Newest ``hour_start`` claimed for this (key, rule), or None.

    Rule alerts store the triggering minute in ``hour_start``, so a cooldown is
    measured in market time and survives a restart.
    """
    row = conn.execute(
        "SELECT MAX(hour_start) AS last FROM telegram_alerts "
        "WHERE code = ? AND box_type = ? AND kind = ?",
        (code, box_type, kind),
    ).fetchone()
    return pd.Timestamp(row["last"]) if row and row["last"] else None


def alert_exists(conn: sqlite3.Connection, code: str, box_type: str, kind: str) -> bool:
    return last_alert_at(conn, code, box_type, kind) is not None


def load_alerts(conn: sqlite3.Connection, code: str, limit: int = 50) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM telegram_alerts WHERE code = ? ORDER BY id DESC LIMIT ?",
        conn,
        params=[code, int(limit)],
    )


# --------------------------------------------------------------------------------------
# Backfill bookkeeping
# --------------------------------------------------------------------------------------
def mark_backfill_done(
    conn: sqlite3.Connection, code: str, task: str, key: str, rows: int, note: str = ""
) -> None:
    conn.execute(
        """
        INSERT INTO backfill_progress (code, task, key, done_at, rows, note)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, task, key) DO UPDATE SET
            done_at=excluded.done_at, rows=excluded.rows, note=excluded.note
        """,
        (code, task, key, _text(market_clock.now()), int(rows), note),
    )
    conn.commit()


def backfill_done_keys(conn: sqlite3.Connection, code: str, task: str) -> set[str]:
    rows = conn.execute(
        "SELECT key FROM backfill_progress WHERE code = ? AND task = ?", (code, task)
    ).fetchall()
    return {row["key"] for row in rows}
