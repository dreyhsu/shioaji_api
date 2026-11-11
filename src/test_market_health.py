import json
from pathlib import Path
import pandas as pd
import pytest
import market_health as mh


def calendar_document(start="2026-10-27 15:00", end="2026-10-28 05:00"):
    return {"source": "offline exchange-calendar fixture", "coverage_start": "2026-10-26",
            "coverage_end": "2026-10-30", "sessions": [{"start": start, "end": end}]}


def bars(start, end):
    return pd.DataFrame({"minute_start": pd.date_range(start, end, freq="min")})


def test_night_end_and_breaks_have_exclusive_minutes():
    calendar = mh.SessionCalendar(calendar_document())
    expected = calendar.expected_minutes("2026-10-27 15:00", "2026-10-28 05:01")
    assert len(expected) == 840
    assert pd.Timestamp("2026-10-28 04:59") in expected
    assert pd.Timestamp("2026-10-28 05:00") not in expected
    assert not calendar.active("2026-10-28 05:00")
    assert not calendar.active("2026-10-28 10:00")  # fixture declares a holiday


@pytest.mark.parametrize('previous,current,expected', [
    ('2026-10-27 13:43', '2026-10-27 13:44', True),
    ('2026-10-27 13:44', '2026-10-27 15:00', True),
    ('2026-10-28 04:59', '2026-10-28 08:45', True),
    ('2026-10-28 13:44', '2026-10-29 08:45', True),  # declared holiday/night closure
    ('2026-10-27 13:42', '2026-10-27 13:44', False),
    ('2026-10-27 13:43', '2026-10-27 15:00', False),  # missing final minute
    ('2026-10-27 13:44', '2026-10-27 15:01', False),  # missing opening minute
    ('2026-10-27 13:44', '2026-10-28 08:45', False),  # missing entire night session
    ('2026-10-27 13:44', '2026-10-30 08:45', False),  # outside coverage
    ('2026-10-27 15:00', '2026-10-27 13:44', False),
])
def test_consecutive_trading_minutes_only_bridge_verified_session_breaks(previous, current, expected):
    document = calendar_document()
    document['sessions'] = [
        dict(start='2026-10-27 08:45', end='2026-10-27 13:45'),
        dict(start='2026-10-27 15:00', end='2026-10-28 05:00'),
        dict(start='2026-10-28 08:45', end='2026-10-28 13:45'),
        dict(start='2026-10-29 08:45', end='2026-10-29 13:45'),
    ]
    assert mh.SessionCalendar(document).consecutive_trading_minutes(previous, current) is expected


def test_unavailable_calendar_cannot_certify_session_break():
    assert not mh.SessionCalendar().consecutive_trading_minutes('2026-10-27 13:44', '2026-10-27 15:00')


def test_gap_is_not_hidden_by_a_fresh_tail_or_no_volume():
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars("2026-10-27 19:00", "2026-10-27 19:09")
    frame = frame[frame.minute_start != pd.Timestamp("2026-10-27 19:03")]
    result = mh.validate_bars(frame, "2026-10-27 19:10", calendar, since="2026-10-27 19:00")
    assert not result["ready"] and result["missing_count"] == 1
    assert result["missing_sample"] == ["2026-10-27 19:03:00"]
    document = calendar_document()
    document["confirmed_empty_minutes"] = [{"minute_start": "2026-10-27 19:03", "evidence": "fixture broker-confirmed no trades"}]
    result = mh.validate_bars(frame, "2026-10-27 19:10", mh.SessionCalendar(document), since="2026-10-27 19:00")
    assert result["ready"]  # evidence excuses the absence without inventing a bar


def test_live_delay_is_allowed_but_final_minute_must_be_caught_up():
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars("2026-10-28 04:57", "2026-10-28 04:58")
    assert mh.validate_bars(frame, "2026-10-28 04:59:30", calendar, since="2026-10-28 04:57")["ready"]
    result = mh.validate_bars(frame, "2026-10-28 05:01", calendar, since="2026-10-28 04:57")
    assert not result["ready"] and result["missing_sample"] == ["2026-10-28 04:59:00"]
    frame = bars("2026-10-28 04:57", "2026-10-28 04:59")
    assert mh.validate_bars(frame, "2026-10-28 08:00", calendar, since="2026-10-28 04:57")["ready"]


def test_recent_interior_gap_is_not_excused_by_live_edge_delay():
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars('2026-10-27 19:00', '2026-10-27 19:09')
    frame = frame[frame.minute_start != pd.Timestamp('2026-10-27 19:08')]
    result = mh.validate_bars(frame, '2026-10-27 19:10:03', calendar, since='2026-10-27 19:00')
    assert not result['ready']
    assert result['missing_sample'] == ['2026-10-27 19:08:00']


