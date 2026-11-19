import {it,expect} from 'vitest';
import {createHmac} from 'node:crypto';
import {networkRecovery} from '../src/network-recovery.js';
import {digest} from '../src/thread-router.js';

const env={SLACK_SIGNING_SECRET:'test',SLACK_TEAM_ID:'T1',SLACK_TARGET_USER_IDS:'U1',SLACK_ALLOWED_CHANNEL_IDS:'D1',
  SLACK_ALLOWED_SENDER_IDS:'U1',SLACK_CONTEXT_TOKEN:'test',SLACK_REACTION_NAME:'eyes',SLACK_USER_TOKEN:'test',
  MULTICA_API_BASE_URL:'https://tasks.test',MULTICA_API_TOKEN:'test',MULTICA_WORKSPACE_ID:'W1',MULTICA_PROJECT_ID:'P1',MULTICA_AGENT_ID:'A1',
  KV_REST_API_URL:'https://redis.test',KV_REST_API_TOKEN:'test',QSTASH_TOKEN:'test',QSTASH_CURRENT_SIGNING_KEY:'test',QSTASH_NEXT_SIGNING_KEY:'test',
  RELAY_CONSUMER_URL:'https://relay.test/api/queue/consume',GITHUB_REVIEW_SIGNING_KEY:'key'};
function fixture(){
  const ts=(Date.now()/1000).toFixed(6), event={teamId:'T1',channelId:'D1',threadTs:ts,messageTs:ts,senderUserId:'U1',text:'<@U1> check',mention:{type:'user',id:'U1'}};
  const thread=digest('T1:D1:'+ts),message=thread,scope=digest('W1:P1:A1');
  const payload={eventPayload:event,context:{githubReviewTicket:'ticket'}};
  const marker='<!-- relay-thread:'+scope+':'+thread+' -->\n';
  const state:any={issueId:'issue'};
  const packet=Buffer.from(JSON.stringify({text:'saved',seconds:60,tools:[],reviews:[],internalFailure:true,failureCode:'model_network_wait'})).toString('base64url');
  const output='relay-agent/1 '+packet+'.'+createHmac('sha256','key').update('agent-result/1:ticket:'+packet).digest('hex');
  const run={id:'run',workspace_id:'W1',issue_id:'issue',agent_id:'A1',status:'completed',result:{output}};
  const runs:any[]=[run];
  const send:typeof fetch=async(url,init)=>{
    if(String(url)==='https://redis.test'){
      const [,key]=JSON.parse(String(init?.body));
      return Response.json({result:key.endsWith(':envelope')?JSON.stringify(payload):key.includes(':thread:')?JSON.stringify(state):null});
    }
    if(String(url).endsWith('/task-runs'))return Response.json(runs);
    if(String(url).endsWith('/issues/issue'))return Response.json({id:'issue',title:'task',project_id:'P1',assignee_type:'agent',assignee_id:'A1',description:marker+JSON.stringify(payload)});
    throw Error('unexpected external write');
  };
  function request(key='key',stamp=String(Math.floor(Date.now()/1000))){
    const body=JSON.stringify({thread,message});
    return new Request('https://relay.test/api/runtime/recovery',{method:'POST',body,headers:{'X-Relay-Time':stamp,'X-Relay-Signature':createHmac('sha256',key).update('network-recovery/1:'+stamp+':'+body).digest('hex')}});
  }
  return {request,send,state,runs,payload};
}
it('returns only the authenticated paused run, and waits while any run in the thread is active',async()=>{
  const f=fixture();
  expect(await (await networkRecovery(f.request(),env,f.send)).json()).toEqual({action:'resume',runId:'run'});
  f.runs.unshift({...f.runs[0],id:'other',status:'running'});
  expect(await (await networkRecovery(f.request(),env,f.send)).json()).toEqual({action:'waiting'});
});
it('rejects unsigned or stale readiness claims before accessing state',async()=>{
  const f=fixture();
  expect((await networkRecovery(f.request('wrong'),env,f.send)).status).toBe(401);
  expect((await networkRecovery(f.request('key','1000000000'),env,f.send)).status).toBe(401);
});
it('does not revive cancelled, already handled, or unauthorized requests',async()=>{
  const f=fixture();f.state.ignoredThrough=f.payload.eventPayload.messageTs;
  expect(await (await networkRecovery(f.request(),env,f.send)).json()).toEqual({action:'cancelled'});
  delete f.state.ignoredThrough;f.runs[0].status='cancelled';
  expect(await (await networkRecovery(f.request(),env,f.send)).json()).toEqual({action:'cancelled'});
  f.runs[0].status='completed';f.payload.eventPayload.senderUserId='U2';
  expect(await (await networkRecovery(f.request(),env,f.send)).json()).toEqual({action:'cancelled'});
});
