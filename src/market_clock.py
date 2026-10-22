"""The market's wall clock, independent of the machine's timezone.

Every timestamp in tmf.db is a naive Taipei stamp, and the session boundaries
(08:45-13:45, 15:00-05:00) are Taipei times.  ``pd.Timestamp.now()`` returns the
*computer's* local time, so on a laptop in Japan (UTC+9) the monitor thought the
day session ended at 12:45 and the dashboard showed every bar an hour stale.

``now()`` is UTC plus ``utc_offset_hours`` from ``config/alerts.json`` (the one
monitor config file; default 8, Taiwan) -- the same answer wherever the machine
is.  Read once at import, so a change needs a restart.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "alerts.json"
DEFAULT_UTC_OFFSET_HOURS = 8.0


def _load_offset(path: Path = SETTINGS_PATH) -> float:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("utc_offset_hours")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_UTC_OFFSET_HOURS
    return DEFAULT_UTC_OFFSET_HOURS if value is None else float(value)


UTC_OFFSET_HOURS = _load_offset()


def now() -> pd.Timestamp:
    """Naive market-time "now", comparable with every stamp in the database."""
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return pd.Timestamp(utc) + pd.Timedelta(hours=UTC_OFFSET_HOURS)


def today() -> date:
    return now().date()
