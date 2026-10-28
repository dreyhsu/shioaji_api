import os
import signal
import subprocess
import sys
import pandas as pd
import market_health as mh
from run_market_service import child_stalled, stop_child, child_process_options
import run_market_service as supervisor
from unittest.mock import Mock


def test_stalled_operation_does_not_get_hidden_by_fresh_heartbeat():
    now = pd.Timestamp("2026-10-27 19:10")
    document = {"pid": 123, "started_at": "2026-10-27 19:00", "heartbeat_at": str(now),
                "operation": "kbars", "operation_started": "2026-10-27 19:01"}
    assert child_stalled(document, pid=123, now=now, started_at="2026-10-27 19:00", settings=mh.HealthSettings())
    document["operation"] = None
    assert not child_stalled(document, pid=123, now=now, started_at="2026-10-27 19:00", settings=mh.HealthSettings())
    document["started_at"] = "2026-10-27 19:00:30"  # slow imports/initialization are legitimate
    assert not child_stalled(document, pid=123, now=now, started_at="2026-10-27 19:00", settings=mh.HealthSettings())


def test_owned_child_is_reaped_and_other_process_is_untouched():
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **child_process_options())
                for _ in range(2)]
    try:
        stop_child(children[0], grace_seconds=1)
        assert children[0].poll() is not None and children[1].poll() is None
    finally:
        for child in children:
            stop_child(child, grace_seconds=1)


def test_windows_child_uses_a_new_process_group(monkeypatch):
    monkeypatch.setattr(supervisor, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    assert child_process_options() == {"creationflags": 512}


def test_windows_stop_uses_owned_child_and_kills_only_after_grace(monkeypatch):
    monkeypatch.setattr(supervisor, "IS_WINDOWS", True)
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    child = Mock()
    child.poll.return_value = None
    child.wait.side_effect = [subprocess.TimeoutExpired("child", 1), 0]
    stop_child(child, grace_seconds=1)
    child.send_signal.assert_called_once_with(signal.CTRL_BREAK_EVENT)
    child.kill.assert_called_once_with()
    child.terminate.assert_not_called()
    assert child.wait.call_count == 2


def test_windows_detached_console_falls_back_to_owned_child_handle(monkeypatch):
    monkeypatch.setattr(supervisor, "IS_WINDOWS", True)
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)
    child = Mock()
    child.poll.return_value = None
    child.send_signal.side_effect = OSError("no console")
    stop_child(child, grace_seconds=1)
    child.terminate.assert_called_once_with()
    child.kill.assert_not_called()


def test_windows_already_exited_child_is_not_signalled(monkeypatch):
    monkeypatch.setattr(supervisor, "IS_WINDOWS", True)
    child = Mock()
    child.poll.return_value = 0
    stop_child(child)
    child.send_signal.assert_not_called()
    child.kill.assert_not_called()
