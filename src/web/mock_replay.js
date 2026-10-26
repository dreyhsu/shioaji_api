'use strict';

(async function () {
  const $ = (id) => document.getElementById(id);
  let snapshot = null;
  let lastCursor = null;
  let busy = false;

  function confirmMock(text) {
    const dialog = $('mock-confirm');
    if (dialog.open) return Promise.resolve(false);
    $('mock-confirm-summary').textContent = text;
    dialog.returnValue = '';
    return new Promise((resolve) => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
      dialog.showModal();
    });
  }
  window.MockReplay = { enabled: false, confirm: confirmMock };

  function message(text, bad = false) {
    $('mock-message').textContent = text;
    $('mock-message').classList.toggle('bad', bad);
  }

  async function request(url, body, method = 'POST') {
    const response = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      ...(body === null ? {} : { body: JSON.stringify(body) }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '請檢查輸入欄位');
    return result;
  }

  function render(info) {
    snapshot = info;
    const start = info.replay.start_bar !== null && info.replay.start_bar !== undefined
      ? `初始載入 ${info.replay.start_bar} 根 K 棒`
      : info.replay.start_at === 'before_pullback'
        ? `初始停在反彈結束、回檔前 ${info.replay.minutes_before_pullback} 分鐘`
        : `初始停在谷底前 ${info.replay.minutes_before_bottom} 分鐘`;
    $('mock-timing').textContent = `${start}；每 ${info.replay.interval_seconds} 秒播放 1 根 1 分 K。`;
    $('mock-phase').textContent = info.phase;
    $('mock-play').textContent = info.playing ? '暫停' : '播放';
    $('mock-progress').textContent = `${info.time} · ${info.cursor}/${info.total} 根 · 現價 ${info.price ?? '—'}`;
    const entry = info.entry;
    const labels = { armed: '等待向上突破', submitting: '送單中', triggered: '已觸發買入', failed: '已停止', cancelled: '已取消' };
    $('mock-entry-state').textContent = entry
      ? `曲線 #${entry.curve_id}：${labels[entry.state] || entry.state}${entry.error ? ' · ' + entry.error : ''}`
      : '尚未啟用曲線買入';
    $('mock-entry-arm').disabled = busy || ['armed', 'submitting'].includes(entry?.state);
    $('mock-entry-cancel').disabled = busy || entry?.state !== 'armed';
    $('mock-play').disabled = busy || info.remaining === 0;
    document.querySelectorAll('[data-mock-step]').forEach((button) => { button.disabled = busy || info.remaining === 0; });
  }

  async function refreshViews() {
    await Promise.all([window.SNRChart?.refresh(), window.TradingUI?.refresh()]);
  }

  async function curves() {
    const response = await fetch('/api/trendlines');
    if (!response.ok) return;
    const select = $('mock-entry-curve');
    const previous = select.value;
    select.replaceChildren(new Option('選擇手動保存的 1h 曲線', ''));
    for (const line of await response.json()) {
      if (line.kind === 'curve') select.add(new Option(line.label || `曲線 #${line.id}`, String(line.id)));
    }
    if (Array.from(select.options).some((option) => option.value === previous)) select.value = previous;
  }

  async function run(action) {
    if (busy) return;
    busy = true;
    render(snapshot);
    try {
      await action();
      const response = await fetch('/api/mock');
      render(await response.json());
      lastCursor = snapshot.cursor;
      await refreshViews();
      message('');
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      render(snapshot);
    }
  }

  let initial;
  try {
    const response = await fetch('/api/mock');
    if (!response.ok) return;
    initial = await response.json();
  } catch (_) { return; }
  if (!initial.enabled) return;
  window.MockReplay.enabled = true;
  $('mock-panel').hidden = false;
  render(initial);
  lastCursor = initial.cursor;
  await curves();

  $('mock-play').addEventListener('click', () => run(() => request('/api/mock/control', { action: snapshot.playing ? 'pause' : 'play' })));
  document.querySelectorAll('[data-mock-step]').forEach((button) => button.addEventListener('click', () => run(() =>
    request('/api/mock/control', { action: 'step', steps: Number(button.dataset.mockStep) }))));
  $('mock-reset').addEventListener('click', async () => {
    if (!await confirmMock('清除本次 Mock 委託、曲線與進場設定，從設定的回放起點重新開始？')) return;
    run(async () => {
      await request('/api/mock/control', { action: 'reset', confirmed: true });
      await curves();
      await window.TradingUI?.refreshCurves();
    });
  });
  $('mock-entry-arm').addEventListener('click', async () => {
    const curveId = Number($('mock-entry-curve').value);
    const quantity = Number($('mock-entry-quantity').value);
    const contract = $('trade-contract').value;
    if (!curveId || !contract || !Number.isInteger(quantity) || quantity < 1 || quantity > 100) {
      message('請先保存曲線、選擇合約，並輸入 1 到 100 的整數口數。', true);
      return;
    }
    if (!await confirmMock(`離線 Mock：啟用曲線 #${curveId} 向上突破買入 ${quantity} 口？\n僅觸發一次，成交後設定 ±2 ATR。`)) return;
    run(() => request('/api/mock/curve-entry', { curve_id: curveId, contract_code: contract, quantity, confirmed: true }));
  });
  $('mock-entry-cancel').addEventListener('click', () => run(() => request('/api/mock/curve-entry', null, 'DELETE')));
  document.addEventListener('trendlines-changed', () => curves().catch((error) => message(error.message, true)));

  async function poll() {
    try {
      if (!busy) {
        const response = await fetch('/api/mock');
        if (!response.ok) throw new Error('回放狀態讀取失敗');
        const info = await response.json();
        render(info);
        if (info.cursor !== lastCursor) {
          lastCursor = info.cursor;
          await refreshViews();
        }
      }
    } catch (error) { message(error.message, true); }
    setTimeout(poll, snapshot.replay.browser_poll_seconds * 1000);
  }
  setTimeout(poll, snapshot.replay.browser_poll_seconds * 1000);
})();
