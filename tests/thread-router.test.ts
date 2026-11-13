import {readTaskEnvelope} from '../src/task-presentation.js';
import { describe, expect, it, vi } from "vitest";
import {
  routeSlackThreadEvent, digest, messageKey,
  type SlackThreadEvent,
  type ThreadRouterConfig,
} from "../src/thread-router.js";
import { MemoryThreadStore } from "../src/thread-store.js";
const root: SlackThreadEvent = {
  teamId: "T1",
  channelId: "C1",
  messageTs: "100.000001",
  threadTs: "100.000001",
  senderUserId: "U2",
  text: "<@U1> test",
  mention: { type: "user", id: "U1" },
};
function fixture() {
  const issues: {
    id: string;
    title: string;
    description: string;
    project_id: string;
    assignee_type: string;
    assignee_id: string;
  }[] = [];
  const comments: { id: string; content: string }[] = [];
  let issuePosts = 0,
    commentPosts = 0,
    failIssue = false,
    failComment = false;
  const fetcher: typeof fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("/api/issues/search?")) return Response.json({ issues });
    if (url.endsWith("/api/issues")) {
      issuePosts++;
      const data = JSON.parse(String(init?.body));
      const row = {
        id: "issue-" + issuePosts,
        title: data.title,
        description: data.description,
        project_id: "project",
        assignee_type: "agent",
        assignee_id: "agent",
      };
      issues.push(row);
      if (failIssue) throw new DOMException("lost response", "TimeoutError");
      return Response.json(row, { status: 201 });
    }
    if (url.includes("/comments")) {
      if (init?.method === "POST") {
        commentPosts++;
        const row = {
          id: "comment-" + commentPosts,
          content: JSON.parse(String(init.body)).content,
        };
        comments.push(row);
        if (failComment)
          throw new DOMException("lost response", "TimeoutError");
        return Response.json(row, { status: 201 });
      }
      return Response.json(comments);
    }
    const found=issues.find(issue=>url.endsWith('/api/issues/'+issue.id));
    if(found)return Response.json(found);
    throw new Error("unexpected endpoint");
  };
  const config: ThreadRouterConfig = {
    multicaApiBaseUrl: "https://multica.test",
    multicaApiToken: "test",
    multicaWorkspaceId: "ws",
    multicaProjectId: "project",
    multicaAgentId: "agent",
    store: new MemoryThreadStore(),
    readContext: async (event) => ({ anchorTs: event.threadTs, cutoffTs: event.messageTs, capturedAt: '2026-01-01T00:00:00Z', timeline: { status: 'complete', messages: [] } }),
  };
  return {
    config,
    fetcher,
    issues,
    comments,
    get issuePosts() {
      return issuePosts;
    },
    get commentPosts() {
      return commentPosts;
    },
    loseIssueResponse() {
      failIssue = true;
    },
    loseCommentResponse() {
      failComment = true;
    },
  };
}
describe("direct Issue routing", () => {
  it("creates distinct issues for two simultaneous different Slack threads", async () => {
    const f = fixture();
    await Promise.all([
      routeSlackThreadEvent(root, f.config, f.fetcher),
      routeSlackThreadEvent(
        { ...root, messageTs: "101.000001", threadTs: "101.000001" },
        f.config,
        f.fetcher,
      ),
    ]);
    expect(f.issuePosts).toBe(2);
    expect(f.issues[0]!.title).not.toBe(f.issues[1]!.title);
  });
  it("one thread creates once and appends once per followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).action,
    ).toBe("duplicate");
    const next = { ...root, messageTs: "102.000001" };
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("recovers a committed Issue after lost response without second POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    expect(
      (await routeSlackThreadEvent(root, f.config, f.fetcher)).issueId,
    ).toBe("issue-1");
    expect(f.issuePosts).toBe(1);
  });
  it("does not blindly replay an ambiguous unconfirmed Issue POST", async () => {
    const f = fixture();
    f.loseIssueResponse();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow();
    f.issues.splice(0);
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("ambiguous_issue_create");
    expect(f.issuePosts).toBe(1);
  });
  it("recovers a committed comment after lost response without duplicate followup", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.loseCommentResponse();
    const next = { ...root, messageTs: "102.000001" };
    await expect(
      routeSlackThreadEvent(next, f.config, f.fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(next, f.config, f.fetcher);
    expect(f.commentPosts).toBe(1);
  });
  it("recovers original root identity after KV mapping expires", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.config.store = new MemoryThreadStore();
    const result = await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001" },
      f.config,
      f.fetcher,
    );
    expect(result.action).toBe("comment_persisted");
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
  });
  it("does not POST while another worker owns the lock", async () => {
    const f = fixture();
    f.config.store.setIfAbsent = async () => false;
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("thread_lock_busy");
    expect(f.issuePosts).toBe(0);
  });
  it("retains both requests when a rejected first create is overtaken by a followup", async () => {
    const f = fixture();
    let reject = true;
    const fetcher: typeof fetch = async (input, init) => {
      if (reject && String(input).endsWith("/api/issues")) {
        reject = false;
        return Response.json({ error: "rate_limited" }, { status: 429 });
      }
      return f.fetcher(input, init);
    };
    await expect(
      routeSlackThreadEvent(root, f.config, fetcher),
    ).rejects.toThrow();
    await routeSlackThreadEvent(
      { ...root, messageTs: "102.000001", text: "<@U1> B" },
      f.config,
      fetcher,
    );
    await routeSlackThreadEvent(root, f.config, fetcher);
    expect(f.issuePosts).toBe(1);
    expect(f.commentPosts).toBe(1);
    expect(readTaskEnvelope(f.issues[0]!.description).eventPayload.text).toBe("<@U1> B");
    expect(readTaskEnvelope(f.comments[0]!.content).eventPayload.text).toBe("<@U1> test");
  });
  it("does not adopt a different configured Agent scope", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    await routeSlackThreadEvent(
      root,
      { ...f.config, multicaAgentId: "another-agent" },
      f.fetcher,
    );
    expect(f.issuePosts).toBe(2);
  });
  it("does not recover an Issue assigned to another Agent", async () => {
    const f = fixture();
    await routeSlackThreadEvent(root, f.config, f.fetcher);
    f.issues[0]!.assignee_id = "another-agent";
    f.config.store = new MemoryThreadStore();
    await expect(
      routeSlackThreadEvent(root, f.config, f.fetcher),
    ).rejects.toThrow("invalid_issue_scope");
    expect(f.issuePosts).toBe(1);
  });
});


