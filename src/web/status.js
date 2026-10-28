'use strict';
// Everything that renders /api/state: the header pills, the two side cards and
// the three tables, kept separate from the chart rendering code so a new
// /api/state field lands in one focused place.

(function () {
  // Data is minute-granular, so anything beyond a couple of minutes means the
  // monitor has stopped rather than simply not having ticked yet.
  const STALE_WARN_MIN = 3;
  const STALE_BAD_MIN = 10;

  const PATTERN_LABEL = {
    long_lower_tail: '長下影',
    long_upper_tail: '長上影',
    bullish_engulfing: '多頭吞噬',
    bearish_engulfing: '空頭吞噬',
    piercing_line: '貫穿線',
    dark_cloud_cover: '烏雲罩頂',
  };
  const STATE_LABEL = { fresh: '未觸發', holds: '已支撐', broken: '已突破' };

  const $ = (id) => document.getElementById(id);
  const num = (value, digits = 0) =>
    value === null || value === undefined || Number.isNaN(value)
      ? '—'
      : Number(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const clock = (text) => (text ? String(text).slice(5, 16) : '—');

  function setPill(el, text, cls) {
    el.textContent = text;
    el.className = 'pill' + (cls ? ' ' + cls : '');
  }

  function renderStatus(state) {
    $('code').textContent = state.code;
    $('price').textContent = num(state.last_price);
    $('atr').textContent = num(state.hourly_atr, 1);
    $('bars').textContent = num(state.hourly_bars);
    $('updated').textContent = clock(state.last_minute_start);

    setPill($('session'), state.in_session ? '盤中時段' : '非交易時段', state.in_session ? 'ok' : '');

    const stale = state.stale_minutes;
    if (state.market_health) {
      const monitor = state.market_health.monitor || {};
      const bars = monitor.bars || {};
      const ready = state.market_health.bars_ready;
      setPill($('freshness'), ready
        ? (bars.active_session ? '行情驗證正常' : '休市 · 最近收盤資料完整')
        : `行情未就緒 · ${monitor.state || '無監督紀錄'}`, ready ? 'ok' : 'bad');
    } else if (stale >= STALE_BAD_MIN) {
      setPill($('freshness'), `資料落後 ${num(stale, 0)} 分`, 'bad');
    } else if (stale >= STALE_WARN_MIN) {
      setPill($('freshness'), `資料落後 ${num(stale, 0)} 分`, 'warn');
    } else {
      setPill($('freshness'), '資料即時', 'ok');
    }

    if (state.book_source) {
      const five = state.book_source.levels >= 5;
      $('book').textContent = `${five ? '五檔' : '一檔'} · ${num(state.orderbook_rows)} 筆`;
    } else {
      $('book').textContent = state.orderbook_rows ? `${num(state.orderbook_rows)} 筆` : '尚未記錄';
    }
  }

  function renderSide(kind, side, price) {
    const card = $(`side-${kind}`);
    if (!card) return; // Trading layout can omit the optional box summary cards.
    const body = card.querySelector('.side-body');
    card.classList.remove('inside');

    if (!side) {
      body.innerHTML = '<div class="empty">目前無有效箱體（可能已突破）</div>';
      return;
    }
    if (side.inside) card.classList.add('inside');

    // Inside the box a percentage is the useful number; outside it stops meaning
    // anything (a box 190 points tall puts price 1000 points away at "598%"), so
    // show the gap in points instead. The marker is clamped either way.
    const pct = side.zone_pct === null ? null : side.zone_pct;
    const clamped = pct === null ? 0 : Math.max(0, Math.min(1, pct));
    let position;
    if (pct === null) {
      position = '—';
    } else if (side.inside) {
      position = `${(pct * 100).toFixed(0)}%`;
    } else if (price > side.top) {
      position = `高於上緣 ${num(price - side.top)} 點`;
    } else {
      position = `低於下緣 ${num(side.bottom - price)} 點`;
    }

    body.innerHTML = `
      <div class="side-range">${num(side.bottom)} ~ ${num(side.top)}</div>
      <div class="side-sub">
        #${side.box_id} · ${STATE_LABEL[side.state] || side.state} ·
        高 ${num(side.height)} ·
        ${side.inside ? '<b>價格在箱內</b>' : '價格在箱外'}
      </div>
      <div class="zone">
        <div class="zone-track"><div class="zone-marker" style="left:${(clamped * 100).toFixed(1)}%"></div></div>
        <div class="zone-labels">
          <span>下緣 0%</span>
          <span>${position}</span>
          <span>上緣 100%</span>
        </div>
      </div>`;
  }

  function table(headers, rows) {
    if (!rows.length) return '<div class="empty">尚無紀錄</div>';
    const head = headers.map((h) => `<th>${h}</th>`).join('');
    const body = rows.map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('');
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function renderVisits(visits) {
    $('visits').innerHTML = table(
      ['箱體', '進入', '離開', '分鐘', '箱內區間', '委託簿', '型態'],
      visits.map((v) => [
        `<span class="tag ${v.box_type}">#${v.box_id}</span>`,
        clock(v.entry_minute_start),
        v.is_open ? '<span class="tag open">進行中</span>' : clock(v.exit_minute_start),
        num(v.minutes_in_box),
        v.min_zone_pct === null ? '—' : `${(v.min_zone_pct * 100).toFixed(0)}~${(v.max_zone_pct * 100).toFixed(0)}%`,
        num(v.orderbook_rows),
        num(v.reversal_count),
      ]),
    );
  }

  function renderReversals(rows) {
    $('reversals').innerHTML = table(
      ['時間', '型態', '方向', '箱體', '箱內', '第幾分'],
      rows.map((r) => [
        clock(r.hour_start),
        PATTERN_LABEL[r.pattern] || r.pattern,
        `<span class="tag ${r.direction}">${r.direction === 'long' ? '做多' : '做空'}</span>`,
        `<span class="tag ${r.box_type}">#${r.box_id}</span>`,
        r.zone_pct === null ? '—' : `${(r.zone_pct * 100).toFixed(0)}%`,
        num(r.minutes_into_bar),
      ]),
    );
  }

  function renderAlerts(rows) {
    $('alerts').innerHTML = table(
      ['時間', '類型', '箱體', '結果'],
      rows.map((a) => [
        clock(a.sent_at),
        a.kind,
        `<span class="tag ${a.box_type}">#${a.box_id ?? '—'}</span>`,
        a.ok === 1 ? '已送出' : a.ok === 0 ? '失敗' : '待送',
      ]),
    );
  }

  window.SNRStatus = {
    renderAll(state) {
      if (state.ready === false) {
        state = { ...state, last_price: null, hourly_atr: null, hourly_bars: 0,
          last_minute_start: null, in_session: false, sides: { support: null, resistance: null },
          recent_visits: [], recent_reversals: [], recent_alerts: [] };
      }
      renderStatus(state);
      renderSide('support', state.sides.support, state.last_price);
      renderSide('resistance', state.sides.resistance, state.last_price);
      renderVisits(state.recent_visits || []);
      renderReversals(state.recent_reversals || []);
      renderAlerts(state.recent_alerts || []);
      if (state.ready === false) setPill($('freshness'), state.message || '尚無資料', 'warn');
    },
    setFreshness(text, cls) {
      setPill($('freshness'), text, cls);
    },
    num,
    clock,
  };
})();
