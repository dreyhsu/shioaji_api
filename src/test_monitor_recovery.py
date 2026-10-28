from contextlib import contextmanager
from types import SimpleNamespace as NS
import sqlite3
import pandas as pd
import pytest
import db
import monitor
import market_health as mh
import session_bars as sb
from test_market_health import calendar_document

pytestmark = pytest.mark.integration


def frame(start, end):
    stamps = pd.date_range(start, end, freq="min")
    return sb.add_sessions(pd.DataFrame({"minute_start": stamps, "open": 20000., "high": 20001.,
                                        "low": 19999., "close": 20000., "volume": 1, "amount": 0}))


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    now, mono, transports = [pd.Timestamp("2026-10-27 19:07")], [0.], []
    path = tmp_path / "market.db"
    conn = db.connect(path)
    db.init_schema(conn)
    db.upsert_minute_bars(conn, "TMFR1", frame("2026-10-27 19:03", "2026-10-27 19:04"))
    @contextmanager
    def factory():
        contract = NS(code="TMFR1")
        api = NS(Contracts=NS(Futures=NS(TMF={"TMFR1": contract})), closed=False,
                 usage=lambda **_: NS(bytes=0, limit_bytes=100))
        api.set_session_down_callback = lambda callback: setattr(api, "down", callback)
        transports.append(api)
        try:
            yield api
        finally:
            api.closed = True
    fresh = [frame("2026-10-27 19:03", "2026-10-27 19:06")]
    requests = []
    def fetch(*args, **kwargs):
        requests.append((args, kwargs))
        if isinstance(fresh[0], Exception):
            raise fresh[0]
        return fresh[0]
    monkeypatch.setattr(monitor.sc, "fetch_minute_bars", fetch)
    monkeypatch.setattr(monitor.sc, "fetch_latest_minute_bars", fetch)
    monkeypatch.setattr(monitor.market_clock, "now", lambda: now[0])
    config = monitor.MonitorConfig(db_path=path, enable_orderbook=False, enable_telegram=False,
                                   alerts_config=tmp_path / "alerts.json")
    item = monitor.MonitorRuntime(conn, config, settings=mh.HealthSettings(),
                                  calendar=mh.SessionCalendar(calendar_document()), connect_factory=factory,
                                  clock=lambda: now[0], monotonic=lambda: mono[0])
    yield NS(item=item, conn=conn, now=now, mono=mono, apis=transports, fresh=fresh, requests=requests)
    item.close()
    conn.close()


def test_exact_session_failure_rebuilds_and_verifies_progress(runtime):
    r = runtime
    r.fresh[0] = RuntimeError("code: NotReady, SubCode(SessionNotEstablished)")
    r.item.step()
    assert r.item.gate.pending and len(r.apis) == 1
    r.item.step()
    assert len(r.apis) == 1  # backoff is enforced
    r.mono[0] += 6
    r.fresh[0] = frame("2026-10-27 19:03", "2026-10-27 19:06")
    r.item.step()
    assert r.apis[0].closed and len(r.apis) == 2
    document = mh.read_health(r.item.publisher.path)
    assert document["state"] == "healthy" and document["bars"]["missing_count"] == 0
    r.apis[0].down()
    assert not r.item.down.is_set()  # obsolete session callback cannot poison new session
    assert db.last_minute_start(r.conn, "TMFR1") == pd.Timestamp("2026-10-27 19:06")


def test_sqlite_failure_does_not_relogin(runtime, monkeypatch):
    r = runtime
    original = db.upsert_minute_bars
    def locked(*_):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "upsert_minute_bars", locked)
    r.item.step()
    assert not r.item.gate.pending and len(r.apis) == 1
    assert mh.read_health(r.item.publisher.path)["error"]["kind"] == "database"
    monkeypatch.setattr(db, "upsert_minute_bars", original)
    r.item.step()
    assert len(r.apis) == 1 and mh.read_health(r.item.publisher.path)["state"] == "healthy"


