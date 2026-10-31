"""Offline acquisition tests: no credentials, network, production DB, or orders."""
from contextlib import contextmanager
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import db
from file_lease import LeaseUnavailable
from kbar_fetcher import KbarFetcher, MinuteAccumulator
from market_fetcher import FetcherSettings, SessionSchedule, Tick
import market_health as mh
from tick_fetcher import TickFetcher

pytestmark = pytest.mark.integration


def calendar():
    return mh.SessionCalendar({
        'source': 'offline verified exchange calendar fixture',
        'coverage_start': '2026-10-26', 'coverage_end': '2026-11-03',
        'sessions': [{'start': '2026-10-30 08:45', 'end': '2026-10-30 13:45'},
                     {'start': '2026-10-30 15:00', 'end': '2026-10-31 05:00'},
                     {'start': '2026-11-02 08:45', 'end': '2026-11-02 13:45'}],
    })


class FakeApi:
    def __init__(self):
        self.callback = None
        self.down = lambda: None
        self.closed = False
        self.subscribed = []
        self.Contracts = NS(Futures=NS(TMF={
            code: NS(code=code, symbol=code, target_code='TMFK6' if code == 'TMFR1' else code)
            for code in ('TMFR1', 'TMFK6', 'TMFL6')}))
        self.quote = NS(subscribe=self.subscribe)

    def subscribe(self, contract, **_):
        self.subscribed.append(contract.code)

    def set_on_tick_fop_v1_callback(self, callback):
        self.callback = callback

    def set_session_down_callback(self, callback):
        self.down = callback

    def tick(self, stamp, price=20000, code='TMFK6', volume=1, **kwargs):
        self.callback(NS(code=code, datetime=pd.Timestamp(stamp), close=price,
                         volume=volume, amount=price * volume, **kwargs))


@pytest.fixture
def rig(tmp_path):
    now, mono, apis, login_failures = [pd.Timestamp('2026-10-30 09:00')], [0.], [], []
    @contextmanager
    def connect():
        api = FakeApi()
        apis.append(api)
        try:
            if login_failures:
                raise login_failures.pop(0)
            yield api
        finally:
            api.closed = True
    sdk = NS(QuoteType=NS(Tick='tick'), QuoteVersion=NS(v1='v1'))
    services = []
    def create(cls=TickFetcher, **kwargs):
        service = cls(tmp_path / 'acquisition.db', kwargs.pop('codes', ['TMFR1']), mode=kwargs.pop('mode', 'simulation'),
                      settings=kwargs.pop('settings', FetcherSettings()), calendar=calendar(),
                      clock=lambda: now[0], monotonic=lambda: mono[0], sdk=sdk,
                      connect_factory=connect, **kwargs)
        services.append(service)
        return service
    def advance(stamp, seconds=1):
        now[0] = pd.Timestamp(stamp)
        mono[0] += seconds
    yield NS(create=create, now=now, mono=mono, apis=apis, advance=advance, failures=login_failures)
    for service in reversed(services):
        service.close()


def rows(service, table):
    return [dict(row) for row in service.store.conn.execute(f'SELECT * FROM {table}')]


@pytest.mark.parametrize('stamp,allowed', [
    ('2026-10-30 08:43:59', False), ('2026-10-30 08:44', True),
    ('2026-10-30 13:45:59', True), ('2026-10-30 13:46', False),
    ('2026-10-30 14:59', True), ('2026-10-31 04:59:59', True),
    ('2026-10-31 05:00:59', True), ('2026-10-31 05:01', False),
    ('2026-10-31 14:59', False), ('2026-11-01 19:00', False),
    ('2026-11-02 08:44', True), ('2026-11-03 08:44', False),
])
def test_schedule_boundaries_and_verified_holidays(stamp, allowed):
    assert bool(SessionSchedule(FetcherSettings()).window(stamp, calendar())) is allowed


