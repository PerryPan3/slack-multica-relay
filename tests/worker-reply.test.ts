import { describe, expect, it } from "vitest";
import type { RelayConfig } from "../src/config.js";
import { agentSlackText, deliverReply, replyJob } from "../src/worker-reply.js";
import { digest } from "../src/thread-router.js";
import { createHmac } from "node:crypto";
import { agentBody } from "../src/slack-message-text.js";

const config = {
  teamId: "T1",
  slackContextToken:"user-test",
  allowedChannelIds: new Set(["D1"]),
  allowedSenderIds: new Set(["U1"]),
  targetUserIds: new Set(["U1"]),
  blockedSenderIds: new Set(),
  blockedChannelIds: new Set(),
  multicaWorkspaceId: "workspace",
  multicaProjectId: "project",
  multicaAgentId: "agent",
  kvRestApiUrl: "https://kv.test",
  multicaApiBaseUrl: "https://tasks.test",
  queueUrl: "https://queue.test",
  consumerUrl: "https://relay.test/api/queue/consume",
} as RelayConfig;
const event = {
  teamId: "T1",
  channelId: "D1",
  senderUserId: "U1",
  messageTs: "10.001",
  threadTs: "10.001",
  text: "<@U1> test",
  mention: { type: "user" as const, id: "U1" },
};
const issueId = "00000000-0000-0000-0000-000000000001";

it("delivers a valid Chinese answer whose signed transport exceeds the display limit",async()=>{
  const f=fixture(event,"user-test","intent1.fixture");
  const packet={text:"已核查。".repeat(2500),seconds:42,tools:[],reviews:[]};
  const body=Buffer.from(JSON.stringify(packet)).toString("base64url");
  f.state.output="relay-agent/1 "+body+"."+createHmac("sha256","key").update("agent-result/1:intent1.fixture:"+body).digest("hex");
  expect(f.state.output.length).toBeGreaterThan(32000);
  expect(await deliverReply(f.job,config,f.send,undefined,undefined,"key")).toBe("sent");
  expect(f.posted()?.text).toContain(packet.text);
});
function fixture(source = event, readIdentity = source.channelId.startsWith("D") ? "user-test" : "bot-test", ticket="ticket") {
  const event = source;
  const job = replyJob(event, issueId),
    scope = digest("workspace:project:agent");
  const values = new Map([
    ["relay:" + scope + ":thread:" + job.thread, JSON.stringify({ issueId })],
    [
      "relay:" + scope + ":message:" + job.message + ":envelope",
      JSON.stringify({ eventPayload: event, context:{githubReviewTicket:ticket} }),
    ],
  ]);
  let posted: Record<string, unknown> | undefined;
  const state = {
    posts: 0,
    sourceText: event.text,
    runStatus: "completed",
    loseResponse: false,
    queued: 0,
    output: "test reply",
  };
  const send: typeof fetch = async (url, init) => {
    const endpoint = String(url);
    if (endpoint === config.kvRestApiUrl) {
      const [cmd, key, value, ...args] = JSON.parse(String(init?.body));
      if (cmd === "GET")
        return Response.json({ result: values.get(key) ?? null });
      if (cmd === "SET") {
        if (args.includes("NX") && values.has(key))
          return Response.json({ result: null });
        values.set(key, value);
        return Response.json({ result: "OK" });
      }
    }
    if (endpoint.endsWith("/task-runs"))
      return Response.json([
        {
          id: "run",
          workspace_id: "workspace",
          issue_id: issueId,
          agent_id: "agent",
          status: state.runStatus,
          result: { output: state.output },
          usage: [{model:"test-model"}],
        },
      ]);
    if (endpoint.endsWith("/issues/" + issueId))
      return Response.json({
        id: issueId,
        title: "request",
        identifier: "TASK-6",
        project_id: "project",
        assignee_type: "agent",
        assignee_id: "agent",
        description:
          "<!-- relay-thread:" +
          scope +
          ":" +
          job.thread +
          " -->\n" +
          JSON.stringify({ eventPayload: event }),
      });
    if (endpoint.includes("/conversations.replies?")) {
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer " + readIdentity);
      return Response.json({
        ok: true,
        messages: [
          { ts: event.threadTs, user: event.senderUserId, text: state.sourceText },
          ...(event.messageTs!==event.threadTs ? [{ts:event.messageTs,thread_ts:event.threadTs,user:event.senderUserId,text:state.sourceText}] : []),
          ...(posted ? [posted] : []),
        ],
      });
    }
    if (endpoint.endsWith("/chat.postMessage")) {
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer user-test");
      const body = JSON.parse(String(init?.body));
      expect(body.channel).toBe(event.channelId);
      expect(body.thread_ts).toBe(event.threadTs);
      state.posts++;
      posted = { ...body, ts: "11.001", user: "U1" };
      if (state.loseResponse) throw new Error("timeout");
      return Response.json({ ok: true, channel: event.channelId, ts: "11.001" });
    }
    if (endpoint.startsWith(config.queueUrl + "/")) {
      state.queued++;
      return Response.json({ messageId: "message" });
    }
    throw new Error("unexpected request");
  };
  return { state, job, send, posted:()=>posted };
}

