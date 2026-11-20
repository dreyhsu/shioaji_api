"""App orders and authoritative broker positions, with no extra broker session."""
import json
from types import SimpleNamespace as NS

import pytest

import db
from test_independent_trade import rig

pytestmark = pytest.mark.integration


def app(r, *, action='Buy', octype='New', quantity=3, identity='app-1'):
    r.runtime.step()
    order = NS(id=identity,seqno=identity+'-seq',ordno=identity+'-ord',account=r.broker.futopt_account,
        action=action,octype=octype,quantity=quantity,price=19000 if action=='Buy' else 21000,
        price_type='LMT',order_type='ROD')
    trade = NS(contract=r.broker.contract,order=order,status=NS(id=identity,status='Submitted',
        deal_quantity=0,cancel_quantity=0,deals=[],order_datetime=r.now[0]))
    r.broker.trades.append(trade)
    r.lots = []
    r.broker.list_positions = lambda *a,**k: ([] if not r.lots else [NS(id=0,code=r.broker.contract.code,
        direction=r.lots[0].direction,quantity=sum(item.quantity for item in r.lots))])
    r.broker.list_position_detail = lambda *a,**k: r.lots
    return trade


def sync(r):
    r.service._sync_managed_orders(force=True)
    assert r.service._broker_ready, r.service._reconcile_error


def fill(r, trade, quantity, *, remaining=None, status='PartFilled'):
    trade.status.status = status
    trade.status.deal_quantity = quantity
    r.lots[:] = [NS(code=r.broker.contract.code,direction=trade.order.action,date='20261030',
        quantity=quantity if remaining is None else remaining,entry_quantity=quantity,
        price=20000,currency='TWD',dseq=trade.order.ordno)] if (quantity if remaining is None else remaining) else []


@pytest.mark.parametrize('action', ['Buy','Sell'])
def test_app_resting_order_is_adopted_then_partial_fills_and_cancel_stay_synchronized(rig, action):
    r = rig
    trade = app(r,action=action)
    sync(r)
    order = r.service._active_orders()[0]
    assert order['order_origin']=='external_order' and order['entry_trade_id']==trade.order.id
    assert order['filled_quantity']==0 and order['entry_status']=='Submitted'
    assert order['auto_exit']==order['breakeven_enabled']==order['profit_lock_enabled']==1
    for _ in range(2): sync(r)
    assert len(r.service.orders())==1 and len(r.broker.trades)==1
    fill(r,trade,1)
    sync(r)
    row = r.service._active_order(order['id'])
    assert row['filled_quantity']==1 and row['avg_fill_price']==20000
    assert row['stop_price']==(19940 if action=='Buy' else 20060)
    assert row['target_price']==(20240 if action=='Buy' else 19760)
    # App edits apply without a Dashboard confirmation operation.
    trade.status.modified_price=trade.order.price+10
    sync(r)
    assert r.service._active_order(order['id'])['price']==trade.status.modified_price
    trade.status.status='Cancelled'
    trade.status.cancel_quantity=2
    sync(r)
    row=r.service._active_order(order['id'])
    assert row['entry_status']=='Cancelled' and row['active'] and row['filled_quantity']==1
    # The App closes that last contract before realized P&L details arrive.
    r.lots.clear()
    sync(r)
    row=r.service._active_order(order['id'])
    assert row['external_exit_quantity']==row['exit_filled_quantity']==1 and not row['active']
    assert row['filled_quantity']==1 and row['avg_exit_price'] is None
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM broker_external_orders').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM position_sync_events').fetchone()[0]>=3
        assert conn.execute("SELECT COUNT(*) FROM pnl_fills WHERE phase='exit'").fetchone()[0]==0
    assert len(r.apis)==1 and len(r.broker.trades)==1


def test_app_order_cancel_without_fills_never_creates_position_or_replays(rig):
    r=rig
    trade=app(r)
    sync(r)
    trade.status.status='Cancelled';trade.status.cancel_quantity=3
    sync(r)
    row=r.service.orders()[0]
    assert not row['active'] and row['filled_quantity']==0
    for _ in range(2): sync(r)
    assert len(r.broker.trades)==1 and len(r.service.orders())==1


