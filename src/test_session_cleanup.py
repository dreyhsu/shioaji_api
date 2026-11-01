"""Never log in merely to look for sessions; let the old owner logout itself."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from file_lease import FileLease
from service_control import (ServiceControl, cleanup_previous, control_folder, read_owner,
                             request_stop)
from test_market_fetchers import rig

pytestmark = pytest.mark.integration


@pytest.fixture
def controls(tmp_path):
    config = NS(acquisition_db=tmp_path / 'fetch.db', unified_db=tmp_path / 'unified.db')
    owners = []
    def owner(role, *, broker=True, active=True, db=None):
        control = ServiceControl(db or config.acquisition_db, role, 'simulation', broker=broker).start()
        owners.append(control)
        if broker and active:
            control.session('login_started')
            control.session('login_succeeded')
        return control
    yield NS(config=config, owner=owner)
    for control in owners:
        control.close()


def test_cleanup_waits_for_old_owner_logout_and_release_before_new_login(controls):
    r = controls
    tick = r.owner('tick-fetcher')
    trade = r.owner('trading-TMFR1', db=r.config.unified_db)
    parent = r.owner('launcher', broker=False, db=r.config.unified_db)
    trace = []
    def old_workers_poll(_):
        for control in (tick, trade, parent):
            if control.stop_requested and control.started:
                if control.document['broker']:
                    control.session('logout_started')
                    trace.append(control.document['role'] + ':logout')
                    control.session('logout_succeeded')
                control.close()
    result = cleanup_previous(r.config, wait=old_workers_poll)
    trace.append('new:login')
    assert len(result) == 2
    assert trace[-1] == 'new:login' and len(trace) == 3
    assert all(record['session_state'] == 'logged_out' for record in result)
    assert read_owner(parent.path)['state'] == 'stopped'


def test_dead_owner_with_unacknowledged_session_blocks_without_creating_api(controls):
    r = controls
    control = r.owner('tick-fetcher')
    control.lease.close()  # crash: no close or logout acknowledgement
    with pytest.raises(RuntimeError, match='登出未確認'):
        cleanup_previous(r.config)
    assert read_owner(control.path)['session_state'] == 'active'
    with pytest.raises(RuntimeError, match='不得新增登入'):
        ServiceControl(r.config.acquisition_db, 'tick-fetcher', 'simulation', broker=True).start()


def test_failed_logout_does_not_unlock_startup(controls):
    r = controls
    control = r.owner('tick-fetcher')
    def worker(_):
        control.session('logout_failed')
        control.close()
    with pytest.raises(RuntimeError, match='登出未確認'):
        cleanup_previous(r.config, wait=worker)
    record = read_owner(control.path)
    assert record['state'] == 'stopped' and record['logout_unconfirmed']


def test_same_owner_cannot_relogin_after_unconfirmed_logout(controls):
    control = controls.owner('tick-fetcher')
    with pytest.raises(RuntimeError, match='不得新增登入'):
        control.session('login_started')
    control.session('logout_failed')
    # A later event cannot erase the uncertainty of an earlier failed logout.
    control.session('logout_succeeded')
    with pytest.raises(RuntimeError, match='不得新增登入'):
        control.session('login_started')


def test_cleanup_timeout_leaves_old_worker_alive_and_never_force_kills(controls, monkeypatch):
    import os
    r = controls
    control = r.owner('tick-fetcher')
    monkeypatch.setattr(os, 'kill', lambda *args: pytest.fail('no PID signals allowed during cleanup'))
    monkeypatch.setattr(os, 'killpg', lambda *args: pytest.fail('no force termination during cleanup'), raising=False)
    clock = [0.]
    def wait(seconds):
        clock[0] += seconds
    with pytest.raises(RuntimeError, match='不強制終止、不新增登入'):
        cleanup_previous(r.config, timeout=.2, clock=lambda: clock[0], wait=wait)
    assert control.started and control.stop_requested


def test_stale_request_cannot_stop_new_instance(controls):
    r = controls
    first = r.owner('tick-fetcher', active=False)
    request_stop(first.path, first.document)
    assert first.stop_requested
    first.close()
    second = r.owner('tick-fetcher', active=False)
    assert second.document['instance'] != first.document['instance']
    assert not second.stop_requested


def test_idle_old_broker_worker_is_stopped_without_inventing_logout(controls):
    r = controls
    control = r.owner('tick-fetcher', active=False)
    def worker(_):
        assert control.stop_requested
        control.close()
    result = cleanup_previous(r.config, wait=worker)
    assert result[0]['session_state'] == 'none'
    assert 'logout_succeeded_at' not in result[0]


def test_mock_only_launcher_is_not_mistaken_for_broker_session(controls):
    r = controls
    parent = r.owner('launcher', broker=False, db=r.config.unified_db)
    assert cleanup_previous(r.config) == []
    assert not parent.stop_requested and parent.started


def test_legacy_running_owner_requires_manual_logout_without_signalling_pid(controls):
    r = controls
    folder = control_folder(r.config.acquisition_db)
    folder.mkdir(parents=True)
    (folder / 'tick-fetcher.json').write_text('{"pid":99999,"state":"running"}')
    with FileLease(folder / 'tick-fetcher.lock'):
        with pytest.raises(RuntimeError, match='舊版服務'):
            cleanup_previous(r.config)


def test_legacy_journal_needs_matching_logout_success(controls):
    r = controls
    folder = control_folder(r.config.acquisition_db)
    folder.mkdir(parents=True)
    (folder / 'tick-fetcher.json').write_text(json.dumps(dict(db_path=str(r.config.acquisition_db), state='stopped')))
    logs = r.config.acquisition_db.parent / 'logs'
    logs.mkdir()
    journal = logs / 'tick_events.jsonl'
    record = dict(instance='old', generation=1, event='login_succeeded')
    journal.write_text(json.dumps(record) + '\n')
    with pytest.raises(RuntimeError, match='登出未確認'):
        cleanup_previous(r.config)
    with journal.open('a') as output:
        output.write(json.dumps({**record, 'event': 'logout_succeeded'}) + '\n')
    assert cleanup_previous(r.config) == []


def test_usage_query_reuses_only_existing_api_and_does_not_replace_logout(controls):
    control = controls.owner('tick-fetcher')
    calls = []
    existing_api = NS(usage=lambda **kwargs: calls.append('usage') or NS(connections=4))
    control.sample_usage(existing_api)
    assert calls == ['usage']
    assert read_owner(control.path)['connections_before_logout'] == 4
    assert read_owner(control.path)['session_state'] == 'active'


def test_real_fetcher_cleanup_uses_its_existing_context_logout(rig, tmp_path):
    service = rig.create()
    service.step()
    config = NS(acquisition_db=tmp_path / 'acquisition.db', unified_db=tmp_path / 'unified.db')
    def worker(_):
        if service.control.stop_requested:
            service.close()
    results = cleanup_previous(config, wait=worker)
    assert len(rig.apis) == 1 and rig.apis[0].closed
    assert results[0]['session_state'] == 'logged_out'


def test_login_intent_must_be_durable_before_sdk_login(monkeypatch):
    import shioaji_client as sc
    calls = []
    monkeypatch.setattr(sc, 'load_dotenv', lambda *args: None)
    monkeypatch.setenv('SHIOAJI_API_KEY', 'offline')
    monkeypatch.setenv('SHIOAJI_SECRET_KEY', 'offline')
    api = NS(login=lambda **kwargs: calls.append('login'), logout=lambda: calls.append('logout') or True)
    monkeypatch.setattr(sc.sj, 'Shioaji', lambda **kwargs: api)
    def observer(event):
        if event == 'login_started':
            raise OSError('cannot persist intent')
    with pytest.raises(OSError):
        with sc.connect(simulation=True, strict_logout=True, session_observer=observer):
            pass
    assert calls == ['logout']
