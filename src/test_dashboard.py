"""Tests for the JSON the chart page is drawn from.

The contract the browser depends on is narrow but easy to break silently:
timestamps are naive Taipei stamps sent as epoch seconds *without* a timezone
offset (lightweight-charts renders every UTCTimestamp as UTC, so the lie is what
makes the axis print Taipei time), every indicator series sits on exactly the
candles' time grid, and a box whose right edge has not frozen yet is flagged
``open`` rather than left for the browser to infer.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard
import db
import session_bars as sb
import snr_core as snr


def minute_frame(start: str, count: int) -> pd.DataFrame:
    """Enough synthetic minutes to produce boxes: a wave with volume spikes."""
    stamps = pd.date_range(pd.Timestamp(start), periods=count, freq="1min")
    base = 20000.0 + 120.0 * ((pd.Series(range(count)) // 180) % 2) + (pd.Series(range(count)) % 37)
    frame = pd.DataFrame(
        {
            "minute_start": stamps,
            "open": base,
            "high": base + 6,
            "low": base - 6,
            "close": base + pd.Series([3 if i % 3 else -3 for i in range(count)]),
            "volume": pd.Series([450.0 if i % 211 == 0 else 50.0 for i in range(count)]),
            "amount": 0.0,
        }
    )
    # add_sessions drops the 13:45-15:00 break and tags the rest, which is what
    # minute_bars_to_hourly keys off.
    return sb.add_sessions(frame).reset_index(drop=True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    with db.session(path) as conn:
        minutes = minute_frame("2026-08-10 08:45:00", 60 * 24 * 40)
        db.upsert_minute_bars(conn, "TMFR1", minutes)
        hourly = sb.minute_bars_to_hourly(minutes)
        db.replace_boxes(conn, "TMFR1", snr.collect_snr_boxes(hourly))

    monkeypatch.setitem(dashboard.CONFIG, "db_path", path)
    monkeypatch.setitem(dashboard.CONFIG, "code", "TMFR1")
    monkeypatch.setitem(dashboard.CONFIG, "days", 30)
    dashboard._LOAD_CACHE.clear()
    yield TestClient(dashboard.app)
    dashboard._LOAD_CACHE.clear()


def test_epoch_reads_a_naive_taipei_stamp_as_utc():
    # 08:45 Taipei must come back as 08:45 UTC, because that is what the chart
    # will print.  Adding the +08:00 offset here would label the bar 16:45.
    assert dashboard._epoch("2026-08-10 08:45:00") == 1786351500
    assert pd.Timestamp(1786351500, unit="s").strftime("%H:%M") == "08:45"


def test_bars_are_ascending_and_unique(client):
    bars = client.get("/api/chart").json()["bars"]
    assert bars
    times = [bar["time"] for bar in bars]
    assert times == sorted(times)
    assert len(set(times)) == len(times)
    first = bars[0]
    assert set(first) == {
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "delta_volume",
    }
    assert first["low"] <= first["open"] <= first["high"]
    # Signed volume, so the magnitude has to match the raw volume.
    assert abs(first["delta_volume"]) == first["volume"]


def test_every_series_sits_on_the_candles_time_grid(client):
    payload = client.get("/api/chart").json()
    times = [bar["time"] for bar in payload["bars"]]
    names = {spec["name"] for spec in payload["series"]}
    assert {"delta_volume", "support_filter", "resistance_filter"} <= names

    for spec in payload["series"]:
        # Equal length and identical timestamps: a series on its own grid would
        # shift the logical index of every bar, and the boxes are positioned by
        # logical index.
        assert [point["time"] for point in spec["data"]] == times, spec["name"]
        assert spec["pane"] in (0, 1, 2)
        assert spec["type"] in ("line", "histogram", "area", "baseline")

    histogram = next(s for s in payload["series"] if s["name"] == "delta_volume")
    coloured = [p for p in histogram["data"] if "value" in p]
    assert coloured
    for point in coloured:
        expected = dashboard.UP_COLOR if point["value"] >= 0 else dashboard.DOWN_COLOR
        assert point["color"] == expected


def test_boxes_are_clipped_to_the_window_and_flag_the_open_one(client):
    payload = client.get("/api/chart").json()
    bars, boxes = payload["bars"], payload["boxes"]
    assert boxes
    first, last = bars[0]["time"], bars[-1]["time"]

    for box in boxes:
        assert box["box_type"] in ("support", "resistance")
        assert box["state"] in ("fresh", "holds", "broken")
        assert box["bottom"] < box["top"]
        # clip_boxes clamps both edges into the window, so neither can escape it.
        assert first <= box["left_time"] <= last
        assert first <= box["right_time"] <= last
        assert box["left_time"] <= box["right_time"]
        # A box is open exactly when its right edge was clamped to the last bar.
        assert box["open"] == (box["right_time"] == last)

    # The newest box of a side is always still advancing, so at least one is open.
    assert any(box["open"] for box in boxes)


def test_thin_endpoints_agree_with_the_combined_one(client):
    whole = client.get("/api/chart").json()
    assert client.get("/api/bars").json()["bars"] == whole["bars"]
    assert client.get("/api/boxes").json()["boxes"] == whole["boxes"]
    assert client.get("/api/series").json()["series"] == whole["series"]


def test_pages_and_assets_are_served(client):
    dashboard.ensure_lwc_bundle()  # raises with the curl command if unvendored
    assert client.get("/").status_code == 200
    assert client.get("/plotly").status_code == 200


def test_missing_code_is_a_404(client):
    assert client.get("/api/chart?code=NOPE").status_code == 404


@pytest.mark.skipif(
    not db.DEFAULT_DB_PATH.exists(), reason="needs the recorded data/tmf.db"
)
def test_real_db_box_18_extends_past_the_last_bar(monkeypatch):
    """The case that silently breaks a naive implementation.

    Box 18's recorded right edge is 05:00 while the last hourly bar is 04:00, so
    its right edge is not a bar timestamp at all.  ``timeToCoordinate`` would
    return null for it and the box would vanish; the browser uses
    ``timeToIndex(..., findNearest=True)`` instead, and the server marks it open.
    """
    monkeypatch.setitem(dashboard.CONFIG, "db_path", db.DEFAULT_DB_PATH)
    dashboard._LOAD_CACHE.clear()
    payload = TestClient(dashboard.app).get("/api/chart?code=TMFR1&days=30").json()
    dashboard._LOAD_CACHE.clear()

    assert len(payload["bars"]) > 300
    assert payload["bars"][0]["time"] == 1788926400  # 2026-09-09 08:00 Taipei
    open_boxes = [box for box in payload["boxes"] if box["open"]]
    assert open_boxes, "the newest box of a side should still be advancing"
    assert all(len(s["data"]) == len(payload["bars"]) for s in payload["series"])
