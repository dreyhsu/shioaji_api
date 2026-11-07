"""Configuration and supervisor lifecycle without broker/network access."""
import json
import errno
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from dashboard_config import DashboardConfig
from file_lease import LeaseUnavailable
from runtime_config import RuntimeConfig
from service_launcher import Supervisor, service_plan

pytestmark = pytest.mark.integration


def config_file(tmp_path, **values):
    values = {'mode': 'mock', 'unified_db': str(tmp_path / 'unified.db'),
              'acquisition_db': str(tmp_path / 'fetch.db'), **values}
    path = tmp_path / 'runtime.json'
    path.write_text(json.dumps(values), encoding='utf-8-sig')
    return path


def test_runtime_defaults_and_relative_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    config = RuntimeConfig.load(path)
    assert config.mode == 'mock'
    assert config.dashboard_config == tmp_path / 'dashboard.json'
    assert config.unified_db == tmp_path / 'unified.db'


@pytest.mark.parametrize('values', [dict(mode='paper'), dict(mode='production'),
    dict(contracts=['TMFR1', 'TMFR1']), dict(contracts=['bad/code']), dict(trade_port=True),
    dict(mode='simulation', contracts=['TMFR1', 'TMFR2']), dict(unknown=True)])
def test_bad_runtime_configuration_is_rejected(tmp_path, values):
    with pytest.raises(ValidationError):
        RuntimeConfig.load(config_file(tmp_path, **values))


@pytest.mark.parametrize('values', [dict(default_timeframe='2h'), dict(initial_bars=0),
    dict(history_days=366), dict(refresh_seconds=0), dict(curve_hover_pixels=0), dict(port=True)])
def test_bad_dashboard_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        DashboardConfig.model_validate(values)


@pytest.mark.parametrize('platform', ['posix', 'nt'])
@pytest.mark.parametrize('active_listener', [False, True])
def test_port_check_allows_posix_restart_but_rejects_active_listener(monkeypatch, platform, active_listener):
    import service_launcher as launcher
    options = []
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def setsockopt(self, level, option, value):
            options.append((level, option, value))
        def bind(self, address):
            assert address == ('127.0.0.1', 8765)
            if active_listener or (platform == 'posix' and not options):
                raise OSError(errno.EADDRINUSE, 'address in use')
    monkeypatch.setattr(launcher.socket, 'socket', lambda *args: Probe())
    # Replace only the module's platform view, not Python's shared os module.
    monkeypatch.setattr(launcher, 'os', NS(name=platform))
    if active_listener:
        with pytest.raises(ValueError, match='8765 已被使用'):
            launcher.check_port('127.0.0.1', 8765)
    else:
        launcher.check_port('127.0.0.1', 8765)
    assert bool(options) == (platform == 'posix')


def test_mock_has_only_dashboard_and_live_plan_shares_sessions(tmp_path):
    path = config_file(tmp_path)
    ui = DashboardConfig()
    assert [item.name for item in service_plan(RuntimeConfig.load(path), ui, path)] == ['dashboard']
    path = config_file(tmp_path, mode='simulation', contracts=['TMFR1', 'TMFX6'])
    plan = service_plan(RuntimeConfig.load(path), ui, path)
    assert [item.name for item in plan] == ['tick', 'kbar', 'monitor-TMFR1', 'monitor-TMFX6', 'trade', 'dashboard']
    assert all('--mode' not in item.arguments and '--mock' not in item.arguments for item in plan)


@pytest.mark.parametrize('module', ['tick_fetcher', 'kbar_fetcher', 'monitor', 'trade', 'health_watchdog'])
def test_mock_entrypoints_exit_without_any_broker_or_database(tmp_path, monkeypatch, module, capsys):
    import importlib
    import db
    path = config_file(tmp_path)
    monkeypatch.setattr(db, 'connect', lambda *args: pytest.fail('Mock skip must not access DB'))
    assert importlib.import_module(module).main(['--runtime-config', str(path)]) == 0
    assert 'start_all' in capsys.readouterr().out


class FakeProcess:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def supervisor(tmp_path):
    path = config_file(tmp_path, mode='simulation', contracts=['TMFR1', 'TMFX6'])
    config = RuntimeConfig.load(path)
    children, commands, stops = [], [], []
    clock = [0.]
    def spawn(command, **kwargs):
        child = FakeProcess(1000 + len(children))
        children.append(child)
        commands.append((command, kwargs))
        return child
    def stop(child, force=False):
        stops.append((child.pid, force))
        child.returncode = 0
    def wait(seconds):
        clock[0] += seconds
    owner = Supervisor(config, DashboardConfig(), path, popen=spawn, sender=stop,
                       probe=lambda *args: True, clock=lambda: clock[0], wait=wait, shutdown_timeout=1)
    yield NS(owner=owner, children=children, commands=commands, stops=stops, clock=clock)
    owner.close()


def test_supervisor_starts_in_order_and_stops_only_owned_children(supervisor):
    r = supervisor
    r.owner.start()
    assert len(r.children) == 6
    assert all(options.get('start_new_session') or options.get('creationflags') for _, options in r.commands)
    r.owner.close()
    assert [pid for pid, _ in r.stops] == [1005, 1004, 1003, 1002, 1001, 1000]
    assert json.loads((r.owner.log_dir / 'run.json').read_text())['state'] == 'stopped'


