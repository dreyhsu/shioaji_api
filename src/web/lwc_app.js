'use strict';
// The chart half of the dashboard, on lightweight-charts 5.2.1.
//
// Replaces the server-rendered Plotly figure: the server now sends plain data
// (/api/chart) and the drawing happens here.  That drops a 4.8 MB plotly bundle
// to a 195 KB one and makes pan/zoom a canvas redraw instead of an SVG rebuild.
//
// Note there is no equivalent of snr_core.session_rangebreaks here, and none is
// needed: lightweight-charts' horizontal scale is a *logical index* scale, so it
// only ever allocates one slot per data point.  TMF's two session gaps per day
// collapse for free.

const LWC = window.LightweightCharts;

const REFRESH_MS = 60000;
const INITIAL_BARS = 120; // ~6 trading days at 19 bars/day; 380 at once is unreadable
const PANE_PRICE = 0;
const PANE_VOLUME = 1;
// Height is directly proportional to the stretch factor, and pane 0 ships with
// a factor of 2 while new panes get 1 -- so both are always set explicitly.
const PANE_STRETCH = [8, 2];

const C = {
  bg: '#1a1c23',
  text: '#e6e8ee',
  muted: '#9aa0ae',
  line: '#2e323d',
  grid: '#23262f',
  up: '#26a69a',
  down: '#ef5350',
};

const params = new URLSearchParams(location.search);
const CODE = params.get('code') || '';
const DAYS = Number(params.get('days')) || 0;

let chart = null;
let candles = null;
let boxes = null;
let framed = false;
const indicators = new Map(); // name -> { series, spec }
let lastBars = [];

// ---------------------------------------------------------------- formatters
// Every timestamp is a naive Taipei stamp sent as if it were UTC, and the
// library formats UTCTimestamps with getUTC* accessors, so reading them back
// with getUTC* prints the original Taipei wall clock.
const pad2 = (n) => (n < 10 ? '0' + n : String(n));

function utcParts(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    hour: d.getUTCHours(),
    minute: d.getUTCMinutes(),
  };
}

// tickMarkType: 0 Year, 1 Month, 2 DayOfMonth, 3 Time, 4 TimeWithSeconds.
function tickMarkFormatter(time, tickMarkType) {
  const t = utcParts(time);
  switch (tickMarkType) {
    case 0:
      return String(t.year);
    case 1:
      return t.month + '月';
    case 2:
      return t.month + '/' + t.day;
    default:
      return pad2(t.hour) + ':' + pad2(t.minute);
  }
}

function crosshairTimeFormatter(time) {
  const t = utcParts(time);
  return t.month + '/' + t.day + ' ' + pad2(t.hour) + ':' + pad2(t.minute);
}