def test_successful_rpc_with_stalled_data_is_not_healthy(runtime):
    r = runtime
    r.now[0] = pd.Timestamp("2026-10-27 19:15")
    r.item.step()
    r.item.step()
    assert r.item.gate.pending
    assert mh.read_health(r.item.publisher.path)["state"] != "healthy"


def test_interior_gap_refetches_without_replacing_healthy_session(runtime):
    r = runtime
    r.now[0] = pd.Timestamp("2026-10-27 19:10")
    r.fresh[0] = frame("2026-10-27 19:03", "2026-10-27 19:09")
    r.fresh[0] = r.fresh[0][r.fresh[0].minute_start != pd.Timestamp("2026-10-27 19:05")]
    r.item.step()
    assert r.item.needs_backfill and not r.item.gate.pending
    assert mh.read_health(r.item.publisher.path)["bars"]["missing_count"] == 1
    r.fresh[0] = frame("2026-10-27 19:03", "2026-10-27 19:09")
    r.item.step()
    assert len(r.apis) == 1 and mh.read_health(r.item.publisher.path)["state"] == "healthy"


def test_final_0459_bar_is_fetched_after_0500_and_not_duplicated(runtime):
    r = runtime
    r.item.since = pd.Timestamp("2026-10-28 04:57")
    r.now[0] = pd.Timestamp("2026-10-28 05:01")
    r.fresh[0] = frame("2026-10-28 04:57", "2026-10-28 04:58")
    r.item.step()
    assert mh.read_health(r.item.publisher.path)["state"] == "degraded"
    r.fresh[0] = frame("2026-10-28 04:57", "2026-10-28 04:59")
    r.item.step()
    assert mh.read_health(r.item.publisher.path)["state"] == "healthy"
    assert db.last_minute_start(r.conn, "TMFR1") == pd.Timestamp("2026-10-28 04:59")
    count = len(db.load_minute_bars(r.conn, "TMFR1"))
    r.item.step()
    assert len(db.load_minute_bars(r.conn, "TMFR1")) == count


def test_partial_backfill_checkpoint_survives_restart(runtime):
    r = runtime
    r.now[0] = pd.Timestamp("2026-10-27 19:10")
    r.fresh[0] = frame("2026-10-27 19:03", "2026-10-27 19:09")
    r.fresh[0] = r.fresh[0][r.fresh[0].minute_start != pd.Timestamp("2026-10-27 19:05")]
    r.item.step()
    factory, config = r.item.connect_factory, r.item.config
    r.item.close()
    r.item = monitor.MonitorRuntime(r.conn, config, settings=mh.HealthSettings(),
                                    calendar=mh.SessionCalendar(calendar_document()), connect_factory=factory,
                                    clock=lambda: r.now[0], monotonic=lambda: r.mono[0])
    r.item.step()
    assert r.item.since == pd.Timestamp("2026-10-27 19:03")
    assert mh.read_health(r.item.publisher.path)["bars"]["missing_count"] == 1
    assert mh.read_health(r.item.publisher.path)["state"] == "degraded"


def test_repeated_relogin_without_data_recovery_keeps_backoff(runtime):
    r = runtime
    r.fresh[0] = RuntimeError("NotReady SessionNotEstablished")
    r.item.gate.jitter = lambda: 1
    r.item.step()
    assert r.item.gate.next_attempt == 5
    for delay in (10, 20, 40, 80, 120, 600, 600):
        r.mono[0] = r.item.gate.next_attempt
        r.item.step()
        assert r.item.gate.next_attempt - r.mono[0] == delay


def test_quota_error_waits_without_relogin(runtime):
    r = runtime
    r.fresh[0] = RuntimeError("QuotaExceeded")
    r.item.step()
    assert mh.read_health(r.item.publisher.path)["error"]["kind"] == "quota"
    assert not r.item.gate.pending
    r.mono[0] += 300
    r.item.step()
    assert len(r.requests) == 1 and len(r.apis) == 1
    r.mono[0] += 301
    r.fresh[0] = frame("2026-10-27 19:03", "2026-10-27 19:06")
    r.item.step()
    assert len(r.apis) == 1 and mh.read_health(r.item.publisher.path)["state"] == "healthy"
