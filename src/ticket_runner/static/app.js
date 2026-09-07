'use strict';

const $ = (id) => document.getElementById(id);
let token = '', baseConfig = null, fingerprint = '', snapshot = null, connected = false;
let dirty = false, pending = false, selectedTrains = new Set(), seatPriority = [];
let lastCatalog = '', lastHistory = '', resolvingTask = '', toastTimer;
const stations = new Map([['深圳北', 'IOQ'], ['信阳东', 'OYN'], ['南京南', 'NKH']]);
const phaseNames = {READY:'尚未启动', WAITING:'等待开始', QUERYING:'正在查询', BACKOFF:'失败退避', PAUSED:'已暂停', DRY_RUN:'发现匹配票 · 未预订', ATTENTION:'需要处理', SUBMITTING:'正在提交', UNKNOWN:'提交结果未确认', ORDER_CREATED:'曾核对到待支付订单', EXPIRED:'已截止', DONE:'已处理'};

function notify(message, error = false) {
  clearTimeout(toastTimer); $('toast').textContent = message;
  $('toast').classList.toggle('error', error); $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 7000 : 4000);
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function beijingInput(iso) { return new Date(new Date(iso).getTime() + 8 * 3600000).toISOString().slice(0, 16); }
function dayRange(first, last) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(first) || !/^\d{4}-\d{2}-\d{2}$/.test(last) || first > last) throw Error('请检查最早、最晚出发日期');
  const dates = []; let value = Date.parse(first + 'T00:00:00Z'); const end = Date.parse(last + 'T00:00:00Z');
  while (value <= end && dates.length < 16) { dates.push(new Date(value).toISOString().slice(0, 10)); value += 86400000; }
  if (!dates.length || dates.length > 15) throw Error('最多选择 15 个乘车日期');
  return dates;
}
function formConfig() {
  if (!baseConfig) throw Error('配置尚未加载');
  const fields = $('config-fields'), wasDisabled = fields.disabled;
  fields.disabled = false;
  try {
    if (!$('journey-form').checkValidity()) {
      document.querySelector('.advanced').open = true; $('journey-form').reportValidity();
      throw Error('请填写有效的行程、乘车人、预算和执行时间');
    }
  } finally { fields.disabled = wasDisabled; }
  if (!seatPriority.length) throw Error('请至少选择一种席别');
  if (!$('all-trains').checked && !selectedTrains.size) throw Error('请选择车次，或明确勾选“接受符合条件的全部车次”');
  const config = structuredClone(baseConfig);
  config.query_backend = $('query-backend').value;
  config.journey = {origin:{name:$('origin').value.trim(),code:$('origin-code').value.trim().toUpperCase()}, destination:{name:$('destination').value.trim(),code:$('destination-code').value.trim().toUpperCase()}, dates:dayRange($('date-from').value, $('date-to').value), passengers:$('passengers').value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean)};
  config.preferences = {...config.preferences, trains:$('all-trains').checked ? [] : [...selectedTrains], seats:[...seatPriority], max_total_price:$('budget').value, departure_after:$('time-from').value, departure_before:$('time-to').value};
  config.execution = {...config.execution, start_at:$('start-at').value + ':00+08:00', stop_at:$('stop-at').value + ':00+08:00', query_interval_seconds:Number($('interval').value), auto_submit:false};
  config.execution.max_backoff_seconds = Math.max(config.execution.max_backoff_seconds, config.execution.query_interval_seconds);
  return config;
}
function setDirty() {
  dirty = true; $('save-status').textContent = '有未保存的更改'; $('submit-consent').checked = false;
  renderSelection(); renderPlan(); updateControls();
}
function renderSelection() {
  const all = $('all-trains').checked;
  $('selected-summary').textContent = all ? '接受符合其他条件的全部车次' : selectedTrains.size ? `已选 ${selectedTrains.size} 个车次（适用全部日期）· ${[...selectedTrains].join(' → ')}` : '尚未选择车次';
  if (document.activeElement !== $('manual-trains')) $('manual-trains').value = all ? '' : [...selectedTrains].join('，');
  document.querySelectorAll('.train-row').forEach(row => {
    const selected = all || selectedTrains.has(row.dataset.train);
    row.classList.toggle('selected', selected); row.querySelector('input').checked = selected;
  });
}
function renderPlan() {
  if (!baseConfig) return;
  const trains = $('all-trains').checked ? '符合条件的全部车次' : [...selectedTrains].join('、') || '尚未选择车次';
  const people = $('passengers').value.split(/[,，\n]/).filter(x=>x.trim()).length;
  $('plan-summary').textContent = `${$('origin').value} → ${$('destination').value} · ${$('date-from').value} 至 ${$('date-to').value}\n${$('time-from').value}–${$('time-to').value} 发车 · ${trains}\n${seatPriority.join(' → ') || '请选择席别'} · ${people} 位成人 · 合计不超过 ¥${$('budget').value}`;
}
function wantsSubmit() { return document.querySelector('input[name="run-mode"]:checked').value === 'submit'; }
function updateControls() {
  const busy = Boolean(snapshot?.busy), disabled = pending || busy || !baseConfig;
  $('config-fields').disabled = disabled;
  $('query-button').disabled = disabled || !connected;
  $('save-button').disabled = disabled || !connected;
  $('login-button').disabled = disabled || !connected;
  $('all-trains').disabled = disabled;
  $('clear-trains').disabled = disabled;
  document.querySelectorAll('.train-row input, input[name="run-mode"]').forEach(x=>{ x.disabled = disabled; });
  $('submit-consent').disabled = disabled;
  const submit = wantsSubmit(); $('consent-row').hidden = !submit;
  $('start-button').textContent = dirty ? '保存并核对当前选择' : submit ? (snapshot?.mode === 'demo' ? '开始模拟抢票' : '开始抢票 · 有票提交') : '开始只读监控';
  $('start-button').disabled = disabled || !connected || (!dirty && (Boolean(snapshot?.blocker) || (submit && !$('submit-consent').checked)));
  $('stop-button').disabled = pending || !busy;
  document.querySelectorAll('.task-actions button').forEach(x=>{ x.disabled = disabled || !connected; });
}
async function api(path, payload) {
  const options = {credentials:'same-origin', headers:{}};
  if (payload !== undefined) { options.method = 'POST'; options.headers = {'Content-Type':'application/json','X-Control-Token':token}; options.body = JSON.stringify(payload); }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); } catch { throw Error('控制台返回异常，请检查服务是否仍在运行'); }
  if (!response.ok) throw Error(result.detail || `请求失败（${response.status}）`);
  return result;
}
async function action(operation) {
  if (pending) return; pending = true; updateControls();
  try { await operation(); renderSnapshot(await api('/api/status')); }
  catch(error) { notify(error.message || '操作未完成，请检查连接', true); }
  finally { pending = false; updateControls(); }
}
async function save() {
  const result = await api('/api/config', formConfig());
  baseConfig = result.config; fingerprint = result.fingerprint; dirty = false;
  $('submit-consent').checked = false; $('save-status').textContent = '偏好已保存';
  renderPlan(); updateControls();
}
function fill(config) {
  baseConfig = config;
  $('query-backend').value = config.query_backend || 'browser';
  for (const station of [config.journey.origin, config.journey.destination]) stations.set(station.name,station.code);
  $('origin').value = config.journey.origin.name; $('origin-code').value = config.journey.origin.code;
  $('destination').value = config.journey.destination.name; $('destination-code').value = config.journey.destination.code;
  const days = [...config.journey.dates].sort(); $('date-from').value = days[0]; $('date-to').value = days.at(-1);
  const today = new Date(Date.now() + 8*3600000).toISOString().slice(0,10);
  const max = new Date(Date.parse(today+'T00:00:00Z') + 14*86400000).toISOString().slice(0,10);
  for (const id of ['date-from','date-to']) { $(id).min=today; $(id).max=max; }
  $('time-from').value = config.preferences.departure_after.slice(0,5); $('time-to').value = config.preferences.departure_before.slice(0,5);
  seatPriority = [...config.preferences.seats];
  document.querySelectorAll('input[name="seat"]').forEach(x=>{x.checked = seatPriority.includes(x.value);});
  $('passengers').value = config.journey.passengers.join('，'); $('budget').value = config.preferences.max_total_price;
  $('start-at').value = beijingInput(config.execution.start_at); $('stop-at').value = beijingInput(config.execution.stop_at);
  $('interval').value = config.execution.query_interval_seconds;
  selectedTrains = new Set(config.preferences.trains); $('all-trains').checked = !selectedTrains.size;
  $('save-status').textContent = '已载入偏好 · 未启动'; renderSelection(); renderPlan();
  // The date editor is a range: make expansion explicit rather than silently buying extra dates.
  if (dayRange(days[0], days.at(-1)).length !== days.length) { setDirty(); notify('原配置包含非连续日期；当前编辑器按整个日期区间监控，请重新核对并保存。', true); }
}
function renderCatalog(state) {
  const key = JSON.stringify([state.catalog, seatPriority, state.busy === 'query']);
  if (key === lastCatalog) return; lastCatalog = key;
  $('train-count').textContent = state.catalog.length;
  const list = $('train-list'); list.replaceChildren();
  if (!state.catalog.length) {
    const empty = node('div','empty-state'); empty.append(node('div','empty-track','··· → ···'));
    empty.append(node('h3','',state.busy === 'query' ? (state.mode==='demo' ? '正在加载模拟车次…' : '正在从官网读取车次…') : '还没有可选的查询结果'));
    empty.append(node('p','',state.busy === 'query' ? '按日期依次查询，不预订、不提交；结果会陆续出现在这里。' : '填写行程后查询。也可以在高级设置手填车次，无票时持续监控。'));
    list.append(empty); return;
  }
  for (const train of state.catalog) {
    const row = node('label','train-row'); row.dataset.train = train.train;
    const check = document.createElement('input'); check.type='checkbox'; check.setAttribute('aria-label',`选择 ${train.date} ${train.train} ${train.departure} 发车`);
    check.addEventListener('change',()=>{ $('all-trains').checked=false; check.checked ? selectedTrains.add(train.train) : selectedTrains.delete(train.train); setDirty(); });
    const identity=node('div','train-identity'); identity.append(node('strong','',train.train),node('small','',train.date.slice(5).replace('-','月')+'日'));
    const times=node('div','train-times');
    for (const [index,info] of [[train.departure,train.origin],[train.arrival || '—',train.destination]].entries()) {
      if (index) times.append(node('span','route-line'));
      const stop=node('div',''); stop.append(node('strong','',info[0]),node('small','',info[1])); times.append(stop);
    }
    const quotes=node('div','train-seats');
    const seats=train.seats.filter(x=>seatPriority.includes(x.name));
    for (const seat of seats) {
      const quote=node('div','seat-quote'); const sold=['候补','无','--','0'].includes(seat.status);
      quote.append(node('span','',seat.name),node('strong','',seat.price == null ? '报价待核实' : `¥${seat.price}`),node('span',`availability${sold?' sold-out':''}`,sold ? '暂无票 · 可监控' : seat.status==='有' ? '有票' : `余 ${seat.status} 张`)); quotes.append(quote);
    }
    if (!seats.length) quotes.append(node('span','muted small','所选席别暂无可核实报价 · 可监控'));
    row.append(check,identity,times,quotes); list.append(row);
  }
  renderSelection(); updateControls();
}
function renderHistory(state) {
  const key=JSON.stringify(state.tasks); if (key===lastHistory) return; lastHistory=key;
  const list=$('task-history'); list.replaceChildren();
  if (!state.tasks.length) { list.append(node('p','muted small','还没有任务记录。打开控制台不会自动下单。')); return; }
  for (const task of state.tasks.slice(0,5)) {
    const item=node('div','task-item'), head=node('div','task-item-head');
    head.append(node('strong','',phaseNames[task.state] || task.state));
    const date=new Date(task.updated*1000); head.append(node('span','muted small',date.toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit'})));
    item.append(head,node('p','',task.message),node('div','task-id',task.task_id));
    if (task.receipt) item.append(node('p','',`${task.receipt.source==='synthetic_demo_not_a_real_order'?'模拟订单 · ':''}${task.receipt.passenger_count} 人 · 合计 ¥${task.receipt.total_price} · ${task.receipt.assigned_seats.join('、')}`));
    if (['SUBMITTING','UNKNOWN','ORDER_CREATED','ATTENTION','DRY_RUN'].includes(task.state)) {
      const actions=node('div','task-actions');
      if (['SUBMITTING','UNKNOWN','ORDER_CREATED'].includes(task.state)) {
        const reconcile=node('button','text-button','只回查订单'); reconcile.type='button'; reconcile.onclick=()=>action(async()=>{ await api('/api/reconcile',{task_id:task.task_id}); connectDesktop(); }); actions.append(reconcile);
      }
      const resolve=node('button','text-button','已在官方核对？'); resolve.type='button'; resolve.onclick=()=>{resolvingTask=task.task_id;$('resolve-confirmation').value='';$('resolve-dialog').showModal();}; actions.append(resolve); item.append(actions);
    }
    list.append(item);
  }
}
function connectDesktop() {
  if (snapshot?.mode==='demo') return;
  const url = new URL($('desktop-link').href);
  $('desktop-frame').src = url.href; $('desktop-frame').hidden=false; $('desktop-placeholder').hidden=true;
}
function renderSnapshot(state) {
  snapshot=state;
  $('mode-badge').textContent = state.mode==='demo' ? '演示模式 · 不联网' : state.query_backend==='api' ? '接口查票 · 浏览器下单' : '官网浏览器模式';
  $('demo-banner').hidden = state.mode!=='demo';
  if (state.mode==='demo') {
    document.querySelector('.desktop-card > p').textContent='演示环境仅验证界面和流程，不连接官网浏览器或真实账号。';
    document.querySelector('#consent-row span').textContent='我已核对演示条件，允许执行模拟提交；不产生真实订单。';
  }
  $('login-status').textContent = state.mode==='demo' ? '模拟登录' : state.logged_in ? '最近检查已登录' : state.phase==='LOGIN_REQUIRED' ? '请扫码' : '未检查登录';
  $('blocker-banner').hidden = !state.blocker;
  if (state.blocker) $('blocker-banner').textContent = `安全保护：存在「${phaseNames[state.blocker.state] || state.blocker.state}」记录。可以保存偏好、查询车次，但新任务暂不能启动。请在右侧回查并核对官方订单；不要为测试重复下单。`;
  if (state.mode==='demo') { $('desktop-placeholder').querySelector('strong').textContent='演示环境不打开官方浏览器'; $('desktop-placeholder').querySelector('span:last-child').textContent='所有流程为模拟，不登录、不付款'; $('desktop-link').hidden=true; $('login-button').textContent='模拟登录'; }
  $('notification-hint').textContent = state.notification_configured ? `手机通知已配置 · ${state.pending_notifications} 条待投递` : '手机通知尚未配置：目前只显示在此页面并保留本地记录。';
  renderCatalog(state); renderHistory(state); updateControls();
}
function establishEvents() {
  const events=new EventSource('/api/events');
  events.addEventListener('open',()=>{connected=true;$('connection-banner').hidden=true;updateControls();});
  events.addEventListener('snapshot',event=>{try {renderSnapshot(JSON.parse(event.data));} catch {notify('收到的状态无法读取，请刷新页面',true);}});
  events.onerror=()=>{connected=false;$('connection-banner').hidden=false;updateControls();};
}

$('journey-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{await save();notify('偏好已保存，没有启动抢票');});});
$('config-fields').addEventListener('input',event=>{
  if (event.target.name==='seat') {
    seatPriority=seatPriority.filter(x=>x!==event.target.value); if(event.target.checked) seatPriority.push(event.target.value);
    if(snapshot) {lastCatalog='';renderCatalog(snapshot);}
  }
  if (event.target.id==='manual-trains') {selectedTrains=new Set(event.target.value.toUpperCase().split(/[,，\s]+/).filter(Boolean));$('all-trains').checked=false;}
  for (const id of ['origin','destination']) if (event.target.id===id) {const code=stations.get(event.target.value.trim());$(id+'-code').value=code || '';}
  if (['origin','destination','date-from','date-to'].includes(event.target.id)) {selectedTrains.clear();$('all-trains').checked=false;}
  setDirty();
});
$('swap').onclick=()=>{for(const suffix of ['', '-code']){const old=$('origin'+suffix).value;$('origin'+suffix).value=$('destination'+suffix).value;$('destination'+suffix).value=old;}selectedTrains.clear();$('all-trains').checked=false;setDirty();};
$('all-trains').onchange=()=>{if($('all-trains').checked)selectedTrains.clear();setDirty();};
$('clear-trains').onclick=()=>{selectedTrains.clear();$('all-trains').checked=false;setDirty();};
$('query-button').onclick=()=>action(async()=>{
  // Querying a timetable must not require a train to have already been selected.
  const previous=$('all-trains').checked;
  if (!selectedTrains.size) $('all-trains').checked=true;
  try {await save();} finally {if(!previous && !selectedTrains.size){$('all-trains').checked=false;setDirty();}}
  await api('/api/query',{});
});
$('login-button').onclick=()=>action(async()=>{connectDesktop();await api('/api/login',{});});
$('stop-button').onclick=()=>action(async()=>{await api('/api/stop',{});notify('已暂停。不会取消订单，也不会付款。');});
$('start-button').onclick=()=>action(async()=>{
  if(dirty){await save();notify('选择已保存，请再次核对后启动');return;}
  const submit=wantsSubmit();
  await api('/api/start',{auto_submit:submit,confirmed:submit && $('submit-consent').checked,fingerprint});
  $('submit-consent').checked=false;notify(snapshot?.mode==='demo'?'已开始模拟流程，不会创建真实订单':'任务已启动，可在右侧实时查看进度');
});
document.querySelectorAll('input[name="run-mode"]').forEach(x=>{x.onchange=()=>{$('submit-consent').checked=false;updateControls();};});
$('submit-consent').onchange=updateControls;
$('resolve-cancel').onclick=()=>{$('resolve-dialog').close();};
$('resolve-form').onsubmit=event=>{event.preventDefault();action(async()=>{await api('/api/resolve',{task_id:resolvingTask,outcome:$('resolve-outcome').value,confirmation:$('resolve-confirmation').value});$('resolve-dialog').close();notify('已记录核对结果，尚未恢复抢票');});};

(async()=>{
  try {
    const boot=await api('/api/bootstrap');token=boot.token;fingerprint=boot.fingerprint;fill(boot.config);renderSnapshot(boot.snapshot);
    const desktop=new URL(location.href);desktop.port='6080';desktop.pathname='/vnc.html';desktop.search='autoconnect=1&resize=scale';desktop.hash='';$('desktop-link').href=desktop.href;
    establishEvents();
    try {const response=await fetch('/assets/stations.json');if(response.ok){const values=await response.json();const list=$('stations');list.replaceChildren();for(const station of values){stations.set(station.name,station.code);const option=document.createElement('option');option.value=station.name;option.label=station.pinyin;list.append(option);}}}catch{/* Existing configured stations and manual code input remain available. */}
  } catch(error){$('save-status').textContent='连接失败';notify(error.message,true);}
})();
