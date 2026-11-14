"""Exercise real worker scheduling with gated HTTP fakes and temporary SQLite."""
import json
import threading
import time

import pytest

import alert_rules as ar
import db
import monitor
import monitor_notify
from monitor_notify import MonitorNotifier


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate(), 'background worker did not complete'


@pytest.fixture
def delivery(tmp_path):
    path = tmp_path / 'monitor.db'
    alerts_path = tmp_path / 'alerts.json'
    alerts_path.write_text('{"telegram_enabled": true}', encoding='utf-8')
    conn = db.connect(path)
    db.init_schema(conn)
    workers, gates = [], []

    def create(send, **kwargs):
        worker = MonitorNotifier(path, alerts_path, send=send, logger=logs.append, **kwargs)
        workers.append(worker)
        return worker

    def gate():
        event = threading.Event()
        gates.append(event)
        return event

    def claim(worker, key):
        return monitor.send_rule(conn, config, alerts, ar.Candidate('trendline_break_below_up', key, True, key),
                                 '2026-11-13 09:00:00', notifier=worker)

    logs = []
    config = monitor.MonitorConfig(db_path=path, alerts_config=alerts_path, enable_telegram=True)
    alerts = ar.AlertConfig(alerts_path)
    from types import SimpleNamespace
    yield SimpleNamespace(conn=conn, path=path, alerts_path=alerts_path, create=create,
                          claim=claim, gate=gate, config=config, alerts=alerts, logs=logs)
    for event in gates:
        event.set()
    for worker in workers:
        worker.close(timeout=3)
        assert not worker.snapshot()['worker_alive']
    conn.close()


def test_blocked_delivery_does_not_hold_monitor_connection_and_uses_one_worker(delivery):
    d = delivery
    started, release = d.gate(), d.gate()
    calls = []
    def send(message):
        calls.append((message, threading.get_ident()))
        started.set()
        assert release.wait(5)
        return True
    worker = d.create(send)
    assert d.claim(worker, 'first')
    assert started.wait(3)
    assert d.claim(worker, 'second')
    assert not d.claim(worker, 'first')  # persisted claim deduplicates pending HTTP
    # Same monitor connection remains usable and the worker holds no write lock.
    d.conn.execute('BEGIN IMMEDIATE')
    d.conn.execute("INSERT INTO market_data_versions VALUES ('UNRELATED', 1)")
    d.conn.commit()
    assert worker.snapshot()['in_flight'] is not None
    assert len(calls) == 1
    assert all(row['ok'] is None for row in d.conn.execute('SELECT ok FROM telegram_alerts'))
    release.set()
    eventually(lambda: worker.snapshot()['sent'] == 2)
    assert len({ident for _, ident in calls}) == 1
    assert calls[0][1] != threading.get_ident()
    assert all(row['ok'] == 1 for row in d.conn.execute('SELECT ok FROM telegram_alerts'))


def test_queue_full_is_recorded_without_blocking_or_replaying(delivery):
    d = delivery
    started, release = d.gate(), d.gate()
    calls = []
    def send(message):
        calls.append(message)
        started.set()
        assert release.wait(5)
        return True
    worker = d.create(send, capacity=1)
    assert d.claim(worker, 'first')
    assert started.wait(3)
    assert d.claim(worker, 'second')
    assert not d.claim(worker, 'overflow')
    assert worker.snapshot()['queued'] == 1
    assert worker.snapshot()['last_error'] == 'queue_full'
    assert d.conn.execute("SELECT ok FROM telegram_alerts WHERE box_type='overflow'").fetchone()[0] == 0
    release.set()
    eventually(lambda: worker.snapshot()['sent'] == 2)
    worker.close()
    restarted = d.create(lambda message: calls.append(message) or True)
    assert not d.claim(restarted, 'overflow')
    assert calls == ['first', 'second']


@pytest.mark.parametrize('result', [False, RuntimeError('secret-in-exception')])
def test_transport_failure_is_persisted_and_does_not_kill_worker(delivery, result):
    d = delivery
    def send(message):
        if message == 'fail':
            if isinstance(result, Exception):
                raise result
            return result
        return True
    worker = d.create(send)
    assert d.claim(worker, 'fail')
    assert d.claim(worker, 'next')
    eventually(lambda: worker.snapshot()['failed'] == worker.snapshot()['sent'] == 1)
    assert worker.snapshot()['worker_alive']
    assert not d.claim(worker, 'fail')
    assert [r[0] for r in d.conn.execute('SELECT ok FROM telegram_alerts ORDER BY id')] == [0, 1]
    assert all('secret-in-exception' not in line for line in d.logs)