describe("review footer on the real Worker delivery path", () => {
  function receipt(status:string) {
    const payload=Buffer.from(JSON.stringify({repo:"team/repo",number:19,review:status==="INCOMPLETE"?null:123,
      sha:"a".repeat(40),seconds:145,text:status==="INCOMPLETE"?"暂缓审批\n字段查询未完成":"已 Approve\n上轮问题已修复",
      status,branch:"fix/review",tools:[{name:"GitHub",count:12,failed:0},{name:"Redshift",count:3,failed:status==="INCOMPLETE"?1:0}]})).toString("base64url");
    return "relay-review/2 "+payload+"."+createHmac("sha256","key").update("receipt:ticket:"+payload).digest("hex");
  }
  it.each(["APPROVE","INCOMPLETE"])("keeps %s evidence counts in context blocks with truthful links", async status=>{
    const f=fixture(); f.state.output=receipt(status);
    expect(await deliverReply(f.job,config,f.send,undefined,undefined,"key","perry")).toBe("sent");
    const message=f.posted() as {blocks:{type:string;text?:{text:string};elements?:{text:string}[]}[];text:string};
    const body=message.blocks.filter(b=>b.type==="section").map(b=>b.text!.text).join("");
    const footer=message.blocks.filter(b=>b.type==="context").map(b=>b.elements![0].text).join("\n");
    expect(body).not.toContain("Redshift");
    expect(footer).toContain("⏱ 2m 25s · 🤖 test-model");
    expect(footer).toContain("Redshift ×3");
    expect(footer).toContain("TASK-6");
    expect(footer).toContain("`fix/review`");
    expect(footer.includes("pullrequestreview-123")).toBe(status!=="INCOMPLETE");
    expect(footer.includes("1 次未取得结果")).toBe(status==="INCOMPLETE");
    expect(message.text).toContain("Redshift ×3");
    expect(agentBody({...message,app_id:"A1"})).toBe(body);
    expect(await deliverReply(f.job,config,f.send,undefined,undefined,"key","perry")).toBe("already_sent");
    expect(f.state.posts).toBe(1);
  });
  it("does not send an unverifiable review receipt",async()=>{
    const f=fixture(); f.state.output=receipt("APPROVE");
    expect(await deliverReply(f.job,config,f.send,undefined,undefined,"wrong","perry")).toBe("invalid_review_receipt");
    expect(f.state.posts).toBe(0);
  });
});
describe("Worker self-DM replies", () => {
  it("checks delivery in the original thread and deduplicates repeated jobs", async () => {
    const { state, job, send } = fixture();
    expect(await deliverReply(job, config, send)).toBe("sent");
    expect(await deliverReply(job, config, send)).toBe("already_sent");
    expect(state.posts).toBe(1);
  });
  it("recovers a lost response by reading the thread", async () => {
    const { state, job, send } = fixture();
    state.loseResponse = true;
    await expect(deliverReply(job, config, send)).rejects.toThrow("timeout");
    expect(await deliverReply(job, config, send)).toBe("sent");
    expect(state.posts).toBe(1);
  });
  it("rejects a changed source and a changed channel policy", async () => {
    const { state, job, send } = fixture();
    state.sourceText = "edited";
    expect(await deliverReply(job, config, send)).toBe("source_changed");
    expect(
      await deliverReply(
        job,
        { ...config, allowedChannelIds: new Set(["D2"]) },
        send,
      ),
    ).toBe("reply_scope_rejected");
    expect(state.posts).toBe(0);
  });
  it("defers a running task and bounds attempts", async () => {
    const { state, job, send } = fixture();
    state.runStatus = "running";
    expect(await deliverReply(job, config, send)).toBe("waiting");
    expect(await deliverReply({ ...job, attempt: 180 }, config, send)).toBe(
      "reply_wait_expired",
    );
    expect(state.posts).toBe(0);
    expect(state.queued).toBe(1);
    expect(await deliverReply({ ...job, attempt: 90 }, config, send)).toBe("waiting");
  });
});

