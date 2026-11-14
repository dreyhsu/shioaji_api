/* This page deliberately has no dependency on chart or order-control scripts. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const number = new Intl.NumberFormat('zh-TW', {maximumFractionDigits: 2});
  const money = n => `${n > 0 ? '+' : n < 0 ? '−' : ''}$${number.format(Math.abs(n))}`;
  const tone = n => n > 0 ? 'profit-text' : n < 0 ? 'loss-text' : '';
  const taipeiDate = () => new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
  let currentMonth = taipeiDate().slice(0, 7), today = taipeiDate(), selected = null;
  let requestId = 0, dayRequestId = 0;
  const query = extra => new URLSearchParams({...extra, ...($('contract').value ? {contract_code: $('contract').value} : {})});
  async function json(url) {
    const response = await fetch(url, {cache:'no-store', signal: AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error(`讀取失敗（${response.status}）`);
    return response.json();
  }
  function message(text, kind='') { $('message').textContent=text; $('message').className=`message ${kind}`; }
  function closeDetails() { selected=null; dayRequestId++; $('detail').hidden=true; document.querySelectorAll('.day.selected').forEach(el=>el.classList.remove('selected')); }
  function render(data) {
    today = data.today;
    const mode = {mock:'離線 Mock',simulation:'模擬交易',production:'正式交易'}[data.mode];
    $('mode').textContent=mode; $('mode').className=`badge ${data.mode}`;
    const prior = $('contract').value;
    $('contract').replaceChildren(new Option('全部合約',''), ...data.contracts.map(code=>new Option(code,code)));
    // Preserve even a temporarily unavailable selection; never silently broaden its scope.
    if (prior && !data.contracts.includes(prior)) $('contract').add(new Option(prior,prior));
    $('contract').value=prior;
    $('total').textContent=money(data.summary.gross_pnl); $('total').className=tone(data.summary.gross_pnl);
    $('orders').textContent=number.format(data.summary.order_count); $('quantity').textContent=number.format(data.summary.quantity);
    if(!data.ledger_available) for(const key of ['total','orders','quantity']) $(key).textContent='—';
    const [year, month] = currentMonth.split('-').map(Number);
    $('month-title').textContent=`${year} 年 ${month} 月`;
    $('previous').disabled=currentMonth==='0001-01'; $('next').disabled=currentMonth==='9999-12';
    const grid=$('calendar'); grid.replaceChildren();
    const leading=new Date(`${currentMonth}-01T12:00:00+08:00`).getUTCDay();
    for(let i=0;i<leading;i++) {const blank=document.createElement('div');blank.className='blank';grid.append(blank);}
    for(const day of data.days) {
      const btn=document.createElement('button'); btn.type='button';
      btn.className=`day ${day.order_count ? day.gross_pnl>0?'profit':day.gross_pnl<0?'loss':'flat' : ''} ${day.date===today?'is-today':''} ${day.date===selected?'selected':''}`;
      const emptyLabel = !data.ledger_available ? '資料待建立' : data.incomplete_order_count ? '無可認列紀錄' : '無平倉';
      btn.setAttribute('aria-label',`${day.date}，${day.order_count ? `${money(day.gross_pnl)}，${day.order_count} 筆，${day.quantity} 口`:emptyLabel}`);
      if(day.date===today) btn.setAttribute('aria-current','date');
      for(const [cls,text] of [['date',day.date.slice(-2)], ...(day.order_count ? [['amount',money(day.gross_pnl)],['count',`${day.order_count} 筆 · ${day.quantity} 口`]] : [['empty',emptyLabel]])]) {
        const span=document.createElement('span');span.className=cls;span.textContent=text;btn.append(span);
      }
      btn.addEventListener('click',()=>showDay(day.date));grid.append(btn);
    }
    while(grid.children.length%7){const blank=document.createElement('div');blank.className='blank';grid.append(blank);}
    $('updated').textContent=data.updated_at ? `成交資料更新：${data.updated_at.replace('T',' ').slice(0,19)}（台北）`:'尚無完整平倉紀錄';
    if(!data.ledger_available) message('成交明細尚未建立。請更新並重啟服務；現有舊資料不會以估算值計入。','warning');
    else if(data.incomplete_order_count) message(`全部歷史中有 ${data.incomplete_order_count} 筆已平倉訂單的資料不完整，尚未計入。可能缺成交時間、價格、乘數或完整口數；不一定屬於本月。`,'warning');
    else message(data.summary.order_count ? '僅顯示已確認成交的毛損益。點選日期查看平倉明細。':'本月尚無可認列的平倉交易。');
  }
  async function load(initial=false) {
    const id=++requestId; $('calendar').setAttribute('aria-busy','true'); message('更新損益資料…');
    try {
      let data=await json(`/api/pnl-calendar?${query({month:currentMonth})}`);
      if(id!==requestId) return;
      if(initial && data.today.slice(0,7)!==currentMonth) {currentMonth=data.today.slice(0,7);data=await json(`/api/pnl-calendar?${query({month:currentMonth})}`);}
      if(id!==requestId) return;
      render(data); if(selected) await showDay(selected, false);
    } catch(error) {
      if(id!==requestId) return;
      // Remove stale statistics instead of presenting old amounts as current data.
      for(const key of ['total','orders','quantity']) $(key).textContent='—';
      $('calendar').replaceChildren(); closeDetails();
      message(`${error.message}。請重新整理；目前未顯示損益結果。`,'error');
    } finally {if(id===requestId) $('calendar').setAttribute('aria-busy','false');}
  }
  async function showDay(date, scroll=true) {
    const id=++dayRequestId;selected=date;$('detail').hidden=false;$('detail-title').textContent=`${date} · 平倉明細`;
    $('detail-summary').textContent='載入明細…';$('fills').replaceChildren();
    document.querySelectorAll('.day').forEach(el=>el.classList.toggle('selected',el.getAttribute('aria-label').startsWith(date)));
    if(scroll) $('detail').scrollIntoView({behavior:'smooth',block:'nearest'});
    try {
      const data=await json(`/api/pnl-calendar/day?${query({date})}`);
      if(id!==dayRequestId) return;
      $('detail-summary').textContent=data.fills.length ? `${money(data.summary.gross_pnl)} · ${data.summary.order_count} 筆 · ${data.summary.quantity} 口 · 未扣費稅`:'當日無可認列的平倉交易。';
      for(const fill of data.fills) {
        const row=document.createElement('tr');
        const stamp=fill.filled_at.length===10 ? `${fill.filled_at}（僅交易日）` : fill.filled_at.replace('T',' ').slice(0,19);
        const values=[stamp,fill.order_id,fill.contract_code,fill.action==='Buy'?'多單':'空單',fill.quantity,number.format(fill.entry_price),number.format(fill.exit_price),money(fill.gross_pnl)];
        values.forEach((text,i)=>{const cell=document.createElement('td');cell.textContent=text;if(i===1) cell.title=text;if(i===7) cell.className=tone(fill.gross_pnl);row.append(cell);});
        $('fills').append(row);
      }
    } catch(error) {if(id===dayRequestId) $('detail-summary').textContent=`${error.message}，請再選取日期重試。`;}
  }
  function changeMonth(delta) {
    const [y,m]=currentMonth.split('-').map(Number);const index=y*12+m-1+delta;
    const year=Math.floor(index/12);if(year<1||year>9999)return;
    currentMonth=`${String(year).padStart(4,'0')}-${String(index%12+1).padStart(2,'0')}`;closeDetails();load();
  }
  $('previous').addEventListener('click',()=>changeMonth(-1));$('next').addEventListener('click',()=>changeMonth(1));
  $('today').addEventListener('click',()=>{currentMonth=today.slice(0,7);closeDetails();load();});
  $('contract').addEventListener('change',()=>{closeDetails();load();});$('refresh').addEventListener('click',()=>load());
  $('close-detail').addEventListener('click',closeDetails);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});
  setInterval(()=>{if(!document.hidden)load();},60000);
  load(true);
})();
