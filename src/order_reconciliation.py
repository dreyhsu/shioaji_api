"""Persistent order inspection and reviewed repairs. Broker I/O is worker-only.

Readers use SQLite mode=ro and never initialize schemas, login, or renew health.
Unknown execution documents remain immutable; resolutions are appended separately.
"""
from contextlib import closing
import base64
from datetime import datetime, timedelta
import hashlib
import json
import math
import sqlite3
import uuid
from zoneinfo import ZoneInfo

import db
import external_reconciliation as er
from pnl_calendar import value, normalized_fills
from position_reconciliation import text

TAIPEI = ZoneInfo('Asia/Taipei')
TERMINAL = {'Filled', 'Cancelled', 'Failed', 'Rejected'}
UNCERTAIN = {'unknown', 'submitting', 'cancelling_entry', 'waiting_entry_cancel'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS trade_commands (
 id TEXT PRIMARY KEY, code TEXT NOT NULL, mode TEXT NOT NULL, request TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 result TEXT, http_status INTEGER);
CREATE INDEX IF NOT EXISTS idx_commands_scope ON trade_commands(code,mode,state,created_at);
CREATE TABLE IF NOT EXISTS trade_reconciliation_previews (
 id TEXT PRIMARY KEY, code TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, document TEXT NOT NULL, applied_command_id TEXT);
CREATE TABLE IF NOT EXISTS trade_reconciliation_events (
 id TEXT PRIMARY KEY, command_id TEXT NOT NULL, code TEXT NOT NULL, mode TEXT NOT NULL,
 kind TEXT NOT NULL, target_id TEXT NOT NULL, action TEXT NOT NULL,
 started_at TEXT NOT NULL, completed_at TEXT, state TEXT NOT NULL, document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_reconciliation_target ON trade_reconciliation_events(code,mode,kind,target_id,started_at);
CREATE TABLE IF NOT EXISTS trade_reconciliation_resolutions (
 code TEXT NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL, target_id TEXT NOT NULL,
 event_id TEXT NOT NULL, conclusion TEXT NOT NULL, PRIMARY KEY(code,mode,kind,target_id));
CREATE TABLE IF NOT EXISTS broker_observation_batches (
 id TEXT PRIMARY KEY, code TEXT NOT NULL, mode TEXT NOT NULL, account_key TEXT NOT NULL,
 observed_at TEXT NOT NULL, instance TEXT, generation INTEGER, document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_observation_scope ON broker_observation_batches(code,mode,observed_at);
CREATE TABLE IF NOT EXISTS broker_order_observations (
 code TEXT NOT NULL, mode TEXT NOT NULL, account_key TEXT NOT NULL, broker_trade_id TEXT NOT NULL,
 batch_id TEXT NOT NULL, observed_at TEXT NOT NULL, document TEXT NOT NULL,
 PRIMARY KEY(code,mode,account_key,broker_trade_id));
CREATE TABLE IF NOT EXISTS broker_order_observation_history (
 id TEXT PRIMARY KEY, code TEXT NOT NULL, mode TEXT NOT NULL, broker_trade_id TEXT NOT NULL,
 batch_id TEXT NOT NULL, observed_at TEXT NOT NULL, document TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_observation_history ON broker_order_observation_history(code,mode,broker_trade_id,observed_at);
CREATE TABLE IF NOT EXISTS trade_reconciliation_schema (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO trade_reconciliation_schema VALUES (1);
'''


class Conflict(ValueError):
    """The reviewed state is no longer current or evidence is insufficient."""


class UncertainRepair(RuntimeError):
    """A broker mutation may have happened. Never retry automatically."""


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(obj):
    return hashlib.sha256(dumps(obj).encode()).hexdigest()


def stamp(raw):
    if hasattr(raw, 'to_pydatetime'):
        raw = raw.to_pydatetime()
    if not isinstance(raw, datetime):
        raw = datetime.fromisoformat(str(raw))
    return (raw.replace(tzinfo=TAIPEI) if raw.tzinfo is None else raw.astimezone(TAIPEI)).isoformat()


def mode_of(service):
    return getattr(service, '_ledger_mode', 'simulation' if service.simulation else 'production')


def exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def pending_command(conn, code, mode):
    if not exists(conn, 'trade_commands'):
        return None
    clause = '''AND NOT EXISTS (SELECT 1 FROM trade_reconciliation_resolutions r
        WHERE r.code=c.code AND r.mode=c.mode AND r.kind='command' AND r.target_id=c.id)''' if exists(conn, 'trade_reconciliation_resolutions') else ''
    return conn.execute(f"""SELECT c.id FROM trade_commands c WHERE c.code=? AND c.mode=? AND c.state='unknown'
        AND json_extract(c.request,'$.name') NOT IN ('refresh_reconciliation','preview_reconciliation') {clause} LIMIT 1""",
                        (code, mode)).fetchone()


def write_connection(path):
    # Schema belongs to service startup, never to each tick or repair transaction.
    return closing(db.connect(path))


def describe_trade(service, trade):
    """Whitelist public fields; never serialize SDK objects, accounts or messages."""
    order, status, contract = trade.order, trade.status, trade.contract
    account = value(order, 'account')
    expected = er.account_key(service._api.futopt_account)
    if account is None or er.account_key(account) != expected:
        raise Conflict('券商委託的帳戶身分無法確認')
    broker_id = str(value(order, 'id', '') or value(status, 'id', ''))
    if not broker_id or set(broker_id) == {'0'}:
        raise Conflict('券商委託缺少有效編號')
    quantity = int(value(status, 'order_quantity') or value(order, 'quantity', 0))
    filled = int(value(status, 'deal_quantity', 0))
    cancelled = int(value(status, 'cancel_quantity', 0))
    state = text(value(status, 'status'))
    if quantity < 1 or min(filled, cancelled) < 0 or filled + cancelled > quantity:
        raise Conflict('券商委託口數尚未穩定')
    price = float(value(status, 'modified_price') or value(order, 'price', 0))
    if not math.isfinite(price):
        raise Conflict('券商委託價格無效')
    raw_at = value(status, 'order_datetime')
    at = stamp(raw_at) if raw_at else None
    try:
        fills = normalized_fills(trade)
    except (ValueError, TypeError, OverflowError, OSError):
        fills = []
    fills = [list(item) for item in fills]
    return dict(broker_trade_id=broker_id, account_key=expected,
        contract_code=str(contract.code), action=text(value(order, 'action')),
        octype=text(value(order, 'octype')), quantity=quantity, filled_quantity=filled,
        cancel_quantity=cancelled, working_quantity=0 if state in TERMINAL else quantity-filled-cancelled,
        price=price, price_type=text(value(order, 'price_type')), order_type=text(value(order, 'order_type')),
        broker_status=state, seqno=str(value(order, 'seqno', '') or ''), ordno=str(value(order, 'ordno', '') or ''),
        ordered_at=at, entry_date=er.trading_day(raw_at, service._calendar) if raw_at else None,
        fills=fills, fill_evidence_complete=sum(item[3] for item in fills) == filled)


def record_observations(service, trades, positions):
    """Persist one bounded, already acquired broker snapshot, without broker I/O."""
    code, mode, at = service.code, mode_of(service), stamp(service._now())
    account = er.account_key(service._api.futopt_account)
    records, rejected = [], 0
    for trade in list(trades.values())[:2000]:
        try:
            records.append(describe_trade(service, trade))
        except (ValueError, TypeError, AttributeError):
            rejected += 1
    lots = sorted([dict(contract_code=str(value(p, 'code')), action=text(value(p, 'direction')),
                        quantity=int(value(p, 'quantity', 0))) for p in positions],
                  key=lambda p: (p['contract_code'], p['action'], p['quantity']))
    if any(p['quantity']<0 or p['action'] not in {'Buy','Sell'} for p in lots):
        raise Conflict('券商部位資料無效，保留上次核對證據')
    content = dict(orders=sorted(records, key=lambda r: r['broker_trade_id']), positions=lots,
        rejected_orders=rejected, truncated=len(trades)>2000,
        coverage='目前 session 券商委託快照；不代表全部歷史，查無紀錄不代表從未下單')
    batch_id = uuid.uuid4().hex
    with write_connection(service.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT document FROM broker_observation_batches WHERE code=? AND mode=? ORDER BY rowid DESC LIMIT 1',
                                (code, mode)).fetchone()
        # A new batch is only needed when evidence/session changes. Freshness is
        # published separately by the existing successful broker reconciliation.
        instance = getattr(getattr(service, 'latency', None), 'instance', 'offline')
        generation = getattr(service, '_generation', 0)
        content.update(instance=instance, generation=generation)
        if previous and json.loads(previous[0]) == content:
            service._inspection_pending = None
            return
        conn.execute('INSERT INTO broker_observation_batches VALUES (?,?,?,?,?,?,?,?)',
            (batch_id, code, mode, account, at, instance, generation, dumps(content)))
        for record in records:
            document = dumps(record)
            old = conn.execute('SELECT document FROM broker_order_observations WHERE code=? AND mode=? AND account_key=? AND broker_trade_id=?',
                               (code, mode, account, record['broker_trade_id'])).fetchone()
            conn.execute('INSERT OR REPLACE INTO broker_order_observations VALUES (?,?,?,?,?,?,?)',
                (code, mode, account, record['broker_trade_id'], batch_id, at, document))
            if old is None or old[0] != document:
                conn.execute('INSERT INTO broker_order_observation_history VALUES (?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, code, mode, record['broker_trade_id'], batch_id, at, document))
        conn.commit()
    service._inspection_pending = None


def publish_pending(service):
    pending=getattr(service,'_inspection_pending',None)
    if pending is None:
        return
    if service._api is None or pending[0]!=getattr(service,'_generation',0):
        service._inspection_pending=None
        return
    try:
        record_observations(service,pending[1],pending[2])
        service._inspection_verified_at=stamp(service._now())
        service._inspection_error=None
    except Exception as error:
        service._inspection_pending=None
        service._inspection_error=type(error).__name__


def _target(conn, code, mode, kind, target_id):
    table, id_column = {'order': ('managed_orders', 'id'), 'curve': ('curve_entries', 'id'),
                        'command': ('trade_commands', 'id'), 'broker': ('broker_order_observations', 'broker_trade_id')}[kind]
    scope = 'mode=?' if kind in {'command', 'broker'} else 'simulation=?'
    scope_value = mode if kind in {'command', 'broker'} else int(mode != 'production')
    matches = conn.execute(f'SELECT * FROM {table} WHERE code=? AND {scope} AND {id_column}=? LIMIT 2',
                           (code, scope_value, target_id)).fetchall()
    if len(matches)>1:
        raise Conflict('委託編號跨帳戶重複，須先核對帳戶與完整書號')
    row=matches[0] if matches else None
    if row is None and kind=='broker':
        old=conn.execute('SELECT * FROM broker_external_orders WHERE code=? AND simulation=? AND broker_trade_id=?',
                         (code,int(mode!='production'),target_id)).fetchone()
        if old:
            original=json.loads(old['document'])
            original.update(contract_code=original.get('code'),broker_trade_id=old['broker_trade_id'],broker_status=original.get('status'))
            row=dict(code=code,mode=mode,broker_trade_id=old['broker_trade_id'],account_key=original.get('account_key'),
                     observed_at=old['updated_at'],batch_id=None,document=dumps(original))
    if row is None:
        raise Conflict('找不到此環境的目標紀錄')
    result=dict(row)
    result.pop('error',None)  # historical exceptions may contain broker secrets
    if kind=='command':
        raw=result.pop('result',None)
        result['result_digest']=digest(raw) if raw else None
    return result


def _claim_evidence(conn, kind, row):
    if kind == 'curve':
        link = conn.execute('SELECT trace_id,command_id FROM latency_entry_links WHERE entry_id=?', (row['id'],)).fetchone()
        if row.get('order_id'):
            raise Conflict('已有關聯委託，請同步或核對該委託')
    else:
        link = {'trace_id': row['id'], 'command_id': row['id']}
    if not link:
        raise Conflict('缺少原指令關聯證據，不能僅憑查無委託解除')
    trace = link['trace_id']
    claims = [dict(r) for r in conn.execute('SELECT * FROM latency_order_links WHERE trace_id=? OR command_id=?',
                                          (trace, link['command_id']))]
    events = [dict(r) for r in conn.execute("SELECT event,order_id,occurred_at FROM latency_events WHERE (trace_id=? OR command_id=?) AND (event LIKE 'sdk_order%' OR event='order_result_persist_failed')", (trace, link['command_id']))]
    if claims or events:
        raise Conflict('存在送單 claim 或 SDK 送單證據，須先配對券商委託，不能標記無委託')
    return dict(trace_id=trace, command_id=link['command_id'], claims=claims, sdk_events=events)


def _batch(conn, code, mode):
    row = conn.execute('SELECT * FROM broker_observation_batches WHERE code=? AND mode=? ORDER BY rowid DESC LIMIT 1',
                       (code, mode)).fetchone()
    if row is None:
        raise Conflict('尚無券商核對資料，請先重新核對')
    result = dict(row)
    result['document'] = json.loads(result['document'])
    return result


def revision(evidence):
    stable = {**evidence, 'target': {k:v for k,v in evidence['target'].items() if k != 'updated_at'}}
    return digest(stable)


def _assert_session(service):
    if service._api is None or getattr(service, '_recovery', None) and service._recovery.pending:
        raise Conflict('原交易 session 尚未就緒，請等待恢復；不另登入')
    control = getattr(service, 'control', None)
    if control and control.stop_requested:
        raise Conflict('交易服務正在停止')
    if callable(getattr(service, 'window', None)) and service.window() is None:
        raise Conflict('目前不在交易 session 時段')
    service._validate_connected_mode()


def refresh(service):
    _assert_session(service)
    now = service._monotonic()
    interval = max(5, service.health_settings.order_sync_seconds)
    last = getattr(service, '_inspection_refresh_at', float('-inf'))
    if now-last < interval:
        raise Conflict(f'券商核對已排程，請等待 {interval:g} 秒後再查詢')
    service._inspection_refresh_at = now
    service._reconcile_required = True
    trades = service._sync_managed_orders(force=True)
    if not service._sync_ok:
        raise Conflict('券商委託同步未成功；保留上次資料')
    kwargs = {'timeout': service.health_settings.api_timeout_ms} if service.api_factory is None else {}
    positions = service._api.list_positions(service._api.futopt_account, **kwargs)
    record_observations(service, trades, positions)
    with db.read_session(service.db_path) as conn:
        batch = _batch(conn, service.code, mode_of(service))
    return dict(batch_id=batch['id'], coverage=batch['document']['coverage'],
                broker_ready=service._broker_ready, observed_at=stamp(service._now()))


def _evidence(service, payload):
    code, mode = service.code, mode_of(service)
    with db.read_session(service.db_path) as conn:
        row = _target(conn, code, mode, payload['kind'], payload['target_id'])
        if payload['action']=='cancel_condition':
            batch=dict(id=None,account_key=None,document={})
        else:
            batch = _batch(conn, code, mode)
        related = []
        if payload['kind'] == 'curve':
            related = [dict(r) for r in conn.execute('''SELECT o.* FROM latency_order_links o JOIN latency_entry_links e
                ON o.trace_id=e.trace_id OR o.command_id=e.command_id WHERE e.entry_id=?''', (row['id'],))]
        elif payload['kind'] == 'command':
            related = [dict(r) for r in conn.execute('SELECT * FROM latency_order_links WHERE command_id=?', (row['id'],))]
        identities = [dict(r) for r in conn.execute('SELECT * FROM broker_order_identities WHERE order_id=?',(row['id'],))] if payload['kind']=='order' else []
        return dict(target=row, batch_id=batch['id'], account_key=batch['account_key'],identities=identities,
                    broker=batch['document'], related=related)


def _validate_action(service, payload, evidence):
    kind, action, row = payload['kind'], payload['action'], evidence['target']
    broker = evidence['broker']
    with db.read_session(service.db_path) as conn:
        if conn.execute('SELECT 1 FROM trade_reconciliation_resolutions WHERE code=? AND mode=? AND kind=? AND target_id=?',
                        (service.code,mode_of(service),kind,payload['target_id'])).fetchone():
            raise Conflict('此事項已有核對結論，請查看處理紀錄')
    if action=='cancel_condition':
        if kind!='curve' or row['state']!='armed':
            raise Conflict('只有尚未送單的等待條件可取消')
        return
    if broker['rejected_orders'] or broker['truncated']:
        raise Conflict('券商快照有缺少身分或截斷資料，請補齊核對')
    if evidence['account_key'] != er.account_key(service._api.futopt_account):
        raise Conflict('券商帳戶已變更')
    if kind=='broker' and row['account_key']!=evidence['account_key']:
        raise Conflict('此歷史委託屬於不同帳戶，僅供查閱')
    if any(identity['account_key']!=evidence['account_key'] for identity in evidence.get('identities',[])):
        raise Conflict('管理單原帳戶與目前券商帳戶不同，不能套用修復')
    if action == 'confirm_no_order':
        if kind not in {'curve', 'command'} or row['state'] not in {'submitting', 'unknown'}:
            raise Conflict('此狀態不能人工標記無委託')
        with db.read_session(service.db_path) as conn:
            _claim_evidence(conn, kind, row)
        request = json.loads(row['request']) if kind == 'command' else None
        if request and request['name'] not in {'arm_curve', 'place_order', 'subscribe', 'cancel_curve'}:
            raise Conflict('此指令可能已操作券商，請以委託證據核對結果')
        expected = request['payload'] if request else row
        if not payload.get('attested') or len(payload.get('note', '').strip()) < 10:
            raise Conflict('請填寫至少 10 字的人工核對說明並確認券商核對結果')
        if request and request['name'] == 'arm_curve':
            with db.read_session(service.db_path) as conn:
                if conn.execute('''SELECT 1 FROM latency_entry_links l JOIN curve_entries e ON e.id=l.entry_id
                    WHERE l.command_id=? AND e.state IN ('armed','submitting')''', (row['id'],)).fetchone():
                    raise Conflict('此指令已有生效的曲線條件，請先處理該條件')
        code = expected.get('contract_code')
        canonical = service._quote_aliases.get(code, code)
        original_day = er.trading_day(row.get('armed_at') or row.get('created_at'),service._calendar)
        if original_day is None:
            raise Conflict('既有已驗證日曆無法確定原交易日，請先補齊日曆證據')
        if payload.get('checked_date') != original_day:
            raise Conflict(f'人工核對交易日必須與原紀錄的已驗證交易日 {original_day} 一致')
        with db.read_session(service.db_path) as conn:
            if canonical and conn.execute('''SELECT 1 FROM managed_orders WHERE code=? AND simulation=?
                AND contract_code=? AND entry_status IN ('unknown','submitting') LIMIT 1''',
                (service.code,int(service.simulation),canonical)).fetchone():
                raise Conflict('同商品仍有未明管理單 claim，請先核對該委託')
        # Absence is only supplementary evidence. The explicit human statement
        # is required, and any matching broker evidence defeats that statement.
        if any(r['contract_code'] == canonical and (not expected.get('action') or r['action'] == expected['action'])
               and (not r.get('entry_date') or r['entry_date'] == payload.get('checked_date')) for r in broker['orders']):
            raise Conflict('券商快照有同商品方向的委託，請核對身分，不能直接標記無委託')
        if not payload.get('checked_date'):
            raise Conflict('請填入人工核對的交易日')
    elif action == 'cancel_working':
        if kind not in {'order', 'broker'}:
            raise Conflict('此紀錄不是券商委託')
        broker_id = row['entry_trade_id'] if kind == 'order' else row['broker_trade_id']
        candidate = next((r for r in broker['orders'] if r['broker_trade_id'] == broker_id), None)
        if not candidate or candidate['working_quantity'] <= 0 or not candidate['seqno'] or not candidate['ordno']:
            raise Conflict('沒有可唯一辨識的未成交餘量，請重新核對')
        if kind == 'order' and (row.get('exit_trade_id') or row.get('exit_status') in UNCERTAIN):
            raise Conflict('此部位已有出場流程，請先同步與核對')
    elif action == 'link_curve':
        if kind != 'curve' or row['state'] != 'submitting':
            raise Conflict('此曲線條件不在待核對狀態')
        linked = payload.get('linked_order_id')
        if not linked or linked not in {r['order_id'] for r in evidence['related']}:
            raise Conflict('缺少原指令到管理單的唯一關聯證據')
        with db.read_session(service.db_path) as conn:
            target = _target(conn, service.code, mode_of(service), 'order', linked)
        if (target['entry_status'] in UNCERTAIN or target['contract_code'] != service._quote_aliases.get(row['contract_code'], row['contract_code'])
                or target['action'] != row['action'] or target['quantity'] != row['quantity']):
            raise Conflict('關聯委託的商品、方向、口數或結果尚未確認')
    elif action in {'link_entry','link_exit'}:
        if kind!='order' or not row['active']:
            raise Conflict('請選擇尚未解決的管理單')
        field = 'entry' if action=='link_entry' else 'exit'
        old_id = row.get(field+'_trade_id')
        if row.get(field+'_status') not in UNCERTAIN:
            raise Conflict('此階段的委託結果並非未知，不得改寫既有委託身分')
        candidate=next((r for r in broker['orders'] if r['broker_trade_id']==payload.get('broker_trade_id')),None)
        if (not candidate or not payload.get('attested') or len(payload.get('note','').strip())<10
                or not candidate['seqno'] or not candidate['ordno'] or not candidate['entry_date']
                or candidate['seqno']!=payload.get('seqno') or candidate['ordno']!=payload.get('ordno')
                or candidate['entry_date']!=payload.get('checked_date')):
            raise Conflict('請人工核對並填入完整的券商 ID、seqno、ordno、交易日與說明')
        if old_id and not old_id.startswith('unresolved:') and old_id!=candidate['broker_trade_id']:
            raise Conflict('已有不同的券商委託身分，不可覆寫')
        expected_side=row['action'] if field=='entry' else ('Sell' if row['action']=='Buy' else 'Buy')
        expected_quantity=int(row['quantity']) if field=='entry' else int(row['filled_quantity'])-int(row.get('external_exit_quantity',0))
        if (candidate['contract_code']!=row['contract_code'] or candidate['action']!=expected_side
                or candidate['quantity']!=expected_quantity or not candidate['fill_evidence_complete']
                or field=='exit' and candidate['octype']!='Cover'):
            raise Conflict('券商商品、方向、口數、平倉倉別或逐筆成交證據不符')
        with db.read_session(service.db_path) as conn:
            conflict=conn.execute('''SELECT 1 FROM managed_orders WHERE id!=? AND code=? AND simulation=?
                AND (entry_trade_id=? OR exit_trade_id=?) LIMIT 1''',
                (row['id'],service.code,int(service.simulation),candidate['broker_trade_id'],candidate['broker_trade_id'])).fetchone()
            identity=conn.execute('SELECT * FROM broker_order_identities WHERE order_id=?',(row['id'],)).fetchone()
        if conflict or field=='entry' and identity and identity['ordno'] and identity['ordno']!=candidate['ordno']:
            raise Conflict('此委託已歸屬其他批次或與原身分矛盾，請補證據')
    elif action == 'resolve_command':
        if kind != 'command' or row['state'] != 'unknown':
            raise Conflict('此指令沒有待核對結果')
        request = json.loads(row['request'])
        if request['name'] == 'cancel_entry':
            with db.read_session(service.db_path) as conn:
                target = _target(conn, service.code, mode_of(service), 'order', request['target'])
            if target['entry_status'] not in TERMINAL:
                raise Conflict('撤單結果尚未確認，請先同步券商終態')
        elif request['name'] == 'place_order':
            if len(evidence['related']) != 1:
                raise Conflict('未能唯一對應原進場管理單')
            with db.read_session(service.db_path) as conn:
                target = _target(conn, service.code, mode_of(service), 'order', evidence['related'][0]['order_id'])
            if target['entry_status'] in UNCERTAIN or target['entry_trade_id'].startswith('unresolved:'):
                raise Conflict('關聯委託的券商結果尚未確認')
        elif request['name']=='apply_reconciliation':
            with db.read_session(service.db_path) as conn:
                event=conn.execute('SELECT * FROM trade_reconciliation_events WHERE command_id=? AND code=? AND mode=?',
                                   (row['id'],service.code,mode_of(service))).fetchone()
            if not event:
                raise Conflict('修復執行證據不足')
            document=json.loads(event['document'])
            if event['state'] not in {'succeeded','confirmed'}:
                if event['action']=='cancel_working':
                    old=document['before']
                    bid=old.get('entry_trade_id') or old.get('broker_trade_id')
                    candidate=next((r for r in broker['orders'] if r['broker_trade_id']==bid),None)
                    if not candidate or candidate['broker_status'] not in TERMINAL:
                        raise Conflict('原撤單的券商終態尚未確認')
                else:
                    with db.read_session(service.db_path) as conn:
                        resolved=conn.execute('SELECT 1 FROM trade_reconciliation_resolutions WHERE event_id=?',(event['id'],)).fetchone()
                        target=_target(conn,service.code,mode_of(service),event['kind'],event['target_id'])
                    local_done=(event['action']=='cancel_condition' and target.get('state')=='cancelled')
                    if event['action'] in {'link_entry','link_exit'}:
                        field='entry' if event['action']=='link_entry' else 'exit'
                        local_done=(target.get(field+'_trade_id')==document['payload'].get('broker_trade_id')
                                    and target.get(field+'_status') in TERMINAL)
                    if not resolved and not local_done and event['action']!='sync':
                        raise Conflict('修復仍待確認，請補齊核對證據')
        elif request['name'] in {'refresh_reconciliation','preview_reconciliation','subscribe'}:
            pass  # these operations cannot submit or cancel a broker order
        else:
            raise Conflict('此類指令請使用人工無委託核對或查看修復證據')
    elif action == 'sync':
        if kind != 'order':
            raise Conflict('請選擇要核對的管理單')
    else:
        raise Conflict('不支援的處理方式')


def preview(service, payload):
    local_only=payload['action']=='cancel_condition'
    if not local_only:
        _assert_session(service)
        publish_pending(service)
    evidence = _evidence(service, payload)
    _validate_action(service, payload, evidence)
    now = datetime.fromisoformat(stamp(service._now()))
    # Preview only reads existing broker evidence, never issues a query.
    generation = getattr(service, '_generation', 0)
    if not local_only and (evidence['broker']['generation'] != generation or evidence['broker']['instance'] != getattr(getattr(service, 'latency', None), 'instance', 'offline')):
        raise Conflict('核對資料來自舊 session，請重新核對券商')
    last_success = getattr(service, '_inspection_verified_at', None)
    age=(now-datetime.fromisoformat(last_success)).total_seconds() if last_success else None
    if not local_only and (age is None or not -5<=age<=max(30,service.health_settings.order_sync_seconds*2)):
        raise Conflict('券商核對資料已過期，請重新核對')
    proposal = dict(id=uuid.uuid4().hex, payload=payload, evidence=evidence, revision=revision(evidence),
        created_at=now.isoformat(), expires_at=(now+timedelta(minutes=2)).isoformat(),
        broker_operation='撤銷券商未成交餘量' if payload['action']=='cancel_working' else '只核對／更新系統紀錄',
        effect={'cancel_condition':'停止等待，不再觸發此進場', 'confirm_no_order':'結束已人工核對的待確認事項',
                'cancel_working':'撤銷未成交餘量，已成交部位繼續管理', 'link_curve':'關聯原委託，不再送單',
                'resolve_command':'保存原執行結果，另記核對結論', 'sync':'依券商證據同步委託及部位',
                'link_entry':'以人工核對的券商身分同步原進場，不重送', 'link_exit':'以人工核對的券商身分同步原平倉，不重送'}[payload['action']],
        before=evidence['target'], instance=getattr(getattr(service, 'latency', None), 'instance', 'offline'), generation=generation)
    with write_connection(service.db_path) as conn:
        conn.execute('INSERT INTO trade_reconciliation_previews VALUES (?,?,?,?,?,?,NULL)',
            (proposal['id'], service.code, mode_of(service), proposal['created_at'], proposal['expires_at'], dumps(proposal)))
        conn.commit()
    return proposal


def _event(service, command_id, proposal, state, **extra):
    return dict(proposal_id=proposal['id'], before=proposal['before'], payload=proposal['payload'],
        evidence=proposal['evidence'], instance=proposal['instance'], generation=proposal['generation'],
        confirmation_source='user_broker_app' if proposal['payload'].get('attested') else 'broker_evidence', **extra)


def apply(service, payload, command_id):
    if payload.get('confirmed') is not True:
        raise Conflict('請確認處理預覽')
    mode, code = mode_of(service), service.code
    with db.read_session(service.db_path) as conn:
        saved = conn.execute('SELECT * FROM trade_reconciliation_previews WHERE id=? AND code=? AND mode=?',
                             (payload['proposal_id'], code, mode)).fetchone()
    if saved is None:
        raise Conflict('找不到此環境的預覽')
    if saved['applied_command_id']:
        raise Conflict('此預覽已執行，請查詢原指令結果')
    proposal = json.loads(saved['document'])
    local_only=proposal['payload']['action']=='cancel_condition'
    if not local_only:
        _assert_session(service)
    now_clock=datetime.fromisoformat(stamp(service._now()))
    if not datetime.fromisoformat(proposal['created_at'])-timedelta(seconds=5)<=now_clock<=datetime.fromisoformat(proposal['expires_at']):
        raise Conflict('預覽已過期，請重新核對')
    if (proposal['generation'] != getattr(service, '_generation', 0)
            or proposal['instance'] != getattr(getattr(service, 'latency', None), 'instance', 'offline')):
        raise Conflict('交易 session 已改變，請重新核對')
    # Always acquire fresh broker data before a change. Never hold a DB write
    # lock across the RPC. Comparing the evidence defeats fill/cancel races.
    kwargs = {'timeout': service.health_settings.api_timeout_ms} if service.api_factory is None else {}
    if not local_only:
        service._reconcile_required = True
        trades = service._sync_managed_orders(force=True)
        if not service._sync_ok:
            raise Conflict('券商核對失敗，保留原狀態')
        positions = service._api.list_positions(service._api.futopt_account, **kwargs)
        record_observations(service, trades, positions)
        service._inspection_verified_at = stamp(service._now())
    current = _evidence(service, proposal['payload'])
    if revision(current) != proposal['revision']:
        raise Conflict('預覽後委託、成交、部位或關聯已變更，請重新預覽')
    _validate_action(service, proposal['payload'], current)
    p, row = proposal['payload'], current['target']
    event_id, now = uuid.uuid4().hex, stamp(service._now())
    with write_connection(service.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        actual = _target(conn, code, mode, p['kind'], p['target_id'])
        if actual != row:
            raise Conflict('本機紀錄已變更')
        changed = conn.execute('UPDATE trade_reconciliation_previews SET applied_command_id=? WHERE id=? AND applied_command_id IS NULL',
                               (command_id, proposal['id'])).rowcount
        if changed != 1:
            raise Conflict('此預覽已被其他操作使用')
        conn.execute('INSERT INTO trade_reconciliation_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (event_id, command_id, code, mode, p['kind'], p['target_id'], p['action'], now, None, 'processing',
             dumps(_event(service, command_id, proposal, 'processing'))))
        if p['action'] == 'cancel_condition':
            conn.execute("UPDATE curve_entries SET state='cancelled',error=NULL WHERE id=? AND state='armed'", (row['id'],))
        elif p['action'] == 'confirm_no_order' and p['kind'] == 'curve':
            conn.execute("UPDATE curve_entries SET state='failed',error='人工核對券商無委託，舊條件已停止' WHERE id=? AND state='submitting'", (row['id'],))
        elif p['action'] == 'link_curve':
            conn.execute("UPDATE curve_entries SET state='triggered',order_id=?,error=NULL WHERE id=? AND state='submitting'",
                         (p['linked_order_id'], row['id']))
        elif p['action'] in {'link_entry','link_exit'}:
            field='entry' if p['action']=='link_entry' else 'exit'
            conn.execute(f'UPDATE managed_orders SET {field}_trade_id=? WHERE id=?',(p['broker_trade_id'],row['id']))
            candidate=next(r for r in current['broker']['orders'] if r['broker_trade_id']==p['broker_trade_id'])
            if field=='entry':
                conn.execute('INSERT OR REPLACE INTO broker_order_identities VALUES (?,?,?,?,?,?,?)',
                    (row['id'],candidate['broker_trade_id'],current['account_key'],candidate['seqno'],candidate['entry_date'],now,candidate['ordno']))
        if p['action'] in {'confirm_no_order', 'resolve_command', 'link_curve'}:
            conn.execute('INSERT INTO trade_reconciliation_resolutions VALUES (?,?,?,?,?,?)',
                (code, mode, p['kind'], p['target_id'], event_id, p['action']))
        conn.commit()
    result = dict(event_id=event_id, action=p['action'], state='succeeded', effect=proposal['effect'])
    try:
        if p['action'] == 'cancel_working':
            broker_id = row['entry_trade_id'] if p['kind']=='order' else row['broker_trade_id']
            trade = trades.get(broker_id)
            if trade is None:
                raise Conflict('原券商委託物件已不存在')
            # The session adapter exposes a narrowly scoped cancel that retains
            # cutoff/session checks while allowing the diagnosed global blocker.
            method = getattr(service._api, 'cancel_reconciled_order', service._api.cancel_order)
            method(trade)
            refreshed = service._sync_managed_orders(force=True)
            positions = service._api.list_positions(service._api.futopt_account, **kwargs)
            record_observations(service, refreshed, positions)
            final = refreshed.get(broker_id)
            result['state'] = 'confirmed' if final and text(value(final.status, 'status')) in TERMINAL else 'pending_confirmation'
        elif p['action']=='sync':
            result['broker_ready'] = service._broker_ready
        elif p['action'] in {'link_entry','link_exit'}:
            trade=trades[p['broker_trade_id']]
            previous_suppression=getattr(service,'_reconciliation_suppress_notifications',False)
            service._reconciliation_suppress_notifications=True
            try:
                if p['action']=='link_entry':service._persist_trade_state(row['id'],entry=trade)
                else:service._persist_trade_state(row['id'],exit_trade=trade)
            finally:
                service._reconciliation_suppress_notifications=previous_suppression
        if p['kind']=='order':
            with db.read_session(service.db_path) as conn:
                result['after'] = _target(conn, code, mode, 'order', p['target_id'])
        elif p['kind']=='curve':
            with db.read_session(service.db_path) as conn:
                result['after'] = _target(conn,code,mode,'curve',p['target_id'])
        with write_connection(service.db_path) as conn:
            conn.execute('UPDATE trade_reconciliation_events SET completed_at=?,state=?,document=? WHERE id=?',
                (stamp(service._now()), result['state'], dumps(_event(service, command_id, proposal, result['state'], result=result)), event_id))
            conn.commit()
    except Exception as error:
        with write_connection(service.db_path) as conn:
            conn.execute("UPDATE trade_reconciliation_events SET completed_at=?,state='unknown',document=? WHERE id=?",
                (stamp(service._now()), dumps(_event(service, command_id, proposal, 'unknown', error_type=type(error).__name__)), event_id))
            conn.commit()
        raise UncertainRepair('處理結果待核對，請查原指令；不重送券商操作') from None
    service._reconcile_required = True
    return result


def execute(service, name, payload, command_id):
    if name == 'refresh_reconciliation':
        result = refresh(service)
        service._inspection_verified_at = stamp(service._now())
        return result
    if name == 'preview_reconciliation':
        return preview(service, payload)
    if name == 'apply_reconciliation':
        return apply(service, payload, command_id)
    raise Conflict('不支援的核對指令')


def _records_sql(conn):
    """A paged SQL projection, independent of curve/tick calculations."""
    order_fields = ('id code contract_code action quantity filled_quantity exit_filled_quantity external_exit_quantity '
        'avg_fill_price avg_exit_price entry_trade_id exit_trade_id entry_status exit_status active auto_exit '
        'created_at updated_at order_origin broker_octype risk_revision stop_price target_price breakeven_enabled '
        'profit_lock_enabled').split()
    document = 'json_object(' + ','.join(f"'{k}',m.{k}" for k in order_fields) + ')'
    resolved = "NOT EXISTS(SELECT 1 FROM trade_reconciliation_resolutions r WHERE r.code=c.code AND r.mode=c.mode AND r.kind='command' AND r.target_id=c.id)"
    parts = [f'''SELECT 'order' kind,CAST(m.id AS TEXT) id,m.code,
        CASE WHEN m.simulation=1 THEN 'simulation' ELSE 'production' END mode,
        m.created_at sort_at,m.contract_code contract,m.action side,
        CASE WHEN m.order_origin IN ('external_order','external_position') THEN 'external' ELSE 'system' END source,
        CASE WHEN m.broker_octype='Cover' THEN 'exit' ELSE 'entry' END phase,
        m.entry_status broker_status,CAST(m.active=0 AS INTEGER) closed,
        CAST(m.active=1 AND (m.entry_status IN ('unknown','submitting') OR m.exit_status IN ('unknown','submitting','cancelling_entry','waiting_entry_cancel')) AS INTEGER) blocked,
        m.entry_status local_status,{document} document,
        m.id||' '||m.entry_trade_id||' '||COALESCE(m.exit_trade_id,'')||' '||COALESCE(b.seqno,'')||' '||COALESCE(b.ordno,'') search
        FROM managed_orders m LEFT JOIN broker_order_identities b ON b.order_id=m.id''',
        '''SELECT 'curve',CAST(e.id AS TEXT),e.code,CASE WHEN e.simulation=1 THEN 'simulation' ELSE 'production' END,
        e.armed_at,e.contract_code,e.action,'system','condition','',CAST(e.state NOT IN ('armed','submitting') AS INTEGER),
        CAST(e.state IN ('armed','submitting') AS INTEGER),e.state,
        json_object('id',e.id,'curve_id',e.curve_id,'contract_code',e.contract_code,'quantity',e.quantity,
            'action',e.action,'state',e.state,'order_id',e.order_id,'armed_at',e.armed_at,'last_checked_minute',e.last_checked_minute),
        CAST(e.id AS TEXT)||' '||CAST(e.curve_id AS TEXT)||' '||COALESCE(e.order_id,'') FROM curve_entries e''']
    if exists(conn, 'trade_commands'):
        parts.append(f'''SELECT 'command',c.id,c.code,c.mode,c.created_at,
            COALESCE(json_extract(c.request,'$.payload.contract_code'),''),COALESCE(json_extract(c.request,'$.payload.action'),''),
            'system','command','',CAST(c.state IN ('succeeded','failed') OR NOT ({resolved}) AS INTEGER),
            CAST(c.state='unknown' AND {resolved} AS INTEGER),c.state,
            json_object('id',c.id,'name',json_extract(c.request,'$.name'),'target',json_extract(c.request,'$.target'),
                'payload',json_extract(c.request,'$.payload'),'state',c.state,'created_at',c.created_at,'updated_at',c.updated_at),
            c.id||' '||COALESCE(json_extract(c.request,'$.target'),'') FROM trade_commands c''')
    parts.append('''SELECT 'broker',o.broker_trade_id,o.code,o.mode,
        COALESCE(json_extract(o.document,'$.ordered_at'),o.observed_at),
        json_extract(o.document,'$.contract_code'),json_extract(o.document,'$.action'),
        CASE WHEN x.id IS NOT NULL AND x.order_origin NOT IN ('external_order','external_position') THEN 'system' ELSE 'external' END,
        CASE WHEN x.id IS NOT NULL OR json_extract(o.document,'$.octype')='Cover' THEN 'exit' ELSE 'entry' END,
        json_extract(o.document,'$.broker_status'),
        CAST(json_extract(o.document,'$.broker_status') IN ('Filled','Cancelled','Failed','Rejected') AS INTEGER),
        0,'observed',json_set(o.document,'$.linked_order_id',x.id),o.broker_trade_id||' '||COALESCE(json_extract(o.document,'$.seqno'),'')||' '||COALESCE(json_extract(o.document,'$.ordno'),'')
        FROM broker_order_observations o LEFT JOIN managed_orders x ON x.code=o.code AND x.simulation=(o.mode!='production') AND x.exit_trade_id=o.broker_trade_id
        WHERE NOT EXISTS(SELECT 1 FROM managed_orders m WHERE m.code=o.code
        AND m.simulation=(o.mode!='production') AND m.entry_trade_id=o.broker_trade_id)''')
    parts.append('''SELECT 'broker',b.broker_trade_id,b.code,CASE WHEN b.simulation=1 THEN 'simulation' ELSE 'production' END,
        COALESCE(json_extract(b.document,'$.ordered_at'),b.updated_at),json_extract(b.document,'$.code'),json_extract(b.document,'$.action'),
        'external',CASE WHEN json_extract(b.document,'$.octype')='Cover' THEN 'exit' ELSE 'entry' END,
        json_extract(b.document,'$.status'),CAST(json_extract(b.document,'$.status') IN ('Filled','Cancelled','Failed','Rejected') AS INTEGER),
        0,'observed',json_set(b.document,'$.contract_code',json_extract(b.document,'$.code'),'$.broker_trade_id',b.broker_trade_id,'$.broker_status',json_extract(b.document,'$.status')),
        b.broker_trade_id||' '||COALESCE(json_extract(b.document,'$.seqno'),'')||' '||COALESCE(json_extract(b.document,'$.ordno'),'')
        FROM broker_external_orders b WHERE NOT EXISTS(SELECT 1 FROM broker_order_observations o
            WHERE o.code=b.code AND o.mode=CASE WHEN b.simulation=1 THEN 'simulation' ELSE 'production' END AND o.broker_trade_id=b.broker_trade_id)
        AND NOT EXISTS(SELECT 1 FROM managed_orders m WHERE m.code=b.code AND m.simulation=b.simulation AND m.entry_trade_id=b.broker_trade_id)''')
    return ' UNION ALL '.join(parts)


def list_records(path, code, mode, *, tab='pending', limit=50, cursor=None, since=None, until=None,
                 contract='', source='', side='', phase='', state='', closed='', search=''):
    if tab not in {'pending','all','positions','conditions','events'} or not 1 <= limit <= 200:
        raise ValueError('無效的清單或分頁大小')
    filters = dict(tab=tab,since=since,until=until,contract=contract,source=source,side=side,
                   phase=phase,state=state,closed=closed,search=search,code=code,mode=mode)
    fingerprint = digest(filters)
    with db.read_session(path) as conn:
        if tab=='events':
            where, params = 'code=? AND mode=?', [code,mode]
            for operator,val in [('>=',since),('<=',until)]:
                if val:
                    datetime.strptime(val,'%Y-%m-%d');where+=f' AND substr(started_at,1,10){operator}?';params.append(val)
            if search:
                where+=' AND (instr(lower(command_id),lower(?))>0 OR instr(lower(target_id),lower(?))>0)';params.extend([search,search])
            if contract:
                where+=" AND json_extract(document,'$.before.contract_code')=?";params.append(contract)
            if side:
                where+=" AND json_extract(document,'$.before.action')=?";params.append(side)
            if cursor:
                where += ' AND rowid<?'
                params.append(int(cursor))
            rows = conn.execute(f'SELECT rowid,* FROM trade_reconciliation_events WHERE {where} ORDER BY rowid DESC LIMIT ?',
                                (*params,limit+1)).fetchall()
            return dict(items=[{**dict(r),'document':json.loads(r['document'])} for r in rows[:limit]],
                        next_cursor=str(rows[limit-1]['rowid']) if len(rows)>limit else None)
        where, params = ['code=?','mode=?'], [code, 'simulation' if mode=='mock' else mode]
        # Mock uses simulation order records but its own command/observation scope.
        if mode=='mock':
            where[1]="(mode='mock' OR mode='simulation')"
            params.pop()
        if tab=='pending':
            where.append('blocked=1')
        elif tab=='positions':
            where.append("kind='order'")
        elif tab=='conditions':
            where.append("kind IN ('curve','command')")
        for column,val in [('contract',contract),('source',source),('side',side),('phase',phase),('closed',closed)]:
            if val != '':
                where.append(f'{column}=?');params.append(int(val) if column=='closed' else val)
        if state:
            where.append('(local_status=? OR broker_status=?)');params.extend([state,state])
        if search:
            where.append("instr(lower(search),lower(?))>0");params.append(search)
        if tab!='pending':
            for operator,val in [('>=',since),('<=',until)]:
                if val:
                    datetime.strptime(val,'%Y-%m-%d')
                    where.append(f'substr(sort_at,1,10){operator}?');params.append(val)
        scope = ' AND '.join(where)
        cte = 'WITH records AS ('+_records_sql(conn)+') '
        total = conn.execute(cte+'SELECT COUNT(*) FROM records WHERE '+scope,params).fetchone()[0]
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded['filter']!=fingerprint:
                    raise ValueError()
                scope += ' AND (sort_at,kind,id)<(?,?,?)'
                params.extend([decoded['at'],decoded['kind'],decoded['id']])
            except (ValueError,KeyError,TypeError,UnicodeError):
                raise ValueError('分頁游標已失效，請重新查詢') from None
        rows = conn.execute(cte+'SELECT * FROM records WHERE '+scope+' ORDER BY sort_at DESC,kind DESC,id DESC LIMIT ?',
                            (*params,limit+1)).fetchall()
        items = [{**dict(r),'document':json.loads(r['document'])} for r in rows[:limit]]
        next_cursor = None
        if len(rows)>limit:
            last=rows[limit-1]
            next_cursor=base64.urlsafe_b64encode(dumps(dict(filter=fingerprint,at=last['sort_at'],kind=last['kind'],id=last['id'])).encode()).decode()
        return dict(items=items,total=total,next_cursor=next_cursor)


def detail(path, code, mode, kind, target_id):
    with db.read_session(path) as conn:
        row = _target(conn, code, mode, kind, target_id)
        observations, timeline, linked = [], [], []
        actions = ['sync'] if kind=='order' else []
        if kind=='curve':
            actions = ['cancel_condition'] if row['state']=='armed' else ['confirm_no_order','link_curve'] if row['state']=='submitting' else []
            linked=[dict(r) for r in conn.execute('''SELECT o.* FROM latency_order_links o JOIN latency_entry_links e
                ON o.trace_id=e.trace_id OR o.command_id=e.command_id WHERE e.entry_id=?''',(row['id'],))]
        elif kind=='command':
            actions=['confirm_no_order','resolve_command'] if row['state']=='unknown' else []
            linked=[dict(r) for r in conn.execute('SELECT * FROM latency_order_links WHERE command_id=?',(row['id'],))]
        broker_ids = [row.get('entry_trade_id'),row.get('exit_trade_id')] if kind=='order' else [row.get('broker_trade_id')]
        for bid in filter(None,broker_ids):
            observations.extend({**dict(r),'document':json.loads(r['document'])} for r in conn.execute(
                'SELECT * FROM broker_order_observation_history WHERE code=? AND mode=? AND broker_trade_id=? ORDER BY observed_at DESC LIMIT 30',
                (code,mode,bid)))
        if kind=='order':
            if row.get('entry_status') in UNCERTAIN:actions.append('link_entry')
            if row.get('exit_status') in UNCERTAIN:actions.append('link_exit')
            timeline.extend({**dict(r),'document':json.loads(r['document'])} for r in conn.execute(
                'SELECT * FROM position_sync_events WHERE order_id=? ORDER BY reconciled_at DESC LIMIT 100',(row['id'],)))
        timeline.extend({**dict(r),'document':json.loads(r['document'])} for r in conn.execute(
            'SELECT * FROM trade_reconciliation_events WHERE code=? AND mode=? AND kind=? AND target_id=? ORDER BY started_at DESC LIMIT 100',
            (code,mode,kind,str(target_id))))
        trace_ids=[r['trace_id'] for r in linked]
        if kind=='curve':
            trace_ids.extend(r[0] for r in conn.execute('SELECT trace_id FROM latency_entry_links WHERE entry_id=?',(row['id'],)))
        elif kind=='command':trace_ids.append(str(target_id))
        elif kind=='order':
            trace_ids.extend(r[0] for r in conn.execute('SELECT trace_id FROM latency_order_links WHERE order_id=?',(row['id'],)))
        for trace in set(trace_ids):
            timeline.extend({**dict(r),'document':json.loads(r['document'])} for r in conn.execute(
                'SELECT event,occurred_at,document FROM latency_events WHERE code=? AND mode=? AND trace_id=? ORDER BY occurred_at DESC LIMIT 100',
                (code,mode,trace)))
        if kind=='order':
            for table,column in [('external_close_reconciliations','order_id'),('order_reconciliations','source_order_id')]:
                timeline.extend({**dict(r),'document':json.loads(r['document'])} for r in conn.execute(f'SELECT * FROM {table} WHERE {column}=?',(row['id'],)))
        resolution=conn.execute('SELECT * FROM trade_reconciliation_resolutions WHERE code=? AND mode=? AND kind=? AND target_id=?',
                               (code,mode,kind,str(target_id))).fetchone()
        if kind in {'order','broker'}:
            bid=row.get('entry_trade_id') if kind=='order' else row.get('broker_trade_id')
            observed=conn.execute('SELECT document FROM broker_order_observations WHERE code=? AND mode=? AND broker_trade_id=?',
                                 (code,mode,bid)).fetchone()
            if observed and json.loads(observed[0])['working_quantity']>0:
                actions.append('cancel_working')
        # Explicit local errors are shown as classifications; never return an
        # arbitrary old exception string or opaque broker response to the UI.
        row.pop('error',None)
        if kind=='command':
            row.pop('result',None)
            row['request']=json.loads(row['request'])
        if kind=='broker':
            row['document']=json.loads(row['document'])
        return dict(kind=kind,id=str(target_id),local=row,observations=observations,timeline=timeline,
                    linked=linked,actions=[] if resolution else actions,resolution=dict(resolution) if resolution else None)


def overview(path, code, mode, status=None):
    status=status or {}
    records=list_records(path,code,mode,tab='pending',limit=200)
    blockers=[]
    for item in records['items']:
        is_wait=item['kind']=='curve' and item['local_status']=='armed'
        read_only=item['kind']=='command' and item['document'].get('name') in {'refresh_reconciliation','preview_reconciliation'}
        blockers.append(dict(id=item['kind']+':'+item['id'],kind=item['kind'],target_id=item['id'],
            reason='查詢／預覽結果待確認，不會單獨停用送單' if read_only else '已有等待中的進場條件' if is_wait else '原執行結果待核對',
            scope=[] if read_only else ['curve_entry'] if item['kind']=='curve' else ['manual_entry','curve_entry'] if item['kind']=='command' else ['manual_entry','curve_entry','protection'],
            state=item['local_status']))
    issues=[]
    def issue(key,reason,scope):issues.append(dict(id='service:'+key,reason=reason,scope=scope))
    if status.get('connected') is False:issue('snapshot',status.get('error') or '交易服務未就緒或快照已過期',['manual_entry','curve_entry','protection'])
    if status.get('scheduled_open') is False:issue('schedule','目前在交易排程之外',['manual_entry','curve_entry','protection'])
    if status.get('broker_ready') is False:issue('broker',status.get('reconciliation_error') or '券商委託與部位核對尚未完成',['manual_entry','curve_entry','protection'])
    if status.get('bars_ready') is False:issue('history','收定 K 棒尚未就緒；曲線進場等待歷史資料',['curve_entry'])
    for contract,error in (status.get('manual_order_errors') or {}).items():
        if error:issue('market:'+contract,f'{contract}：{error}',['manual_entry'])
    with db.read_session(path) as conn:
        count=conn.execute("""SELECT COUNT(*) FROM trade_commands c WHERE c.code=? AND c.mode=? AND c.state='unknown'
            AND NOT EXISTS(SELECT 1 FROM trade_reconciliation_resolutions r WHERE r.code=c.code AND r.mode=c.mode AND r.kind='command' AND r.target_id=c.id)""",
                           (code,mode)).fetchone()[0] if exists(conn,'trade_commands') else 0
        batch=None
        try: batch=_batch(conn,code,mode)
        except Conflict: pass
    from pathlib import Path
    return dict(mode=mode,code=code,scope=digest([str(Path(path).resolve()),code,mode]),
        last_verified_at=status.get('last_inspection_at'),
        blockers=blockers,blocker_count=records['total'],service_issues=issues,
        pending_unknown_commands=count,status={k:status.get(k) for k in ('connected','broker_ready','bars_ready','scheduled_open',
            'manual_order_ready','manual_order_errors','protection','reconciliation_error','error','inspection_error')},
        observation=None if batch is None else {k:batch[k] for k in ('id','observed_at','instance','generation')},
        account='未核對' if batch is None else '帳戶 …'+batch['account_key'][-8:],
        coverage='尚未取得券商核對資料' if batch is None else batch['document']['coverage'])
