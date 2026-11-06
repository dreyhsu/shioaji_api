"""Default tests are offline, including collection; no private credentials."""
from pathlib import Path
import socket
import sys
import tempfile

import pytest

_guards = pytest.MonkeyPatch()
_socket_connect = socket.socket.connect


def _local_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    """Windows asyncio needs a TCP self-pipe even for in-process HTTP tests.

    Use the original connect only for this newly bound ephemeral listener. All
    ordinary socket/network entry points remain blocked, including loopback.
    """
    if family not in (socket.AF_INET, socket.AF_INET6) or type != socket.SOCK_STREAM or proto != 0:
        raise ValueError('offline socketpair requires IPv4/IPv6 TCP with protocol zero')
    host = '127.0.0.1' if family == socket.AF_INET else '::1'
    with socket.socket(family, type, proto) as listener:
        listener.bind((host, 0))
        listener.listen(1)
        listener.settimeout(5)
        client = socket.socket(family, type, proto)
        try:
            client.setblocking(False)
            try:
                _socket_connect(client, listener.getsockname())
            except (BlockingIOError, InterruptedError):
                pass
            client.setblocking(True)
            server, _ = listener.accept()
        except BaseException:
            client.close()
            raise
    if server.getsockname() != client.getpeername() or client.getsockname() != server.getpeername():
        server.close()
        client.close()
        raise ConnectionError('unexpected offline socketpair peer')
    return server, client


def _blocked(*args, **kwargs):
    raise RuntimeError("Offline tests: real network/broker access is disabled; inject a Mock API")


def pytest_configure(config):
    import dotenv
    import requests
    import shioaji
    # Installed before test modules are collected and import application modules.
    _guards.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for name in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY", "SHIOAJI_CA_PATH", "SHIOAJI_CA_PASSWD",
                 "SHIOAJI_PERSON_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        _guards.delenv(name, raising=False)
    _guards.setattr(shioaji, "Shioaji", _blocked)
    if sys.platform == 'win32':
        _guards.setattr(socket, 'socketpair', _local_socketpair)
    _guards.setattr(socket.socket, "connect", _blocked)
    _guards.setattr(socket.socket, "connect_ex", _blocked)
    _guards.setattr(socket, "create_connection", _blocked)
    _guards.setattr(requests.sessions.Session, "request", _blocked)


def pytest_unconfigure(config):
    _guards.undo()


@pytest.fixture(autouse=True)
def temporary_database_only(monkeypatch):
    import db
    original = db.connect

    def connect(path=db.DEFAULT_DB_PATH):
        if not Path(path).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError("Offline tests: databases must live in a temporary directory")
        return original(path)

    monkeypatch.setattr(db, "connect", connect)


def pytest_collection_modifyitems(items):
    integration_files = {"test_dashboard.py", "test_monitor.py", "test_trading.py", "test_mock_runtime.py"}
    for item in items:
        if not (list(item.iter_markers("unit")) or list(item.iter_markers("integration"))):
            item.add_marker(pytest.mark.integration if item.path.name in integration_files else pytest.mark.unit)