@pytest.mark.parametrize('changes,ready', [
    ({}, True),
    ({'operation_started': '2026-10-27 19:07:59'}, False),
    ({'cycle_completed_at': '2026-10-27 19:06:59'}, False),
    ({'state': 'failed'}, False),
    ({'bars': {'ready': False, 'last_minute_start': '2026-10-27 19:09:00'}}, False),
    ({'bars': {'ready': True, 'last_minute_start': '2026-10-27 19:05:00'}}, False),
])
def test_monitor_ready_uses_bounded_completed_result_during_update(tmp_path, monkeypatch, changes, ready):
    path = tmp_path / 'market.db'
    document = dict(db_path=str(path.resolve()), code='TMFR1', mode='simulation',
        state='healthy', heartbeat_at='2026-10-27 19:10:00',
        cycle_completed_at='2026-10-27 19:09:59', operation='derive',
        operation_started='2026-10-27 19:10:00',
        bars=dict(ready=True, last_minute_start='2026-10-27 19:09:00'))
    document.update(changes)
    mh.atomic_json(mh.health_path(path, 'monitor', 'TMFR1'), document)
    monkeypatch.setattr(mh.SessionCalendar, 'load', lambda path: mh.SessionCalendar(calendar_document()))
    assert mh.monitor_ready(path, 'TMFR1', '2026-10-27 19:10:00', mh.HealthSettings(), mode='simulation')[0] is ready


def test_lag_uses_seconds_and_opening_grace_does_not_certify_old_data():
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars("2026-10-27 19:00", "2026-10-27 19:06")
    result = mh.validate_bars(frame, "2026-10-27 19:10:30", calendar, since="2026-10-27 19:00")
    assert not result["ready"] and result["lag_seconds"] == 210
    result = mh.validate_bars(pd.DataFrame(columns=["minute_start"]), "2026-10-27 15:00:30", calendar, since="2026-10-27 15:00")
    assert not result["ready"] and result["opening_grace"]


@pytest.mark.parametrize('lag', [180, 90.5])
def test_cached_validation_matches_uncached_across_time_boundaries(lag):
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars('2026-10-28 04:53', '2026-10-28 04:57')
    cache = mh.BarValidationCache()
    # Includes lag expiry inside a minute, new closed minutes, session end,
    # backward clock movement, a corrected frame and a changed calendar.
    for stamp in ['04:58:00', '04:59:30', '04:59:30.500', '04:59:31',
                  '04:59:59', '05:00:00', '05:01:00', '04:58:00']:
        now = pd.Timestamp(f'2026-10-28 {stamp}')
        options = dict(since='2026-10-28 04:53', lag_seconds=lag,
                       pending_repair_minutes=[pd.Timestamp('2026-10-28 04:58')])
        assert cache.validate(frame, now, calendar, **options) == mh.validate_bars(frame, now, calendar, **options)
    for updated_frame, updated_calendar in [(bars('2026-10-28 04:53', '2026-10-28 04:59'), calendar),
                                             (frame, mh.SessionCalendar())]:
        assert cache.validate(updated_frame, now, updated_calendar, **options) == mh.validate_bars(updated_frame, now, updated_calendar, **options)


def test_validation_cache_reuses_history_but_updates_lag_each_second(monkeypatch):
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars('2026-10-27 19:00', '2026-10-27 19:06')
    cache = mh.BarValidationCache()
    calls = []
    original = calendar.expected_minutes
    def expected(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(calendar, 'expected_minutes', expected)
    results = [cache.validate(frame, f'2026-10-27 19:09:{second:02}', calendar, since='2026-10-27 19:00')
               for second in range(10)]
    assert len(calls) == 1
    assert results[0]['lag_seconds'] == 120 and results[-1]['lag_seconds'] == 129


def test_unknown_or_expired_calendar_never_certifies_recovery():
    frame = bars("2026-10-27 19:00", "2026-10-27 19:09")
    assert not mh.validate_bars(frame, "2026-10-27 19:10", mh.SessionCalendar(), since="2026-10-27 19:00")["ready"]
    calendar = mh.SessionCalendar(calendar_document())
    assert not calendar.covered("2026-10-30")
    unknown = mh.SessionCalendar()
    assert unknown.active("2026-10-31 04:00")  # Friday night extends to Saturday
    assert not unknown.active("2026-11-01 19:00")


def test_bad_and_unreviewed_calendars_fail_closed(tmp_path):
    example = mh.ROOT / "config" / "market_calendar.example.json"
    assert mh.SessionCalendar.load(example).error
    document = calendar_document()
    document["sessions"] *= 2
    with pytest.raises(ValueError, match="overlapping"):
        mh.SessionCalendar(document)
    path = tmp_path / "calendar.json"
    path.write_text("{broken")
    assert mh.SessionCalendar.load(path).error


def test_lock_and_atomic_health_identity_survive_process_replacement(tmp_path):
    now = pd.Timestamp("2026-10-27 19:10")
    first = mh.HealthPublisher(tmp_path / "market.db", "monitor", "TMFR1", "simulation", clock=lambda: now)
    second = mh.HealthPublisher(tmp_path / "market.db", "monitor", "TMFR1", "simulation", clock=lambda: now)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another monitor"):
            second.acquire()
        first.publish(state="degraded", operation="kbars", operation_started=mh.iso(now))
        stored = mh.read_health(first.path)
        assert stored["pid"] and stored["source_hashes"]["monitor.py"]
        assert stored["db_path"] == str((tmp_path / "market.db").resolve())
    finally:
        first.close()
    second.acquire()
    try:
        assert mh.read_health(second.path)["instance"] != stored["instance"]
    finally:
        second.close()


def test_abrupt_process_death_releases_service_lease(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "market.db"
    code = "import sys, os; sys.path.insert(0, sys.argv[1]); import market_health as mh; p=mh.HealthPublisher(sys.argv[2], 'monitor', 'TMFR1', 'simulation'); p.acquire(); os._exit(9)"
    result = subprocess.run([sys.executable, "-c", code, str(mh.ROOT / "src"), str(path)], capture_output=True, timeout=10)
    assert result.returncode == 9
    replacement = mh.HealthPublisher(path, "monitor", "TMFR1", "simulation")
    replacement.acquire()
    replacement.close()


def test_settings_are_strict_and_resolve_relative_calendar(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"calendar_path": "calendar.json"}))
    assert mh.load_settings(path).calendar_path == str(tmp_path / "calendar.json")
    for document in ({"unknown": True}, {"api_timeout_ms": True}, {"recovery_initial_seconds": 0},
                     {"trading_reconnect_enabled": "false"}, {"tick_stale_seconds": float("nan")}):
        path.write_text(json.dumps(document))
        with pytest.raises(ValueError):
            mh.load_settings(path)


