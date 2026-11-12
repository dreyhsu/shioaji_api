"""Isolated P&L ledger, read-only reports and calendar routes.

Only the existing trading thread writes broker-confirmed fills. HTTP readers never
initialize schemas, consult a broker, or infer fill dates from order update times.
"""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3

TAIPEI = timezone(timedelta(hours=8), 'Asia/Taipei')
WEB_DIR = Path(__file__).resolve().parent / 'web' / 'pnl_calendar'
SCHEMA = """
CREATE TABLE IF NOT EXISTS pnl_fills (
  order_id TEXT NOT NULL, phase TEXT NOT NULL, fill_key TEXT NOT NULL,
  broker_trade_id TEXT NOT NULL, filled_at TEXT NOT NULL, fill_date TEXT NOT NULL,
  quantity INTEGER NOT NULL, price REAL NOT NULL,
  PRIMARY KEY(order_id, phase, fill_key)
);
CREATE INDEX IF NOT EXISTS idx_pnl_fills_date ON pnl_fills(fill_date, order_id);
CREATE INDEX IF NOT EXISTS idx_pnl_entry_trade ON managed_orders(entry_trade_id);
CREATE INDEX IF NOT EXISTS idx_pnl_exit_trade ON managed_orders(exit_trade_id);
CREATE TABLE IF NOT EXISTS pnl_order_metadata (
  order_id TEXT PRIMARY KEY, multiplier REAL, currency TEXT,
  entry_error TEXT, exit_error TEXT, updated_at TEXT NOT NULL
);
"""


def value(obj, name, default=None):
    try:
        result = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        return default if result is None else result
    except Exception:
        return default


def positive(number):
    try:
        number = float(number)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def fill_time(deal):
    stamp = value(deal, 'datetime')
    if stamp is None:
        ts = positive(value(deal, 'ts'))
        if ts is None:
            raise ValueError('missing_fill_time')
        stamp = datetime.fromtimestamp(ts, timezone.utc)
    elif isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp)
    if not isinstance(stamp, datetime):
        raise ValueError('invalid_fill_time')
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TAIPEI)
    return stamp.astimezone(TAIPEI).isoformat(timespec='microseconds')


def normalized_fills(trade):
    """Normalize a complete cumulative deal list; identical anonymous fills keep multiplicity."""
    status = value(trade, 'status')
    deals = value(status, 'deals', [])
    if isinstance(deals, dict):
        deals = list(deals.values())
    rows, occurrences, seen = [], Counter(), {}
    for deal in deals:
        quantity = value(deal, 'quantity')
        if isinstance(quantity, bool) or positive(quantity) is None or int(quantity) != float(quantity):
            raise ValueError('invalid_fill_quantity')
        price = positive(value(deal, 'price'))
        if price is None:
            raise ValueError('missing_fill_price')
        stamp = fill_time(deal)
        seq = value(deal, 'seq')
        if seq:
            key = 'seq:' + str(seq)
        else:
            content = json.dumps([stamp, int(quantity), price], separators=(',', ':'))
            occurrences[content] += 1
            key = 'anon:' + hashlib.sha256(content.encode()).hexdigest() + ':' + str(occurrences[content])
        row = (key, stamp, stamp[:10], int(quantity), price)
        if key in seen:
            if seen[key] != row:
                raise ValueError('conflicting_fill_identity')
            continue
        seen[key] = row
        rows.append(row)
    return rows


