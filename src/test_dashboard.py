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
    # Price + volume only: the delta-volume and ATR sub-charts are gone.
    assert names == {"volume"}

    for spec in payload["series"]:
        # Equal length and identical timestamps: a series on its own grid would
        # shift the logical index of every bar, and the boxes are positioned by
        # logical index.
        assert [point["time"] for point in spec["data"]] == times, spec["name"]
        assert spec["pane"] in (0, 1)
        assert spec["type"] in ("line", "histogram", "area", "baseline")

    volume = next(s for s in payload["series"] if s["name"] == "volume")
    assert volume["pane"] == 1 and volume["type"] == "histogram"
    assert volume["autoscale"] is True
    for bar, point in zip(payload["bars"], volume["data"]):
        assert point["value"] == bar["volume"]
        expected = dashboard.UP_COLOR if bar["close"] >= bar["open"] else dashboard.DOWN_COLOR
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


@pytest.mark.parametrize("timeframe,step", [("1m", 60), ("5m", 300), ("15m", 900)])
def test_chart_timeframes_keep_hourly_boxes_and_align_volume(client, timeframe, step):
    hourly = client.get("/api/chart?days=1").json()
    query = f"?days=1&timeframe={timeframe}"
    response = client.get("/api/chart" + query)
    assert response.status_code == 200
    payload = response.json()
    assert payload["timeframe"] == timeframe
    bars = payload["bars"]
    assert len(bars) > len(hourly["bars"])
    times = [bar["time"] for bar in bars]
    assert times == sorted(set(times))
    assert times[1] - times[0] == step
    assert all(sb.session_fields(pd.Timestamp(t, unit="s")) for t in times)
    volume = next(s for s in payload["series"] if s["name"] == "volume")
    assert [p["time"] for p in volume["data"]] == times
    assert [p["value"] for p in volume["data"]] == [bar["volume"] for bar in bars]
    hourly_boxes = {b["box_id"]: b for b in hourly["boxes"]}
    for box in payload["boxes"]:
        reference = hourly_boxes[box["box_id"]]
        assert (box["top"], box["bottom"]) == (reference["top"], reference["bottom"])
        assert times[0] <= box["left_time"] <= box["right_time"] <= times[-1]
    assert any(box["open"] for box in payload["boxes"])
    assert client.get("/api/bars" + query).json()["bars"] == bars
    assert client.get("/api/series" + query).json()["series"] == payload["series"]
    assert client.get("/api/boxes" + query).json()["boxes"] == payload["boxes"]
    # Changing views must not replace the cached hourly data used by the monitor.
    assert client.get("/api/chart?days=1").json()["bars"] == hourly["bars"]


def test_invalid_timeframe_is_rejected(client):
    assert client.get("/api/chart?timeframe=2m").status_code == 422


def test_hourly_drawing_keeps_its_values_on_minute_views(client):
    bars = client.get("/api/chart?days=1").json()["bars"]
    a, b = bars[-3]["time"], bars[-2]["time"]
    created = client.post("/api/trendlines", json={"anchors": [
        {"time": a, "price": 20000}, {"time": b, "price": 20120}
    ]})
    line_id = created.json()["id"]
    payload = client.get("/api/chart?days=1&timeframe=5m").json()
    spec = next(s for s in payload["series"] if s["name"] == f"trendline_{line_id}")
    assert [p["time"] for p in spec["data"]] == [bar["time"] for bar in payload["bars"]]
    values = {point["time"]: point.get("value") for point in spec["data"]}
    assert values[a] == pytest.approx(20000)
    assert values[a + 300] == pytest.approx(20010)
    assert values[b] == pytest.approx(20120)
    assert values[b + 300] == pytest.approx(20130)


def test_pages_and_assets_are_served(client):
    dashboard.ensure_lwc_bundle()  # raises with the curl command if unvendored
    assert client.get("/").status_code == 200
    assert client.get("/plotly").status_code == 404
    assert client.get("/api/figure").status_code == 404


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


