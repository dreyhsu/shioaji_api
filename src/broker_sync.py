"""Import App orders and reconcile quantities from stable broker snapshots.

Only the existing trade worker calls this module. It never submits, cancels or
replays an order. Broker fills and prior local states remain in the audit ledger.
"""
import hashlib
import json
import math
import uuid

import pandas as pd

import db
import external_reconciliation as er
from pnl_calendar import value, normalized_fills
from position_reconciliation import ReconciliationPending, TERMINAL, text


def audit(conn, row_id, kind, stamp, **evidence):
    conn.execute('INSERT INTO position_sync_events VALUES (?,?,?,?)',
        (str(uuid.uuid4()), row_id, json.dumps(dict(kind=kind, **evidence), ensure_ascii=False), stamp))


def describe(service, trade):
    """Validate an immutable broker identity; never infer a missing order ID."""
    order, status, contract = trade.order, trade.status, trade.contract
    account = value(order, 'account')
    if account is None or er.account_key(account) != er.account_key(service._api.futopt_account):
        raise ReconciliationPending('App 委託帳戶身分尚未確認，等待券商回報')
    identifiers = {key: str(value(order, key, '')).strip() for key in ('id', 'seqno', 'ordno')}
    if any(not item or set(item) == {'0'} for item in identifiers.values()):
        raise ReconciliationPending('App 委託書號尚未完整，等待券商回報')
    code, action = str(contract.code), text(value(order, 'action'))
    allowed = service.position_sync_codes
    if allowed is not None and code not in {service._quote_aliases.get(item, item) for item in allowed}:
        raise ReconciliationPending('App 委託商品尚未配置行情，請加入服務合約設定')
    if code not in {str(service._find_contract(item['code']).code) for item in service.contracts()} or action not in {'Buy', 'Sell'}:
        raise ReconciliationPending('App 委託商品或方向無法辨識')
    stamp = pd.Timestamp(value(status, 'order_datetime'))
    if pd.isna(stamp):
        raise ReconciliationPending('App 委託時間尚未完整，等待券商回報')
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert('Asia/Taipei').tz_localize(None)
    day = er.trading_day(stamp, service._calendar)
    if day is None or stamp > pd.Timestamp(service._now()) + pd.Timedelta(seconds=5):
        raise ReconciliationPending('App 委託交易日尚未確認，等待券商回報')
    quantity = value(status,'order_quantity') or value(order,'quantity')
    price = float(value(status,'modified_price') or value(order,'price',0))
    filled = value(status, 'deal_quantity', 0)
    if (isinstance(quantity, bool) or int(quantity) != quantity or quantity <= 0
            or isinstance(filled, bool) or int(filled) != filled or not 0 <= filled <= quantity
            or not math.isfinite(price) or price < 0):
        raise ReconciliationPending('App 委託口數或價格無效，等待券商確認')
    octype = text(value(order, 'octype'))
    if octype not in {'New', 'NewPosition', 'Auto', 'Cover', 'DayTrade'}:
        raise ReconciliationPending('App 委託倉別尚未確認，等待券商回報')
    record = dict(**identifiers, account_key=er.account_key(account), code=code, action=action,
        entry_date=day, ordered_at=str(stamp), quantity=int(quantity), price=price,
        price_type=text(value(order, 'price_type')), order_type=text(value(order, 'order_type')),
        octype=octype, status=text(value(status, 'status')), filled_quantity=int(filled),
        cancel_quantity=int(value(status, 'cancel_quantity', 0)))
    try:
        fills=normalized_fills(trade)
    except (ValueError,TypeError,OverflowError,OSError):
        fills=[]
    if fills and sum(item[3] for item in fills)>filled:
        raise ReconciliationPending('App 成交回報正在更新，稍後自動同步')
    if sum(item[3] for item in fills)!=filled:
        fills=[]
    average=(sum(item[3]*item[4] for item in fills)/filled if fills and filled else
             value(status,'average_price') or value(status,'deal_price'))
    if average is not None:
        average=float(average)
        if not math.isfinite(average) or average<=0:
            average=None
    record['average_fill_price']=average
    record['fills']=fills
    record['fill_digest']=hashlib.sha256(json.dumps(fills).encode()).hexdigest() if fills else None
    identity = [record[key] for key in ('account_key', 'code', 'action', 'entry_date', 'seqno', 'ordno')]
    record['key'] = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return record


