"""Own and supervise a child process; never searches for or kills other PIDs.

Monitor crashes/hangs may be restarted. Trading hangs are reported but NEVER
automatically killed or restarted. Use an OS service for this supervisor and
the separate health_watchdog, and an external check for host/watchdog death.
"""
from __future__ import annotations

import argparse
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
from file_lease import FileLease
from runtime_config import RuntimeConfig, DEFAULT_RUNTIME, skip_mock

IS_WINDOWS = sys.platform == "win32"


def child_process_options():
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


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
    if IS_WINDOWS:
        # CTRL_BREAK targets only the group created by this supervisor. A
        # detached console may reject it; fall back to this owned child handle.
        try:
            child.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            child.terminate()
        try:
            child.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=grace_seconds)
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
    parser.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--db", default=None, type=Path)
    parser.add_argument("--code", default=None)
    parser.add_argument("--health-config", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None, type=int)
    args = parser.parse_args(argv)
    config = RuntimeConfig.load(args.runtime_config)
    if skip_mock(config.mode):
        return 0
    args.db, args.code = args.db or config.unified_db, args.code or config.signal_code
    args.health_config = args.health_config or config.health_config
    args.host, args.port = args.host or config.trade_host, args.port or config.trade_port
    settings = mh.load_settings(args.health_config)
    path = mh.health_path(args.db, args.role, args.code)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "src" / ("monitor.py" if args.role == "monitor" else "trade.py")),
               "--runtime-config", str(args.runtime_config.resolve()),
               "--db", str(args.db.resolve()), "--code", args.code, "--health-config", str(args.health_config.resolve())]
    if args.role == "trading":
        command += ["--host", args.host, "--port", str(args.port)]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    with FileLease(path.with_suffix(".supervisor.lock")):
        attempts = 0
        while not stop.is_set():
            started_at = market_clock.now()
            child = subprocess.Popen(command, cwd=ROOT, **child_process_options())
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
