// Distinguish an old trade quote snapshot from waiting for the first quote.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, { value: '', textContent: '' });
  return elements.get(id);
};
const ctx = { state: {}, $: get, number: String, selectedContract: () => ({ code: 'TMFK6' }), renderEntry() {}, busy: false };
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('  function setMode('), source.indexOf('  async function loadContracts(')), ctx);
const info = { connected: true, market_quotes: { TMFK6: { price: 44525, at: '2026-11-03 10:16:08', fresh: false } } };
ctx.setMode(info);
assert.match(get('trade-connection').textContent, /行情快照過舊/);
assert.match(get('trade-connection').textContent, /10:16:08/);
assert.equal(ctx.state.price, 44525);
info.market_quotes.TMFK6 = { price: 44530, at: '2026-11-03 10:16:09', fresh: true };
ctx.setMode(info);
assert.match(get('trade-connection').textContent, /行情連線/);
assert.equal(ctx.state.price, 44530);
ctx.setMode({ connected: true, market_quotes: {} });
assert.match(get('trade-connection').textContent, /等待 monitor 行情資料/);
console.log('3 trade quote freshness display scenarios passed');
