import { expect, it } from 'vitest';
import { notifyOwnerFailure } from '../src/failure-notification.js';
import type { RelayConfig } from '../src/config.js';
const config={allowedSenderIds:new Set(['U1']),targetUserIds:new Set(['U1']),teamId:'T1',slackContextToken:'test',
  multicaWorkspaceId:'w',multicaProjectId:'p',multicaAgentId:'a',kvRestApiUrl:'https://kv.test'} as RelayConfig;
const event={teamId:'T1',channelId:'C1',senderUserId:'U2',threadTs:'1.000',messageTs:'2.000',text:'private input',mention:{type:'user' as const,id:'U1'}};
function fixture() {
  const values=new Map<string,string>();
  const state={posts:[] as any[], user:'U1',team:'T1',lose:false,unknown:false,rate:false};
  const send:typeof fetch=async(url,init)=>{
    const path=String(url);
    if(path===config.kvRestApiUrl) {
      const [cmd,key,value,...args]=JSON.parse(String(init?.body));
      if(cmd==='GET')return Response.json({result:values.get(key) ?? null});
      if(cmd==='SET') {if(args.includes('NX') && values.has(key))return Response.json({result:null});values.set(key,value);return Response.json({result:'OK'});}
      if(cmd==='EVAL') {values.delete(args[0]);return Response.json({result:1});}
    }
    if(path.includes('/auth.test'))return Response.json({ok:true,user_id:'U1',team_id:state.team});
    if(path.includes('/conversations.info'))return Response.json({ok:true,channel:{id:'D1',is_im:true,user:state.user}});
    if(path.includes('/conversations.history'))return Response.json({ok:true,messages:state.unknown?[]:state.posts});
    if(path.endsWith('/chat.postMessage')) {
      if(state.rate)return new Response('',{status:429});
      const body=JSON.parse(String(init?.body));expect(body.channel).toBe('D1');expect(body.thread_ts).toBeUndefined();
      state.posts.push({...body,ts:'3.000',user:'U1'});
      if(state.lose)throw new Error('timeout');
      return Response.json({ok:true,channel:'D1',ts:'3.000'});
    }
    throw new Error('unexpected '+path);
  };
  return {state,send};
}
it('privately reports safe failure categories once, without copying channel content',async()=>{
  const f=fixture();
  for(let i=0;i<2;i++)expect(await notifyOwnerFailure(event,'message','model_not_found','D1',config,f.send)).toBe('failure_owner_notified');
  expect(f.state.posts).toHaveLength(1);
  expect(f.state.posts[0].text).toContain('模型不受当前登录方式支持');
  expect(f.state.posts[0].text).toContain('/archives/C1/p2000');
  expect(f.state.posts[0].text).not.toContain('private input');
});
it.each(['wrong recipient','wrong workspace'])('rejects %s before posting',async(test)=>{
  const f=fixture();if(test==='wrong recipient')f.state.user='U2';else f.state.team='T2';
  await expect(notifyOwnerFailure(event,'m','execution_failed','D1',config,f.send)).rejects.toThrow('identity_rejected');
  expect(f.state.posts).toHaveLength(0);
});
it('reads back uncertain delivery and never reposts an unknown write',async()=>{
  const f=fixture();f.state.lose=true;
  await expect(notifyOwnerFailure(event,'m','execution_failed','D1',config,f.send)).rejects.toThrow('timeout');
  f.state.unknown=true;
  await expect(notifyOwnerFailure(event,'m','execution_failed','D1',config,f.send)).rejects.toThrow('slack_delivery_unknown');
  f.state.unknown=false;
  expect(await notifyOwnerFailure(event,'m','execution_failed','D1',config,f.send)).toBe('failure_owner_notified');
  expect(f.state.posts).toHaveLength(1);
});
it('does not redirect to a group or unconfigured destination',async()=>{
  const f=fixture();for(const destination of [undefined,'C1'])
    expect(await notifyOwnerFailure(event,'m','execution_failed',destination,config,f.send)).toBe('failure_notification_unconfigured');
  expect(f.state.posts).toHaveLength(0);
});

it('retries a definitive rate rejection without duplicating an accepted message',async()=>{
  const f=fixture();f.state.rate=true;
  await expect(notifyOwnerFailure(event,'m','execution_failed','D1',config,f.send)).rejects.toThrow('slack_rate_limited');
  f.state.rate=false;
  expect(await notifyOwnerFailure(event,'m','execution_failed','D1',config,f.send)).toBe('failure_owner_notified');
  expect(f.state.posts).toHaveLength(1);
});
