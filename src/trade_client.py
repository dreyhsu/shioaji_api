"""Dashboard client: snapshots and durable commands; no Shioaji login."""
from contextvars import ContextVar
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

from fastapi import HTTPException

import trading

command_id = ContextVar('trade_command_id', default=None)


class TradeClient:
    def __init__(self, db_path, code, *, url='http://127.0.0.1:8766', mode='simulation', timeout=10,
                 request_timeout=8):
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'} or parsed.username or parsed.password or parsed.path not in {'', '/'}:
            raise ValueError('trade URL 必須是本機 HTTP 位址')
        self.db_path, self.code = Path(db_path), code
        self.url, self.mode, self.timeout = url.rstrip('/'), mode, timeout
        self.request_timeout = request_timeout
        self.curve_entry = CurveClient(self)

    def _request(self, path, payload=None):
        request = Request(self.url + path, data=None if payload is None else json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                detail = json.load(error).get('detail', 'trade API 拒絕請求')
            except (ValueError, AttributeError):
                detail = 'trade API 拒絕請求'
            raise HTTPException(error.code, detail) from None
        except (URLError, TimeoutError, OSError) as error:
            timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError)
            detail = 'trade API 回應逾時' if timed_out else '暫時無法連線至 trade 本機 API'
            raise HTTPException(504 if timed_out else 503, detail) from error

    def _snapshot(self):
        result = self._request('/api/snapshot')
        if (os.path.normcase(result['db_path']) != os.path.normcase(str(self.db_path.resolve())) or result['code'] != self.code or result['mode'] != self.mode):
            raise HTTPException(503, 'Dashboard 與 trade 的 DB、商品或環境設定不同')
        return result

    @property
    def simulation(self):
        return self.mode == 'simulation'

    def ensure_connected(self):
        return self._snapshot()

    def status(self):
        try:
            return self._snapshot()['status']
        except HTTPException as error:
            return dict(connected=False, simulation=self.simulation, quote_connected=False,
                        broker_ready=False, bars_ready=False, error=error.detail,
                        mode='模擬環境' if self.simulation else '正式環境',
                        auto_entry_enabled=False, auto_exit_enabled=False, orders=[], curve_entry=None)

    def contracts(self):
        return self._snapshot()['contracts']

    def margin(self):
        return self._snapshot()['margin']

    def orders(self):
        return self._snapshot()['orders']

    def auto_entry_enabled(self):
        return self.status().get('auto_entry_enabled', False)

    def command(self, identity):
        return self._request('/api/commands/' + quote(identity, safe=''))

    def _command(self, name, payload=None, target=None):
        identity = command_id.get()
        if not identity:
            raise HTTPException(400, '交易請求必須提供 Idempotency-Key')
        self._snapshot()  # verify DB/code/mode before accepting any action
        try:
            result = self._request('/api/commands', dict(id=identity, name=name, target=target, payload=payload or {}))
        except HTTPException as error:
            if error.status_code not in {502, 503, 504}:
                raise
            # A lost POST response is ambiguous. Query this ID; never POST again.
            result = {'state': 'accepted'}
        deadline = time.monotonic() + self.timeout
        while result['state'] in {'accepted', 'processing'} and time.monotonic() < deadline:
            time.sleep(.1)
            try:
                result = self.command(identity)
            except HTTPException as error:
                if error.status_code not in {404, 502, 503, 504}:
                    raise
                # A transient read failure says nothing about command execution.
                continue
        if result['state'] == 'succeeded':
            return result['result']
        if result['state'] in {'accepted', 'processing', 'unknown'}:
            raise HTTPException(504, f'指令 {identity} 結果待核對，請使用同一 ID 查詢')
        detail = (result.get('result') or {}).get('detail', '指令未執行')
        if isinstance(detail, dict) and detail.get('code') == 'risk_conflict':
            raise trading.RiskConflict(detail['message'])
        if isinstance(detail, dict) and detail.get('code') == 'confirmation_required':
            raise trading.RiskConfirmation(detail['message'])
        raise ValueError(detail)

    def subscribe_contract(self, contract_code):
        return self._command('subscribe', {'contract_code': contract_code})

    def place_order(self, request):
        return self._command('place_order', request)

    def adopt_external_position(self, **payload):
        return self._command('adopt_external', payload)

    def cancel_entry(self, row_id):
        return self._command('cancel_entry', target=row_id)

    def update_levels(self, row_id, stop, target):
        return self._command('update_levels', {'stop_price': stop, 'target_price': target}, row_id)

    def update_risk_settings(self, row_id, **kwargs):
        return self._command('update_risk', kwargs, row_id)

    def update_curve(self, row_id, target_price):
        return self._command('update_curve', {'target_price': target_price}, row_id)

    def set_order_curve(self, row_id, curve_id):
        return self._command('select_curve', {'curve_id': curve_id}, row_id)

    def close(self):
        pass  # closing a browser must never stop the independent trading owner


class CurveClient:
    def __init__(self, client):
        self.client = client

    def snapshot(self):
        return self.client.status().get('curve_entry')

    def arm(self, **kwargs):
        return self.client._command('arm_curve', kwargs)

    def cancel(self):
        return self.client._command('cancel_curve')
