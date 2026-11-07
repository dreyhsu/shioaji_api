// Exercise the real change handler, confirmation summary and submission payload.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
const elements = new Map();
function get(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', handlers: {},
    addEventListener(event, callback) { this.handlers[event] = callback; },
  });
  return elements.get(id);
}
get('trade-order-type').options = ['ROD', 'IOC', 'FOK'].map(value => ({ value, disabled: false }));
get('trade-price').value = '20000';
get('trade-quantity').value = '1';
const sent = [], summaries = [], messages = [];
const ctx = {
  $: get, busy: false, state: { simulation: true, price: 20000, atr: 100, curves: [] },
  currentAction: () => 'Buy', selectedContract: () => ({ code: 'TMFK6', delivery_month: '202611', name: 'TMF' }),
  number: String, renderPreview() {}, refreshTrading: async () => {},
  confirmOperation: async summary => { summaries.push(summary); return true; },
  message: text => messages.push(text),
  tradeFetch: async (url, options) => {
    sent.push(JSON.parse(options.body));
    return { ok: true, json: async () => ({ id: 'order', entry_trade_id: 'trade', entry_status: 'PendingSubmit' }) };
  },
};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('  function syncOrderType('),
  source.indexOf("  $('trade-contract').addEventListener('change'")), ctx);

(async () => {
  for (const priceType of ['LMT', 'MKT', 'MKP', 'LMT']) {
    get('trade-price-type').value = priceType;
    get('trade-order-type').value = 'FOK';
    get('trade-price-type').handlers.change();
    const expected = priceType === 'LMT' ? 'ROD' : 'IOC';
    assert.equal(get('trade-order-type').value, expected);
    assert.equal(get('trade-order-type').options[0].disabled, priceType !== 'LMT');
    await ctx.submitOrder();
    assert.equal(sent.at(-1).order_type, expected);
    assert.match(summaries.at(-1), new RegExp(`${priceType} / ${expected}`));
  }
  // Explicit legal overrides survive confirmation and submission.
  for (const priceType of ['LMT', 'MKT', 'MKP']) {
    get('trade-price-type').value = priceType;
    get('trade-price-type').handlers.change();
    get('trade-order-type').value = 'FOK';
    await ctx.submitOrder();
    assert.equal(sent.at(-1).order_type, 'FOK');
  }
  const count = sent.length;
  get('trade-price-type').value = 'MKT';
  get('trade-order-type').value = 'ROD';
  await ctx.submitOrder();
  assert.equal(sent.length, count);
  assert.match(messages.at(-1), /不可搭配 ROD/);
  console.log('8 order type UI scenarios passed (switches, summaries, payloads, legal overrides, invalid blocking)');
})().catch(error => { console.error(error); process.exitCode = 1; });