it('freezes first nearby discussion across follow-ups, cache loss and out-of-order events',async()=>{
  const f=fixture();let text='original';let reads=0;
  f.config.readContext=async (e,options)=>{reads++;return {anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',timeline:{status:'complete',messages:[...(options?.includeNearby?[{ts:'99.000001',authorId:'U1',origin:'unknown' as const,text,files:[]}]:[]),{ts:e.threadTs,authorId:'U2',origin:'unknown',text:'root',files:[]}]}};};
  const decode=(s:string):any=>readTaskEnvelope(s);
  await routeSlackThreadEvent(root,f.config,f.fetcher);
  for(const ts of ['101.000001','102.000001'])await routeSlackThreadEvent({...root,messageTs:ts},f.config,f.fetcher);
  expect(f.comments.every(c=>decode(c.content).context.timeline.messages.length===2)).toBe(true);
  text='edited';f.loseCommentResponse();
  const e={...root,messageTs:'103.000001'};
  await expect(routeSlackThreadEvent(e,f.config,f.fetcher)).rejects.toThrow();
  const before=reads;await routeSlackThreadEvent(e,f.config,f.fetcher);expect(reads).toBe(before);
  expect(decode(f.comments.at(-1)!.content).context.timeline.messages[0].text).toBe('original');
  const fetcher:typeof fetch=async(input,init)=>{
    if(init?.method==='POST'){const value={id:'last',content:JSON.parse(String(init.body)).content};f.comments.push(value);return Response.json(value);}
    return f.fetcher(input,init);
  };
  await routeSlackThreadEvent({...root,messageTs:'104.000001'},f.config,fetcher);
  expect(decode(f.comments.at(-1)!.content).context.timeline.messages).toHaveLength(2);
  f.config.store=new MemoryThreadStore();
  await routeSlackThreadEvent({...root,messageTs:'102.500001'},f.config,fetcher);
  expect(decode(f.comments.at(-1)!.content).context.sinceTs).toBe('102.000001');
  await routeSlackThreadEvent({...root,messageTs:'105.000001'},f.config,fetcher);
  expect(decode(f.comments.at(-1)!.content).context.timeline.messages).toHaveLength(2);
  expect(decode(f.comments.at(-1)!.content).context.sinceTs).toBe('104.000001');
});

it('freezes the Agent configuration snapshot across retries and refreshes it for new messages',async()=>{
  const f=fixture();let calls=0;let model='model-one';
  const fetcher:typeof fetch=async(input,init)=>{
    if(String(input).includes('/api/agents/')){calls++;return Response.json({id:'agent',workspace_id:'ws',model,service_tier:'priority'});}
    return f.fetcher(input,init);
  };
  f.loseIssueResponse();
  await expect(routeSlackThreadEvent(root,f.config,fetcher)).rejects.toThrow();
  model='model-two';await routeSlackThreadEvent(root,f.config,fetcher);
  expect(calls).toBe(1);
  expect(readTaskEnvelope(f.issues[0]!.description).replyContext).toMatchObject({model:'model-one'});
  await routeSlackThreadEvent({...root,messageTs:'101.000001'},f.config,fetcher);
  expect(calls).toBe(2);
  expect(readTaskEnvelope(f.comments[0]!.content).replyContext).toMatchObject({model:'model-two'});
});

it('retains first sibling context without refreshing unrelated side replies',async()=>{
  const f=fixture();let added=false;
  f.config.readContext=async (e,options)=>({anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',timeline:{status:'complete',messages:[
    ...(options?.includeNearby?[
    {ts:'90.000001',authorId:'U1',origin:'unknown' as const,text:'side-root',files:[],replies:{status:'complete' as const,messages:[
      {ts:'91.000001',authorId:'U1',origin:'unknown' as const,text:'old-reply',files:[]},
      ...(added?[{ts:'100.500001',authorId:'U1',origin:'unknown' as const,text:'new-reply',files:[]}]:[])
    ]}}]:[]),
    {ts:e.threadTs,authorId:'U2',origin:'unknown',text:'current-root',files:[]}
  ]}});
  await routeSlackThreadEvent(root,f.config,f.fetcher);added=true;
  await routeSlackThreadEvent({...root,messageTs:'101.000001'},f.config,f.fetcher);
  const second=readTaskEnvelope(f.comments[0]!.content) as any;
  expect(second.context.timeline.messages[0].replies.messages.map((m:any)=>m.text)).toEqual(['old-reply']);
  expect(second.context.initialCutoffTs).toBe(root.messageTs);
  await routeSlackThreadEvent({...root,messageTs:'102.000001'},f.config,f.fetcher);
  expect((readTaskEnvelope(f.comments[1]!.content) as any).context.timeline.messages).toHaveLength(2);
});


