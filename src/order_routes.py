"""Dashboard order inspection routes. Reads never access the broker."""
import json
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

import db
import order_reconciliation as reconciliation
from trade_client import TradeClient, command_id


class RequestIn(BaseModel):
    name: Literal['refresh_reconciliation', 'preview_reconciliation', 'apply_reconciliation']
    payload: dict = Field(default_factory=dict)


def require_schema(ctx):
    try:
        with db.read_session(ctx['db_path']) as conn:
            ready=reconciliation.exists(conn,'trade_reconciliation_schema')
    except sqlite3.Error:
        raise HTTPException(503,'統一資料庫目前無法讀取，請檢查服務狀態') from None
    if not ready:
        raise HTTPException(503,'委託管理資料表尚未建立，請依啟動流程更新並重啟 trade／Dashboard')


def mock_command(service, identity):
    with db.read_session(service.db_path) as conn:
        row=conn.execute('SELECT * FROM trade_commands WHERE id=? AND code=? AND mode=?',
                         (identity,service.code,reconciliation.mode_of(service))).fetchone()
    if row is None:
        raise HTTPException(404,'找不到指令')
    result=dict(row)
    result['result']=json.loads(result['result']) if result['result'] else None
    return result


def execute_mock(service, identity, name, payload):
    """Offline replay owns the service lock; persist IDs just like trade."""
    from trade import Command
    payload=Command(id=identity,name=name,payload=payload).validated()
    document=reconciliation.dumps(dict(name=name,target=None,payload=payload))
    mode=reconciliation.mode_of(service)
    with service._lock:
        with reconciliation.write_connection(service.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT * FROM trade_commands WHERE id=?',(identity,)).fetchone()
            if old:
                if (old['code'],old['mode'],old['request'])!=(service.code,mode,document):
                    raise HTTPException(409,'指令 ID 已用於不同內容')
            else:
                at=reconciliation.stamp(service._now())
                conn.execute('INSERT INTO trade_commands VALUES (?,?,?,?,?,?,?,?,?)',
                    (identity,service.code,mode,document,'processing',at,at,None,None))
                conn.commit()
        if old:
            saved=mock_command(service,identity)
            if saved['state']=='succeeded':return saved['result']
            raise HTTPException(saved['http_status'] or 409,(saved['result'] or {}).get('detail','指令結果待核對'))
        try:
            result=reconciliation.execute(service,name,payload,identity)
            state,status='succeeded',200
        except reconciliation.Conflict as error:
            state,status,result='failed',409,{'detail':str(error)}
        except Exception as error:
            read_only=name in {'refresh_reconciliation','preview_reconciliation'}
            state,status=('failed',503) if read_only else ('unknown',409)
            result={'detail':'查詢／預覽未完成，既有紀錄保留' if read_only else '處理結果待核對，不自動重送','error_type':type(error).__name__}
        with reconciliation.write_connection(service.db_path) as conn:
            conn.execute('UPDATE trade_commands SET state=?,result=?,http_status=?,updated_at=? WHERE id=?',
                (state,reconciliation.dumps(result),status,reconciliation.stamp(service._now()),identity))
            conn.commit()
        if state!='succeeded':raise HTTPException(status,result['detail'])
        return result


def create_router(context, service_provider):
    router=APIRouter()

    @router.get('/orders',response_class=HTMLResponse)
    def page():
        return HTMLResponse((Path(__file__).parent/'web'/'orders.html').read_text(encoding='utf-8'))

    @router.get('/api/trading/reconciliation/overview')
    def overview():
        ctx=context()
        require_schema(ctx)
        service=service_provider()
        status=service.status() if isinstance(service,TradeClient) else dict(connected=service._api is not None,
            broker_ready=service._broker_ready,bars_ready=True,scheduled_open=True,
            last_inspection_at=getattr(service,'_inspection_verified_at',None),
            manual_order_ready={code:service._broker_ready for code in service._wanted})
        return reconciliation.overview(ctx['db_path'],ctx['code'],ctx['mode'],status)

    @router.get('/api/trading/reconciliation/records')
    def records(tab:Literal['pending','all','positions','conditions','events']='pending',
                limit:int=Query(default=50,ge=1,le=200),cursor:str|None=Query(default=None,max_length=2048),
                since:str|None=None,until:str|None=None,contract:str=Query(default='',max_length=24),
                source:Literal['','system','external']='',side:Literal['','Buy','Sell']='',
                phase:Literal['','entry','exit','condition','command']='',
                state:str=Query(default='',max_length=32),closed:Literal['','0','1']='',
                search:str=Query(default='',max_length=128)):
        ctx=context()
        require_schema(ctx)
        try:
            return reconciliation.list_records(ctx['db_path'],ctx['code'],ctx['mode'],tab=tab,limit=limit,cursor=cursor,
                since=since,until=until,contract=contract,source=source,side=side,phase=phase,state=state,closed=closed,search=search)
        except (ValueError,TypeError) as error:
            raise HTTPException(422,str(error)) from None

    @router.get('/api/trading/reconciliation/detail/{kind}/{target_id}')
    def detail(kind:Literal['order','curve','command','broker'],target_id:str):
        ctx=context()
        require_schema(ctx)
        try:return reconciliation.detail(ctx['db_path'],ctx['code'],ctx['mode'],kind,target_id)
        except reconciliation.Conflict as error:raise HTTPException(404,str(error)) from None

    @router.post('/api/trading/reconciliation/command')
    def submit(body:RequestIn):
        from trade import Command
        identity=command_id.get()
        if not identity:raise HTTPException(400,'交易請求必須提供 Idempotency-Key')
        require_schema(context())
        try:
            payload=Command(id=identity,name=body.name,payload=body.payload).validated()
            service=service_provider()
            if isinstance(service,TradeClient):return service._command(body.name,payload)
            if context()['mode']!='mock':raise HTTPException(503,'正式／模擬必須使用獨立 trade API')
            return execute_mock(service,identity,body.name,payload)
        except reconciliation.Conflict as error:raise HTTPException(409,str(error)) from None
        except ValueError as error:raise HTTPException(422,str(error)) from None

    return router
