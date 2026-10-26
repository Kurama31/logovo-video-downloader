import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createWakeLockController} from '../app/web/wake-lock.js';
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
function environment(){
  const document=new EventTarget();document.visibilityState='visible';
  const locks=[],states=[];
  const navigator={wakeLock:{async request(){const lock=new EventTarget();lock.released=false;lock.release=async()=>{lock.released=true;lock.dispatchEvent(new Event('release'));};locks.push(lock);return lock;}}};
  const controller=createWakeLockController({document,navigator,onState:state=>states.push(state)});
  return {document,navigator,locks,states,controller};
}
test('screen lock is reacquired on returning to the tab and released after pause/end',async()=>{
  const {document,locks,states,controller}=environment();
  await controller.setActive(true);assert.equal(locks.length,1);assert.equal(states.at(-1),'active');
  document.visibilityState='hidden';document.dispatchEvent(new Event('visibilitychange'));await tick();
  assert.equal(locks[0].released,true);assert.equal(states.at(-1),'hidden');
  document.visibilityState='visible';document.dispatchEvent(new Event('visibilitychange'));await tick();
  assert.equal(locks.length,2);assert.equal(states.at(-1),'active');
  await controller.setActive(false);assert.equal(locks[1].released,true);assert.equal(states.at(-1),'idle');await controller.dispose();
});
test('late wake-lock responses cannot leave the screen awake after cancellation',async()=>{
  const {controller,navigator,states}=environment();let resolve;
  const lock=new EventTarget();lock.released=false;lock.release=async()=>{lock.released=true;};
  navigator.wakeLock.request=()=>new Promise(done=>{resolve=done;});
  const starting=controller.setActive(true);await tick();await controller.setActive(false);resolve(lock);await starting;
  assert.equal(lock.released,true);assert.equal(states.at(-1),'idle');await controller.dispose();
});
test('unsupported and denied screen locks report a nonfatal state without retry loops',async()=>{
  const {controller,navigator,states}=environment();navigator.wakeLock.request=()=>Promise.reject(Error('Denied'));
  await controller.setActive(true);assert.equal(states.at(-1),'denied');
  const count=states.length;await controller.setActive(true);assert.equal(states.length,count);await controller.dispose();
  const doc=new EventTarget();doc.visibilityState='visible';const unsupported=[];
  const other=createWakeLockController({navigator:{},document:doc,onState:state=>unsupported.push(state)});
  await other.setActive(true);assert.equal(unsupported.at(-1),'unavailable');await other.dispose();
});
