"""Session-aware validation and atomic health files, independent of SQLite writes.

An explicit exchange calendar is required to certify completeness. Without it,
weekday hours are only a polling hint; they never certify recovery or enable
curve signals. No file, network or broker access occurs at import time.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import timedelta
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pandas as pd
import market_clock
from connection_recovery import RecoveryPolicy

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "market_health.json"


@dataclass(frozen=True)
class HealthSettings:
    calendar_path: str = str(ROOT / "config" / "market_calendar.json")
    bar_lag_seconds: float = 180
    tick_stale_seconds: float = 10
    tick_recovery_seconds: float = 60
    book_stale_seconds: float = 60
    heartbeat_seconds: float = 180
    order_sync_seconds: float = 60
    operation_timeout_seconds: float = 120
    api_timeout_ms: int = 30000
    recovery_failure_threshold: int = 2
    recovery_initial_seconds: float = 5
    recovery_maximum_seconds: float = 120
    recovery_slow_after: int = 6
    recovery_cooldown_seconds: float = 600
    # Promote only after simulation verifies cross-session login/logout effects.
    trading_reconnect_enabled: bool = False
    alert_repeat_seconds: float = 900
    notification_retry_seconds: float = 60

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name not in {"calendar_path", "trading_reconnect_enabled"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{item.name} must be positive and finite")
        if type(self.trading_reconnect_enabled) is not bool:
            raise ValueError("trading_reconnect_enabled must be boolean")
        if self.tick_recovery_seconds < self.tick_stale_seconds:
            raise ValueError("tick recovery must allow a subscription grace period")
        if type(self.api_timeout_ms) is not int or type(self.recovery_failure_threshold) is not int or type(self.recovery_slow_after) is not int:
            raise ValueError("timeouts and failure counts must be integers")
        self.policy()

    def policy(self):
        return RecoveryPolicy(self.recovery_failure_threshold, self.recovery_initial_seconds,
                              self.recovery_maximum_seconds, self.recovery_slow_after, self.recovery_cooldown_seconds)


def load_settings(path=DEFAULT_CONFIG):
    path = Path(path)
    if not path.exists():
        return HealthSettings()
    values = json.loads(path.read_text())
    if not isinstance(values, dict) or set(values) - {item.name for item in fields(HealthSettings)}:
        raise ValueError(f"invalid market health settings: {path}")
    if "calendar_path" in values:
        values["calendar_path"] = str((path.parent / values["calendar_path"]).resolve())
    return HealthSettings(**values)


class SessionCalendar:
    """Explicit Taipei session intervals, including special closes and holidays.

    coverage_start/end certify ALL sessions in that interval; holidays are the
    absence of sessions. Empty-minute evidence is explicit and never fabricated.
    """
    def __init__(self, document=None):
        self.source = None
        self.start = self.end = None
        self.sessions = []
        self.empty_minutes = set()
        self.error = "exchange calendar not configured"
        if document is None:
            return
        if not document.get("source"):
            raise ValueError("calendar requires a provenance/source")
        if any(word in str(document["source"]).lower() for word in ("example only", "replace with")):
            raise ValueError("example calendar must be reviewed before use")
        self.source = str(document["source"])
        self.start, self.end = map(pd.Timestamp, (document["coverage_start"], document["coverage_end"]))
        if self.start.tzinfo or self.end.tzinfo or self.start >= self.end:
            raise ValueError("calendar coverage must be increasing naive Taipei timestamps")
        for row in document["sessions"]:
            start, end = map(pd.Timestamp, (row["start"], row["end"]))
            if start.tzinfo or end.tzinfo or not self.start <= start < end <= self.end:
                raise ValueError("session outside calendar coverage")
            if start != start.floor("min") or end != end.floor("min"):
                raise ValueError("sessions must use minute boundaries")
            self.sessions.append((start, end))
        self.sessions.sort()
        if any(a[1] > b[0] for a, b in zip(self.sessions, self.sessions[1:])):
            raise ValueError("overlapping calendar sessions")
        for row in document.get("confirmed_empty_minutes", []):
            stamp = pd.Timestamp(row["minute_start"])
            if not row.get("evidence") or not any(a <= stamp < b for a, b in self.sessions):
                raise ValueError("empty minute needs in-session evidence")
            self.empty_minutes.add(stamp)
        self.error = None

    @classmethod
    def load(cls, path):
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError, ValueError, KeyError, TypeError) as error:
            result = cls()
            result.error = f"calendar unavailable: {type(error).__name__}"
            return result

    def covered(self, stamp):
        return self.start is not None and self.start <= pd.Timestamp(stamp) < self.end

    def active(self, now):
        now = pd.Timestamp(now)
        if self.covered(now):
            return any(start <= now < end for start, end in self.sessions)
        # Only a polling hint. Friday's night session continues into Saturday.
        clock = now.time()
        if clock >= pd.Timestamp("15:00").time():
            return now.weekday() < 5
        if clock < pd.Timestamp("05:00").time():
            return (now - pd.Timedelta(days=1)).weekday() < 5
        return now.weekday() < 5 and pd.Timestamp("08:45").time() <= clock < pd.Timestamp("13:45").time()

    def expected_minutes(self, start, now):
        """Only CLOSED minutes; session end is exclusive."""
        pieces = []
        for left, right in self.sessions:
            left = max(left, pd.Timestamp(start).ceil("min"))
            right = min(right, pd.Timestamp(now).floor("min"))
            if left < right:
                pieces.extend(pd.date_range(left, right - pd.Timedelta(minutes=1), freq="min"))
        return set(pieces) - self.empty_minutes


def validate_bars(bars, now, calendar, *, since, lag_seconds=180):
    now, since = pd.Timestamp(now), pd.Timestamp(since)
    stamps = set(pd.to_datetime(bars["minute_start"])) if not bars.empty else set()
    closed = sorted(stamp for stamp in stamps if stamp + pd.Timedelta(minutes=1) <= now)
    latest = closed[-1] if closed else None
    expected = calendar.expected_minutes(since, now)
    # Delay is tolerated only at the live edge, never as an unexplained interior gap.
    mature = {stamp for stamp in expected if stamp + pd.Timedelta(minutes=1, seconds=lag_seconds) <= now}
    if not calendar.active(now):
        mature = expected  # final closing minute must be fetched after session end
    missing = sorted(mature - stamps)
    expected_latest = max(expected) if expected else None
    active = calendar.active(now)
    lag = (None if latest is None or expected_latest is None else
           max(0, ((now - pd.Timedelta(minutes=1) if active else expected_latest) - latest).total_seconds()))
    current_start = next((start for start, end in calendar.sessions if start <= now < end), None)
    opening_grace = (current_start is not None and (latest is None or latest < current_start)
                     and (now - current_start).total_seconds() <= lag_seconds + 60)
    covered = calendar.covered(now) and calendar.covered(since)
    future_count = sum(stamp > now for stamp in stamps)
    ready = (covered and not missing and not future_count and not opening_grace
             and (not expected or latest is not None and lag is not None and lag <= lag_seconds))
    return {"ready": ready, "calendar_verified": covered, "calendar_error": calendar.error,
            "active_session": active, "opening_grace": opening_grace, "last_minute_start": iso(latest),
            "expected_minute_start": iso(expected_latest), "lag_seconds": lag,
            "missing_count": len(missing), "missing_sample": [iso(stamp) for stamp in missing[:20]],
            "future_count": future_count,
            "validation_since": iso(since)}


def iso(value):
    return None if value is None else pd.Timestamp(value).isoformat(sep=" ")


def health_path(db_path, role, code):
    if not code.isalnum() or role not in {"monitor", "trading"}:
        raise ValueError("invalid health identity")
    path = Path(db_path).resolve()
    return path.parent / ".health" / path.name / f"{role}-{code}.json"


def read_health(path):
    try:
        result = json.loads(Path(path).read_text())
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint():
    sources = {}
    for name in ("monitor.py", "dashboard.py", "trading.py", "shioaji_client.py", "market_health.py", "connection_recovery.py",
                 "db.py", "orderbook.py", "session_bars.py", "market_clock.py", "curve_entry.py", "curve_entry_controller.py",
                 "shioaji_order_adapter.py", "place_order.py"):
        sources[name] = hashlib.sha256((ROOT / "src" / name).read_bytes()).hexdigest()
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        revision = "unknown"
    return {"pid": os.getpid(), "python": sys.executable, "cwd": str(Path.cwd()),
            "revision": revision, "source_hashes": sources, "shioaji": importlib.metadata.version("shioaji")}


class HealthPublisher:
    """One writer per DB/role/code, with a lock released automatically on death."""
    def __init__(self, db_path, role, code, mode, *, clock=market_clock.now):
        self.path = health_path(db_path, role, code)
        self.clock = clock
        self.lock = None
        self.document = {"schema": 1, "instance": uuid.uuid4().hex, "role": role, "code": code,
                         "db_path": str(Path(db_path).resolve()), "mode": mode,
                         "started_at": iso(clock()), "state": "starting"}

    def acquire(self):
        if self.lock is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError(f"another {self.document['role']} owns {self.path}") from None
        self.lock = stream
        self.document.update(fingerprint())
        self.publish()

    def publish(self, **changes):
        if self.lock is None:
            raise RuntimeError("health publisher must acquire ownership first")
        self.document.update(changes)
        self.document["heartbeat_at"] = iso(self.clock())
        atomic_json(self.path, self.document)

    def close(self):
        if self.lock is not None:
            try:
                self.publish(state="stopped")
            finally:
                self.lock.close()
                self.lock = None


def monitor_ready(db_path, code, now, settings, *, mode=None):
    document = read_health(health_path(db_path, "monitor", code))
    try:
        age = (pd.Timestamp(now) - pd.Timestamp(document["heartbeat_at"])).total_seconds()
        ready = (document["db_path"] == str(Path(db_path).resolve()) and document["code"] == code
                 and (mode is None or document.get("mode") == mode)
                 and document["state"] == "healthy" and document["bars"]["ready"]
                 and -5 <= age <= settings.heartbeat_seconds)
        calendar = SessionCalendar.load(settings.calendar_path)
        if not calendar.covered(now):
            ready = False
        elif calendar.active(now):
            last = pd.Timestamp(document["bars"]["last_minute_start"])
            ready = ready and pd.notna(last) and -5 <= (pd.Timestamp(now) - last - pd.Timedelta(minutes=1)).total_seconds() <= settings.bar_lag_seconds
        return bool(ready), document
    except (KeyError, TypeError, ValueError):
        return False, document
