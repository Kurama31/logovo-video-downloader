// Saved queues never retry failures implicitly, including browser-local failures.
export function resumableQueue(serverJobs,localJobs=[],liveJobs=[],now=Date.now()/1000){
  const local=new Map(localJobs.map(job=>[job.id,job]));
  for(const job of liveJobs)local.set(job.id,job);
  return serverJobs.filter(job=>{
    const saved=local.get(job.id);
    return job.batch&&!job.data_expired&&!job.error&&
      ['pending','paused','awaiting'].includes(job.status)&&
      (job.expires_at==null||job.expires_at>now)&&
      !saved?.error&&!saved?.localExpired&&!saved?.data_expired&&
      !['error','completed','cancelled','cancelling'].includes(saved?.status);
  });
}