def save_execution(conn, row_id, record, expected, phase, identity):
    fills=record['fills']
    if not fills or sum(item[3] for item in fills)!=expected:
        return False
    conn.execute('DELETE FROM pnl_fills WHERE order_id=? AND phase=?',(row_id,phase))
    conn.executemany('INSERT INTO pnl_fills VALUES (?,?,?,?,?,?,?,?)',
        [(row_id,phase,key,identity,at,day,qty,price) for key,at,day,qty,price in fills])
    return True


def synchronize(service, trades, positions, actual, kwargs):
    from trading import align_tick, stop_target_prices
    stamp = str(pd.Timestamp(service._now()))
    account = er.account_key(service._api.futopt_account)
    with db.read_session(service.db_path) as conn:
        imports = {row['broker_trade_id']: dict(row) for row in conn.execute(
            'SELECT * FROM broker_external_orders WHERE code=? AND simulation=?',
            (service.code, int(service.simulation)))}
        known = {str(row[key]) for row in conn.execute(
            'SELECT entry_trade_id,exit_trade_id FROM managed_orders WHERE code=? AND simulation=?',
            (service.code, int(service.simulation))) for key in ('entry_trade_id', 'exit_trade_id') if row[key]}
        waiting_atr=conn.execute("SELECT 1 FROM managed_orders WHERE code=? AND simulation=? AND active=1 "
            "AND order_origin IN ('external_order','external_position') AND atr=0 AND levels_customized=0 LIMIT 1",
            (service.code,int(service.simulation))).fetchone() is not None
        version=conn.execute('SELECT revision FROM market_data_versions WHERE code=?',(service.code,)).fetchone()
    imports_by_key = {item['identity_key']:item for item in imports.values()}
    candidates = {}
    for identity, trade in trades.items():
        terminal = text(value(trade.status,'status')) in TERMINAL
        if identity in known and identity not in imports:
            continue
        try:
            record=describe(service,trade)
        except ReconciliationPending:
            if terminal and identity not in imports:
                continue
            raise
        except (ValueError,TypeError,OverflowError):
            if terminal and identity not in imports:
                continue
            raise ReconciliationPending('App 委託欄位尚未完整，等待券商回報後自動重試') from None
        if not terminal or identity in imports or record['key'] in imports_by_key:
            candidates[identity]=record
    # Position-detail IDs belong to their immediately preceding positions query.
    lots = service._read_broker_lots(positions, actual, kwargs) if callable(
        getattr(service._api, 'list_position_detail', None)) else None
    fresh = service._api.list_positions(service._api.futopt_account, **kwargs)
    import external_positions as ep
    if ep.position_totals(fresh, service._quote_aliases) != actual:
        raise ReconciliationPending('App 操作期間券商部位正在變動，稍後自動同步')
    if lots is not None and service._read_broker_lots(fresh, actual, kwargs) != lots:
        raise ReconciliationPending('App 操作期間券商進場批次正在變動，稍後自動同步')
    # Reject order changes during the accounting query too, including cancellation.
    cache = {str(value(item.order, 'id')): item for item in service._api.list_trades()}
    if any(key not in cache or describe(service, cache[key]) != record for key, record in candidates.items()):
        raise ReconciliationPending('App 委託正在變動，稍後自動同步')
    atr=0.
    if waiting_atr or any(record['key'] not in imports_by_key and record['octype']!='Cover' for record in candidates.values()):
        context=service._risk_context
        token=(version[0] if version else 0,pd.Timestamp(service._now()).floor('min'),
               id(getattr(context,'__func__',context)))
        cached=getattr(service,'_app_default_atr_cache',None)
        if cached is not None and cached[0]==token:
            atr=cached[1]
        else:
            try:
                atr,_=context(service.code)
            except RuntimeError:
                atr=0.
            service._app_default_atr_cache=(token,atr)
    lot_map = {(code, action, lot['entry_date'], lot['ordno']): lot
        for (code, action), items in (lots or {}).items() for lot in items}
    service._broker_lots_snapshot, service._broker_entry_lots = lots, lot_map if lots is not None else None
    legacy_cover=any(row.get('broker_octype')=='Cover' and row.get('order_origin')!='external_order'
                     for row in service._active_orders())
    # Prefer complete realized execution evidence when already available. Open
    # lots can still correct remaining quantity while that report is pending.
    if not legacy_cover:
        service._reconcile_external_closes(service._active_orders(), actual, kwargs)
    verified, working = set(), []
    with db.session(service.db_path) as conn:
        if conn.execute("SELECT 1 FROM trade_commands WHERE code=? AND mode=? AND state='unknown' LIMIT 1",
                        (service.code, 'simulation' if service.simulation else 'production')).fetchone():
            raise ReconciliationPending('先前指令結果未確認，保留原指令且不自動重送')
        for identity, record in candidates.items():
            previous = imports.get(identity) or imports_by_key.get(record['key'])
            if previous and previous['identity_key'] != record['key']:
                raise ReconciliationPending('券商委託身分已變更，等待重新核對')
            row_id = previous['order_id'] if previous else None
            if previous and previous['broker_trade_id'] != identity and row_id:
                conn.execute('UPDATE managed_orders SET entry_trade_id=? WHERE id=? AND entry_trade_id=?',
                    (identity,row_id,previous['broker_trade_id']))
                conn.execute('UPDATE broker_order_identities SET broker_trade_id=?,updated_at=? WHERE order_id=?',
                    (identity,stamp,row_id))
                conn.execute("UPDATE pnl_fills SET broker_trade_id=? WHERE order_id=? AND phase='entry' AND broker_trade_id=?",
                    (identity,row_id,previous['broker_trade_id']))
            if previous is None:
                row_id = str(uuid.uuid4())
                contract = service._find_contract(record['code'])
                tick = float(value(contract, 'tick', 1))
                if not math.isfinite(tick) or tick <= 0:
                    raise ReconciliationPending('App 委託合約最小跳動單位無效')
                enabled = int(record['octype'] != 'Cover')
                conn.execute('''INSERT INTO managed_orders
                    (id,code,contract_code,action,quantity,price,price_type,order_type,atr,
                     auto_exit,breakeven_enabled,profit_lock_enabled,simulation,entry_trade_id,
                     entry_status,broker_octype,order_origin,contract_tick,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'external_order',?,?,?)''',
                    (row_id,service.code,record['code'],record['action'],record['quantity'],record['price'],
                     record['price_type'],record['order_type'],atr,enabled,enabled,enabled,int(service.simulation),
                     identity,record['status'],record['octype'],tick,record['ordered_at'],stamp))
                conn.execute('INSERT INTO broker_order_identities VALUES (?,?,?,?,?,?,?)',
                    (row_id,identity,account,record['seqno'],record['entry_date'],stamp,record['ordno']))
                audit(conn, row_id, 'app_order_adopted', stamp, broker_order=record)
            document = json.dumps(record, sort_keys=True, ensure_ascii=False)
            if previous is None or previous['document'] != document:
                conn.execute('''INSERT INTO broker_external_orders VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(identity_key) DO UPDATE SET broker_trade_id=excluded.broker_trade_id,
                    document=excluded.document,updated_at=excluded.updated_at''',
                    (record['key'],service.code,int(service.simulation),identity,row_id,document,
                     previous['adopted_at'] if previous else stamp,stamp))
                if previous:
                    audit(conn, row_id or record['key'], 'app_order_updated', stamp,
                          before=json.loads(previous['document']), after=record)
            verified.add(identity)
            if record['status'] not in TERMINAL:
                working.append(record)
            if row_id is None:
                continue
            row = dict(conn.execute('SELECT * FROM managed_orders WHERE id=?', (row_id,)).fetchone())
            updates = dict(price=record['price'],price_type=record['price_type'],order_type=record['order_type'],
                           quantity=record['quantity'],entry_status=record['status'])
            lot = lot_map.get((record['code'],record['action'],record['entry_date'],record['ordno']))
            if record['octype']=='Cover':
                average=record['average_fill_price']
                updates.update(broker_octype='Cover',active=int(record['status'] not in TERMINAL),
                    filled_quantity=0,avg_fill_price=average,auto_exit=0,breakeven_enabled=0,profit_lock_enabled=0)
                if previous is None or json.loads(previous['document']).get('fill_digest')!=record['fill_digest']:
                    save_execution(conn,row_id,record,record['filled_quantity'],'cover',identity)
            elif lot is not None:
                represented = sum(item['entry_quantity'] for item in lot['details'])
                count = (max(row['filled_quantity'],record['filled_quantity']) if record['octype'] in {'New','NewPosition','DayTrade'}
                         else max(row['filled_quantity'], represented))
                if count > record['filled_quantity'] and count > row['filled_quantity']:
                    raise ReconciliationPending('App 成交回報與部位明細尚未同步，稍後自動核對')
                average = record['average_fill_price']
                if count == row['filled_quantity'] and row['avg_fill_price'] is not None:
                    average = row['avg_fill_price']
                elif count != record['filled_quantity'] or average is None:
                    if represented != count:
                        raise ReconciliationPending('App 原始進場價格明細尚未完整，稍後自動同步')
                    average = sum(item['entry_quantity']*item['price'] for item in lot['details'])/count
                updates.update(filled_quantity=count,avg_fill_price=average,broker_octype='New',active=1)
                if not row['filled_quantity']:
                    updates['profit_lock_started_at'] = stamp
                if not row['levels_customized'] and (row['atr'] > 0 or atr > 0):
                    risk_atr = row['atr'] or atr
                    stop,target = stop_target_prices(record['action'],average,risk_atr)
                    updates.update(atr=risk_atr,stop_price=align_tick(stop,row['contract_tick']),
                                   target_price=align_tick(target,row['contract_tick']),auto_exit=1)
                # A mixed Auto order may contain both covering and new fills.
                # Position detail defines only the newly opened batch's entry book.
                saved = conn.execute("SELECT SUM(quantity) FROM pnl_fills WHERE order_id=? AND phase='entry'", (row_id,)).fetchone()[0]
                execution_changed = previous is None or json.loads(previous['document']).get('fill_digest')!=record['fill_digest']
                execution_saved = (save_execution(conn,row_id,record,count,'entry',identity)
                    if execution_changed else False)
                if not execution_saved and saved != count and represented == count:
                    conn.execute("DELETE FROM pnl_fills WHERE order_id=? AND phase='entry'", (row_id,))
                    conn.executemany('INSERT INTO pnl_fills VALUES (?,?,?,?,?,?,?,?)',
                        [(row_id,'entry',f'app-position:{i}',identity,item['date'],item['date'],
                          item['entry_quantity'],item['price']) for i,item in enumerate(lot['details'])])
                conn.execute('INSERT OR IGNORE INTO pnl_order_metadata (order_id,multiplier,currency,updated_at) VALUES (?,?,?,?)',
                    (row_id,value(service._find_contract(record['code']),'multiplier'),'TWD',stamp))
            elif record['filled_quantity'] and lots is not None and record['octype'] in {'Cover','Auto'} and not row['filled_quantity']:
                updates.update(active=0,auto_exit=0,broker_octype='Cover')
            elif record['filled_quantity'] and record['octype'] in {'New','NewPosition','DayTrade'}:
                average = record['average_fill_price']
                if average is not None:
                    updates.update(filled_quantity=record['filled_quantity'],avg_fill_price=average)
            elif record['status'] in TERMINAL and not record['filled_quantity']:
                updates.update(active=0,auto_exit=0)
            changed = {key:item for key,item in updates.items() if row.get(key) != item}
            if changed:
                audit(conn,row_id,'app_order_state_synchronized',stamp,before=row,changes=changed)
                conn.execute('UPDATE managed_orders SET '+','.join(f'{key}=?' for key in changed)
                    +',risk_revision=risk_revision+1,updated_at=? WHERE id=?', (*changed.values(),stamp,row_id))
        if lots is not None and not legacy_cover:
            synchronize_quantities(service, conn, lot_map, account, stamp)
        # Upgrade only automatic imports that have never had an individual risk
        # edit. Explicitly saved per-order choices always remain authoritative.
        for saved in conn.execute("SELECT * FROM managed_orders WHERE code=? AND simulation=? AND active=1 "
            "AND order_origin='external_position' AND auto_exit=0 AND breakeven_enabled=0 "
            "AND profit_lock_enabled=0 AND risk_revision=0",(service.code,int(service.simulation))).fetchall():
            automatic=any(json.loads(item['document']).get('kind')=='automatic_adoption' for item in conn.execute(
                'SELECT document FROM position_sync_events WHERE order_id=?',(saved['id'],)))
            if automatic:
                conn.execute('UPDATE managed_orders SET auto_exit=1,breakeven_enabled=1,profit_lock_enabled=1,'
                    'levels_customized=0,profit_lock_started_at=?,risk_revision=risk_revision+1,updated_at=? WHERE id=?',
                    (stamp,stamp,saved['id']))
                audit(conn,saved['id'],'automatic_default_protection_enabled',stamp,before=dict(saved))
        if atr > 0:
            for saved in conn.execute("SELECT * FROM managed_orders WHERE code=? AND simulation=? AND active=1 "
                "AND order_origin IN ('external_order','external_position') AND atr=0 AND levels_customized=0",
                (service.code,int(service.simulation))).fetchall():
                if saved['avg_fill_price'] is None:
                    continue
                stop,target=stop_target_prices(saved['action'],saved['avg_fill_price'],atr)
                stop,target=align_tick(stop,saved['contract_tick']),align_tick(target,saved['contract_tick'])
                if saved['stop_price'] is not None:
                    stop=max(stop,saved['stop_price']) if saved['action']=='Buy' else min(stop,saved['stop_price'])
                conn.execute('UPDATE managed_orders SET atr=?,stop_price=?,target_price=?,risk_revision=risk_revision+1,updated_at=? WHERE id=?',
                    (atr,stop,target,stamp,saved['id']))
                audit(conn,saved['id'],'app_default_atr_ready',stamp,atr=atr,stop_price=stop,target_price=target)
        conn.commit()
    service._broker_lots_snapshot,service._broker_entry_lots = lots,lot_map if lots is not None else None
    service._broker_authority_snapshot_token = service._last_position_sync_at
    service._external_working_orders = working
    return verified


