"""Own and supervise only the services started by this invocation."""
from __future__ import annotations

import argparse
import errno
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.request import build_opener, ProxyHandler
import uuid
import webbrowser

from dashboard_config import DashboardConfig
from file_lease import FileLease
from service_control import ServiceControl, cleanup_previous, control_folder, read_owner, request_stop
from runtime_config import RuntimeConfig, DEFAULT_RUNTIME, ROOT


@dataclass(frozen=True)
class Service:
    name: str
    arguments: list[str]
    health: Path | None = None
    url: str | None = None


def http_root(host, port):
    return f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}'


def service_plan(config, ui, path):
    common = ['--runtime-config', str(Path(path).resolve())]
    dashboard = Service('dashboard', ['src/dashboard.py', *common],
                        url=http_root(ui.host, ui.port) + '/api/ready')
    if config.mode == 'mock':
        return [dashboard]
    folder = config.acquisition_db.parent / '.health' / config.acquisition_db.name
    unified = config.unified_db.parent / '.health' / config.unified_db.name
    return [Service('tick', ['src/tick_fetcher.py', *common], folder / 'tick-fetcher.json'),
            Service('kbar', ['src/kbar_fetcher.py', *common], folder / 'kbar-fetcher.json'),
            *(Service(f'monitor-{code}', ['src/monitor.py', *common, '--code', code],
                       unified / f'monitor-{code}.json') for code in config.contracts),
            Service('trade', ['src/trade.py', *common], url=config.trade_url + '/api/ready'), dashboard]


def check_port(host, port):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        # Match uvicorn's POSIX restart behavior: a closed connection in
        # TIME_WAIT is not a running service. Active listeners still block bind.
        # Windows SO_REUSEADDR can share an active listener, so never use it there.
        if os.name != 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise ValueError(f'{host}:{port} 已被使用，請先停止原服務') from error
            raise OSError(f'無法監聽 {host}:{port}：{type(error).__name__}') from error


def preflight(config, ui, *, ports=True):
    # Complete validation before starting even the first broker-owning child.
    if config.mode != 'mock':
        from market_fetcher import FetcherSettings
        from market_health import load_settings, SessionCalendar
        market = FetcherSettings.load(config.market_config)
        load_settings(config.health_config)
        calendar = SessionCalendar.load(market.calendar_path)
        if calendar.error:
            raise ValueError(calendar.error)
        if config.acquisition_db == config.unified_db:
            raise ValueError('行情與統一資料庫須使用不同路徑')
        if (config.trade_host, config.trade_port) == (ui.host, ui.port):
            raise ValueError('trade 與 Dashboard 不可使用相同 port')
        if ports:
            check_port(config.trade_host, config.trade_port)
    else:
        from mock_config import load_mock_config
        load_mock_config(config.mock_config)
    if ports:
        check_port(ui.host, ui.port)


def ready(service, process, mode):
    if process.poll() is not None:
        raise RuntimeError(f'{service.name} 已結束（exit {process.returncode}）')
    try:
        if service.health:
            record = json.loads(service.health.read_text(encoding='utf-8'))
            return record.get('pid') == process.pid and record.get('mode') == mode
        with build_opener(ProxyHandler({})).open(service.url, timeout=1) as response:
            record = json.load(response)
        return record.get('pid') == process.pid and record.get('mode') == mode
    except (OSError, ValueError):
        return False


def send_stop(process, force=False):
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            if force:
                process.kill()
            else:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    process.terminate()
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
    except ProcessLookupError:
        pass


