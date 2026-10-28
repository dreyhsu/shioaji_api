// Exercise the dashboard's actual cancel handler with delayed broker responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
const handler = source.slice(source.indexOf('  async function cancelEntry('), source.indexOf('  function renderOrders()'));

function scenario(statuses, { confirmed = true, failure = false, postGate = Promise.resolve() } = {}) {
  const order = { id: 'test-order', action: 'Buy', contract_code: 'TMFX6', quantity: 1,
    filled_quantity: 0, active: 1, entry_status: 'Submitted' };
  const calls = [], messages = [], renders = [];
  let reads = 0, margins = 0;
  const ctx = {
    state: { orders: [order] }, cancellingOrders: new Set(), cancelErrors: new Map(),
    canCancelEntry: () => true, confirmOperation: async () => confirmed,
    renderOrders: () => renders.push(ctx.cancellingOrders.has(order.id)),
    message: (text, bad = false) => messages.push({ text, bad }),
    acceptOrders: (orders) => { ctx.state.orders = orders; },
    refreshOrders: async () => {}, loadAccountMargin: async () => { margins++; },
    setTimeout: (fn) => fn(),
    fetch: async (url, options) => {
      calls.push({ url, method: options?.method || 'GET' });
      if (options?.method === 'POST') {
        await postGate;
        return { ok: !failure,
          json: async () => failure ? { detail: 'broker rejected cancellation' } : { ...order } };
      }
      const status = statuses[Math.min(reads++, statuses.length - 1)];
      return { ok: true, json: async () => [{ ...order, entry_status: status,
        active: status === 'Cancelled' ? 0 : 1 }] };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(handler + '\nthis.runCancel = cancelEntry;', ctx);
  return { ctx, order, calls, messages, renders, margins: () => margins };
}

(async () => {
  let release;
  const postGate = new Promise((resolve) => { release = resolve; });
  let s = scenario(['Submitted', 'Cancelled'], { postGate });
  const first = s.ctx.runCancel(s.order);
  while (!s.calls.length) await Promise.resolve();
  await s.ctx.runCancel(s.order);
  release();
  await first;
  assert.equal(s.calls.filter((call) => call.method === 'POST').length, 1);
  assert.equal(s.calls.filter((call) => call.method === 'GET').length, 2);
  assert.match(s.messages.at(-1).text, /券商已確認取消/);
  assert.equal(s.ctx.state.orders[0].active, 0);
  assert.equal(s.ctx.cancelErrors.size, 0);
  assert.equal(s.ctx.cancellingOrders.size, 0);
  assert(s.renders.includes(true));
  assert.equal(s.renders.at(-1), false);
  assert.equal(s.margins(), 1);

  s = scenario(['Submitted']);
  await s.ctx.runCancel(s.order);
  assert.equal(s.calls.filter((call) => call.method === 'POST').length, 1);
  assert.equal(s.calls.filter((call) => call.method === 'GET').length, 10);
  assert.match(s.ctx.cancelErrors.get(s.order.id), /取消尚未確認/);
  assert.equal(s.ctx.state.orders[0].active, 1);

  s = scenario(['Filled']);
  await s.ctx.runCancel(s.order);
  assert.match(s.ctx.cancelErrors.get(s.order.id), /全部成交/);
  assert.equal(s.messages.at(-1).bad, true);

  s = scenario([], { failure: true });
  await s.ctx.runCancel(s.order);
  assert.equal(s.ctx.cancelErrors.get(s.order.id), 'broker rejected cancellation');
  assert.equal(s.ctx.cancellingOrders.size, 0);

  s = scenario([], { confirmed: false });
  await s.ctx.runCancel(s.order);
  assert.equal(s.calls.length, 0);
  assert.equal(s.margins(), 0);
  console.log('5 cancellation UI scenarios passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
