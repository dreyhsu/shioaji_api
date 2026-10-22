"""Read-only dashboard over the monitor's database.

Deliberately separate from ``monitor.py`` and deliberately read-only: it never
touches Shioaji, so a browser left open overnight cannot disturb data collection,
and if the dashboard crashes the monitor keeps recording.  sqlite runs in WAL mode
so the two overlap without blocking.

The page re-fetches once a minute, which matches how often the monitor has
anything new.  At that cadence a whole-figure ``Plotly.react`` over ~800 bars is
imperceptible, so there is no incremental-update machinery to get wrong.

``/api/state`` carries a ``stale_minutes`` field: the page turns red when the data
stops advancing, which is the only way to notice from the browser that the monitor
has died.

::

    /opt/homebrew/Caskroom/miniconda/base/bin/conda run -n tss python \
        src/dashboard.py --code TMFR1 --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.io as pio
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db
import session_bars as sb
import snr_core as snr

WEB_DIR = Path(__file__).resolve().parent / "web"
PLOTLY_BUNDLE = WEB_DIR / "plotly.min.js"
LWC_BUNDLE = WEB_DIR / "lightweight-charts.standalone.production.js"
LWC_VERSION = "5.2.1"
LWC_URL = (
    "https://cdn.jsdelivr.net/npm/lightweight-charts@"
    f"{LWC_VERSION}/dist/lightweight-charts.standalone.production.js"
)

UP_COLOR = "#20ca26"
DOWN_COLOR = "#e92929"

# ``_load`` re-reads every minute bar and resamples, so one browser poll hitting
# four endpoints would do that work four times over.  The monitor only has
# something new once a minute; five seconds is enough to collapse a poll's worth
# of requests into a single read while never showing a stale price.
_LOAD_TTL_SECONDS = 5.0
_LOAD_CACHE: dict[tuple[str, int], tuple[float, object]] = {}

app = FastAPI(title="TMF S/R monitor")
CONFIG = {"code": "TMFR1", "db_path": db.DEFAULT_DB_PATH, "days": 30}


def ensure_plotly_bundle() -> None:
    """Vendor plotly.js out of the installed package so the page works offline."""
    if PLOTLY_BUNDLE.exists():
        return
    from plotly.offline import get_plotlyjs

    WEB_DIR.mkdir(parents=True, exist_ok=True)
    PLOTLY_BUNDLE.write_text(get_plotlyjs(), encoding="utf-8")


def ensure_lwc_bundle() -> None:
    """lightweight-charts ships no Python package, so the file is vendored by hand.

    Vendored rather than loaded from a CDN for the same reason plotly is: a
    monitoring page that goes blank when the network hiccups is worse than no
    page at all.
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
            }
        )
    return rows


def _series_payload(
    data, volume_length: int = snr.DEFAULT_VOLUME_LENGTH
) -> list[dict]:
    """Indicator curves as a generic, open-ended list.

    The browser builds whatever this returns by ``type`` and ``pane``, so a new
    indicator is a change here and nowhere else.  Panes: 0 price, 1 volume,
    2 oscillators.  Everything is computed on the full history and sliced to the
    window, so rolling windows are not distorted by the window edge.
    """
    hourly, visible = data["hourly"], data["visible"]
    tail = len(visible)
    times = _epochs(visible["timestamp"])

    scaled = snr.delta_volume(hourly) / 2.5
    specs = [
        {
            "name": "delta_volume",
            "label": "delta volume / 2.5",
            "pane": 1,
            "type": "histogram",
            "color": UP_COLOR,
            "values": scaled,
            "color_by_sign": True,
            "precision": 0,
        },
        {
            "name": "support_filter",
            "label": "support filter (highest)",
            "pane": 1,
            "type": "line",
            "color": snr.SUPPORT_COLOR,
            "values": scaled.rolling(volume_length, min_periods=volume_length).max(),
            "precision": 0,
        },
        {
            "name": "resistance_filter",
            "label": "resistance filter (lowest)",
            "pane": 1,
            "type": "line",
            "color": snr.RESISTANCE_COLOR,
            "values": scaled.rolling(volume_length, min_periods=volume_length).min(),
            "precision": 0,
        },
        {
            "name": f"atr{snr.DEFAULT_ATR_LENGTH}",
            "label": f"ATR({snr.DEFAULT_ATR_LENGTH})",
            "pane": 2,
            "type": "line",
            "color": "#8ab4f8",
            "values": snr.pine_atr(hourly, snr.DEFAULT_ATR_LENGTH),
            "precision": 1,
        },
    ]

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
            if spec.get("color_by_sign"):
                point["color"] = UP_COLOR if value >= 0 else DOWN_COLOR
            points.append(point)
        out.append(
            {
                "name": spec["name"],
                "label": spec["label"],
                "pane": spec["pane"],
                "type": spec["type"],
                "color": spec["color"],
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
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "lwc.html")


@app.get("/plotly")
def plotly_page() -> FileResponse:
    """The old server-rendered Plotly page, kept as a reference to diff against."""
    return FileResponse(WEB_DIR / "index.html")


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
        "generated_at": pd.Timestamp.now().isoformat(sep=" "),
        "bars": _bars_payload(data),
        "boxes": _boxes_payload(data),
        "series": _series_payload(data),
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


@app.get("/api/figure")
def api_figure(
    code: str | None = None, days: int = Query(default=0, ge=0, le=365)
) -> Response:
    code = code or CONFIG["code"]
    window = days or CONFIG["days"]
    data = _cached_load(code, window)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no minute bars stored for {code}")

    visible = data["visible"]
    boxes = snr.clip_boxes(
        data["boxes"], visible["timestamp"].iloc[0], visible["timestamp"].iloc[-1]
    ) if not data["boxes"].empty else data["boxes"]

    span = f"{visible['hour_start'].iloc[0]:%Y-%m-%d} to {visible['hour_start'].iloc[-1]:%Y-%m-%d}"
    figure = snr.build_figure(
        visible,
        boxes,
        title=f"{code} 1h Support &amp; Resistance boxes ({span})",
        instrument=code,
        reversals=data["reversals"] if not data["reversals"].empty else None,
    )
    # plotly.io.to_json is the only serializer that agrees with the bundled
    # plotly.js about dates and typed arrays -- a hand-rolled encoder emits
    # datetimes as raw nanosecond ints, which the browser reads as milliseconds
    # and plots tens of millions of years into the future.
    return Response(content=pio.to_json(figure), media_type="application/json")


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
    stale_minutes = (pd.Timestamp.now() - last_stamp).total_seconds() / 60.0

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
        "server_time": pd.Timestamp.now().isoformat(sep=" "),
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
    ensure_plotly_bundle()
    ensure_lwc_bundle()
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    print(f"dashboard for {args.code} -> http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