class Supervisor:
    def __init__(self, config, ui, path, *, popen=subprocess.Popen, probe=ready, sender=send_stop,
                 clock=time.monotonic, wait=None, startup_timeout=30, shutdown_timeout=45):
        self.config, self.ui, self.path = config, ui, path
        self.popen, self.probe, self.sender, self.clock = popen, probe, sender, clock
        self.stop = threading.Event()
        self.wait = wait or self.stop.wait
        self.startup_timeout, self.shutdown_timeout = startup_timeout, shutdown_timeout
        self.closed = False
        self.control = None
        self.children = []
        self.outputs = []
        self.run_id = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        self.log_dir = config.unified_db.parent / 'logs' / 'launcher' / self.run_id
        self.lease = FileLease(config.unified_db.parent / '.health' / config.unified_db.name / 'launcher.lock')

    def start(self):
        if self.closed or self.children:
            raise RuntimeError('此啟動器已啟動或已關閉')
        self.lease.acquire()
        try:
            self.control = ServiceControl(self.config.unified_db, 'launcher', self.config.mode).start()
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # Freeze the selected mode/paths for all children in this run.
            # Editing the user's JSON while booting cannot create mixed modes.
            ui_snapshot = self.log_dir / 'dashboard.json'
            ui_snapshot.write_text(self.ui.model_dump_json(indent=2), encoding='utf-8')
            runtime_snapshot = self.log_dir / 'runtime.json'
            values = self.config.model_dump(mode='json')
            values['dashboard_config'] = str(ui_snapshot.resolve())
            runtime_snapshot.write_text(json.dumps(values, indent=2), encoding='utf-8')
            for spec in service_plan(self.config, self.ui, runtime_snapshot):
                if self.stop.is_set() or self.control.stop_requested:
                    raise InterruptedError('啟動已停止')
                output = (self.log_dir / (spec.name + '.log')).open('ab')
                self.outputs.append(output)
                options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
                process = self.popen([sys.executable, '-X', 'utf8', '-u', *spec.arguments],
                    cwd=str(ROOT), stdout=output, stderr=subprocess.STDOUT, **options)
                self.children.append((spec, process))
                deadline = self.clock() + self.startup_timeout
                while not self.probe(spec, process, self.config.mode):
                    self.check_children()
                    if self.stop.is_set() or self.control.stop_requested:
                        raise InterruptedError('啟動已停止')
                    if self.clock() >= deadline:
                        raise TimeoutError(f'{spec.name} 啟動逾時；請查看 {self.log_dir}')
                    self.wait(.1)
                print(f'{spec.name} 已啟動（PID {process.pid}）', flush=True)
            self.write_status('running')
        except BaseException:
            self.close()
            raise

    def check_children(self):
        for spec, process in self.children:
            if process.poll() is not None:
                raise RuntimeError(f'{spec.name} 已結束；請查看 {self.log_dir}')

    def write_status(self, state, forced=None, pending=None):
        document = dict(state=state, mode=self.config.mode, launcher_pid=os.getpid(),
            timestamp=datetime.now(timezone(timedelta(hours=8))).isoformat(),
            services={spec.name: process.pid for spec, process in self.children},
            forced_pids=forced or [], pending_pids=pending or [], logout_unconfirmed=bool(forced or pending))
        (self.log_dir / 'run.json').write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')

    def request_owned_stops(self, requested):
        """Propagate cooperative shutdown only to our still-running children."""
        pids = {process.pid for _, process in self.children if process.poll() is None}
        for folder in {control_folder(self.config.acquisition_db), control_folder(self.config.unified_db)}:
            for path in folder.glob('*.owner.json'):
                try:
                    record = read_owner(path)
                    if record.get('pid') in pids and record['instance'] not in requested:
                        request_stop(path, record)
                        requested.add(record['instance'])
                except (OSError, ValueError, RuntimeError):
                    continue  # keep waiting; an invalid marker cannot authorize a signal

    def close(self):
        if self.closed:
            return
        self.closed = True
        graceful_only = bool(self.control and self.control.stop_requested)
        requested = set()
        try:
            if graceful_only:
                self.request_owned_stops(requested)
            for _, process in reversed(self.children):
                if graceful_only:
                    # Use instance-specific requests. Do not interrupt SDK logout
                    # or fall back to Windows TerminateProcess during cleanup.
                    continue
                try:
                    self.sender(process)
                except OSError:
                    print(f'程序 {process.pid} 未回應停止要求，等待後再結束。', flush=True)
            deadline = self.clock() + self.shutdown_timeout
            while any(process.poll() is None for _, process in self.children) and self.clock() < deadline:
                if graceful_only:
                    self.request_owned_stops(requested)
                # stop is already set during Ctrl+C; Event.wait would busy-spin.
                time.sleep(.1) if self.wait == self.stop.wait else self.wait(.1)
            forced, pending = [], []
            for _, process in reversed(self.children):
                if process.poll() is None:
                    if not graceful_only:
                        forced.append(process.pid)
                    try:
                        if not graceful_only:
                            self.sender(process, force=True)
                    except OSError:
                        print(f'程序 {process.pid} 無法結束；登出未確認。', flush=True)
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pending.append(process.pid)
            if self.log_dir.exists():
                self.write_status('stop_unconfirmed' if any(process.poll() is None for _, process in self.children) else 'stopped', forced, pending)
            if graceful_only and pending:
                print('舊服務尚未完成登出；保留程序，不強制終止、不新增登入。', flush=True)
            elif forced:
                print('部分程序逾時，已發出終止要求；券商登出未確認，請核對連線後再啟動。', flush=True)
        finally:
            for output in self.outputs:
                output.close()
            self.outputs.clear()
            self.children.clear()
            try:
                if self.control:
                    self.control.close()
            finally:
                self.lease.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-config', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--no-browser', action='store_true', help='啟動後不自動開啟瀏覽器')
    args = parser.parse_args(argv)
    if Path(sys.prefix).name != 'tss':
        parser.error('請使用 conda tss 環境，或執行 start_all.sh / start_all.command')
    try:
        config = RuntimeConfig.load(args.runtime_config)
        ui = DashboardConfig.load(config.dashboard_config)
        preflight(config, ui, ports=False)
        # Serialize cleanup+startup so concurrent invocations cannot restart
        # services while another invocation is still stopping old processes.
        startup = FileLease(control_folder(config.unified_db) / 'startup.lock')
        startup.acquire()
        supervisor = Supervisor(config, ui, args.runtime_config)
        for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), lambda *_: supervisor.stop.set())
        try:
            cleanup_previous(config)
            preflight(config, ui)
            supervisor.start()
        finally:
            startup.close()
        url = http_root(ui.host, ui.port)
        print(f'Dashboard：{url} · 模式 {config.mode}\n日誌：{supervisor.log_dir}\n啟動完成不代表已開放交易；Ctrl+C 停止本次服務。', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            while not supervisor.stop.wait(.5):
                if supervisor.control and supervisor.control.stop_requested:
                    break
                supervisor.check_children()
        finally:
            supervisor.close()
        return 0
    except (ValueError, OSError, RuntimeError, InterruptedError) as error:
        print(f'啟動失敗：{error}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