def test_app_cover_is_tracked_without_phantom_entry_or_duplicate_sdk_exit(rig):
    r=rig
    entry=app(r,quantity=2)
    fill(r,entry,2,status='Filled')
    sync(r)
    row=r.service._active_orders()[0]
    cover=NS(contract=r.broker.contract,order=NS(**vars(entry.order)),status=NS(**vars(entry.status)))
    cover.order.id='app-cover';cover.order.seqno='cover-seq';cover.order.ordno='cover-ord'
    cover.order.action='Sell';cover.order.octype='Cover';cover.order.price=21000
    cover.status.status='Submitted';cover.status.deal_quantity=0
    r.broker.trades.append(cover)
    sync(r)
    assert len(r.service.orders())==2
    r.service._try_auto_exit(row,'stop')
    assert len(r.broker.trades)==2 and r.service._active_order(row['id'])['exit_trade_id'] is None
    cover.status.status='Filled';cover.status.deal_quantity=2;r.lots.clear()
    sync(r)
    assert not r.service._active_order(row['id'])['active']
    assert len(r.service.orders())==2 and len(r.broker.trades)==2
    covered=next(item for item in r.service.orders() if item['broker_octype']=='Cover')
    assert covered['filled_quantity']==0 and covered['broker_order_filled_quantity']==2


def test_auto_cover_and_new_fill_only_books_the_actual_new_broker_lot(rig):
    r=rig
    trade=app(r,octype='Auto')
    sync(r)
    fill(r,trade,3,status='Filled')
    r.lots[0].quantity=r.lots[0].entry_quantity=1
    sync(r)
    row=r.service._active_orders()[0]
    assert row['filled_quantity']==1 and row['quantity']==3
    with db.read_session(r.path) as conn:
        document=json.loads(conn.execute('SELECT document FROM broker_external_orders').fetchone()[0])
        assert document['filled_quantity']==3
        assert conn.execute("SELECT SUM(quantity) FROM pnl_fills WHERE phase='entry'").fetchone()[0]==1


def test_closed_local_lot_is_corrected_from_broker_without_reimporting_entry(rig):
    r=rig
    trade=app(r,quantity=1)
    fill(r,trade,1,status='Filled')
    sync(r)
    row=r.service._active_orders()[0]
    with db.session(r.path) as conn:
        conn.execute("UPDATE managed_orders SET active=0,exit_filled_quantity=1,exit_trade_id='old-cover',"
            "exit_status='Filled',auto_exit=0 WHERE id=?",(row['id'],))
        conn.commit()
    sync(r)
    corrected=r.service._active_order(row['id'])
    assert corrected['active'] and corrected['exit_filled_quantity']==0
    assert corrected['exit_trade_id'] is None and corrected['auto_exit']
    assert len(r.service.orders())==1 and len(r.broker.trades)==1
    with db.read_session(r.path) as conn:
        assert conn.execute("SELECT SUM(quantity) FROM pnl_fills WHERE phase='entry'").fetchone()[0]==1


def test_incomplete_or_wrong_account_app_order_is_not_guessed(rig):
    r=rig
    trade=app(r)
    trade.order.account=NS(account_id='OTHER')
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and not r.service.orders()
    assert '帳戶' in r.service._reconcile_error and len(r.broker.trades)==1


def test_order_changing_during_broker_snapshot_is_retried_without_partial_adoption(rig):
    r=rig
    trade=app(r)
    # Positions are empty, so use the second positions call as the change boundary.
    positions=r.broker.list_positions
    count=[0]
    def moved(*a,**k):
        count[0]+=1
        if count[0]==2: trade.order.price+=1
        return positions(*a,**k)
    r.broker.list_positions=moved
    r.service._sync_managed_orders(force=True)
    assert not r.service._broker_ready and not r.service.orders()
    assert '委託正在變動' in r.service._reconcile_error


