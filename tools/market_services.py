"""Read-only deployment preflight and launchd file generation; never installs jobs.

Run in tss. Rendering writes only the requested output folder. Preflight never
loads credentials, logs in, migrates a database, or changes orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import sqlite3
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import market_clock
import market_health as mh


def launchd_specs(root, python, db_path, *, code, mode, health_config, port=8765, notify=False):
    root, db_path = Path(root).resolve(), Path(db_path).resolve()
    suffix = hashlib.sha256(f"{db_path}:{code}".encode()).hexdigest()[:10]
    specs = {}
    for role in ("monitor", "trading", "watchdog"):
        label = f"com.shioaji.{role}.{suffix}"
        command = ([str(python), str(root / "src" / "health_watchdog.py"), "--mode", mode]
                   if role == "watchdog" else
                   [str(python), str(root / "tools" / "run_market_service.py"), "--role", role])
        command += ["--db", str(db_path), "--code", code, "--health-config", str(Path(health_config).resolve())]
        if role == "trading":
            command += ["--port", str(port)]
        if role == "watchdog" and notify:
            command += ["--notify"]
        specs[label] = {"Label": label, "ProgramArguments": command, "WorkingDirectory": str(root),
                        "RunAtLoad": True, "KeepAlive": role != "trading", "ThrottleInterval": 30,
                        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "SHIOAJI_SIMULATION": "1" if mode == "simulation" else "0"},
                        "StandardOutPath": str(root / "data" / "logs" / f"{label}.out.log"),
                        "StandardErrorPath": str(root / "data" / "logs" / f"{label}.err.log")}
    return specs


def preflight(db_path, code, mode, settings, *, now):
    calendar = mh.SessionCalendar.load(settings.calendar_path)
    result = {"db_path": str(Path(db_path).resolve()), "mode": mode, "calendar_error": calendar.error,
              "calendar_covers_now": calendar.covered(now), "trading_reconnect_enabled": settings.trading_reconnect_enabled,
              "broker_positions_and_protection": "NOT VERIFIED — check broker separately", "unresolved_orders": None,
              "local_positions": [], "runtime": {}}
    since = pd.Timestamp(now).normalize() - pd.Timedelta(days=60)
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as conn:
            row = conn.execute("SELECT MAX(minute_start) FROM minute_bars WHERE code = ?", (code,)).fetchone()
            if row and row[0]:
                since = pd.Timestamp(row[0]) - pd.Timedelta(minutes=1)
            rows = conn.execute("SELECT contract_code, action, filled_quantity, exit_filled_quantity, entry_status, exit_status "
                                "FROM managed_orders WHERE code = ? AND simulation = ? AND active = 1",
                                (code, int(mode == "simulation"))).fetchall()
        unresolved = 0
        for contract, action, filled, exited, entry_status, exit_status in rows:
            unresolved += int(entry_status in {"submitting", "unknown"} or exit_status in {
                "submitting", "unknown", "waiting_entry_cancel", "cancelling_entry"})
            if filled > exited:
                result["local_positions"].append({"contract": contract, "side": action, "quantity": filled - exited})
        result["unresolved_orders"] = unresolved
        result["database_readable"] = True
    except sqlite3.Error as error:
        result["database_readable"] = False
        result["database_error"] = type(error).__name__
    previous = mh.read_health(mh.health_path(db_path, "monitor", code))
    if previous.get("validation_pending") and previous.get("validation_since"):
        since = min(since, pd.Timestamp(previous["validation_since"]))
    result["calendar_covers_resume"] = calendar.covered(since)
    result["resume_validation_since"] = mh.iso(since)
    current_hashes = mh.fingerprint()["source_hashes"]
    for role in ("monitor", "trading"):
        document = mh.read_health(mh.health_path(db_path, role, code))
        result["runtime"][role] = {"pid": document.get("pid"), "python": document.get("python"),
                                   "revision": document.get("revision"), "state": document.get("state"),
                                   "mode_matches": document.get("mode") == mode,
                                   "source_matches": document.get("source_hashes") == current_hashes}
    result["configuration_ready"] = bool(calendar.covered(now) and calendar.covered(since))
    result["runtime_verified"] = False  # local files cannot verify broker-side execution/protection
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "render"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--mode", required=True, choices=("production", "simulation"))
    parser.add_argument("--health-config", type=Path, default=mh.DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args(argv)
    mh.health_path(args.db, "monitor", args.code)  # validate identity before writing paths
    settings = mh.load_settings(args.health_config)
    if args.action == "preflight":
        result = preflight(args.db, args.code, args.mode, settings, now=market_clock.now())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["configuration_ready"] and result["database_readable"] and result["unresolved_orders"] == 0 else 1
    if not args.output:
        parser.error("render requires --output")
    if Path(sys.prefix).name != "tss":
        parser.error("render must run in conda env tss")
    specs = launchd_specs(ROOT, sys.executable, args.db, code=args.code, mode=args.mode,
                          health_config=args.health_config, port=args.port, notify=args.notify)
    args.output.mkdir(parents=True, exist_ok=True)
    for label, document in specs.items():
        path = args.output / f"{label}.plist"
        if path.exists():
            parser.error(f"refusing to overwrite {path}; use a new output directory")
        path.write_bytes(plistlib.dumps(document))
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
