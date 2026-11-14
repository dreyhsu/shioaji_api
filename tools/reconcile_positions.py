"""Read-only broker check and backed-up local repair while trade is stopped.

Default is a preview in a copied database. --apply updates local records only;
this tool never activates CA, places/cancels orders, or starts services.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace as NS
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import db
from file_lease import FileLease
import market_health as mh
from runtime_config import RuntimeConfig, DEFAULT_RUNTIME
from service_control import control_folder
from shioaji_client import connect
from trading import TradingService, _trade_id, _enum_text, _value
from pnl_calendar import normalized_fills


def with_confirmed_fills(current, saved):
    """A later login may return a terminal summary without historical deals.

    Keep the fresh status; reuse earlier broker fills only for the same immutable
    order identity and exact cumulative quantity. Never override new deal data.
    """
    if saved is None or _value(current.status, 'deals', []):
        return current
    for field in ('id', 'action', 'octype'):
        if _enum_text(_value(current.order, field)) != _enum_text(_value(saved.order, field)):
            return current
    if str(current.contract.code) != str(saved.contract.code):
        return current
    if _enum_text(current.status.status) != _enum_text(saved.status.status):
        return current
    try:
        fills = normalized_fills(saved)
    except (ValueError, TypeError, OverflowError, OSError):
        return current
    if not fills or sum(fill[3] for fill in fills) != int(_value(current.status, 'deal_quantity', 0)):
        return current
    status = NS(**{key: _value(current.status, key) for key in
        ('id', 'status', 'status_code', 'msg', 'order_datetime', 'deal_quantity', 'cancel_quantity')})
    status.deals = saved.status.deals
    return NS(order=current.order, contract=current.contract, status=status)


def now():
    return datetime.now(ZoneInfo('Asia/Taipei')).isoformat()


def objects(value):
    if isinstance(value, dict):
        return NS(**{key: objects(item) for key, item in value.items()})
    return [objects(item) for item in value] if isinstance(value, list) else value


def active(path, code, simulation):
    with db.read_session(path) as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM managed_orders WHERE code=? AND simulation=? AND active=1 ORDER BY id',
            (code, int(simulation)))]


def copy_updates(path, work, before):
    """Compare original rows under a write lock; transfer only reviewed order data."""
    with db.read_session(work) as planned, db.session(path) as live:
        live.execute('BEGIN IMMEDIATE')
        for old in before:
            current = live.execute('SELECT * FROM managed_orders WHERE id=?', (old['id'],)).fetchone()
            if current is None or any(current[key] != item for key, item in old.items()):
                raise RuntimeError('local order changed during reconciliation')
        for old in before:
            row = dict(planned.execute('SELECT * FROM managed_orders WHERE id=?', (old['id'],)).fetchone())
            columns = [key for key in row if key != 'id']
            live.execute('UPDATE managed_orders SET ' + ','.join(key + '=?' for key in columns) + ' WHERE id=?',
                (*[row[key] for key in columns], row['id']))
            for table in ('pnl_fills', 'pnl_order_metadata'):
                live.execute(f'DELETE FROM {table} WHERE order_id=?', (row['id'],))
                for record in planned.execute(f'SELECT * FROM {table} WHERE order_id=?', (row['id'],)):
                    live.execute(f'INSERT INTO {table} VALUES (' + ','.join('?' for _ in record) + ')', tuple(record))
            audit = planned.execute('SELECT * FROM order_reconciliations WHERE source_order_id=?', (row['id'],)).fetchone()
            if audit is not None:
                live.execute('INSERT INTO order_reconciliations VALUES (?,?,?,?,?)', tuple(audit))
            for table in ('broker_order_identities', 'external_close_reconciliations'):
                record = planned.execute(f'SELECT * FROM {table} WHERE order_id=?', (row['id'],)).fetchone()
                if record is not None:
                    live.execute(f'INSERT OR REPLACE INTO {table} VALUES (' + ','.join('?' for _ in record) + ')', tuple(record))
        live.commit()


def run(args):
    config = RuntimeConfig.load(args.runtime_config)
    if config.mode == 'mock':
        raise ValueError('Mock does not use a broker account')
    simulation = config.mode == 'simulation'
    folder = ROOT / 'out' / ('position-repair-' + datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    report = dict(started_at=now(), mode=config.mode, applied=False, instance=uuid.uuid4().hex)
    def session_event(event):
        with (folder / 'session_events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(timestamp=now(), timezone='Asia/Taipei', role='position_reconciliation',
                mode=config.mode, instance=report['instance'], generation=1, event=event)) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    try:
        with ExitStack() as locks:
            # Exclude both launcher startup and direct/legacy trade writers. Never stop another owner.
            for path in (control_folder(config.unified_db) / 'startup.lock',
                         control_folder(config.unified_db) / f'trading-{config.signal_code}.owner.lock',
                         mh.health_path(config.unified_db, 'trading', config.signal_code).with_suffix('.lock')):
                locks.enter_context(FileLease(path))
            before = active(config.unified_db, config.signal_code, simulation)
            report['before'] = before
            backup, work = folder / 'before.db', folder / 'preview.db'
            with sqlite3.connect(config.unified_db.resolve().as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(backup) as dest:
                    source.backup(dest)
            with sqlite3.connect(backup) as source, sqlite3.connect(work) as dest:
                source.backup(dest)
            report['backup'] = str(backup)
            service = TradingService(work, config.signal_code, simulation=simulation, start_worker=False,
                api_factory=lambda _: None, health_enabled=True, enable_telegram=False,
                settings_path=config.trading_config, health_config=config.health_config)
            with db.session(work):
                pass
            with connect(simulation=simulation, strict_logout=True, session_observer=session_event) as api:
                service._api = api
                service._connected_simulation = simulation
                report['account_suffix'] = str(api.futopt_account.account_id)[-4:]
                evidence = {}
                if args.evidence:
                    raw = Path(args.evidence).read_bytes()
                    saved = json.loads(raw)
                    if saved['mode'] != config.mode or saved['account']['account_suffix'] != report['account_suffix'] or not saved['queries']['orders']['success']:
                        raise ValueError('broker evidence account or mode mismatch')
                    evidence = {str(row['order']['id']): objects(row) for row in saved['queries']['orders']['data']}
                    report['evidence'] = dict(path=str(Path(args.evidence).resolve()), sha256=hashlib.sha256(raw).hexdigest(),
                        queried_at=saved['queries']['orders']['completed_at'])
                # These are the only live broker calls made by the preview engine.
                # Fresh broker records take precedence over the earlier verified fills.
                api.update_status(api.futopt_account, timeout=15000)
                trades = dict(evidence)
                trades.update({str(_trade_id(trade)): with_confirmed_fills(trade, evidence.get(str(_trade_id(trade))))
                               for trade in api.list_trades() if _trade_id(trade)})
                service._trades_by_id = lambda **_: trades
                service._sync_ok = True
                service._sync_managed_orders(force=True)
                report.update(positions=service._broker_positions, position_checked_at=service._last_position_sync_at,
                    ready=service._broker_ready, error=service._reconcile_error,
                    after=active(work, config.signal_code, simulation))
                if not service._broker_ready:
                    raise RuntimeError('broker/local reconciliation is unresolved; original DB was not changed')
                if args.apply:
                    copy_updates(config.unified_db, work, before)
                    report['applied'] = True
                    report['remaining_active'] = active(config.unified_db, config.signal_code, simulation)
    except Exception as error:
        report['error_type'] = type(error).__name__
    finally:
        report['completed_at'] = now()
        (folder / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(str(folder / 'result.json'))
    return 0 if report.get('ready') and (not args.apply or report['applied']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--evidence', type=Path, help='Earlier verified broker fills; account/mode must match')
    parser.add_argument('--apply', action='store_true', help='Apply backed-up local changes after a fresh broker position check')
    raise SystemExit(run(parser.parse_args()))
