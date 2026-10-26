'use strict';

(function () {
  const $ = (id) => document.getElementById(id);
  const number = (value, digits = 0) => value === null || value === undefined || !Number.isFinite(Number(value))
    ? '—'
    : Number(value).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits });
  const state = { simulation: true, connected: false, price: null, atr: null, contracts: [], orders: [], selectedId: null, curves: [], curveChoices: {} };
  let lastMarginAt = 0;
  let lastStateAt = 0;
  let busy = false;
  let entryBusy = false;
  let entrySnapshot = null;
  let entryEnabled = false;

  async function confirmOperation(text) {
    const dialog = $('trade-confirm');
    if (dialog.open) return false;
    $('trade-confirm-summary').textContent = text;
    dialog.returnValue = '';
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
      dialog.showModal();
    });
  }

  function message(text, bad = false) {
    $('trade-message').textContent = text || '';
    $('trade-message').classList.toggle('bad', Boolean(bad));
  }

  function selectedContract() {
    return state.contracts.find((item) => item.code === $('trade-contract').value) || null;
  }

  function setMode(info) {
    state.mock = Boolean(info.mock);
    state.simulation = Boolean(info.simulation);
    state.connected = Boolean(info.connected);
    entryEnabled = Boolean(info.auto_entry_enabled);
    entrySnapshot = info.curve_entry || null;
    renderEntry();
    const badge = $('trade-mode');
    badge.textContent = info.mode || (state.simulation ? '模擬環境' : '正式環境');
    badge.className = 'trade-badge ' + (state.simulation ? 'simulation' : 'production');
    const connection = $('trade-connection');
    const quoteReady = Boolean(info.quote_connected);
    connection.textContent = info.connected
      ? (quoteReady ? `行情連線 · ${number(info.last_quote)} · ${info.last_quote_at || ''}` : '帳戶已連線，等待即時行情 tick')
      : (info.error || '尚未連線');
    connection.className = 'trade-connection ' + (info.connected && quoteReady ? 'ok' : 'bad');
    $('auto-exit-state').textContent = info.auto_exit_enabled
      ? (quoteReady ? '自動市價出場：啟用' : '自動出場已啟用；等待行情 tick')
      : '自動出場：停用（僅監控提醒）';
    state.price = info.last_quote === null ? (state.mock ? null : state.price) : Number(info.last_quote);
    if (state.price && !$('trade-price').value) $('trade-price').value = Math.round(state.price);
    $('trade-submit').disabled = !info.connected || !selectedContract() || busy;
  }

  async function loadContracts() {
    const response = await fetch('/api/trading/contracts');
    if (!response.ok) throw new Error((await response.json()).detail || '合約載入失敗');
    state.contracts = await response.json();
    const select = $('trade-contract');
    const previous = select.value;
    select.replaceChildren(...state.contracts.map((item) => {
      const option = document.createElement('option');
      option.value = item.code;
      option.textContent = `${item.delivery_month} · ${item.name} (${item.code})`;
      return option;
    }));
    select.disabled = state.contracts.length === 0;
    if (state.contracts.some((item) => item.code === previous)) select.value = previous;
    else if (state.contracts.length) select.selectedIndex = 0;
    $('trade-submit').disabled = !state.connected || !selectedContract() || busy;
    if (selectedContract()) subscribeSelectedContract();
  }

  async function loadCurves() {
    const response = await fetch('/api/trendlines');
    if (!response.ok) return;
    const lines = (await response.json()).filter((line) => line.kind === 'curve');
    state.curves = lines;
    const select = $('trade-curve');
    const previous = select.value;
    select.replaceChildren(new Option('固定 +2 ATR', ''));
    for (const line of lines) {
      select.add(new Option(line.label || `曲線 #${line.id}`, String(line.id)));
    }
    if (lines.some((line) => String(line.id) === previous)) select.value = previous;
    const entrySelect = $('entry-curve');
    const entryPrevious = entrySelect.value;
    entrySelect.replaceChildren(new Option('選擇手動保存的 1h 曲線', ''));
    for (const line of lines) entrySelect.add(new Option(line.label || `曲線 #${line.id}`, String(line.id)));
    if (lines.some((line) => String(line.id) === entryPrevious)) entrySelect.value = entryPrevious;
    renderPreview();
    renderOrders();
  }

  async function loadAccountMargin() {
    $('margin-status').textContent = '查詢中…';
    $('margin-status').classList.remove('bad', 'ok');
    try {
      const response = await fetch('/api/trading/margin');
      if (!response.ok) throw new Error((await response.json()).detail || '查詢失敗');
      const result = await response.json();
      $('margin-risk').textContent = result.risk_indicator === null ? '—' : number(result.risk_indicator, 1);
      $('margin-maintenance').textContent = result.maintenance_margin === null ? '—' : number(result.maintenance_margin);
      $('margin-initial').textContent = result.initial_margin === null ? '—' : number(result.initial_margin);
      $('margin-available').textContent = result.available_margin === null ? '—' : number(result.available_margin);
      $('margin-status').textContent = `${result.status || '已更新'} · ${result.fetched_at || ''}`;
      $('margin-status').classList.add('ok');
    } catch (error) {
      $('margin-status').textContent = '查詢失敗：' + error.message;
      $('margin-status').classList.add('bad');
    }
    lastMarginAt = Date.now();
  }

  function currentAction() {
    return document.querySelector('.trade-toggle button.active')?.dataset.action || 'Buy';
  }

  function renderPreview() {
    const priceType = $('trade-price-type').value;
    const entry = priceType === 'LMT' ? Number($('trade-price').value) : Number(state.price);
    const atr = Number(state.atr);
    const action = currentAction();
    const preview = $('trade-preview');
    const contract = selectedContract();
    if (priceType !== 'LMT') $('trade-price').disabled = true;
    else $('trade-price').disabled = false;
    if (!contract || !entry || !atr || !Number.isFinite(entry) || !Number.isFinite(atr)) {
      preview.textContent = '需要有效商品、價格與 1 小時 ATR 才能計算出場價';
      $('trade-submit').disabled = true;
      return;
    }
    const direction = action === 'Buy' ? 1 : -1;
    const stop = entry - direction * 2 * atr;
    const target = entry + direction * 2 * atr;
    const curve = $('trade-curve').value;
    preview.innerHTML = `基準 ${priceType === 'LMT' ? '委託價' : '最新成交價'} ${number(entry)} · ATR(1h) ${number(atr, 1)}<br>`
      + `停損 <b>${number(stop)}</b> · `
      + (curve ? `移動停利：${escapeHtml($('trade-curve').selectedOptions[0]?.textContent || '已選曲線')}` : `停利 <b>${number(target)}</b>`);
    $('trade-submit').disabled = !state.connected || !contract || busy;
  }

  function escapeHtml(text) {
    const span = document.createElement('span');
    span.textContent = text;
    return span.innerHTML;
  }

  function orderStatus(order) {
    if (order.error) return order.error;
    if (order.entry_status === 'CancelRequested') return '取消送出中';
    if (order.exit_status === 'waiting_entry_cancel' || order.exit_status === 'cancelling_entry') return '取消進場餘量中';
    if (order.exit_status === 'submitting') return '出場委託送出中';
    if (order.exit_status === 'unknown') return '出場狀態不明，請核對券商端';
    if (order.trigger_reason && !order.exit_trade_id) return '已觸發 · 自動出場停用';
    if (order.exit_trade_id && Number(order.exit_filled_quantity) > 0 && !order.active) return '已平倉';
    if (Number(order.filled_quantity) > Number(order.exit_filled_quantity)) {
      if (order.entry_status === 'Filled') return '持倉監控中';
      if (order.entry_status === 'Cancelled') return `部分成交 ${order.filled_quantity} 口 · 餘量已取消`;
      return `部分成交 ${order.filled_quantity}/${order.quantity} 口`;
    }
    if (['Cancelled', 'Failed', 'Rejected'].includes(order.entry_status)) return order.entry_status === 'Cancelled' ? '已取消' : order.entry_status;
    return `未成交 · ${order.entry_status || '委託中'}`;
  }

  function canCancelEntry(order) {
    return Boolean(Number(order.active))
      && !['Filled', 'Cancelled', 'Failed', 'Rejected'].includes(order.entry_status)
      && !order.triggered_at && !order.exit_trade_id;
  }

  function canManageCurve(order) {
    return Boolean(Number(order.active))
      && Number(order.filled_quantity) > Number(order.exit_filled_quantity)
      && !order.triggered_at && !order.exit_trade_id;
  }

  async function applyOrderCurve(order, button) {
    const select = button.parentElement.querySelector('[data-order-curve]');
    const curveId = select.value ? Number(select.value) : null;
    const choice = curveId
      ? `1 小時曲線 #${curveId}；多單在 1 分 K 收盤跌破曲線時停利，空單反向。原 -2 ATR 停損保留，固定 +2 ATR 停利停用。`
      : '固定 +2 ATR 停利；原停損維持不變。';
    if (!await confirmOperation(`變更 ${order.contract_code} 這筆持倉的停利方式？\n${choice}`)) return;
    button.disabled = true;
    try {
      const response = await fetch(`/api/trading/orders/${encodeURIComponent(order.id)}/curve-selection`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ curve_id: curveId }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '變更停利方式失敗');
      delete state.curveChoices[order.id];
      state.orders = state.orders.map((item) => item.id === result.id ? result : item);
      renderOrders();
      message(curveId ? '已啟用 1 小時曲線；停利將依 1 分 K 收盤穿越判斷。' : '已恢復固定 +2 ATR 停利。');
    } catch (error) {
      message(error.message, true);
    } finally {
      await refreshOrders();
    }
  }

  async function cancelEntry(order, button) {
    const filled = Number(order.filled_quantity || 0);
    const remaining = Math.max(0, Number(order.quantity) - filled);
    const action = order.action === 'Buy' ? '買進' : '賣出';
    const text = `確定取消 ${order.contract_code} ${action}委託？\n已成交 ${filled} 口；將取消尚未成交的 ${remaining} 口。已成交部位會保留並繼續監控停損停利。`;
    if (!await confirmOperation(text)) return;
    button.disabled = true;
    message('正在向券商取消未成交委託…');
    try {
      const response = await fetch(`/api/trading/orders/${encodeURIComponent(order.id)}/cancel`, { method: 'POST' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '取消委託失敗');
      state.orders = state.orders.map((item) => item.id === result.id ? result : item);
      renderOrders();
      message(result.entry_status === 'Cancelled'
        ? (Number(result.filled_quantity) ? `未成交餘量已取消；保留 ${result.filled_quantity} 口成交部位。` : '委託已取消。')
        : `取消要求已送出，券商目前狀態：${result.entry_status}`);
    } catch (error) {
      message(error.message, true);
    } finally {
      await refreshOrders();
    }
  }

  function renderOrders() {
    const list = $('managed-orders');
    // Polling must not close a dropdown or discard a user's pending selection.
    if (list.contains(document.activeElement) && document.activeElement.matches('[data-order-curve]')) return;
    if (!state.orders.length) {
      list.innerHTML = '<div class="empty">尚無委託</div>';
      window.SNRChart?.setTradeOrder(null);
      return;
    }
    const active = state.orders.filter((order) => Number(order.active));
    if (!state.selectedId || !active.some((order) => order.id === state.selectedId)) {
      state.selectedId = (active[0] || state.orders[0]).id;
    }
    list.innerHTML = state.orders.map((order) => {
      const selected = order.id === state.selectedId;
      const side = order.action === 'Buy' ? '多單' : '空單';
      const filled = Number(order.filled_quantity || 0);
      const entry = filled > 0 ? order.avg_fill_price : order.price;
      const entryLabel = filled > 0 ? '成交均價' : '委託價';
      const actions = [];
      if (canManageCurve(order)) {
        const chosen = state.curveChoices[order.id] ?? (order.curve_id ? String(order.curve_id) : '');
        const curveOptions = state.curves.map((line) =>
          `<option value="${line.id}" ${chosen === String(line.id) ? 'selected' : ''}>${escapeHtml(line.label || `曲線 #${line.id}`)}</option>`
        ).join('');
        actions.push(`<select data-order-curve="${escapeHtml(order.id)}" aria-label="${escapeHtml(order.contract_code)} 停利方式"><option value="" ${chosen === '' ? 'selected' : ''}>固定 +2 ATR</option>${curveOptions}</select><button type="button" data-apply-curve="${escapeHtml(order.id)}">套用停利</button>`);
      }
      if (canCancelEntry(order)) actions.push(`<button type="button" class="cancel-entry" data-cancel-order="${escapeHtml(order.id)}">${filled ? '取消未成交餘量' : '取消委託'}</button>`);
      const actionMarkup = actions.length ? `<div class="managed-order-actions">${actions.join('')}</div>` : '';
      return `<div class="managed-order ${selected ? 'selected' : ''}" data-order-id="${escapeHtml(order.id)}" tabindex="0">
        <div class="managed-order-main"><b>${escapeHtml(order.contract_code)} · ${side} ${filled}/${order.quantity} 口</b><br>
          <span class="managed-order-sub">${entryLabel} ${number(entry)} · 停損 ${number(order.stop_price)} · ${order.curve_id ? '1h 曲線；1m 收盤穿越停利' : `固定停利 ${number(order.target_price)}`}</span>
        </div><span class="order-status ${order.active ? 'active' : ''}">${escapeHtml(orderStatus(order))}</span>${actionMarkup}</div>`;
    }).join('');
    list.querySelectorAll('[data-order-id]').forEach((item) => {
      item.addEventListener('click', () => selectOrder(item.dataset.orderId));
      item.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') selectOrder(item.dataset.orderId);
      });
    });
    list.querySelectorAll('[data-cancel-order]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const order = state.orders.find((item) => item.id === button.dataset.cancelOrder);
        if (order) cancelEntry(order, button);
      });
    });
    list.querySelectorAll('[data-apply-curve]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const order = state.orders.find((item) => item.id === button.dataset.applyCurve);
        if (order) applyOrderCurve(order, button);
      });
    });
    list.querySelectorAll('[data-order-curve]').forEach((select) => {
      select.addEventListener('click', (event) => event.stopPropagation());
      select.addEventListener('change', (event) => {
        state.curveChoices[select.dataset.orderCurve] = select.value;
        event.stopPropagation();
      });
    });
    window.SNRChart?.setTradeOrder(state.orders.find((order) => order.id === state.selectedId) || null);
  }

  function selectOrder(id) {
    state.selectedId = id;
    renderOrders();
  }

  async function refreshOrders() {
    const response = await fetch('/api/trading/orders');
    if (!response.ok) return;
    state.orders = await response.json();
    renderOrders();
  }

  async function refreshTrading() {
    try {
      const [response, stateResponse] = await Promise.all([
        fetch('/api/trading/status'),
        state.mock || Date.now() - lastStateAt > 30000 ? fetch('/api/state') : Promise.resolve(null),
      ]);
      const info = await response.json();
      setMode(info);
      if (info.orders) state.orders = info.orders;
      renderOrders();
      if (state.contracts.length === 0 && info.connected) await loadContracts();
      if ((state.mock || Date.now() - lastMarginAt > 30000) && info.connected) await loadAccountMargin();
      if (stateResponse) {
        if (stateResponse.ok) state.atr = (await stateResponse.json()).hourly_atr;
        lastStateAt = Date.now();
      }
    } catch (error) {
      $('trade-connection').textContent = '連線狀態查詢失敗：' + error.message;
      $('trade-connection').className = 'trade-connection bad';
    }
    renderPreview();
    renderEntry();
  }

  function renderEntry() {
    const entry = entrySnapshot;
    const labels = { armed: '等待向上突破', submitting: '送單中，請勿重複送單', triggered: '已觸發買入', failed: '已停止，請核對委託狀態', cancelled: '已取消' };
    $('entry-state').textContent = entry
      ? `曲線 #${entry.curve_id}：${labels[entry.state] || entry.state}${entry.error ? ' · ' + entry.error : ''}`
      : entryEnabled ? '尚未啟用曲線買入' : '自動進場：停用，請檢查 config/trading.json';
    $('entry-arm').disabled = entryBusy || !state.connected || !entryEnabled || !selectedContract()
      || !(Number(state.atr) > 0) || ['armed', 'submitting'].includes(entry?.state);
    $('entry-cancel').disabled = entryBusy || entry?.state !== 'armed';
  }

  async function controlEntry(cancel = false) {
    if (entryBusy) return;
    const curveId = Number($('entry-curve').value);
    const quantity = Number($('entry-quantity').value);
    const contract = selectedContract();
    const simulation = state.simulation;
    if (!cancel) {
      if (!curveId || !contract || !Number.isInteger(quantity) || quantity < 1 || quantity > 100) {
        $('entry-message').textContent = '請先保存曲線、選擇合約，並輸入 1 到 100 的整數口數。';
        return;
      }
      const mode = state.mock ? '離線 Mock' : simulation ? '券商 Simulation' : '正式交易';
      if (!await confirmOperation(`環境：${mode}\n合約：${contract.code}\n啟用曲線 #${curveId} 向上突破後市價買入 ${quantity} 口？\n僅觸發一次，成交後設定 ±2 ATR。`)) return;
    }
    entryBusy = true;
    renderEntry();
    try {
      const response = await fetch('/api/trading/curve-entry', {
        method: cancel ? 'DELETE' : 'POST', headers: { 'Content-Type': 'application/json' },
        ...(cancel ? {} : { body: JSON.stringify({ curve_id: curveId, contract_code: contract.code,
          quantity, simulation, confirmed: true }) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '請檢查輸入欄位');
      $('entry-message').textContent = cancel ? '已取消等待買入。' : '已啟用一次突破買入。';
      await refreshTrading();
    } catch (error) {
      $('entry-message').textContent = error.message;
    } finally {
      entryBusy = false;
      renderEntry();
    }
  }

  async function subscribeSelectedContract() {
    const contract = selectedContract();
    if (!contract || !state.connected) return;
    try {
      const response = await fetch('/api/trading/subscribe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ contract_code: contract.code }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || '行情訂閱失敗');
    } catch (error) {
      $('trade-connection').textContent = error.message;
      $('trade-connection').className = 'trade-connection bad';
    }
  }

  async function submitOrder() {
    if (busy) return;
    const simulation = state.simulation;
    const contract = selectedContract();
    const action = currentAction();
    const quantity = Number($('trade-quantity').value);
    const priceType = $('trade-price-type').value;
    const orderType = $('trade-order-type').value;
    const price = Number($('trade-price').value || 0);
    const curveId = $('trade-curve').value ? Number($('trade-curve').value) : null;
    if (!contract || !Number.isInteger(quantity) || quantity < 1 || (priceType === 'LMT' && !(price > 0))) {
      message('請確認月份、口數與委託價格。', true);
      return;
    }
    const entry = priceType === 'LMT' ? price : state.price;
    const atr = state.atr;
    const side = action === 'Buy' ? '多單' : '空單';
    const stop = entry && atr ? entry - (action === 'Buy' ? 1 : -1) * 2 * atr : null;
    const target = entry && atr ? entry + (action === 'Buy' ? 1 : -1) * 2 * atr : null;
    const summary = `環境：${simulation ? '模擬' : '正式'}\n商品：${contract.delivery_month} ${contract.name}\n方向：${side} · ${quantity} 口\n委託：${priceType} / ${orderType}${priceType === 'LMT' ? ' / ' + number(price) : ''}\n停損：${number(stop)}\n停利：${curveId ? '1h 曲線，1m K 收盤穿越觸發' : number(target)}\n\n確認送單？`;
    if (!await confirmOperation(summary)) return;
    busy = true;
    $('trade-submit').disabled = true;
    message('委託送出中…');
    try {
      const response = await fetch('/api/trading/orders', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          contract_code: contract.code, action, quantity, price, price_type: priceType,
          order_type: orderType, curve_id: curveId, simulation, confirmed: true,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '委託失敗');
      message(`已送出委託 ${result.entry_trade_id}；狀態 ${result.entry_status || '處理中'}`);
      state.selectedId = result.id;
      await refreshTrading();
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      renderPreview();
    }
  }

  $('trade-price-type').addEventListener('change', renderPreview);
  $('trade-contract').addEventListener('change', () => { subscribeSelectedContract(); renderPreview(); });
  $('trade-price').addEventListener('input', renderPreview);
  $('trade-quantity').addEventListener('input', renderPreview);
  $('trade-curve').addEventListener('change', renderPreview);
  document.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('[data-action]').forEach((item) => item.classList.toggle('active', item === button));
    renderPreview();
  }));
  $('trade-submit').addEventListener('click', submitOrder);
  $('margin-refresh').addEventListener('click', loadAccountMargin);
  $('entry-arm').addEventListener('click', () => controlEntry());
  $('entry-cancel').addEventListener('click', () => controlEntry(true));

  window.TradingUI = { message, refreshOrders, refresh: refreshTrading, refreshCurves: loadCurves, confirm: confirmOperation };
  document.addEventListener('trendlines-changed', () => loadCurves().catch((error) => message(error.message, true)));
  fetch('/api/state').then((response) => response.json()).then((data) => {
    state.atr = data.hourly_atr;
    renderPreview();
  }).catch(() => {});
  loadCurves();
  refreshTrading();
  setInterval(refreshTrading, 2500);
})();
