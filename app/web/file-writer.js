let shared=null,sequence=0;
export const needsWorkerWriter=()=>typeof globalThis.FileSystemFileHandle?.prototype?.createWritable!=='function';
function workerClient(){
  if(shared)return shared;
  if(typeof Worker!=='function')throw new Error('В этом браузере недоступна совместимая запись файлов. Откройте сайт в обычной вкладке Safari и разрешите хранение данных сайта.');
  const worker=new Worker(new URL('./opfs-writer.worker.js',import.meta.url),{type:'module'});
  const pending=new Map();let failure=null;
  const client={request(message,transfer=[]){
    if(failure)return Promise.reject(failure);
    const id=++sequence;
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>fail(new Error('Запись на устройство не отвечает. Полученные части можно попробовать продолжить после обновления страницы.')),30000);
      pending.set(id,{resolve,reject,timer});
      try{worker.postMessage({...message,id},transfer);}catch(error){clearTimeout(timer);pending.delete(id);reject(error);}
    });
  }};
  function fail(error){
    failure=error;
    worker.terminate();if(shared===client)shared=null;
    for(const item of pending.values()){clearTimeout(item.timer);item.reject(error);}
    pending.clear();
  }
  worker.onmessage=({data})=>{
    const item=pending.get(data.id);if(!item)return;
    pending.delete(data.id);clearTimeout(item.timer);
    if(data.error){const error=new Error(data.error.message);error.name=data.error.name;item.reject(error);}else item.resolve(data.result);
  };
  worker.onerror=event=>{event.preventDefault();fail(new Error('Не удалось запустить запись файлов на устройстве. Обновите страницу и попробуйте продолжить загрузку.'));};
  worker.onmessageerror=()=>fail(new Error('Не удалось передать данные для записи на устройство.'));
  shared=client;return client;
}
export async function createFileWriter(handle,options={}){
  if(typeof handle.createWritable==='function')return handle.createWritable(options);
  const root=await navigator.storage.getDirectory(),path=await root.resolve(handle);
  if(!path?.length)throw new Error('Браузер разрешает совместимую запись только во временное хранилище сайта. Используйте папку загрузок браузера.');
  const client=workerClient(),writerId=++sequence;
  const request=(op,extra={},transfer=[])=>client.request({op,writerId,...extra},transfer);
  await request('open',{path,keepExistingData:!!options.keepExistingData});
  let released=false;
  async function release(op='close'){
    if(released)return;released=true;
    await request(op);
  }
  async function write(value){
    try{
      let data=value,position;
      if(value&&typeof value==='object'&&!(value instanceof Blob)&&'type' in value){
        if(value.type==='seek')return await request('seek',{position:value.position});
        if(value.type==='truncate')return await request('truncate',{size:value.size});
        if(value.type!=='write')throw new TypeError('Unknown write command');
        data=value.data;position=value.position;
      }
      if(data instanceof Blob)data=await data.arrayBuffer();
      if(typeof data==='string')data=new TextEncoder().encode(data);
      const view=data instanceof ArrayBuffer?new Uint8Array(data):ArrayBuffer.isView(data)?new Uint8Array(data.buffer,data.byteOffset,data.byteLength):null;
      if(!view)throw new TypeError('Unsupported file data');
      // Copy only the current chunk: never detach the caller's packet/stream buffer.
      const buffer=view.slice().buffer;
      await request('write',{data:buffer,position},[buffer]);
    }catch(error){await release('abort').catch(()=>{});throw error;}
  }
  const stream=new WritableStream({write,close:()=>release(),abort:()=>release('abort')});
  const withWriter=async operation=>{const writer=stream.getWriter();try{return await operation(writer);}finally{writer.releaseLock();}};
  stream.write=value=>withWriter(writer=>writer.write(value));
  stream.seek=position=>stream.write({type:'seek',position});
  stream.truncate=size=>stream.write({type:'truncate',size});
  stream.close=()=>withWriter(writer=>writer.close());
  stream.abort=reason=>withWriter(writer=>writer.abort(reason));
  stream.read=async(position,length)=>new Uint8Array(await request('read',{position,length}));
  // The worker writes in place. Abort keeps any received prefix, not a rollback.
  return stream;
}
