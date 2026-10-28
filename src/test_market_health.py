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


def test_lag_uses_seconds_and_opening_grace_does_not_certify_old_data():
    calendar = mh.SessionCalendar(calendar_document())
    frame = bars("2026-10-27 19:00", "2026-10-27 19:06")
    result = mh.validate_bars(frame, "2026-10-27 19:10:30", calendar, since="2026-10-27 19:00")
    assert not result["ready"] and result["lag_seconds"] == 210
    result = mh.validate_bars(pd.DataFrame(columns=["minute_start"]), "2026-10-27 15:00:30", calendar, since="2026-10-27 15:00")
    assert not result["ready"] and result["opening_grace"]


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
