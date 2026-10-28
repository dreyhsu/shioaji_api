import json
import pandas as pd
import pytest
import db
import market_health as mh
from health_watchdog import IncidentNotifier, inspect_health
from test_market_health import calendar_document
from test_monitor_recovery import frame

pytestmark = pytest.mark.integration


def test_independent_checks_notice_dead_service_stale_tick_and_db_tail(tmp_path):
    now = pd.Timestamp("2026-10-27 19:10")
    path = tmp_path / "market.db"
    with db.session(path) as conn:
        db.upsert_minute_bars(conn, "TMFR1", frame("2026-10-27 19:00", "2026-10-27 19:04"))
    documents = {}
    for role in ("monitor", "trading"):
        documents[role] = {"role": role, "code": "TMFR1", "mode": "simulation", "db_path": str(path.resolve()),
                           "heartbeat_at": "2026-10-27 19:09:59", "state": "healthy", "broker_ready": True,
                           "bars": {"ready": True}, "last_order_sync_at": "2026-10-27 19:09:59"}
    documents["trading"]["quotes"] = {"TMFK6": {"fresh": True, "last_quote_at": "2026-10-27 19:09:00"}}
    documents["monitor"].update(operation="kbars", operation_started="2026-10-27 19:00")
    read = lambda path: documents["monitor" if path.name.startswith("monitor") else "trading"]
    issues = inspect_health(path, "TMFR1", now=now, settings=mh.HealthSettings(),
                            calendar=mh.SessionCalendar(calendar_document()), mode="simulation", read=read)
    assert {"monitor.operation", "trading.tick.TMFK6", "database.bars"} <= issues.keys()
    assert "trading.order_sync" not in issues  # silent callbacks with successful polling are normal
    documents["trading"]["heartbeat_at"] = "2026-10-27 19:00"
    assert "trading.heartbeat" in inspect_health(path, "TMFR1", now=now, settings=mh.HealthSettings(),
                                                calendar=mh.SessionCalendar(calendar_document()), mode="simulation", read=read)


def test_notification_failure_survives_restart_and_recovery_is_sent_once(tmp_path):
    path = tmp_path / "incidents.json"
    settings = mh.HealthSettings(notification_retry_seconds=10)
    messages = []
    notifier = IncidentNotifier(path, settings, send=lambda text: messages.append(text) or False)
    now = pd.Timestamp("2026-10-27 19:10")
    notifier.update({"monitor.bars": "stopped"}, now)
    notifier.update({"monitor.bars": "stopped"}, now + pd.Timedelta(seconds=1))
    assert len(messages) == 1 and mh.read_health(path)["incidents"]["monitor.bars"]["attempts"] == 1
    restarted = IncidentNotifier(path, settings, send=lambda text: messages.append(text) or True)
    restarted.update({"monitor.bars": "stopped"}, now + pd.Timedelta(seconds=11))
    restarted.update({"monitor.bars": "stopped"}, now + pd.Timedelta(seconds=12))
    assert len(messages) == 2
    restarted.update({}, now + pd.Timedelta(seconds=30))
    restarted.update({}, now + pd.Timedelta(seconds=31))
    assert len(messages) == 3 and "恢復" in messages[-1]
    assert len(path.with_suffix(".events.jsonl").read_text().splitlines()) == 2


def test_rest_day_does_not_report_stale_quotes_or_completed_session(tmp_path):
    now = pd.Timestamp("2026-10-28 10:00")  # explicit fixture calendar has no day session
    path = tmp_path / "market.db"
    with db.session(path) as conn:
        db.upsert_minute_bars(conn, "TMFR1", frame("2026-10-28 04:57", "2026-10-28 04:59"))
    docs = {}
    for role in ("monitor", "trading"):
        docs[role] = {"role": role, "code": "TMFR1", "mode": "simulation", "db_path": str(path),
                      "heartbeat_at": str(now), "state": "healthy", "broker_ready": True,
                      "bars": {"ready": True}, "last_order_sync_at": str(now),
                      "quotes": {"TMFK6": {"fresh": False, "last_quote_at": "2026-10-28 04:59"}}}
    issues = inspect_health(path, "TMFR1", now=now, settings=mh.HealthSettings(),
                            calendar=mh.SessionCalendar(calendar_document()), mode="simulation",
                            read=lambda path: docs["monitor" if path.name.startswith("monitor") else "trading"])
    assert issues == {}


def test_many_failed_channels_use_one_bounded_notification(tmp_path):
    sends = []
    notifier = IncidentNotifier(tmp_path / "incidents.json", mh.HealthSettings(), send=lambda text: sends.append(text) or True)
    notifier.update({f"tick.{index}": "stale" for index in range(15)}, pd.Timestamp("2026-10-27 19:10"))
    assert len(sends) == 1 and len(sends[0]) < 4000


def test_opening_wait_does_not_make_a_normal_first_minute_an_outage(tmp_path):
    path = tmp_path / "market.db"
    with db.session(path):
        pass
    now = pd.Timestamp("2026-10-27 15:00:30")
    documents = {}
    for role in ("monitor", "trading"):
        documents[role] = {"role": role, "code": "TMFR1", "db_path": str(path), "mode": "simulation",
                           "state": "waiting_market" if role == "monitor" else "healthy", "heartbeat_at": str(now),
                           "bars": {"ready": False, "opening_grace": True}, "broker_ready": True, "quotes": {}}
    issues = inspect_health(path, "TMFR1", now=now, settings=mh.HealthSettings(),
                            calendar=mh.SessionCalendar(calendar_document()), mode="simulation",
                            read=lambda path: documents["monitor" if path.name.startswith("monitor") else "trading"])
    assert issues == {}
