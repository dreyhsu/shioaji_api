"""Order desk with real durable commands/SQLite and an offline broker only."""
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

import db
import dashboard
import order_reconciliation as rec
from test_independent_trade import rig, arm_curve, request
from trade_client import TradeClient

pytestmark = pytest.mark.integration


def drive(r, name, payload=None, identity=None):
    identity=identity or name+'-'+str(len(list(r.path.parent.glob('unused*'))))
    response=r.client.post('/api/commands',json={'id':identity,'name':name,'payload':payload or {}})
    assert response.status_code==202,response.text
    r.runtime.step()
    return r.client.get('/api/commands/'+identity).json()


def legacy_pending(r):
    arm_curve(r)
    entry=r.service.curve_entry.snapshot()
    with rec.write_connection(r.path) as conn:
        conn.execute("UPDATE curve_entries SET state='submitting' WHERE id=?",(entry['id'],))
        conn.commit()
    assert drive(r,'refresh_reconciliation')['state']=='succeeded'
    return r.service.curve_entry.snapshot()


def no_order(entry):
    return dict(kind='curve',target_id=str(entry['id']),action='confirm_no_order',
                note='已在券商 App 核對當日委託成交及部位，確認沒有此筆委託。',
                checked_date='2026-10-30',attested=True)


def test_reads_are_paginated_and_never_initialize_or_login(rig,monkeypatch):
    r=rig
    with db.session(r.path) as conn:
        for i in range(63):
            conn.execute("INSERT INTO curve_entries(code,simulation,curve_id,contract_code,quantity,action,state,armed_at,check_from) VALUES ('TMFR1',1,99,'TMF_OLD',1,'Sell','cancelled',?,?)",
                         (f'2026-09-{i%28+1:02} 09:{i%60:02}:00','2026-09-01'))
        conn.commit()
    def forbidden(*a,**k):raise AssertionError('read must not initialize schema')
    monkeypatch.setattr(db,'init_schema',forbidden)
    first=rec.list_records(r.path,'TMFR1','simulation',tab='all',limit=17)
    assert first['total']==63 and len(first['items'])==17
    seen={r['id'] for r in first['items']}
    while first['next_cursor']:
        first=rec.list_records(r.path,'TMFR1','simulation',tab='all',limit=17,cursor=first['next_cursor'])
        assert not seen.intersection(r['id'] for r in first['items'])
        seen.update(r['id'] for r in first['items'])
    assert len(seen)==63
    assert rec.list_records(r.path,'TMFR1','production',tab='all')['total']==0
    assert rec.overview(r.path,'TMFR1','simulation')['blocker_count']==0
    assert not r.apis


def test_manual_legacy_curve_resolution_preserves_evidence_and_never_submits(rig):
    r=rig;entry=legacy_pending(r)
    preview=drive(r,'preview_reconciliation',no_order(entry))
    assert preview['state']=='succeeded',preview
    proposal=preview['result']
    result=drive(r,'apply_reconciliation',dict(proposal_id=proposal['id'],confirmed=True))
    assert result['state']=='succeeded',result
    assert r.service.curve_entry.snapshot()['state']=='failed'
    assert not r.broker.trades and len(r.apis)==1
    with db.read_session(r.path) as conn:
        event=conn.execute('SELECT * FROM trade_reconciliation_events').fetchone()
        evidence=json.loads(event['document'])
        assert evidence['before']['state']=='submitting'
        assert evidence['payload']['attested'] is True
        assert evidence['confirmation_source']=='user_broker_app'
        assert conn.execute('SELECT COUNT(*) FROM curve_entries').fetchone()[0]==1
    again=r.client.post('/api/commands',json=dict(id=result['id'],name='apply_reconciliation',payload=dict(proposal_id=proposal['id'],confirmed=True)))
    assert again.json()['state']=='succeeded'
    assert drive(r,'apply_reconciliation',dict(proposal_id=proposal['id'],confirmed=True),identity='apply-again')['state']=='failed'
    assert not r.broker.trades


def test_actual_claim_cannot_be_cleared_by_attestation(rig):
    r=rig;entry=legacy_pending(r)
    with rec.write_connection(r.path) as conn:
        link=conn.execute('SELECT * FROM latency_entry_links WHERE entry_id=?',(entry['id'],)).fetchone()
        conn.execute('INSERT INTO latency_order_links VALUES (?,?,?,NULL)',('durable-claim',link['trace_id'],link['command_id']))
        conn.commit()
    result=drive(r,'preview_reconciliation',no_order(entry))
    assert result['state']=='failed' and 'claim' in result['result']['detail']
    assert r.service.curve_entry.snapshot()['state']=='submitting'