def test_start_failure_cleans_up_previously_started_services(supervisor):
    r = supervisor
    def probe(spec, process, mode):
        if spec.name == 'kbar':
            raise RuntimeError('startup failed')
        return True
    r.owner.probe = probe
    with pytest.raises(RuntimeError):
        r.owner.start()
    assert len(r.children) == 2 and all(child.poll() is not None for child in r.children)


def test_duplicate_launcher_does_not_stop_existing_services(supervisor):
    r = supervisor
    r.owner.start()
    duplicate = Supervisor(r.owner.config, r.owner.ui, r.owner.path)
    with pytest.raises(LeaseUnavailable):
        duplicate.start()
    assert not r.stops
    assert all(child.poll() is None for child in r.children)


def test_forced_stop_marks_logout_unconfirmed(supervisor):
    r = supervisor
    r.owner.start()
    def delayed(child, force=False):
        r.stops.append((child.pid, force))
        if force:
            child.returncode = -9
    r.owner.sender = delayed
    r.owner.close()
    report = json.loads((r.owner.log_dir / 'run.json').read_text())
    assert report['logout_unconfirmed'] and report['forced_pids'] == list(range(1005, 999, -1))


def test_cooperative_cleanup_never_signals_or_forces_old_children(supervisor):
    from service_control import request_stop
    import subprocess
    r = supervisor
    r.owner.start()
    request_stop(r.owner.control.path, r.owner.control.document)
    def pending(timeout=None):
        raise subprocess.TimeoutExpired('offline fake', timeout)
    for child in r.children:
        child.wait = pending
    r.owner.close()
    report = json.loads((r.owner.log_dir / 'run.json').read_text())
    assert not r.stops
    assert not report['forced_pids'] and report['logout_unconfirmed']
    assert report['pending_pids'] == list(range(1005, 999, -1))


def test_cooperative_launcher_propagates_stop_only_to_owned_child(supervisor):
    from service_control import ServiceControl, atomic_json, request_stop
    r = supervisor
    r.owner.start()
    with_owner = ServiceControl(r.owner.config.acquisition_db, 'tick-fetcher', 'simulation', broker=True).start()
    unrelated = ServiceControl(r.owner.config.acquisition_db, 'other', 'simulation', broker=True).start()
    try:
        with_owner.document['pid'] = r.children[0].pid
        atomic_json(with_owner.path, with_owner.document)
        request_stop(r.owner.control.path, r.owner.control.document)
        def poll(seconds):
            assert with_owner.stop_requested and not unrelated.stop_requested
            for child in r.children:
                child.returncode = 0
        r.owner.wait = poll
        r.owner.close()
        assert not r.stops and not unrelated.stop_requested
    finally:
        with_owner.close()
        unrelated.close()


def test_prestart_cleanup_failure_prevents_any_new_service(tmp_path, monkeypatch):
    import service_launcher as launcher
    path = config_file(tmp_path)
    (tmp_path / 'dashboard.json').write_text('{}')
    calls = []
    monkeypatch.setattr(launcher, 'preflight', lambda *args, **kwargs: calls.append(('preflight', kwargs['ports'])))
    def still_running(config):
        calls.append('cleanup')
        raise RuntimeError('舊服務停止逾時')
    monkeypatch.setattr(launcher, 'cleanup_previous', still_running)
    monkeypatch.setattr(launcher.Supervisor, 'start', lambda self: pytest.fail('must not start new services'))
    assert launcher.main(['--runtime-config', str(path), '--no-browser']) == 1
    assert calls == [('preflight', False), 'cleanup']


def test_all_children_use_one_frozen_runtime_snapshot(supervisor):
    r = supervisor
    r.owner.start()
    paths = {command[command.index('--runtime-config') + 1] for command, _ in r.commands}
    assert len(paths) == 1
    snapshot = Path(paths.pop())
    assert snapshot != r.owner.path
    assert json.loads(snapshot.read_text())['mode'] == 'simulation'
    r.owner.path.write_text('{"mode":"mock"}')
    assert json.loads(snapshot.read_text())['mode'] == 'simulation'


def test_readiness_rejects_wrong_pid_or_mode(tmp_path):
    from service_launcher import ready, Service
    path = tmp_path / 'health.json'
    process = FakeProcess(123)
    service = Service('tick', [], health=path)
    path.write_text('{"pid":123,"mode":"production"}')
    assert not ready(service, process, 'simulation')
    path.write_text('{"pid":124,"mode":"simulation"}')
    assert not ready(service, process, 'simulation')
    path.write_text('{"pid":123,"mode":"simulation"}')
    assert ready(service, process, 'simulation')


def test_wrapper_handles_project_and_conda_paths_with_spaces(tmp_path):
    import os
    import shutil
    import subprocess
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / 'project with spaces'
    tools = tmp_path / 'conda with spaces'
    project.mkdir()
    tools.mkdir()
    for name in ('start_all.sh', 'start_all.command'):
        shutil.copy2(root / name, project / name)
    conda = tools / 'conda'
    conda.write_text('#!/bin/bash\nprintf \'conda() { return 0; }\\n\'\n')
    conda.chmod(0o755)
    fake_python = tools / 'python'
    fake_python.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@"\n')
    fake_python.chmod(0o755)
    result = subprocess.run([str(project / 'start_all.command'), '--no-browser'], text=True,
        capture_output=True, timeout=10, env={**os.environ, 'CONDA_EXE': str(conda),
                                            'PATH': str(tools) + os.pathsep + os.environ['PATH']})
    assert result.returncode == 0, result.stderr
    assert str(project / 'src' / 'service_launcher.py') in result.stdout
    assert '--no-browser' in result.stdout
