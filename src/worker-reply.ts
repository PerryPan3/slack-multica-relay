import { businessPresentation, businessAllowed, type BusinessScope } from "./semantic-route.js";
import { genericReviewReply, reviewPresentation, reviewStatistics } from "./github-review.js";
import { compareTs } from "./context-envelope.js";
import type { RelayConfig } from "./config.js";
import { getIssue, listIssueRuns, findComment } from "./multica-api.js";
import { findTargetMention, allowedSourceApp, isSupportedMessage } from "./mentions.js";
import { readTaskEnvelope } from "./task-presentation.js";
import {
  digest,
  messageKey,
  threadKey,
  STATE_TTL_SECONDS,
  type SlackThreadEvent,
} from "./thread-router.js";
import { UpstashThreadStore } from "./thread-store.js";

const MAX_REPLY_ATTEMPTS = 180;

export function agentSlackText(text: string): string {
  return text.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/\[([^\]\n]+)\]\((https:\/\/[^\s()]+)\)/g,"<$2|$1>")
    .replace(/\*\*([^*\n]+)\*\*/g,(_,content:string)=>content.includes("`")?content:"*"+content+"*");
}

interface ReplyJob {
  kind: "thread_reply";
  issueId: string;
  message: string;
  thread: string;
  attempt: number;
}
export function replyJob(event: SlackThreadEvent, issueId: string): ReplyJob {
  return {
    kind: "thread_reply",
    issueId,
    message: digest(messageKey(event)),
    thread: digest(threadKey(event)),
    attempt: 0,
  };
}

export async function scheduleReply(
  job: ReplyJob,
  config: RelayConfig,
  send: typeof fetch,
) {
  const response = await send(
    config.queueUrl + "/v2/publish/" + config.consumerUrl,
    {
      method: "POST",
      headers: {
        authorization: "Bearer " + config.queueToken,
        "content-type": "application/json",
        "Upstash-Deduplication-Id": digest(
          [config.consumerUrl, job.issueId, job.message, job.attempt].join(":"),
        ),
        "Upstash-Delay": job.attempt < 4 ? "6s" : "20s",
        "Upstash-Retries": "3",
      },
      body: JSON.stringify(job),
    },
  );
  if (!response.ok || !(await response.json()).messageId)
    throw new Error("reply_queue_failed");
}

export function channelSenderAllowed(event: SlackThreadEvent, config: RelayConfig, grants = "") {
  if (config.blockedSenderIds.has(event.senderUserId)) return false;
  if (config.allowAllSenders || config.allowedSenderIds.has(event.senderUserId)) return true;
  if (!/^[CDG][A-Z0-9]+$/.test(event.channelId) || !config.allowedChannelIds.has(event.channelId)) return false;
  const pair = event.channelId + ":" + event.senderUserId;
  return grants.split(",").some(grant => grant.trim() === pair);
}

export function isOwnerSelfAssignment(event: SlackThreadEvent, config: RelayConfig): boolean {
  const [owner] = config.allowedSenderIds;
  return !!config.selfAssignScope && !config.allowAllChannels && !config.allowAllSenders &&
    config.allowedSenderIds.size === 1 && event.senderUserId === owner &&
    config.targetUserIds.has(owner) && !config.blockedSenderIds.has(owner) &&
    event.teamId === config.teamId && !config.blockedChannelIds.has(event.channelId) &&
    (config.selfAssignScope === "all" ? /^[CDG][A-Z0-9]+$/ : /^D[A-Z0-9]+$/).test(event.channelId) &&
    findTargetMention(event.text, config.targetUserIds, new Set())?.id === owner;
}

export function workerReadToken(event: SlackThreadEvent, config: RelayConfig, channelToken?: string) {
  if (config.allowAllChannels || config.blockedChannelIds.has(event.channelId)) return undefined;
  if (config.allowedChannelIds.has(event.channelId)) {
    if (event.channelId.startsWith("D")) return config.slackContextToken;
    return /^[CG]/.test(event.channelId) ? channelToken : undefined;
  }
  return isOwnerSelfAssignment(event, config) ? config.slackContextToken : undefined;
}