def test_manual_attestation_is_required_and_preview_is_versioned(rig):
    r=rig;entry=legacy_pending(r)
    body=no_order(entry);body['attested']=False
    assert drive(r,'preview_reconciliation',body,identity='without-confirm')['state']=='failed'
    preview=drive(r,'preview_reconciliation',no_order(entry))['result']
    with rec.write_connection(r.path) as conn:
        conn.execute('UPDATE curve_entries SET quantity=2 WHERE id=?',(entry['id'],));conn.commit()
    result=drive(r,'apply_reconciliation',dict(proposal_id=preview['id'],confirmed=True))
    assert result['state']=='failed' and '變更' in result['result']['detail']
    assert not r.broker.trades


def test_unknown_command_resolution_does_not_rewrite_or_reexecute_original(rig):
    r=rig;r.runtime.step()
    with rec.write_connection(r.path) as conn:
        at=rec.stamp(r.now[0]);document=json.dumps(dict(name='place_order',target=None,payload=dict(contract_code='TMF209912',action='Buy',quantity=1)))
        conn.execute('INSERT INTO trade_commands VALUES (?,?,?,?,?,?,?,?,?)',('original','TMFR1','simulation',document,'unknown',at,at,'{"detail":"unknown"}',409));conn.commit()
    assert drive(r,'refresh_reconciliation')['state']=='succeeded'
    payload=dict(kind='command',target_id='original',action='confirm_no_order',attested=True,
                 checked_date='2026-10-30',note='已核對券商完整委託及成交帳，此筆指令未建立委託。')
    preview=drive(r,'preview_reconciliation',payload)
    assert preview['state']=='succeeded',preview
    result=drive(r,'apply_reconciliation',dict(proposal_id=preview['result']['id'],confirmed=True))
    assert result['state']=='succeeded',result
    original=r.client.get('/api/commands/original').json()
    assert original['state']=='unknown' and original['resolution']['conclusion']=='confirm_no_order'
    with db.read_session(r.path) as conn:assert rec.pending_command(conn,'TMFR1','simulation') is None
    assert not r.broker.trades


def test_read_routes_have_no_broker_side_effects(rig,monkeypatch):
    r=rig
    monkeypatch.setitem(dashboard.CONFIG,'db_path',r.path)
    monkeypatch.setitem(dashboard.CONFIG,'code','TMFR1')
    monkeypatch.setitem(dashboard.CONFIG,'mode','simulation')
    client=TradeClient(r.path,'TMFR1')
    monkeypatch.setattr(client,'status',lambda:r.client.get('/api/snapshot').json()['status'])
    monkeypatch.setattr(dashboard,'_trading_service',lambda:client)
    http=TestClient(dashboard.app)
    assert http.get('/orders').status_code==200
    assert http.get('/api/trading/reconciliation/overview').status_code==200
    assert http.get('/api/trading/reconciliation/records?tab=all').json()['total']==0
    assert http.get('/api/trading/reconciliation/records?limit=999').status_code==422
    assert http.get('/api/trading/reconciliation/detail/curve/999').status_code==404
    assert http.post('/api/trading/reconciliation/command',json=dict(name='refresh_reconciliation')).status_code==400
    assert not r.apis


def test_pending_entries_ignore_date_filter_and_bad_cursor_fails(rig):
    r=rig;entry=legacy_pending(r)
    result=rec.list_records(r.path,'TMFR1','simulation',tab='pending',since='2099-01-01')
    assert any(row['id']==str(entry['id']) and row['kind']=='curve' for row in result['items'])
    with pytest.raises(ValueError):rec.list_records(r.path,'TMFR1','simulation',tab='all',cursor='corrupt')


def test_repair_rechecks_session_and_expiration(rig):
    r=rig;entry=legacy_pending(r)
    proposal=drive(r,'preview_reconciliation',no_order(entry))['result']
    r.service._generation+=1
    result=drive(r,'apply_reconciliation',dict(proposal_id=proposal['id'],confirmed=True))
    assert result['state']=='failed' and 'session' in result['result']['detail']
    assert r.service.curve_entry.snapshot()['state']=='submitting'


