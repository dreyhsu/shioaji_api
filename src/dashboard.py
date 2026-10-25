"""Read-only dashboard over the monitor's database.

Deliberately separate from ``monitor.py`` and deliberately read-only: it never
touches Shioaji, so a browser left open overnight cannot disturb data collection,
and if the dashboard crashes the monitor keeps recording.  sqlite runs in WAL mode
so the two overlap without blocking.

The page re-fetches once a minute, which matches how often the monitor has
anything new.

``/api/state`` carries a ``stale_minutes`` field: the page turns red when the data
stops advancing, which is the only way to notice from the browser that the monitor
has died.

::

    /opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python \
        src/dashboard.py --code TMFR1 --port 8765
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from typing import Literal

from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import market_clock
import session_bars as sb
import snr_core as snr
import trendline as tl

WEB_DIR = Path(__file__).resolve().parent / "web"
LWC_BUNDLE = WEB_DIR / "lightweight-charts.standalone.production.js"
LWC_VERSION = "5.2.1"
LWC_URL = (
    "https://cdn.jsdelivr.net/npm/lightweight-charts@"
    f"{LWC_VERSION}/dist/lightweight-charts.standalone.production.js"
)

UP_COLOR = "#20ca26"
DOWN_COLOR = "#e92929"
TRENDLINE_COLOR = "#f5c542"

# ``_load`` re-reads every minute bar and resamples, so one browser poll hitting
# four endpoints would do that work four times over.  The monitor only has
# something new once a minute; five seconds is enough to collapse a poll's worth
# of requests into a single read while never showing a stale price.
_LOAD_TTL_SECONDS = 5.0
_LOAD_CACHE: dict[tuple[str, int], tuple[float, object]] = {}

app = FastAPI(title="TMF S/R monitor")
CONFIG = {"code": "TMFR1", "db_path": db.DEFAULT_DB_PATH, "days": 30}


def ensure_lwc_bundle() -> None:
    """lightweight-charts ships no Python package, so the file is vendored by hand.

    Vendored rather than loaded from a CDN because a monitoring page that goes
    blank when the network hiccups is worse than no page at all.
    """
    if LWC_BUNDLE.exists():
        return
    raise SystemExit(
        f"missing {LWC_BUNDLE}\n  fetch it once with:\n"
        f"  curl -fsSL -o {LWC_BUNDLE} {LWC_URL}"
    )


def _cached_load(code: str, days: int):
    key = (code, days)
    now = time.monotonic()
    cached = _LOAD_CACHE.get(key)
    if cached is not None and now - cached[0] < _LOAD_TTL_SECONDS:
        return cached[1]
    data = _load(code, days)
    _LOAD_CACHE[key] = (now, data)
    return data


def _epoch(stamp) -> int:
    """Seconds since the epoch, reading a naive Taipei stamp *as* UTC.

    lightweight-charts has no timezone support -- it renders every UTCTimestamp
    as UTC -- and every timestamp in tmf.db is naive Taipei wall clock.  So
    taking the naive value at face value is exactly what makes the axis print
    Taipei time.  Adding an 8h offset here would push the labels to 16:45.
    """
    return int(pd.Timestamp(stamp).value // 10**9)


def _epochs(stamps: pd.Series) -> list[int]:
    return [int(value) for value in pd.to_datetime(stamps).astype("int64") // 10**9]


def _bars_payload(data) -> list[dict]:
    hourly, visible = data["hourly"], data["visible"]
    tail = len(visible)
    # delta volume holds its direction across doji bars, so it has to be computed
    # on the full history and sliced afterwards -- computing it on the visible
    # window alone would guess the sign of the first bar.
    delta = snr.delta_volume(hourly).iloc[-tail:].to_numpy()
    times = _epochs(visible["timestamp"])
    return [
        {
            "time": times[i],
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "delta_volume": float(delta[i]),
        }
        for i, row in enumerate(visible.itertuples(index=False))
    ]


def _boxes_payload(data) -> list[dict]:
    """Boxes clipped to the visible window, with an explicit ``open`` flag.

    ``open`` says "this box's right edge is still advancing", which the browser
    draws as "extend to the right edge of the pane".  It is decided here, off the
    same frame the bars come from: inferring it in the browser from
    ``right_time == last_bar_time`` would break for one poll every time an hourly
    bar closes between the two requests.
    """
    boxes, visible = data["boxes"], data["visible"]
    if boxes.empty:
        return []
    start = visible["timestamp"].iloc[0]
    end = visible["timestamp"].iloc[-1]
    clipped = snr.clip_boxes(boxes, start, end)
    overrides = data.get("box_overrides") or {}
    rows = []
    for box in clipped.itertuples(index=False):
        rows.append(
            {
                "box_id": int(box.box_id),
                "box_type": str(box.box_type),
                "state": str(box.state),
                "top": float(box.top),
                "bottom": float(box.bottom),
                "left_time": _epoch(box.left_time),
                "right_time": _epoch(box.right_time),
                "open": bool(pd.Timestamp(box.right_time) >= end),
                "holds": int(box.holds),
                "breakouts": int(box.breakouts),
                "delta_volume": float(box.delta_volume),
                "created_time": _json_safe(box.created_time),
                "adjusted": (str(box.box_type), pd.Timestamp(box.created_time)) in overrides,
            }
        )
    return rows


def _series_payload(data) -> list[dict]:
    """Indicator curves as a generic, open-ended list.

    The browser builds whatever this returns by ``type`` and ``pane``, so a new
    indicator is a change here and nowhere else.  Panes: 0 price, 1 volume.
    Everything is computed on the full history and sliced to the window, so
    rolling windows are not distorted by the window edge.

    ``autoscale: False`` keeps a series out of the price scale's fit: the zoom
    follows the candles, not a trendline extended far off-screen.
    """
    hourly, visible = data["hourly"], data["visible"]
    tail = len(visible)
    times = _epochs(visible["timestamp"])
    rising = (visible["close"] >= visible["open"]).to_numpy()

    specs = [
        {
            "name": "volume",
            "label": "volume",
            "pane": 1,
            "type": "histogram",
            "color": UP_COLOR,
            "values": hourly["volume"].astype(float),
            "color_by_candle": True,
            "precision": 0,
        },
    ]
    for line in data.get("trendlines") or []:
        specs.append(
            {
                "name": f"trendline_{line['id']}",
                "label": line.get("label") or tl.default_label(line),
                "pane": 0,
                "type": "line",
                "color": line.get("color") or TRENDLINE_COLOR,
                "line_width": 2,
                "last_value_visible": True,
                "autoscale": False,
                "values": tl.line_values(hourly, line["anchors"], line.get("kind") or "line"),
                "precision": 0,
            }
        )

    out = []
    for spec in specs:
        values = spec["values"].iloc[-tail:].to_numpy()
        points = []
        for index, stamp in enumerate(times):
            value = values[index]
            if pd.isna(value):
                # Whitespace: keeps every series on the candles' time grid, which
                # is what makes bar N sit at logical index N for all of them.
                points.append({"time": stamp})
                continue
            point = {"time": stamp, "value": float(value)}
            if spec.get("color_by_candle"):
                point["color"] = UP_COLOR if rising[index] else DOWN_COLOR
            points.append(point)
        out.append(
            {
                "name": spec["name"],
                "label": spec["label"],
                "pane": spec["pane"],
                "type": spec["type"],
                "color": spec["color"],
                "line_width": spec.get("line_width", 1),
                "last_value_visible": spec.get("last_value_visible", True),
                "autoscale": spec.get("autoscale", True),
                "price_format": {
                    "type": "price",
                    "precision": spec["precision"],
                    "minMove": 10 ** -spec["precision"],
                },
                "data": points,
            }
        )
    return out


def _json_safe(value):
    """pandas/numpy scalars and NaT/NaN are not JSON-serialisable as-is."""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return None if pd.isna(value) else value.isoformat(sep=" ")
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:  # noqa: BLE001
            return str(value)
    return value


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    return [
        {key: _json_safe(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _load(code: str, days: int):
    """Hourly bars, boxes and the ATR for the window the page shows."""
    with db.session(CONFIG["db_path"]) as conn:
        minute_bars = db.load_minute_bars(conn, code)
        reversals = db.load_reversals(conn, code, limit=300)
        boxes_db = db.load_boxes(conn, code)
        visits = db.load_visits(conn, code, limit=30)
        alerts = db.load_alerts(conn, code, limit=30)
        book_rows = db.count_orderbook_rows(conn, code)
        trendlines = db.load_trendlines(conn, code)
        box_overrides = db.load_box_overrides(conn, code)

    if minute_bars.empty:
        return None

    hourly = sb.minute_bars_to_hourly(minute_bars)
    cutoff = hourly["hour_start"].max() - pd.Timedelta(days=days)
    visible = hourly[hourly["hour_start"] >= cutoff].reset_index(drop=True)

    atr = None
    if len(hourly) > snr.DEFAULT_ATR_LENGTH:
        series = snr.pine_atr(hourly, snr.DEFAULT_ATR_LENGTH)
        raw = series.iloc[-1]
        atr = float(raw) if pd.notna(raw) else None

    return {
        "minute_bars": minute_bars,
        "hourly": hourly,
        "visible": visible,
        "boxes": boxes_db,
        "reversals": reversals,
        "visits": visits,
        "alerts": alerts,
        "book_rows": book_rows,
        "atr": atr,
        "trendlines": trendlines,
        "box_overrides": box_overrides,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "lwc.html")


@app.get("/api/chart")
def api_chart(
    code: str | None = None, days: int = Query(default=0, ge=0, le=365)
) -> dict:
    """Bars, boxes and indicator curves from a single read, so they agree.

    One endpoint rather than three because the box edges are expressed in bar
    times: if an hourly bar closed between separate requests, the bars and the
    boxes would disagree about where the right edge of the chart is.
    """
    code = code or CONFIG["code"]
    data = _cached_load(code, days or CONFIG["days"])
    if data is None:
        raise HTTPException(status_code=404, detail=f"no minute bars stored for {code}")
    return {
        "code": code,
        "timeframe": "1h",
        "generated_at": market_clock.now().isoformat(sep=" "),
        "bars": _bars_payload(data),
        "boxes": _boxes_payload(data),
        "series": _series_payload(data),
        "trendlines": data.get("trendlines") or [],
    }


@app.get("/api/bars")
def api_bars(
    code: str | None = None, days: int = Query(default=0, ge=0, le=365)
) -> dict:
    payload = api_chart(code=code, days=days)
    return {k: payload[k] for k in ("code", "timeframe", "generated_at", "bars")}


@app.get("/api/boxes")
def api_boxes(
    code: str | None = None, days: int = Query(default=0, ge=0, le=365)
) -> dict:
    payload = api_chart(code=code, days=days)
    return {k: payload[k] for k in ("code", "generated_at", "boxes")}


@app.get("/api/series")
def api_series(
    code: str | None = None, days: int = Query(default=0, ge=0, le=365)
) -> dict:
    payload = api_chart(code=code, days=days)
    return {k: payload[k] for k in ("code", "generated_at", "series")}


# --------------------------------------------------------------------------------------
# Trendlines and box edits -- the only writes the dashboard makes.  sqlite runs in
# WAL mode, so they do not block the monitor; the monitor picks them up next cycle.
# --------------------------------------------------------------------------------------
class Anchor(BaseModel):
    time: int
    price: float


class TrendlineIn(BaseModel):
    code: str | None = None
    label: str | None = Field(default=None, max_length=40)
    color: str | None = Field(default=None, max_length=20)
    anchors: list[Anchor] = Field(min_length=2, max_length=20)
    kind: Literal["line", "curve"] = "line"


def _invalidate_cache() -> None:
    _LOAD_CACHE.clear()


@app.get("/api/trendlines")
def api_trendlines(code: str | None = None) -> list[dict]:
    code = code or CONFIG["code"]
    with db.session(CONFIG["db_path"]) as conn:
        return db.load_trendlines(conn, code)


@app.post("/api/trendlines")
def api_create_trendline(body: TrendlineIn) -> dict:
    code = body.code or CONFIG["code"]
    anchors = [anchor.model_dump() for anchor in body.anchors]
    if len({anchor["time"] for anchor in anchors}) != len(anchors):
        raise HTTPException(status_code=422, detail="anchors must be on different bars")
    if body.kind == "curve" and len(anchors) != 3:
        raise HTTPException(status_code=422, detail="a curve takes exactly 3 anchors")
    with db.session(CONFIG["db_path"]) as conn:
        line_id = db.insert_trendline(
            conn, code, anchors, label=body.label, color=body.color, kind=body.kind
        )
    _invalidate_cache()
    return {"id": line_id}


@app.delete("/api/trendlines/{line_id}")
def api_delete_trendline(line_id: int) -> dict:
    with db.session(CONFIG["db_path"]) as conn:
        if not db.delete_trendline(conn, line_id):
            raise HTTPException(status_code=404, detail=f"no trendline {line_id}")
    _invalidate_cache()
    return {"deleted": line_id}


class BoxOverrideIn(BaseModel):
    code: str | None = None
    box_type: Literal["support", "resistance"]
    created_time: str
    top: float
    bottom: float


@app.put("/api/boxes/override")
def api_set_box_override(body: BoxOverrideIn) -> dict:
    code = body.code or CONFIG["code"]
    if not body.top > body.bottom:
        raise HTTPException(status_code=422, detail="top must be above bottom")
    with db.session(CONFIG["db_path"]) as conn:
        if not db.set_box_override(
            conn, code, body.box_type, body.created_time, body.top, body.bottom
        ):
            raise HTTPException(status_code=404, detail="no such box")
    _invalidate_cache()
    return {"box_type": body.box_type, "created_time": body.created_time, "top": body.top, "bottom": body.bottom}


@app.delete("/api/boxes/override")
def api_delete_box_override(
    box_type: Literal["support", "resistance"], created_time: str, code: str | None = None
) -> dict:
    code = code or CONFIG["code"]
    with db.session(CONFIG["db_path"]) as conn:
        if not db.delete_box_override(conn, code, box_type, created_time):
            raise HTTPException(status_code=404, detail="no override for that box")
    _invalidate_cache()
    return {"deleted": created_time}


@app.get("/api/state")
def api_state(code: str | None = None) -> dict:
    code = code or CONFIG["code"]
    data = _cached_load(code, CONFIG["days"])
    if data is None:
        return {"code": code, "ready": False, "message": "no minute bars stored yet"}

    minute_bars = data["minute_bars"]
    last_minute = minute_bars.iloc[-1]
    last_price = float(last_minute["close"])
    last_stamp = pd.Timestamp(last_minute["minute_start"])
    stale_minutes = (market_clock.now() - last_stamp).total_seconds() / 60.0

    sides = {}
    for box_type in ("support", "resistance"):
        box = snr.current_box(data["boxes"], box_type)
        if box is None:
            sides[box_type] = None
            continue
        pct = snr.zone_pct(last_price, box)
        sides[box_type] = {
            "box_id": int(box["box_id"]),
            "state": str(box["state"]),
            "top": float(box["top"]),
            "bottom": float(box["bottom"]),
            "height": snr.box_height(box),
            "zone_pct": None if pd.isna(pct) else float(pct),
            "inside": bool(0.0 <= pct <= 1.0) if pd.notna(pct) else False,
        }

    open_visits = data["visits"][data["visits"]["is_open"] == 1]
    book_source = None
    if data["book_rows"]:
        with db.session(CONFIG["db_path"]) as conn:
            row = conn.execute(
                "SELECT source, levels FROM orderbook_snapshots WHERE code = ? "
                "ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
        if row:
            book_source = {"source": row["source"], "levels": row["levels"]}

    return {
        "code": code,
        "ready": True,
        "server_time": market_clock.now().isoformat(sep=" "),
        "last_minute_start": last_stamp.isoformat(sep=" "),
        "last_price": last_price,
        "stale_minutes": round(stale_minutes, 1),
        "in_session": sb.is_trading_time(),
        "hourly_bars": len(data["hourly"]),
        "minute_bars": len(minute_bars),
        "hourly_atr": data["atr"],
        "sides": sides,
        "open_visits": _records(open_visits),
        "recent_visits": _records(data["visits"].head(15)),
        "recent_reversals": _records(data["reversals"].head(15)),
        "recent_alerts": _records(data["alerts"].head(15)),
        "orderbook_rows": data["book_rows"],
        "book_source": book_source,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--code", default="TMFR1")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--days", type=int, default=30, help="how much history to chart")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    args = parse_args(argv)
    CONFIG.update({"code": args.code, "db_path": args.db, "days": args.days})
    ensure_lwc_bundle()
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    print(f"dashboard for {args.code} -> http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