// ---------------------------------------------------------------- chart setup
function initChart() {
  chart = LWC.createChart(document.getElementById('chart'), {
    autoSize: true,
    layout: {
      background: { type: 'solid', color: C.bg },
      textColor: C.muted,
      fontSize: 11,
      fontFamily: '-apple-system, "Helvetica Neue", "PingFang TC", "Noto Sans TC", sans-serif',
      panes: {
        enableResize: true,
        separatorColor: C.line,
        separatorHoverColor: 'rgba(154, 160, 174, 0.25)',
      },
    },
    grid: {
      // The boxes are drawn in the 'bottom' z-bucket, which paints before the
      // grid as well as before the candles -- vertical grid lines would show
      // through all 18 of them.  Faint horizontals are still worth keeping.
      vertLines: { visible: false },
      horzLines: { color: C.grid },
    },
    crosshair: {
      mode: 0, // Normal. The default (Magnet) snaps to OHLC and fights reading a box edge.
      vertLine: { color: C.muted, width: 1, style: 3, labelBackgroundColor: '#2a2d36' },
      horzLine: { color: C.muted, width: 1, style: 3, labelBackgroundColor: '#2a2d36' },
    },
    rightPriceScale: { borderColor: C.line, scaleMargins: { top: 0.08, bottom: 0.08 } },
    timeScale: {
      borderColor: C.line,
      timeVisible: true,
      secondsVisible: false,
      barSpacing: 6,
      minBarSpacing: 0.5,
      rightOffset: 8,
      tickMarkFormatter: tickMarkFormatter,
    },
    localization: {
      locale: 'zh-TW',
      timeFormatter: crosshairTimeFormatter,
      priceFormatter: (price) => price.toFixed(0),
    },
  });

  // Pre-create the volume pane: addSeries(..., paneIndex) would create it, but a
  // pane whose last series is removed is deleted, so an indicator disappearing
  // from /api/chart would shuffle the layout.
  while (chart.panes().length < PANE_STRETCH.length) chart.addPane(true);
  const panes = chart.panes();
  for (let i = 0; i < PANE_STRETCH.length; i++) {
    panes[i].setStretchFactor(PANE_STRETCH[i]);
    if (i > 0 && panes[i].setPreserveEmptyPane) panes[i].setPreserveEmptyPane(true);
  }

  candles = chart.addSeries(
    LWC.CandlestickSeries,
    {
      upColor: C.up,
      downColor: C.down,
      borderUpColor: C.up,
      borderDownColor: C.down,
      wickUpColor: C.up,
      wickDownColor: C.down,
      priceFormat: { type: 'price', precision: 0, minMove: 1 },
    },
    PANE_PRICE,
  );

  boxes = new BoxesPrimitive({ showLabels: true });
  candles.attachPrimitive(boxes);

  chart.subscribeCrosshairMove(onCrosshairMove);
  listenForBoxEdits(document.getElementById('chart'));
  listenForDrawClicks(document.getElementById('chart'));
  chart.subscribeCrosshairMove(onDrawHover);
}

// -------------------------------------------------------- series registry
// /api/chart's series list is deliberately open-ended: a new indicator on the
// server shows up here with no front-end change, as long as its type is known.
const SERIES_TYPES = {
  line: () => LWC.LineSeries,
  histogram: () => LWC.HistogramSeries,
  area: () => LWC.AreaSeries,
  baseline: () => LWC.BaselineSeries,
};

function seriesOptions(spec) {
  const options = {
    priceLineVisible: false,
    lastValueVisible: spec.last_value_visible !== false,
    priceFormat: spec.price_format || { type: 'price', precision: 1, minMove: 0.1 },
    color: spec.color || C.muted,
  };
  if (spec.type === 'histogram') {
    options.base = spec.base === undefined ? 0 : spec.base;
  } else {
    options.lineWidth = spec.line_width || 1;
  }
  // Trendlines opt out, so the price zoom fits the candles rather than a line
  // extended far above or below them.
  if (spec.autoscale === false) options.autoscaleInfoProvider = () => null;
  return options;
}

function applySeries(specs) {
  const seen = new Set();
  for (const spec of specs) {
    const factory = SERIES_TYPES[spec.type];
    if (!factory) {
      console.warn('unknown series type', spec.type, 'for', spec.name);
      continue;
    }
    seen.add(spec.name);

    let entry = indicators.get(spec.name);
    if (entry && (entry.spec.type !== spec.type || entry.spec.pane !== spec.pane)) {
      chart.removeSeries(entry.series);
      indicators.delete(spec.name);
      entry = undefined;
    }
    if (!entry) {
      const pane = spec.pane === undefined ? PANE_VOLUME : spec.pane;
      entry = { series: chart.addSeries(factory(), seriesOptions(spec), pane), spec: spec };
      indicators.set(spec.name, entry);
    } else {
      entry.series.applyOptions(seriesOptions(spec));
      entry.spec = spec;
    }
    // Histogram items carry their own `color`, so the up/down colouring of delta
    // volume stays a server-side decision.
    entry.series.setData(spec.data);
  }

  for (const [name, entry] of indicators) {
    if (!seen.has(name)) {
      chart.removeSeries(entry.series);
      indicators.delete(name);
    }
  }
}

