import sqlite3
from types import SimpleNamespace as NS
import pytest
from connection_recovery import RecoveryGate, RecoveryPolicy, error_kind
import shioaji_client as sc


@pytest.mark.parametrize("error,kind", [
    (RuntimeError("code: NotReady, SubCode(SessionNotEstablished)"), "session"),
    (type("SessionNotEstablished", (RuntimeError,), {})(""), "session"),
    (RuntimeError("Response Code: 401"), "authentication"),
    (RuntimeError("file 401.db: NotReadyYet"), "other"),
    (sqlite3.OperationalError("NotReady"), "database"),
    (TimeoutError(), "transport"), (ValueError("broken config"), "other"),
    (RuntimeError("QuotaExceeded"), "quota"),
])
def test_exact_failure_classification(error, kind):
    assert error_kind(error) == kind


def test_exception_graph_and_cycles_keep_database_precedence():
    outer = RuntimeError("NotReady")
    outer.__cause__ = ValueError("wrapped")
    outer.__context__ = sqlite3.OperationalError("database locked")
    outer.__cause__.__cause__ = outer
    assert error_kind(outer) == "database"


def test_bounded_backoff_threshold_and_slow_circuit():
    clock = [0]
    policy = RecoveryPolicy(2, 5, 20, 4, 100)
    gate = RecoveryGate(policy, clock=lambda: clock[0], jitter=lambda: 2)
    gate.fail("session")
    assert not gate.pending
    gate.fail("session")
    assert gate.pending and not gate.due
    clock[0] = 5
    assert gate.due
    for delay in (20, 20, 20, 100, 100):
        gate.attempted()
        assert gate.next_attempt - clock[0] == delay
        clock[0] += delay
    gate.success()
    assert not gate.pending and gate.attempts == gate.failures == 0


def test_session_fails_fast_preserving_cause(monkeypatch):
    calls = []
    def call():
        calls.append(1)
        raise RuntimeError("NotReady, SessionNotEstablished")
    monkeypatch.setattr(sc.time, "sleep", lambda *_: pytest.fail("session retry slept"))
    with pytest.raises(sc.DataUnavailable) as caught:
        sc._with_retries("kbars", call)
    assert len(calls) == 1 and sc.is_session_unavailable(caught.value)


def test_transport_retry_has_limit(monkeypatch):
    calls, sleeps = [], []
    def call():
        calls.append(1)
        raise ConnectionError("offline")
    monkeypatch.setattr(sc.time, "sleep", sleeps.append)
    with pytest.raises(sc.DataUnavailable):
        sc._with_retries("kbars", call)
    assert len(calls) == 3 and sleeps == [2, 4]


def test_partial_login_failure_cleans_up(monkeypatch):
    calls = []
    def login(**_):
        raise RuntimeError("SessionNotEstablished")
    monkeypatch.setenv("SHIOAJI_API_KEY", "offline-test")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "offline-test")
    monkeypatch.setattr(sc.sj, "Shioaji", lambda **_: NS(login=login, logout=lambda: calls.append("logout")))
    with pytest.raises(RuntimeError):
        with sc.connect():
            pytest.fail("failed login yielded")
    assert calls == ["logout"]
