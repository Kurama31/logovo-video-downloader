import {createOpfsEngine} from './opfs-worker-core.js';
const execute=createOpfsEngine(()=>navigator.storage.getDirectory());
let queue=Promise.resolve();
self.onmessage=event=>{
  const message=event.data;
  queue=queue.then(async()=>{
    try{
      const result=await execute(message);
      self.postMessage({id:message.id,result},result instanceof ArrayBuffer?[result]:[]);
    }catch(error){self.postMessage({id:message.id,error:{name:error.name||'Error',message:error.message||String(error)}});}
  });
};
