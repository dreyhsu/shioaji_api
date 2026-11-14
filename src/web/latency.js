'use strict';

// Local diagnostics only. Never part of the order/retry or health decisions.
(() => {
  const commands = new Map(), received = new Map(), rendered = new Map();
  function post(body) {
    body.client_observed_at = new Date(Date.now() + 8 * 3600000).toISOString().replace('T', ' ').replace('Z', '+08:00');
    fetch('/api/latency/browser', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), keepalive: true }).catch(() => {});
  }
  window.TradeLatency = {
    commandStarted(id) {
      if (commands.size >= 256) commands.delete(commands.keys().next().value);
      commands.set(id, performance.now());
    },
    commandCompleted(id) {
      const start = commands.get(id); commands.delete(id);
      if (start !== undefined) post({ event: 'browser_command_completed', command_id: id, client_elapsed_ms: performance.now() - start });
    },
    received(snapshot) {
      if (!snapshot?.snapshot_id) return;
      if (received.size >= 256) received.delete(received.keys().next().value);
      received.set(snapshot.snapshot_id, performance.now());
    },
    rendered(snapshot, orders) {
      const start = received.get(snapshot?.snapshot_id);
      if (start === undefined) return;
      received.delete(snapshot.snapshot_id);
      // Run after a paint opportunity; a background tab may delay this sample.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        for (const order of orders) {
          const key = JSON.stringify([order.risk_revision, order.entry_status, order.exit_status,
            order.filled_quantity, order.exit_filled_quantity]);
          if (rendered.get(order.id) === key) continue;
          if (rendered.size >= 512) rendered.delete(rendered.keys().next().value);
          rendered.set(order.id, key);
          post({ event: 'browser_orders_rendered', order_id: order.id, snapshot_id: snapshot.snapshot_id,
            client_elapsed_ms: performance.now() - start });
        }
      }));
    },
  };
})();
