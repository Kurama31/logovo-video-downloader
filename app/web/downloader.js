import {directory, getLocal, saveLocal, removeLocalFiles} from './local-store.js';
import {remux} from './local-mux.js';
import {receivePart} from './transfer-part.js';

export const running=new Map();
export function safeName(title,ext){return (title.replace(/[\x00-\x1f\x7f/\\:*?"<>|]/g,'_').slice(0,140).replace(/[. ]+$/,'')||'video')+'.'+ext;}
export async function api(path,method='GET',body){
  const controller=new AbortController();let timedOut=false;
  const timeout=setTimeout(()=>{timedOut=true;controller.abort();},20000);
  try{
    const response=await fetch('/api/'+path,{method,credentials:'same-origin',signal:controller.signal,headers:{'Content-Type':'application/json','X-Requested-With':'DAV'},body:body?JSON.stringify(body):undefined});
    let data;
    try{data=await response.json();}catch{
      const error=new Error(`Сервер вернул непонятный ответ (HTTP ${response.status}). Повторите попытку.`);error.kind='response';throw error;
    }
    if(!response.ok){const error=new Error(typeof data?.detail==='string'?data.detail:`Не удалось выполнить запрос (HTTP ${response.status}).`);error.kind='response';error.httpStatus=response.status;throw error;}
    return data;
  }catch(error){
    if(timedOut){const problem=new Error('Сервер не ответил за 20 секунд. Проверьте соединение и повторите попытку.');problem.kind='timeout';throw problem;}
    if(error instanceof TypeError){const problem=new Error('Нет соединения с сервером. Проверьте сеть и повторите попытку.');problem.kind='network';throw problem;}
    throw error;
  }finally{clearTimeout(timeout);}
}
function abortError(){return new DOMException('Приостановлено','AbortError');}
export async function stop(id,cancel=false){
  const active=running.get(id);
  if(active){active.cancel=cancel;active.controller.abort();await active.done;}
  const remote=await api(`jobs/${id}/${cancel?'cancel':'pause'}`,'POST');
  if(cancel){
    await removeLocalFiles(id);
    const local=await getLocal(id);
    const record={...remote,...local,status:'cancelled',history_visible:true,data_expired:true,expires_at:null,
      finished_at:remote.finished_at||Date.now()/1000,error:local?.error||remote.error||''};
    await saveLocal(record);
  }
}
export async function exportFile(record){
  const dir=await directory(record.id);
  const file=await (await dir.getFileHandle('result')).getFile();
  const url=URL.createObjectURL(file);
  const link=document.createElement('a');link.href=url;link.download=record.outputName;link.textContent='Сохранить файл';
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
  record.location=`Передан в загрузки браузера: «${record.outputName}». Фактическую папку смотрите в загрузках браузера.`;
  record.exportRequested=true;await saveLocal(record);
}

export async function download(job,chosenHandle,onChange){
  if(navigator.locks){
    return navigator.locks.request('dav-download-'+job.id,{ifAvailable:true},async lock=>{
      if(!lock)throw new Error('Это видео уже скачивается в другой вкладке.');
      const remote=(await api('jobs')).find(j=>j.id===job.id);
      if(remote?.status==='client_downloading')await api(`jobs/${job.id}/pause`,'POST');
      return executeDownload(job,chosenHandle,onChange);
    });
  }
  return executeDownload(job,chosenHandle,onChange);
}

async function executeDownload(job,chosenHandle,onChange){
  if(running.has(job.id))return running.get(job.id).done;
  const controller=new AbortController();
  const active={controller,cancel:false,done:null};
  running.set(job.id,active);
  active.done=(async()=>{
    let record=await getLocal(job.id)||{...job,parts:{},downloaded:0};
    if(chosenHandle)record.handle=chosenHandle;
    if(job.destinationLabel)record.destinationLabel=job.destinationLabel;
    record.status='client_downloading';record.error='';record.phase='download';record.history_visible=true;
    record.finished_at=null;
    record.outputName=safeName(job.title,job.extension);
    record.location=record.handle?`Куда скачать: ${record.destinationLabel||'выбранная папка'} / ${record.handle.name}. Полный путь браузер не раскрывает.`:'Куда скачать: папка загрузок браузера. Полный путь недоступен сайту.';
    await saveLocal(record);onChange();
    let lastReport=0,lastBytes=record.downloaded,lastTime=performance.now(),reporting=false,pendingReport=Promise.resolve();
    const report=async(force=false)=>{
      if(reporting){if(!force)return;await pendingReport;}
      if(!force&&performance.now()-lastReport<1500)return;
      reporting=true;lastReport=performance.now();
      pendingReport=(async()=>{try{await saveLocal(record);await api(`jobs/${job.id}/client/progress`,'POST',{downloaded:record.downloaded,total:record.total||null,phase:record.phase,speed:record.speed||null});}
      finally{reporting=false;onChange();}})();
      await pendingReport;
    };
    let heartbeat;
    try{
      await api(`jobs/${job.id}/${job.status==='awaiting'?'start':'resume'}`,'POST');
      const plan=await api(`jobs/${job.id}/client/plan`);
      if(record.signature&&record.signature!==plan.signature)throw new Error('Потоки источника изменились. Отмените задание и вставьте ссылку заново.');
      record.signature=plan.signature;record.total=job.total||null;
      const estimate=await navigator.storage.estimate();
      if(record.total&&estimate.quota-estimate.usage<record.total*2)throw new Error('Недостаточно свободного места в локальном хранилище браузера.');
      await navigator.storage.persist?.().catch(()=>false);
      const dir=await directory(job.id);
      record.downloaded=0;
      for await(const [name,handle] of dir.entries())if(name.startsWith('part-'))record.downloaded+=(await handle.getFile()).size;
      lastBytes=record.downloaded;
      heartbeat=setInterval(()=>report(true).catch(()=>{}),15000);
      const blobs=[];
      for(let t=0;t<plan.tracks.length;t++){
        const files=[];
        for(let p=0;p<plan.tracks[t].parts.length;p++){
          controller.signal.throwIfAborted();
          const key=`${t}-${p}`;
          const state=record.parts[key]||(record.parts[key]={});
          const handle=await dir.getFileHandle('part-'+key,{create:true});
          let existing=await handle.getFile();
          if(state.complete&&existing.size===state.size){files.push(existing);continue;}
          await receivePart({handle,state,expectedSize:plan.tracks[t].parts[p].size??null,signal:controller.signal,
            request:(headers,signal)=>fetch(`/api/jobs/${job.id}/client/tracks/${t}/parts/${p}`,{headers,signal}),
            onBytes:delta=>{
              record.downloaded=Math.max(0,record.downloaded+delta);
              if(record.downloaded>plan.max_bytes)throw new Error('Превышен допустимый размер видео.');
              const now=performance.now();
              if(delta<0){lastBytes=record.downloaded;lastTime=now;record.speed=null;}
              else record.speed=(record.downloaded-lastBytes)/Math.max((now-lastTime)/1000,.01);
              if(now-lastTime>1000){lastBytes=record.downloaded;lastTime=now;}
              onChange(record);
            },checkpoint:()=>report()});
          await saveLocal(record);
          files.push(await handle.getFile());
        }
        blobs.push(new Blob(files));
      }
      controller.signal.throwIfAborted();record.speed=null;
      const destination=record.handle||await dir.getFileHandle('result',{create:true});
      if(plan.remux){
        record.phase='mux';await report(true);
        await remux(blobs,job.extension,destination,controller.signal,value=>{record.muxProgress=value;onChange(record);});
      }else{
        record.phase='save';await report(true);
        const writer=await destination.createWritable();
        const reader=blobs[0].stream().getReader();
        try{while(true){controller.signal.throwIfAborted();const {done,value}=await reader.read();if(done)break;await writer.write(value);}await writer.close();}
        catch(e){await writer.abort().catch(()=>{});throw e;}finally{reader.releaseLock();}
      }
      record.phase='complete';await report(true);record.status='completed';record.completedAt=Date.now();record.finished_at=record.completedAt/1000;record.expires_at=null;
      record.location=record.handle?`Сохранено: ${record.destinationLabel||'выбранная папка'} / ${record.handle.name}. Полный путь браузер не раскрывает.`:`Готовый файл «${record.outputName}» находится на этом устройстве в данных сайта.`;
      await saveLocal(record);
      if(!record.handle)await exportFile(record);
      // Raw tracks are no longer needed. Keep fallback final file available for export.
      for await(const [name] of dir.entries())if(name.startsWith('part-'))await dir.removeEntry(name);
      return record;
    }catch(problem){
      const e=problem instanceof Error?problem:new Error(typeof problem==='string'?problem:'Произошла неизвестная ошибка при скачивании. Повторите попытку.');
      const disconnected=!navigator.onLine||['network','timeout'].includes(e.kind)||(e instanceof TypeError&&/fetch|network/i.test(e.message));
      record.status=active.cancel?'cancelled':e.name==='AbortError'||disconnected?'paused':'error';
      record.finished_at=['error','cancelled'].includes(record.status)?Date.now()/1000:null;
      record.speed=null;record.expires_at=Date.now()/1000+7*86400;
      record.error=e.name==='AbortError'?'':disconnected?'Соединение прервано. Полученные части находятся на этом устройстве; нажмите «Продолжить».':e.name==='QuotaExceededError'?'Недостаточно свободного места на устройстве.':e.message||'Неизвестная ошибка при скачивании. Повторите попытку.';
      await api(`jobs/${job.id}/pause`,'POST').catch(()=>{});
      await saveLocal(record);return record;
    }finally{
      clearInterval(heartbeat);running.delete(job.id);onChange();
    }
  })();
  return active.done;
}