@pytest.mark.parametrize('values', [
    {'scheduled_login_times': ['8:44']}, {'scheduled_logout_times': ['25:01']},
    {'scheduled_login_times': []}, {'scheduled_login_times': ['08:44', '08:44']},
    {'scheduled_login_times': ['08:44']}, {'tick_publish_seconds': 0},
    {'queue_capacity': True}, {'kbar_publish_seconds': 30},
    {'bar_close_delay_seconds': 60}, {'timezone': 'UTC'}, {'backfill_days': -1},
])
def test_invalid_config_is_rejected(values):
    with pytest.raises(ValueError):
        FetcherSettings(**values)


def test_bom_relative_config_and_custom_login_times(tmp_path):
    path = tmp_path / 'services.json'
    path.write_text(json.dumps({'scheduled_login_times': ['08:40', '14:55'],
                               'calendar_path': 'calendar.json'}), encoding='utf-8-sig')
    settings = FetcherSettings.load(path)
    assert settings.calendar_path == str(tmp_path / 'calendar.json')
    assert SessionSchedule(settings).window('2026-10-30 08:41', calendar())
    path.write_text('{"typo": 10}')
    with pytest.raises(ValueError):
        FetcherSettings.load(path)


def test_tick_latest_is_published_per_second_without_inventing_timestamps(rig):
    service = rig.create()
    service.step()
    api = rig.apis[0]
    api.tick(rig.now[0], 20000)
    service.step()
    assert rows(service, 'market_latest_quotes')[0]['price'] == 20000
    rig.advance('2026-10-30 09:00:00.500', .5)
    api.tick(rig.now[0], 20005)
    service.step()
    assert rows(service, 'market_latest_quotes')[0]['price'] == 20000
    rig.advance('2026-10-30 09:00:01', .5)
    service.step()
    record = rows(service, 'market_latest_quotes')[0]
    assert record['price'] == 20005 and record['quote_at'].endswith('00.500000')
    rig.advance('2026-10-30 09:00:11', 10)
    service.step()
    assert rows(service, 'market_latest_quotes')[0]['quote_at'] == record['quote_at']
    assert service.health['stream_fresh'] is False
    assert not service.store.conn.execute("SELECT name FROM sqlite_master WHERE name='minute_bars'").fetchone()


def test_late_quote_does_not_replace_latest_and_invalid_ticks_are_ignored(rig):
    service = rig.create()
    service.step()
    api = rig.apis[0]
    api.tick(rig.now[0], 20001)
    api.tick(rig.now[0] - pd.Timedelta(seconds=1), 19000)
    api.tick(rig.now[0], float('nan'))
    api.tick(rig.now[0], 20000, simtrade=1)
    api.tick(rig.now[0] + pd.Timedelta(seconds=6), 21000)
    service.step()
    assert rows(service, 'market_latest_quotes')[0]['price'] == 20001
    assert service.health['invalid_ticks'] == 3


def test_callback_accepts_exchange_argument_and_timezone_conversion(rig):
    service = rig.create()
    service.step()
    rig.apis[0].callback('TAIFEX', NS(code='TMFK6', datetime=pd.Timestamp('2026-10-30 01:00Z'), close=20000))
    service.step()
    assert rows(service, 'market_latest_quotes')[0]['quote_at'] == '2026-10-30 09:00:00'


def test_two_services_have_separate_sessions_and_can_stop_independently(rig):
    tick = rig.create()
    bar = rig.create(KbarFetcher, backfill_enabled=False)
    tick.step()
    bar.step()
    assert len(rig.apis) == 2
    rig.apis[0].tick(rig.now[0], 20001)
    rig.apis[1].tick(rig.now[0], 21000)
    tick.close()
    assert rig.apis[0].closed and not rig.apis[1].closed
    rig.advance('2026-10-30 09:01:02', 62)
    bar.step()
    assert rows(bar, 'market_stream_bars')[0]['close'] == 21000


