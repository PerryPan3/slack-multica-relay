import { createHash, createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import worker from '../src/cloudflare.js';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { createTestHarness } from 'wrangler';

// Inline config uses the production entry/compatibility settings without loading private dotenv files.
const config = JSON.parse(readFileSync('wrangler.jsonc', 'utf8'));
const env = {
  SLACK_SIGNING_SECRET: 'test-signing', SLACK_TEAM_ID: 'T1', SLACK_TARGET_USER_IDS: 'U1',
  SLACK_ALLOWED_CHANNEL_IDS: 'C1', SLACK_USER_TOKEN: 'test', SLACK_CONTEXT_TOKEN: 'test',
  SLACK_REACTION_NAME: 'eyes', MULTICA_API_BASE_URL: 'https://multica.invalid', MULTICA_API_TOKEN: 'test',
  MULTICA_WORKSPACE_ID: 'W1', MULTICA_PROJECT_ID: 'P1', MULTICA_AGENT_ID: 'A1',
  KV_REST_API_URL: 'https://redis.invalid', KV_REST_API_TOKEN: 'test', QSTASH_TOKEN: 'test',
  QSTASH_CURRENT_SIGNING_KEY: 'current-test-key', QSTASH_NEXT_SIGNING_KEY: 'next-test-key',
  RELAY_CONSUMER_URL: 'https://relay.test/api/queue/consume',
};
const server = createTestHarness({workers:[{config:{...config, vars:env}}]});
function slackHeaders(body: string) {
  const ts = String(Math.floor(Date.now()/1000));
  return {'x-slack-request-timestamp':ts,'x-slack-signature':'v0='+createHmac('sha256',env.SLACK_SIGNING_SECRET).update(`v0:${ts}:${body}`).digest('hex')};
}
function queueSignature(body: string, subject = env.RELAY_CONSUMER_URL, key = env.QSTASH_CURRENT_SIGNING_KEY) {
  const now = Math.floor(Date.now()/1000);
  const head = Buffer.from(JSON.stringify({alg:'HS256',typ:'JWT'})).toString('base64url');
  const claims = Buffer.from(JSON.stringify({iss:'Upstash',sub:subject,iat:now,nbf:now-1,exp:now+60,
    body:createHash('sha256').update(body).digest('base64url')})).toString('base64url');
  return `${head}.${claims}.`+createHmac('sha256',key).update(`${head}.${claims}`).digest('base64url');
}
describe('Cloudflare workerd runtime',()=>{
  beforeAll(async()=>{await server.listen();},60_000);
  afterAll(async()=>{await server.close();},30_000);
  it('routes health, missing paths and unsupported methods',async()=>{
    expect(await (await server.fetch('/api/health')).json()).toEqual({ok:true,service:'slack-multica-relay'});
    expect((await server.fetch('/missing')).status).toBe(404);
    expect((await server.fetch('/api/slack/events')).status).toBe(405);
    expect((await server.fetch('/api/queue/consume')).status).toBe(405);
  });
  it('preserves escaped Slack bytes and rejects tampering',async()=>{
    const raw='{"type":"url_verification","challenge":"\\u4f60\\u597d"}';
    const headers=slackHeaders(raw);
    expect(await (await server.fetch('/api/slack/events',{method:'POST',body:raw,headers})).json()).toEqual({challenge:'你好'});
    expect((await server.fetch('/api/slack/events',{method:'POST',body:raw+' ',headers})).status).toBe(401);
  });
  it('applies injected channel policy before any external side effect',async()=>{
    const raw=JSON.stringify({type:'event_callback',team_id:'T1',event:{type:'message',channel:'C2',user:'U2',ts:'100.000001',text:'<@U1> test'}});
    expect(await (await server.fetch('/api/slack/events',{method:'POST',body:raw,headers:slackHeaders(raw)})).json()).toEqual({action:'ignored',reason:'not_allowed'});
  });
  it.each([env.QSTASH_CURRENT_SIGNING_KEY,env.QSTASH_NEXT_SIGNING_KEY])('verifies QStash JWT with %s inside workerd',async(key)=>{
    const body='{}';
    const response=await server.fetch(env.RELAY_CONSUMER_URL,{method:'POST',body,headers:{'upstash-signature':queueSignature(body,undefined,key)}});
    expect(await response.json()).toEqual({action:'rejected',error:'invalid_event',retryable:false});
  });
  it('rejects a modified queue body and wrong consumer URL',async()=>{
    for(const [body,signature] of [['{"changed":true}',queueSignature('{}')],['{}',queueSignature('{}','https://wrong.test/api/queue/consume')]]){
      expect((await server.fetch(env.RELAY_CONSUMER_URL,{method:'POST',body,headers:{'upstash-signature':signature!}})).status).toBe(401);
    }
  });
});

describe('scheduled Multica renewal', () => {
  afterEach(() => { vi.unstubAllGlobals(); });
  it.each([true, false])('accepts an authoritative renewal receipt (renewed=%s)', async renewed => {
    const send = vi.fn().mockResolvedValue(Response.json({renewed, expires_at:'2099-01-01T00:00:00Z'}));
    vi.stubGlobal('fetch', send);
    await worker.scheduled({}, env);
    const [url, options] = send.mock.calls[0];
    expect(String(url)).toBe('https://multica.invalid/api/tokens/current/renew');
    expect(options).toMatchObject({method:'POST', redirect:'manual', headers:{authorization:'Bearer test'}});
  });
  it.each([302, 401, 503])('reports an unsuccessful check (%i)', async status => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null,{status})));
    await expect(worker.scheduled({},env)).rejects.toThrow('multica_renewal_http_' + status);
  });
  it('rejects a successful HTTP response without a valid expiry', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({renewed:true,expires_at:''})));
    await expect(worker.scheduled({},env)).rejects.toThrow('invalid_renewal_receipt');
  });
});


