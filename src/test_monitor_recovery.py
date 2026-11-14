"""Offline fetcher → monitor integration; the monitor must never log in."""
from types import SimpleNamespace as NS
import json
import sqlite3

import pandas as pd
import pytest

import db
from fetcher_storage import FetcherStore
import market_health as mh
import monitor
import shioaji_client as sc
from test_market_fetchers import calendar, rig

pytestmark = pytest.mark.integration


def bar_rows(start='2026-10-30 09:00', end='2026-10-30 09:01', price=20000, complete=True):
    return [dict(minute_start=stamp, open=price, high=price, low=price, close=price,
                 volume=1, amount=price, complete=complete)
            for stamp in pd.date_range(start, end, freq='min')]


def frame(start, end):
    """Shared canonical frame fixture retained for watchdog tests."""
    return monitor.sb.add_sessions(pd.DataFrame(bar_rows(start, end)))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    now, mono = [pd.Timestamp('2026-10-30 09:02:03')], [0.]
    source = tmp_path / 'fetch' / 'acquisition.db'
    store = FetcherStore(source)
    conn = db.connect(tmp_path / 'unified.db')
    db.init_schema(conn)
    # A pre-existing canonical tail provides the validation origin.
    db.upsert_minute_bars(conn, 'TMFR1', monitor.sb.add_sessions(pd.DataFrame(bar_rows(end='2026-10-30 09:00'))))
    def forbidden(*args, **kwargs):
        raise AssertionError('monitor must not call the broker')
    for name in ('connect', 'fetch_minute_bars', 'fetch_latest_minute_bars'):
        monkeypatch.setattr(sc, name, forbidden)
    monkeypatch.setattr(monitor.market_clock, 'now', lambda: now[0])
    config = monitor.MonitorConfig(db_path=tmp_path / 'unified.db', fetcher_db=source,
                                   enable_telegram=False, alerts_config=tmp_path / 'alerts.json')
    item = monitor.MonitorRuntime(conn, config, settings=mh.HealthSettings(), calendar=calendar(),
                                  clock=lambda: now[0], monotonic=lambda: mono[0])
    def put(rows=None, source='broker', mode='simulation'):
        store.bars('TMFR1', mode, rows or bar_rows(), source, 'offline', 1)
    def live(**changes):
        document = dict(mode='simulation', db_path=str(source.resolve()), codes=['TMFR1'],
                        state='running', instance='offline', generation=1, heartbeat_at=str(now[0]))
        document.update(changes)
        mh.atomic_json(source.parent / '.health' / source.name / 'tick-fetcher.json', document)
        store.quotes([dict(code='TMFR1', mode='simulation', price=20000, volume=1,
                           quote_at=str(now[0]), received_at=str(now[0]), instance='offline', generation=1)])
    yield NS(item=item, conn=conn, store=store, now=now, mono=mono, put=put, live=live, config=config)
    item.close()
    store.close()
    conn.close()


def health(r):
    return mh.read_health(r.item.publisher.path)


def test_blocked_telegram_does_not_delay_next_minute_or_health(runtime, monkeypatch):
    import threading
    from alert_rules import Candidate
    r = runtime
    started, release = threading.Event(), threading.Event()
    def blocked_send(message):
        started.set()
        assert release.wait(5)
        return True
    r.item.notifier.send = blocked_send
    r.config.enable_telegram = True
    monkeypatch.setattr(monitor.ar, 'evaluate', lambda **kwargs: [Candidate('box_enter', 'realtime-test', False, 'm')])
    try:
        r.put()
        r.item.step()
        assert started.wait(3)
        assert health(r)['state'] == 'healthy'
        r.now[0] = pd.Timestamp('2026-10-30 09:03:03')
        r.mono[0] += 1
        r.put(bar_rows('2026-10-30 09:02', '2026-10-30 09:02'))
        r.live()
        r.item.step()
        assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:02')
        current = health(r)
        assert current['state'] == 'healthy'
        assert pd.Timestamp(current['cycle_completed_at']) == r.now[0]
        assert current['notifications']['in_flight'] is not None
        assert current['notifications']['worker_alive']
        r.item.close()
        assert health(r)['notifications']['last_error'] == 'shutdown_delivery_unconfirmed'
    finally:
        release.set()


