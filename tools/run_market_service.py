"""Own and supervise a child process; never searches for or kills other PIDs.

Monitor crashes/hangs may be restarted. Trading hangs are reported but NEVER
automatically killed or restarted. Use an OS service for this supervisor and
the separate health_watchdog, and an external check for host/watchdog death.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import market_health as mh
import market_clock
from health_watchdog import age_seconds


def child_stalled(document, *, pid, now, started_at, settings):
    try:
        stamp = pd.Timestamp(document.get("started_at"))
        valid_start = pd.Timestamp(started_at) - pd.Timedelta(seconds=5) <= stamp <= pd.Timestamp(now) + pd.Timedelta(seconds=5)
    except (TypeError, ValueError):
        valid_start = False
    if document.get("pid") != pid or not valid_start:
        return age_seconds(started_at, now) > settings.operation_timeout_seconds
    return (age_seconds(document.get("heartbeat_at"), now) > settings.heartbeat_seconds
            or bool(document.get("operation")) and
            age_seconds(document.get("operation_started"), now) > settings.operation_timeout_seconds)


def stop_child(child, *, grace_seconds=10):
    """The Popen object is the ownership proof; no arbitrary PID argument."""
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=grace_seconds)
    except ProcessLookupError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("monitor", "trading"))
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--health-config", type=Path, default=mh.DEFAULT_CONFIG)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(argv)
    settings = mh.load_settings(args.health_config)
    path = mh.health_path(args.db, args.role, args.code)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "src" / ("monitor.py" if args.role == "monitor" else "dashboard.py")),
               "--db", str(args.db.resolve()), "--code", args.code, "--health-config", str(args.health_config.resolve())]
    if args.role == "trading":
        command += ["--host", args.host, "--port", str(args.port)]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with path.with_suffix(".supervisor.lock").open("a+") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempts = 0
        while not stop.is_set():
            started_at = market_clock.now()
            child = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
            warned = False
            document = {}
            try:
                while child.poll() is None and not stop.wait(1):
                    document = mh.read_health(path)
                    if child_stalled(document, pid=child.pid, now=market_clock.now(), started_at=started_at, settings=settings):
                        if args.role == "monitor":
                            print("owned monitor stalled; stopping child before restart", flush=True)
                            stop_child(child)
                            break
                        if not warned:
                            print("trading stalled: manual broker reconciliation required; child left running", flush=True)
                            warned = True
                if stop.is_set():
                    stop_child(child)
                    return 0
                if args.role == "trading":
                    print("trading exited; no automatic restart; reconcile broker before restarting", flush=True)
                    return child.returncode or 0
                attempts += 1
                # Heartbeat alone must not reset a crash loop; require durable progress.
                if (document.get("state") == "healthy" and document.get("cycle_completed_at")
                        and age_seconds(started_at, market_clock.now()) >= 600):
                    attempts = 0
                stop.wait(min(settings.recovery_cooldown_seconds,
                              settings.recovery_initial_seconds * 2 ** min(attempts, 10)))
            finally:
                if stop.is_set():
                    stop_child(child)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