// ---------------------------------------------------------------- legend
function renderLegendRows() {
  const rows = [
    '<span class="lg"><i style="background:' + C.up + '"></i>' + (CODE || 'TMF') + ' 1h</span>',
  ];
  for (const [, entry] of indicators) {
    rows.push('<span class="lg"><i style="background:' + (entry.spec.color || C.muted) + '"></i>' + entry.spec.label + '</span>');
  }
  document.getElementById('legend-series').innerHTML = rows.join('');
}

function onCrosshairMove(param) {
  const el = document.getElementById('legend-ohlc');
  if (!param || !param.time || !param.seriesData) {
    el.textContent = '';
    return;
  }
  const bar = param.seriesData.get(candles);
  if (!bar) {
    el.textContent = '';
    return;
  }
  const change = bar.close - bar.open;
  const cls = change >= 0 ? 'up' : 'down';
  el.innerHTML =
    crosshairTimeFormatter(param.time) +
    '　開 ' + bar.open.toFixed(0) +
    ' 高 ' + bar.high.toFixed(0) +
    ' 低 ' + bar.low.toFixed(0) +
    ' 收 ' + bar.close.toFixed(0) +
    ' <b class="' + cls + '">' + (change >= 0 ? '+' : '') + change.toFixed(0) + '</b>';
}

// ---------------------------------------------------------------- viewport
function frameInitialViewport(barCount) {
  if (!barCount) return;
  const timeScale = chart.timeScale();
  timeScale.setVisibleLogicalRange({
    from: Math.max(-2, barCount - 1 - INITIAL_BARS),
    to: barCount - 1 + timeScale.options().rightOffset,
  });
}

// ---------------------------------------------------------------- trendlines
// Drawing only places anchors; the saved shape comes back from the server as a
// pane-0 series in /api/chart (trendline.line_values), so what is drawn is
// exactly what the monitor alerts on.  Anchors snap to the clicked bar's time:
// the maths is in bar-index space, and bar times are what map back to indices.
//
// Two tools:
//   line  -- 2 clicks = straight line, more = polyline; 完成 / Enter saves.
//   curve -- TradingView's Curve: click start, click end, then move the mouse to
//            bend it toward a control point and click to save.  The control must
//            sit between start and end in time (keeps one value per bar).
const draw = { kind: null, anchors: [], hover: null, preview: null, frame: 0 };
const TRENDLINE_COLOR = '#f5c542';
const HINTS = {
  line: '畫線模式：點 K 棒放錨點（2 點 = 直線，3+ 點 = 折線），按「完成」或 Enter 儲存 · Esc 取消',
  curve: [
    '曲線模式：點起點',
    '曲線模式：點終點',
    '曲線模式：移動滑鼠拉彎曲線，點一下儲存（控制點須在起點與終點之間）· Esc 取消',
  ],
};

function setDrawing(kind) {
  draw.kind = kind;
  draw.anchors = [];
  draw.hover = null;
  if (draw.preview) {
    chart.removeSeries(draw.preview);
    draw.preview = null;
  }
  for (const tool of ['line', 'curve']) {
    const button = document.getElementById('draw-' + tool);
    button.classList.toggle('active', kind === tool);
  }
  document.getElementById('draw-done').hidden = true;
  updateHint();
}

function updateHint() {
  const el = document.getElementById('draw-hint');
  el.hidden = !draw.kind;
  if (draw.kind === 'line') el.textContent = HINTS.line;
  else if (draw.kind === 'curve') el.textContent = HINTS.curve[Math.min(draw.anchors.length, 2)];
}

