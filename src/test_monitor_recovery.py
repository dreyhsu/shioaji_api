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
    yield NS(item=item, conn=conn, store=store, now=now, mono=mono, put=put, config=config)
    item.close()
    store.close()
    conn.close()


def health(r):
    return mh.read_health(r.item.publisher.path)


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


def test_stream_candidates_visible_but_not_trade_ready_until_broker_check(runtime):
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


def test_late_tick_invalidation_removes_previously_imported_candidate(runtime):
    r = runtime
    r.put(source='stream')
    r.item.step()
    r.store.invalidate('TMFR1', 'simulation', [pd.Timestamp('2026-10-30 09:01')])
    r.mono[0] += 1
    r.item.step()
    assert db.last_minute_start(r.conn, 'TMFR1') == pd.Timestamp('2026-10-30 09:00')
    assert health(r)['state'] != 'healthy'


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