def test_broker_history_updates_unified_bars_and_indicators_without_login(runtime):
    r = runtime
    r.put()
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:01')
    assert not db.load_hourly_bars(r.conn, 'TMFR1').empty


def test_only_closed_complete_bars_of_selected_mode_are_imported(runtime):
    r = runtime
    r.put()
    r.put(bar_rows('2026-10-30 09:02', '2026-10-30 09:02', 21000))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01', 22000), mode='production')
    r.item.step()
    bars = db.load_minute_bars(r.conn, 'TMFR1')
    assert len(bars) == 2 and bars.iloc[-1]['close'] == 20000


def test_stream_only_waits_for_broker_history(runtime):
    r = runtime
    r.put(source='stream')
    r.item.step()
    assert health(r)['staged_only'] and health(r)['state'] == 'degraded'
    assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:01')
    r.put(bar_rows(price=20005))  # broker revision wins even when stream mismatches
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert db.load_minute_bars(r.conn, 'TMFR1').iloc[-1]['close'] == 20005


def test_broker_history_bridges_to_healthy_tick_tail_without_per_bar_check(runtime):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'), source='stream')
    r.live()
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert not health(r)['staged_only']
    assert health(r)['unverified_stream_minutes'] == 1
    # Further complete tick minutes keep trading ready without a broker refresh.
    r.now[0] += pd.Timedelta(minutes=1)
    r.mono[0] += 1
    r.put(bar_rows('2026-10-30 09:02', '2026-10-30 09:02'), source='stream')
    r.live()
    r.item.step()
    assert health(r)['state'] == 'healthy'
    # Broker revisions still win over a previously trusted tick minute.
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01', price=20005))
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert db.load_minute_bars(r.conn, 'TMFR1').iloc[-2]['close'] == 20005


@pytest.mark.parametrize('changes', [dict(state='stale'), dict(state='stopped'),
    dict(generation=2), dict(instance='other'), dict(mode='production'),
    dict(heartbeat_at='2026-10-30 09:00:00'), dict(recovery={'pending': True})])
def test_live_tail_requires_current_healthy_tick_owner(runtime, changes):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'), source='stream')
    r.live(**changes)
    r.item.step()
    assert health(r)['bars']['ready']
    assert health(r)['state'] == 'degraded'


def test_live_tail_rejects_stale_quotes_and_history_gap(runtime):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'), source='stream')
    r.live()
    r.now[0] += pd.Timedelta(seconds=11)
    r.live(heartbeat_at=str(r.now[0]))
    r.store.conn.execute("UPDATE market_latest_quotes SET quote_at='2026-10-30 09:02:03'")
    r.store.conn.commit()
    r.item.step()
    assert health(r)['stream_error'] == 'stale_or_missing_tick'
    r.now[0] = pd.Timestamp('2026-10-30 09:04:03')
    r.put(bar_rows('2026-10-30 09:03', '2026-10-30 09:03'), source='stream')
    r.live()
    r.mono[0] += 1
    r.item.step()
    assert health(r)['bars']['missing_count'] == 1
    assert health(r)['state'] == 'degraded'


def test_same_frame_is_evaluated_when_tick_health_recovers(runtime, monkeypatch):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'), source='stream')
    r.live(state='stale')
    calls = []
    original = monitor.run_cycle
    def cycle(*args, **kwargs):
        calls.append(kwargs['allow_signals'])
        return original(*args, **kwargs)
    monkeypatch.setattr(monitor, 'run_cycle', cycle)
    r.item.step()
    r.live()
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert calls == [False, True]


