// Run real browser command handling against lost responses and durable results.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/web/trading.js'), 'utf8');
const handlers = source.slice(source.indexOf('  async function resolvePendingCommand('), source.indexOf('  async function confirmOperation('));
const key = 'shioaji-trade-pending';
const url = '/api/trading/curve-entry';
const options = { method: 'POST', body: '{"curve_id":20}' };
function rig({ lost = false, command = { state: 'succeeded', result: { state: 'armed' }, http_status: 200 } } = {}) {
  const storage = new Map(), calls = [], text = { textContent: '' };
  const ctx = { pendingCommandResolution: null, Response, AbortController, setTimeout, clearTimeout,
    sessionStorage: { getItem: (k) => storage.get(k) || null, setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k) },
    crypto: { randomUUID: () => 'original-id' }, $: () => text, message: (value) => { text.textContent = value; },
    fetch: async (target, request = {}) => {
      calls.push({ target, method: request.method || 'GET', identity: request.headers?.['Idempotency-Key'] });
      if (request.method === 'POST') {
        if (lost) throw new Error('socket closed');
        return new Response('{}', { status: 504 });
      }
      return new Response(JSON.stringify(ctx.command), { status: ctx.readStatus || 200 });
    }, command,
  };
  vm.createContext(ctx);
  vm.runInContext(handlers, ctx);
  return { ctx, calls, storage, text };
}
(async () => {
  for (const lost of [false, true]) {
    const r = rig({ lost });
    const response = await r.ctx.tradeFetch(url, options);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).state, 'armed');
    assert.deepEqual(r.calls.map((c) => c.method), ['POST', 'GET']);
    assert(r.calls[1].target.endsWith('/original-id'));
    assert(!r.storage.has(key));
  }
  const r = rig({ command: { state: 'processing' } });
  await assert.rejects(r.ctx.tradeFetch(url, options), /original-id/);
  const saved = r.storage.get(key);
  await assert.rejects(r.ctx.tradeFetch(url, options), /結果待確認/);
  assert.equal(r.calls.filter((c) => c.method === 'POST').length, 1);
  assert.equal(r.storage.get(key), saved);
  r.ctx.command = { state: 'succeeded', result: { state: 'armed' }, http_status: 200 };
  await r.ctx.refreshPendingCommand();
  assert(!r.storage.has(key));
  assert.match(r.text.textContent, /已確認成功/);
  assert.equal(r.calls.filter((c) => c.method === 'POST').length, 1);

  const failed = rig({ command: { state: 'failed', http_status: 422, result: { detail: 'invalid curve' } } });
  const result = await failed.ctx.tradeFetch(url, options);
  assert.equal(result.status, 422);
  assert.equal((await result.json()).detail, 'invalid curve');
  assert(!failed.storage.has(key));

  const unknown = rig({ command: { state: 'unknown', http_status: 409 } });
  await assert.rejects(unknown.ctx.tradeFetch(url, options), /結果待確認/);
  await unknown.ctx.refreshPendingCommand();
  assert(unknown.storage.has(key));
  assert.equal(unknown.calls.filter((c) => c.method === 'POST').length, 1);

  const missing = rig();
  missing.ctx.readStatus = 404;
  await assert.rejects(missing.ctx.tradeFetch(url, options), /結果待確認/);
  await missing.ctx.refreshPendingCommand();
  assert.equal(missing.calls.filter((c) => c.method === 'POST').length, 1);
  await assert.rejects(missing.ctx.tradeFetch(url, options), /結果待確認/);
  assert(missing.calls.filter((c) => c.method === 'POST').every((c) => c.identity === 'original-id'));

  // An old, delayed response cannot erase a different pending command.
  const delayed = rig();
  const originalFetch = delayed.ctx.fetch;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  delayed.ctx.fetch = async (target, request) => {
    if (request?.method === 'POST') { await gate; return new Response('{}', { status: 200 }); }
    return originalFetch(target, request);
  };
  const inFlight = delayed.ctx.tradeFetch(url, options);
  await delayed.ctx.refreshPendingCommand();
  delayed.storage.set(key, JSON.stringify({ id: 'next-id', fingerprint: 'next-operation' }));
  release();
  await inFlight;
  assert.equal(JSON.parse(delayed.storage.get(key)).id, 'next-id');
  console.log('7 command recovery scenarios passed (GET-only recovery, original IDs retained)');
})().catch((error) => { console.error(error); process.exitCode = 1; });
