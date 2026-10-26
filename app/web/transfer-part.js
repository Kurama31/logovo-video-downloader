// One streaming request per media resource; Range is only a source-driven fallback/resume.
const OVERLAP=64*1024;
export async function receivePart({handle,state,expectedSize=null,signal,request,onBytes,checkpoint}){
  let existing=await handle.getFile(),offset=existing.size,total=state.total??expectedSize;
  let lastLength=state.lastLength||offset,restarts=0,requests=0;
  const reset=async()=>{
    const writer=await handle.createWritable();await writer.close();
    onBytes(-offset);offset=0;total=expectedSize;state.validator=null;state.complete=false;state.size=0;state.total=total;
  };
  // Bytes from a previous run are reusable only with a version validator.
  if(offset&&!state.validator)await reset();
  while(true){
    signal.throwIfAborted();
    if(++requests>20000)throw new Error('Источник отдаёт слишком много мелких порций. Загрузка остановлена.');
    const previousOffset=offset,previousValidator=state.validator;
    const overlap=offset&&!previousValidator?Math.min(OVERLAP,Math.max(1,Math.floor(lastLength/2)),offset):0;
    const start=offset-overlap,headers={};
    if(offset){headers.Range=`bytes=${start}-`;if(previousValidator)headers['If-Range']=previousValidator;}
    const response=await request(headers,signal);
    if(response.status===409&&response.headers.get('X-DAV-Restart')==='1'){
      await response.body?.cancel();
      if(restarts++>=1)throw new Error('Источник повторно изменил видео. Вставьте ссылку заново.');
      await reset();continue;
    }
    if(!response.ok){
      const problem=await response.json().catch(()=>({}));
      throw new Error(problem.detail||'Источник прервал передачу. Попробуйте продолжить позже.');
    }
    const match=response.headers.get('content-range')?.match(/^bytes (\d+)-(\d+)\/(\d+)$/);
    const range=match?match.slice(1).map(Number):null;
    const lengthText=response.headers.get('content-length');
    const length=lengthText!==null?Number(lengthText):null;
    const etag=response.headers.get('etag');
    const validator=(etag&&!etag.startsWith('W/')?etag:response.headers.get('last-modified'))||null;
    if(response.status===206&&(!range||!range.every(Number.isSafeInteger)||range[0]!==start||range[1]<range[0]||range[1]>=range[2])){
      await response.body?.cancel();throw new Error('Источник вернул некорректный диапазон видео.');
    }
    // A server may ignore Range / fail If-Range by returning the full new resource.
    if(response.status===200&&offset){
      if(restarts++>=1){await response.body?.cancel();throw new Error('Источник не позволяет продолжить видео. Начните скачивание заново.');}
      await reset();
    }else if(offset&&((previousValidator&&validator&&previousValidator!==validator)||(range&&total!=null&&range[2]!==total))){
      await response.body?.cancel();
      if(restarts++>=1)throw new Error('Источник повторно изменил видео. Вставьте ссылку заново.');
      await reset();continue;
    }
    total=range?range[2]:length??expectedSize;
    if(total!=null&&(!Number.isSafeInteger(total)||total<=0)){
      await response.body?.cancel();throw new Error('Источник передал некорректный размер видео.');
    }
    state.total=total;state.complete=false;
    state.validator=validator||(response.status===206?previousValidator:null);
    existing=await handle.getFile();
    const writer=await handle.createWritable({keepExistingData:true});await writer.seek(offset);
    const reader=response.body.getReader();let received=0,closed=false;
    const bodyStart=response.status===200?0:start;
    try{
      while(true){
        signal.throwIfAborted();
        const {done,value}=await reader.read();if(done)break;
        const position=bodyStart+received;received+=value.length;
        if((range&&received>range[1]-range[0]+1)||(!range&&total!=null&&received>total))throw new Error('Источник передал больше данных, чем заявлено.');
        const skip=Math.max(0,Math.min(value.length,offset-position));
        if(skip){
          const saved=new Uint8Array(await existing.slice(position,position+skip).arrayBuffer());
          if(saved.length!==skip||saved.some((byte,i)=>byte!==value[i]))throw new Error('Данные видео изменились на стыке порций. Продолжение остановлено; повторите загрузку.');
        }
        if(skip<value.length){
          const bytes=value.subarray(skip);await writer.write(bytes);offset+=bytes.length;onBytes(bytes.length);
        }
      }
      await writer.close();closed=true;
      if((range&&received!==range[1]-range[0]+1)||(!range&&total!=null&&received!==total)){
        const error=new Error('Соединение прервано до получения фрагмента целиком.');error.kind='network';throw error;
      }
    }catch(error){
      await reader.cancel().catch(()=>{});
      if(!closed)await writer.close().catch(()=>{});
      state.size=offset;throw error;
    }finally{reader.releaseLock();}
    state.size=offset;lastLength=received;state.lastLength=lastLength;
    state.complete=!range||offset===total;
    await checkpoint();
    if(state.complete)return;
    if(offset<=previousOffset)throw new Error('Источник не отдаёт следующую порцию видео. Продолжение остановлено.');
  }
}
