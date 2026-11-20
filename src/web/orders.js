/* Order inspection: durable IDs, explicit review, no automatic mutation retry. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = '/api/trading/reconciliation/';
  const KIND = {order:'管理單',curve:'曲線進場',command:'交易指令',broker:'券商委託'};
  const ACTION = {cancel_condition:'取消等待突破',confirm_no_order:'人工核對：確認無委託並結束事項',
    cancel_working:'撤銷券商未成交餘量',link_curve:'關聯已送出的原委託',resolve_command:'依已確認結果解決指令',sync:'同步券商委託與部位',link_entry:'人工配對原進場委託',link_exit:'人工配對原平倉委託'};
  const STATUS = {Submitted:'未成交',PendingSubmit:'等待券商確認',PartFilled:'部分成交',Filled:'全部成交',Cancelled:'已撤銷',
    Failed:'失敗',Rejected:'拒絕',unknown:'結果待核對',submitting:'送單待核對',armed:'等待突破',triggered:'已觸發',
    cancelled:'已取消',failed:'已停止',accepted:'已接受',processing:'處理中',succeeded:'已完成',observed:'券商紀錄',
    confirmed:'券商已確認',pending_confirmation:'等待券商確認'};
  let tab='pending',cursor=null,nextCursor=null,history=[],page=1,detail=null,proposal=null,info=null,busy=false;
  let storeKey=null,pending={},sequence=0,refreshing=false;
  const el=(tag,text,className)=>{const n=document.createElement(tag);if(text!=null)n.textContent=String(text);if(className)n.className=className;return n;};
  const label=s=>STATUS[s]||s||'—';
  const num=v=>v==null?'—':String(v);
  function notice(message,bad=false){$('notice').textContent=message;$('notice').classList.toggle('bad',bad);}
  function savePending(){try{localStorage.setItem(storeKey,JSON.stringify(pending));}catch{notice('瀏覽器無法保存待確認指令，請先開啟儲存權限。',true);throw Error('無法持久保存指令 ID');}}
  async function read(url){const r=await fetch(url,{cache:'no-store'});let data;try{data=await r.json();}catch{throw Error('服務未提供有效回應，請檢查 trade／Dashboard 版本與狀態');}if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'讀取失敗');return data;}
  function pendingPanel(){
    const box=$('pending-commands');box.replaceChildren();box.hidden=!Object.keys(pending).length;
    for(const [fingerprint,item] of Object.entries(pending)){
      const row=el('div');row.append(el('span',`待確認操作 ${item.id} · ${item.body.name}`));
      const check=el('button','查詢原結果');check.onclick=()=>checkPending(fingerprint).catch(e=>notice(e.message,true));row.append(check);
      const view=el('button','查看指令');view.onclick=()=>openDetail('command',item.id);row.append(view);
      if(item.missing){const retry=el('button','沿用原 ID 重試');retry.onclick=async()=>{
        if(!window.confirm('查詢原 ID 尚無紀錄。確認沿用同一 ID 與原內容重試這次操作？'))return;
        try{const current=await fetch('/api/trading/commands/'+encodeURIComponent(item.id),{cache:'no-store'});
          if(current.status!==404){await checkPending(fingerprint);return;}
          await postPending(fingerprint,item);await load();
        }catch(e){notice(e.message,true);}
      };row.append(retry);}
      box.append(row);
    }
  }
  async function checkPending(fingerprint){
    const item=pending[fingerprint];if(!item)return null;
    const response=await fetch('/api/trading/commands/'+encodeURIComponent(item.id),{cache:'no-store'});
    if(response.status===404){item.missing=true;savePending();pendingPanel();return null;}
    if(!response.ok)throw Error('無法確認原指令結果，保留原 ID，請稍後查詢。');
    const result=await response.json();item.missing=false;
    if(['succeeded','failed'].includes(result.state)||result.resolution){
      delete pending[fingerprint];savePending();pendingPanel();
      if(result.state==='failed')throw Error(result.result?.detail?.message||result.result?.detail||'操作未完成');
      notice(result.resolution?'原指令已有後續核對結論。':'原操作已完成。');return result.resolution?{resolved:true}:result.result;
    }
    pendingPanel();notice(`指令 ${item.id}：${label(result.state)}。保留原 ID，請查看並核對。`,result.state==='unknown');return null;
  }
  async function postPending(fingerprint,item){
    try{
      const r=await fetch(API+'command',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':item.id},body:JSON.stringify(item.body)});
      const data=await r.json();
      if(r.ok){delete pending[fingerprint];savePending();pendingPanel();return data;}
      // A non-success response never establishes that a POST was unaccepted.
      // Query its original ID; only a user-triggered retry after 404 can POST.
      const settled=await checkPending(fingerprint);
      if(settled?.resolved)throw Error('原操作已有核對結論，請重新查閱紀錄並建立新預覽。');
      if(settled)return settled;
      throw Error(typeof data.detail==='string'?data.detail:'指令結果待確認，請查詢原結果');
    }catch(error){pendingPanel();throw error;}
  }
  async function command(name,payload={}){
    if(!storeKey)throw Error('尚未確認交易環境');
    const body={name,payload},fingerprint=JSON.stringify(body);
    if(pending[fingerprint]){const result=await checkPending(fingerprint);if(result?.resolved)throw Error('原操作已有核對結論，請重新查閱紀錄並建立新預覽。');if(result)return result;throw Error('相同操作仍待確認，請查詢原 ID。');}
    const item={id:crypto.randomUUID(),body,created_at:new Date().toISOString()};pending[fingerprint]=item;
    savePending();pendingPanel();return postPending(fingerprint,item);
  }
  function renderOverview(data){
    info=data;
    const key='order-reconciliation:'+data.scope;
    if(storeKey!==key){storeKey=key;try{pending=JSON.parse(localStorage.getItem(key)||'{}');}catch{pending={};}pendingPanel();}
    $('environment').textContent=data.mode==='production'?'正式交易':data.mode==='mock'?'離線 Mock':'券商模擬';
    $('pending-count').textContent=data.blocker_count;
    $('broker-state').textContent=data.status.broker_ready?'核對一致':'待核對';$('account').textContent=data.account;
    const ready=data.status.manual_order_ready;
    $('manual-state').textContent=!data.status.connected?'服務未就緒':ready&&Object.values(ready).some(Boolean)?'目前可送單':'請查看交易狀態';
    $('curve-state').textContent=data.status.bars_ready?'收定 K 棒已就緒':'曲線進場等待歷史資料';
    $('observation-at').textContent=data.last_verified_at||data.observation?.observed_at||'尚無核對紀錄';
    $('snapshot-state').textContent=data.status.inspection_error?'委託觀測保存失敗，請查看 trade 日誌':data.status.connected?'交易服務已連線':'交易服務未連線／快照過期';
    $('refresh-broker').disabled=busy||!data.status.connected;
    $('coverage').textContent=data.coverage;
    const blockers=$('blockers');blockers.replaceChildren();
    for(const issue of data.service_issues||[]){blockers.append(el('div',issue.reason,'blocker warning'));}
    for(const b of data.blockers.slice(0,8)){
      const row=el('div',null,'blocker'),text=el('div',`${KIND[b.kind]} #${b.target_id} · ${b.reason}`);
      text.append(el('span',b.scope.map(s=>({manual_entry:'手動進場',curve_entry:'曲線進場',protection:'相關出場保護'}[s]||s)).join('／')));
      const button=el('button','查看並處理');button.onclick=()=>openDetail(b.kind,b.target_id);row.append(text,button);blockers.append(row);
    }
    if(data.blocker_count>8)blockers.append(el('p','其他待處理事項請見下方分頁清單。','muted'));
  }
  function cell(row,text,sub){const td=el('td',text);if(sub)td.append(el('small',sub));row.append(td);return td;}
  function renderRecords(data){
    const body=$('records-body'),head=$('records-head');body.replaceChildren();head.replaceChildren();
    const headings=tab==='events'?['發起／完成時間','對象','操作','結果','詳情']:['交易日／時間','來源／類型','商品／方向','口數／剩餘','券商狀態','系統結案','影響／操作'];
    const tr=el('tr');headings.forEach(t=>tr.append(el('th',t)));head.append(tr);
    for(const item of data.items){
      const row=el('tr'),d=item.document||{};
      if(tab==='events'){
        cell(row,item.started_at,item.completed_at||'尚未完成');cell(row,`${KIND[item.kind]} #${item.target_id}`);
        cell(row,ACTION[item.action]||item.action);cell(row,label(item.state));
        const button=el('button','查看處理證據');button.onclick=()=>openDetail(item.kind,item.target_id);cell(row,'').append(button);
      }else{
        const date=d.entry_date||item.sort_at?.slice(0,10)||'日期待確認';
        cell(row,date,item.sort_at||'僅交易日');
        cell(row,item.source==='external'?'券商外部':'系統',KIND[item.kind]);
        cell(row,item.contract||'—',(item.side==='Buy'?'買進':item.side==='Sell'?'賣出':'—')+(item.phase==='exit'?' · 平倉':''));
        const isCover=item.kind==='order'&&d.broker_octype==='Cover';
        const remaining=item.kind==='order'&&!isCover?Math.max(0,Number(d.filled_quantity)-Number(d.exit_filled_quantity)):null;
        cell(row,`委託 ${num(d.quantity)} · 成交 ${num(d.filled_quantity)}`,isCover?'平倉委託，不建立新部位':remaining==null?`未成交餘量 ${num(d.working_quantity)}`:`剩餘部位 ${remaining} · 外部已平 ${num(d.external_exit_quantity)}`);
        cell(row,label(item.broker_status),item.kind==='command'?d.name:d.entry_trade_id||d.broker_trade_id);
        let closed=isCover?(d.active?'平倉委託追蹤中':'平倉委託已結束'):item.kind==='order'?(d.active?'管理中':'系統已結案'):(item.closed?'事項已結束':'尚未結束');
        if(item.kind==='order'&&!d.active&&remaining>0)closed='結案／口數有差異，請核對';
        cell(row,closed,label(item.local_status));
        const target=cell(row,'');if(item.blocked)target.append(el('span',item.local_status==='armed'?'等待進場':'待核對','tag warn'),document.createTextNode(' '));
        const button=el('button','查看');button.onclick=()=>openDetail(item.kind,item.id);target.append(button);
      }
      body.append(row);
    }
    if(!data.items.length){const row=el('tr'),td=el('td','目前篩選沒有紀錄。','empty');td.colSpan=headings.length;row.append(td);body.append(row);}
    nextCursor=data.next_cursor;$('next-page').disabled=!nextCursor;$('previous-page').disabled=!history.length;
    $('page-number').textContent=`第 ${page} 頁`;$('list-status').textContent=tab==='events'?'每頁最多 50 筆處理紀錄':`共 ${data.total} 筆${tab==='pending'?' · 待處理包含更早未解決事項，不受日期限制':''}`;
  }
  async function load(reset=false){
    if(reset){cursor=null;nextCursor=null;history=[];page=1;}
    const turn=++sequence;$('list-status').textContent='載入中…';
    const params=new URLSearchParams(new FormData($('filters')));params.set('tab',tab);if(cursor)params.set('cursor',cursor);
    try{const [overview,records]=await Promise.all([read(API+'overview'),read(API+'records?'+params)]);if(turn!==sequence)return;renderOverview(overview);renderRecords(records);}
    catch(e){if(turn===sequence){$('list-status').textContent='載入失敗';notice(e.message,true);}}
  }
  const FIELD={contract_code:'商品',action:'方向',quantity:'委託口數',filled_quantity:'已成交',exit_filled_quantity:'已平倉累計',
    external_exit_quantity:'外部已平',entry_status:'進場狀態',exit_status:'出場狀態',active:'系統管理中',
    avg_fill_price:'進場均價',avg_exit_price:'平倉均價',stop_price:'固定停損',target_price:'固定停利',
    entry_trade_id:'券商進場 ID',exit_trade_id:'券商平倉 ID',state:'本機狀態',curve_id:'曲線 ID',order_id:'關聯管理單',name:'指令操作'};
  function dataCard(title,data){const box=el('section',null,'detail-card');box.append(el('h3',title));const dl=el('dl');
    for(const [key,name] of Object.entries(FIELD)){if(data[key]!==undefined){dl.append(el('dt',name),el('dd',data[key]==null?'—':typeof data[key]==='boolean'?data[key]?'是':'否':String(data[key])));}}
    if(!dl.children.length)dl.append(el('dt','狀態'),el('dd','請展開核對證據查看'));box.append(dl);return box;}
  function evidence(title,data){const box=el('details');box.append(el('summary',title),el('pre',JSON.stringify(data,null,2)));return box;}
  async function openDetail(kind,id){
    $('detail-message').textContent='';proposal=null;$('detail-content').replaceChildren(el('p','載入詳情…'));$('repair-form').hidden=true;
    if(!$('detail-dialog').open)$('detail-dialog').showModal();
    try{
      const data=await read(API+'detail/'+kind+'/'+encodeURIComponent(id));detail=data;
      $('detail-title').textContent=`${KIND[kind]} #${id}`;
      const grid=el('div',null,'detail-grid');grid.append(dataCard('系統紀錄',data.local));
      const broker=data.observations[0]?.document||data.local.document;
      grid.append(dataCard('券商核對證據',broker||{}));
      $('detail-content').replaceChildren(grid);
      if(data.resolution)$('detail-content').append(el('p','此事項已有核對結論；原始執行紀錄仍保留。','tag good'));
      $('detail-content').append(evidence('本機完整欄位與身分',data.local),evidence('券商觀測與來源時間',data.observations),evidence('指令與管理單關聯',data.linked));
      const timeline=el('ul',null,'timeline');for(const entry of data.timeline){const li=el('li',ACTION[entry.action]||entry.document.kind||entry.event||'部位同步');li.append(el('time',entry.started_at||entry.reconciled_at||entry.occurred_at),evidence('修正前後與核對證據',entry.document));timeline.append(li);}
      $('detail-content').append(el('h3','核對與修正時間軸'),timeline.children.length?timeline:el('p','尚無人工處理或部位修正紀錄。','muted'));
      const select=$('repair-action');select.replaceChildren(...data.actions.map(action=>new Option(ACTION[action],action)));
      $('linked-order').replaceChildren(...data.linked.map(r=>new Option(r.order_id,r.order_id)));
      $('checked-date').value=(data.local.armed_at||data.local.created_at||'').slice(0,10);$('repair-note').value='';$('attested').checked=false;
      for(const id of ['link-broker-id','link-seqno','link-ordno'])$(id).value='';
      $('repair-form').hidden=!data.actions.length;syncActionFields();
    }catch(e){$('detail-content').replaceChildren(el('p',e.message,'warning'));}
  }
  function syncActionFields(){const action=$('repair-action').value,link=['link_entry','link_exit'].includes(action);$('attestation-fields').hidden=action!=='confirm_no_order'&&!link;$('linked-order-field').hidden=action!=='link_curve';$('broker-link-fields').hidden=!link;$('attestation-label').textContent=link?'我已核對券商完整書號、成交與部位，確認此委託屬於原管理單':'我已核對券商委託、成交與部位，確認本事項沒有對應委託';}
  async function guarded(fn){if(busy)return;busy=true;$('refresh-broker').disabled=true;$('preview-repair').disabled=true;$('apply-repair').disabled=true;
    try{await fn();}catch(e){notice(e.message,true);$('detail-message').textContent=e.message;$('preview-message').textContent=e.message;}
    finally{busy=false;$('refresh-broker').disabled=!info?.status.connected;$('preview-repair').disabled=false;$('apply-repair').disabled=false;}}
  $('refresh-broker').onclick=()=>guarded(async()=>{notice('正在沿用交易服務核對券商…');await command('refresh_reconciliation');notice('券商核對完成。');await load();});
  $('preview-repair').onclick=()=>guarded(async()=>{
    const action=$('repair-action').value;
    const human=action==='confirm_no_order'||['link_entry','link_exit'].includes(action);
    const payload={kind:detail.kind,target_id:detail.id,action,note:human?$('repair-note').value:'',
      checked_date:human?$('checked-date').value||null:null,attested:human&&$('attested').checked,
      broker_trade_id:human?$('link-broker-id').value||null:null,seqno:human?$('link-seqno').value||null:null,ordno:human?$('link-ordno').value||null:null,
      linked_order_id:action==='link_curve'?$('linked-order').value||null:null};
    proposal=await command('preview_reconciliation',payload);
    const content=$('preview-content');content.replaceChildren(el('p',`環境：${$('environment').textContent}`,'warning'),
      el('h3',proposal.broker_operation),el('p',proposal.effect),el('p',`目標：${KIND[payload.kind]} #${payload.target_id}`),
      el('p','確認時會再次核對；資料變更後必須重新預覽。','muted'),evidence('原始狀態',proposal.before),evidence('核對依據',proposal.evidence));
    $('preview-message').textContent='';$('preview-dialog').showModal();
  });
  $('apply-repair').onclick=()=>guarded(async()=>{
    if(!proposal)throw Error('請先重新取得預覽');
    const result=await command('apply_reconciliation',{proposal_id:proposal.id,confirmed:true});
    $('preview-dialog').close();$('detail-dialog').close();notice(`處理完成：${label(result.state)}。${result.effect||''}`);proposal=null;await load();
  });
  $('repair-action').onchange=syncActionFields;
  $('close-detail').onclick=()=>$('detail-dialog').close();
  $('close-preview').onclick=$('back-preview').onclick=()=>$('preview-dialog').close();
  $('filters').onsubmit=e=>{e.preventDefault();load(true);};
  $('filters').onreset=()=>setTimeout(()=>load(true),0);
  $('reload-list').onclick=()=>load();
  $('next-page').onclick=()=>{history.push(cursor);cursor=nextCursor;page++;load();};
  $('previous-page').onclick=()=>{cursor=history.pop()??null;page--;load();};
  document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{tab=button.dataset.tab;document.querySelectorAll('[data-tab]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));load(true);});
  load();
  setInterval(async()=>{if(refreshing||busy||!storeKey||document.hidden)return;refreshing=true;
    try{for(const key of Object.keys(pending)){try{await checkPending(key);}catch(e){notice(e.message,true);}}renderOverview(await read(API+'overview'));}
    catch(e){notice(e.message,true);}finally{refreshing=false;}
  },5000);
})();
