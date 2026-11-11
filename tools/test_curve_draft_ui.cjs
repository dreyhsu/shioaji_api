'use strict';
// Exercise the actual drawing handlers with chart coordinates and pointer capture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/web/lwc_app.js'), 'utf8').replace(/\r\n/g, '\n');
const section = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
class Element {
  constructor() { this.style = {}; this.children = []; this.listeners = []; this.classList = { toggle() {} }; this.captured = null; }
  addEventListener(type, fn, capture = false) { this.listeners.push({ type, fn, capture }); }
  fire(type, props = {}) {
    const event = { button: 0, pointerId: 1, clientX: 0, clientY: 0, target: this,
      stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }, ...props };
    for (const capture of [true, false]) {
      for (const item of this.listeners.filter(item => item.type === type && item.capture === capture)) item.fn(event);
      if (event.stopped) break;
    }
    return event;
  }
  getBoundingClientRect() { return { left: 0, top: 0 }; }
  setPointerCapture(id) { this.captured = id; }
  hasPointerCapture(id) { return this.captured === id; }
  releasePointerCapture() { this.captured = null; }
  replaceChildren() { this.children = []; }
  appendChild(child) { this.children.push(child); }
  setAttribute(name, value) { this[name] = value; }
  closest() { return null; }
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const document = new Element();
Object.assign(document, { getElementById: get, createElementNS: () => new Element(), dispatchEvent() {} });
let xScale = 20, xOffset = 0, autoScale = true, options = { handleScroll: { pressedMouseMove: true }, handleScale: { mouseWheel: true } };
const timeScale = { width: () => 300, coordinateToLogical: x => (x - xOffset) / xScale,
  options: () => ({ barSpacing: xScale }),
  timeToCoordinate: time => ((time - 1000) / 60) * xScale + xOffset };
const chart = { timeScale: () => timeScale, panes: () => [{ getHeight: () => 500 }],
  options: () => options, applyOptions: next => { options = { ...options, ...next }; },
  addSeries: () => ({ setData(data) { this.data = data; } }), removeSeries() {} };
const candles = { coordinateToPrice: y => 500 - y, priceToCoordinate: p => 500 - p,
  priceScale: () => ({ options: () => ({ autoScale }), applyOptions: next => { autoScale = next.autoScale; } }) };
const requests = [], alerts = [], frames = new Map();
let frameId = 0, fetchImpl = async () => ({ ok: true });
const context = { document, chart, candles, LWC: { LineSeries: 0 }, PANE_PRICE: 0,
  CurveHover: require('../src/web/curve_hover.js'), switchingTimeframe: false, timeframe: '1h', displayedTimeframe: '1h',
  CODE: 'TMFR1', canDraw: () => true, hideCurveHover() {}, refresh() {},
  lastBars: Array.from({ length: 15 }, (_, i) => ({ time: 1000 + i * 60 })),
  requestAnimationFrame(fn) { frames.set(++frameId, fn); return frameId; }, cancelAnimationFrame(id) { frames.delete(id); },
  Event: class {}, alert: message => alerts.push(message),
  fetch: async (url, config) => { requests.push({ url, ...JSON.parse(config.body) }); return fetchImpl(); } };
vm.createContext(context);
vm.runInContext(section('const draw =', 'async function deleteTrendline(')
  + section("for (const tool of ['line', 'curve']) {\n  document.getElementById('draw-'", '// ---------------------------------------------------------------- box edits')
  + '\nglobalThis.draft = draw;', context);
const el = get('chart'), draft = context.draft;
context.listenForDrawClicks(el);
const anchor = (index, price) => ({ time: 1000 + index * 60, price });
const json = value => JSON.parse(JSON.stringify(value));
function click(index, price) {
  const position = { clientX: index * xScale + xOffset, clientY: 500 - price };
  el.fire('pointerdown', position); el.fire('pointerup', position);
}
function ready(reverse = false) {
  context.setDrawing('curve');
  click(reverse ? 10 : 2, reverse ? 320 : 300);
  click(reverse ? 2 : 10, reverse ? 300 : 320);
  click(6, 400);
  assert.equal(draft.anchors.length, 3);
}
function drag(index, xIndex, price, end = 'pointerup') {
  const original = json(draft.anchors);
  const point = index === null ? context.previewData()[3] : { time: draft.anchors[index].time, value: draft.anchors[index].price };
  el.fire('pointerdown', { clientX: timeScale.timeToCoordinate(point.time), clientY: 500 - point.value });
  assert.equal(draft.drag.index, index);
  assert.equal(autoScale, false);
  assert.equal(options.handleScroll, false);
  const pos = { clientX: xIndex * xScale + xOffset, clientY: 500 - price };
  el.fire('pointermove', pos); el.fire(end, pos);
  assert.equal(draft.drag, null);
  assert.equal(el.captured, null);
  assert.equal(autoScale, true);
  assert.equal(options.handleScroll.pressedMouseMove, true);
  return original;
}
const key = (key, props = {}) => document.fire('keydown', { key, ...props });