def test_duplicate_role_is_rejected_but_other_mode_data_is_separate(rig):
    first = rig.create()
    first.step()
    second = rig.create(mode='production')
    with pytest.raises(LeaseUnavailable):
        second.step()
    assert len(rig.apis) == 1
    rig.apis[0].tick(rig.now[0])
    first.step()
    first.close()
    second.step()
    rig.apis[-1].tick(rig.now[0], 21000)
    second.step()
    assert {row['mode'] for row in rows(second, 'market_latest_quotes')} == {'simulation', 'production'}


@pytest.mark.parametrize('last,cutoff,next_open', [
    ('2026-10-30 13:45:59', '2026-10-30 13:46', '2026-10-30 14:59'),
    ('2026-10-31 05:00:59', '2026-10-31 05:01', '2026-11-02 08:44'),
])
def test_scheduled_logout_happens_once_then_next_verified_login(rig, last, cutoff, next_open):
    rig.advance(last)
    service = rig.create()
    service.step()
    old = rig.apis[-1]
    rig.advance(cutoff)
    service.step()
    assert old.closed and service.health['state'] == 'scheduled_closed'
    service.step()
    assert len(rig.apis) == 1 and not service.gate.pending
    rig.advance(next_open, 3600)
    service.step()
    assert len(rig.apis) == 2 and not rig.apis[-1].closed


def test_sleep_across_sessions_replaces_the_old_connection(rig):
    service = rig.create()
    service.step()
    rig.advance('2026-10-30 16:00', 25200)
    service.step()
    assert len(rig.apis) == 2 and rig.apis[0].closed


def test_failed_login_has_backoff_and_no_retry_during_scheduled_rest(rig):
    rig.failures.extend([RuntimeError('SessionNotEstablished')] * 5)
    service = rig.create()
    service.step()
    assert service.health['state'] == 'failed' and rig.apis[0].closed
    service.step()
    assert len(rig.apis) == 1
    rig.advance('2026-10-30 09:00:06', 6)
    service.step()
    assert len(rig.apis) == 2
    rig.advance('2026-10-30 13:46', 18000)
    service.step()
    assert len(rig.apis) == 2 and service.health['state'] == 'scheduled_closed'


def test_session_down_recovery_ignores_callbacks_from_old_api(rig):
    service = rig.create()
    service.step()
    old = rig.apis[0]
    old.down()
    service.step()
    assert old.closed and service.gate.pending
    rig.advance('2026-10-30 09:00:06', 6)
    service.step()
    assert len(rig.apis) == 2
    old.tick(rig.now[0], 1)
    old.down()
    rig.apis[-1].tick(rig.now[0], 20001)
    service.step()
    assert not service.down.is_set() and not service.gate.pending
    assert rows(service, 'market_latest_quotes')[0]['price'] == 20001


def test_queue_overflow_is_visible_and_requests_recovery(rig):
    service = rig.create(settings=replace(FetcherSettings(), queue_capacity=1))
    service.step()
    for price in (20000, 20001):
        rig.apis[0].tick(rig.now[0], price)
    service.step()
    assert service.gate.reason == 'queue_overflow'
    assert service.health['queue_overflows'] == 1 and rig.apis[0].closed


def test_db_lock_does_not_relogin_or_discard_pending_quotes(rig, monkeypatch):
    service = rig.create()
    service.step()
    api = rig.apis[0]
    api.tick(rig.now[0], 20001)
    original = service.store.quotes
    def blocked(_):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(service.store, 'quotes', blocked)
    service.step()
    assert service.health['error']['kind'] == 'database' and not service.gate.pending
    monkeypatch.setattr(service.store, 'quotes', original)
    service.step()
    assert len(rig.apis) == 1 and rows(service, 'market_latest_quotes')[0]['price'] == 20001


