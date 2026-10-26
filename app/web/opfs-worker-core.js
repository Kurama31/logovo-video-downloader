// File operations are serialized by the dedicated worker. No video is held in RAM.
function position(value){
  if(!Number.isSafeInteger(value)||value<0)throw new TypeError('Invalid file position');
  return value;
}
export function createOpfsEngine(getRoot){
  const files=new Map();
  async function release(id){
    const file=files.get(id);if(!file)return;
    files.delete(id);
    try{await file.handle.flush();}finally{await file.handle.close();}
  }
  return async function execute(message){
    const {op,writerId}=message;
    if(op==='open'){
      if(files.has(writerId))throw new Error('File writer already open');
      const segments=message.path;
      if(!Array.isArray(segments)||!segments.length||segments.some(part=>typeof part!=='string'||!part||['.','..'].includes(part)||/[\\/\0]/.test(part)))throw new TypeError('Invalid OPFS path');
      let parent=await getRoot();
      for(const segment of segments.slice(0,-1))parent=await parent.getDirectoryHandle(segment);
      const file=await parent.getFileHandle(segments.at(-1));
      if(typeof file.createSyncAccessHandle!=='function')throw new Error('В этом браузере недоступна совместимая запись файлов. Откройте сайт в обычной вкладке Safari и разрешите хранение данных сайта.');
      const handle=await file.createSyncAccessHandle();
      files.set(writerId,{handle,offset:0});
      try{if(!message.keepExistingData)await handle.truncate(0);}catch(error){await release(writerId).catch(()=>{});throw error;}
      return null;
    }
    if(op==='close'||op==='abort'){await release(writerId);return null;}
    const file=files.get(writerId);if(!file)throw new Error('File writer is closed');
    const handle=file.handle;
    if(op==='seek'){file.offset=position(message.position);return null;}
    if(op==='truncate'){
      const size=position(message.size);await handle.truncate(size);file.offset=Math.min(file.offset,size);return null;
    }
    if(op==='write'){
      const data=new Uint8Array(message.data);
      if(message.position!==undefined)file.offset=position(message.position);
      position(file.offset+data.length);
      let written=0;
      while(written<data.length){
        const count=await handle.write(data.subarray(written),{at:file.offset});
        if(!Number.isSafeInteger(count)||count<=0||count>data.length-written)throw new Error('File write made no progress');
        file.offset+=count;written+=count;
      }
      return written;
    }
    if(op==='read'){
      const start=position(message.position),length=position(message.length);
      if(length>1024*1024)throw new RangeError('Read request too large');
      const buffer=new Uint8Array(length),read=await handle.read(buffer,{at:start});
      if(!Number.isSafeInteger(read)||read<0||read>length)throw new Error('Invalid read result');
      return buffer.slice(0,read).buffer;
    }
    throw new TypeError('Unknown file operation');
  };
}