it('logs content-free clipping measurements',async()=>{
  const f=fixture();const info=vi.spyOn(console,'info').mockImplementation(()=>{});
  try {
    f.config.readContext=async e=>({anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',readStats:{slackCalls:3,rawMessages:5,messageReadMs:7,nameLookupCalls:2,nameReadMs:3},timeline:{status:'complete',messages:[{ts:e.threadTs,authorId:'U2',origin:'unknown',text:'PRIVATE_BODY_MARKER',files:[]}]}});
    await routeSlackThreadEvent(root,f.config,f.fetcher);
    const row=info.mock.calls.find(c=>c[0]==='relay_context')![1] as Record<string,unknown>;
    expect(row).toMatchObject({slackCalls:3,rawMessages:5,candidateRoots:1,candidateMessages:1,retainedRoots:1,retainedMessages:1,omittedMessages:0});
    expect(row).toMatchObject({messageReadMs:7,nameLookupCalls:2,nameReadMs:3});
    expect(row.agentConfigMs).toBeGreaterThanOrEqual(0);expect(row.envelopeBytes).toBeGreaterThan(0);
    const log=JSON.stringify(row);expect(log).not.toContain('PRIVATE_BODY_MARKER');expect(log).not.toContain(root.text);expect(log).not.toContain('multicaApiToken');
  } finally {info.mockRestore();}
});

it('advances the interval only after confirmed persistence, not after a rejected prepared comment',async()=>{
  const f=fixture();const bounds:(string|undefined)[]=[];
  f.config.readContext=async(e,options)=>{
    bounds.push(options?.sinceTs);
    return {anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',timeline:{status:'complete',messages:[]}};
  };
  let reject=true;
  const fetcher:typeof fetch=async(input,init)=>{
    if(reject&&String(input).includes('/comments')&&init?.method==='POST'){
      reject=false;return new Response('',{status:429});
    }
    return f.fetcher(input,init);
  };
  await routeSlackThreadEvent(root,f.config,fetcher);
  const failed={...root,messageTs:'101.000001'};
  await expect(routeSlackThreadEvent(failed,f.config,fetcher)).rejects.toThrow('multica_http_error');
  await routeSlackThreadEvent({...root,messageTs:'102.000001'},f.config,fetcher);
  expect(bounds).toEqual([undefined,root.messageTs,root.messageTs]);
  await routeSlackThreadEvent(failed,f.config,fetcher); // frozen earlier event; no new context read
  expect(bounds).toHaveLength(3);
  await routeSlackThreadEvent({...root,messageTs:'103.000001'},f.config,fetcher);
  expect(bounds.at(-1)).toBe('102.000001');
});

it.each([false,true])('preserves initial background for an earlier late mention (cache lost: %s), without future content',async(lost)=>{
  const f=fixture();
  const node=(ts:string,text:string)=>({ts,authorId:'U1',origin:'unknown' as const,text,files:[]});
  const reader=vi.fn<ThreadRouterConfig['readContext']>().mockImplementation(async(e,options)=>({anchorTs:e.threadTs,cutoffTs:e.messageTs,capturedAt:'fixed',timeline:{status:'complete',messages:[
    ...(options?.includeNearby?[
      {...node('99.000001','initial background'),replies:{status:'complete' as const,messages:[node('99.500001','past reply'),node('101.500001','future reply')]}},
      node('101.600001','future root'),
    ]:[]),
    {...node(e.threadTs,'root'),replies:{status:'complete',messages:[node(e.messageTs,e.text)]}},
  ]}}));
  f.config.readContext=reader;
  await routeSlackThreadEvent({...root,messageTs:'102.000001'},f.config,f.fetcher);
  if(lost)f.config.store=new MemoryThreadStore();
  await routeSlackThreadEvent({...root,messageTs:'101.000001'},f.config,f.fetcher);
  const late=readTaskEnvelope(f.comments[0]!.content).context as import('../src/context-envelope.js').ThreadContext;
  expect(late.initialCutoffTs).toBe('102.000001');expect(late.sinceTs).toBeUndefined();
  expect(late.timeline.messages.map(m=>m.text)).toEqual(['initial background','root']);
  expect(late.timeline.messages[0]!.replies!.messages.map(m=>m.text)).toEqual(['past reply']);
  expect(late.timeline.messages[0]!.replies!.reason).toBe('request_cutoff');
  expect(reader.mock.calls[1]![1]).toMatchObject({includeNearby:false,sinceTs:undefined});
  const count=reader.mock.calls.length;
  await routeSlackThreadEvent({...root,messageTs:'101.000001'},f.config,f.fetcher);
  expect(reader).toHaveBeenCalledTimes(count);
  await routeSlackThreadEvent({...root,messageTs:'103.000001'},f.config,f.fetcher);
  const next=readTaskEnvelope(f.comments[1]!.content).context as import('../src/context-envelope.js').ThreadContext;
  expect(next.initialCutoffTs).toBe('102.000001');expect(next.sinceTs).toBe('102.000001');
  expect(next.timeline.messages.some(m=>m.text==='future root')).toBe(true);
  expect(next.timeline.messages[0]!.replies!.messages.map(m=>m.text)).toEqual(['past reply','future reply']);
  expect(next.timeline.messages.find(m=>m.ts===root.threadTs)!.replies!.messages[0]).toMatchObject({ts:'102.000001',text:root.text,restoredFrom:'persisted_request'});
});

it('does not treat failed baseline recovery as proof that there is no previous mention',async()=>{
  const f=fixture();await routeSlackThreadEvent(root,f.config,f.fetcher);
  f.config.store=new MemoryThreadStore();
  const reader=vi.fn(f.config.readContext);f.config.readContext=reader;
  let lookups=0;
  const fetcher:typeof fetch=async(input,init)=>{
    if(String(input).includes('/comments')&&init?.method!=='POST'&&++lookups===2)return new Response('',{status:503});
    return f.fetcher(input,init);
  };
  await expect(routeSlackThreadEvent({...root,messageTs:'103.000001'},f.config,fetcher)).rejects.toThrow('multica_http_error');
  expect(reader).not.toHaveBeenCalled();expect(f.commentPosts).toBe(0);
});


it('keeps frozen reply context across a multi-day offline interval in durable mode',async()=>{
  vi.useFakeTimers();
  try {
    const f=fixture();f.config.durableScheduler=true;
    await routeSlackThreadEvent(root,f.config,f.fetcher);
    const key='relay:'+digest('ws:project:agent')+':message:'+digest(messageKey(root))+':envelope';
    const before=await f.config.store.get(key);expect(before).not.toBeNull();
    vi.setSystemTime(Date.now()+2*86400000);
    expect(await f.config.store.get(key)).toBe(before);
    vi.setSystemTime(Date.now()+6*86400000);
    expect(await f.config.store.get(key)).toBeNull();
  } finally {vi.useRealTimers();}
});
