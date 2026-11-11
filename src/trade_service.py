"""Order-only Shioaji service using the monitor's unified database.

Legacy TradingService remains available to isolated replay drivers. This adapter
never subscribes to quotes or queries historical market data.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import uuid

import pandas as pd
from dotenv import load_dotenv

import db
import market_health as mh
import shioaji_client as sc
import trendline as tl
from curve_entry import crossed_curve
from curve_entry_controller import CurveEntryController
from market_fetcher import FetcherSettings, SessionSchedule
from trading import TradingService, _value, crossed_exit, crossed_curve_close


class OrderSession:
    """Recheck the absolute cutoff immediately before every broker mutation."""
    def __init__(self, api, service):
        self.api, self.service = api, service

    def __getattr__(self, name):
        return getattr(self.api, name)

    def _check(self):
        if ((self.service.control and self.service.control.stop_requested)
                or self.service.window() is None
                or self.service._recovery.pending or not self.service._broker_ready):
            raise ValueError('交易窗口或券商核對狀態已改變，暫停送單')

    def place_order(self, *args, **kwargs):
        self._check()
        return self.api.place_order(*args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        self._check()
        return self.api.cancel_order(*args, **kwargs)


def _pending_minute_indices(closed, check_from, watermark):
    """Select work without constructing a pandas Series for every old minute."""
    pending = closed['minute_start'] >= pd.Timestamp(check_from)
    if watermark:
        pending &= closed['minute_start'] > pd.Timestamp(watermark)
    return [index for index in closed.index[pending] if index > 0]


class ClosedMinuteEntry(CurveEntryController):
    """Consume adjacent completed minutes once; never replay expired entries."""
    @staticmethod
    def _signature(curve):
        # Preserve existing armed curve signatures across the upgrade.
        fields = [curve['anchors'], curve['timeframe']]
        if curve['kind'] != 'curve':
            fields.append(curve['kind'])
        return json.dumps(fields, sort_keys=True)

    def check(self):
        service = self.service
        entry = self.snapshot()
        if not entry or entry['state'] != 'armed' or not service._entry_data_ready(entry):
            return
        if not service.auto_entry_enabled():
            self._update(entry['id'], state='cancelled', error='自動進場已停用')
            return
        curve = self._curve(entry['curve_id'])
        if curve is None:
            self._update(entry['id'], state='failed', error='曲線已停用或刪除')
            return
        signature = self._signature(curve)
        with db.session(service.db_path) as conn:
            config = conn.execute('SELECT * FROM trade_curve_versions WHERE entry_id=?', (entry['id'],)).fetchone()
            if config is None:
                # An old armed entry has no version to verify: require re-arming.
                self._update(entry['id'], state='cancelled', error='請重新啟用以確認曲線版本')
                return
            if config['signature'] != signature:
                self._update(entry['id'], state='cancelled', error='曲線已修改，請重新啟用')
                return
            minutes = db.load_minute_bars(conn, service.code)
        now = pd.Timestamp(service._now())
        closed = minutes[minutes['minute_start'] + pd.Timedelta(minutes=1) <= now].reset_index(drop=True)
        contract = service._find_contract(entry['contract_code'])
        if service._fresh_risk_quote({'contract_code': str(contract.code)}) is None:
            return
        watermark = entry.get('last_checked_minute')
        for index in _pending_minute_indices(closed, entry['check_from'], watermark):
            stamp = pd.Timestamp(closed.iloc[index]['minute_start'])
            elapsed = (now - stamp - pd.Timedelta(minutes=1)).total_seconds()
            if elapsed > 60:
                self._update(entry['id'], last_checked_minute=mh.iso(stamp), error='訊號已超過收盤後 60 秒，略過')
                continue
            through = closed.iloc[:index + 1]
            self._update(entry['id'], last_checked_minute=mh.iso(stamp))
            if not crossed_curve(through, curve['anchors'], now, entry['action'], curve['timeframe'], curve['kind']):
                continue
            # Require the fresh signal quote to remain on the breakout side.
            signal_quote = service.signal_quote()
            source = tl.source_bars(through, curve['timeframe'])
            threshold = tl.values_at_times(source, curve['anchors'], [stamp], kind=curve['kind'], timeframe=curve['timeframe']).iloc[0]
            direction = 1 if entry['action'] == 'Buy' else -1
            if signal_quote is None or not math.isfinite(threshold) or direction * (signal_quote - threshold) <= 0:
                self._update(entry['id'], error='新鮮行情已離開突破側，略過訊號')
                continue
            with db.session(service.db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT anchors, timeframe, kind, enabled FROM trendlines WHERE id=?',
                                       (entry['curve_id'],)).fetchone()
                if (current is None or not current['enabled'] or current['kind'] not in tl.KINDS
                        or self._signature({**dict(current), 'anchors': json.loads(current['anchors'])}) != signature):
                    conn.rollback()
                    self._update(entry['id'], state='cancelled', error='曲線版本已變更，請重新啟用')
                    return
                claimed = conn.execute("UPDATE curve_entries SET state='submitting' WHERE id=? AND state='armed'",
                                       (entry['id'],)).rowcount
                conn.commit()
            if not claimed:
                return
            try:
                result = service.place_order(dict(contract_code=entry['contract_code'], action=entry['action'],
                    quantity=entry['quantity'], price=0, price_type='MKT', order_type='IOC',
                    simulation=entry['simulation'], confirmed=True))
                self._update(entry['id'], state='triggered', order_id=result['id'], triggered_at=mh.iso(now), error=None)
            except Exception:
                # Keep the claim, even if the transport accepted then lost its response.
                self._update(entry['id'], state='submitting', error='送單結果待核對，不自動重送')
            return

    def arm(self, *args, **kwargs):
        result = super().arm(*args, **kwargs)
        curve = self._curve(result['curve_id'])
        with db.session(self.service.db_path) as conn:
            conn.execute('INSERT INTO trade_curve_versions VALUES (?,?)',
                         (result['id'], self._signature(curve)))
            conn.commit()
        return result


class IndependentTradingService(TradingService):
    def __init__(self, *args, fetcher_settings=None, **kwargs):
        kwargs['start_worker'] = False
        kwargs['health_enabled'] = True
        super().__init__(*args, **kwargs)
        self.control = None
        self.fetcher_settings = fetcher_settings or FetcherSettings.load()
        self.schedule = SessionSchedule(self.fetcher_settings)
        self.session_started_at = None
        self.logout_unconfirmed = False
        self.session_window = None
        self.contract_cache = []
        self.margin_cache = {'status': 'unavailable'}
        self.last_rpc_sync = float('-inf')
        self.last_margin = float('-inf')
        self.bar_codes = {}
        self.curve_entry = ClosedMinuteEntry(self)
        with db.session(self.db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS trade_curve_versions (entry_id INTEGER PRIMARY KEY, signature TEXT NOT NULL)')
            conn.commit()

    def _session_event(self, event):
        if self.control is not None:
            self.control.session(event)

    def event(self, event, **details):
        path = self.db_path.resolve().parent / 'logs' / 'trade_events.jsonl'
        record = dict(id=uuid.uuid4().hex, timestamp=mh.iso(self._now()), timezone='Asia/Taipei',
                      role='trade', mode='simulation' if self.simulation else 'production',
                      codes=[self.code], instance=self._health.document['instance'] if self._health else None,
                      generation=self._generation, event=event)
        record.update(details)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as output:
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
                output.flush()
                os.fsync(output.fileno())
        except OSError as error:
            self._connect_error = f'稽核寫入失敗：{type(error).__name__}'

    def window(self):
        if not self._calendar_injected:
            self._calendar = mh.SessionCalendar.load(self.fetcher_settings.calendar_path)
        return self.schedule.window(self._now(), self._calendar)

    def ensure_connected(self):
        if self._recovery.pending and self._api is not None:
            raise ValueError('交易連線恢復中，請先核對委託與部位')
        if self.window() is None:
            raise ValueError('目前不在已驗證的交易登入窗口')
        return super().ensure_connected()

    def _open_generation(self, generation):
        self.event('login_started')
        logged_in = False
        try:
            if self.api_factory is not None:
                self._session_event('login_started')
                self._api = self.api_factory(self.simulation)
                self._session_event('login_succeeded')
            else:
                self._api_context = sc.connect(simulation=self.simulation, strict_logout=True, session_observer=self._session_event)
                self._api = self._api_context.__enter__()
            self.event('login_succeeded')
            logged_in = True
            if not self.simulation and self.api_factory is None:
                load_dotenv(Path(__file__).resolve().parents[1] / '.env')
                activated = self._api.activate_ca(ca_path=os.environ['SHIOAJI_CA_PATH'],
                    ca_passwd=os.environ['SHIOAJI_CA_PASSWD'], person_id=os.environ['SHIOAJI_PERSON_ID'])
                if activated is False:
                    raise RuntimeError('正式環境憑證啟用失敗')
            sc.bind_session_down(self._api, lambda: self._session_down.set() if generation == self._generation else None)
            if hasattr(self._api, 'set_order_callback'):
                self._api.set_order_callback(lambda *args: setattr(self, '_last_order_event_at', mh.iso(self._now()))
                                            if generation == self._generation else None)
            self._api = OrderSession(self._api, self)
            self._connected_simulation = self.simulation
            self.session_started_at = pd.Timestamp(self._now())
            self.session_window = self.window()
            self._broker_ready = False
            self._reconcile_required = True
            self._recovery.pending = False
            self.last_rpc_sync = float('-inf')
            self._recover_subscriptions()  # registers DB quote aliases only
            self.contract_cache = super().contracts()
            return self._api
        except Exception as error:
            self.event('session_setup_failed' if logged_in else 'login_failed', reason=type(error).__name__)
            raise

    def _close_api(self):
        api, context = self._api, self._api_context
        closing_generation = self._generation
        self._generation += 1
        try:
            if api is not None:
                if self.control and self._recovery.reason != 'rate_limit':
                    self.control.sample_usage(api)
                if self.api_factory is not None:
                    self._session_event('logout_started')
                self.event('logout_started', generation=closing_generation)
                if context is not None:
                    context.__exit__(None, None, None)
                elif api.logout() is False:
                    raise RuntimeError('logout returned false')
                if self.api_factory is not None:
                    self._session_event('logout_succeeded')
                self.event('logout_succeeded', generation=closing_generation)
        except Exception as error:
            self._session_event('logout_failed')
            self.logout_unconfirmed = True
            self.event('logout_failed', generation=closing_generation, reason=type(error).__name__)
        finally:
            self._api = self._api_context = self._connected_simulation = None
            self._broker_ready = self._sync_ok = False
            self._reconcile_required = True
            self._quotes.clear()
            self._quote_received.clear()
            self._subscribed.clear()
            self._quote_aliases.clear()

    def _subscribe(self, contract):
        code = str(contract.code)
        self._wanted.add(code)
        for key in ('code', 'symbol', 'target_code', 'full_code'):
            alias = str(_value(contract, key, '') or '')
            if alias:
                self._quote_aliases[alias] = code
                if alias != code:
                    self._wanted.discard(alias)
        self._subscribed.add(code)  # local watchlist only; no broker subscription
        self.load_quotes()

    def load_quotes(self):
        mode = 'simulation' if self.simulation else 'production'
        with db.read_session(self.db_path) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='market_latest_quotes'").fetchone()
            rows = conn.execute('SELECT * FROM market_latest_quotes WHERE mode=?', (mode,)).fetchall() if exists else []
        self._quotes.clear()
        self._quote_received.clear()
        for row in rows:
            try:
                stamp, received = pd.Timestamp(row['quote_at']), pd.Timestamp(row['received_at'])
                price = float(row['price'])
                if not math.isfinite(price) or price <= 0 or pd.isna(stamp) or pd.isna(received):
                    continue
                if self.session_started_at is not None and received < self.session_started_at:
                    continue
                code = self._quote_aliases.get(row['code'], row['code'])
                previous = self._quotes.get(code)
                if previous and previous['at'] >= stamp:
                    continue
                self._quotes[code] = {'price': price, 'at': stamp}
                self._quote_received[code] = self._monotonic() - max(0, (pd.Timestamp(self._now()) - received).total_seconds())
                self.bar_codes[code] = row['code']
            except (TypeError, ValueError):
                continue

    def signal_quote(self):
        quote = self._quotes.get(self.code)
        if quote and -5 <= (pd.Timestamp(self._now()) - quote['at']).total_seconds() <= self.health_settings.tick_stale_seconds:
            return quote['price']
        return None

    def _maintain_connection(self):
        # Quote faults belong to fetchers; never reconnect the order session for stale data.
        if self._session_down.is_set():
            self._session_down.clear()
            self._broker_ready = False
            self._reconcile_required = True
            self._recovery.fail('session_down', immediate=True)
        if self._recovery.due and self.health_settings.trading_reconnect_enabled:
            self._recovery.attempted()
            self._close_api()
            self._open()

    def _entry_data_ready(self, entry):
        return (self._curve_data_ready() and self._broker_ready and not self._recovery.pending
                and self._api is not None and self.window() is not None)

    def _trades_by_id(self):
        if self._monotonic() - self.last_rpc_sync >= self.health_settings.order_sync_seconds:
            self.last_rpc_sync = self._monotonic()
            result = super()._trades_by_id()
            if not self._sync_ok:
                self._connect_error = '券商委託同步失敗，等待核對'
            return result
        return {str(_value(item.order, 'id', _value(item.status, 'id'))): item for item in self._api.list_trades()}

    def closed_price(self, row):
        code = self.bar_codes.get(row['contract_code'], row['contract_code'])
        ready, _ = mh.monitor_ready(self.db_path, code, self._now(), self.health_settings,
                                   mode='simulation' if self.simulation else 'production')
        if not ready:
            return None
        cutoff = pd.Timestamp(self._now()).floor('min') - pd.Timedelta(minutes=1)
        with db.read_session(self.db_path) as conn:
            last = conn.execute('SELECT minute_start, close FROM minute_bars WHERE code=? AND minute_start<=? '
                                'ORDER BY minute_start DESC LIMIT 1', (code, mh.iso(cutoff))).fetchone()
        if last is None:
            return None
        minute = pd.Timestamp(last['minute_start'])
        # Share the monitor's freshness budget. A separate 60s cutoff falsely
        # rejects the previous complete bar during the configured close delay.
        if (pd.Timestamp(self._now()) - minute - pd.Timedelta(minutes=1)).total_seconds() > self.health_settings.bar_lag_seconds:
            return None
        # A position cannot react to a candle completed before it was entered.
        if row.get('created_at') and minute + pd.Timedelta(minutes=1) <= pd.Timestamp(row['created_at']):
            return None
        return float(last['close'])

    def _fresh_risk_quote(self, row):
        if super()._fresh_risk_quote(row) is None:
            return None
        return self.closed_price(row)

    def _apply_breakeven(self, row, price):
        closed = self.closed_price(row)
        return row if closed is None else super()._apply_breakeven(row, closed)

    def _exit_signal(self, row, price):
        closed = self.closed_price(row)
        if closed is None or not self._curve_data_ready():
            return None
        stop = crossed_exit(row['action'], closed, float(row['stop_price']), None)
        if stop:
            return stop
        if not row.get('curve_id'):
            return crossed_exit(row['action'], closed, float(row['stop_price']), row['target_price'])
        with db.session(self.db_path) as conn:
            curves = db.load_trendlines(conn, self.code)
            minutes = db.load_minute_bars(conn, self.code)
        curve = next((item for item in curves if item['id'] == row['curve_id'] and item['kind'] in tl.KINDS), None)
        bars = minutes[minutes['minute_start'] + pd.Timedelta(minutes=1) <= pd.Timestamp(self._now())]
        if curve is None or len(bars) < 2:
            return 'curve_unavailable'
        pair = bars.iloc[-2:]
        if not self._calendar.consecutive_trading_minutes(pair.iloc[0]['minute_start'], pair.iloc[-1]['minute_start']):
            return 'curve_unavailable'
        if row.get('curve_check_from') and pair.iloc[-1]['minute_start'] < pd.Timestamp(row['curve_check_from']):
            return None
        values = tl.values_at_times(tl.source_bars(bars, curve['timeframe']), curve['anchors'],
                                    pair['minute_start'].tolist(), kind=curve['kind'], timeframe=curve['timeframe']).tolist()
        if any(not math.isfinite(value) for value in values):
            return 'curve_unavailable'
        return 'curve' if crossed_curve_close(row['action'], float(pair.iloc[0]['close']), values[0],
                                               float(pair.iloc[1]['close']), values[1]) else None

    def place_order(self, request):
        with db.session(self.db_path) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='trade_commands'").fetchone():
                unknown = conn.execute("SELECT id FROM trade_commands WHERE code=? AND mode=? AND state='unknown' LIMIT 1",
                                      (self.code, 'simulation' if self.simulation else 'production')).fetchone()
                if unknown:
                    raise ValueError('先前交易指令結果待核對，暫停新增委託')
        self.ensure_connected()
        self.load_quotes()
        contract = self._find_contract(request['contract_code'])
        self._subscribe(contract)
        reference = {'contract_code': str(contract.code)}
        if super()._fresh_risk_quote(reference) is None:
            raise ValueError(f'月份合約 {contract.code} 的即時報價缺少或已過期（行情／接收時間須在 '
                             f'{self.health_settings.tick_stale_seconds:g} 秒內）')
        if self.closed_price(reference) is None:
            raise ValueError(f'月份合約 {contract.code} 的 monitor 或已收定 1 分 K 尚未就緒（K 棒時效上限 '
                             f'{self.health_settings.bar_lag_seconds:g} 秒）')
        return super().place_order(request)

    def contracts(self):
        return list(self.contract_cache)

    def cancel_entry(self, row_id):
        if not self._broker_ready or self._recovery.pending:
            raise ValueError('委託與部位核對尚未完成，暫停撤單')
        return super().cancel_entry(row_id)

    def _try_auto_exit(self, row, reason):
        if self.window() is None or self._recovery.pending or not self._curve_data_ready():
            return
        return super()._try_auto_exit(row, reason)

    def process_market_update(self):
        with self._lock:
            self.load_quotes()
            self._maintain_connection()
            if self._api is not None and not self._recovery.pending and self.window() is not None:
                self._curve_cache.clear()
                self._curve_break_cache.clear()
                self._last_sync = float('-inf')  # consume callback cache once each cycle; RPC remains throttled
                self._monitor_once()  # exactly one reconciliation cycle

    def _public_order(self, row):
        result = super()._public_order(row)
        current = super()._fresh_risk_quote(row)
        result['protection']['closed_1m_ready'] = self.closed_price(row) is not None
        result['protection']['tick_fresh'] = current is not None
        if current is not None:
            result['last_quote'] = current
        return result

    def snapshot(self):
        status = super().status() if self._api is not None else dict(
            connected=False, session_present=False, simulation=self.simulation, error=self._connect_error,
            auto_entry_enabled=self.auto_entry_enabled(), auto_exit_enabled=self.auto_exit_enabled(),
            curve_entry=self.curve_entry.snapshot(), bars_ready=False, broker_ready=False)
        status.update(signal_mode='closed_1m', scheduled_open=self.window() is not None,
                      logout_unconfirmed=self.logout_unconfirmed, market_source='monitor_db')
        status.setdefault('mode', '模擬環境' if self.simulation else '正式環境')
        status.update(self.breakeven_settings())
        contract_bars = {code: self.closed_price({'contract_code': code}) is not None for code in self._wanted}
        market_quotes = {code: dict(price=quote['price'], at=mh.iso(quote['at']),
            fresh=-5 <= (pd.Timestamp(self._now()) - quote['at']).total_seconds() <= self.health_settings.tick_stale_seconds)
            for code, quote in self._quotes.items()}
        for alias, canonical in self._quote_aliases.items():
            if canonical in market_quotes:
                market_quotes[alias] = market_quotes[canonical]
            if canonical in contract_bars:
                contract_bars[alias] = contract_bars[canonical]
        status['market_quotes'] = market_quotes
        status['contract_bars_ready'] = contract_bars
        if 'protection' in status:
            for key in ('fixed_exit', 'breakeven', 'curve_exit'):
                status['protection'][key] = status['protection'][key] and bool(contract_bars) and all(contract_bars.values())
        return dict(status=status, orders=self.orders(), contracts=self.contracts(), margin=self.margin_cache)
