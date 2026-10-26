// Keep the screen awake only while requested and this tab is visible.
// The operating system can still refuse/release a lock (e.g. low battery).
export function createWakeLockController({navigator=globalThis.navigator,document=globalThis.document,onState=()=>{}}={}){
  let active=false,sentinel=null,pending=null,epoch=0,disposed=false;
  const visible=()=>document.visibilityState==='visible';
  const emit=state=>onState(state);
  async function release(){const current=sentinel;sentinel=null;if(current)try{await current.release();}catch{}}
  async function acquire(){
    if(disposed||!active)return;
    if(!visible()){emit('hidden');return;}
    if(!navigator?.wakeLock?.request){emit('unavailable');return;}
    if(sentinel&&!sentinel.released){emit('active');return;}
    if(pending)return pending;
    const generation=epoch;
    emit('requesting');
    pending=(async()=>{
      try{
        const lock=await Promise.resolve().then(()=>navigator.wakeLock.request('screen'));
        if(disposed||!active||generation!==epoch||!visible()){await lock.release().catch(()=>{});return;}
        sentinel=lock;
        lock.addEventListener('release',()=>{if(sentinel===lock){sentinel=null;emit(!active?'idle':visible()?'denied':'hidden');}});
        emit('active');
      }catch{if(active&&generation===epoch)emit(visible()?'denied':'hidden');}
      finally{
        pending=null;
        if(!disposed&&active&&visible()&&generation!==epoch)void acquire();
      }
    })();
    return pending;
  }
  async function changed(){
    if(!active||disposed)return;
    epoch++;
    if(!visible()){emit('hidden');await release();}else await acquire();
  }
  document.addEventListener('visibilitychange',changed);
  return {
    setActive(value){
      const next=!!value;if(next===active||disposed)return pending||Promise.resolve();
      active=next;epoch++;
      if(active)return acquire();
      emit('idle');return release();
    },
    async dispose(){active=false;disposed=true;epoch++;document.removeEventListener('visibilitychange',changed);await release();},
  };
}
