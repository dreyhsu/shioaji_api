import errno
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest
from file_lease import FileLease, LeaseUnavailable


def test_native_lock_rejects_a_second_process_then_allows_takeover(tmp_path):
    path = tmp_path / "service.lock"
    program = """
import sys
sys.path.insert(0, sys.argv[1])
from file_lease import FileLease, LeaseUnavailable
try:
    with FileLease(sys.argv[2]):
        pass
except LeaseUnavailable:
    raise SystemExit(42)
"""
    command = [sys.executable, "-c", program, str(Path(__file__).parent), str(path)]
    with FileLease(path):
        result = subprocess.run(command, capture_output=True, timeout=10)
        assert result.returncode == 42, result.stderr.decode(errors="replace")
    assert path.exists()  # never unlink a lock inode during normal release
    result = subprocess.run(command, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_exception_in_owner_releases_native_lease(tmp_path):
    path = tmp_path / "service.lock"
    with pytest.raises(ValueError):
        with FileLease(path):
            raise ValueError("work failed")
    with FileLease(path):
        pass


def test_windows_backend_locks_the_same_byte_and_releases_it(monkeypatch, tmp_path):
    held, calls = {}, []
    def locking(fd, operation, count):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0 and count == 1
        key = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        calls.append(operation)
        if operation == 1:
            if key in held:
                raise OSError(errno.EACCES, "already locked")
            held[key] = fd
        else:
            assert held.pop(key) == fd
    monkeypatch.setitem(sys.modules, "msvcrt", NS(LK_NBLCK=1, LK_UNLCK=2, locking=locking))
    path = tmp_path / "service.lock"
    first, second = [FileLease(path, _platform="win32") for _ in range(2)]
    with first:
        assert first.acquire() is first
        with pytest.raises(LeaseUnavailable):
            second.acquire()
    with second:
        pass
    second.close()
    assert calls == [1, 1, 2, 1, 2] and not held


def test_failed_windows_lock_closes_handle_and_preserves_io_error(monkeypatch, tmp_path):
    descriptors = []
    def locking(fd, *_):
        descriptors.append(fd)
        raise OSError(errno.EIO, "disk error")
    monkeypatch.setitem(sys.modules, "msvcrt", NS(LK_NBLCK=1, locking=locking))
    lease = FileLease(tmp_path / "service.lock", _platform="win32")
    with pytest.raises(OSError) as caught:
        lease.acquire()
    assert caught.value.errno == errno.EIO and lease._stream is None
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_runtime_modules_import_without_fcntl():
    # Keep import fault injection in its own interpreter; reloading FastAPI's
    # application globals in pytest can change other integration fixtures.
    program = """
import builtins, sys, subprocess, pandas, fastapi, requests, shioaji, dotenv, socket
sys.path[:0] = [sys.argv[1], sys.argv[2]]
def blocked(*args, **kwargs):
    raise RuntimeError('offline import test: external access forbidden')
shioaji.Shioaji = blocked
dotenv.load_dotenv = lambda *args, **kwargs: False
socket.socket.connect = blocked
socket.create_connection = blocked
requests.sessions.Session.request = blocked
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import market_health, health_watchdog, run_market_service, dashboard, monitor
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", program, str(root / "src"), str(root / "tools")],
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