it("reads the approved channel with the app identity and replies as the owner", async () => {
  const scoped = {...config, allowedChannelIds: new Set(["D1","C2"])};
  const {job,send,state} = fixture({...event,channelId:"C2"});
  expect(await deliverReply(job,scoped,send)).toBe("reply_scope_rejected");
  expect(await deliverReply(job,scoped,send,"bot-test")).toBe("sent");
  expect(state.posts).toBe(1);
  const foreign = fixture({...event,channelId:"C3"});
  expect(await deliverReply(foreign.job,scoped,foreign.send,"bot-test")).toBe("reply_scope_rejected");
  const colleague = fixture({...event,channelId:"C2",senderUserId:"U2"});
  expect(await deliverReply(colleague.job,scoped,colleague.send,"bot-test")).toBe("reply_scope_rejected");
});

describe("channel-specific trigger permissions", () => {
  const scoped = {...config, allowedChannelIds:new Set(["D1","C2","C3"])};
  const grants = "C2:U2,C2:U3";
  it.each([
    ["C2","U2","sent"], ["C2","U3","sent"], ["C2","U4","reply_scope_rejected"],
    ["C3","U2","reply_scope_rejected"], ["D1","U3","reply_scope_rejected"], ["D1","U1","sent"],
  ])("checks the trigger and verifies the owner's reply for %s / %s",async (channelId,senderUserId,want) => {
    const f = fixture({...event,channelId,senderUserId});
    expect(await deliverReply(f.job,scoped,f.send,"bot-test",grants)).toBe(want);
    expect(f.state.posts).toBe(want === "sent" ? 1 : 0);
  });
  it("honors a sender block even with a channel grant",async () => {
    const f = fixture({...event,channelId:"C2",senderUserId:"U2"});
    expect(await deliverReply(f.job,{...scoped,blockedSenderIds:new Set(["U2"])},f.send,"bot-test",grants)).toBe("reply_scope_rejected");
  });
});

it.each(["D9","G9","C9"])("uses the owner's identity for a self-assignment in %s",async(channelId)=>{
  const scoped = {...config,selfAssignScope:"all" as const};
  const owner = fixture({...event,channelId},"user-test");
  expect(await deliverReply(owner.job,scoped,owner.send,"bot-test","C1:U2")).toBe("sent");
  const other = fixture({...event,channelId,senderUserId:"U2"});
  expect(await deliverReply(other.job,scoped,other.send,"bot-test","C1:U2")).toBe("reply_scope_rejected");
});

it("replies to a colleague's granted DM thread and rejects revoked or mismatched grants",async()=>{
  const scoped={...config,allowedChannelIds:new Set(["D1","D2","D3"])};
  const grants="D2:U2,D3:U3";
  const source={...event,channelId:"D2",senderUserId:"U2",threadTs:"9.001"};
  const packet={text:"已核实",seconds:1,tools:[{name:"Jira",count:1,failed:0}],reviews:[]};
  const body=Buffer.from(JSON.stringify(packet)).toString("base64url");
  for(const [change,policy,want] of [
    [{},grants,"sent"], [{senderUserId:"U3"},grants,"reply_scope_rejected"],
    [{channelId:"D3"},grants,"reply_scope_rejected"], [{},"D3:U3","reply_scope_rejected"],
  ] as const){
    const f=fixture({...source,...change},"user-test","intent1.fixture");
    f.state.output="relay-agent/1 "+body+"."+createHmac("sha256","key").update("agent-result/1:intent1.fixture:"+body).digest("hex");
    expect(await deliverReply(f.job,scoped,f.send,"bot-test",policy,"key")).toBe(want);
    expect(f.state.posts).toBe(want==="sent"?1:0);
    if(want==="sent") expect(f.posted()).toMatchObject({channel:"D2",thread_ts:"9.001"});
  }
  const blocked=fixture(source);
  expect(await deliverReply(blocked.job,{...scoped,blockedChannelIds:new Set(["D2"])},blocked.send,"bot-test",grants))
    .toBe("reply_scope_rejected");
});

