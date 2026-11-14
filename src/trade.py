"""Independent order service. No login, orders or database writes on import.

Run: conda run --no-capture-output -n tss python -X utf8 -u src/trade.py
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import threading
import uuid

import pandas as pd

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

import db
from connection_recovery import error_kind
import market_health as mh
from service_control import ServiceControl, run_server
from runtime_config import RuntimeConfig, DEFAULT_RUNTIME, skip_mock
from market_fetcher import FetcherSettings, DEFAULT_CONFIG, taipei_now
from trade_service import IndependentTradingService
from latency_trace import trace_scope, order_link, link_order
from trading import RiskConfirmation, RiskConflict
from trading_models import (OrderIn, TradeLevelsIn, TradeCurveIn, TradeCurveSelectionIn,
                            TradeRiskSettingsIn, ContractSubscriptionIn, CurveEntryIn, ExternalPositionIn)

COMMAND_MODELS = dict(place_order=OrderIn, arm_curve=CurveEntryIn, subscribe=ContractSubscriptionIn,
    update_levels=TradeLevelsIn, update_risk=TradeRiskSettingsIn, update_curve=TradeCurveIn,
    select_curve=TradeCurveSelectionIn, adopt_external=ExternalPositionIn)
TARGETS = {'cancel_entry', 'update_levels', 'update_risk', 'update_curve', 'select_curve'}


class Command(BaseModel):
    id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    name: Literal['place_order', 'arm_curve', 'cancel_curve', 'subscribe', 'cancel_entry',
                  'update_levels', 'update_risk', 'update_curve', 'select_curve', 'adopt_external']
    target: str | None = Field(default=None, max_length=128)
    payload: dict = Field(default_factory=dict)

    def validated(self):
        if self.name in TARGETS and not self.target:
            raise ValueError('缺少委託 ID')
        if self.name not in TARGETS and self.target is not None:
            raise ValueError('此指令不接受委託 ID')
        model = COMMAND_MODELS.get(self.name)
        if model:
            if self.payload.keys() - model.model_fields.keys():
                raise ValueError('指令包含不支援的欄位')
            return model.model_validate(self.payload).model_dump()
        if self.payload:
            raise ValueError('此指令不接受額外參數')
        return {}


SCHEMA = '''
CREATE TABLE IF NOT EXISTS trade_commands (
    id TEXT PRIMARY KEY, code TEXT NOT NULL, mode TEXT NOT NULL, request TEXT NOT NULL,
    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    result TEXT, http_status INTEGER);
CREATE TABLE IF NOT EXISTS trade_snapshots (
    code TEXT NOT NULL, mode TEXT NOT NULL, updated_at TEXT NOT NULL, document TEXT NOT NULL,
    PRIMARY KEY(code,mode));
'''


class TradeRuntime:
    def __init__(self, service):
        self.service = service
        self.mode = 'simulation' if service.simulation else 'production'
        self.stop = threading.Event()
        self.thread = None
        self.closed = False
        self.last_window = None
        self._accepted_mono = {}
        self._timing_lock = threading.Lock()
        self._published_order_versions = {}
        service._health = mh.HealthPublisher(service.db_path, 'trading', service.code, self.mode, clock=service._now)
        service._health.acquire()  # also excludes the legacy embedded trading owner
        try:
            service.control = ServiceControl(service.db_path, f'trading-{service.code}', self.mode, broker=True).start()
            with db.session(service.db_path) as conn:
                conn.executescript(SCHEMA)
                interrupted = conn.execute("SELECT id FROM trade_commands WHERE state='processing' AND code=? AND mode=?",
                                           (service.code, self.mode)).fetchall()
                conn.execute("UPDATE trade_commands SET state='unknown', result=?, http_status=409 WHERE state='processing' AND code=? AND mode=?",
                             (json.dumps({'detail': '程序中斷，指令結果待核對；不自動重送'}), service.code, self.mode))
                conn.commit()
            for row in interrupted:
                service.latency.record('command_recovered_unknown', trace_id=row[0], command_id=row[0], state='unknown')
            self.save_snapshot()
        except BaseException:
            if service.control:
                service.control.close()
            service._health.close()
            service._health = None
            raise

    def save_snapshot(self):
        service = self.service
        snapshot = service.snapshot()
        snapshot_id = uuid.uuid4().hex
        snapshot['status']['latency'] = dict(snapshot_id=snapshot_id,
                                            generated_at=mh.iso(service._now()))
        with db.session(service.db_path) as conn:
            conn.execute('INSERT OR REPLACE INTO trade_snapshots VALUES (?,?,?,?)',
                         (service.code, self.mode, mh.iso(service._now()), json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
            conn.commit()
        service.latency.record('snapshot_published', snapshot_id=snapshot_id, generation=service._generation)
        for order in snapshot['orders']:
            version = tuple(order.get(key) for key in ('risk_revision', 'entry_status', 'exit_status',
                                                      'filled_quantity', 'exit_filled_quantity'))
            if self._published_order_versions.get(order['id']) != version:
                service.latency.record('order_snapshot_published', snapshot_id=snapshot_id,
                    order_id=order['id'], generation=service._generation, **order_link(service.db_path, order['id']))
                self._published_order_versions[order['id']] = version
        present = {order['id'] for order in snapshot['orders']}
        self._published_order_versions = {key: value for key, value in self._published_order_versions.items() if key in present}
        service.latency.flush()

    def submit(self, command):
        if self.service.control and self.service.control.stop_requested:
            raise HTTPException(503, '舊服務正在登出，暫停接收交易指令')
        payload = command.validated()
        document = json.dumps(dict(name=command.name, target=command.target, payload=payload), sort_keys=True)
        with db.session(self.service.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM trade_commands WHERE id=?', (command.id,)).fetchone()
            if existing:
                if (existing['code'], existing['mode'], existing['request']) != (self.service.code, self.mode, document):
                    raise HTTPException(409, '指令 ID 已用於不同的請求')
            else:
                now = mh.iso(self.service._now())
                conn.execute('INSERT INTO trade_commands VALUES (?,?,?,?,?,?,?,?,?)',
                    (command.id, self.service.code, self.mode, document, 'accepted', now, now, None, None))
                with self._timing_lock:
                    conn.commit()
                    if len(self._accepted_mono) >= 4096:
                        self._accepted_mono.pop(next(iter(self._accepted_mono)))
                    self._accepted_mono[command.id] = self.service.latency.record(
                        'command_accepted', trace_id=command.id, command_id=command.id, order_id=command.target,
                        generation=self.service._generation, state='accepted')
        self.service._wake.set()
        return self.command(command.id)

    def command(self, command_id):
        with db.read_session(self.service.db_path) as conn:
            row = conn.execute('SELECT * FROM trade_commands WHERE id=? AND code=? AND mode=?',
                               (command_id, self.service.code, self.mode)).fetchone()
        if row is None:
            raise HTTPException(404, '找不到指令')
        result = dict(row)
        result['result'] = json.loads(result['result']) if result['result'] else None
        return result

    def execute(self, command):
        s, payload, target = self.service, command['payload'], command['target']
        if command['name'] == 'place_order':
            return s.place_order(payload)
        if command['name'] == 'adopt_external':
            return s.adopt_external_position(**payload)
        if command['name'] == 'arm_curve':
            return s.curve_entry.arm(**payload)
        if command['name'] == 'cancel_curve':
            return s.curve_entry.cancel()
        if command['name'] == 'subscribe':
            return s.subscribe_contract(payload['contract_code'])
        if command['name'] == 'cancel_entry':
            return s.cancel_entry(target)
        if command['name'] == 'update_levels':
            return s.update_levels(target, payload['stop_price'], payload['target_price'])
        if command['name'] == 'update_risk':
            return s.update_risk_settings(target, **payload)
        if command['name'] == 'update_curve':
            return s.update_curve(target, payload['target_price'])
        return s.set_order_curve(target, payload['curve_id'])

    def process_commands(self):
        # All broker I/O stays on this one worker; HTTP handlers only enqueue/read.
        with db.session(self.service.db_path) as conn:
            row = conn.execute("SELECT * FROM trade_commands WHERE code=? AND mode=? AND state='accepted' ORDER BY rowid LIMIT 1",
                               (self.service.code, self.mode)).fetchone()
            if row is None:
                return
            claimed = conn.execute("UPDATE trade_commands SET state='processing', updated_at=? WHERE id=? AND state='accepted'",
                                   (mh.iso(self.service._now()), row['id'])).rowcount
            conn.commit()
        if not claimed:
            return
        with self._timing_lock:
            accepted = self._accepted_mono.pop(row['id'], None)
        began = self.service.latency.record('command_processing', trace_id=row['id'], command_id=row['id'],
            order_id=json.loads(row['request']).get('target'), started=accepted,
            generation=self.service._generation, state='processing')
        state, status = 'succeeded', 200
        try:
            with self.service._lock, trace_scope(trace_id=row['id'], command_id=row['id'],
                                                order_id=json.loads(row['request']).get('target')):
                result = self.execute(json.loads(row['request']))
                if isinstance(result, dict):
                    identities = result.get('order_ids', [])
                    if 'entry_trade_id' in result:
                        identities = [*identities, result['id']]
                    if identities:
                        try:
                            with db.session(self.service.db_path) as conn:
                                for local_id in identities:
                                    if not link_order(conn, local_id):
                                        self.service.latency.error = 'OrderLinkWriteFailed'
                                conn.commit()
                        except Exception as error:
                            self.service.latency.error = type(error).__name__
        except (RiskConfirmation, RiskConflict) as error:
            state, status = 'failed', 409
            result = {'detail': {'code': 'confirmation_required' if isinstance(error, RiskConfirmation) else 'risk_conflict',
                                 'message': str(error)}}
        except (ValueError, KeyError) as error:
            if json.loads(row['request'])['name'] == 'cancel_entry':
                state, status, result = 'unknown', 409, {'detail': '撤單結果待核對，不自動重送'}
            else:
                state, status, result = 'failed', 422, {'detail': str(error)}
        except Exception as error:
            state, status = 'unknown', 409
            result = {'detail': '指令結果待核對，不自動重送', 'error_type': type(error).__name__}
        with db.session(self.service.db_path) as conn:
            conn.execute('UPDATE trade_commands SET state=?, result=?, http_status=?, updated_at=? WHERE id=?',
                         (state, json.dumps(result, ensure_ascii=False, allow_nan=False), status,
                          mh.iso(self.service._now()), row['id']))
            conn.commit()
        self.service.latency.record('command_completed', trace_id=row['id'], command_id=row['id'],
            order_id=result.get('id') if isinstance(result, dict) and 'entry_trade_id' in result else json.loads(row['request']).get('target'),
            started=began, state=state)

    def step(self):
        s = self.service
        if s.control and s.control.stop_requested:
            self.stop.set()
            return
        with s._lock:
            try:
                window = s.window()
                if window is not None and self.last_window is None:
                    s._recovery.success()  # a new scheduled session still requires broker reconciliation
                self.last_window = window
                if s._api is not None and window != s.session_window:
                    # Reconcile before closing, without making strategy decisions after the cutoff.
                    s.last_rpc_sync = float('-inf')
                    trades = s._trades_by_id()
                    for order in s._active_orders():
                        s._persist_trade_state(order['id'], entry=trades.get(order['entry_trade_id']),
                            exit_trade=trades.get(order.get('exit_trade_id')))
                    s._close_api()
                if window is not None:
                    if s._api is None and not s._recovery.pending:
                        s.ensure_connected()
                    elif s._api is None:
                        s._maintain_connection()
                    if s._api is not None and s.window() != window:
                        s._close_api()
                        window = s.window()
                    if s._api is not None:
                        s.process_market_update()
                        if not s._recovery.pending and s._monotonic() - s.last_margin >= s.health_settings.order_sync_seconds:
                            s.last_margin = s._monotonic()
                            s.margin_cache = s.margin()
                self.process_commands()
                s._publish_health(state=('scheduled_closed' if window is None else 'healthy' if s._broker_ready and not s._recovery.pending else 'degraded'),
                    operation=None, cycle_completed_at=mh.iso(s._now()))
                self.save_snapshot()
            except Exception as error:
                s._connect_error = type(error).__name__
                kind = error_kind(error)
                if kind in {'session', 'transport', 'authentication', 'rate_limit'}:
                    s._broker_ready = False
                    s._reconcile_required = True
                    s._recovery.fail(kind, immediate=True)
                s.event('trade_cycle_failed', reason=kind, error_type=type(error).__name__)
                s._publish_health(state='failed', operation=None, error={'kind': kind, 'type': type(error).__name__})
                try:
                    self.save_snapshot()
                except Exception:
                    pass  # keep the loop alive; the health file still reports failure

    def start(self):
        def run():
            while not self.stop.is_set():
                self.service._wake.clear()
                try:
                    self.step()
                except Exception as error:
                    print(f'trade worker failed: {type(error).__name__}', flush=True)
                self.service._wake.wait(.5)
        self.thread = threading.Thread(target=run, name='independent-trade', daemon=True)
        self.thread.start()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        self.service._wake.set()
        if self.thread:
            self.thread.join()  # keep ownership until in-flight broker calls finish
        try:
            self.service.close()  # no automatic close/cancel of broker positions
        finally:
            self.service.latency.flush()
            if self.service.control:
                self.service.control.close()


def create_app(service=None, *, start_loop=True, **service_options):
    @asynccontextmanager
    async def lifespan(app):
        owned_service = service or IndependentTradingService(**service_options)
        runtime = TradeRuntime(owned_service)
        app.state.runtime = runtime
        if start_loop:
            runtime.start()
        try:
            yield
        finally:
            runtime.close()
    app = FastAPI(title='Independent trade service', lifespan=lifespan)

    @app.post('/api/commands', status_code=202)
    def submit(command: Command):
        try:
            return app.state.runtime.submit(command)
        except (ValueError, ValidationError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get('/api/commands/{command_id}')
    def command(command_id: str):
        return app.state.runtime.command(command_id)

    @app.get('/api/ready')
    def ready():
        return {'pid': os.getpid(), 'mode': app.state.runtime.mode}

    @app.get('/api/snapshot')
    def snapshot():
        runtime = app.state.runtime
        with db.read_session(runtime.service.db_path) as conn:
            row = conn.execute('SELECT * FROM trade_snapshots WHERE code=? AND mode=?',
                               (runtime.service.code, runtime.mode)).fetchone()
        result = json.loads(row['document'])
        result.update(code=runtime.service.code, mode=runtime.mode,
                      db_path=str(runtime.service.db_path.resolve()), updated_at=row['updated_at'])
        health = mh.read_health(mh.health_path(runtime.service.db_path, 'trading', runtime.service.code))
        age = (pd.Timestamp(runtime.service._now()) - pd.Timestamp(health.get('heartbeat_at'))).total_seconds()
        snapshot_age = (pd.Timestamp(runtime.service._now()) - pd.Timestamp(row['updated_at'])).total_seconds()
        failure = health.get('state') in {'failed', 'stopped', 'logout_unconfirmed', 'recovering', 'connecting'}
        if failure or not (-5 <= age <= runtime.service.health_settings.heartbeat_seconds
                           and -5 <= snapshot_age <= runtime.service.health_settings.heartbeat_seconds):
            result['status'].update(connected=False, broker_ready=False,
                error='trade 背景工作失敗或尚未就緒' if failure else 'trade 背景快照已過期')
            for key in ('fixed_exit', 'breakeven', 'curve_exit', 'curve_entry'):
                if key in result['status'].get('protection', {}):
                    result['status']['protection'][key] = False
        return result
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--db', type=Path, default=None)
    parser.add_argument('--code', default=None)
    parser.add_argument('--config', type=Path, default=None)
    parser.add_argument('--health-config', type=Path, default=None)
    parser.add_argument('--settings', type=Path, default=None)
    parser.add_argument('--host', choices=('127.0.0.1', '::1'), default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args(argv)
    runtime_config = RuntimeConfig.load(args.runtime_config)
    if skip_mock(runtime_config.mode):
        return 0
    args.code = args.code or runtime_config.signal_code
    if not re.fullmatch('[A-Za-z0-9]{1,24}', args.code):
        parser.error('invalid code')
    app = create_app(db_path=args.db or runtime_config.unified_db, code=args.code, simulation=runtime_config.mode == 'simulation',
        fetcher_settings=FetcherSettings.load(args.config or runtime_config.market_config), health_config=args.health_config or runtime_config.health_config,
        settings_path=args.settings or runtime_config.trading_config, enable_telegram=not args.no_telegram)
    run_server(app, control=lambda: getattr(getattr(app.state, 'runtime', None), 'service', None), host=args.host or runtime_config.trade_host, port=args.port or runtime_config.trade_port, workers=1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
