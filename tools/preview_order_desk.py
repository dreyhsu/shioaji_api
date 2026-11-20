"""Serve the order desk with a disposable, paused offline Mock on port 8779.

No production DB, credentials, broker session or Telegram access. Ctrl+C stops
the preview. The printed temporary path contains all synthetic preview records.
"""
from pathlib import Path
import sys
from tempfile import mkdtemp

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

import pandas as pd
import shioaji
import uvicorn
from fastapi.staticfiles import StaticFiles
import db
import dashboard
import market_health as mh
from mock_runtime import MockRuntime
import order_reconciliation as rec


def forbidden(*args,**kwargs):
    raise RuntimeError('This preview permits only the injected offline MockBroker')


def main():
    shioaji.Shioaji=forbidden
    folder=Path(mkdtemp(prefix='shioaji-orderdesk-preview-'))
    settings=folder/'trading.json'
    settings.write_text('{"auto_entry_enabled":true,"auto_exit_enabled":false}',encoding='utf-8')
    runtime=MockRuntime(folder/'preview.db',settings_path=settings)
    service=runtime.service
    day=pd.Timestamp(runtime.now()).normalize()
    sessions=[]
    for offset in (-1,0,1):
        start=day+pd.Timedelta(days=offset)
        sessions.extend([dict(start=str(start+pd.Timedelta(hours=8,minutes=45)),end=str(start+pd.Timedelta(hours=13,minutes=45))),
                         dict(start=str(start+pd.Timedelta(hours=15)),end=str(start+pd.Timedelta(days=1,hours=5)))])
    service._calendar=mh.SessionCalendar(dict(source='offline order desk UI fixture',
        coverage_start=str(day-pd.Timedelta(days=2)),coverage_end=str(day+pd.Timedelta(days=4)),sessions=sessions))
    with db.session(service.db_path) as conn:
        for i in range(37):
            conn.execute('''INSERT INTO curve_entries (code,simulation,curve_id,contract_code,quantity,action,state,armed_at,check_from)
                VALUES ('TMFR1',1,?,?,2,'Sell','cancelled',?,?)''',
                (100+i,runtime.broker.contract.code,
                 str(day-pd.Timedelta(days=i+1)),str(day-pd.Timedelta(days=i+1))))
        conn.execute('''INSERT INTO curve_entries(id,code,simulation,curve_id,contract_code,quantity,action,state,armed_at,check_from,last_checked_minute)
            VALUES (46,'TMFR1',1,123,?,2,'Sell','submitting',?,?,?)''',
            (runtime.broker.contract.code,str(runtime.now()-pd.Timedelta(minutes=45)),str(runtime.now()-pd.Timedelta(minutes=44)),str(runtime.now()-pd.Timedelta(minutes=1))))
        conn.execute("INSERT INTO latency_entry_links VALUES (46,'offline-legacy-46','offline-legacy-46')")
        conn.commit()
    dashboard.CONFIG.update(db_path=service.db_path,control_db_path=service.db_path,code='TMFR1',mode='mock')
    dashboard.app.state.mock_runtime=runtime
    dashboard.app.mount('/static',StaticFiles(directory=str(dashboard.WEB_DIR)),name='orderdesk-static')
    print(f'Offline synthetic preview: http://127.0.0.1:8779/orders\nTemporary DB: {service.db_path}',flush=True)
    uvicorn.run(dashboard.app,host='127.0.0.1',port=8779,log_level='warning')


if __name__=='__main__':main()