def synchronize_quantities(service, conn, lot_map, account, stamp):
    """Keep original entry accounting; broker open lots define remaining volume."""
    rows = [dict(row) for row in conn.execute('''SELECT o.*,b.entry_date,b.ordno,b.account_key
        FROM managed_orders o JOIN broker_order_identities b ON b.order_id=o.id
        WHERE o.code=? AND o.simulation=? AND b.account_key=?''',
        (service.code,int(service.simulation),account))]
    grouped = {}
    for row in rows:
        grouped.setdefault((row['contract_code'],row['action'],row['entry_date'],row['ordno']),[]).append(row)
    for key, items in grouped.items():
        if len(items) != 1:
            if key in lot_map:
                raise ReconciliationPending('券商進場批次對應多筆本機紀錄，等待唯一身分核對')
            continue
        row = items[0]
        if row.get('broker_octype') == 'Cover' or not row['filled_quantity']:
            continue
        lot = lot_map.get(key)
        remaining = lot['quantity'] if lot else 0
        if remaining > row['filled_quantity']:
            raise ReconciliationPending('券商剩餘口數超過已確認進場口數，等待成交回報')
        previous = row['filled_quantity'] - row['exit_filled_quantity']
        if remaining == previous and bool(row['active']) == bool(remaining or row['entry_status'] not in TERMINAL):
            continue
        if row.get('exit_status') not in TERMINAL | {None}:
            raise ReconciliationPending('本機平倉委託仍在處理，等待券商完成後自動同步')
        total_exit = row['filled_quantity'] - remaining
        sdk_exit = row['exit_filled_quantity'] - row['external_exit_quantity']
        changes = dict(exit_filled_quantity=total_exit,external_exit_quantity=max(0,total_exit-sdk_exit),
                       active=int(bool(remaining or row['entry_status'] not in TERMINAL)))
        if total_exit != row['exit_filled_quantity']:
            changes['avg_exit_price'] = None
        if total_exit < row['exit_filled_quantity']:
            conn.execute("INSERT OR REPLACE INTO pnl_fills SELECT order_id,'broker_unallocated_exit',fill_key,"
                "broker_trade_id,filled_at,fill_date,quantity,price FROM pnl_fills WHERE order_id=? AND phase='exit'", (row['id'],))
            conn.execute("DELETE FROM pnl_fills WHERE order_id=? AND phase='exit'", (row['id'],))
        if total_exit < sdk_exit:
            # A confirmed close can have been allocated to another broker lot.
            # Preserve the execution evidence, but release this lot's *completed*
            # claim so only its actual remaining contracts can be protected.
            changes.update(exit_trade_id=None,exit_status=None,exit_status_code=None,avg_exit_price=None,
                           external_exit_quantity=total_exit,triggered_at=None,trigger_reason=None)
            if remaining:
                changes.update(auto_exit=1,breakeven_enabled=1,profit_lock_enabled=1,
                               profit_lock_started_at=stamp)
        if remaining == 0:
            changes.update(auto_exit=0,breakeven_enabled=0,profit_lock_enabled=0,curve_armed=0,
                           trigger_reason=row['trigger_reason'] or 'broker_position_sync')
        elif not row['active']:
            changes.update(auto_exit=1,breakeven_enabled=1,profit_lock_enabled=1,
                           profit_lock_started_at=stamp)
        if remaining and (not row['active'] or total_exit < sdk_exit):
            service._risk_settings_saved(conn, row['id'], stamp)
            conn.execute('DELETE FROM managed_profit_lock_ticks WHERE order_id=?', (row['id'],))
        audit(conn,row['id'],'broker_position_authority',stamp,before=row,changes=changes,
              broker_lot=lot,broker_remaining_quantity=remaining,
              exit_execution_known=False if total_exit != row['exit_filled_quantity'] else True)
        conn.execute('UPDATE managed_orders SET '+','.join(f'{key}=?' for key in changes)
            +',risk_revision=risk_revision+1,updated_at=? WHERE id=?', (*changes.values(),stamp,row['id']))
        if total_exit != row['exit_filled_quantity']:
            conn.execute('UPDATE pnl_order_metadata SET exit_error=?,updated_at=? WHERE order_id=?',
                ('broker_position_sync_pending_realized_detail' if total_exit else None,stamp,row['id']))


def pending_close(service, row):
    opposite = 'Sell' if row['action'] == 'Buy' else 'Buy'
    return any(item['code']==row['contract_code'] and item['action']==opposite
        and item['octype'] in {'Cover','Auto'} and item['status'] not in TERMINAL
        for item in getattr(service,'_external_working_orders',[]))