export async function deliverReply(
  job: ReplyJob,
  config: RelayConfig,
  send: typeof fetch,
  channelToken?: string,
  senderGrants?: string,
  reviewKey?: string,
  reviewWorkspaceSlug?: string,
  businessScope: BusinessScope = {},
) {
  if (
    !/^[a-f0-9-]{36}$/.test(job.issueId) ||
    ![job.thread, job.message].every((id) => /^[0-9a-f]{64}$/.test(id)) ||
    !Number.isInteger(job.attempt) ||
    job.attempt < 0 ||
    job.attempt > MAX_REPLY_ATTEMPTS
  )
    return "invalid_reply_job";
  const scope = digest(
    [
      config.multicaWorkspaceId,
      config.multicaProjectId,
      config.multicaAgentId,
    ].join(":"),
  );
  const prefix = "relay:" + scope;
  const store = new UpstashThreadStore(
    config.kvRestApiUrl,
    config.kvRestApiToken,
    send,
  );
  const ledger = prefix + ":reply:" + job.message;
  const receipt = await store.get(ledger);
  if (receipt?.startsWith("sent:")) return "already_sent";
  const state = JSON.parse(
    (await store.get(prefix + ":thread:" + job.thread)) || "null",
  );
  const frozen = JSON.parse(
    (await store.get(prefix + ":message:" + job.message + ":envelope")) ||
      "null",
  );
  const e: SlackThreadEvent | undefined = frozen?.eventPayload;
  const [ownerId] = config.allowedSenderIds;
  const readToken = e && workerReadToken(e, config, channelToken);
  if (
    !e ||
    !readToken ||
    state?.issueId !== job.issueId ||
    (state.cancellation && state.cancellation.phase !== "done") ||
    (state.ignoredThrough &&
      compareTs(e.messageTs, state.ignoredThrough) <= 0) ||
    e.teamId !== config.teamId ||
    !allowedSourceApp(e.sourceAppId, config.allowedUserAppIds) ||
    config.allowAllSenders ||
    config.allowedSenderIds.size !== 1 ||
    !ownerId || !config.targetUserIds.has(ownerId) ||
    !channelSenderAllowed(e, config, senderGrants) ||
    !findTargetMention(e.text, config.targetUserIds, new Set()) ||
    digest(messageKey(e)) !== job.message ||
    digest(threadKey(e)) !== job.thread
  )
    return "reply_scope_rejected";
  const issue = await getIssue(config, job.issueId, send);
  if (
    !issue.description?.startsWith(
      "<!-- relay-thread:" + scope + ":" + job.thread + " -->\n",
    )
  )
    return "reply_scope_rejected";
  const rootRequest = readTaskEnvelope(issue.description).eventPayload;
  let trigger: string | undefined;
  if (digest(messageKey(rootRequest)) !== job.message) {
    trigger = (
      await findComment(
        config,
        job.issueId,
        "<!-- relay-message:" + job.message + " -->",
        send,
      )
    )?.id;
    if (!trigger) return "reply_trigger_missing";
  }
  const runs = (await listIssueRuns(config, job.issueId, send)).filter(
    (r) =>
      r.agent_id === config.multicaAgentId &&
      (r.trigger_comment_id || undefined) === trigger,
  );
  const completed = runs.filter((r) => r.status === "completed");
  let output: unknown;
  if (!completed.length) {
    if (job.attempt < MAX_REPLY_ATTEMPTS) {
      await scheduleReply({ ...job, attempt: job.attempt + 1 }, config, send);
      return "waiting";
    }
    if (!frozen.context?.githubReviewTicket?.startsWith("intent1.")) return "reply_wait_expired";
    output = runs.some(r=>r.status==="failed" || r.status==="cancelled") &&
      runs.every(r=>r.status==="failed" || r.status==="cancelled")
      ? "这次任务已失败或取消，未取得可确认的执行结果。请核对已有结果后再决定是否重试。"
      : "等待执行结果的时间已到，尚未收到完成回执。请先核对任务或 GitHub 记录，再决定是否重试。";
  } else {
    if (completed.length !== 1) return "ambiguous_run";
    output = completed[0].result?.output;
  }
  if (
    typeof output !== "string" ||
    !output.trim() ||
    Array.from(output).length > (frozen.context?.githubReviewTicket ? 256000 : 12000)
  )
    return "invalid_reply_text";
  const headers = { authorization: "Bearer " + config.slackContextToken };
  const blockId = "relay-delivery-" + digest(ledger);
  async function readThread(oldest: string, latest?: string) {
    const query = new URLSearchParams({
      channel: e!.channelId,
      ts: e!.threadTs,
      oldest,
      inclusive: "true",
      limit: "100",
      ...(latest ? { latest } : {}),
    });
    const response = await send(
      "https://slack.com/api/conversations.replies?" + query,
      { headers: { authorization: "Bearer " + readToken } },
    );
    const body = await response.json();
    if (!response.ok || !body.ok || !Array.isArray(body.messages))
      throw new Error("reply_read_failed");
    return body.messages as {
      ts: string;
      thread_ts?: string;
      user?: string;
      text?: string;
      bot_id?: string;
      app_id?: string;
      bot_profile?: unknown;
      subtype?: string;
      blocks?: { block_id?: string }[];
    }[];
  }
  const verify = async (ts?: string) => {
    const rows = await readThread(ts || e.messageTs, ts);
    const reply = rows.find(
      (m) =>
        m.thread_ts === e.threadTs &&
        m.user === ownerId &&
        (!ts || m.ts === ts) &&
        m.blocks?.some((b) => b.block_id === blockId),
    );
    if (!reply) throw new Error("slack_delivery_unknown");
    await store.set(ledger, "sent:" + reply.ts, STATE_TTL_SECONDS);
    return "sent";
  };
  if (receipt)
    return verify(
      receipt.startsWith("accepted:") ? receipt.slice(9) : undefined,
    );
  const source = (await readThread(e.messageTs, e.messageTs)).find(
    (m) => m.ts === e.messageTs,
  );
  if (
    !source ||
    (source.thread_ts || source.ts) !== e.threadTs ||
    source.user !== e.senderUserId ||
    !isSupportedMessage({...source,type:'message',channel:e.channelId},config.allowedUserAppIds) ||
    source.app_id !== e.sourceAppId ||
    source.text !== e.text
  )
    return "source_changed";
  const review = reviewPresentation(output, frozen.context?.githubReviewTicket, reviewKey);
  if ((output.startsWith("relay-review/") || output.startsWith("relay-agent/")) && !review) return "invalid_review_receipt";
  const business = businessPresentation(output, frozen.context?.githubReviewTicket, reviewKey);
  if (output.startsWith("relay-business/") && !business) return "invalid_business_receipt";
  if (business && !businessAllowed(e,businessScope,ownerId,config.teamId)) return "reply_scope_rejected";
  const visible = business?.text || review?.text || (frozen.context?.githubReviewTicket ? output.trim() : genericReviewReply(e.text, output));
  if (Array.from(visible).length>32000) return "invalid_reply_text";
  const agentOutput=output.startsWith("relay-agent/1 ");
  const chars = Array.from(agentOutput?agentSlackText(visible):visible);
  const blocks: unknown[] = [];
  for (let i = 0; i < chars.length; i += 3000)
    blocks.push({
      type: "section",
      text: agentOutput
        ? {type:"mrkdwn",text:chars.slice(i,i+3000).join(""),verbatim:true}
        : { type: "plain_text", text: chars.slice(i, i + 3000).join("") },
    });
  const issueLink = (review || business) && reviewWorkspaceSlug && /^[a-z0-9-]+$/.test(reviewWorkspaceSlug)
    ? new URL("/" + reviewWorkspaceSlug + "/issues/" + job.issueId, config.multicaApiBaseUrl).href
    : undefined;
  const usage = completed[0]?.usage;
  const models = Array.isArray(usage) ? usage.flatMap(u => u && typeof u.model === "string" ? [u.model] : []) : [];
  const contextLines: string[] = [];
  const stats = review ?? business;
  if (stats) {
    contextLines.push(reviewStatistics(stats, review?.models?.length ? review.models : models));
    const label = typeof issue.identifier === "string" && /^[A-Z][A-Z0-9-]*-\d+$/.test(issue.identifier) ? issue.identifier : "Multica 任务";
    if (issueLink) contextLines.push("<" + issueLink + "|" + label + ">");
    if (review?.repositoryLine) contextLines.push(review.repositoryLine);
  }
  for (const text of contextLines) {
    // Each repository and statistic is a complete link-safe line.
    for (const line of text.split("\n")) blocks.push({type:"context", elements:[{type:"mrkdwn", text:line}]});
  }
  blocks.push({
    type: "context",
    block_id: blockId,
    elements: [
      { type: "plain_text", text: "Sent by 🤖 Perry’s Agent" },
    ],
  });
  if (!(await store.setIfAbsent(ledger, "attempting", STATE_TTL_SECONDS)))
    return "delivery_in_progress";
  const response = await send("https://slack.com/api/chat.postMessage", {
    method: "POST",
    headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify({
      channel: e.channelId,
      thread_ts: e.threadTs,
      text: visible + "\n\n" + [...contextLines, "Sent by 🤖 Perry’s Agent"].join("\n"),
      mrkdwn: false,
      parse: "none",
      unfurl_links: false,
      unfurl_media: false,
      blocks,
    }),
  });
  if (response.status === 429) {
    await store.releaseIfOwner(ledger, "attempting");
    throw new Error("slack_rate_limited");
  }
  const body = await response.json();
  if (
    !response.ok ||
    !body.ok ||
    body.channel !== e.channelId ||
    !/^\d+\.\d+$/.test(body.ts)
  )
    throw new Error("slack_delivery_unknown");
  await store.set(ledger, "accepted:" + body.ts, STATE_TTL_SECONDS);
  return verify(body.ts);
}