def test_partial_app_close_preserves_original_entry_cost_and_user_risk(rig):
    r=rig
    trade=app(r)
    fill(r,trade,3,status='Filled')
    sync(r)
    row=r.service._active_orders()[0]
    saved=r.service.update_risk_settings(row['id'],curve_id=None,stop_price=19000,target_price=22000,
        breakeven_enabled=True,breakeven_trigger_points=200,breakeven_offset_points=10,
        profit_lock_enabled=True,risk_revision=row['risk_revision'])
    r.lots[0].quantity=2
    sync(r)
    row=r.service._active_order(row['id'])
    assert row['filled_quantity']==3 and row['external_exit_quantity']==1
    assert row['avg_fill_price']==20000 and row['active']
    for key in ('stop_price','target_price','breakeven_trigger_points','breakeven_offset_points'):
        assert row[key]==saved[key]
    r.lots[0].quantity=1
    sync(r)
    row=r.service._active_order(row['id'])
    assert row['filled_quantity']==3 and row['external_exit_quantity']==2
    assert row['stop_price']==19000 and len(r.broker.trades)==1


def test_restart_keeps_the_same_app_order_without_new_submission(rig):
    from trade import TradeRuntime
    r=rig
    app(r)
    sync(r)
    original=r.service.orders()[0]['id']
    r.runtime.close()
    service=r.create()
    service.bar_codes[r.broker.contract.code]='TMF209912'
    runtime=TradeRuntime(service)
    try:
        runtime.step()
        assert service._broker_ready,service._reconcile_error
        assert service.orders()[0]['id']==original and len(service.orders())==1
        assert len(r.broker.trades)==1 and len(r.apis)==2
    finally:
        runtime.close()


def test_new_sdk_cache_id_for_the_same_broker_order_rebinds_without_duplication(rig):
    r=rig
    trade=app(r)
    sync(r)
    original=r.service.orders()[0]['id']
    trade.order.id='new-sdk-cache-id'
    sync(r)
    assert len(r.service.orders())==1 and r.service.orders()[0]['id']==original
    assert r.service.orders()[0]['entry_trade_id']=='new-sdk-cache-id'
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM broker_external_orders').fetchone()[0]==1


def test_missing_atr_never_fabricates_fixed_target_and_fills_defaults_when_ready(rig):
    import pandas as pd
    r=rig
    trade=app(r,quantity=1)
    def missing(code): raise RuntimeError('no hourly history')
    r.service._risk_context=missing
    sync(r)
    fill(r,trade,1,status='Filled')
    sync(r)
    row=r.service._active_orders()[0]
    assert row['auto_exit']==row['breakeven_enabled']==row['profit_lock_enabled']==1
    assert row['atr']==0 and row['target_price'] is None
    assert r.service.snapshot()['status']['pending_default_atr']==1
    r.service._risk_context=lambda code:(120.,pd.DataFrame())
    sync(r)
    row=r.service._active_order(row['id'])
    assert row['atr']==120 and row['target_price']==20240 and row['stop_price']==19940
    assert r.service.snapshot()['status']['pending_default_atr']==0


def test_realized_report_arriving_later_enriches_the_already_synchronized_close(rig):
    from test_external_reconciliation import install
    r=rig
    trade=app(r,quantity=2)
    fill(r,trade,2,status='Filled')
    sync(r)
    order=r.service._active_orders()[0]
    r.lots.clear()
    sync(r)
    closed=r.service._active_order(order['id'])
    assert not closed['active'] and closed['avg_exit_price'] is None
    reports=[NS(code=r.broker.contract.code,direction='Buy',date='20261030',entry_date='20261030',
        entry_seqno=trade.order.ordno,entry_quantity=2,quantity=2,entry_price=20000,cover_price=19900,
        dseq='app-realized-close',currency='TWD',pnl=-2000,fee=30,tax=10)]
    calls=install(r,reports)
    r.mono[0]+=61
    sync(r)
    closed=r.service._active_order(order['id'])
    assert closed['external_exit_quantity']==2 and closed['avg_exit_price']==19900 and not closed['active']
    with db.read_session(r.path) as conn:
        assert conn.execute('SELECT exit_error FROM pnl_order_metadata WHERE order_id=?',(order['id'],)).fetchone()[0] is None
        assert conn.execute("SELECT SUM(quantity) FROM pnl_fills WHERE order_id=? AND phase='exit'",(order['id'],)).fetchone()[0]==2
    r.mono[0]+=61
    sync(r)
    assert len(calls)==2 and len(r.broker.trades)==1