def test_ohlcv_uses_tick_event_order_and_delays_close():
    accumulator = MinuteAccumulator('2026-10-30 09:00')
    for stamp, price, volume in [('09:00:40', 101, 2), ('09:00:10', 99, 1), ('09:00:50', 105, 3)]:
        at = pd.Timestamp('2026-10-30 ' + stamp)
        accumulator.add(Tick('TMFR1', at, at, price, volume, price * volume))
    assert accumulator.finish('2026-10-30 09:01:01') == []
    row = accumulator.finish('2026-10-30 09:01:02')[0]
    assert [row[key] for key in ('open', 'high', 'low', 'close', 'volume', 'amount')] == [99, 105, 99, 105, 6, 616]
    assert row['complete']


def test_partial_start_and_late_ticks_are_not_certified(rig):
    rig.advance('2026-10-30 09:00:30')
    service = rig.create(KbarFetcher, backfill_enabled=False)
    service.step()
    rig.apis[0].tick(rig.now[0])
    rig.advance('2026-10-30 09:01:02', 32)
    service.step()
    assert rows(service, 'market_stream_bars')[0]['complete'] == 0
    rig.advance('2026-10-30 09:01:03', 1)
    rig.apis[0].tick(rig.now[0])
    service.step()
    rig.advance('2026-10-30 09:02:02', 59)
    service.step()
    assert rows(service, 'market_stream_bars')[1]['complete'] == 1
    rig.apis[0].tick('2026-10-30 09:01:50')
    service.step()
    assert rows(service, 'market_stream_bars')[1]['complete'] == 0


def test_no_empty_bars_or_closing_auction_are_fabricated(rig):
    rig.advance('2026-10-30 13:44')
    service = rig.create(KbarFetcher, backfill_enabled=False)
    service.step()
    rig.advance('2026-10-30 13:44:59', 59)
    rig.apis[0].tick('2026-10-30 13:44:59')
    rig.advance('2026-10-30 13:45:02', 3)
    rig.apis[0].tick('2026-10-30 13:45:00')
    service.step()
    rig.advance('2026-10-30 13:46', 58)
    service.step()
    result = rows(service, 'market_stream_bars')
    assert len(result) == 1 and result[0]['minute_start'] == '2026-10-30 13:44:00'


def test_last_night_minute_is_written_before_0501_logout(rig):
    rig.advance('2026-10-31 04:59')
    service = rig.create(KbarFetcher, backfill_enabled=False)
    service.step()
    rig.advance('2026-10-31 04:59:59', 59)
    rig.apis[0].tick('2026-10-31 04:59:59', 20001)
    rig.advance('2026-10-31 05:00:02', 3)
    service.step()
    assert rows(service, 'market_stream_bars')[0]['minute_start'] == '2026-10-31 04:59:00'
    rig.advance('2026-10-31 05:01', 58)
    service.step()
    assert rig.apis[0].closed


def test_history_only_on_start_gap_or_session_close_and_parity_is_recorded(rig):
    requests = []
    def history(*args, **kwargs):
        requests.append((args, kwargs))
        return pd.DataFrame([dict(minute_start=pd.Timestamp('2026-10-30 09:00'),
                                 open=20000, high=20000, low=20000, close=20000, volume=1, amount=20000)])
    service = rig.create(KbarFetcher, fetch_history=history)
    service.step()
    assert len(requests) == 1 and not rows(service, 'market_stream_bars')  # reject still-open history bar
    rig.apis[0].tick(rig.now[0], 20000)
    rig.advance('2026-10-30 09:01:02', 62)
    rig.apis[0].tick(rig.now[0], 20000)
    service.step()
    assert len(requests) == 1
    service.last_history = float('-inf')
    service.history_pending = True
    service.step()
    assert rows(service, 'market_bar_checks')[0]['result'] == 'match'
    changed = dict(minute_start=pd.Timestamp('2026-10-30 09:00'), open=20001, high=20001,
                   low=20001, close=20001, volume=1, amount=20001, complete=True)
    service.store.bars(service.code, service.mode, [changed], 'broker', service.instance, service.generation)
    assert rows(service, 'market_bar_checks')[0]['result'] == 'mismatch'


