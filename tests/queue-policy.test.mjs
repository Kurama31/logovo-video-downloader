import {test} from 'node:test';
import assert from 'node:assert/strict';
import {resumableQueue} from '../app/web/queue-policy.js';

const job=(id,status='paused',extra={})=>({id,status,batch:true,...extra});
test('saved queue selects waiting and paused entries only',()=>{
  const states=['pending','paused','awaiting','error','completed','cancelled','analyzing','client_downloading'];
  assert.deepEqual(resumableQueue(states.map(state=>job(state,state))).map(j=>j.id),['pending','paused','awaiting']);
});
test('browser-local errors exclude remotely paused entries',()=>{
  const remote=[job('error'),job('disconnect'),job('paused')];
  const local=[job('error','error'),job('disconnect','paused',{error:'Соединение прервано.'})];
  assert.deepEqual(resumableQueue(remote,local).map(j=>j.id),['paused']);
});
test('expired entries and non-batch jobs cannot resume',()=>{
  assert.deepEqual(resumableQueue([job('expired','paused',{expires_at:5}),job('gone','paused',{data_expired:true}),
    job('single','paused',{batch:false}),job('ok','pending',{expires_at:11})],[],[],10).map(j=>j.id),['ok']);
});
test('server errors cannot be masked by stale local state',()=>{
  assert.deepEqual(resumableQueue([job('server','paused',{error:'Failure'}),job('status','error')],
    [job('server'),job('status')]),[]);
});
test('completed and expired local entries stay excluded',()=>{
  assert.deepEqual(resumableQueue([job('done'),job('expired'),job('cancel')],
    [job('done','completed'),job('expired','paused',{localExpired:true}),job('cancel','cancelled')]),[]);
});
test('live failures override a stale paused local snapshot',()=>{
  assert.deepEqual(resumableQueue([job('active')],[job('active')],[job('active','error')]),[]);
});
