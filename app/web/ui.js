import {api, download, stop, running, exportFile} from './downloader.js';
import {allLocal, getLocal, saveLocal, deleteLocal, supportsStorage, removeLocalFiles} from './local-store.js';
import {initDestination,destinationFile,destination} from './destination.js';
import {analysisStatus} from './analysis-status.js';
import {showCatalog,closeCatalog} from './catalog-dialog.js';
import {reportError,errorText,forgetError} from './error-dialog.js';
import {playerInfo} from './player-info.js';
import {resumableQueue} from './queue-policy.js';
import {initConsent,preferencesAllowed,removePreferenceCookie} from './consent.js';

await initConsent();

const $=s=>document.querySelector(s);
const empty=$('#jobs').innerHTML;
const labels={client_downloading:'Идёт загрузка',paused:'На паузе',completed:'Готово',error:'Ошибка',cancelled:'Отменено пользователем',ready:'Готовый файл',streaming:'Передача',downloading:'Предыдущее задание',queued:'Предыдущее задание',cancelling:'Отмена…',pausing:'Пауза…'};
Object.assign(labels,{pending:'В очереди',analyzing:'Проверяем серию',awaiting:'Можно скачать',selection:'Выбор серий'});
let serverJobs=[],localJobs=[],checkJob=null,sequence=0,timer,busy=false,capabilityError='',checking=false,checkKey='';
const live=new Map();
let lastRender=0,primaryJobId=null;
let catalogShown=null,batchActive=false,queueStopped=false,queueCurrent=null;
let queueState=null;
let playerData=null;
function renderPlayer(){
  const detection=checkJob?.player_detection||(queueCurrent&&serverJobs.find(job=>job.id===queueCurrent)?.player_detection);
  if(detection)playerData=detection;
  playerInfo(playerData,{visible:!!$('#url').value.trim(),pending:checking&&!playerData});
}
const cancelledHere=new Set();
function notify(error,options={}){message(errorText(error));reportError(error,options);}
function message(text){$('#message').textContent=text;}
function bytes(value){if(value==null)return 'размер неизвестен';const u=['Б','КБ','МБ','ГБ'];let n=value,i=0;while(n>=1024&&i<3){n/=1024;i++;}return n.toFixed(i?1:0)+' '+u[i];}
const historyClock=new Intl.DateTimeFormat('ru-RU',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
function historyDate(job){
  const finished=job.finished_at||(job.status==='completed'&&job.completedAt?job.completedAt/1000:null);
  const seconds=finished||job.created_at;
  if(!seconds||!Number.isFinite(Number(seconds)))return {text:'Дата не сохранена',iso:''};
  const date=new Date(Number(seconds)*1000);
  const prefix=finished?(job.status==='completed'?'Загружено':job.status==='cancelled'?'Отменено':job.status==='error'?'Неудачная попытка':'Добавлено'):'Добавлено';
  return {text:prefix+': '+historyClock.format(date),iso:date.toISOString()};
}
async function forgetJob(job){
  try{await api(`jobs/${job.id}/forget`,'POST');}catch(e){if(e.httpStatus!==404)throw e;}
  await removeLocalFiles(job.id);await deleteLocal(job.id);live.delete(job.id);
}
function checkResult(title,detail,kind=''){
  $('#check-result').hidden=false;
  $('#check-title').textContent=title;$('#check-detail').textContent=detail;$('#check-result').className='check-result '+kind;
  let state='idle',text=$('#url').value.trim()?title:'Вставьте ссылку для анализа',percent=0;
  if($('#url').value.trim()){
    if(title==='Проверяем ссылку…'){state='analyzing';percent=checkJob?.analysis_percent||0;}
    else if(title==='Можно скачать'||title==='Идёт загрузка'){state='success';percent=100;}
    else if(kind==='bad'){state='error';text=title+': '+detail;percent=100;}
  }
  analysisStatus(state,percent,text);
  renderPlayer();
}
function updateButton(){const active=busy||batchActive,b=$('#download');b.textContent=active?'Идёт загрузка':'Скачать';b.disabled=active||checking||!!capabilityError||!['awaiting','selection'].includes(checkJob?.status);$('#url').disabled=active;$('#quality').disabled=active;$('#destination-button').disabled=active;}
function describeCheck(){
  if(busy){updateButton();return;}
  if(capabilityError&&$('#url').value.trim()){checkResult('Скачать в этом браузере нельзя',capabilityError,'bad');}
  else if(checkJob?.status==='selection'){
    const embedded=checkJob.catalog.kind==='embedded';
    checkResult(embedded?'Найдены видеоисточники':'Найдены серии',embedded?'Выберите источник в открывшемся окне. Найденная ссылка ещё не гарантирует доступность потока.':'Выберите озвучку и серии в открывшемся окне. Кнопка «Скачать» открывает его повторно.');
    if(catalogShown!==checkJob.id){catalogShown=checkJob.id;showCatalog(checkJob,beginCatalogQueue);}
  }
  else if(checkJob?.status==='awaiting')checkResult('Можно скачать',`${checkJob.title} · ${checkJob.actual_quality?checkJob.actual_quality+'p · ':''}${checkJob.extension?.toUpperCase()} · ${bytes(checkJob.total)}. Сохранение и сборка выполняются на вашем устройстве.`,'good');
  else if(checkJob?.status==='error'){
    checkResult('Скачать нельзя',errorText(checkJob.error),'bad');
    reportError(checkJob.error,{title:'Проверка видео завершилась ошибкой',jobTitle:checkJob.title,key:'job:'+checkJob.id});
  }
  else if(checking||checkJob?.status==='analyzing')checkResult('Проверяем ссылку…','Проверяем доступность видео, качество и ограничения источника.');
  else if(checkJob?.status==='paused')checkResult('Загрузка приостановлена','Продолжите её кнопкой в списке ниже.');
  updateButton();
}
async function scheduleCheck(delay=0,force=false){
  const value=$('#url').value.trim();
  const key=value+'|'+$('#quality').value;
  if(!force&&key===checkKey&&(checking||['analyzing','awaiting','selection','error'].includes(checkJob?.status)))return;
  checkKey=key;
  clearTimeout(timer);const generation=++sequence;
  closeCatalog();
  playerData=null;
  if(!batchActive){queueState=null;primaryJobId=null;renderQueue();}
  const previous=checkJob;checkJob=null;checking=false;
  if(previous&&['analyzing','awaiting','error','selection'].includes(previous.status))api(`jobs/${previous.id}/discard`,'POST').catch(()=>{});
  if(!value){checkResult('Можно ли скачать?','Вставьте ссылку — проверка начнётся автоматически.');updateButton();return;}
  try{const url=new URL(value);if(!['http:','https:'].includes(url.protocol))throw new Error();}
  catch{checkResult('Скачать нельзя','Нужна полная ссылка http:// или https:// на одно видео.','bad');updateButton();return;}
  if(capabilityError){describeCheck();return;}
  checking=true;describeCheck();
  const analyze=async()=>{
    try{
      const job=await api('jobs','POST',{url:value,quality:Number($('#quality').value)});
      if(generation!==sequence){await api(`jobs/${job.id}/discard`,'POST');return;}
      checkJob=job;await refresh();
    }catch(e){if(generation===sequence){checking=false;checkResult('Проверка не завершена',errorText(e),'bad');notify(e,{title:'Не удалось проверить ссылку'});updateButton();}}
  };
  if(delay>0)timer=setTimeout(analyze,delay);else await analyze();
}
function actionButton(text,handler,style='secondary'){
  const b=document.createElement('button');b.type='button';b.textContent=text;b.className=style;
  b.onclick=async()=>{b.disabled=true;try{await handler();await refresh();}catch(e){notify(e,{retry:handler});}finally{b.disabled=false;}};return b;
}
async function runJob(job,handle){
  if(queueState?.errorId===job.id){queueState.error='';queueState.errorId=null;}
  forgetError('job:'+job.id);
  primaryJobId=job.id;
  live.set(job.id,{...job,status:'client_downloading',phase:'download'});render();
  busy=true;updateButton();checkResult('Идёт загрузка','Пауза и отмена доступны ниже. Данные сохраняются на вашем устройстве.');
  let wake;
  try{
    wake=await navigator.wakeLock?.request('screen').catch(()=>null);
    const result=await download({...job,destinationLabel:handle?(job.destinationLabel||destination.label):null},handle,record=>{
      if(record)live.set(job.id,{...record});
      if(performance.now()-lastRender>200){render();lastRender=performance.now();}
    });
    if(result?.status==='completed'){
      $('#url').value='';checkJob=null;sequence++;checking=false;
      checkResult('Готово',result.location,'good');
    }else if(result?.status==='paused'){
      if(checkJob?.id===job.id)checkJob={...checkJob,status:'paused'};
      checkResult('Загрузка на паузе','Полученные части сохранены на этом устройстве. Продолжите скачивание ниже.');
      if(result.error&&!batchActive)notify(result.error,{title:'Загрузка прервана',jobTitle:job.title,key:'job:'+job.id,retry:()=>runQueue([job.id])});
    }else if(result?.status==='error'){
      if(checkJob?.id===job.id)checkJob={...checkJob,status:'error',error:result.error};
      checkResult('Не удалось завершить скачивание',result.error,'bad');
      if(!batchActive)notify(result.error,{title:'Ошибка скачивания',jobTitle:job.title,key:'job:'+job.id,retry:()=>runQueue([job.id])});
    }else if(result?.status==='cancelled'){
      checkJob=null;checkResult('Загрузка отменена','Можно вставить другую ссылку.');
    }
    if(!result)throw new Error('Загрузчик завершился без результата. Повторите попытку.');
    return result;
  }catch(e){
    checkResult('Не удалось завершить скачивание',errorText(e),'bad');
    if(!batchActive)notify(e,{title:'Ошибка скачивания',jobTitle:job.title,key:'job:'+job.id,retry:()=>runQueue([job.id])});
    throw e;
  }finally{await wake?.release().catch(()=>{});busy=false;live.delete(job.id);await refresh();updateButton();}
}
function renderQueue(){
  const pending=resumableQueue(serverJobs,localJobs,[...live.values()]);
  const records=new Map(serverJobs.map(job=>[job.id,job]));
  for(const job of localJobs)records.set(job.id,{...records.get(job.id),...job});
  for(const [id,job] of live)records.set(id,{...records.get(id),...job});
  const ids=queueState?.ids||pending.map(job=>job.id);
  const jobs=ids.map(id=>records.get(id)).filter(Boolean);
  const completed=jobs.filter(job=>job.status==='completed').length;
  const failed=jobs.filter(job=>job.status==='error').length;
  const errors=Math.max(failed,queueState?.error?1:0);
  const cancelled=ids.filter(id=>cancelledHere.has(id)||records.get(id)?.status==='cancelled').length;
  const remaining=jobs.filter(job=>!['completed','cancelled'].includes(job.status));
  const current=records.get(queueCurrent);
  $('#queue-panel').hidden=!batchActive&&(!queueState||!!queueState.error);
  $('#history-resume').hidden=batchActive||!pending.length;
  $('#history-resume-row').hidden=$('#history-resume').hidden;
  $('#history-resume').disabled=busy||!!capabilityError;
  $('#queue-resume').hidden=batchActive||!pending.length;
  $('#queue-resume').disabled=busy||!!capabilityError;
  $('#queue-pause').hidden=!batchActive;
  $('#queue-panel').dataset.state=batchActive?'active':completed===ids.length&&ids.length?'completed':errors?'error':'paused';
  $('#queue-text').textContent=batchActive?`Идёт загрузка очереди · видео ${(queueState?.index||0)+1} из ${ids.length}`:completed===ids.length&&ids.length?'Очередь завершена':errors?'Очередь остановлена из-за ошибки':'Очередь на паузе';
  const progress=$('#queue-progress');progress.max=Math.max(1,ids.length);
  let fraction=0;
  if(current?.status==='client_downloading')fraction=current.phase==='mux' ? .9+.09*(current.muxProgress||0) : current.total?Math.min(.9,.9*current.downloaded/current.total):0;
  progress.value=Math.min(ids.length,completed+fraction);
  const stage=current?.phase==='analyze'?'Проверка доступности':current?.phase==='mux'?'Сборка на устройстве':current?.phase==='save'?'Сохранение':'Скачивание';
  $('#queue-detail').textContent=`Готово: ${completed} из ${ids.length}. Не завершено: ${remaining.length}. Ошибок: ${errors}. Отменено: ${cancelled}.`+(batchActive&&current?`\n${stage}: ${current.title}`:pending.length?'\nОчередь сохранена в сессии. Для продолжения нажмите «Скачать очередь».':'\nМесто сохранения указано в карточках видео.');
}
async function beginCatalogQueue(jobs){
  checkJob=null;checking=false;checkKey='';sequence++;
  try{await refresh();await runQueue(jobs.map(job=>job.id));}catch(e){notify(e,{title:'Не удалось запустить очередь'});}
}
async function prepareQueued(id){
  let job=(await api('jobs')).find(item=>item.id===id);
  if(!job){if(cancelledHere.has(id))return null;throw new Error('Задание не найдено или истёк срок его хранения. Вставьте ссылку заново.');}
  if(['cancelled','completed'].includes(job.status))return null;
  if(job.phase==='analyze'&&job.status!=='analyzing')job=await api(`jobs/${id}/start`,'POST');
  const deadline=Date.now()+180000;
  while(job.status==='analyzing'||job.status==='pausing'){
    if(queueStopped)return null;
    if(Date.now()>deadline){await api(`jobs/${id}/pause`,'POST').catch(()=>{});throw new Error('Проверка серии заняла слишком долго. Задание приостановлено.');}
    await new Promise(resolve=>setTimeout(resolve,1000));
    job=(await api('jobs')).find(item=>item.id===id);
    if(!job){if(queueStopped||cancelledHere.has(id))return null;throw new Error('Задание исчезло во время проверки. Возможно, оно отменено в другой вкладке или истёк срок хранения.');}
    await refresh();
  }
  if(job.status==='error')throw new Error(errorText(job.error));
  if(job.status==='selection'){
    queueStopped=true;checkJob=job;catalogShown=job.id;
    showCatalog(job,beginCatalogQueue);return null;
  }
  if(job.phase==='analyze'||['cancelled','cancelling','completed'].includes(job.status))return null;
  return job;
}
async function runQueue(ids){
  if(busy||batchActive)throw new Error('Сначала приостановите текущую загрузку или очередь.');
  const process=async lock=>{
    if(lock===null)throw new Error('Очередь уже запущена в другой вкладке.');
    if(!Array.isArray(ids)||!ids.length)throw new Error('В очереди нет видео для скачивания. Выберите серии заново.');
    batchActive=true;queueStopped=false;queueState={ids:[...ids],index:0};updateButton();renderQueue();
    let failures=0,issue=null;
    try{
      for(let i=0;i<ids.length&&!queueStopped;i++){
        queueCurrent=ids[i];
        queueState.index=i;primaryJobId=ids[i];forgetError('job:'+ids[i]);render();
        try{
          const job=await prepareQueued(ids[i]);if(!job||queueStopped)continue;
          const local=await getLocal(job.id);
          const handle=local?.handle||await destinationFile(job);
          const result=await runJob({...job,destinationLabel:local?.destinationLabel||destination.label},handle);
          if(result?.status==='paused'){queueStopped=true;if(result.error)throw new Error(result.error);}
          if(result?.status==='error')throw new Error(errorText(result.error));
        }catch(e){
          failures++;queueStopped=true;message(errorText(e));
          queueState.error=errorText(e);queueState.errorId=ids[i];
          checkResult('Очередь приостановлена из-за ошибки',errorText(e),'bad');
          const failedJob=serverJobs.find(job=>job.id===ids[i]);
          issue={error:e,id:ids[i],title:failedJob?.title||'Выбранное видео',retryIds:ids.slice(i),analysisFailure:failedJob?.phase==='analyze'};
        }
      }
      if(!queueStopped&&!failures){$('#url').value='';checkKey='';checkResult('Готово','Выбранные серии обработаны. Место сохранения указано в карточках загрузок.','good');}
      else if(failures)message('Очередь приостановлена из-за ошибки. Остальные серии не запускались; задания сохранены.');
    }finally{
      batchActive=false;queueCurrent=null;await refresh();updateButton();renderQueue();
      if(issue)reportError(issue.error,{title:'Не удалось скачать видео. Очередь приостановлена',jobTitle:issue.title,key:'job:'+issue.id,retry:issue.analysisFailure?null:()=>runQueue(issue.retryIds)});
    }
  };
  if(navigator.locks)return navigator.locks.request('dav-catalog-queue',{ifAvailable:true},process);
  return process(undefined);
}
function resumeHistoryQueue(){return runQueue(resumableQueue(serverJobs,localJobs,[...live.values()]).map(job=>job.id)).catch(e=>notify(e,{title:'Не удалось продолжить очередь'}));}
$('#queue-resume').onclick=resumeHistoryQueue;
$('#history-resume').onclick=resumeHistoryQueue;
$('#queue-pause').onclick=async()=>{
  queueStopped=true;
  checkResult('Очередь на паузе','Задания сохранены. Для продолжения нажмите «Скачать очередь».');
  try{if(queueCurrent){
    if(running.has(queueCurrent))await stop(queueCurrent);
    else await api(`jobs/${queueCurrent}/pause`,'POST');
  }
  await refresh();}catch(e){notify(e,{title:'Не удалось приостановить очередь'});}
};
function render(){
  renderPlayer();
  renderQueue();
  const joined=new Map(serverJobs.filter(j=>j.status!=='selection'&&j.history_visible!==false&&(j.id!==checkJob?.id||['error','cancelled','completed'].includes(j.status))).map(j=>[j.id,j]));
  for(const local of localJobs){
    const remote=serverJobs.find(job=>job.id===local.id);
    joined.set(local.id,{...remote,...local,error:local.error||remote?.error||'',
      ...(remote?.status==='cancelled'?{status:'cancelled',finished_at:remote.finished_at||local.finished_at}:{})});
  }
  for(const [id,record] of live)joined.set(id,record);
  const jobs=[...joined.values()].sort((a,b)=>b.created_at-a.created_at);
  const current=jobs.find(job=>job.id===primaryJobId&&!['error','cancelled','completed'].includes(job.status))||jobs.find(job=>job.status==='client_downloading');
  primaryJobId=current?.id||null;
  const top=$('#current-download');top.replaceChildren();top.hidden=!current;
  $('#count').textContent=jobs.length;
  if(!jobs.length){$('#jobs').innerHTML=empty;return;}
  const fragment=document.createDocumentFragment();
  for(const job of jobs){
    const node=$('#job-template').content.cloneNode(true);
    node.querySelector('.job').dataset.jobId=job.id;
    node.querySelector('h3').textContent=job.title;
    const date=historyDate(job),dateNode=node.querySelector('.job-date');dateNode.textContent=date.text;if(date.iso)dateNode.dateTime=date.iso;
    node.querySelector('.job-meta').textContent=[job.actual_quality?job.actual_quality+'p':null,job.extension?.toUpperCase()].filter(Boolean).join(' · ');
    node.querySelector('.badge').textContent=labels[job.status]||job.status;
    const progress=node.querySelector('progress');
    if(job.status==='completed')progress.value=100;
    else if(job.status==='analyzing')progress.removeAttribute('value');
    else if(job.phase==='mux')progress.value=90+9*(job.muxProgress||0);
    else if(job.total)progress.value=Math.min(90,90*job.downloaded/job.total);
    else if(running.has(job.id))progress.removeAttribute('value');
    node.querySelector('.progress-detail').textContent=job.status==='cancelled'?'Загрузка отменена':job.phase==='analyze'?(job.status==='error'?'Проверка завершилась ошибкой':job.status==='pending'?'Ожидает своей очереди':job.status==='paused'?'Проверка приостановлена':'Проверяем доступность и параметры видео…'):[bytes(job.downloaded)+' / '+bytes(job.total),job.speed?bytes(job.speed)+'/с':null,job.phase==='mux'?'Завершаем файл на устройстве':null].filter(Boolean).join(' · ');
    node.querySelector('.job-location').textContent=job.location||'Сохранение: папка, выбранная в браузере. Сайт не видит её полный путь.';
    node.querySelector('.job-error').textContent=job.status==='cancelled'?'Отменено пользователем'+(job.error?'\nНе скачалось из-за ошибки ('+errorText(job.error)+')':''):job.status==='error'?'Не скачалось из-за ошибки ('+errorText(job.error)+')':job.error||'';
    node.querySelector('.job').dataset.status=job.status;
    const actions=node.querySelector('.actions');
    if(running.has(job.id))actions.append(actionButton('Пауза',()=>stop(job.id)));
    else if(job.status==='analyzing')actions.append(actionButton('Пауза',()=>api(`jobs/${job.id}/pause`,'POST')));
    else if(!['cancelled','completed','cancelling'].includes(job.status)&&!job.data_expired&&!job.localExpired&&(job.phase==='analyze'||['pending','awaiting'].includes(job.status)))actions.append(actionButton('Скачать',()=>runQueue([job.id]),''));
    else if(['paused','error','client_downloading'].includes(job.status)&&job.mode==='browser'&&!job.localExpired&&!job.data_expired)actions.append(actionButton('Продолжить',async()=>{
      if(busy)throw new Error('Сначала приостановите текущую загрузку.');
      const record=await getLocal(job.id);let handle=record?.handle;
      if(handle?.requestPermission&&await handle.queryPermission({mode:'readwrite'})!=='granted')await handle.requestPermission({mode:'readwrite'});
      await runJob({...job,status:'paused',destinationLabel:record?.destinationLabel||'выбранная ранее папка'},handle);
    },''));
    if(job.status==='completed'&&!job.handle&&!job.localExpired)actions.append(actionButton('Сохранить файл',async()=>{const record=await getLocal(job.id);await exportFile(record);},'secondary'));
    if(job.mode!=='browser'&&job.status==='ready'){
      const a=document.createElement('a');a.href=`/api/jobs/${job.id}/file`;a.download='';a.textContent='Скачать ранее подготовленный файл';actions.append(a);
    }
    if(['completed','cancelled','error'].includes(job.status))actions.append(actionButton('Убрать из списка',()=>forgetJob(job),'cancel'));
    if(!['completed','cancelled','cancelling'].includes(job.status))actions.append(actionButton('Отменить',async()=>{cancelledHere.add(job.id);try{await stop(job.id,true);}catch(e){cancelledHere.delete(job.id);throw e;}},'cancel'));
    if(job.expires_at&&!['completed','cancelled'].includes(job.status))node.querySelector('.expiry').textContent='Продолжение доступно до '+historyClock.format(new Date(job.expires_at*1000));
    if(job.id===primaryJobId)top.append(node);else fragment.append(node);
  }
  if(fragment.childNodes.length)$('#jobs').replaceChildren(fragment);
  else $('#jobs').innerHTML='<div class="empty"><p>Текущая загрузка показана выше. Других заданий пока нет.</p></div>';
}
let refreshing=false;
function backgroundStatus(text=''){
  const status=$('#connection-status');status.textContent=text;status.hidden=!text;
}
async function refresh(){
  if(refreshing)return;refreshing=true;
  let connected=false;
  try{
    const freshJobs=await api('jobs');connected=true;
    const freshLocal=await allLocal();serverJobs=freshJobs;localJobs=freshLocal;
    forgetError('refresh:network');forgetError('refresh:timeout');
    if(checkJob){const job=serverJobs.find(j=>j.id===checkJob.id);if(job){checkJob=job;checking=job.status==='analyzing';}}
    // A tab/process that died cannot keep a local task marked running forever.
    for(const local of localJobs){
      if(!running.has(local.id)&&local.status==='client_downloading'){
        const remote=serverJobs.find(j=>j.id===local.id);
        if(remote?.status==='paused'){local.status='paused';await saveLocal(local);}
      }
      const expired=local.status==='completed'?local.completedAt<Date.now()-7*86400000:local.expires_at&&local.expires_at<Date.now()/1000;
      if(expired&&!running.has(local.id)&&!local.localExpired){await removeLocalFiles(local.id);local.localExpired=true;local.data_expired=true;if(!['completed','cancelled'].includes(local.status)){local.error=local.error||'Срок хранения временных данных истёк. Для нового скачивания вставьте ссылку заново.';local.status='error';}await saveLocal(local);}
    }
    render();if(!busy)describeCheck();
    backgroundStatus();
  }catch(e){backgroundStatus(connected?'Не удалось обновить локальную историю. Повторяем обновление…':'Восстанавливаем соединение…');}
  finally{refreshing=false;}
}
const cookie=preferencesAllowed()?document.cookie.split('; ').find(v=>v.startsWith('dav_quality='))?.split('=')[1]:null;
if([...$('#quality').options].some(o=>o.value===cookie))$('#quality').value=cookie;
function rememberQuality(){
  if(preferencesAllowed())document.cookie=`dav_quality=${$('#quality').value}; Max-Age=34560000; Path=/; SameSite=Lax${location.protocol==='https:'?'; Secure':''}`;
  else removePreferenceCookie('dav_quality');
}
$('#quality').onchange=()=>{rememberQuality();scheduleCheck();};
window.addEventListener('dav-consent-change',rememberQuality);
$('#url').addEventListener('input',event=>scheduleCheck(event.inputType==='insertText'&&event.data?.length===1?350:0));
$('#url').addEventListener('change',()=>scheduleCheck());
$('#analysis-clear').addEventListener('click',()=>{
  if(busy||batchActive)return;
  clearTimeout(timer);sequence++;
  const previous=checkJob;
  checkJob=null;checking=false;checkKey='';
  playerData=null;
  queueState=null;primaryJobId=null;renderQueue();
  if(previous&&(['analyzing','awaiting','selection'].includes(previous.status)||previous.phase==='analyze'))api(`jobs/${previous.id}/discard`,'POST').catch(()=>{});
  closeCatalog();
  $('#url').value='';message('');
  checkResult('Можно ли скачать?','Вставьте ссылку — проверка начнётся автоматически.');
  $('#check-result').hidden=true;
  updateButton();$('#url').focus();
});
$('#new-job').onsubmit=async event=>{
  event.preventDefault();if(busy||batchActive||!checkJob||capabilityError)return;
  if(checkJob.status==='selection'){showCatalog(checkJob,beginCatalogQueue);return;}
  if(checkJob.status!=='awaiting')return;
  try{forgetError('job:'+checkJob.id);const handle=await destinationFile(checkJob);await runJob(checkJob,handle);}catch(e){if(e?.name!=='AbortError')notify(e,{title:'Не удалось начать скачивание',key:checkJob?'job:'+checkJob.id:null});}
};
window.addEventListener('online',refresh);
window.addEventListener('offline',()=>backgroundStatus('Восстанавливаем соединение…'));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
try{await supportsStorage();}catch(e){capabilityError=errorText(e);notify(e,{title:'Браузер не поддерживает сохранение'});}
try{await initDestination();}catch(e){notify(e,{title:'Не удалось загрузить настройку папки'});}
await refresh();setInterval(refresh,2000);updateButton();