// Mirrors trendline._bezier_at, so the preview is the shape that will be saved.
function bezierAt(p0, p1, p2, x) {
  const a = p0.i - 2 * p1.i + p2.i;
  const b = 2 * (p1.i - p0.i);
  const c = p0.i - x;
  let t = Math.abs(a) < 1e-12 ? -c / b : (-b + Math.sqrt(Math.max(b * b - 4 * a * c, 0))) / (2 * a);
  t = Math.min(Math.max(t, 0), 1);
  return (1 - t) * (1 - t) * p0.price + 2 * (1 - t) * t * p1.price + t * t * p2.price;
}

function barIndex(time) {
  for (let i = lastBars.length - 1; i >= 0; i--) if (lastBars[i].time === time) return i;
  return -1;
}

// [start, control, end] in bar-index form, or null if the control is not
// strictly between start and end.
function curvePoints(anchors) {
  const [start, end, control] = anchors;
  const pts = [start, control, end].map((a) => ({ i: barIndex(a.time), price: a.price, time: a.time }));
  if (pts.some((p) => p.i < 0)) return null;
  if (pts[0].i > pts[2].i) [pts[0], pts[2]] = [pts[2], pts[0]];
  if (!(pts[0].i < pts[1].i && pts[1].i < pts[2].i)) return null;
  return pts;
}

function previewData() {
  if (draw.kind === 'curve') {
    const anchors = draw.anchors.slice();
    if (anchors.length === 2) {
      // Before the control is placed, show the straight chord (TradingView does too).
      anchors.push(draw.hover);
    }
    if (anchors.length === 3 && anchors[2]) {
      const pts = curvePoints(anchors);
      if (pts) {
        const data = [];
        for (let i = pts[0].i; i <= pts[2].i; i++) {
          data.push({ time: lastBars[i].time, value: bezierAt(pts[0], pts[1], pts[2], i) });
        }
        return data;
      }
    }
  }
  return draw.anchors
    .slice()
    .sort((a, b) => a.time - b.time)
    .map((a) => ({ time: a.time, value: a.price }));
}

function renderPreview() {
  if (!draw.preview) {
    draw.preview = chart.addSeries(
      LWC.LineSeries,
      {
        color: TRENDLINE_COLOR,
        lineWidth: 2,
        lineStyle: 2, // dashed until saved
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        pointMarkersVisible: draw.kind === 'line',
        // Kept out of autoscale: a preview that rescaled the axis would move the
        // price under the cursor while you are placing the point.
        autoscaleInfoProvider: () => null,
      },
      PANE_PRICE,
    );
  }
  draw.preview.setData(previewData());
  document.getElementById('draw-done').hidden = !(draw.kind === 'line' && draw.anchors.length >= 2);
  updateHint();
}

// Clicks come from raw pointer events, not chart.subscribeClick: the library
// swallows a second click that lands within its double-click window (even on a
// different bar), so placing anchors at a normal pace dropped every other one.
// A press that moves more than a few px is a pan, not a click.
function listenForDrawClicks(el) {
  let down = null;
  el.addEventListener('pointerdown', (event) => {
    down = draw.kind && event.button === 0 ? { x: event.clientX, y: event.clientY } : null;
  });
  el.addEventListener('pointerup', (event) => {
    if (!down) return;
    const moved = Math.hypot(event.clientX - down.x, event.clientY - down.y);
    down = null;
    if (moved > 4) return;
    const rect = el.getBoundingClientRect();
    onChartClick(anchorFromPoint(event.clientX - rect.left, event.clientY - rect.top));
  });
}

function anchorFromPoint(x, y) {
  if (y < 0 || y > chart.panes()[PANE_PRICE].getHeight()) return null;
  const logical = chart.timeScale().coordinateToLogical(x);
  if (logical === null) return null;
  const index = Math.round(logical);
  if (index < 0 || index >= lastBars.length) return null; // empty space right of the last bar
  const price = candles.coordinateToPrice(y);
  if (price === null) return null;
  return { time: lastBars[index].time, price: Math.round(price) };
}