def test_failed_final_history_does_not_spin_every_step(rig):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise ValueError('bad broker history')
    rig.advance('2026-10-31 04:59')
    service = rig.create(KbarFetcher, fetch_history=unavailable)
    service.step()
    assert len(calls) == 1
    rig.advance('2026-10-31 05:00:02', 62)
    service.step()
    assert len(calls) == 2  # one final reconciliation allowed
    for _ in range(5):
        service.step()
    assert len(calls) == 2
    assert service.health['history_error']['type'] == 'ValueError'


def test_validation_origin_survives_restart_without_touching_strategy_bars(rig, tmp_path):
    conn = db.connect(tmp_path / 'acquisition.db')
    db.init_schema(conn)
    conn.execute("INSERT INTO minute_bars (code,minute_start,close) VALUES ('TMFR1','2026-10-30 08:50:00',123)")
    conn.commit()
    conn.close()
    first = rig.create(KbarFetcher, backfill_enabled=False)
    first.step()
    assert first.since == pd.Timestamp('2026-10-30 08:49')
    first.close()
    second = rig.create(KbarFetcher, backfill_enabled=False)
    second.step()
    assert second.since == pd.Timestamp('2026-10-30 08:49')
    assert second.store.conn.execute('SELECT close FROM minute_bars').fetchone()[0] == 123


def test_subscribe_failure_closes_partial_connection_and_retries_with_backoff(rig, monkeypatch):
    original = FakeApi.subscribe
    def unavailable(*_args, **_kwargs):
        raise ValueError('subscription setup failed')
    monkeypatch.setattr(FakeApi, 'subscribe', unavailable)
    service = rig.create()
    service.step()
    assert rig.apis[0].closed and service.api is None and service.gate.pending
    service.step()
    assert len(rig.apis) == 1
    monkeypatch.setattr(FakeApi, 'subscribe', original)
    rig.advance('2026-10-30 09:00:06', 6)
    service.step()
    assert len(rig.apis) == 2 and not rig.apis[-1].closed


def test_pending_kbar_survives_storage_failure_without_broker_relogin(rig, monkeypatch):
    service = rig.create(KbarFetcher, backfill_enabled=False)
    service.step()
    rig.apis[0].tick(rig.now[0])
    rig.advance('2026-10-30 09:01:02', 62)
    rig.apis[0].tick(rig.now[0])
    original = service.store.bars
    def blocked(*_args):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(service.store, 'bars', blocked)
    service.step()
    assert service.pending_rows and len(rig.apis) == 1 and not service.gate.pending
    monkeypatch.setattr(service.store, 'bars', original)
    service.step()
    assert rows(service, 'market_stream_bars')[0]['minute_start'] == '2026-10-30 09:00:00'
    assert not service.pending_rows and len(rig.apis) == 1


def test_reconnect_does_not_certify_a_minute_across_the_gap(rig):
    service = rig.create(KbarFetcher, backfill_enabled=False)
    service.step()
    rig.apis[0].tick(rig.now[0])
    service.step()
    original_generation = service.generation
    rig.advance('2026-10-30 09:00:30', 30)
    rig.apis[0].down()
    service.step()
    rig.advance('2026-10-30 09:00:36', 6)
    service.step()
    rig.apis[-1].tick(rig.now[0], 20001)
    rig.advance('2026-10-30 09:01:02', 26)
    service.step()
    record = rows(service, 'market_stream_bars')[0]
    assert record['complete'] == 0
    assert len(rig.apis) == 2 and original_generation != service.generation


