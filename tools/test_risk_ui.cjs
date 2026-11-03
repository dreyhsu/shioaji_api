// Exercise the real risk editor: switching orders must not mix saved points.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
const editor = source.slice(source.indexOf('  function riskValues('), source.indexOf('  async function cancelEntry('));
const elements = new Map();
function get(id) {
  if (!elements.has(id)) elements.set(id, { value: '', classList: { toggle() {} },
    querySelectorAll: () => [], replaceChildren() {}, add() {} });
  return elements.get(id);
}
const order = (id, trigger, offset) => ({ id, curve_id: null, stop_price: 19000,
  target_price: 21000, breakeven_enabled: true, breakeven_trigger_points: trigger,
  breakeven_offset_points: offset, risk_revision: 1, contract_tick: 1 });
const calls = [];
const ctx = { state: { selectedId: 'a', orders: [order('a', 100, 5), order('b', 150, 10)] },
  riskDrafts: new Map(), riskSaving: new Set(), riskErrors: new Map(), riskNotice: '', curveOptionsKey: null,
  $: get, document: { activeElement: null }, window: { SNRChart: { setTradeOrder() {} } },
  canManageCurve: () => true, exitCurves: () => [], orderStatus: () => 'Filled',
  Option: class { constructor(text, value) { this.text = text; this.value = value; } },
  number: String, message() {}, confirmOperation: async () => true, refreshOrders: async () => {},
  tradeFetch: async (url, options) => {
    const payload = JSON.parse(options.body);
    calls.push({ url, payload });
    return { ok: true, status: 200, json: async () => ({ ...ctx.state.orders[0], ...payload, risk_revision: 2 }) };
  },
};
vm.createContext(ctx);
vm.runInContext(editor, ctx);
(async () => {
  ctx.renderRiskEditor();
  assert.equal(get('risk-breakeven-trigger').value, 100);
  assert.equal(get('risk-breakeven-offset').value, 5);
  ctx.updateRiskDraft('a', { breakeven_trigger_points: '60', breakeven_offset_points: '8' });
  ctx.state.selectedId = 'b';
  ctx.renderRiskEditor();
  assert.equal(get('risk-breakeven-trigger').value, 150);
  assert.equal(get('risk-breakeven-offset').value, 10);
  ctx.updateRiskDraft('b', { breakeven_offset_points: '12' });
  ctx.state.selectedId = 'a';
  ctx.renderRiskEditor();
  assert.equal(get('risk-breakeven-trigger').value, '60');
  await ctx.saveRisk({ preventDefault() {} });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/trading/orders/a/risk-settings');
  assert.equal(calls[0].payload.breakeven_trigger_points, 60);
  assert.equal(calls[0].payload.breakeven_offset_points, 8);
  assert(!ctx.riskDrafts.has('a'));
  assert.equal(ctx.riskDrafts.get('b').breakeven_offset_points, '12');
  assert.equal(ctx.state.orders[1].breakeven_offset_points, 10);
  console.log('Risk editor order isolation and numeric save passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