def save_fills(conn, order, *, entry=None, exit_trade=None, multiplier=None, currency=None, observed_at):
    """Called inside the managed-order transaction; missing metadata never guesses a value."""
    currency = str(currency).strip().upper() if currency else None
    conn.execute("""INSERT INTO pnl_order_metadata(order_id, multiplier, currency, updated_at)
        VALUES (?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET
        multiplier=COALESCE(pnl_order_metadata.multiplier, excluded.multiplier),
        currency=COALESCE(pnl_order_metadata.currency, excluded.currency), updated_at=excluded.updated_at
        WHERE (pnl_order_metadata.multiplier IS NULL AND excluded.multiplier IS NOT NULL)
           OR (pnl_order_metadata.currency IS NULL AND excluded.currency IS NOT NULL)""",
        (order['id'], positive(multiplier), currency, observed_at))
    for phase, trade, expected, trade_id in (
        ('entry', entry, order['filled_quantity'], order['entry_trade_id']),
        ('exit', exit_trade, order['exit_filled_quantity'], order['exit_trade_id']),
    ):
        if trade is None:
            continue
        if expected > 0 and not value(value(trade, 'status'), 'deals', []):
            # A relogin's order summary can omit old deals. A previously verified
            # ledger with exactly the same quantity remains valid evidence.
            saved = conn.execute('SELECT sum(quantity) FROM pnl_fills WHERE order_id=? AND phase=? AND broker_trade_id=?',
                                 (order['id'], phase, str(trade_id or ''))).fetchone()[0]
            if saved == int(expected):
                conn.execute(f'UPDATE pnl_order_metadata SET {phase}_error=NULL WHERE order_id=?', (order['id'],))
                continue
        error = None
        try:
            rows = normalized_fills(trade)
            if sum(row[3] for row in rows) != int(expected):
                raise ValueError('incomplete_fill_details')
        except (ValueError, TypeError, OverflowError, OSError):
            rows = None
            error = 'incomplete_fill_details'
        conn.execute(f'UPDATE pnl_order_metadata SET {phase}_error=?, updated_at=? WHERE order_id=? AND {phase}_error IS NOT ?',
                     (error, observed_at, order['id'], error))
        if rows is not None:
            existing = [tuple(row) for row in conn.execute('''SELECT fill_key, filled_at, fill_date, quantity, price
                FROM pnl_fills WHERE order_id=? AND phase=? ORDER BY fill_key''', (order['id'], phase))]
            if existing == sorted(rows):
                continue
            # The SDK exposes cumulative lists. Replace this phase atomically so
            # later seq/time/price corrections cannot duplicate anonymous rows.
            conn.execute('DELETE FROM pnl_fills WHERE order_id=? AND phase=?', (order['id'], phase))
            conn.executemany('INSERT INTO pnl_fills VALUES (?,?,?,?,?,?,?,?)',
                [(order['id'], phase, key, str(trade_id or ''), at, day, qty, price)
                 for key, at, day, qty, price in rows])
            conn.execute('UPDATE pnl_order_metadata SET updated_at=? WHERE order_id=?', (observed_at, order['id']))


def _decimal(number):
    return Decimal(str(number))


def report(conn, mode, month, contract_code=None):
    """Build one consistent SQLite read snapshot, including global undated exceptions."""
    simulation = int(mode != 'production')
    params = [simulation]
    where = 'o.simulation=?'
    if contract_code:
        where += ' AND o.contract_code=?'
        params.append(contract_code)
    contracts = [row[0] for row in conn.execute(
        'SELECT DISTINCT contract_code FROM managed_orders WHERE simulation=? ORDER BY contract_code', (simulation,))]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'pnl_fills', 'pnl_order_metadata'} <= tables:
        pending = conn.execute(f'SELECT COUNT(*) FROM managed_orders o WHERE {where} AND exit_filled_quantity>0', params).fetchone()[0]
        return [], contracts, pending, None, False
    orders = conn.execute(f"""SELECT o.*, m.multiplier, m.currency, m.entry_error, m.exit_error, m.updated_at AS ledger_updated_at
        FROM managed_orders o LEFT JOIN pnl_order_metadata m ON m.order_id=o.id
        WHERE {where} AND o.exit_filled_quantity>0""", params).fetchall()
    details, pending, latest = [], 0, None
    for raw in orders:
        order = dict(raw)
        fills = conn.execute('SELECT * FROM pnl_fills WHERE order_id=? ORDER BY filled_at, fill_key', (order['id'],)).fetchall()
        entries = [dict(row) for row in fills if row['phase'] == 'entry']
        exits = [dict(row) for row in fills if row['phase'] == 'exit']
        # Final entry quantity must be known before allocating weighted cost.
        complete = (order['entry_status'] in {'Filled', 'Cancelled', 'Failed', 'Rejected'}
            and sum(row['quantity'] for row in entries) == order['filled_quantity'] > 0
            and sum(row['quantity'] for row in exits) == order['exit_filled_quantity']
            and 0 < order['exit_filled_quantity'] <= order['filled_quantity']
            and positive(order['multiplier']) is not None and order['currency'] == 'TWD'
            and not order['entry_error'] and not order['exit_error'] and order['action'] in {'Buy', 'Sell'})
        if not complete:
            pending += 1
            continue
        latest = max(latest or '', order['ledger_updated_at'] or '') or None
        average = sum(_decimal(row['price']) * row['quantity'] for row in entries) / order['filled_quantity']
        direction = 1 if order['action'] == 'Buy' else -1
        for fill in exits:
            if fill['fill_date'][:7] != month:
                continue
            pnl = (_decimal(fill['price']) - average) * direction * fill['quantity'] * _decimal(order['multiplier'])
            details.append(dict(order_id=order['id'], contract_code=order['contract_code'], action=order['action'],
                filled_at=fill['filled_at'], date=fill['fill_date'], quantity=fill['quantity'],
                entry_price=float(average), exit_price=fill['price'], multiplier=order['multiplier'], gross_pnl=float(pnl)))
    return sorted(details, key=lambda row: (row['filled_at'], row['order_id'])), contracts, pending, latest, True