function anchorFromParam(param) {
  if (!param || !param.point || param.time === undefined) return null;
  if (param.paneIndex !== undefined && param.paneIndex !== PANE_PRICE) return null;
  const price = candles.coordinateToPrice(param.point.y);
  if (price === null) return null;
  return { time: Number(param.time), price: Math.round(price) };
}

function onChartClick(anchor) {
  if (!draw.kind || !anchor) return;

  if (draw.kind === 'curve') {
    if (draw.anchors.length < 2) {
      if (draw.anchors.length === 1 && draw.anchors[0].time === anchor.time) return;
      draw.anchors.push(anchor);
      renderPreview();
      return;
    }
    if (!curvePoints([draw.anchors[0], draw.anchors[1], anchor])) return; // control out of range
    draw.anchors.push(anchor);
    saveDrawing();
    return;
  }

  // Re-clicking a bar moves that anchor instead of making a vertical segment.
  draw.anchors = draw.anchors.filter((a) => a.time !== anchor.time);
  draw.anchors.push(anchor);
  draw.anchors.sort((a, b) => a.time - b.time);
  renderPreview();
}

function onDrawHover(param) {
  if (draw.kind !== 'curve' || draw.anchors.length !== 2) return;
  const anchor = anchorFromParam(param);
  if (!anchor) return;
  if (draw.hover && draw.hover.time === anchor.time && draw.hover.price === anchor.price) return;
  draw.hover = anchor;
  // setData fires a crosshair move synchronously, so rendering from inside this
  // handler recursed until the stack overflowed.  Defer to the next frame.
  if (!draw.frame) {
    draw.frame = requestAnimationFrame(() => {
      draw.frame = 0;
      if (draw.kind === 'curve' && draw.anchors.length === 2) renderPreview();
    });
  }
}

async function saveDrawing() {
  const enough = draw.kind === 'curve' ? draw.anchors.length === 3 : draw.anchors.length >= 2;
  if (!enough) return;
  const body = { kind: draw.kind, anchors: draw.anchors };
  if (CODE) body.code = CODE;
  const response = await fetch('/api/trendlines', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    alert('儲存失敗：' + response.status);
    return;
  }
  setDrawing(null);
  refresh();
}

async function deleteTrendline(id) {
  if (!confirm('刪除 #' + id + '？')) return;
  const response = await fetch('/api/trendlines/' + id, { method: 'DELETE' });
  if (!response.ok && response.status !== 404) {
    alert('刪除失敗：' + response.status);
    return;
  }
  refresh();
}

function renderTrendlineList(lines) {
  const el = document.getElementById('trendlines');
  el.innerHTML = lines
    .map((line) => {
      const noun = line.kind === 'curve' ? '曲線' : '趨勢線';
      return (
        '<span class="tl"><i style="background:' + (line.color || TRENDLINE_COLOR) + '"></i>' +
        (line.label || noun + ' #' + line.id) +
        (line.kind === 'curve' ? '' : ' <small>(' + line.anchors.length + ' 點)</small>') +
        '<button type="button" data-id="' + line.id + '" title="刪除">✕</button></span>'
      );
    })
    .join('');
}

document.getElementById('trendlines').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-id]');
  if (button) deleteTrendline(Number(button.dataset.id));
});
for (const tool of ['line', 'curve']) {
  document.getElementById('draw-' + tool).addEventListener('click', () => {
    setDrawing(draw.kind === tool ? null : tool);
  });
}
document.getElementById('draw-done').addEventListener('click', saveDrawing);
document.addEventListener('keydown', (event) => {
  if (!draw.kind) return;
  if (event.key === 'Escape') setDrawing(null);
  else if (event.key === 'Enter' && draw.kind === 'line') saveDrawing();
});