it("posts one truthful notice if a semantic task exhausts the wait window",async()=>{
  const f=fixture(event,"user-test","intent1.test");
  f.state.runStatus="running";
  const job={...f.job,attempt:180};
  expect(await deliverReply(job,config,f.send)).toBe("sent");
  expect(f.posted()?.text).toContain("尚未收到完成回执");
  expect(await deliverReply(job,config,f.send)).toBe("already_sent");
});

it("binds a business result to its request, original self-thread and currently granted channel",async()=>{
  const f=fixture(event,"user-test","intent1.fixture");
  const body=Buffer.from(JSON.stringify({text:"只读排查：已核实",seconds:12,tools:[{name:"Jira",count:1,failed:0}]})).toString("base64url");
  f.state.output="relay-business/1 "+body+"."+createHmac("sha256","key").update("business-result/1:intent1.fixture:"+body).digest("hex");
  expect(await deliverReply(f.job,config,f.send,undefined,undefined,"key","perry",{})).toBe("reply_scope_rejected");
  expect(f.state.posts).toBe(0);
  expect(await deliverReply(f.job,config,f.send,undefined,undefined,"wrong","perry",{businessChannel:"C2",businessTestDm:"D1"})).toBe("invalid_business_receipt");
  expect(await deliverReply(f.job,config,f.send,undefined,undefined,"key","perry",{businessChannel:"C2",businessTestDm:"D1"})).toBe("sent");
  expect(f.posted()?.text).toContain("Jira ×1");
  expect(f.posted()?.text).not.toContain("GitHub 审批");
});

it("delivers an additional channel's business result only while its exact sender grant remains active",async()=>{
  const scoped={...config,allowedChannelIds:new Set(["D1","C2","C3"])};
  const grants="C2:U2,C2:U3,C3:U3";
  const body=Buffer.from(JSON.stringify({text:"已核实业务问题",seconds:12,tools:[{name:"Jira",count:1,failed:0}]})).toString("base64url");
  for(const sender of ["U2","U3"]){
    const f=fixture({...event,channelId:"C3",senderUserId:sender},"bot-test","intent1.fixture");
    f.state.output="relay-business/1 "+body+"."+createHmac("sha256","key").update("business-result/1:intent1.fixture:"+body).digest("hex");
    expect(await deliverReply(f.job,scoped,f.send,"bot-test",grants,"key","perry",{businessGrants:grants}))
      .toBe(sender==="U3"?"sent":"reply_scope_rejected");
    if(sender==="U3") expect(f.posted()?.thread_ts).toBe(event.threadTs);
  }
  const revoked=fixture({...event,channelId:"C3",senderUserId:"U3"},"bot-test","intent1.fixture");
  revoked.state.output="relay-business/1 "+body+"."+createHmac("sha256","key").update("business-result/1:intent1.fixture:"+body).digest("hex");
  expect(await deliverReply(revoked.job,scoped,revoked.send,"bot-test",grants,"key","perry",{businessGrants:"C2:U3"})).toBe("reply_scope_rejected");
  expect(revoked.state.posts).toBe(0);
});


it("renders native tools and model in the footer without activating injected mentions",async()=>{
  const f=fixture(event,"user-test","intent1.fixture");
  const packet={text:"**Done** [review](https://github.com/team/repo/pull/1) <@U2> <!channel>",seconds:42,
    tools:[{name:"Redshift",count:2,failed:0}],models:["host-model"],reviews:[]};
  const body=Buffer.from(JSON.stringify(packet)).toString("base64url");
  f.state.output="relay-agent/1 "+body+"."+createHmac("sha256","key").update("agent-result/1:intent1.fixture:"+body).digest("hex");
  expect(await deliverReply(f.job,config,f.send,undefined,undefined,"key","test",{})).toBe("sent");
  const message=f.posted() as {blocks:Array<{type:string;text?:{type:string;text:string};elements?:unknown[]}>};
  expect(message.blocks[0].text?.type).toBe("mrkdwn");
  expect(message.blocks[0].text?.text).toContain("*Done*");
  expect(message.blocks[0].text?.text).toContain("&lt;@U2&gt;");
  expect(message.blocks[0].text?.text).toContain("&lt;!channel&gt;");
  expect(JSON.stringify(message.blocks.filter(x=>x.type==="context"))).toContain("host-model");
  expect(agentSlackText("[PR](https://github.com/t/r/pull/1)")).toBe("<https://github.com/t/r/pull/1|PR>");
});
