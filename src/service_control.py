"""Cooperative process shutdown and session telemetry, with no broker login.

A stop request targets an instance nonce, never an arbitrary PID. A free OS lock
is evidence that an owner exited, not evidence that its broker logout succeeded.
"""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import tempfile
import uuid

from file_lease import FileLease, LeaseUnavailable

ROOT = Path(__file__).resolve().parents[1]
CLEAN_SESSION_STATES = {'none', 'logged_out'}


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as output:
            name = output.name
            json.dump(document, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def control_folder(db_path):
    path = Path(db_path).resolve()
    return path.parent / '.health' / path.name


def read_owner(path):
    record = json.loads(Path(path).read_text(encoding='utf-8'))
    if (record.get('protocol') != 1 or record.get('project_root') != str(ROOT)
            or not isinstance(record.get('instance'), str) or not record['instance']
            or not isinstance(record.get('role'), str) or not record['role']
            or not isinstance(record.get('db_path'), str) or not record['db_path']
            or not isinstance(record.get('broker'), bool)):
        raise RuntimeError(f'無法確認舊服務身分：{path}；停止啟動，請人工核對')
    return record


def locked(path):
    lease = FileLease(path)
    try:
        lease.acquire()
    except LeaseUnavailable:
        return True
    else:
        lease.close()
        return False


def owner_active(path):
    return locked(Path(path).with_suffix('.lock'))


def request_stop(path, record):
    request_path = Path(path).with_suffix('.stop.json')
    atomic_json(request_path, dict(protocol=1, instance=record['instance'], action='stop', requested_at=now()))


class ServiceControl:
    def __init__(self, db_path, role, mode, *, broker=False):
        self.path = control_folder(db_path) / f'{role}.owner.json'
        self.request_path = self.path.with_suffix('.stop.json')
        self.lease = FileLease(self.path.with_suffix('.lock'))
        self.document = dict(protocol=1, project_root=str(ROOT), db_path=str(Path(db_path).resolve()),
            role=role, mode=mode, broker=broker, pid=os.getpid(), instance=uuid.uuid4().hex,
            state='running', session_state='none', started_at=now(), logout_unconfirmed=False)
        self.started = False

    def start(self):
        self.lease.acquire()
        try:
            atomic_json(self.path, self.document)
            self.started = True
        except BaseException:
            self.lease.close()
            raise
        return self

    @property
    def stop_requested(self):
        if not self.started or not self.request_path.exists():
            return False
        try:
            record = json.loads(self.request_path.read_text(encoding='utf-8'))
            return record.get('instance') == self.document['instance'] and record.get('action') == 'stop'
        except (OSError, ValueError):
            return False

    def session(self, event):
        states = dict(login_started='login_started', login_succeeded='active',
                      logout_started='logout_started', logout_succeeded='logged_out', logout_failed='logout_unconfirmed')
        if event not in states or not self.started:
            return
        self.document.update(session_state=states[event], updated_at=now())
        self.document[event + '_at'] = now()
        if event == 'logout_failed':
            self.document['logout_unconfirmed'] = True
        atomic_json(self.path, self.document)

    def sample_usage(self, api):
        """Only reuse this owner's existing API. Never construct or log in one."""
        if not self.started or api is None or not callable(getattr(api, 'usage', None)):
            return
        try:
            result = api.usage(timeout=3000)
            connections = getattr(result, 'connections', None)
            if isinstance(connections, int) and not isinstance(connections, bool):
                self.document.update(connections_before_logout=connections, usage_at=now())
        except Exception as error:
            self.document['usage_error_type'] = type(error).__name__
        try:
            atomic_json(self.path, self.document)
        except OSError:
            pass  # telemetry must not prevent the actual logout

    def close(self):
        if not self.started:
            return
        try:
            self.document.update(state='stopped', stopped_at=now())
            if self.document['broker'] and self.document['session_state'] not in CLEAN_SESSION_STATES:
                self.document['logout_unconfirmed'] = True
            atomic_json(self.path, self.document)
        finally:
            self.started = False
            self.lease.close()


def cleanup_previous(config, *, timeout=45, clock=None, wait=None):
    import time
    clock, wait = clock or time.monotonic, wait or time.sleep
    folders = {control_folder(config.acquisition_db), control_folder(config.unified_db)}
    owners = []
    for folder in folders:
        for path in folder.glob('*.owner.json'):
            record = read_owner(path)
            if Path(record['db_path']).resolve() not in {config.acquisition_db.resolve(), config.unified_db.resolve()}:
                raise RuntimeError(f'舊服務 DB 身分不符：{path}')
            owners.append((path, record, owner_active(path)))
    # Legacy services cannot acknowledge cooperative logout. Never signal a PID
    # just because it was found in an old health file (it might have been reused).
    legacy = [(control_folder(config.acquisition_db), role) for role in ('tick-fetcher', 'kbar-fetcher')]
    legacy += [(control_folder(config.unified_db), path.stem) for path in
               control_folder(config.unified_db).glob('trading-*.json') if '.owner' not in path.name]
    for folder, role in legacy:
        if (folder / f'{role}.owner.json').exists():
            continue
        health = folder / f'{role}.json'
        if health.exists() and locked(folder / f'{role}.lock'):
            raise RuntimeError(f'{role} 為尚未停止的舊版服務；請在原視窗 Ctrl+C 正常登出後重啟')
    broker_owners = [(path, record, active) for path, record, active in owners if record['broker']]
    if not any(active for _, _, active in broker_owners):
        return []
    # Stop old supervisors too, so they cannot restart an owner after logout.
    active_owners = [(path, record) for path, record, active in owners if active]
    for path, record in active_owners:
        request_stop(path, record)
    print('已要求舊服務正常停止，等待程序結束。', flush=True)
    deadline = clock() + timeout
    while True:
        pending = []
        for path, original in active_owners:
            current = read_owner(path)
            if current['instance'] != original['instance']:
                raise RuntimeError('清理期間舊服務已換代，停止啟動以避免重複登入')
            if owner_active(path):
                pending.append(original['role'])
        if not pending:
            break
        if clock() >= deadline:
            raise RuntimeError('舊服務停止逾時，不強制終止、不啟動重複服務：' + ', '.join(pending))
        wait(.1)
    print('舊服務已停止，開始啟動新服務。', flush=True)
    return [read_owner(path) for path, _, _ in broker_owners]


def run_server(app, *, control, **options):
    """Let a cooperative request invoke uvicorn's normal lifespan shutdown."""
    import threading
    import uvicorn
    options.pop('workers', None)  # this owner always has exactly one process
    server = uvicorn.Server(uvicorn.Config(app, workers=1, **options))
    finished = threading.Event()
    def watch():
        while not finished.wait(.1):
            owner = control()
            owner = getattr(owner, 'control', owner)
            if owner is not None and owner.stop_requested:
                server.should_exit = True
                return
    watcher = threading.Thread(target=watch, name='service-stop-control', daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        finished.set()
        watcher.join(timeout=1)
