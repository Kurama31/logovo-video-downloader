import {test} from 'node:test';
import assert from 'node:assert/strict';
import {receivePart} from '../app/web/transfer-part.js';

function file(initial=Buffer.alloc(0)){
  let bytes=Buffer.from(initial);
  return {get bytes(){return bytes;},getFile:async()=>new Blob([bytes]),
    createWritable:async({keepExistingData=false}={})=>{
      let draft=keepExistingData?Buffer.from(bytes):Buffer.alloc(0),position=0;
      return {seek:async n=>{position=n;},write:async value=>{
        if(position+value.length>draft.length){const expanded=Buffer.alloc(position+value.length);draft.copy(expanded);draft=expanded;}
        draft.set(value,position);position+=value.length;
      },close:async()=>{bytes=draft;}};
    }};
}
async function run(handle,state,request,options={}){
  let downloaded=handle.bytes.length;
  await receivePart({handle,state,signal:new AbortController().signal,request,
    onBytes:n=>{downloaded+=n;},checkpoint:async()=>{},...options});
  return downloaded;
}
function partial(bytes,start,end,headers={}){
  return new Response(bytes.subarray(start,end+1),{status:206,headers:{'Content-Range':`bytes ${start}-${end}/${bytes.length}`,...headers}});
}

test('large unvalidated file uses a single request without fixed Range',async()=>{
  const bytes=Buffer.alloc(6*1024*1024,81),handle=file(),state={};let calls=0;
  assert.equal(await run(handle,state,async headers=>{calls++;assert.deepEqual(headers,{});return new Response(bytes);}),bytes.length);
  assert.equal(calls,1);assert.deepEqual(handle.bytes,bytes);assert.equal(state.complete,true);
});
test('source-sized partial responses work without validators; overlaps are not duplicated',async()=>{
  const bytes=Buffer.from('abcdefghijklmno'),handle=file(),state={},seen=[];
  await run(handle,state,async headers=>{
    seen.push(headers);const start=Number(headers.Range?.match(/bytes=(\d+)-$/)?.[1]||0);
    assert.equal(headers['If-Range'],undefined);return partial(bytes,start,Math.min(start+4,bytes.length-1));
  });
  assert.deepEqual(handle.bytes,bytes);assert.ok(seen.length>1);assert.deepEqual(seen[0],{});
});
test('interrupted unvalidated part restarts at zero on the next run',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(bytes.subarray(0,5)),state={size:5};
  assert.equal(await run(handle,state,async headers=>{assert.deepEqual(headers,{});return new Response(bytes);}),bytes.length);
  assert.deepEqual(handle.bytes,bytes);
});
test('validated part resumes with an open-ended Range',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(bytes.subarray(0,5)),state={validator:'"v1"',total:bytes.length};
  await run(handle,state,async headers=>{assert.deepEqual(headers,{Range:'bytes=5-','If-Range':'"v1"'});return partial(bytes,5,bytes.length-1,{ETag:'"v1"'});});
  assert.deepEqual(handle.bytes,bytes);
});
test('ignored Range / changed If-Range returns full body without appending duplicates',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(Buffer.from('oldxx')),state={validator:'"old"',total:bytes.length};
  await run(handle,state,async()=>new Response(bytes,{headers:{ETag:'"new"'}}));
  assert.deepEqual(handle.bytes,bytes);
});
test('changed validator triggers one fresh request',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(bytes.subarray(0,5)),state={validator:'"old"',total:bytes.length};let calls=0;
  await run(handle,state,async headers=>{
    if(++calls===1)return partial(bytes,5,bytes.length-1,{ETag:'"new"'});
    assert.deepEqual(headers,{});return new Response(bytes,{headers:{ETag:'"new"'}});
  });assert.equal(calls,2);assert.deepEqual(handle.bytes,bytes);
});
test('backend restart signal triggers a fresh request',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(bytes.subarray(0,5)),state={validator:'"old"'};let calls=0;
  await run(handle,state,async headers=>{
    if(++calls===1)return new Response('{}',{status:409,headers:{'X-DAV-Restart':'1'}});
    assert.deepEqual(headers,{});return new Response(bytes);
  });assert.deepEqual(handle.bytes,bytes);
});
test('weak ETag does not become a resumable validator',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(),state={};
  await run(handle,state,async()=>new Response(bytes,{headers:{ETag:'W/"weak"'}}));
  assert.equal(state.validator,null);
});
test('source changes at overlap are rejected',async()=>{
  const bytes=Buffer.from('abcdefghijk'),handle=file(),state={};let calls=0;
  await assert.rejects(run(handle,state,async headers=>{
    const start=Number(headers.Range?.match(/bytes=(\d+)-$/)?.[1]||0);
    return partial(++calls===1?bytes:Buffer.from('XXXXXXXXXXX'),start,Math.min(start+4,bytes.length-1));
  }),/стыке/);assert.equal(state.complete,false);
});
test('a truncated response never becomes a completed video',async()=>{
  const handle=file(),state={};
  await assert.rejects(run(handle,state,async()=>new Response('abc',{status:206,headers:{'Content-Range':'bytes 0-5/10'}})),/прервано/);
  assert.equal(state.complete,false);assert.equal(handle.bytes.toString(),'abc');
});
test('invalid Content-Range is rejected before writing',async()=>{
  const handle=file(),state={};
  await assert.rejects(run(handle,state,async()=>new Response('abc',{status:206,headers:{'Content-Range':'bytes 2-4/10'}})),/диапазон/);
  assert.equal(handle.bytes.length,0);
});
test('pause saves an incomplete prefix and keeps a usable validator',async()=>{
  const handle=file(),state={},controller=new AbortController();
  await assert.rejects(run(handle,state,async()=>new Response(new ReadableStream({start(c){c.enqueue(Buffer.from('abc'));}}),{headers:{ETag:'"v1"','Content-Length':'10'}}),
    {signal:controller.signal,onBytes:()=>controller.abort()}),e=>e.name==='AbortError');
  assert.equal(handle.bytes.toString(),'abc');assert.equal(state.validator,'"v1"');assert.equal(state.complete,false);
});