def test_pause_invalidates_old_queue_even_after_resume(delivery):
    d = delivery
    started, release = d.gate(), d.gate()
    calls = []
    def send(message):
        calls.append(message)
        started.set()
        assert release.wait(5)
        return True
    worker = d.create(send)
    assert d.claim(worker, 'in-flight')
    assert started.wait(3)
    assert d.claim(worker, 'old-queue')
    worker.set_enabled(False)
    d.alerts.data['telegram_enabled'] = False
    assert not d.claim(worker, 'paused')
    assert d.conn.execute("SELECT COUNT(*) FROM telegram_alerts WHERE box_type='paused'").fetchone()[0] == 0
    worker.set_enabled(True)
    d.alerts.data['telegram_enabled'] = True
    assert d.claim(worker, 'after-resume')
    release.set()
    eventually(lambda: worker.snapshot()['sent'] == 2 and worker.snapshot()['skipped'] == 1)
    assert calls == ['in-flight', 'after-resume']


def test_worker_checks_master_switch_before_sending_queued_alerts(delivery):
    d = delivery
    started, release = d.gate(), d.gate()
    calls = []
    def send(message):
        calls.append(message)
        started.set()
        assert release.wait(5)
        return True
    worker = d.create(send)
    assert d.claim(worker, 'in-flight')
    assert started.wait(3)
    assert d.claim(worker, 'pending')
    d.alerts_path.write_text(json.dumps({'telegram_enabled': False}), encoding='utf-8')
    release.set()
    eventually(lambda: worker.snapshot()['skipped'] == 1)
    assert calls == ['in-flight']


def test_shutdown_is_bounded_drops_queue_and_never_replays_unconfirmed_jobs(delivery):
    d = delivery
    started, release = d.gate(), d.gate()
    calls = []
    def send(message):
        calls.append(message)
        started.set()
        assert release.wait(5)
        return True
    worker = d.create(send)
    assert d.claim(worker, 'in-flight')
    assert started.wait(3)
    assert d.claim(worker, 'pending')
    assert not worker.close(timeout=0)  # no wait for blocked HTTP
    assert worker.snapshot()['last_error'] == 'shutdown_delivery_unconfirmed'
    assert d.conn.execute("SELECT ok FROM telegram_alerts WHERE box_type='in-flight'").fetchone()[0] is None
    restarted = d.create(lambda message: calls.append(message) or True)
    assert not d.claim(restarted, 'in-flight')
    assert not d.claim(restarted, 'pending')
    release.set()
    eventually(lambda: not worker.snapshot()['worker_alive'])
    assert d.conn.execute("SELECT ok FROM telegram_alerts WHERE box_type='pending'").fetchone()[0] == 0
    assert calls == ['in-flight']


def test_delivery_status_storage_failure_is_visible_without_repeating_http(delivery, monkeypatch):
    d = delivery
    original = monitor_notify.db.session
    def session(path):
        if threading.current_thread().name == 'monitor-telegram':
            raise OSError('sensitive-db-error')
        return original(path)
    monkeypatch.setattr(monitor_notify.db, 'session', session)
    calls = []
    worker = d.create(lambda message: calls.append(message) or True)
    assert d.claim(worker, 'once')
    eventually(lambda: worker.snapshot()['last_error'] == 'delivery_status_OSError')
    assert worker.snapshot()['worker_alive']
    assert d.conn.execute('SELECT ok FROM telegram_alerts').fetchone()[0] is None
    assert not d.claim(worker, 'once')
    assert calls == ['once']
    assert all('sensitive-db-error' not in line for line in d.logs)


def test_claim_storage_failure_does_not_escape_into_market_loop(delivery, monkeypatch):
    d = delivery
    worker = d.create(lambda message: True)
    monkeypatch.setattr(db, 'record_alert', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('secret')))
    assert not d.claim(worker, 'failed-claim')
    assert worker.snapshot()['last_error'] == 'notification_claim_OSError'


def test_missing_worker_never_falls_back_to_synchronous_http(delivery, monkeypatch):
    d = delivery
    monkeypatch.setattr(monitor_notify.tg, 'send_message', lambda message: pytest.fail('inline HTTP forbidden'))
    assert not monitor.send_rule(d.conn, d.config, d.alerts, ar.Candidate('box_enter', 'missing', False, 'm'),
                                 '2026-11-13 09:00:00')
    assert d.conn.execute('SELECT ok FROM telegram_alerts').fetchone()[0] == 0
