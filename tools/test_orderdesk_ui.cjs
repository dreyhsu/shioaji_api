// Execute the actual page script against an isolated DOM/fetch harness.
// No browser, production service, network or broker is contacted.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../src/web/orders.js'),'utf8');
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.value='';this._text='';this.hidden=false;this.open=false;this.disabled=false;this.dataset={};this.attributes={};this.classes=new Set();this.classList={toggle:(key,on)=>on?this.classes.add(key):this.classes.delete(key)};}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  set innerHTML(_){throw Error('Untrusted content must never be assigned to innerHTML');}
  append(...children){this.children.push(...children.map(c=>typeof c==='string'?Object.assign(new Element('text'),{textContent:c}):c));}
  replaceChildren(...children){this._text='';this.children=[];this.append(...children);if(this.tagName==='select')this.value=children[0]?.value||'';}
  setAttribute(k,v){this.attributes[k]=v;}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function fixture(){
  const elements=new Map();
  function get(id){if(!elements.has(id))elements.set(id,new Element(['repair-action','linked-order'].includes(id)?'select':'div'));return elements.get(id);}
  const tabs=['pending','all','positions','conditions','events'].map(name=>{const button=new Element('button');button.dataset.tab=name;return button;});
  let resolved=false,nextId=0,networkFailures=0;
  const calls=[],storage=new Map(),commands=new Map();
  const detail=()=>({kind:'curve',id:'46',local:{id:46,curve_id:123,contract_code:'TMF_OLD',quantity:2,action:'Sell',state:resolved?'failed':'submitting',armed_at:'2026-11-18 12:01:00'},
    actions:resolved?[]:['confirm_no_order','link_curve'],observations:[],linked:[],timeline:[],resolution:resolved?{conclusion:'confirm_no_order'}:null});
  const overview=()=>({scope:'isolated-ui-test',mode:'mock',code:'TMFR1',blocker_count:resolved?0:1,
    blockers:resolved?[]:[{kind:'curve',target_id:'46',reason:'結果待核對',scope:['curve_entry']}],
    status:{connected:true,broker_ready:true,bars_ready:true,manual_order_ready:{TMF_MOCK:true}},account:'測試帳戶',coverage:'離線測試資料'});
  const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
  const ctx={document:{getElementById:get,createElement:tag=>new Element(tag),createTextNode:text=>{const e=new Element('text');e.textContent=text;return e;},
      querySelectorAll:()=>tabs,hidden:false},
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
    URLSearchParams,FormData:class {constructor(){return [][Symbol.iterator]();}},
    Option:class extends Element {constructor(text,value){super('option');this.textContent=text;this.value=value;}},
    crypto:{randomUUID:()=>`test-id-${++nextId}`},setInterval:()=>0,setTimeout:callback=>callback(),
    window:{confirm:()=>true},console,
    fetch:async(url,options={})=>{
      calls.push({url,options});
      if(url.endsWith('/overview'))return response(overview());
      if(url.includes('/records?'))return response({total:1,next_cursor:null,items:[{kind:'curve',id:'46',sort_at:'2026-11-18 12:01:00',contract:'TMF_OLD',side:'Sell',source:'system',phase:'condition',blocked:!resolved,closed:resolved,local_status:resolved?'failed':'submitting',document:detail().local}]});
      if(url.includes('/detail/curve/46'))return response(detail());
      if(url.includes('/api/trading/commands/'))return commands.has(url.split('/').at(-1))?response(commands.get(url.split('/').at(-1))):response({detail:'not found'},404);
      if(url.endsWith('/command')){
        if(networkFailures-- >0)throw Error('offline simulated response loss');
        const id=options.headers['Idempotency-Key'],body=JSON.parse(options.body);
        let result={state:'confirmed',effect:'已完成核對'};
        if(body.name==='preview_reconciliation')result={id:'proposal-1',before:detail().local,evidence:{checked:true},broker_operation:'只更新系統紀錄',effect:'結束已人工核對事項'};
        if(body.name==='apply_reconciliation')resolved=true;
        commands.set(id,{id,state:'succeeded',result});return response(result);
      }
      throw Error('Unexpected request '+url);
    },
  };
  vm.createContext(ctx);vm.runInContext(source,ctx);await tick();await tick();
  return {get,tabs,calls,storage,commands,setNetworkFailures:n=>{networkFailures=n;}};
}
(async()=>{
  const f=await fixture();
  assert.equal(f.get('pending-count').textContent,'1');
  assert.match(f.get('records-body').textContent,/TMF_OLD/);
  assert.equal(f.calls.filter(c=>c.options.method==='POST').length,0,'opening the page only reads');
  await f.get('blockers').children[0].children[1].onclick();
  assert.equal(f.get('detail-dialog').open,true);
  assert.equal(f.get('repair-action').value,'confirm_no_order');
  assert.equal(f.get('attestation-fields').hidden,false);
  f.get('repair-note').value='<img src=x onerror=alert(1)> 這是人工核對測試，券商無委託。';
  f.get('checked-date').value='2026-11-18';f.get('attested').checked=true;
  await f.get('preview-repair').onclick();
  assert.equal(f.get('preview-dialog').open,true);
  assert.match(f.get('preview-content').textContent,/只更新系統紀錄/);
  const preview=f.calls.find(c=>c.options.method==='POST'&&JSON.parse(c.options.body).name==='preview_reconciliation');
  assert.equal(JSON.parse(preview.options.body).payload.attested,true);
  await f.get('apply-repair').onclick();
  assert.equal(f.get('preview-dialog').open,false);assert.equal(f.get('detail-dialog').open,false);
  assert.equal(f.get('pending-count').textContent,'0');
  const posted=f.calls.filter(c=>c.options.method==='POST');
  assert.equal(posted.length,2);assert.notEqual(posted[0].options.headers['Idempotency-Key'],posted[1].options.headers['Idempotency-Key']);
  assert.equal(JSON.parse([...f.storage.values()][0])&&Object.keys(JSON.parse([...f.storage.values()][0])).length,0);

  const lost=await fixture();lost.setNetworkFailures(1);
  await lost.get('refresh-broker').onclick();
  assert.equal(lost.calls.filter(c=>c.options.method==='POST').length,1);
  const pending=JSON.parse([...lost.storage.values()][0]);assert.equal(Object.keys(pending).length,1);
  const oldId=Object.values(pending)[0].id;
  await lost.get('refresh-broker').onclick();
  assert.equal(lost.calls.filter(c=>c.options.method==='POST').length,1,'no blind retry after ambiguous response');
  assert.equal(Object.values(JSON.parse([...lost.storage.values()][0]))[0].id,oldId);
  const retry=lost.get('pending-commands').children[0].children.find(c=>c.textContent==='沿用原 ID 重試');assert(retry);
  await retry.onclick();
  const retried=lost.calls.filter(c=>c.options.method==='POST');assert.equal(retried.length,2);
  assert.equal(retried[1].options.headers['Idempotency-Key'],oldId);

  const unknown=await fixture();unknown.setNetworkFailures(1);
  await unknown.get('refresh-broker').onclick();
  const active=Object.values(JSON.parse([...unknown.storage.values()][0]))[0];
  unknown.commands.set(active.id,{id:active.id,state:'unknown'});
  await unknown.get('refresh-broker').onclick();
  assert.equal(unknown.calls.filter(c=>c.options.method==='POST').length,1);
  assert.equal(unknown.get('pending-commands').hidden,false);
  console.log('Order desk UI: read-only open, detail/review/apply, evidence text, durable IDs, timeout, unknown and same-ID retry passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
