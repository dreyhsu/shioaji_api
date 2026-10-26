"""Default tests are offline, including collection; no private credentials."""
from pathlib import Path
import socket
import tempfile

import pytest

_guards = pytest.MonkeyPatch()


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