def aggregate(rows):
    return dict(gross_pnl=float(sum((_decimal(row['gross_pnl']) for row in rows), Decimal(0))),
                order_count=len({row['order_id'] for row in rows}), quantity=sum(row['quantity'] for row in rows))


def create_router(context):
    """context() supplies db_path/mode/now/lock without importing Dashboard."""
    from contextlib import nullcontext
    import db
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import FileResponse
    router = APIRouter()

    @router.get('/pnl-calendar')
    def page():
        return FileResponse(WEB_DIR / 'index.html')

    def load(month, contract_code):
        settings = context()
        mode = settings['mode']
        if mode not in {'mock', 'simulation', 'production'}:
            raise HTTPException(503, '損益模式尚未設定')
        try:
            with settings.get('lock') or nullcontext():
                with db.read_session(settings['db_path']) as conn:
                    conn.execute('BEGIN')
                    rows, contracts, pending, updated, available = report(conn, mode, month, contract_code)
                now = settings['now']()
                if hasattr(now, 'to_pydatetime'):
                    now = now.to_pydatetime()
                if now.tzinfo is None:
                    now = now.replace(tzinfo=TAIPEI)
                today = now.astimezone(TAIPEI).date().isoformat()
        except (sqlite3.Error, OSError):
            raise HTTPException(503, '損益資料暫時無法讀取，請稍後重試') from None
        return rows, dict(mode=mode, currency='TWD', timezone='Asia/Taipei', basis='realized_gross',
            month=month, today=today, contracts=contracts, contract_code=contract_code,
            incomplete_order_count=pending, incomplete_scope='all_history', ledger_available=available,
            updated_at=updated)

    def valid_month(month):
        try:
            parsed = date.fromisoformat(month + '-01')
            if parsed.isoformat()[:7] != month:
                raise ValueError
        except ValueError:
            raise HTTPException(422, '月份格式須為 YYYY-MM') from None
        return parsed

    @router.get('/api/pnl-calendar')
    def month_report(month: str = Query(pattern=r'^\d{4}-\d{2}$'), contract_code: str | None = Query(default=None, min_length=1, max_length=24)):
        start = valid_month(month)
        rows, metadata = load(month, contract_code)
        days = []
        for day in range(1, calendar.monthrange(start.year, start.month)[1] + 1):
            stamp = start.replace(day=day).isoformat()
            daily = [row for row in rows if row['date'] == stamp]
            days.append(dict(date=stamp, **aggregate(daily)))
        return dict(**metadata, summary=aggregate(rows), days=days)

    @router.get('/api/pnl-calendar/day')
    def day_report(date: str = Query(pattern=r'^\d{4}-\d{2}-\d{2}$'), contract_code: str | None = Query(default=None, min_length=1, max_length=24)):
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(422, '日期無效') from None
        valid_month(date[:7])
        rows, metadata = load(date[:7], contract_code)
        rows = [row for row in rows if row['date'] == date]
        return dict(**metadata, date=date, summary=aggregate(rows), fills=rows)

    return router
