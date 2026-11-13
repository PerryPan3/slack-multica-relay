import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { acceptSlack, consumeQueue, consumerFailure, recoverConfiguredMessages } from "../src/http.js";
import { readContext } from "../src/slack-context.js";
import { messageFingerprint } from "../src/context-envelope.js";
vi.mock("@upstash/qstash", () => ({
  Receiver: class {
    verify = vi.fn().mockResolvedValue(true);
  },
}));
const env = {
  SLACK_SIGNING_SECRET: "test",
  SLACK_TEAM_ID: "T1",
  SLACK_TARGET_USER_IDS: "U1",
  SLACK_ALLOWED_CHANNEL_IDS: "C1",
  MULTICA_API_BASE_URL: "https://multica.test",
  MULTICA_API_TOKEN: "test",
  MULTICA_WORKSPACE_ID: "ws",
  MULTICA_PROJECT_ID: "project",
  MULTICA_AGENT_ID: "agent",
  SLACK_USER_TOKEN: "test",
  SLACK_CONTEXT_TOKEN: "test",
  SLACK_REACTION_NAME: "eyes",
  KV_REST_API_URL: "https://kv.test",
  KV_REST_API_TOKEN: "test",
  QSTASH_TOKEN: "test",
  QSTASH_CURRENT_SIGNING_KEY: "test",
  QSTASH_NEXT_SIGNING_KEY: "test",
  RELAY_CONSUMER_URL: "https://relay.test/api/queue/consume",
};
function request(event: unknown, teamId = "T1"): Request {
  const body = JSON.stringify({
      type: "event_callback",
      team_id: teamId,
      event,
    }),
    ts = String(Math.floor(Date.now() / 1000));
  return new Request("https://relay.test/api/slack/events", {
    method: "POST",
    headers: {
      "x-slack-request-timestamp": ts,
      "x-slack-signature":
        "v0=" +
        createHmac("sha256", "test")
          .update("v0:" + ts + ":" + body)
          .digest("hex"),
    },
    body,
  });
}
const event = {
  type: "message",
  channel: "C1",
  user: "U2",
  ts: "100.000001",
  text: "<@U1> test",
};
afterEach(() => vi.restoreAllMocks());
describe("durable admission", () => {
  it('moves existing verified QStash deliveries into durable storage without executing them',async()=>{
    const send=vi.fn<typeof fetch>(async(url,init)=>{
      expect(String(url)).toBe(env.KV_REST_API_URL);
      expect(JSON.parse(String(init?.body))[0]).toBe('EVAL');
      return Response.json({result:1});
    });
    const body=JSON.stringify({kind:'thread_reply',issueId:'i',attempt:3});
    const response=await consumeQueue(new Request(env.RELAY_CONSUMER_URL,{method:'POST',body}),{...env,RELAY_DURABLE_SCHEDULER:'true'},send);
    expect(await response.json()).toEqual({action:'accepted',transport:'redis'});
    expect(send).toHaveBeenCalledTimes(1);
  });
  it("acknowledges only persisted Redis jobs and does not consume QStash quota",async()=>{
    const send=vi.fn<typeof fetch>(async(url,init)=>{
      expect(String(url)).toBe(env.KV_REST_API_URL);
      const command=JSON.parse(String(init?.body));expect(command[0]).toBe('EVAL');
      expect(JSON.parse(command[5]).payload.text).toBe(event.text);
      return Response.json({result:1});
    });
    expect((await acceptSlack(request(event),{...env,RELAY_DURABLE_SCHEDULER:'true'},send)).status).toBe(200);
    expect(send).toHaveBeenCalledTimes(1);
    const broken=vi.fn<typeof fetch>().mockResolvedValue(Response.json({}, {status:503}));
    expect((await acceptSlack(request(event),{...env,RELAY_DURABLE_SCHEDULER:'true'},broken)).status).toBe(503);
  });
  it("recovers a missed Slack message from Slack itself and rechecks sender grants",async()=>{
    for(const user of ['U2','U3']){
      const calls:unknown[][]=[];
      const send:typeof fetch=async(url,init)=>{
        if(String(url).startsWith('https://slack.com/api/'))return Response.json({ok:true,messages:[{...event,user}]});
        const command=JSON.parse(String(init?.body));calls.push(command);
        return Response.json({result:command[0]==='GET'?null:1});
      };
      const settings={...env,SLACK_ALLOWED_SENDER_IDS:'U1',SLACK_CHANNEL_SENDER_GRANTS:'C1:U2',SLACK_CHANNEL_READ_TOKEN:'test',RELAY_RECOVERY_MESSAGES:'C1:'+event.ts};
      if(user==='U2'){
        await recoverConfiguredMessages(settings,send);
        expect(calls.some(c=>c[0]==='EVAL')).toBe(true);
      }else{
        await expect(recoverConfiguredMessages(settings,send)).rejects.toThrow('recovery_source_rejected');
        expect(calls.some(c=>c[0]==='EVAL')).toBe(false);
      }
    }
  });

  it.each(["invalid_event","invalid_thread_state","ambiguous_issue_mapping","invalid_issue_scope","invalid_context_scope",
    "context_request_too_large","task_presentation_too_large","comment_lookup_limit"])("acknowledges permanent consumer rejection %s",async(code)=>{
    const response=consumerFailure(code);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({action:"rejected",error:code,retryable:false});
  });
  it("verifies the signed Slack challenge before the task backend is configured", async () => {
    const body = JSON.stringify({ type: "url_verification", challenge: "slack-test" });
    const timestamp = String(Math.floor(Date.now() / 1000));
    const signed = new Request("https://relay.test/api/slack/events", {
      method: "POST",
      headers: {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": "v0=" + createHmac("sha256", env.SLACK_SIGNING_SECRET)
          .update(`v0:${timestamp}:${body}`).digest("hex"),
      },
      body,
    });
    const settings = { SLACK_SIGNING_SECRET: env.SLACK_SIGNING_SECRET };
    const publish = vi.fn<typeof fetch>();
    const wrong = signed.clone();
    wrong.headers.delete("x-slack-signature");
    expect((await acceptSlack(wrong, settings, publish)).status).toBe(401);
    expect(await (await acceptSlack(signed, settings, publish)).json()).toEqual({ challenge: "slack-test" });
    expect((await acceptSlack(request(event), settings, publish)).status).toBe(500);
    expect(publish).not.toHaveBeenCalled();
  });
  it("keeps transient consumer failures retryable",async()=>{
    expect(consumerFailure("timeout").status).toBe(503);
  });
  it("acknowledges a signed but invalid queued event without retrying",async()=>{
    const response=await consumeQueue(new Request(env.RELAY_CONSUMER_URL,{method:"POST",body:"{}"}),env,vi.fn<typeof fetch>());
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({action:"rejected",error:"invalid_event",retryable:false});
  });
  it("only publishes to queue before acknowledging", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(request(event), env, fetcher);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      action: "accepted",
      queueMessageId: "msg",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0]![0])).toContain(
      "/v2/publish/https://relay.test/api/queue/consume",
    );
  });
  it("admits a trusted user app only within the existing sender and channel grants", async () => {
    const settings={...env,SLACK_ALLOWED_SENDER_IDS:'U1',SLACK_CHANNEL_SENDER_GRANTS:'C1:U2',SLACK_ALLOWED_USER_APP_IDS:'ATRUSTED'};
    const send=vi.fn<typeof fetch>().mockResolvedValue(Response.json({messageId:'app-message'}));
    const response=await acceptSlack(request({...event,user:'U2',app_id:'ATRUSTED'}),settings,send);
    expect(await response.json()).toMatchObject({action:'accepted'});
    expect(JSON.parse(String(send.mock.calls[0]![1]?.body))).toMatchObject({senderUserId:'U2',sourceAppId:'ATRUSTED'});
    for(const change of [{user:'U3'},{channel:'COTHER'},{app_id:'AOTHER'},{bot_id:'BAGENT'},
      {bot_profile:{id:'BAGENT'}},{subtype:'bot_message'},
      {blocks:[{block_id:'relay-delivery-'+ 'a'.repeat(64)}]}]){
      const blocked=vi.fn<typeof fetch>();
      await acceptSlack(request({...event,user:'U2',app_id:'ATRUSTED',...change}),settings,blocked);
      expect(blocked).not.toHaveBeenCalled();
    }
  });
  it("rejects a queued user-app request after trust is removed",async()=>{
    const source={teamId:'T1',channelId:'C1',senderUserId:'U1',messageTs:'1.000001',threadTs:'1.000001',text:'<@U1> review',mention:{type:'user',id:'U1'},sourceAppId:'ATRUSTED'};
    const send=vi.fn<typeof fetch>();
    const response=await consumeQueue(new Request(env.RELAY_CONSUMER_URL,{method:'POST',body:JSON.stringify(source)}),env,send);
    expect(await response.json()).toMatchObject({action:'ignored',reason:'policy_changed'});
    expect(send).not.toHaveBeenCalled();
  });
  it("projects attachment metadata before publishing to QStash and hashes log identity", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
    const files=[{id:"F1",name:"photo.png",mimetype:"image/png",size:12,
      url_private:"https://private.test/file",thumb_360:"https://private.test/thumb",shares:{private:{}}},
      ...Array.from({length:5},(_,i)=>({id:`F${i+2}`,name:`${i+2}.png`,mimetype:"image/png"}))];
    await acceptSlack(request({...event,files}),env,fetcher);
    const queued=JSON.parse(String(fetcher.mock.calls[0]![1]?.body));
    expect(queued.files).toHaveLength(6);expect(queued.files[0]).toEqual({id:"F1",name:"photo.png",mime:"image/png",size:12,contentStatus:"not_loaded"});
    expect(queued.filesTruncated).toBeUndefined();
    expect(queued.sourceFingerprint).toMatch(/^[a-f0-9]{64}$/u);
    expect(JSON.stringify(queued)).not.toContain("private.test");
    const initial=await readContext(queued,"test",async()=>Response.json({ok:true,messages:[]}));
    const followup={...queued,messageTs:"101.000001",text:"<@U1> next",files:undefined,filesTruncated:undefined,sourceFingerprint:undefined};
    const refreshed=await readContext(followup,"test",async input=>String(input).includes("history")
      ? Response.json({ok:true,messages:[]})
      : Response.json({ok:true,messages:[{...event,files},{...event,ts:"101.000001",thread_ts:event.ts,text:"<@U1> next"}]}));
    expect(messageFingerprint(initial.timeline.messages[0]!)).toBe(messageFingerprint(refreshed.timeline.messages[0]!));
    const logged=JSON.stringify(info.mock.calls);
    expect(logged).not.toContain("T1:C1:100.000001");
    expect(logged).toContain("eventId");
  });
  it.each([
    { channel: "C2" },
    { user: undefined },
    { text: "ordinary", thread_ts: "1.000001" },
    { bot_id: "B1" },
    { app_id: "A1" },
    { bot_profile: { id: "B1" } },
    { subtype: "message_changed" },
  ])("no queue side effects for %j", async (change) => {
    const fetcher = vi.fn<typeof fetch>();
    await acceptSlack(request({ ...event, ...change }), env, fetcher);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it.each([
    ["DSELF", "U1", true],
    ["COTHER", "U1", false],
    ["DSELF", "U2", false],
  ] as const)("checks self-DM channel %s and sender %s", async (channel, user, allowed) => {
    const settings = { ...env, SLACK_ALLOWED_CHANNEL_IDS: "DSELF", SLACK_ALLOWED_SENDER_IDS: "U1" };
    const publish = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(request({ ...event, channel, user }), settings, publish);
    expect((await response.json()).action).toBe(allowed ? "accepted" : "ignored");
    expect(publish).toHaveBeenCalledTimes(allowed ? 1 : 0);
  });
  it("rejects another Slack team", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptSlack(request(event, "T2"), env, f);
    expect(f).not.toHaveBeenCalled();
  });
  it("accepts all as an explicit channel allowlist", async () => {
    const f = vi
      .fn<typeof fetch>()
      .mockResolvedValue(Response.json({ messageId: "msg" }));
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      { ...env, SLACK_ALLOWED_CHANNEL_IDS: "all" },
      f,
    );
    expect(response.status).toBe(200);
    expect(f).toHaveBeenCalledTimes(1);
  });
  it.each([undefined, "", ", ,"])(
    "fails closed for a missing or empty channel allowlist %s",
    async (value) => {
      const f = vi.fn<typeof fetch>();
      const candidate = { ...env, SLACK_ALLOWED_CHANNEL_IDS: value };
      expect((await acceptSlack(request(event), candidate, f)).status).toBe(500);
      expect(f).not.toHaveBeenCalled();
    },
  );
  it("blocks a channel even when the allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request({ ...event, channel: "C2" }),
      {
        ...env,
        SLACK_ALLOWED_CHANNEL_IDS: "all",
        SLACK_BLOCKED_CHANNEL_IDS: "C2",
      },
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "not_allowed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it("applies sender policy", async () => {
    const f = vi.fn<typeof fetch>();
    await acceptSlack(
      request(event),
      { ...env, SLACK_ALLOWED_SENDER_IDS: "U3" },
      f,
    );
    expect(f).not.toHaveBeenCalled();
  });
  it("blocks a sender even when the sender allowlist is all", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(
      request(event),
      {
        ...env,
        SLACK_ALLOWED_SENDER_IDS: "all",
        SLACK_BLOCKED_SENDER_IDS: "U2",
      },
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "not_allowed",
    });
    expect(f).not.toHaveBeenCalled();
  });
  it("keeps Slack retry ownership when queue publish fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const f = vi.fn<typeof fetch>().mockRejectedValue(new Error("secret body"));
    const response = await acceptSlack(request(event), env, f);
    expect(response.status).toBe(503);
    expect(JSON.stringify(vi.mocked(console.warn).mock.calls)).not.toContain(
      "secret body",
    );
  });
  it("rejects invalid signature before network", async () => {
    const f = vi.fn<typeof fetch>();
    expect(
      (
        await acceptSlack(
          new Request("https://relay.test", { method: "POST", body: "{}" }),
          env,
          f,
        )
      ).status,
    ).toBe(401);
    expect(f).not.toHaveBeenCalled();
  });
  it("rechecks channel policy for queued messages", async () => {
    const f = vi.fn<typeof fetch>();
    const queued = {
      teamId: "T1",
      channelId: "C2",
      senderUserId: "U2",
      messageTs: "1.000001",
      threadTs: "1.000001",
      text: "<@U1> test",
      mention: { type: "user", id: "U1" },
    };
    const response = await consumeQueue(
      new Request(env.RELAY_CONSUMER_URL, {
        method: "POST",
        body: JSON.stringify(queued),
      }),
      env,
      f,
    );
    expect(await response.json()).toEqual({
      action: "ignored",
      reason: "policy_changed",
    });
    expect(f).not.toHaveBeenCalled();
  });
});

