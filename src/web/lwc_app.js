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
const PANE_OSC = 2;
// Height is directly proportional to the stretch factor, and pane 0 ships with
// a factor of 2 while new panes get 1 -- so all three are always set explicitly.
const PANE_STRETCH = [7, 2, 2];

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

  // Pre-create panes 1 and 2: addSeries(..., paneIndex) would create them, but a
  // pane whose last series is removed is deleted and the rest renumber, so an
  // indicator disappearing from /api/chart would shuffle the layout.
  while (chart.panes().length < 3) chart.addPane(true);
  const panes = chart.panes();
  for (let i = 0; i < 3; i++) {
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
      const pane = spec.pane === undefined ? PANE_OSC : spec.pane;
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
  if (!boxes.setBoxes(payload.boxes || [])) boxes.refreshIndices();

  renderLegendRows();
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
