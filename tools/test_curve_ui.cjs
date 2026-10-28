// Run the chart and entry menu handlers against lightweight UI doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const chartSource = fs.readFileSync(path.join(__dirname, '../src/web/lwc_app.js'), 'utf8');
const tradingSource = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
function section(source, start, end) {
  assert(source.includes(start) && source.includes(end));
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}
function element() {
  return { value: '', options: [], classList: { toggle() {} }, setAttribute() {},
    replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; },
    add(option) { this.options.push(option); } };
}

(async () => {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  get('timeframes').querySelectorAll = () => [];
  const saved = [], events = [];
  const chart = { timeframe: '1m', displayedTimeframe: '1m', switchingTimeframe: false,
    draw: { kind: null, anchors: [] }, CODE: 'TMFR1', updateHint() {}, refresh() {},
    chart: { removeSeries() {} }, Event: class { constructor(type) { this.type = type; } },
    document: { getElementById: get, dispatchEvent: (event) => events.push(event.type) },
    fetch: async (url, options) => { saved.push({ url, ...JSON.parse(options.body) }); return { ok: true }; },
  };
  vm.createContext(chart);
  vm.runInContext(section(chartSource, 'function setDrawing(', 'function updateHint(')
    + section(chartSource, 'async function saveDrawing(', 'async function deleteTrendline(')
    + section(chartSource, 'function canDraw(', 'async function selectTimeframe('), chart);
  for (const timeframe of ['1m', '5m', '1h', '15m']) {
    chart.timeframe = chart.displayedTimeframe = timeframe;
    chart.updateTimeframeButtons();
    assert.equal(get('draw-curve').disabled, timeframe === '15m');
    assert.equal(get('draw-line').disabled, timeframe !== '1h');
    chart.setDrawing('curve');
    assert.equal(chart.draw.kind, timeframe === '15m' ? null : 'curve');
    if (timeframe !== '15m') {
      chart.draw.anchors = [0, 1, 2].map((i) => ({ time: 100 + i * 60, price: 20000 }));
      await chart.saveDrawing();
      assert.equal(saved.at(-1).timeframe, timeframe);
      assert.equal(saved.at(-1).kind, 'curve');
      assert.equal(chart.draw.kind, null);
    }
    chart.setDrawing(null);
  }
  assert.equal(saved.length, 3);
  assert.equal(events.length, 3);
  chart.timeframe = '5m'; chart.displayedTimeframe = '1m';
  chart.setDrawing('curve');
  assert.equal(chart.draw.kind, null); // cannot draw against the previous chart
  chart.displayedTimeframe = '5m'; chart.switchingTimeframe = true;
  chart.setDrawing('curve');
  assert.equal(chart.draw.kind, null);

  const lines = [{ id: 1, kind: 'curve', timeframe: '1m' },
    { id: 2, kind: 'curve', timeframe: '5m', label: '短線' },
    { id: 3, kind: 'curve', timeframe: '1h' }, { id: 4, kind: 'line', timeframe: '1h' }];
  const trade = { state: { curves: [], connected: true, atr: 100, simulation: true },
    curvesListKey: null, entrySnapshot: null, entryEnabled: true, entryBusy: false,
    $: get, Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    fetchRead: async () => ({ ok: true, json: async () => lines }),
    renderPreview() {}, renderOrders() {}, selectedContract: () => ({ code: 'TMF209912' }),
    entryAction: () => 'Buy',
  };
  vm.createContext(trade);
  vm.runInContext(section(tradingSource, '  function curveLabel(', '  async function loadAccountMargin(')
    + section(tradingSource, '  function renderEntry(', '  async function subscribeSelectedContract('), trade);
  get('entry-quantity').value = '1';
  await trade.loadCurves();
  assert.deepEqual(get('entry-curve').options.map((option) => option.text).slice(1),
    ['[1m] 曲線 #1', '[5m] 短線', '[1h] 曲線 #3']);
  assert.deepEqual(get('trade-curve').options.map((option) => option.value), ['', '3']);
  get('entry-curve').value = '2';
  lines.push({ id: 5, kind: 'curve', timeframe: '1m' });
  await trade.loadCurves();
  assert.equal(get('entry-curve').value, '2');
  let summary;
  trade.confirmOperation = async (message) => { summary = message; return false; };
  await trade.controlEntry();
  assert.match(summary, /\[5m\] 短線/);
  assert.match(summary, /已收盤 1 分 K/);
  assert.match(summary, /各 2 倍進場 1h ATR/);
  trade.entrySnapshot = { state: 'armed', curve_id: 2, timeframe: '5m', quantity: 1,
    contract_code: 'TMF209912', action: 'Sell' };
  trade.renderEntry();
  assert.match(get('entry-state').textContent, /\[5m\] 曲線 #2/);
  assert.match(get('entry-state').textContent, /向下突破做空/);
  console.log('Curve drawing, timeframe guards, entry menus and confirmation scenarios passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