describe("取消指令的事件路由", () => {
  it("授权取消入队并保存指令类型", async () => {
    const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "cancel-msg" }));
    const response = await acceptSlack(request({ ...event, user: "U1", text: "<@U1> CANCEL", thread_ts: event.ts }), env, f);
    expect(response.status).toBe(200);
    expect(JSON.parse(String(f.mock.calls[0]![1]!.body))).toMatchObject({ operation: "cancel", senderUserId: "U1" });
  });
  it("非目标用户的取消指令不会入队或作为新任务处理", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await acceptSlack(request({ ...event, text: "<@U1> 取消" }), env, f);
    expect(await response.json()).toEqual({ action: "ignored", reason: "cancel_not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it("自定义关键词替换默认值，普通讨论仍入队为 dispatch", async () => {
    for (const [text, operation] of [["<@U1> stop", "cancel"], ["<@U1> cancel", "dispatch"], ["<@U1> 帮我取消", "dispatch"]]) {
      const f = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ messageId: "msg" }));
      await acceptSlack(request({ ...event, user: "U1", text }), { ...env, SLACK_CANCEL_KEYWORDS: "stop,停止" }, f);
      expect(JSON.parse(String(f.mock.calls[0]![1]!.body)).operation).toBe(operation);
    }
  });
  it("消费时重新核对取消身份，不能借队列绕过权限", async () => {
    const f = vi.fn<typeof fetch>();
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify({
      teamId: "T1", channelId: "C1", senderUserId: "U2", messageTs: "102.000001", threadTs: "100.000001",
      text: "<@U1> cancel", mention: { type: "user", id: "U1" }, operation: "cancel",
    }) }), env, f);
    expect(await response.json()).toEqual({ action: "ignored", reason: "cancel_not_allowed" });
    expect(f).not.toHaveBeenCalled();
  });
  it.each(["cancel", undefined])("消费取消 %s 不建卡、不加 reaction；已分类取消不随关键词变更变成任务", async (operation) => {
    const calls: string[] = [];
    const kv = new Map<string, string>();
    const f: typeof fetch = async (input, init) => {
      const url = String(input); calls.push(url);
      if (url === env.KV_REST_API_URL) {
        const [command, key, value] = JSON.parse(String(init?.body));
        if (command === "GET") return Response.json({ result: kv.get(key) ?? null });
        if (command === "SET") { kv.set(key, value); return Response.json({ result: "OK" }); }
        if (command === "EVAL") return Response.json({ result: 1 });
      }
      if (url.includes("/api/issues/search?")) return Response.json({ issues: [] });
      throw new Error("不应调用建卡或 reaction 接口");
    };
    const response = await consumeQueue(new Request(env.RELAY_CONSUMER_URL, { method: "POST", body: JSON.stringify({
      teamId: "T1", channelId: "C1", senderUserId: "U1", messageTs: "102.000001", threadTs: "100.000001",
      text: "<@U1> cancel", mention: { type: "user", id: "U1" }, operation,
    }) }), operation ? { ...env, SLACK_CANCEL_KEYWORDS: "stop" } : env, f);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ action: "ignored" });
    expect(calls.every((url) => url === env.KV_REST_API_URL || url.includes("/api/issues/search?"))).toBe(true);
  });
});