def test_partial_fill_cancel_preserves_filled_position(rig,monkeypatch):
    r=rig
    payload=dict(contract_code='TMF209912',action='Buy',quantity=2,price=19999,price_type='LMT',order_type='ROD',simulation=True,confirmed=True)
    response=request(r,payload=payload);assert response.status_code==202;r.runtime.step()
    row=r.client.get('/api/commands/order-1').json()['result'];trade=r.broker.trades[0]
    trade.status.status='PartFilled';trade.status.deal_quantity=1
    trade.status.deals=[NS(seq='partial',quantity=1,price=19999,datetime=r.now[0])]
    r.broker.position=1
    calls=[]
    def cancel(item):
        calls.append(item.order.id);item.status.status='Cancelled';item.status.cancel_quantity=1;return item
    monkeypatch.setattr(r.broker,'cancel_order',cancel)
    assert drive(r,'refresh_reconciliation')['state']=='succeeded'
    preview=drive(r,'preview_reconciliation',dict(kind='order',target_id=row['id'],action='cancel_working'))
    assert preview['state']=='succeeded',preview['result']
    result=drive(r,'apply_reconciliation',dict(proposal_id=preview['result']['id'],confirmed=True))
    assert result['state']=='succeeded',result
    assert calls==[trade.order.id] and len(r.broker.trades)==1
    saved=r.service._active_order(row['id'])
    assert saved['filled_quantity']==1 and saved['active']==1 and saved['auto_exit']==1
    assert rec.list_records(r.path,'TMFR1','simulation',tab='all',source='system')['total']>=1


def test_cancellation_response_loss_retains_claim_and_can_be_reconciled(rig,monkeypatch):
    r=rig
    payload=dict(contract_code='TMF209912',action='Buy',quantity=1,price=19999,price_type='LMT',order_type='ROD',simulation=True,confirmed=True)
    request(r,payload=payload);r.runtime.step()
    order=r.client.get('/api/commands/order-1').json()['result']
    drive(r,'refresh_reconciliation')
    proposal=drive(r,'preview_reconciliation',dict(kind='order',target_id=order['id'],action='cancel_working'))['result']
    calls=[]
    def lost(trade):
        calls.append(trade.order.id);trade.status.status='Cancelled';trade.status.cancel_quantity=1
        raise TimeoutError('test only: response lost')
    monkeypatch.setattr(r.broker,'cancel_order',lost)
    applied=drive(r,'apply_reconciliation',dict(proposal_id=proposal['id'],confirmed=True))
    assert applied['state']=='unknown' and len(calls)==1
    r.runtime.step()
    replay=r.client.post('/api/commands',json=dict(id=applied['id'],name='apply_reconciliation',payload=dict(proposal_id=proposal['id'],confirmed=True)))
    assert replay.json()['state']=='unknown' and len(calls)==1
    checked=drive(r,'preview_reconciliation',dict(kind='command',target_id=applied['id'],action='resolve_command'),identity='review-cancel-result')
    assert checked['state']=='succeeded',checked['result']
    settled=drive(r,'apply_reconciliation',dict(proposal_id=checked['result']['id'],confirmed=True),identity='resolve-cancel-result')
    assert settled['state']=='succeeded',settled['result']
    original=r.client.get('/api/commands/'+applied['id']).json()
    assert original['state']=='unknown' and original['resolution']
    assert len(calls)==1


def test_broker_records_include_old_external_history_and_system_exit(rig):
    r=rig;r.runtime.step()
    with db.session(r.path) as conn:
        doc=dict(code='EXPIRED',action='Sell',status='Cancelled',quantity=2,filled_quantity=0,seqno='old-seq',ordno='old-ord',account_key='old-account')
        conn.execute('INSERT INTO broker_external_orders VALUES (?,?,?,?,?,?,?,?)',
                     ('old','TMFR1',1,'old-broker',None,json.dumps(doc),'2026-01-01','2026-01-01'))
        conn.commit()
    found=rec.list_records(r.path,'TMFR1','simulation',tab='all',contract='EXPIRED',closed='1')
    assert len(found['items'])==1 and found['items'][0]['id']=='old-broker'
    details=rec.detail(r.path,'TMFR1','simulation','broker','old-broker')
    assert details['local']['document']['contract_code']=='EXPIRED'
    assert details['actions']==[]


def test_wrong_trade_day_and_unconfirmed_broker_identity_do_not_resolve(rig):
    r=rig;entry=legacy_pending(r)
    payload=no_order(entry);payload['checked_date']='2026-10-29'
    result=drive(r,'preview_reconciliation',payload)
    assert result['state']=='failed' and '交易日' in result['result']['detail']


def test_preview_expiry_is_checked_before_broker_io(rig,monkeypatch):
    r=rig;entry=legacy_pending(r)
    proposal=drive(r,'preview_reconciliation',no_order(entry))['result']
    r.now[0]+=__import__('pandas').Timedelta(minutes=3)
    def forbidden(*a,**k):raise AssertionError('expired preview must not query broker')
    monkeypatch.setattr(r.service,'_sync_managed_orders',forbidden)
    with pytest.raises(rec.Conflict,match='過期'):
        rec.apply(r.service,dict(proposal_id=proposal['id'],confirmed=True),'expired')


