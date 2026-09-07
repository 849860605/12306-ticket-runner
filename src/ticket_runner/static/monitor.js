'use strict';
const $=id=>document.getElementById(id);
const phases={IDLE:['等待启动',-1],CONFIG_SAVED:['偏好已保存',-1],STARTING:['正在启动',0],WAITING:['等待计划时间',-1],LOGIN:['检查登录',0],LOGIN_REQUIRED:['等待扫码登录',0],LOGIN_READY:['登录已确认',0],ACCOUNT_CHECK:['核对未完成订单',0],QUERYING:['正在查询余票',1],QUERY:['正在查询余票',1],NO_MATCH:['暂无匹配 · 继续等票',1],CATALOG_READY:['车次查询完成',1],BACKOFF:['官网异常 · 退避等待',1],MATCHED:['发现符合条件的票',1],PREPARE:['选择乘车人和席别',2],SUBMITTING:['提交意图已保存',3],SUBMIT_AND_RECONCILE:['提交并核对订单',3],RECONCILE:['只回查既有订单',3],ORDER_CREATED:['已核对到待支付订单',4],DRY_RUN:['匹配成功 · 未预订',1],UNKNOWN:['结果未确认 · 已停止',3],ATTENTION:['需要你处理',-1],PAUSED:['任务已暂停',-1],EXPIRED:['任务已截止',-1],RESOLVED:['已记录人工核对',-1]};
let state=null,lastEvents='',started=null,priorTask=null,elapsedAtStop=null;
Object.assign(phases,{ORDER_SUBMITTED:['等待最终确认窗口',3],REVIEW:['核对最终确认信息',3],QUEUED:['官网处理中 · 等待结果',3]});
Object.assign(phases,{ACCOUNT_READY:['登录与订单检查通过',0],PREPARED:['预订信息已核对',2]});
Object.assign(phases,{API_RESULT:['接口查票完成',1],REVALIDATE:['页面复核接口候选票',2]});
const clock=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
function render(next){
  state=next;
  const [title,step]=phases[next.phase] || [next.phase,-1];
  $('phase-title').textContent=next.mode==='demo' && next.phase==='ORDER_CREATED'?'模拟订单完成 · 非真实购票':title;
  $('phase-description').textContent=next.message;
  $('flow-mode').textContent=next.mode==='demo'?'DEMO · 不联网 / 不下单':next.query_backend==='api'?'API QUERY · 浏览器下单':'LIVE ACTIVITY · 实时状态';
  $('query-count').firstChild.textContent=next.query_count;
  $('query-interval').firstChild.textContent=next.interval;
  $('monitor-safety').textContent=next.mode==='demo'?'演示数据，未访问12306，未创建真实订单。':'此小窗只读。关闭小窗不会停止任务；付款请在官方处理。';
  document.querySelectorAll('#flow-steps li').forEach((item,index)=>{
    const complete=(next.completed_steps || []).includes(index);
    item.classList.toggle('current',index===step && !complete);item.classList.toggle('complete',complete);
    item.querySelector('.step-marker').textContent=complete?'✓':String(index+1);
  });
  if(next.active_task!==priorTask){started=null;elapsedAtStop=null;priorTask=next.active_task;}
  const first=next.events.find(event=>event.stage==='STARTING' && event.task_id===next.active_task);
  if(first)started=first.recorded;
  if(!next.busy && started && elapsedAtStop===null)elapsedAtStop=Date.now()/1000-started;
  if(next.busy)elapsedAtStop=null;
  const key=JSON.stringify(next.events);
  if(key!==lastEvents){
    lastEvents=key;
    const log=$('event-log'),nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
    log.replaceChildren();$('event-count').textContent=`${next.events.length} 条`;
    if(!next.events.length){const p=document.createElement('p');p.className='muted small';p.textContent='尚未启动任务，没有模拟进度。';log.append(p);}
    for(const event of next.events){
      const row=document.createElement('div');row.className='event-row';
      if(['MATCHED','ORDER_CREATED','LOGIN_READY'].includes(event.stage))row.classList.add('important');
      if(['UNKNOWN','ATTENTION','BACKOFF'].includes(event.stage))row.classList.add('problem');
      const time=document.createElement('time');time.className='event-time';time.textContent=clock.format(new Date(event.recorded*1000));
      const text=document.createElement('p');text.textContent=event.message + (event.details.date?` · ${event.details.date}`:'');
      if(event.details.offer)text.textContent+=` · ${event.details.offer.train} ${event.details.offer.departure.slice(0,5)} ${event.details.offer.seat}`;
      row.append(time,text);log.append(row);
    }
    if(nearBottom)log.scrollTop=log.scrollHeight;
  }
  updateClock();
}
function updateClock(){
  if(!started){$('elapsed').textContent='—';return;}
  const seconds=Math.max(0,Math.floor(elapsedAtStop ?? (Date.now()/1000-started)));
  $('elapsed').textContent=seconds<3600?`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`:`${Math.floor(seconds/3600)}h${Math.floor(seconds/60)%60}m`;
}
(async()=>{
  try{
    const response=await fetch('/api/bootstrap',{credentials:'same-origin'});if(!response.ok)throw Error('连接失败');
    const boot=await response.json();render(boot.snapshot);
    const source=new EventSource('/api/events');
    source.onopen=()=>{$('flow-connection').textContent='实时连接';$('flow-connection').className='pill';};
    source.addEventListener('snapshot',event=>{try{render(JSON.parse(event.data));}catch{$('flow-connection').textContent='数据异常';}});
    source.onerror=()=>{$('flow-connection').textContent='正在重连';$('flow-connection').className='pill warning';};
    setInterval(updateClock,1000);
  }catch{$('phase-title').textContent='无法连接控制台';$('phase-description').textContent='请检查本地服务，然后刷新小窗。不会因为断线自动重新下单。';}
})();