(async () => {
  context.setDrawing('curve');
  key('Enter');
  click(2, 300); click(3, 310); // Too little space for a third anchor.
  assert.equal(draft.anchors.length, 1);
  click(10, 320); key('Enter');
  assert.equal(requests.length, 0);
  click(6, 400);
  assert.equal(requests.length, 0, 'third click places, never saves');
  const placed = json(draft.anchors);
  context.onChartClick(anchor(8, 350)); context.onDrawHover({ point: { x: 5, y: 5 }, time: 1000 });
  assert.deepEqual(json(draft.anchors), placed);
  context.renderCurveHandles();
  assert.equal(get('curve-draft-handles').children.length, 3);
  xScale = 15; xOffset = 3;
  context.renderCurveHandles();
  assert.equal(Number(get('curve-draft-handles').children[0].cx), 33, 'handles follow zoom/pan');
  assert.equal(context.curveDraftHit(33, 200).index, 0, 'handle takes precedence');
  assert.equal(context.curveDraftHit(33, 208).index, 0);
  assert.equal(context.curveDraftHit(-1, 200), null);
  drag(0, 8, 305); assert.equal(draft.anchors[0].time, anchor(5, 0).time);
  drag(1, 1, 310); assert.equal(draft.anchors[1].time, anchor(7, 0).time);
  drag(2, 14, 390); assert.equal(draft.anchors[2].time, anchor(6, 0).time);
  ready();
  const before = drag(null, 8, 380);
  const moved = json(draft.anchors);
  assert.equal(moved[0].time - before[0].time, 180);
  assert.equal(moved[1].time - before[1].time, 180);
  assert.equal(moved[2].price - before[2].price, moved[0].price - before[0].price);
  drag(null, 100, 350);
  assert.equal(Math.max(...draft.anchors.map(a => a.time)), anchor(14, 0).time);
  drag(null, -100, 350);
  assert.equal(Math.min(...draft.anchors.map(a => a.time)), anchor(0, 0).time);
  ready();
  const cancelled = drag(2, 7, 370, 'pointercancel');
  assert.deepEqual(json(draft.anchors), cancelled);
  drag(0, 4, 330, 'lostpointercapture');
  assert.deepEqual(json(draft.anchors), cancelled);
  ready(true); drag(0, 0, 330);
  assert.equal(draft.anchors[0].time, anchor(7, 0).time, 'reversed endpoints stay valid');
  ready();
  for (const tag of ['input', 'textarea', 'select', 'contenteditable']) {
    key('Enter', { target: { closest: () => ({ tag }) } });
    key('Escape', { target: { closest: () => ({ tag }) } });
  }
  assert.equal(requests.length, 0); assert.equal(draft.kind, 'curve');
  let release;
  fetchImpl = () => new Promise(resolve => { release = resolve; });
  const expected = json(draft.anchors), expectedPreview = json(context.previewData());
  key('Enter'); key('Enter'); key('Enter', { repeat: true });
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].anchors, expected);
  assert.equal(expectedPreview[0].value, expected[0].price);
  assert.equal(expectedPreview.at(-1).value, expected[1].price);
  release({ ok: true }); await new Promise(resolve => setImmediate(resolve));
  assert.equal(draft.kind, null);
  ready(); fetchImpl = async () => ({ ok: false, status: 500 });
  await context.saveDrawing(); assert.equal(draft.kind, 'curve'); assert.equal(alerts.length, 1);
  fetchImpl = async () => { throw new Error('offline'); };
  await context.saveDrawing(); assert.equal(draft.anchors.length, 3); assert.equal(draft.saving, false);
  const count = requests.length; key('Escape');
  assert.equal(draft.kind, null); assert.equal(requests.length, count);
  ready(); context.setDrawing('line'); assert.equal(draft.anchors.length, 0);
  // Refreshes retain time-based anchors, or explicitly discard unavailable ones.
  const bars = json(context.lastBars);
  let payload = { timeframe: '1h', bars, series: [], trendlines: [], boxes: [], gaps: [] };
  Object.assign(context, { chartRequest: 0, DAYS: 30, hoverCurves: [], framed: true,
    applySeries() {}, renderLegendRows() {}, renderTrendlineList() {},
    boxes: { setBoxes: () => true }, gaps: { setBoxes: () => true }, boxDrag: { hit: null },
    frameInitialViewport() {}, indicators: new Map(), updateTimeframeButtons() {},
    URL, location: { href: 'http://localhost/' }, history: { replaceState() {} },
    SNRStatus: { setFreshness() {} }, console });
  candles.setData = () => {};
  vm.runInContext(section('async function refreshChart()', 'async function refresh()')
    + section('async function selectTimeframe(', "for (const button of document.querySelectorAll('[data-timeframe]'))"), context);
  context.fetch = async () => ({ ok: true, json: async () => payload });
  ready(); const retained = json(draft.anchors);
  await context.refreshChart(); assert.deepEqual(json(draft.anchors), retained);
  const position = { clientX: timeScale.timeToCoordinate(draft.anchors[0].time), clientY: 500 - draft.anchors[0].price };
  el.fire('pointerdown', position);
  payload = { ...payload, bars: [] };
  await context.refreshChart(); assert.equal(context.lastBars.length, bars.length, 'refresh waits during drag');
  el.fire('pointercancel', position);
  await context.refreshChart(); assert.equal(draft.kind, null);
  assert.equal(get('draw-hint').hidden, false);
  assert.match(get('draw-hint').textContent, /歷史資料已變更/);
  context.lastBars = bars; ready();
  payload = { ...payload, bars, timeframe: '5m' };
  await context.selectTimeframe('5m'); assert.equal(draft.kind, null);
  // Restore the save spy after the chart-refresh checks.
  context.fetch = async (url, config) => { requests.push({ url, ...JSON.parse(config.body) }); return fetchImpl(); };
  // A response from an abandoned draft must not clear a new drawing.
  ready(); fetchImpl = () => new Promise(resolve => { release = resolve; });
  const pending = context.saveDrawing(); key('Escape'); context.setDrawing('line');
  release({ ok: true }); await pending; assert.equal(draft.kind, 'line');
  console.log('Curve draft placement, handles, movement, cancellation, shortcuts, refresh, timeframe and save checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