def test_routine_derivation_preserves_last_completed_ready_state(runtime, monkeypatch):
    r = runtime
    r.put()
    r.item.step()
    previous_completed = health(r)['cycle_completed_at']
    original = monitor.run_cycle
    observed = []
    def cycle(*args, **kwargs):
        h = health(r)
        observed.append(h)
        return original(*args, **kwargs)
    monkeypatch.setattr(monitor, 'run_cycle', cycle)
    r.put(bar_rows(price=20001))
    r.mono[0] += 1
    r.item.step()
    assert len(observed) == 1
    assert observed[0]['operation'] == 'derive'
    assert observed[0]['state'] == 'healthy'
    assert observed[0]['bars']['ready'] and not observed[0]['validation_pending']
    assert observed[0]['cycle_completed_at'] == previous_completed


def test_first_validation_does_not_claim_ready_during_derivation(runtime, monkeypatch):
    r = runtime
    r.put()
    original = monitor.run_cycle
    def cycle(*args, **kwargs):
        assert health(r)['state'] != 'healthy'
        return original(*args, **kwargs)
    monkeypatch.setattr(monitor, 'run_cycle', cycle)
    r.item.step()
    assert health(r)['state'] == 'healthy'


def test_late_tick_invalidation_removes_candidate_without_disabling_current_data(runtime):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'), source='stream')
    r.live()
    r.item.step()
    assert health(r)['state'] == 'healthy'
    r.store.invalidate('TMFR1', 'simulation', [pd.Timestamp('2026-10-30 09:01')])
    r.mono[0] += 1
    r.item.step()
    assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:00')
    assert health(r)['state'] == 'healthy'
    assert health(r)['incomplete_tick_minutes'] == 1
    assert health(r)['bars']['pending_repair_count'] == 1
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'))
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert health(r)['bars']['pending_repair_count'] == 0


def test_incomplete_restart_minute_does_not_block_live_tail_or_trade_health(runtime, monkeypatch):
    r = runtime
    r.now[0] = pd.Timestamp('2026-10-30 09:03:03')
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01', complete=False), source='stream')
    r.put(bar_rows('2026-10-30 09:02', '2026-10-30 09:02'), source='stream')
    r.live()
    r.item.step()
    h = health(r)
    assert h['state'] == 'healthy' and h['bars']['ready']
    assert h['bars']['missing_count'] == 1
    assert h['bars']['blocking_missing_count'] == 0
    assert h['bars']['pending_repair_count'] == 1
    assert h['stream_error'] is None
    assert pd.Timestamp('2026-10-30 09:01') not in set(db.load_minute_bars(r.conn, 'TMFR1').minute_start)
    # Same shared health gate consumed by Dashboard and trade.
    monkeypatch.setattr(mh.SessionCalendar, 'load', lambda path: r.item.calendar)
    assert mh.monitor_ready(r.config.db_path, 'TMFR1', r.now[0], r.item.settings, mode='simulation')[0]
    # The exemption does not excuse an outage or stale market data.
    r.live(state='stale')
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'degraded'


def test_interior_gap_waits_for_fetcher_repair_without_login(runtime):
    r = runtime
    r.now[0] = pd.Timestamp('2026-10-30 09:05:03')
    r.put([row for row in bar_rows(end='2026-10-30 09:03') if row['minute_start'].minute != 1])
    r.item.step()
    assert health(r)['bars']['missing_count'] == 1
    r.put(bar_rows(end='2026-10-30 09:03'))
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'healthy'


def test_db_failure_is_reported_and_next_cycle_recovers(runtime, monkeypatch):
    r = runtime
    r.put()
    original = db.upsert_minute_bars
    def blocked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(db, 'upsert_minute_bars', blocked)
    r.item.step()
    assert health(r)['error']['kind'] == 'database'
    monkeypatch.setattr(db, 'upsert_minute_bars', original)
    r.item.step()
    assert health(r)['state'] == 'healthy'