describe('durable scheduler',()=>{
  afterEach(()=>vi.unstubAllGlobals());
  const settings={...env,RELAY_DURABLE_SCHEDULER:'true',MULTICA_RUNTIME_ID:'R1'};
  const online=()=>[{id:'R1',workspace_id:'W1',status:'online',last_seen_at:new Date().toISOString()}];
  it('does not touch queued jobs or retry counters while offline, then drains after recovery',async()=>{
    let connected=false;
    const send=vi.fn(async(url:unknown,init:RequestInit)=>{
      if(String(url).endsWith('/api/runtimes'))return Response.json(connected?online():[{...online()[0],status:'offline'}]);
      expect(JSON.parse(String(init.body))[0]).toBe('ZRANGEBYSCORE');
      return Response.json({result:[]});
    });
    vi.stubGlobal('fetch',send);
    for(let i=0;i<10;i++)await worker.scheduled({cron:'* * * * *'},settings);
    expect(send).toHaveBeenCalledTimes(10);
    expect(send.mock.calls.every(([url])=>String(url).endsWith('/api/runtimes'))).toBe(true);
    connected=true;
    await worker.scheduled({cron:'* * * * *'},settings);
    expect(send).toHaveBeenCalledTimes(12);
  });
  it.each(['stale','wrong_workspace','wrong_runtime','missing','unavailable','invalid','future'])('waits without retries when heartbeat is %s',async kind=>{
    const rows=online();
    if(kind==='stale')rows[0]!.last_seen_at=new Date(Date.now()-90001).toISOString();
    if(kind==='future')rows[0]!.last_seen_at=new Date(Date.now()+60000).toISOString();
    if(kind==='wrong_workspace')rows[0]!.workspace_id='W2';
    if(kind==='wrong_runtime')rows[0]!.id='R2';
    const send=vi.fn(async()=>Response.json(kind==='invalid'?{}:kind==='missing'?[]:rows,{status:kind==='unavailable'?503:200}));
    vi.stubGlobal('fetch',send);
    await worker.scheduled({cron:'* * * * *'},settings);
    expect(send).toHaveBeenCalledTimes(1);
  });
  it('keeps daily token renewal independent of local connectivity',async()=>{
    const send=vi.fn(async(_url:unknown)=>Response.json({renewed:true,expires_at:'2099-01-01T00:00:00Z'}));vi.stubGlobal('fetch',send);
    await worker.scheduled({cron:'15 1 * * *'},settings);
    expect(String(send.mock.calls[0]?.[0])).toContain('/api/tokens/current/renew');
  });
});
