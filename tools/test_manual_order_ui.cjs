// Exercise the actual manual/automatic readiness and preview functions offline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, { value: '', classList: { toggle() {} } });
  return elements.get(id);
};
get('trade-contract').value = 'TMFL6';
get('trade-price-type').value = 'MKT';
const ctx = { $: get, state: { contracts: [{ code: 'TMFL6' }], atr: 100 },
  busy: false, lastTradeStatusAt: 0, entryEnabled: false, entrySnapshot: null,
  renderEntry() {}, number: String, currentAction: () => 'Buy', escapeHtml: String };
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('  function selectedContract('), source.indexOf('  async function loadContracts('))
  + source.slice(source.indexOf('  function renderPreview('), source.indexOf('  function escapeHtml(')), ctx);
const info = { connected: true, broker_ready: true, bars_ready: false,
  contract_bars_ready: { TMFL6: false }, manual_order_ready: { TMFL6: true },
  market_quotes: { TMFL6: { price: 20000, fresh: true } } };
ctx.setMode(info);
ctx.renderPreview();
assert.equal(ctx.state.entryReady, false);
assert.equal(ctx.state.manualReady, true);
assert.equal(get('trade-submit').disabled, false);
assert.match(get('trade-preview').innerHTML, /歷史 K 棒待就緒/);
ctx.setMode({ ...info, manual_order_ready: { TMFL6: false } });
ctx.renderPreview();
assert.equal(get('trade-submit').disabled, true);
// A stale quote must keep the button disabled even when a limit price exists.
get('trade-price-type').value = 'LMT';
get('trade-price').value = '20000';
ctx.setMode({ ...info, manual_order_ready: { TMFL6: false },
  market_quotes: { TMFL6: { price: 20000, fresh: false } } });
ctx.renderPreview();
assert.equal(get('trade-submit').disabled, true);
// Offline Mock keeps its existing readiness contract.
ctx.setMode({ connected: true, broker_ready: true, bars_ready: true, last_quote: 20000 });
ctx.renderPreview();
assert.equal(get('trade-submit').disabled, false);
console.log('Manual tick readiness UI checks passed');
