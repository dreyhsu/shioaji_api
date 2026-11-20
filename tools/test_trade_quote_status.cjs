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
ctx.setMode({ connected: true, manual_order_ready: { TMFK6: true },
  market_quotes: { TMFK6: { price: 44530, at: '2026-11-03 10:16:09', fresh: true } },
  broker_external_orders: [{ code: 'TMFK6', action: 'Buy', quantity: 3, filled_quantity: 1,
    cancel_quantity: 0, status: 'PartFilled' }], pending_default_atr: 1 });
assert.equal(get('trade-submit').disabled, false);
assert.match(get('auto-exit-state').textContent, /App 委託已自動納管.*2 口/);
assert.match(get('auto-exit-state').textContent, /等待 1h ATR/);

// A status refresh removes the expired choice and its draft limit price,
// while retaining a still-valid user selection. No trading command is sent.
ctx.document = { createElement: () => ({}) };
ctx.state.contracts = [];
get('trade-contract').replaceChildren = (...options) => { get('trade-contract').options = options; };
vm.runInContext(source.slice(source.indexOf('  function setContracts('), source.indexOf('  function setMode(')), ctx);
const november = { code: 'TMFK6', delivery_month: '202611', name: 'November' };
const december = { code: 'TMFL6', delivery_month: '202612', name: 'December' };
const january = { code: 'TMFA7', delivery_month: '202701', name: 'January' };
ctx.setContracts([november, december]);
get('trade-price').value = '40156';
assert.equal(get('trade-contract').value, 'TMFK6');
ctx.selectedContract = () => ctx.state.contracts.find((item) => item.code === get('trade-contract').value);
ctx.setMode({ connected: true, entry_contracts: [december],
  market_quotes: { TMFL6: { price: 40515, at: '2026-11-18 17:03:26', fresh: true } },
  manual_order_ready: { TMFL6: true } });
assert.equal(get('trade-contract').value, 'TMFL6');
assert.equal(get('trade-price').value, 40515);
assert.equal(get('trade-submit').disabled, false);
get('trade-price').value = '40500';
ctx.setContracts([december, january]);
assert.equal(get('trade-contract').value, 'TMFL6');
assert.equal(get('trade-price').value, '40500');
ctx.setContracts([]);
assert.equal(get('trade-contract').value, '');
assert.equal(get('trade-price').value, '');
assert.equal(get('trade-contract').disabled, true);
console.log('3 contract rollover selection scenarios passed');

ctx.cancellingOrders = new Set();
vm.runInContext(source.slice(source.indexOf('  function orderStatus('), source.indexOf('  function canCancelEntry(')), ctx);
const protectedOrder = { id: 'tick-risk', active: 1, auto_exit: 1, entry_status: 'Filled',
  filled_quantity: 1, exit_filled_quantity: 0,
  protection: { broker_ready: true, tick_fresh: true, closed_1m_ready: false, exit_signal_mode: 'tick' } };
assert.equal(ctx.orderStatus(protectedOrder), '持倉監控中');
protectedOrder.protection.tick_fresh = false;
assert.match(ctx.orderStatus(protectedOrder), /tick 過期/);
console.log('tick protection display remains active with missing historical bars');