def test_missing_source_does_not_create_database_or_fallback_to_broker(runtime):
    r = runtime
    r.config.fetcher_db = r.config.fetcher_db.parent / 'missing.db'
    r.item.step()
    assert not r.config.fetcher_db.exists()
    assert health(r)['state'] == 'failed'


def test_restart_keeps_validation_origin_and_reprocesses_older_corrections(runtime):
    r = runtime
    r.put(source='stream')
    r.item.step()
    r.item.close()
    r.put(bar_rows(price=20001))
    r.item = monitor.MonitorRuntime(r.conn, r.config, calendar=calendar(),
                                  clock=lambda: r.now[0], monotonic=lambda: r.mono[0])
    r.item.step()
    assert r.item.since == pd.Timestamp('2026-10-30 09:00')
    assert health(r)['state'] == 'healthy'


def test_quote_only_polls_do_not_read_or_rewrite_history(runtime, monkeypatch):
    r = runtime
    r.put()
    r.item.step()
    queries = []
    original_connect = sqlite3.connect
    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(sqlite3, 'connect', traced_connect)
    r.conn.set_trace_callback(queries.append)
    for _ in range(5):
        r.now[0] += pd.Timedelta(seconds=1)
        r.mono[0] += 1
        r.live()
        r.item.step()
        assert health(r)['state'] == 'healthy'
        assert health(r)['cycle_completed_at'] == str(r.now[0])
    sql = '\n'.join(queries).lower()
    assert 'select * from market_stream_bars' not in sql
    assert 'select * from minute_bars' not in sql
    assert 'insert into minute_bars' not in sql
    assert 'insert or replace into monitor_bar_sources' not in sql
    assert 'delete from minute_bars' not in sql
    assert r.conn.execute('SELECT quote_at FROM market_latest_quotes').fetchone()[0] == str(r.now[0])


def test_new_bar_and_old_correction_write_only_changed_minutes(runtime, monkeypatch):
    r = runtime
    r.put()
    r.item.step()
    writes = []
    original = db.upsert_minute_bars
    def record(conn, code, bars, **kwargs):
        if not bars.empty:
            writes.extend(bars.minute_start)
        return original(conn, code, bars, **kwargs)
    monkeypatch.setattr(db, 'upsert_minute_bars', record)
    r.now[0] += pd.Timedelta(minutes=1)
    r.mono[0] += 1
    r.put(bar_rows('2026-10-30 09:02', '2026-10-30 09:02'))
    r.item.step()
    assert writes == [pd.Timestamp('2026-10-30 09:02')]
    writes.clear()
    r.put(bar_rows('2026-10-30 09:00', '2026-10-30 09:00', price=21000))
    r.mono[0] += 1
    r.item.step()
    assert writes == [pd.Timestamp('2026-10-30 09:00')]
    assert db.load_minute_bars(r.conn, 'TMFR1').iloc[0]['close'] == 21000


def test_cached_future_bar_becomes_importable_without_another_source_write(runtime):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:02'))
    r.item.step()
    assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:01')
    r.now[0] = pd.Timestamp('2026-10-30 09:03:00')
    r.mono[0] += 1
    r.item.step()
    assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:02')


def test_idle_history_does_not_cache_readiness_forever(runtime):
    r = runtime
    r.put()
    r.item.step()
    assert health(r)['state'] == 'healthy'
    r.now[0] = pd.Timestamp('2026-10-30 09:05:01')
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'degraded'
    assert health(r)['bars']['lag_seconds'] == 181
    r.now[0] = pd.Timestamp('2026-10-30 09:06:01')
    r.mono[0] += 1
    r.item.step()
    assert health(r)['bars']['blocking_missing_count'] > 0