// ---------------------------------------------------------------- box edits
// The live support and resistance boxes can be reshaped by hand: drag the top
// or bottom edge to move just that edge, drag the body to move the whole box,
// double-click an edited box to reset it.  The edit is saved per box
// (/api/boxes/override) and the monitor recomputes the box from it, so the
// alerts follow the edges you set, not the computed ones.
//
// Listeners are on the capture phase so a press on a box never reaches the
// library: otherwise the same drag would also pan the chart.
const MIN_BOX_HEIGHT = 1;
const boxDrag = { hit: null, startY: 0, startTop: 0, startBottom: 0, moved: false, autoScale: true };

function boxHitAt(el, event) {
  if (draw.kind) return null; // drawing a trendline owns the clicks
  const rect = el.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const pane = chart.panes()[PANE_PRICE];
  if (y < 0 || y > pane.getHeight()) return null;
  const paneWidth = chart.timeScale().width();
  if (x < 0 || x > paneWidth) return null; // the price axis
  return boxes.hitTest(x, y, paneWidth);
}

function boxCursor(hit) {
  if (!hit) return '';
  return hit.part === 'body' ? 'grab' : 'ns-resize';
}

function listenForBoxEdits(el) {
  el.addEventListener(
    'pointerdown',
    (event) => {
      if (event.button !== 0) return;
      const hit = boxHitAt(el, event);
      if (!hit) return;
      event.stopPropagation();
      event.preventDefault();
      el.setPointerCapture(event.pointerId);
      boxDrag.hit = hit;
      boxDrag.startY = event.clientY;
      boxDrag.startTop = hit.box.top;
      boxDrag.startBottom = hit.box.bottom;
      boxDrag.moved = false;
      // Freeze the price axis: the live boxes vote in autoscale, so without this
      // the scale would rescale under the cursor while the edge moves.
      const scale = candles.priceScale();
      boxDrag.autoScale = scale.options().autoScale;
      scale.applyOptions({ autoScale: false });
      el.style.cursor = hit.part === 'body' ? 'grabbing' : 'ns-resize';
    },
    true,
  );

  // The library listens for mouse/touch events, not pointer events, so those
  // have to be stopped as well while a box is under the press.
  for (const type of ['mousedown', 'touchstart']) {
    el.addEventListener(
      type,
      (event) => {
        if (boxDrag.hit) event.stopPropagation();
      },
      true,
    );
  }

  el.addEventListener(
    'pointermove',
    (event) => {
      if (!boxDrag.hit) {
        if (event.buttons === 0) el.style.cursor = boxCursor(boxHitAt(el, event));
        return;
      }
      event.stopPropagation();
      const rect = el.getBoundingClientRect();
      const price = candles.coordinateToPrice(event.clientY - rect.top);
      const startPrice = candles.coordinateToPrice(boxDrag.startY - rect.top);
      if (price === null || startPrice === null) return;
      const delta = Math.round(price - startPrice);
      if (Math.abs(event.clientY - boxDrag.startY) > 2) boxDrag.moved = true;

      let top = boxDrag.startTop;
      let bottom = boxDrag.startBottom;
      if (boxDrag.hit.part === 'top') top = Math.max(Math.round(top + delta), bottom + MIN_BOX_HEIGHT);
      else if (boxDrag.hit.part === 'bottom') bottom = Math.min(Math.round(bottom + delta), top - MIN_BOX_HEIGHT);
      else {
        top = Math.round(top + delta);
        bottom = Math.round(bottom + delta);
      }
      boxes.previewEdges(boxDrag.hit.box, top, bottom);
    },
    true,
  );

  const finish = (event, commit) => {
    if (!boxDrag.hit) return;
    event.stopPropagation();
    const { box } = boxDrag.hit;
    const changed = box.top !== boxDrag.startTop || box.bottom !== boxDrag.startBottom;
    const start = { top: boxDrag.startTop, bottom: boxDrag.startBottom };
    boxDrag.hit = null;
    el.style.cursor = '';
    candles.priceScale().applyOptions({ autoScale: boxDrag.autoScale });
    if (commit && changed) {
      saveBoxEdit(box, start);
    } else {
      box.top = start.top;
      box.bottom = start.bottom;
      boxes.endPreview();
    }
  };
  el.addEventListener('pointerup', (event) => finish(event, true), true);
  el.addEventListener('pointercancel', (event) => finish(event, false), true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && boxDrag.hit) finish(event, false);
  });

  el.addEventListener(
    'dblclick',
    (event) => {
      const hit = boxHitAt(el, event);
      if (!hit) return;
      event.stopPropagation();
      event.preventDefault();
      if (hit.box.adjusted) resetBoxEdit(hit.box);
    },
    true,
  );
}