describe("owner self-assignment outside explicitly granted channels", () => {
  const settings = {...env, SLACK_SELF_ASSIGN_SCOPE:"all", SLACK_ALLOWED_SENDER_IDS:"U1", SLACK_CHANNEL_SENDER_GRANTS:"C1:U2"};
  it.each([
    ["D9","U1",true], ["G9","U1",true], ["C9","U1",true], ["C1","U2",true],
    ["D9","U2",false], ["G9","U2",false], ["C9","U2",false], ["C1","U9",false],
  ])("enforces the sender for %s / %s", async (channel,user,allowed) => {
    const publish = vi.fn<typeof fetch>().mockResolvedValue(Response.json({messageId:"test"}));
    await acceptSlack(request({...event,channel,user}),settings,publish);
    expect(publish).toHaveBeenCalledTimes(allowed ? 1 : 0);
  });
  it("requires an actual self-mention and honors exclusions",async () => {
    const publish = vi.fn<typeof fetch>();
    await acceptSlack(request({...event,channel:"C9",user:"U1",text:"Perry test"}),settings,publish);
    await acceptSlack(request({...event,channel:"C9",user:"U1"}),{...settings,SLACK_BLOCKED_CHANNEL_IDS:"C9"},publish);
    await acceptSlack(request({...event,channel:"C9",user:"U1"},"T9"),settings,publish);
    await acceptSlack(request({...event,channel:"C9",user:"U1"}),{...settings,SLACK_SELF_ASSIGN_SCOPE:"dm"},publish);
    expect(publish).not.toHaveBeenCalled();
  });
});