def test_repair_audit_never_copies_arbitrary_old_exception_text(rig):
    r=rig;entry=legacy_pending(r)
    with rec.write_connection(r.path) as conn:
        conn.execute('UPDATE curve_entries SET error=? WHERE id=?',('secret-error-value-must-not-leak',entry['id']));conn.commit()
    proposal=drive(r,'preview_reconciliation',no_order(entry))['result']
    assert 'secret-error-value' not in json.dumps(proposal)
    result=drive(r,'apply_reconciliation',dict(proposal_id=proposal['id'],confirmed=True))
    assert result['state']=='succeeded',result['result']
    details=rec.detail(r.path,'TMFR1','simulation','curve',str(entry['id']))
    assert 'secret-error-value' not in json.dumps(details)


@pytest.mark.parametrize('phase',['entry','exit'])
def test_manual_broker_identity_link_never_resubmits_or_replays_notifications(rig,monkeypatch,phase):
    r=rig
    payload=dict(contract_code='TMF209912',action='Buy',quantity=1,price=19999 if phase=='entry' else 0,
                 price_type='LMT' if phase=='entry' else 'MKT',order_type='ROD' if phase=='entry' else 'IOC',simulation=True,confirmed=True)
    request(r,payload=payload);r.runtime.step()
    order=r.client.get('/api/commands/order-1').json()['result']
    if phase=='entry':trade=r.broker.trades[0]
    else:
        trade=r.broker.place_order(r.broker.contract,r.broker.sdk.Order(action='Sell',quantity=1,price=0,
            price_type='MKT',order_type='IOC',octype='Cover',account=r.broker.futopt_account))
    with rec.write_connection(r.path) as conn:
        if phase=='entry':conn.execute("UPDATE managed_orders SET entry_status='unknown',entry_trade_id=? WHERE id=?",('unresolved:'+order['id'],order['id']))
        else:conn.execute("UPDATE managed_orders SET exit_status='unknown',exit_trade_id=NULL WHERE id=?",(order['id'],))
        conn.commit()
    drive(r,'refresh_reconciliation')
    body=dict(kind='order',target_id=order['id'],action='link_'+phase,broker_trade_id=trade.order.id,
        seqno=trade.order.seqno,ordno=trade.order.ordno,checked_date='2026-10-30',attested=True,
        note='已從券商逐筆成交及完整書號人工核對，確認為原管理單的此階段委託。')
    proposal=drive(r,'preview_reconciliation',body)
    assert proposal['state']=='succeeded',proposal['result']
    def forbidden(*a,**k):raise AssertionError('must not replay notifications during historical reconciliation')
    monkeypatch.setattr(r.service.entry_notifier,'notify',forbidden)
    monkeypatch.setattr(r.service.entry_notifier,'notify_exit',forbidden)
    result=drive(r,'apply_reconciliation',dict(proposal_id=proposal['result']['id'],confirmed=True))
    assert result['state']=='succeeded',result['result']
    after=r.service._active_order(order['id'])
    assert after[phase+'_trade_id']==trade.order.id and after[phase+'_status'] not in rec.UNCERTAIN
    assert len(r.broker.trades)==(1 if phase=='entry' else 2)
    if phase=='exit':
        assert after['active']==0 and after['exit_filled_quantity']==1 and after['avg_exit_price']==20000
        exits=rec.list_records(r.path,'TMFR1','simulation',tab='all',source='system',phase='exit')
        assert len(exits['items'])==1 and exits['items'][0]['id']==trade.order.id


def test_waiting_condition_can_be_cancelled_outside_session_without_login(rig):
    r=rig;arm_curve(r)
    entry=r.service.curve_entry.snapshot()
    r.now[0]=__import__('pandas').Timestamp('2026-10-30 14:00:00')
    r.runtime.step()
    assert r.service._api is None
    count=len(r.apis)
    result=drive(r,'preview_reconciliation',dict(kind='curve',target_id=str(entry['id']),action='cancel_condition'))
    assert result['state']=='succeeded',result['result']
    applied=drive(r,'apply_reconciliation',dict(proposal_id=result['result']['id'],confirmed=True))
    assert applied['state']=='succeeded',applied['result']
    assert r.service.curve_entry.snapshot()['state']=='cancelled' and len(r.apis)==count


def test_inspection_timeout_is_not_an_uncertain_broker_mutation(rig,monkeypatch):
    r=rig;r.runtime.step()
    def failed_query(*a,**k):raise TimeoutError('arbitrary exception text must not be exposed')
    monkeypatch.setattr(rec,'refresh',failed_query)
    result=drive(r,'refresh_reconciliation')
    assert result['state']=='failed' and result['http_status']==503
    assert 'arbitrary exception' not in json.dumps(result)
    with db.read_session(r.path) as conn:assert rec.pending_command(conn,'TMFR1','simulation') is None
    assert not r.broker.trades