function boxKey(box) {
  const body = { box_type: box.box_type, created_time: box.created_time };
  if (CODE) body.code = CODE;
  return body;
}

async function saveBoxEdit(box, start) {
  const body = Object.assign(boxKey(box), { top: box.top, bottom: box.bottom });
  try {
    const response = await fetch('/api/boxes/override', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(String(response.status));
  } catch (error) {
    box.top = start.top;
    box.bottom = start.bottom;
    alert('箱體調整儲存失敗：' + error.message);
  }
  boxes.endPreview();
  refresh();
}

async function resetBoxEdit(box) {
  const noun = box.box_type === 'support' ? '支撐箱' : '阻力箱';
  if (!confirm('將' + noun + ' #' + box.box_id + ' 還原為計算值？')) return;
  const query = new URLSearchParams(boxKey(box));
  const response = await fetch('/api/boxes/override?' + query.toString(), { method: 'DELETE' });
  if (!response.ok && response.status !== 404) {
    alert('還原失敗：' + response.status);
    return;
  }
  refresh();
}

document.getElementById('fit').addEventListener('click', () => {
  chart.timeScale().fitContent();
});
document.getElementById('recent').addEventListener('click', () => {
  frameInitialViewport(lastBars.length);
});

// ---------------------------------------------------------------- refresh loop
async function refreshChart() {
  const query = [];
  if (CODE) query.push('code=' + encodeURIComponent(CODE));
  if (DAYS) query.push('days=' + DAYS);
  const response = await fetch('/api/chart' + (query.length ? '?' + query.join('&') : ''));
  if (!response.ok) throw new Error('chart ' + response.status);
  const payload = await response.json();

  lastBars = payload.bars || [];
  // A full setData keeps barSpacing and the visible window -- it is not a view
  // reset -- and when bars are appended the library either follows the newest
  // bar (if you are parked at the right edge) or compensates the scroll so the
  // bars under the cursor stay put.  That is exactly the Plotly.react behaviour
  // this page replaces, so there is no visible-range save/restore here.
  candles.setData(lastBars);
  applySeries(payload.series || []);

  // After setData: resolving a box's time to a logical index needs the new time
  // points in place.  An unchanged box payload still has to be re-resolved,
  // because appending a bar shifts every index.
  // Mid-drag, the box being dragged belongs to the pointer; the next poll catches up.
  if (!boxDrag.hit && !boxes.setBoxes(payload.boxes || [])) boxes.refreshIndices();

  renderLegendRows();
  renderTrendlineList(payload.trendlines || []);
  if (!framed && lastBars.length) {
    frameInitialViewport(lastBars.length);
    framed = true;
  }
  return lastBars.length;
}

async function refresh() {
  try {
    const stateUrl = '/api/state' + (CODE ? '?code=' + encodeURIComponent(CODE) : '');
    const [stateResponse] = await Promise.all([fetch(stateUrl), refreshChart()]);
    if (stateResponse.ok) {
      const state = await stateResponse.json();
      if (state.ready) SNRStatus.renderAll(state);
      else SNRStatus.setFreshness(state.message || '尚無資料', 'warn');
    }
  } catch (error) {
    SNRStatus.setFreshness('連線失敗', 'bad');
    console.error(error);
  }
}

initChart();
refresh();
setInterval(refresh, REFRESH_MS);