def test_completed_app_order_returns_to_normal_query_interval(rig):
    r=rig
    trade=app(r,quantity=1)
    sync(r)
    fill(r,trade,1,status='Filled')
    sync(r)
    r.service._persist_trade_state(r.service._active_orders()[0]['id'],entry=trade)
    r.service._report_repair_needed=False
    calls=[]
    r.broker.update_status=lambda *a,**k:calls.append(1)
    r.mono[0]+=3
    r.service._trades_by_id()
    assert calls==[] and not r.service._external_working_orders


def test_existing_app_order_sync_does_not_reload_historical_strategy_bars(rig):
    r=rig
    app(r)
    sync(r)
    def forbidden(code): raise AssertionError('unchanged App order must not rescan history')
    r.service._risk_context=forbidden
    sync(r)
    assert len(r.service.orders())==1 and len(r.broker.trades)==1


def test_prior_automatic_import_arms_defaults_only_if_risk_was_never_edited(rig):
    from test_external_positions import external
    r=rig
    external(r,automatic=True)
    row=r.service._active_orders()[0]
    with db.session(r.path) as conn:
        conn.execute('UPDATE managed_orders SET auto_exit=0,breakeven_enabled=0,profit_lock_enabled=0,'
            'levels_customized=1,risk_revision=0 WHERE id=?',(row['id'],))
        conn.commit()
    sync(r)
    armed=r.service._active_order(row['id'])
    assert armed['auto_exit']==armed['breakeven_enabled']==armed['profit_lock_enabled']==1
    with db.session(r.path) as conn:
        conn.execute('UPDATE managed_orders SET auto_exit=0,breakeven_enabled=0,profit_lock_enabled=0,'
            'levels_customized=1,risk_revision=risk_revision+1 WHERE id=?',(row['id'],))
        conn.commit()
    sync(r)
    unchanged=r.service._active_order(row['id'])
    assert unchanged['auto_exit']==unchanged['breakeven_enabled']==unchanged['profit_lock_enabled']==0


def test_existing_sdk_cover_repair_precedes_position_authority_with_real_lot_details(rig):
    from test_position_reconciliation import enter
    import pandas as pd
    r=rig
    original=r.broker.place_order
    def identified(*a,**k):
        trade=original(*a,**k)
        trade.order.seqno=trade.order.id+'-seq';trade.order.ordno=trade.order.id+'-ord'
        return trade
    r.broker.place_order=identified
    positions=r.broker.list_positions
    def indexed(*a,**k):
        result=positions(*a,**k)
        for item in result: item.id=0
        return result
    r.broker.list_positions=indexed
    r.broker.list_position_detail=lambda *a,**k: [NS(code=r.broker.contract.code,direction='Buy',
        date='20261030',quantity=2,entry_quantity=2,price=20000,currency='TWD',
        dseq=r.broker.trades[0].order.ordno)] if r.broker.position else []
    opened=enter(r,'sdk-open')
    r.runtime.step()
    assert r.service._broker_ready,(r.service._reconcile_error,r.service._reconcile_diagnostic)
    r.now[0]+=pd.Timedelta(seconds=1)
    from test_independent_trade import request
    request(r,'sdk-cover',payload=dict(contract_code='TMF209912',action='Sell',quantity=2,
        price=0,price_type='MKT',order_type='IOC',simulation=True,confirmed=True))
    r.runtime.step()
    result=r.client.get('/api/commands/sdk-cover').json()
    assert result['state']=='succeeded',result.get('result')
    covered=result['result']['id']
    r.runtime.step()
    assert r.service._broker_ready,r.service._reconcile_error
    assert not r.service._active_order(opened)['active']
    assert not r.service._active_order(covered)['active']
    assert r.service._active_order(opened)['exit_trade_id']==r.service._active_order(covered)['entry_trade_id']
