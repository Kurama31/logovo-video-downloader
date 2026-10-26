import {needsWorkerWriter} from './file-writer.js';
const recent=[],sent=new Map();
export const recentDeliveryLog=()=>recent.map(entry=>({...entry}));
export function deliveryLog(jobId,method,event,details={}){
  const count=sent.get(jobId)||0;if(count>=40)return;
  if(!sent.has(jobId)&&sent.size>=256)sent.delete(sent.keys().next().value);
  sent.set(jobId,count+1);
  const report={method,event,activation:details.activation??null,visible:details.visible===true,
    worker_storage:needsWorkerWriter(),error_name:details.error_name||null};
  const entry={job_id:jobId,at:new Date().toISOString(),...report};
  recent.push(entry);if(recent.length>100)recent.shift();
  console.info('[Logovo delivery]',entry);
  // No source URLs, filenames, folder paths, UA strings or exception text are sent.
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),4000);
  fetch(`/api/jobs/${encodeURIComponent(jobId)}/client/delivery-log`,{
    method:'POST',credentials:'same-origin',keepalive:true,signal:controller.signal,
    headers:{'Content-Type':'application/json','X-Requested-With':'DAV'},body:JSON.stringify(report),
  }).then(response=>{if(!response.ok)console.info('[Logovo delivery] Server log unavailable:',response.status);})
    .catch(()=>{}).finally(()=>clearTimeout(timer));
}
