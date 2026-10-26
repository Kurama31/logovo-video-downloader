import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createOpfsEngine} from '../app/web/opfs-worker-core.js';
import {createFileWriter} from '../app/web/file-writer.js';

function filesystem(){
  const files=new Map();
  const root={
    async resolve(file){return file.path;},
    async getFileHandle(name){if(!files.has(name))throw Error('Not found');return files.get(name);},
    async getDirectoryHandle(){return root;},
  };
  function file(name,initial='',options={}){
    const result={path:['jobs',name],bytes:Buffer.from(initial),locked:false,closes:0,flushes:0};
    result.createSyncAccessHandle=async()=>{
      if(result.locked)throw Error('Locked');result.locked=true;
      return {
        async truncate(size){const next=Buffer.alloc(size);result.bytes.copy(next,0,0,size);result.bytes=next;},
        async write(bytes,{at}){
          if(options.quota)throw Object.assign(new Error('Storage full'),{name:'QuotaExceededError'});
          const count=Math.min(bytes.length,options.partial||bytes.length);
          const next=Buffer.alloc(Math.max(result.bytes.length,at+count));result.bytes.copy(next);Buffer.from(bytes).copy(next,at,0,count);result.bytes=next;return count;
        },
        async read(bytes,{at}){const part=result.bytes.subarray(at,at+bytes.length);bytes.set(part);return part.length;},
        async flush(){result.flushes++;},
        async close(){result.locked=false;result.closes++;},
      };
    };
    files.set(name,result);return result;
  }
  return {root,file};
}
test('OPFS engine preserves prefixes, handles short writes and releases locks',async()=>{
  const fs=filesystem(),file=fs.file('part','old bytes',{partial:2}),execute=createOpfsEngine(()=>fs.root);
  await execute({op:'open',writerId:1,path:file.path,keepExistingData:true});
  await execute({op:'seek',writerId:1,position:4});
  await execute({op:'write',writerId:1,data:new TextEncoder().encode('video').buffer});
  assert.equal(file.bytes.toString(),'old video');
  assert.equal(Buffer.from(await execute({op:'read',writerId:1,position:0,length:4})).toString(),'old ');
  await execute({op:'close',writerId:1});
  assert.equal(file.locked,false);assert.equal(file.closes,1);assert.equal(file.flushes,1);
});
test('OPFS engine validates paths and seek sizes before operating',async()=>{
  const fs=filesystem(),file=fs.file('part','prefix'),execute=createOpfsEngine(()=>fs.root);
  await assert.rejects(execute({op:'open',writerId:1,path:['..','part']}),/Invalid OPFS path/);
  await execute({op:'open',writerId:1,path:file.path});
  assert.equal(file.bytes.length,0);
  await assert.rejects(execute({op:'seek',writerId:1,position:-1}),/Invalid file position/);
  await execute({op:'abort',writerId:1});assert.equal(file.locked,false);
});

let storage;
class WorkerShim{
  constructor(){this.execute=createOpfsEngine(()=>storage.root);this.queue=Promise.resolve();}
  postMessage(message,transfer){
    const data=structuredClone(message,{transfer});
    this.queue=this.queue.then(async()=>{
      try{this.onmessage({data:{id:data.id,result:await this.execute(data)}});}catch(error){this.onmessage({data:{id:data.id,error:{name:error.name,message:error.message}}});}
    });
  }
  terminate(){}
}
test('Safari adapter is a real WritableStream with positioned writes and safe chunk transfers',async()=>{
  storage=filesystem();const file=storage.file('adapter','');
  const previousWorker=globalThis.Worker,previousNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  globalThis.Worker=WorkerShim;
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{storage:{getDirectory:async()=>storage.root}}});
  try{
    const stream=await createFileWriter(file),writer=stream.getWriter();
    const bytes=new Uint8Array([1,2,3,4]);
    await writer.write({type:'write',position:2,data:bytes});
    assert.equal(bytes.byteLength,4,'Input buffer was detached');
    await writer.write({type:'write',position:0,data:new Uint8Array([9,8])});
    await writer.close();writer.releaseLock();
    assert.deepEqual([...file.bytes],[9,8,1,2,3,4]);assert.equal(file.locked,false);
    const resumed=await createFileWriter(file,{keepExistingData:true});
    await resumed.seek(6);await resumed.write('ok');
    assert.equal(Buffer.from(await resumed.read(6,2)).toString(),'ok');
    await resumed.truncate(7);await resumed.close();
    assert.deepEqual([...file.bytes],[9,8,1,2,3,4,111]);
    const blobFile=storage.file('blob',''),blobWriter=await createFileWriter(blobFile);
    await blobWriter.write(new Blob(['video'],{type:'video/mp4'}));await blobWriter.close();
    assert.equal(blobFile.bytes.toString(),'video');
    const quota=storage.file('quota','',{quota:true}),failed=await createFileWriter(quota);
    await assert.rejects(failed.write(new Uint8Array([1])),{name:'QuotaExceededError'});
    assert.equal(quota.locked,false,'Failed write leaked an exclusive file lock');
  }finally{
    globalThis.Worker=previousWorker;
    if(previousNavigator)Object.defineProperty(globalThis,'navigator',previousNavigator);else delete globalThis.navigator;
  }
});
