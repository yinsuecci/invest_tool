'use strict';
const report=JSON.parse(document.getElementById('report').textContent),$=id=>document.getElementById(id);
const keys=Object.keys(report.assets),zone=8*3600000,day=86400000;
const state={period:'full',policy:report.default_policy,base:'four',asset:'SOL',chart:null,rangeKey:''};
const names={SPY:'标普500',QQQ:'纳指100',XAU:'黄金',GOOG:'GOOG',SOL:'SOL',BTC:'BTC',ETH:'ETH'};
const fmt=(value,n=2)=>value===null||value===undefined?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:n,maximumFractionDigits:n});
const signed=(value,n=2)=>(value>0?'+':'')+fmt(value,n),local=t=>new Date(t+zone).toISOString().slice(0,16).replace('T',' ');
const dateMs=d=>Date.parse(d+'T00:00:00+08:00');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const visiblePolicies=report.family==='chronological'?{[report.default_policy]:report.policies[report.default_policy]}:report.policies;
Object.entries(visiblePolicies).forEach(([value,label])=>$('policy').add(new Option(value===report.default_policy?(report.family==='chronological'?'回测首选 · '+label:'逐标的策略'):label,value)));
if(report.period_labels){$('period').replaceChildren();Object.entries(report.period_labels).forEach(([value,label])=>$('period').add(new Option(label,value)));}
if(report.comparison_note)$('comparison-note').textContent=report.comparison_note;
keys.forEach(k=>$('asset').add(new Option(names[k]+' · '+k,k)));
$('policy').value=state.policy;$('asset').value=state.asset;
function update(){
  const p=report.results[state.period][state.policy],a=p.current,b=p[state.base];
  $('strategy-equity').textContent=fmt(a.equity)+' U';$('ordinary-equity').textContent=fmt(b.equity)+' U';
  $('strategy-profit').textContent='损益 '+signed(a.profit)+' U';$('ordinary-profit').textContent='损益 '+signed(b.profit)+' U';
  $('equity-delta').textContent=signed(a.equity-b.equity)+' U';$('equity-delta').className=a.equity>=b.equity?'positive':'negative';
  $('same-budget').textContent='各投入 '+fmt(a.contributed)+' U'+(a.average_daily_spend!==undefined?' · 日均买入 '+fmt(a.average_daily_spend)+' U':'');$('date-range').textContent=a.start+' — '+a.end+' · '+a.days+'天';
  $('cost-rows').innerHTML=keys.map(k=>{const x=a.assets[k],y=b.assets[k],gain=x.unit_cost&&y.unit_cost?(1-x.unit_cost/y.unit_cost)*100:null;
    const count=r=>r.orders.filter(o=>o.asset===k&&o.quantity>0).length;
    return '<tr role="button" tabindex="0" data-asset="'+k+'" aria-selected="'+(k===state.asset)+'" aria-label="查看'+k+'买点"><td title="'+esc(report.assets[k].identity.symbol)+'">'+names[k]+'</td><td>'+fmt(y.unit_cost,y.unit_cost>10000?2:4)+'</td><td>'+fmt(x.unit_cost,x.unit_cost>10000?2:4)+'</td><td class="'+(gain===null?'neutral':gain>=0?'positive':'negative')+'">'+(gain===null?'—':signed(gain)+'%')+'</td><td>'+count(a)+' / '+count(b)+'</td><td>'+fmt(x.spent+x.fees)+'</td></tr>';
  }).join('');
  document.querySelectorAll('[data-asset]').forEach(row=>{const select=()=>{state.asset=row.dataset.asset;$('asset').value=state.asset;update();};row.onclick=select;row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}};});
  $('footer-source').textContent='币安现货 · UTC+8 · 费率 '+report.config.fee_bps+'bp / 滑点 '+report.config.slip_bps+'bp';
  const rangeKey=state.period+'/'+state.asset;
  if(state.rangeKey!==rangeKey){state.rangeKey=rangeKey;$('range-end').value=a.end;$('range-start').value=local(Math.max(dateMs(a.start),dateMs(a.end)-13*day)).slice(0,10);}
  for(const id of ['range-start','range-end']){$(id).min=a.start;$(id).max=a.end;}
  draw();
}
function draw(){
  if(state.chart){state.chart.remove();state.chart=null;}
  const p=report.results[state.period][state.policy],start=dateMs($('range-start').value),end=dateMs($('range-end').value)+day;
  const error=!Number.isFinite(start)||!Number.isFinite(end)||end<=start?'日期无效':end-start>31*day?'最多展示31天':start<dateMs(p.current.start)||end>dateMs(p.current.end)+day?'日期超出区间':'';
  $('chart-error').textContent=error;$('candles').replaceChildren();$('buy-rows').replaceChildren();$('selected-fill').textContent='';if(error)return;
  const LC=window.LightweightCharts;if(!LC||LC.version()!=='5.2.1'){$('chart-error').textContent='图表库不可用';return;}
  const unit=Number($('unit').value)*3600000,bucket=t=>Math.floor((t+zone)/unit)*unit-zone,stamp=t=>Math.floor((t+zone)/1000);
  const chart=LC.createChart($('candles'),{autoSize:true,layout:{background:{type:'solid',color:'#ffffff'},textColor:'#5d718b',fontSize:11,attributionLogo:true},grid:{vertLines:{color:'#f0f4f9'},horzLines:{color:'#edf2f7'}},timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#dce6f0'},rightPriceScale:{borderColor:'#dce6f0'},localization:{locale:'zh-CN',timeFormatter:t=>new Date(t*1000).toISOString().slice(0,16).replace('T',' ')}});
  state.chart=chart;const series=chart.addSeries(LC.CandlestickSeries,{upColor:'#c27464',downColor:'#53856f',wickUpColor:'#c27464',wickDownColor:'#53856f',borderVisible:false,lastValueVisible:true,priceLineVisible:false});
  const bars=new Map();for(const b of report.assets[state.asset].candles){if(b[0]<start||b[0]>=end)continue;const t=bucket(b[0]);let row=bars.get(t);if(!row){row={time:stamp(t),open:b[1],high:b[2],low:b[3],close:b[4]};bars.set(t,row);}row.high=Math.max(row.high,b[2]);row.low=Math.min(row.low,b[3]);row.close=b[4];}
  series.setData([...bars.values()]);const averages=new Map();for(const r of report.assets[state.asset].indicators||[]){if(r[0]>=start&&r[0]<end&&Number.isFinite(r[2]))averages.set(bucket(r[0]),r[2]);}
  const ma=chart.addSeries(LC.LineSeries,{color:'#9aabc1',lineWidth:1,title:'EMA168',lastValueVisible:false,priceLineVisible:false});ma.setData([...averages].map(([t,value])=>({time:stamp(t),value})));
  const trades=[];for(const [source,result] of [['策略',p.current],['普通',p[state.base]]])for(const order of result.orders){const t=Date.parse(order.time);if(order.asset===state.asset&&order.quantity>0&&t>=start&&t<end)trades.push({...order,t,source});}
  trades.sort((a,b)=>a.t-b.t);const groups=new Map();for(const trade of trades){const t=bucket(trade.t),key=t+'/'+trade.source;if(!groups.has(key))groups.set(key,{t,source:trade.source,count:0});groups.get(key).count++;}
  const marks=[...groups.values()].sort((a,b)=>a.t-b.t||a.source.localeCompare(b.source)).map(g=>({time:stamp(g.t),position:g.source==='策略'?'belowBar':'aboveBar',shape:g.source==='策略'?'arrowUp':'circle',color:g.source==='策略'?'#155bd7':'#7c8fa9',text:(g.source==='策略'?'策':'普')+(g.count>1?'×'+g.count:'')}));
  LC.createSeriesMarkers(series,marks);chart.timeScale().fitContent();
  $('buy-rows').innerHTML=trades.map(t=>'<tr><td>'+local(t.t)+'</td><td>'+t.source+'</td><td>'+fmt(t.price,4)+'</td><td>'+fmt(t.quantity,8)+'</td><td>'+fmt(t.notional+t.fee,4)+'</td></tr>').join('');
  const show=trade=>{$('selected-fill').textContent=trade?local(trade.t)+' · '+trade.source+'买入 '+fmt(trade.notional+trade.fee)+' U @ '+fmt(trade.price,4)+' · '+fmt(trade.quantity,8)+' '+state.asset:state.asset+' · 当前窗口无成交';};
  show(trades.find(t=>t.source==='策略')||trades[0]);
  chart.subscribeClick(event=>{if(!event.time)return;const t=Number(event.time)*1000-zone;show(trades.find(x=>bucket(x.t)===t&&x.source==='策略')||trades.find(x=>bucket(x.t)===t));});
}
for(const id of ['period','policy','baseline'])$(id).onchange=()=>{state[id==='baseline'?'base':id]=$(id).value;update();};
$('asset').onchange=()=>{state.asset=$('asset').value;update();};for(const id of ['range-start','range-end','unit'])$(id).onchange=draw;
let token='',accountState=null;
async function api(path,body){if(!token){const s=await fetch('/api/session',{credentials:'same-origin'});if(!s.ok)throw new Error('本地服务未连接');token=(await s.json()).csrf_token;}const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'X-DCA-Token':token,...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await response.json();if(!response.ok)throw new Error(data.error||'请求失败');return data;}
function accountMessage(text,error=false){$('account-message').textContent=text;$('account-message').className=error?'error':'';}
function lockControls(){if(!accountState)return;for(const id of ['disconnect','load-products','earn-preview','earn-run','refresh-account'])$(id).disabled=!accountState.connected;$('auto-earn').disabled=!accountState.connected||$('mode').value==='read_only';}
function showAccount(s){accountState=s;$('connection-dot').className=s.connected?'connected':'';$('account-connection').textContent=s.connected?(s.mode==='live'?'实盘':s.mode==='paper'?'已连接 / 模拟':'已连接 / 只读'):'未连接';$('spot-free').textContent=fmt(s.free_usdt);$('earn-balance').textContent=fmt(s.earn_usdt);$('earn-status').textContent=s.blocked?'待核对，已暂停':s.auto_earn?(s.mode==='paper'?'模拟运行':'运行中'):'关闭';$('mode').value=s.mode;$('mode').querySelector('[value=live]').disabled=!s.live_enabled;$('mode').querySelector('[value=live]').textContent=s.live_enabled?'实盘':'实盘（服务未开放）';$('reserve').value=s.reserve_usdt;$('auto-earn').checked=s.auto_earn;if(s.product_id&&!Array.from($('product').options).some(o=>o.value===s.product_id))$('product').add(new Option(s.product_id,s.product_id));if(s.product_id)$('product').value=s.product_id;lockControls();}
async function products(){const data=await api('/api/earn/products');$('product').replaceChildren(new Option('选择USDT活期产品',''));for(const p of data.products){const option=new Option(p.product_id+' · '+(p.apr===null?'—':fmt(Number(p.apr)*100))+'%'+(p.available?'':' / 不可用'),p.product_id);option.disabled=!p.available;$('product').add(option);}if(accountState?.product_id)$('product').value=accountState.product_id;accountMessage('已查询活期产品，利率浮动。');}
async function action(button,task){button.disabled=true;try{await task();}catch(error){accountMessage(error.message||'请求失败',true);if(accountState)showAccount(accountState);}finally{button.disabled=false;lockControls();}}
$('mode').onchange=()=>{if($('mode').value==='read_only')$('auto-earn').checked=false;lockControls();};
$('account-toggle').onclick=()=>{$('account-panel').open=!$('account-panel').open;$('account-toggle').setAttribute('aria-expanded',String($('account-panel').open));};$('account-panel').ontoggle=()=>{$('account-toggle').setAttribute('aria-expanded',String($('account-panel').open));};
$('connect-form').onsubmit=event=>{event.preventDefault();const button=event.submitter;action(button,async()=>{try{showAccount(await api('/api/connect',{api_key:$('api-key').value,api_secret:$('api-secret').value}));accountMessage('连接成功，当前为模拟模式。');try{await products();}catch(error){accountMessage('现货已连接；活期产品暂不可用。',true);}}finally{$('api-key').value='';$('api-secret').value='';}});};
$('disconnect').onclick=()=>action($('disconnect'),async()=>{showAccount(await api('/api/disconnect',{}));$('product').replaceChildren(new Option('先连接账户',''));accountMessage('已断开，自动理财已关闭。');});
$('load-products').onclick=()=>action($('load-products'),products);
$('earn-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{showAccount(await api('/api/settings',{mode:$('mode').value,auto_earn:$('auto-earn').checked,reserve_usdt:$('reserve').value,product_id:$('product').value}));accountMessage(accountState.mode==='paper'?'已保存，仅模拟；自动任务每5分钟检查。':'已保存。');});};
$('refresh-account').onclick=()=>action($('refresh-account'),async()=>{showAccount(await api('/api/refresh',{}));accountMessage('余额已刷新。');});
$('earn-preview').onclick=()=>action($('earn-preview'),async()=>{const p=await api('/api/earn/preview',{});accountMessage('可申购 '+fmt(p.amount)+' U；保留 '+fmt(p.reserve_usdt)+' U（'+(p.mode==='paper'?'模拟':p.mode==='live'?'实盘':'只读')+'）。');});
$('earn-run').onclick=()=>action($('earn-run'),async()=>{const result=await api('/api/earn/run',{request_id:'ui_'+crypto.randomUUID().replaceAll('-','')});accountMessage(({simulated:'模拟完成，未申购真实理财。',completed:'请求已提交。',unknown:'结果未知，已暂停，禁止重试。',failed:'请求失败。',skipped:'暂无可申购闲置USDT。'}[result.state]||'请检查账户状态。'));showAccount(await api('/api/state'));});
update();api('/api/state').then(showAccount).catch(()=>accountMessage('账户服务未连接，回测可正常查看。',true));
