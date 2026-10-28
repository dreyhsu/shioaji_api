import os
import signal
import subprocess
import sys
import pandas as pd
import market_health as mh
from run_market_service import child_stalled, stop_child


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
    children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
                for _ in range(2)]
    try:
        stop_child(children[0], grace_seconds=1)
        assert children[0].poll() is not None and children[1].poll() is None
    finally:
        for child in children:
            stop_child(child, grace_seconds=1)