def test_tick_health_and_generation_are_rechecked_without_bar_changes(runtime):
    r = runtime
    r.put(bar_rows(end='2026-10-30 09:00'))
    r.put(bar_rows('2026-10-30 09:01', '2026-10-30 09:01'), source='stream')
    r.live()
    r.item.step()
    assert health(r)['state'] == 'healthy'
    for changes, error in [(dict(state='stale'), 'tick_fetcher_unhealthy'),
                           (dict(generation=2), 'tick_generation_changed')]:
        r.mono[0] += 1
        r.live(**changes)
        r.item.step()
        assert health(r)['stream_error'] == error


def test_replaced_source_with_same_revision_is_not_hidden_by_cache(runtime):
    r = runtime
    r.put()
    r.item.step()
    # A replacement DB can have the same counter. Its identity must differ.
    r.store.conn.execute("UPDATE market_storage_identity SET identity='replacement'")
    r.store.conn.execute('DROP TRIGGER market_bar_version_update')
    r.store.conn.execute('UPDATE market_stream_bars SET close=21000')
    r.store.conn.commit()
    r.mono[0] += 1
    r.item.step()
    assert db.load_minute_bars(r.conn, 'TMFR1').iloc[-1]['close'] == 21000


def test_source_without_revision_tables_still_detects_old_corrections(runtime):
    r = runtime
    for name in ('insert', 'update', 'delete'):
        r.store.conn.execute(f'DROP TRIGGER market_bar_version_{name}')
    r.store.conn.execute('DROP TABLE market_bar_versions')
    r.store.conn.commit()
    r.put()
    r.item.step()
    r.put(bar_rows('2026-10-30 09:00', '2026-10-30 09:00', price=21000))
    r.mono[0] += 1
    r.item.step()
    assert health(r)['state'] == 'healthy'
    assert db.load_minute_bars(r.conn, 'TMFR1').iloc[0]['close'] == 21000


def test_audit_deduplicates_restart_and_waits_for_complete_lines(runtime):
    r = runtime
    folder = r.config.fetcher_db.parent / 'logs'
    folder.mkdir()
    record = dict(id='evt-1', timestamp='2026-10-30 09:00:00', role='tick', mode='simulation',
                  event='realtime_fetch_failed', reason='session_down')
    path = folder / 'tick_events.jsonl'
    text = json.dumps(record)
    path.write_text(text[:20])
    r.item.step()
    assert r.conn.execute('SELECT COUNT(*) FROM monitor_fetch_events').fetchone()[0] == 0
    with path.open('a') as out:
        out.write(text[20:] + '\n')
    r.item.step()
    r.item.close()
    r.item = monitor.MonitorRuntime(r.conn, r.config, calendar=calendar(), clock=lambda: r.now[0])
    r.item.step()
    assert r.conn.execute('SELECT COUNT(*) FROM monitor_fetch_events').fetchone()[0] == 1
    assert 'session_down' in r.item.audit_path.read_text()


def test_tick_events_are_collected_by_monitor_with_original_timestamp(rig, tmp_path):
    tick = rig.create()
    tick.step()
    rig.apis[0].down()
    tick.step()
    tick.close()
    path = tmp_path / 'monitor.db'
    with db.session(path) as conn:
        config = monitor.MonitorConfig(db_path=path, fetcher_db=tmp_path / 'acquisition.db', enable_telegram=False)
        runtime = monitor.MonitorRuntime(conn, config, calendar=calendar(), clock=lambda: rig.now[0])
        try:
            runtime.step()
            records = [dict(row) for row in conn.execute('SELECT * FROM monitor_fetch_events')]
            assert {'login_started', 'login_succeeded', 'logout_started', 'logout_succeeded',
                    'realtime_fetch_failed'} <= {row['event'] for row in records}
            failed = next(json.loads(row['document']) for row in records if row['event'] == 'realtime_fetch_failed')
            assert failed['timestamp'] == str(rig.now[0]) and failed['reason'] == 'session_down'
        finally:
            runtime.close()