@pytest.mark.parametrize('cls', [TickFetcher, KbarFetcher])
def test_quiet_open_stream_stays_stale_without_relogin(rig, cls):
    options = {'backfill_enabled': False} if cls is KbarFetcher else {}
    service = rig.create(cls, **options)
    service.step()
    for minute in (1, 2, 10):
        rig.advance(f'2026-10-30 09:{minute:02d}', 60)
        service.step()
        assert service.health['state'] == 'stale'
        assert not service.health['stream_fresh'] and not service.gate.pending
        assert service.health['quiet_codes'] == ['TMFR1']
        assert len(rig.apis) == 1 and not rig.apis[0].closed
    rig.apis[0].tick(rig.now[0])
    service.step()
    assert service.health['state'] == 'running' and service.health['quiet_codes'] == []


def test_idle_contract_does_not_disconnect_other_live_subscriptions(rig):
    service = rig.create(codes=['TMFR1', 'TMFL6'])
    service.step()
    for minute in (0, 1, 2):
        rig.advance(f'2026-10-30 09:{minute:02d}', 60)
        rig.apis[0].tick(rig.now[0], price=20000 + minute)
        service.step()
        assert len(rig.apis) == 1 and not service.gate.pending
    assert service.health['quiet_codes'] == ['TMFL6']
    assert not service.health['stream_fresh']
    assert rows(service, 'market_latest_quotes')[0]['price'] == 20002
    # A real disconnect still replaces the session and restores both subscriptions.
    rig.apis[0].down()
    service.step()
    assert rig.apis[0].closed
    rig.advance('2026-10-30 09:02:06', 6)
    service.step()
    assert len(rig.apis) == 2
    assert rig.apis[-1].subscribed == ['TMFR1', 'TMFL6']


@pytest.mark.parametrize('cls', [TickFetcher, KbarFetcher])
def test_banned_login_waits_a_full_minute_after_each_rejection(rig, cls):
    rig.failures.extend([RuntimeError('503 操作異常，請1分鐘後再重新登入')] * 2)
    options = {'backfill_enabled': False} if cls is KbarFetcher else {}
    service = rig.create(cls, **options)
    service.step()
    assert service.health['error']['kind'] == 'rate_limit' and rig.apis[0].closed
    for seconds, expected_calls in ((5, 1), (20, 1), (59.9, 1), (60, 2), (65, 2), (119.9, 2), (120, 3)):
        rig.now[0] = pd.Timestamp('2026-10-30 09:00') + pd.Timedelta(seconds=seconds)
        rig.mono[0] = seconds
        service.step()
        assert len(rig.apis) == expected_calls
    assert service.api is rig.apis[-1] and not service.gate.pending


def test_rate_limit_on_connected_session_stops_work_during_cooldown(rig, monkeypatch):
    calls = []
    service = rig.create()
    def limited(*_):
        calls.append(1)
        raise RuntimeError('操作異常，請1分鐘後再重新登入')
    monkeypatch.setattr(service, 'work', limited)
    service.step()
    assert rig.apis[0].closed and service.api is None
    for seconds in (5, 20, 59.9):
        rig.mono[0] = seconds
        service.step()
    assert len(rig.apis) == 1 and len(calls) == 1
    rig.mono[0] = 60
    service.step()
    assert len(rig.apis) == 2 and len(calls) == 2


def test_login_crossing_logout_does_not_start_a_historical_query(rig):
    calls = []
    rig.advance('2026-10-30 13:45:59')
    service = rig.create(KbarFetcher, fetch_history=lambda *_args, **_kwargs: calls.append(1))
    original = service.connect_factory
    @contextmanager
    def delayed():
        with original() as api:
            rig.advance('2026-10-30 13:46:01', 2)
            yield api
    service.connect_factory = delayed
    service.step()
    assert not calls and rig.apis[0].closed and service.health['state'] == 'scheduled_closed'


def test_cli_help_never_starts_a_session(capsys):
    from tick_fetcher import main as tick_main
    from kbar_fetcher import main as bar_main
    for entry in (tick_main, bar_main):
        with pytest.raises(SystemExit) as result:
            entry(['--help'])
        assert result.value.code == 0
        assert '--mode' in capsys.readouterr().out
