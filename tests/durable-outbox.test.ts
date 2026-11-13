import {describe,it,expect,vi} from 'vitest';
import {DurableOutbox} from '../src/durable-outbox.js';
import {scheduleReply} from '../src/worker-reply.js';
import type {RelayConfig} from '../src/config.js';

const config={kvRestApiUrl:'https://redis.test',kvRestApiToken:'private',multicaWorkspaceId:'w',multicaProjectId:'p',multicaAgentId:'a',consumerUrl:'https://relay.test',durableScheduler:true} as RelayConfig;
function fixture(){
  let now=100000;
  const values=new Map<string,string>(),due=new Map<string,number>();
  const send=vi.fn<typeof fetch>(async(url,init)=>{
    expect(url).toBe(config.kvRestApiUrl);
    const c=JSON.parse(String(init?.body));let result:unknown=null;
    if(c[0]==='EVAL' && c[2]==='2'){
      if(!values.has(c[3])){values.set(c[3],c[5]);due.set(c[3],Number(c[6]));result=1;}
      else result=0;
    }else if(c[0]==='EVAL' && c[2]==='3'){
      if(values.get(c[5])===c[6]){
        if(c[7]){values.set(c[3],c[7]);due.set(c[3],Number(c[8]));}
        else{values.delete(c[3]);due.delete(c[3]);}
        values.delete(c[5]);result=1;
      }else result=0;
    }else if(c[0]==='GET')result=values.get(c[1])??null;
    else if(c[0]==='SET'){
      if(c[3]!=='NX' || !values.has(c[1])){values.set(c[1],c[2]);result='OK';}
    }else if(c[0]==='ZRANGEBYSCORE')result=[...due].filter(([,ts])=>ts<=Number(c[3])).sort((a,b)=>a[1]-b[1]).slice(0,8).map(([k])=>k);
    else throw new Error('unexpected command');
    return Response.json({result});
  });
  return {send,values,due,outbox:new DurableOutbox(config,send,()=>now),advance:(seconds:number)=>now+=seconds*1000};
}
describe('durable recovery without polling messages',()=>{
  it('persists duplicate requests once and defers execution until due',async()=>{
    const f=fixture(),process=vi.fn(async()=>Response.json({action:'routed'}));
    expect(await f.outbox.schedule({message:'one'},120)).toBe(await f.outbox.schedule({message:'one'},120));
    await f.outbox.drain(process);expect(process).not.toHaveBeenCalled();
    f.advance(120);await f.outbox.drain(process);await f.outbox.drain(process);
    expect(process).toHaveBeenCalledTimes(1);expect(f.due.size).toBe(0);
  });
  it('retries transient failure on a bounded delay, preserving the original request',async()=>{
    const f=fixture();await f.outbox.schedule({message:'one'});
    const process=vi.fn().mockResolvedValueOnce(Response.json({error:'unavailable'},{status:503})).mockResolvedValue(Response.json({action:'routed'}));
    await f.outbox.drain(process);await f.outbox.drain(process);expect(process).toHaveBeenCalledTimes(1);
    f.advance(60);await f.outbox.drain(process);expect(process.mock.calls[0][0]).toBe(process.mock.calls[1][0]);expect(f.due.size).toBe(0);
  });
  it('does not retry a rejected request or lose a job when Redis reads fail',async()=>{
    const f=fixture();await f.outbox.schedule({message:'one'});
    const send:typeof fetch=async(url,init)=>JSON.parse(String(init?.body))[0]==='GET'?Response.json({},{status:503}):f.send(url,init);
    const process=vi.fn(async()=>Response.json({action:'rejected',retryable:false}));
    await expect(new DurableOutbox(config,send,()=>100000).drain(process)).rejects.toThrow('outbox_read_failed');
    expect(f.due.size).toBe(1);expect(process).not.toHaveBeenCalled();
    for(const k of f.values.keys())if(k.endsWith(':lock'))f.values.delete(k);
    await f.outbox.drain(process);expect(f.due.size).toBe(0);
  });
  it('leases concurrent cron invocations and does not remove a newly scheduled successor',async()=>{
    const f=fixture();await f.outbox.schedule({attempt:0});
    const process=vi.fn(async()=>{await f.outbox.schedule({attempt:1},120);return Response.json({action:'waiting'});});
    await Promise.all([f.outbox.drain(process),f.outbox.drain(process)]);
    expect(process).toHaveBeenCalledTimes(1);expect(f.due.size).toBe(1);
  });
  it('retains exhausted failures privately and stops retrying',async()=>{
    const f=fixture();await f.outbox.schedule({message:'one'});
    const process=vi.fn(async()=>Response.json({error:'unavailable'},{status:503}));
    for(let i=0;i<25;i++){await f.outbox.drain(process);f.advance(300);}
    expect(process).toHaveBeenCalledTimes(24);expect(f.due.size).toBe(0);
    expect([...f.values.keys()].filter(k=>k.endsWith(':failed'))).toHaveLength(1);
  });
  it('stores reply checks in Redis without publishing QStash messages',async()=>{
    const f=fixture();await scheduleReply({kind:'thread_reply',issueId:'i',message:'m',thread:'t',attempt:1},config,f.send);
    expect(f.due.size).toBe(1);
    expect([...f.values.values()].some(v=>v.includes('thread_reply'))).toBe(true);
  });
});

it('does not bind the native fetch receiver to an outbox instance',async()=>{
  const send:typeof fetch=async function(this:unknown){
    if(this!==undefined && this!==globalThis)throw new TypeError('Illegal invocation');
    return Response.json({result:1});
  };
  await expect(new DurableOutbox(config,send).schedule({message:'offline'})).resolves.toEqual(expect.any(String));
});