def test_trendline_round_trip_renders_on_the_price_pane(client):
    bars = client.get("/api/chart").json()["bars"]
    a, b = bars[-20]["time"], bars[-10]["time"]
    assert client.post(
        "/api/trendlines", json={"anchors": [{"time": a, "price": 1}]}
    ).status_code == 422
    created = client.post(
        "/api/trendlines",
        json={"anchors": [{"time": b, "price": 20100}, {"time": a, "price": 20000}]},
    )
    assert created.status_code == 200
    line_id = created.json()["id"]

    payload = client.get("/api/chart").json()
    assert [line["id"] for line in payload["trendlines"]] == [line_id]
    spec = next(s for s in payload["series"] if s["name"] == f"trendline_{line_id}")
    assert spec["pane"] == 0
    # Kept out of the price autoscale, so the zoom fits the candles.
    assert spec["autoscale"] is False
    assert [p["time"] for p in spec["data"]] == [bar["time"] for bar in payload["bars"]]
    values = [p.get("value") for p in spec["data"]]
    assert values[-20] == pytest.approx(20000)
    assert values[-10] == pytest.approx(20100)
    assert values[-1] == pytest.approx(20190)  # extended right at +10/bar
    assert values[-21] is None

    assert client.delete(f"/api/trendlines/{line_id}").status_code == 200
    assert client.delete(f"/api/trendlines/{line_id}").status_code == 404
    assert client.get("/api/chart").json()["trendlines"] == []


def test_curve_round_trip(client):
    bars = client.get("/api/chart").json()["bars"]
    t0, t1, t2 = bars[-30]["time"], bars[-20]["time"], bars[-10]["time"]
    bad = client.post(
        "/api/trendlines",
        json={"kind": "curve", "anchors": [{"time": t0, "price": 1}, {"time": t2, "price": 2}]},
    )
    assert bad.status_code == 422
    created = client.post(
        "/api/trendlines",
        json={
            "kind": "curve",
            "anchors": [
                {"time": t2, "price": 20200},
                {"time": t0, "price": 20000},
                {"time": t1, "price": 20000},
            ],
        },
    )
    line_id = created.json()["id"]
    payload = client.get("/api/chart").json()
    assert payload["trendlines"][0]["kind"] == "curve"
    spec = next(s for s in payload["series"] if s["name"] == f"trendline_{line_id}")
    assert spec["label"] == f"曲線 #{line_id}"
    values = [p.get("value") for p in spec["data"]]
    assert values[-30] == pytest.approx(20000)
    assert values[-20] == pytest.approx(20050)  # bent: chord would be 20100
    assert values[-10] == pytest.approx(20200)
    assert values[-9] == pytest.approx(20220)  # end tangent: +200 / 10 bars


def test_box_override_round_trip(client):
    live = next(b for b in client.get("/api/chart").json()["boxes"] if b["open"])
    body = {
        "box_type": live["box_type"],
        "created_time": live["created_time"],
        "top": live["top"] + 10,
        "bottom": live["bottom"] - 10,
    }
    assert client.put("/api/boxes/override", json={**body, "top": 1, "bottom": 2}).status_code == 422
    assert client.put("/api/boxes/override", json={**body, "created_time": "2000-01-01 00:00:00"}).status_code == 404
    assert client.put("/api/boxes/override", json=body).status_code == 200

    edited = next(b for b in client.get("/api/chart").json()["boxes"] if b["box_id"] == live["box_id"])
    assert edited["adjusted"] is True
    assert (edited["top"], edited["bottom"]) == (body["top"], body["bottom"])

    params = {"box_type": live["box_type"], "created_time": live["created_time"]}
    assert client.delete("/api/boxes/override", params=params).status_code == 200
    assert client.delete("/api/boxes/override", params=params).status_code == 404
    reset = next(b for b in client.get("/api/chart").json()["boxes"] if b["box_id"] == live["box_id"])
    assert reset["adjusted"] is False
    assert (reset["top"], reset["bottom"]) == (live["top"], live["bottom"])
