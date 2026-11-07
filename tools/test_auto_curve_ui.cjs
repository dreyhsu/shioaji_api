'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const AutoCurve = require('../src/web/auto_curve.js');
const CurveHover = require('../src/web/curve_hover.js');

class Element {
  constructor() {
    this.listeners = {}; this.style = {}; this.hidden = true; this.disabled = false;
    this.classes = new Set(); this.attributes = {};
    this.classList = {add: v => this.classes.add(v), remove: v => this.classes.delete(v)};
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  setAttribute(key, value) { this.attributes[key] = value; }
  appendChild() {}
  getBoundingClientRect() { return {left: 0, top: 0}; }
  setPointerCapture(id) { this.capture = id; }
  hasPointerCapture(id) { return this.capture === id; }
  releasePointerCapture() { this.capture = null; }
  getContext() { return {setTransform() {}, clearRect() {}, fillRect() {}, strokeRect() {},
    setLineDash() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillText() {},
    measureText: s => ({width: s.length * 6})}; }
}

async function main() {
  const document = new Element(), frames = [];
  document.createElement = () => new Element();
  let settings = {handleScroll: {pressedMouseMove: true}, handleScale: {mouseWheel: true}};
  let autoScale = true, spacing = 10, priceScale = 2;
  const previews = [];
  const scale = {options: () => ({autoScale}), applyOptions: o => { autoScale = o.autoScale; }};
  const timeScale = {width: () => 600, options: () => ({barSpacing: spacing}),
    coordinateToLogical: x => x / spacing, logicalToCoordinate: i => i * spacing,
    getVisibleLogicalRange: () => ({from: 0, to: 35}), subscribeVisibleLogicalRangeChange() {}};
  const chart = {timeScale: () => timeScale, panes: () => [{getHeight: () => 400}],
    options: () => settings, applyOptions: o => { settings = {...settings, ...o}; },
    subscribeCrosshairMove() {}, removeSeries: series => { series.removed = true; },
    addSeries: () => {
      const series = {setData: data => { series.data = data; }};
      previews.push(series); return series;
    }};
  const candles = {priceScale: () => scale, coordinateToPrice: y => 200 - y / priceScale,
    priceToCoordinate: p => (200 - p) * priceScale};
  const frame = Array.from({length: 40}, (_, i) => {
    const low = 100 + .2 * i + .01 * i * i + [4, 2, 0, 2, 4][i % 5];
    return {time: 100000 + 900 * i, low, high: low + 10, close: low + 5};
  });
  const context = {AutoCurve, CurveHover, document, structuredClone,
    window: {devicePixelRatio: 1, LightweightCharts: {LineSeries: 'line'}},
    requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
    ResizeObserver: class { observe() {} }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/web/auto_curve_tool.js'), 'utf8'), context);
  const element = new Element(), button = new Element(), saveButton = new Element(), cancelButton = new Element(), hint = new Element();
  let saved = [], ended = 0, fail = false;
  const tool = new context.AutoCurveTool({element, button, saveButton, cancelButton, hint, chart, candles,
    available: () => true, snapshot: () => ({bars: frame, timeframe: '15m', now: Infinity}),
    beforeStart() {}, afterEnd: () => { ended++; },
    save: async body => { if (fail) throw new Error('offline'); saved.push(body); }});
  const flush = () => { while (frames.length) frames.shift()(); };
  function event(i, price) {
    return {clientX: i * spacing, clientY: candles.priceToCoordinate(price), button: 0,
      pointerId: 1, cancelable: true, stopImmediatePropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }};
  }
  function click(i, price) { tool.down(event(i, price)); tool.up(event(i, price)); flush(); }
  function select() {
    const initialSaves = saved.length;
    tool.start(); flush();
    assert.equal(settings.handleScroll, false);
    click(0, 140);
    tool.move(event(30, 90)); flush();
    assert.equal(saved.length, initialSaves);
    click(30, 90);
    assert.ok(tool.draft);
    assert.equal(settings.handleScroll.pressedMouseMove, true);
    assert.equal(autoScale, true);
  }

  select();
  const original = tool.draft.points.map(p => ({...p}));
  const reference = tool.reference;
  const angle = AutoCurve.projection(original, reference);
  tool.down(event(10, AutoCurve.value(original, 10)));
  assert.equal(autoScale, false);
  tool.move(event(13, AutoCurve.value(original, 10) + 5)); flush();
  tool.up(event(13, AutoCurve.value(original, 10) + 5)); flush();
  assert.equal(tool.draft.points[0].i, 3);
  assert.equal(tool.draft.points[0].price, original[0].price + 5);
  assert.deepEqual(AutoCurve.projection(tool.draft.points, reference), angle);
  assert.equal(autoScale, true);
  assert.ok(previews[0].data.every(p => frame.some(b => b.time === p.time)));

  // Zoom only changes screen geometry; the fixed numeric angle remains intact.
  spacing = 15; priceScale = 3;
  tool.schedule(); flush();
  assert.equal(tool.reference, reference);
  assert.deepEqual(AutoCurve.projection(tool.draft.points, reference), angle);
  const beforeCancel = JSON.stringify(tool.draft.points);
  const hitPrice = AutoCurve.value(tool.draft.points, 15);
  tool.down(event(15, hitPrice));
  tool.move(event(17, hitPrice + 7));
  tool.abortPointer(event(17, hitPrice + 7)); flush();
  assert.equal(JSON.stringify(tool.draft.points), beforeCancel);
  assert.equal(autoScale, true);

  fail = true;
  await tool.commit(); flush();
  assert.ok(tool.draft);
  assert.match(hint.textContent, /offline/);
  assert.equal(saveButton.disabled, false);
  fail = false;
  const pending = tool.commit();
  await tool.commit(); // Double click must produce only one save.
  await pending; flush();
  assert.equal(saved.length, 1);
  assert.equal(saved[0].kind, 'curve');
  assert.equal(saved[0].timeframe, '15m');
  assert.equal(saved[0].angle_reference_points_per_bar, reference);
  assert.equal(tool.active, false);
  assert.equal(ended, 1);
  assert.ok(previews[0].removed);

  // A new identical selection must still populate a new preview series.
  spacing = 10; priceScale = 2;
  select();
  assert.ok(previews[1].data.length > 0);
  tool.cancel(); flush();
  assert.equal(saveButton.hidden, true);
  tool.start(); click(0, 140); click(1, 90);
  assert.equal(tool.draft, null);
  assert.match(hint.textContent, /3 根/);
  click(30, 90);
  assert.ok(tool.draft);
  tool.cancel(); flush();
  tool.start(); tool.down(event(0, 140)); tool.abortPointer(event(0, 140)); flush();
  assert.equal(tool.active, false);
  assert.equal(autoScale, true);
  assert.equal(settings.handleScale.mouseWheel, true);
  tool.start(); click(0, 140);
  document.listeners.keydown[0]({key: 'Escape', target: {tagName: 'DIV'}, preventDefault() {}});
  flush();
  assert.equal(tool.active, false);

  // Chart polls must not replace the frozen snapshot while selecting or editing.
  const source = fs.readFileSync(path.join(__dirname, '../src/web/lwc_app.js'), 'utf8');
  const functionSource = source.slice(source.indexOf('async function refreshChart()'), source.indexOf('async function refresh()'));
  const integration = {chart, autoCurve: {active: true}, lastBars: frame,
    fetch: () => { throw new Error('active draft should not fetch chart'); }};
  vm.createContext(integration); vm.runInContext(functionSource, integration);
  assert.equal(await integration.refreshChart(), frame.length);

  let cancelled = 0;
  const lifecycle = {chart: {}, autoCurve: {busy: false, cancel: () => { cancelled++; }},
    boxDrag: {hit: null}, timeframe: '15m', switchingTimeframe: false,
    hideCurveHover() {}, setDrawing() {}, updateTimeframeButtons() {}, refreshChart: async () => {},
    URL, location: {href: 'http://localhost/?timeframe=15m'}, history: {replaceState() {}},
    SNRStatus: {setFreshness() {}}, console, window: {}, setTradeOrder() {}, refresh() {}};
  vm.createContext(lifecycle);
  vm.runInContext(source.slice(source.indexOf('async function selectTimeframe('), source.indexOf("for (const button of document.querySelectorAll('[data-timeframe]')")), lifecycle);
  await lifecycle.selectTimeframe('1h');
  assert.equal(cancelled, 1);
  assert.equal(lifecycle.timeframe, '1h');
  lifecycle.autoCurve.busy = true;
  await lifecycle.selectTimeframe('1m');
  assert.equal(lifecycle.timeframe, '1h'); // Do not switch under an in-flight save.
  vm.runInContext(source.slice(source.indexOf('window.SNRChart = {'), source.indexOf('function boxKey(')), lifecycle);
  assert.equal(lifecycle.window.SNRChart.prepareReplayReset(), false);
  lifecycle.autoCurve.busy = false;
  assert.equal(lifecycle.window.SNRChart.prepareReplayReset(), true);
  assert.equal(cancelled, 2);
  console.log('Auto curve range, drag, zoom, pointer cancellation, save/retry and refresh-isolation checks passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