def test_live_book_checks_both_receipt_and_exchange_time_and_old_callbacks():
    from types import SimpleNamespace as NS
    from orderbook import LiveBook
    callbacks = []
    api = NS(set_on_bidask_fop_v1_callback=callbacks.append,
             quote=NS(subscribe=lambda *a, **k: None, unsubscribe=lambda *a, **k: None))
    now, mono = [pd.Timestamp("2026-10-27 19:10")], [0.]
    book = LiveBook(api, NS(code="TMFR1"), stale_seconds=10, clock=lambda: now[0], monotonic=lambda: mono[0])
    quote = NS(datetime=pd.Timestamp("2026-10-27 11:10", tz="UTC"), bid_price=[100], ask_price=[101],
               bid_volume=[1], ask_volume=[1], diff_bid_vol=[], diff_ask_vol=[], bid_total_vol=1,
               ask_total_vol=1, underlying_price=0, simtrade=False)
    assert book.start() and not book.alive
    callbacks[0](quote)
    assert book.alive
    mono[0] += 11
    assert not book.alive  # frozen wall clock cannot hide stopped receipt
    book.stop()
    assert book.start() and not book.alive
    callbacks[0](quote)
    assert not book.alive
    callbacks[1](quote)
    assert book.alive
    now[0] += pd.Timedelta(seconds=11)
    callbacks[1](quote)
    assert not book.alive  # repeated delivery of old exchange timestamps is stale


def test_health_and_notepad_calendar_use_explicit_utf8(tmp_path):
    path = tmp_path / "health.json"
    document = {"state": "行情恢復", "detail": "✅"}
    mh.atomic_json(path, document)
    assert json.loads(path.read_bytes().decode("utf-8")) == document
    assert mh.read_health(path) == document
    calendar = calendar_document()
    calendar["source"] = "期交所公告核對 ✅"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(calendar, ensure_ascii=False).encode("utf-8"))
    assert mh.SessionCalendar.load(path).source == calendar["source"]


def test_windows_brief_sharing_conflict_retries_atomic_replace(monkeypatch, tmp_path):
    monkeypatch.setattr(mh, "IS_WINDOWS", True)
    sleeps, attempts = [], []
    original = mh.os.replace
    def replace(source, destination):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("snapshot briefly open in another process")
        return original(source, destination)
    monkeypatch.setattr(mh.os, "replace", replace)
    monkeypatch.setattr(mh.time, "sleep", sleeps.append)
    path = tmp_path / "state.json"
    mh.atomic_json(path, {"state": "healthy"})
    assert mh.read_health(path) == {"state": "healthy"}
    assert sleeps == [0.01, 0.02] and len(attempts) == 3


def test_windows_permanent_replace_denial_is_bounded_and_keeps_old_snapshot(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    mh.atomic_json(path, {"state": "old"})
    attempts, sleeps = [], []
    monkeypatch.setattr(mh, "IS_WINDOWS", True)
    def denied(*_):
        attempts.append(1)
        raise PermissionError("permanently read-only")
    monkeypatch.setattr(mh.os, "replace", denied)
    monkeypatch.setattr(mh.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError):
        mh.atomic_json(path, {"state": "new"})
    assert len(attempts) == 4 and sleeps == [0.01, 0.02, 0.04]
    assert mh.read_health(path) == {"state": "old"}
    assert list(tmp_path.iterdir()) == [path]
