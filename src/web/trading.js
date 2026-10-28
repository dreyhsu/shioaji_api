'use strict';

(function () {
  const $ = (id) => document.getElementById(id);
  const number = (value, digits = 0) => value === null || value === undefined || !Number.isFinite(Number(value))
    ? '—'
    : Number(value).toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits });
  const state = { simulation: true, connected: false, price: null, atr: null, contracts: [], orders: [], selectedId: null, curves: [], curveChoices: {} };
  const riskDrafts = new Map();
  const riskSaving = new Set();
  const riskErrors = new Map();
  const cancellingOrders = new Set();
  const cancelErrors = new Map();
  let curvesListKey = null;
  let curveOptionsKey = null;
  let riskNotice = '';
  let lastMarginAt = 0;
  let lastStateAt = 0;
  let busy = false;
  let entryBusy = false;
  let entrySnapshot = null;
  let entryEnabled = false;
  let refreshing = false;
  let lastTradeStatusAt = 0;

  async function fetchRead(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      return await fetch(url, { signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

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
    $('risk-breakeven-label').textContent = `浮盈超過 ${info.breakeven_trigger_points ?? '—'} 點自動保本（保留 ${info.breakeven_offset_points ?? '—'} 點）`;
    state.mock = Boolean(info.mock);
    state.simulation = Boolean(info.simulation);
    state.connected = Boolean(info.connected);
    state.entryReady = info.bars_ready !== false && info.broker_ready !== false;
    lastTradeStatusAt = Date.now();
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
    const protection = info.protection;
    $('auto-exit-state').textContent = !info.auto_exit_enabled
      ? '自動出場：停用（僅監控提醒）'
      : protection
        ? `固定出場：${protection.fixed_exit ? '監控中' : '受影響'} · 保本：${protection.breakeven ? '監控中' : '受影響'} · 曲線出場：${protection.curve_exit ? '監控中' : '暫停'} · 券商端保護：待核對`
        : (quoteReady ? '自動市價出場：啟用' : '自動出場已啟用；等待行情 tick');
    if (info.reconciliation_error) connection.textContent = info.reconciliation_error;
    state.price = info.last_quote === null ? (state.mock ? null : state.price) : Number(info.last_quote);
    if (state.price && !$('trade-price').value) $('trade-price').value = Math.round(state.price);
    $('trade-submit').disabled = !state.connected || !state.entryReady || !selectedContract() || busy;
  }

  async function loadContracts() {
    const response = await fetchRead('/api/trading/contracts');
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
    $('trade-submit').disabled = !state.connected || !state.entryReady || !selectedContract() || busy;
    if (selectedContract()) subscribeSelectedContract();
  }

  async function loadCurves() {
    const response = await fetchRead('/api/trendlines');
    if (!response.ok) return;
    const lines = (await response.json()).filter((line) => line.kind === 'curve');
    state.curves = lines;
    const listKey = lines.map((line) => `${line.id}:${line.label}`).join('|');
    if (listKey !== curvesListKey) {
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
      curvesListKey = listKey;
    }
    renderPreview();
    renderEntry();
    renderOrders();
  }

  async function loadAccountMargin() {
    $('margin-status').textContent = '查詢中…';
    $('margin-status').classList.remove('bad', 'ok');
    try {
      const response = await fetchRead('/api/trading/margin');
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
    return $('manual-direction').querySelector('button.active')?.dataset.action || 'Buy';
  }

  function entryAction() {
    return $('entry-direction').querySelector('button.active')?.dataset.action || 'Buy';
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
    $('trade-submit').disabled = !state.connected || !state.entryReady || !contract || busy;
  }

  function escapeHtml(text) {
    const span = document.createElement('span');
    span.textContent = text;
    return span.innerHTML;
  }

  function orderStatus(order) {
    if (cancellingOrders.has(order.id)) return `取消處理中 · 券商狀態：${order.entry_status}`;
    if (order.error) return order.error;
    if (order.entry_status === 'unknown' || order.entry_status === 'submitting') return '進場結果待核對，禁止自動重送';
    if (order.active && Number(order.filled_quantity) > Number(order.exit_filled_quantity) && order.protection) {
      if (!order.protection.broker_ready) return '券商委託／部位待核對 · 自動送單暫停';
      if (!order.protection.tick_fresh) return '此合約 tick 過期 · 本機自動保護受影響';
      if (order.curve_id && !order.protection.curve_exit) return '曲線停利暫停 · 固定停損依設定監控';
    }
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
      && !order.triggered_at && !order.exit_trade_id && !order.exit_status;
  }

  function canManageCurve(order) {
    return Boolean(Number(order.active))
      && Number(order.filled_quantity) > Number(order.exit_filled_quantity)
      && !order.triggered_at && !order.exit_trade_id && !order.exit_status;
  }

  function riskValues(order) {
    return { curve_id: order.curve_id || null, stop_price: order.stop_price,
      target_price: order.target_price, breakeven_enabled: Boolean(order.breakeven_enabled),
      risk_revision: Number(order.risk_revision) };
  }

  function updateRiskDraft(id, patch) {
    const order = state.orders.find((item) => item.id === id);
    if (!order || !canManageCurve(order) || riskSaving.has(id)) return;
    const draft = riskDrafts.get(id) || riskValues(order);
    Object.assign(draft, patch);
    riskDrafts.set(id, draft);
    riskErrors.delete(id);
    riskNotice = '';
    if (state.selectedId === id) renderRiskEditor();
  }

  function curvePrice(id, order) {
    if (!id) return null;
    const curve = state.curves.find((item) => Number(item.id) === Number(id));
    if (curve) return curve.curve_price ?? null;
    return Number(order.curve_id) === Number(id) ? order.curve_price : null;
  }

  function effectiveRiskOrder(order) {
    if (!order) return null;
    const values = riskDrafts.get(order.id) || riskValues(order);
    const stop = Number(values.stop_price);
    const target = Number(values.target_price);
    return { ...order, ...values, risk_status: orderStatus(order), risk_editable: canManageCurve(order) && !riskSaving.has(order.id),
      stop_price: values.stop_price !== '' && Number.isFinite(stop) && stop > 0 ? stop : order.stop_price,
      target_price: values.curve_id ? null : values.target_price !== '' && Number.isFinite(target) && target > 0 ? target : order.target_price,
      curve_price: curvePrice(values.curve_id, order) };
  }

  function renderRiskEditor() {
    const order = state.orders.find((item) => item.id === state.selectedId);
    const editor = $('risk-editor');
    const editable = Boolean(order && canManageCurve(order));
    editor.hidden = !editable;
    if (editable) {
      const values = riskDrafts.get(order.id) || riskValues(order);
      const mode = $('risk-mode');
      const optionsKey = state.curves.map((item) => `${item.id}:${item.label}`).join('|') + '|' + values.curve_id;
      if (curveOptionsKey !== optionsKey) {
        mode.replaceChildren(new Option('固定 target／stop', ''));
        for (const curve of state.curves) mode.add(new Option(curve.label || `曲線 #${curve.id}`, String(curve.id)));
        if (values.curve_id && !state.curves.some((item) => Number(item.id) === Number(values.curve_id))) {
          mode.add(new Option(`曲線 #${values.curve_id}（暫不可用）`, String(values.curve_id)));
        }
        curveOptionsKey = optionsKey;
      }
      mode.value = values.curve_id ? String(values.curve_id) : '';
      const target = values.curve_id ? curvePrice(values.curve_id, order) : values.target_price;
      const tick = Number(order.contract_tick) || 1;
      for (const [key, value] of [['stop', values.stop_price], ['target', target]]) {
        const input = $('risk-' + key);
        input.step = tick;
        if (document.activeElement !== input) input.value = value ?? '';
      }
      $('risk-target').readOnly = Boolean(values.curve_id);
      $('risk-target').required = !values.curve_id;
      $('risk-target').placeholder = values.curve_id && target == null ? '曲線暫不可用' : '';
      $('risk-breakeven').checked = values.breakeven_enabled;
      const stale = Number(values.risk_revision) !== Number(order.risk_revision);
      let status = riskErrors.get(order.id) || riskNotice || (riskDrafts.has(order.id) ? '尚未儲存：系統仍沿用原出場設定。' : '已儲存；拖曳邊界或輸入價格後按儲存。');
      if (stale) status = '出場設定或成交狀態已更新（可能已自動保本）。草稿已保留，請按還原重新核對。';
      else if (order.breakeven_enabled && order.breakeven_triggered_at) status += ` 保本已觸發，保護價 ${number(order.breakeven_price, 2)}；stop 只能收緊。`;
      if (values.curve_id) status += ' 曲線 target 僅供顯示，依 1m 收盤穿越出場。';
      $('risk-state').textContent = status;
      $('risk-state').classList.toggle('bad', stale || riskErrors.has(order.id));
      for (const input of editor.querySelectorAll('input, select, button')) input.disabled = riskSaving.has(order.id);
      $('risk-save').disabled = riskSaving.has(order.id) || !riskDrafts.has(order.id) || stale;
    }
    window.SNRChart?.setTradeOrder(effectiveRiskOrder(order));
  }

  function changeRiskMode(id, curveId) {
    const order = state.orders.find((item) => item.id === id);
    if (!order) return;
    const draft = riskDrafts.get(id) || riskValues(order);
    const cancelling = !curveId && Boolean(draft.curve_id);
    const patch = { curve_id: curveId };
    if (cancelling) {
      const entry = Number(order.avg_fill_price);
      const direction = order.action === 'Buy' ? 1 : -1;
      const tick = Number(order.contract_tick) || 1;
      patch.stop_price = order.default_stop_price ?? Math.round((entry - direction * 2 * Number(order.atr)) / tick) * tick;
      patch.target_price = order.default_target_price ?? Math.round((entry + direction * 2 * Number(order.atr)) / tick) * tick;
      if (order.breakeven_enabled && order.breakeven_triggered_at) {
        patch.stop_price = direction > 0 ? Math.max(patch.stop_price, Number(order.stop_price)) : Math.min(patch.stop_price, Number(order.stop_price));
      }
    }
    updateRiskDraft(id, patch);
    if (cancelling && order.breakeven_enabled && order.breakeven_triggered_at) {
      riskNotice = '已帶入 ±2 ATR 預設值，stop 保留已觸發的保本保護；儲存後才生效。';
      renderRiskEditor();
    }
  }

  async function saveRisk(event) {
    event.preventDefault();
    const id = state.selectedId;
    const draft = riskDrafts.get(id);
    if (!draft || riskSaving.has(id)) return;
    const payload = { ...draft, stop_price: Number(draft.stop_price),
      target_price: draft.curve_id ? null : Number(draft.target_price) };
    riskSaving.add(id);
    renderRiskEditor();
    try {
      let result;
      for (let attempt = 0; attempt < 2; attempt++) {
        const response = await fetch(`/api/trading/orders/${encodeURIComponent(id)}/risk-settings`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        });
        result = await response.json();
        if (response.status === 409 && result.detail?.code === 'confirmation_required' && !payload.confirmed) {
          if (!await confirmOperation(result.detail.message)) return;
          payload.confirmed = true;
          continue;
        }
        if (!response.ok) throw new Error(result.detail?.message || (Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join('；') : result.detail) || '儲存出場設定失敗');
        riskDrafts.delete(id);
        riskErrors.delete(id);
        state.orders = state.orders.map((item) => item.id === id ? result : item);
        message('出場設定已儲存，系統將使用新設定監控這筆單。');
        break;
      }
    } catch (error) {
      riskErrors.set(id, error.message);
      message(error.message, true);
    } finally {
      riskSaving.delete(id);
      await refreshOrders();
      renderRiskEditor();
    }
  }

  async function cancelEntry(order) {
    const id = order.id;
    if (cancellingOrders.has(id) || !canCancelEntry(order)) return;
    const filled = Number(order.filled_quantity || 0);
    const remaining = Math.max(0, Number(order.quantity) - filled);
    const action = order.action === 'Buy' ? '買進' : '賣出';
    const text = `確定取消 ${order.contract_code} ${action}委託？\n已成交 ${filled} 口；將取消尚未成交的 ${remaining} 口。取消確認前仍可能成交；已成交部位會保留。`;
    if (!await confirmOperation(text)) return;
    if (cancellingOrders.has(id)) return;
    cancellingOrders.add(id);
    cancelErrors.delete(id);
    renderOrders();
    message('正在向券商取消未成交委託…');
    try {
      const response = await fetch(`/api/trading/orders/${encodeURIComponent(order.id)}/cancel`, { method: 'POST' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || '取消委託失敗');
      state.orders = state.orders.map((item) => item.id === result.id ? result : item);
      renderOrders();
      let current = result;
      message('取消要求已送出，正在等待券商確認…');
      for (let attempt = 0; attempt < 10 && !['Cancelled', 'Filled', 'Failed', 'Rejected'].includes(current.entry_status); attempt++) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const statusResponse = await fetchRead('/api/trading/orders');
        if (!statusResponse.ok) throw new Error('取消後委託狀態查詢失敗，請核對券商端');
        acceptOrders(await statusResponse.json());
        current = state.orders.find((item) => item.id === id);
        if (!current) throw new Error('找不到取消後的委託狀態，請核對券商端');
        renderOrders();
      }
      if (current.entry_status === 'Cancelled') {
        message(Number(current.filled_quantity)
          ? `未成交餘量已取消；保留 ${current.filled_quantity} 口成交部位。` : '券商已確認取消委託。');
      } else if (current.entry_status === 'Filled') {
        throw new Error(`取消未及完成，委託已全部成交 ${current.filled_quantity} 口；請確認持倉。`);
      } else {
        throw new Error(`取消尚未確認，券商狀態：${current.entry_status}。請核對券商端；系統會繼續更新委託狀態。`);
      }
    } catch (error) {
      cancelErrors.set(id, error.message);
      message(error.message, true);
    } finally {
      cancellingOrders.delete(id);
      await Promise.all([
        refreshOrders().catch((error) => cancelErrors.set(id, '委託更新失敗：' + error.message)),
        loadAccountMargin(),
      ]);
      renderOrders();
    }
  }

  function renderOrders() {
    const list = $('managed-orders');
    if (!state.orders.length) {
      list.innerHTML = '<div class="empty">尚無委託</div>';
      renderRiskEditor();
      return;
    }
    const active = state.orders.filter((order) => Number(order.active));
    if (!state.selectedId || !state.orders.some((order) => order.id === state.selectedId)) {
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
        actions.push(`<button type="button" data-edit-risk="${escapeHtml(order.id)}">編輯出場</button>`);
      }
      if (canCancelEntry(order)) actions.push(`<button type="button" class="cancel-entry" data-cancel-order="${escapeHtml(order.id)}" ${cancellingOrders.has(order.id) ? 'disabled' : ''}>${cancellingOrders.has(order.id) ? '等待取消確認…' : filled ? '取消未成交餘量' : '取消委託'}</button>`);
      if (cancelErrors.has(order.id)) actions.push(`<span class="cancel-error" role="alert">${escapeHtml(cancelErrors.get(order.id))}</span>`);
      const actionMarkup = actions.length ? `<div class="managed-order-actions">${actions.join('')}</div>` : '';
      return `<div class="managed-order ${selected ? 'selected' : ''}" data-order-id="${escapeHtml(order.id)}" tabindex="0">
        <div class="managed-order-main"><b>${escapeHtml(order.contract_code)} · ${side} ${filled}/${order.quantity} 口</b><br>
          <span class="managed-order-sub">${entryLabel} ${number(entry)} · 停損 ${number(order.stop_price)} · ${order.curve_id ? '1h 曲線；1m 收盤穿越停利' : `固定停利 ${number(order.target_price)}`}</span>
        </div><span class="order-status ${order.active ? 'active' : ''}">${escapeHtml(orderStatus(order))}</span>${actionMarkup}</div>`;
    }).join('');
    list.querySelectorAll('[data-order-id]').forEach((item) => {
      item.addEventListener('click', () => selectOrder(item.dataset.orderId));
      item.addEventListener('keydown', (event) => {
        if (event.target === item && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          selectOrder(item.dataset.orderId);
        }
      });
    });
    list.querySelectorAll('[data-cancel-order]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const order = state.orders.find((item) => item.id === button.dataset.cancelOrder);
        if (order) cancelEntry(order);
      });
    });
    list.querySelectorAll('[data-edit-risk]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        selectOrder(button.dataset.editRisk);
        $('risk-editor').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    });
    renderRiskEditor();
  }

  function selectOrder(id) {
    state.selectedId = id;
    riskNotice = '';
    renderOrders();
  }

  function acceptOrders(orders) {
    const cancellationConfirmed = orders.some((order) => {
      const current = state.orders.find((item) => item.id === order.id);
      return current && current.entry_status !== 'Cancelled' && order.entry_status === 'Cancelled';
    });
    // A response begun before a save must not roll back its newer revision.
    state.orders = orders.map((order) => {
      const current = state.orders.find((item) => item.id === order.id);
      return current && Number(current.risk_revision) > Number(order.risk_revision) ? current : order;
    });
    for (const id of riskDrafts.keys()) if (!orders.some((order) => order.id === id)) riskDrafts.delete(id);
    for (const order of state.orders) if (order.entry_status === 'Cancelled') cancelErrors.delete(order.id);
    if (cancellationConfirmed) void loadAccountMargin();
  }

  async function refreshOrders() {
    const response = await fetchRead('/api/trading/orders');
    if (!response.ok) return;
    acceptOrders(await response.json());
    renderOrders();
  }

  async function refreshTrading() {
    if (refreshing) return;
    refreshing = true;
    try {
      const [response, stateResponse] = await Promise.all([
        fetchRead('/api/trading/status'),
        state.mock || Date.now() - lastStateAt > 30000 ? fetchRead('/api/state') : Promise.resolve(null),
      ]);
      if (!response.ok) throw new Error('無法確認交易服務健康狀態');
      const info = await response.json();
      setMode(info);
      if (info.orders) acceptOrders(info.orders);
      await loadCurves();
      renderOrders();
      if (state.contracts.length === 0 && info.connected) await loadContracts();
      if ((state.mock || Date.now() - lastMarginAt > 30000) && info.connected) await loadAccountMargin();
      if (stateResponse) {
        if (stateResponse.ok) state.atr = (await stateResponse.json()).hourly_atr;
        lastStateAt = Date.now();
      }
    } catch (error) {
      state.connected = state.entryReady = false;
      $('trade-connection').textContent = '連線狀態查詢失敗：' + error.message;
      $('trade-connection').className = 'trade-connection bad';
      $('auto-exit-state').textContent = '保護狀態無法確認，請檢查獨立告警與券商端';
    } finally {
      refreshing = false;
    }
    renderPreview();
    renderEntry();
  }

  function renderEntry() {
    const entry = entrySnapshot;
    const side = entry?.action === 'Sell' ? '向下突破做空' : '向上突破做多';
    const labels = { armed: '等待' + side, submitting: '送單中，請勿重複送單', triggered: '已觸發' + (entry?.action === 'Sell' ? '做空' : '做多'), failed: '已停止，請核對委託狀態', cancelled: '已取消' };
    $('entry-state').textContent = entry
      ? `${entry.contract_code} · 曲線 #${entry.curve_id} · ${entry.quantity} 口 · ${side}：${labels[entry.state] || entry.state}${entry.error ? ' · ' + entry.error : ''}`
      : entryEnabled ? '尚未啟用曲線進場' : '自動進場：停用，請檢查 config/trading.json';
    if (entry?.state === 'armed' && !state.entryReady) $('entry-state').textContent += ' · 行情／券商未就緒，恢復後等待新訊號';
    const quantity = Number($('entry-quantity').value);
    const validCurve = state.curves.some((curve) => String(curve.id) === $('entry-curve').value);
    $('entry-arm').textContent = '啟用一次突破' + (entryAction() === 'Sell' ? '做空' : '做多');
    $('entry-arm').disabled = entryBusy || !state.connected || !entryEnabled || !selectedContract()
      || !validCurve || !Number.isInteger(quantity) || quantity < 1 || quantity > 100
      || !(Number(state.atr) > 0) || ['armed', 'submitting'].includes(entry?.state);
    $('entry-cancel').disabled = entryBusy || entry?.state !== 'armed';
  }

  async function controlEntry(cancel = false) {
    if (entryBusy) return;
    const curveId = Number($('entry-curve').value);
    const quantity = Number($('entry-quantity').value);
    const contract = selectedContract();
    const simulation = state.simulation;
    const action = entryAction();
    if (!cancel) {
      if (!curveId || !contract || !Number.isInteger(quantity) || quantity < 1 || quantity > 100) {
        $('entry-message').textContent = '請先保存曲線、選擇合約，並輸入 1 到 100 的整數口數。';
        return;
      }
      const mode = state.mock ? '離線 Mock' : simulation ? '券商 Simulation' : '正式交易';
      const side = action === 'Sell' ? '向下突破做空（賣出）' : '向上突破做多（買進）';
      if (!await confirmOperation(`環境：${mode}\n合約：${contract.code}\n曲線：#${curveId}\n方向：${side}\n口數：${quantity}\n委託：市價 MKT / IOC\n僅觸發一次，依成交均價設定停損、停利各 2 倍進場 1h ATR。\n確認啟用？`)) return;
    }
    entryBusy = true;
    renderEntry();
    try {
      const response = await fetch('/api/trading/curve-entry', {
        method: cancel ? 'DELETE' : 'POST', headers: { 'Content-Type': 'application/json' },
        ...(cancel ? {} : { body: JSON.stringify({ curve_id: curveId, contract_code: contract.code,
          action, quantity, simulation, confirmed: true }) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '請檢查輸入欄位');
      $('entry-message').textContent = cancel ? '已取消等待進場。' : `已啟用一次突破${action === 'Sell' ? '做空' : '做多'}。`;
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
  $('trade-contract').addEventListener('change', () => { subscribeSelectedContract(); renderPreview(); renderEntry(); });
  $('trade-price').addEventListener('input', renderPreview);
  $('trade-quantity').addEventListener('input', renderPreview);
  $('trade-curve').addEventListener('change', renderPreview);
  for (const id of ['manual-direction', 'entry-direction']) {
    const buttons = $(id).querySelectorAll('[data-action]');
    buttons.forEach((button) => {
      button.setAttribute('aria-pressed', String(button.classList.contains('active')));
      button.addEventListener('click', () => {
        buttons.forEach((item) => {
          item.classList.toggle('active', item === button);
          item.setAttribute('aria-pressed', String(item === button));
        });
        id === 'manual-direction' ? renderPreview() : renderEntry();
      });
    });
  }
  const tabs = Array.from(document.querySelectorAll('.trade-tabs [role="tab"]'));
  function selectTab(tab) {
    tabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
      $(item.getAttribute('aria-controls')).hidden = !selected;
    });
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', (event) => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;
      event.preventDefault();
      selectTab(tabs[next]);
      tabs[next].focus();
    });
  });
  $('entry-curve').addEventListener('change', renderEntry);
  $('entry-quantity').addEventListener('input', renderEntry);
  $('trade-submit').addEventListener('click', submitOrder);
  $('margin-refresh').addEventListener('click', loadAccountMargin);
  $('entry-arm').addEventListener('click', () => controlEntry());
  $('entry-cancel').addEventListener('click', () => controlEntry(true));

  $('risk-editor').addEventListener('submit', saveRisk);
  $('risk-mode').addEventListener('change', (event) => changeRiskMode(state.selectedId, event.target.value ? Number(event.target.value) : null));
  for (const key of ['stop', 'target']) {
    $('risk-' + key).addEventListener('input', (event) => updateRiskDraft(state.selectedId, { [key + '_price']: event.target.value }));
  }
  $('risk-breakeven').addEventListener('change', (event) => updateRiskDraft(state.selectedId, { breakeven_enabled: event.target.checked }));
  $('risk-reset').addEventListener('click', () => {
    riskDrafts.delete(state.selectedId);
    riskErrors.delete(state.selectedId);
    riskNotice = '';
    renderRiskEditor();
  });

  window.TradingUI = { updateRiskDraft, message, refreshOrders, refresh: refreshTrading, refreshCurves: loadCurves, confirm: confirmOperation };
  document.addEventListener('trendlines-changed', () => loadCurves().catch((error) => message(error.message, true)));
  fetchRead('/api/state').then((response) => response.json()).then((data) => {
    state.atr = data.hourly_atr;
    renderPreview();
  }).catch(() => {});
  loadCurves().catch((error) => message(error.message, true));
  refreshTrading();
  setInterval(() => {
    if (lastTradeStatusAt && Date.now() - lastTradeStatusAt > 15000) {
      state.connected = state.entryReady = false;
      $('trade-connection').textContent = '交易狀態更新逾時';
      $('trade-connection').className = 'trade-connection bad';
      $('auto-exit-state').textContent = '保護狀態無法確認，請檢查獨立告警與券商端';
      renderPreview();
      renderEntry();
    }
    refreshTrading();
  }, 2500);
})();
