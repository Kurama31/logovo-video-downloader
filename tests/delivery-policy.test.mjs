import test from 'node:test';
import assert from 'node:assert/strict';
import {attemptAutomaticDownload,sharePreparedFile} from '../app/web/delivery-policy.js';

function setup(error){
  const events=[];let removed=false,clicked=false;
  const document={visibilityState:'visible',body:{append(){}},createElement(){return {click(){clicked=true;if(error)throw error;},remove(){removed=true;}};}};
  return {document,navigator:{userActivation:{isActive:false}},emit:(...entry)=>events.push(entry),events,get removed(){return removed;},get clicked(){return clicked;}};
}
test('automatic command is unconfirmed, even without user activation',()=>{
  const env=setup();const result=attemptAutomaticDownload({...env,url:'blob:test',name:'video.mp4'});
  assert.equal(result.status,'unconfirmed');assert.ok(env.clicked&&env.removed);
  assert.deepEqual(env.events.map(e=>e[0]),['attempt','command_sent']);
  assert.equal(env.events[0][1].activation,false);
});
test('actual browser denial is recorded and anchor removed',()=>{
  const env=setup(Object.assign(new Error('private detail'),{name:'NotAllowedError'}));
  assert.equal(attemptAutomaticDownload({...env,url:'blob:test',name:'video.mp4'}).status,'error');
  assert.equal(env.events[1][1].error_name,'NotAllowedError');assert.ok(env.removed);
  assert.ok(!JSON.stringify(env.events).includes('private detail'));
});
test('share is called synchronously with the prepared file; handoff is not Photos confirmation',async()=>{
  let called=false;const file={name:'video.mp4',type:'video/mp4'};
  const action=sharePreparedFile({file,navigator:{share(payload){called=true;assert.equal(payload.files[0],file);return Promise.resolve();}},document:{visibilityState:'visible'}});
  assert.ok(called);assert.equal((await action).status,'handed_off');
});
for(const name of ['AbortError','NotAllowedError','SecurityError'])test('share handles '+name,async()=>{
  const events=[];
  const result=await sharePreparedFile({file:{},navigator:{share(){throw Object.assign(new Error(),{name});}},emit:event=>events.push(event)});
  assert.equal(result.status,name==='AbortError'?'cancelled':'error');assert.equal(events[1],result.status);
});
