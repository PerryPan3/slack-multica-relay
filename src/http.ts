import { routeTicket } from "./semantic-route.js";
import { channelSenderAllowed, deliverReply, isOwnerSelfAssignment, workerReadToken, replyJob, scheduleReply } from "./worker-reply.js";
import { Buffer } from 'node:buffer';
import { Receiver } from "@upstash/qstash";
import { loadRelayConfig, type RelayConfig } from "./config.js";
import { verifySlackSignature } from "./signature.js";
import {
  findTargetMention,
  isCancelCommand,
  canCancelTask,
  isSupportedMessage,
  type SlackMessageEvent,
} from "./mentions.js";
import { readContext, enrichParticipantNames } from "./slack-context.js";
import {
  routeSlackThreadEvent,
  digest,
  threadKey,
  messageKey,
  type SlackThreadEvent,
} from "./thread-router.js";
import { UpstashThreadStore } from "./thread-store.js";
import { projectFiles, sourceMessageFingerprint } from "./context-envelope.js";

export function json(value: unknown, status = 200): Response {
  return Response.json(value, { status });
}
async function readBody(request: Request): Promise<string> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      bytes += part.value.length;
      if (bytes > 256 * 1024) throw new Error("body_too_large");
      parts.push(part.value);
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  return Buffer.concat(parts).toString("utf8");
}
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function admitted(event: SlackThreadEvent, config: RelayConfig, senderGrants?: string): boolean {
  return (
    event.teamId === config.teamId &&
    (config.allowAllChannels || config.allowedChannelIds.has(event.channelId) || isOwnerSelfAssignment(event, config)) &&
    !config.blockedChannelIds.has(event.channelId) &&
    channelSenderAllowed(event, config, senderGrants) &&
    !!findTargetMention(
      event.text,
      config.targetUserIds,
      config.targetSubteamIds,
    )
  );
}
function parsedEvent(value: unknown): SlackThreadEvent {
  if (
    !record(value) ||
    ["teamId", "channelId", "senderUserId"].some(
      (k) =>
        typeof value[k] !== "string" ||
        !/^[A-Z][A-Z0-9]+$/u.test(value[k] as string),
    ) ||
    ["messageTs", "threadTs"].some(
      (k) =>
        typeof value[k] !== "string" || !/^\d+\.\d{1,6}$/u.test(value[k] as string),
    ) ||
    typeof value.text !== "string" ||
    (value.sourceFingerprint !== undefined &&
      (typeof value.sourceFingerprint !== "string" || !/^[a-f0-9]{64}$/u.test(value.sourceFingerprint))) ||
    (value.operation !== undefined &&
      value.operation !== "dispatch" &&
      value.operation !== "cancel") ||
    !record(value.mention) ||
    !["user", "subteam"].includes(String(value.mention.type)) ||
    typeof value.mention.id !== "string"
  )
    throw new Error("invalid_event");
  return value as unknown as SlackThreadEvent;
}
function reason(error: unknown): string {
  if (
    error instanceof Error &&
    ["TimeoutError", "AbortError"].includes(error.name)
  )
    return "timeout";
  const codes = [
    "reply_queue_failed",
    "reply_read_failed",
    "slack_delivery_unknown",
    "body_too_large",
    "invalid_event",
    "thread_lock_busy",
    "ambiguous_issue_create",
    "ambiguous_comment_create",
    "invalid_thread_state",
    "ambiguous_issue_mapping",
    "invalid_issue_scope",
    "invalid_context_scope",
    "context_request_too_large",
    "context_required_page_limit",
    "context_required_unavailable",
    "task_presentation_too_large",
    "context_rate_limited",
    "context_upstream_failed",
    "context_invalid_response",
    "context_invalid_cursor",
    "context_response_too_large",
    "comment_lookup_limit",
    "multica_http_error",
    "invalid_multica_response",
    "invalid_comment_cursor",
    "cancellation_pending",
    "cancellation_run_missing",
    "cancellation_new_run",
    "reaction_cleanup_failed",
  ];
  return error instanceof Error && codes.includes(error.message)
    ? error.message
    : "upstream_failed";
}
const permanentConsumerErrors = new Set([
  "invalid_event",
  "invalid_thread_state",
  "ambiguous_issue_mapping",
  "invalid_issue_scope",
  "invalid_context_scope",
  "context_request_too_large",
  "context_required_page_limit",
  "context_required_unavailable",
  "task_presentation_too_large",
  "comment_lookup_limit",
]);
export function consumerFailure(code: string): Response {
  if (permanentConsumerErrors.has(code))
    return json({ action: "rejected", error: code, retryable: false });
  return json({ error: code, retryable: true }, 503);
}
export async function acceptSlack(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  const start = Date.now();
  if (request.method !== "POST")
    return json({ error: "method_not_allowed" }, 405);
  const signingSecret = env.SLACK_SIGNING_SECRET?.trim();
  if (!signingSecret) return json({ error: "relay_not_configured" }, 500);
  let raw: string;
  try {
    raw = await readBody(request);
  } catch {
    return json({ error: "body_too_large" }, 413);
  }
  if (
    !verifySlackSignature(
      raw,
      request.headers.get("x-slack-request-timestamp") ?? undefined,
      request.headers.get("x-slack-signature") ?? undefined,
      signingSecret,
    )
  ) {
    console.warn("relay_admission", {
      reason: "invalid_slack_signature",
      hasTimestamp: request.headers.has("x-slack-request-timestamp"),
      hasSignature: request.headers.has("x-slack-signature"),
      contentType: request.headers.get("content-type") ?? "missing",
      bodyBytes: Buffer.byteLength(raw),
    });
    return json({ error: "invalid_slack_signature" }, 401);
  }
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  if (!record(body)) return json({ error: "invalid_envelope" }, 400);
  if (body.type === "url_verification" && typeof body.challenge === "string")
    return json({ challenge: body.challenge });
  let config: RelayConfig;
  try {
    config = loadRelayConfig(env);
  } catch {
    return json({ error: "relay_not_configured" }, 500);
  }
  if (
    body.type !== "event_callback" ||
    !record(body.event) ||
    !isSupportedMessage(body.event as SlackMessageEvent)
  )
    return json({ action: "ignored" });
  const event = body.event;
  const mention = findTargetMention(
    event.text as string,
    config.targetUserIds,
    config.targetSubteamIds,
  );
  if (!mention) return json({ action: "ignored", reason: "not_addressed" });
  let payload: SlackThreadEvent;
  try {
    payload = parsedEvent({
      teamId: body.team_id,
      channelId: event.channel,
      messageTs: event.ts,
      threadTs: event.thread_ts ?? event.ts,
      senderUserId: event.user,
      text: event.text,
      mention,
      sourceFingerprint: sourceMessageFingerprint(event),
      ...(event.files ? { files: projectFiles(event.files,Number.MAX_SAFE_INTEGER) } : {}),
    });
  } catch {
    return json({ error: "invalid_event" }, 400);
  }
  if (!admitted(payload, config, env.SLACK_CHANNEL_SENDER_GRANTS))
    return json({ action: "ignored", reason: "not_allowed" });
  payload.operation = isCancelCommand(
    payload.text,
    config.targetUserIds,
    config.targetSubteamIds,
    config.cancelKeywords,
  )
    ? "cancel"
    : "dispatch";
  if (
    payload.operation === "cancel" &&
    !canCancelTask(payload.senderUserId, config.targetUserIds)
  )
    return json({ action: "ignored", reason: "cancel_not_allowed" });
  try {
    const response = await fetchImpl(
      config.queueUrl + "/v2/publish/" + config.consumerUrl,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${config.queueToken}`,
          "content-type": "application/json",
          "Upstash-Deduplication-Id": digest(
            config.consumerUrl + ":" + messageKey(payload),
          ),
          "Upstash-Retries": "3",
          "Upstash-Timeout": "50s",
          "Upstash-Flow-Control-Key": digest(
            config.consumerUrl + ":" + threadKey(payload),
          ),
          "Upstash-Flow-Control-Value": "parallelism=1",
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(2000),
      },
    );
    if (!response.ok) throw new Error("queue_publish_failed");
    const queued: unknown = await response.json();
    if (!record(queued) || typeof queued.messageId !== "string")
      throw new Error("invalid_queue_response");
    console.info("relay_admission", {
      eventId: digest(messageKey(payload)),
      queueMessageId: queued.messageId,
      durationMs: Date.now() - start,
    });
    return json({ action: "accepted", queueMessageId: queued.messageId });
  } catch (error) {
    console.warn("relay_admission", {
      eventId: digest(messageKey(payload)),
      reason: reason(error),
      durationMs: Date.now() - start,
    });
    return json({ error: "queue_unavailable", retryable: true }, 503);
  }
}
export async function consumeQueue(
  request: Request,
  env: NodeJS.ProcessEnv = process.env,
  fetchImpl: typeof fetch = fetch,
): Promise<Response> {
  if (request.method !== "POST")
    return json({ error: "method_not_allowed" }, 405);
  let config: RelayConfig;
  try {
    config = loadRelayConfig(env);
  } catch {
    return json({ error: "relay_not_configured" }, 500);
  }
  let raw: string;
  try {
    raw = await readBody(request);
  } catch {
    return json({ error: "body_too_large" }, 413);
  }
  try {
    const valid = await new Receiver({
      currentSigningKey: config.queueCurrentSigningKey,
      nextSigningKey: config.queueNextSigningKey,
      devMode: false,
    }).verify({
      body: raw,
      signature: request.headers.get("upstash-signature") ?? "",
      url: config.consumerUrl,
    });
    if (!valid) return json({ error: "invalid_queue_signature" }, 401);
  } catch {
    return json({ error: "invalid_queue_signature" }, 401);
  }
  let event: SlackThreadEvent;
  try {
    const payload = JSON.parse(raw);
    if (payload?.kind === 'thread_reply') {
      if (env.WORKER_THREAD_REPLIES !== 'true') return json({action:'ignored'});
      try {
        const action = await deliverReply(payload, config, (input,init)=>fetchImpl(input,init), env.SLACK_CHANNEL_READ_TOKEN, env.SLACK_CHANNEL_SENDER_GRANTS, env.GITHUB_REVIEW_SIGNING_KEY, env.GITHUB_REVIEW_WORKSPACE_SLUG,
          {businessChannel:env.BUSINESS_CHANNEL,businessSenders:env.BUSINESS_SENDERS,businessTestDm:env.BUSINESS_TEST_DM});
        console.info('relay_reply', {action, issueId:payload.issueId, attempt:payload.attempt});
        return json({action});
      } catch (error) {
        console.warn('relay_reply', {action:'retry', reason:reason(error)});
        return json({error:'reply_unavailable'},503);
      }
    }
    event = parsedEvent(payload);
  } catch {
    return consumerFailure("invalid_event");
  }
  if (!admitted(event, config, env.SLACK_CHANNEL_SENDER_GRANTS))
    return json({ action: "ignored", reason: "policy_changed" });
  // 新消息保留入队时的指令分类，配置变更不能把排队中的取消变成启动任务。
  event.operation ??= isCancelCommand(
    event.text,
    config.targetUserIds,
    config.targetSubteamIds,
    config.cancelKeywords,
  )
    ? "cancel"
    : "dispatch";
  if (
    event.operation === "cancel" &&
    !canCancelTask(event.senderUserId, config.targetUserIds)
  )
    return json({ action: "ignored", reason: "cancel_not_allowed" });
  const start = Date.now(),
    deadline = AbortSignal.timeout(45000);
  const boundedFetch: typeof fetch = (input, init = {}) => {
    deadline.throwIfAborted();
    return fetchImpl(input, {
      ...init,
      signal: init.signal ? AbortSignal.any([init.signal, deadline]) : deadline,
    });
  };
  try {
    const contextToken = env.WORKER_THREAD_REPLIES === 'true'
      ? workerReadToken(event, config, env.SLACK_CHANNEL_READ_TOKEN)
      : config.slackContextToken;
    if (!contextToken) return json({action:'ignored',reason:'channel_read_not_configured'});
    const result = await routeSlackThreadEvent(
      event,
      {
        ...config,
        readContext: async (event, options) => {
          const context = await enrichParticipantNames(event,
            await readContext(event, contextToken, boundedFetch, env.WORKER_THREAD_REPLIES === 'true' ? {...options,includeNearby:false,currentThreadOnly:true} : options),
            config.slackContextToken, boundedFetch);
          const taskTicket = routeTicket(event,context,{
            team:config.teamId, owner:config.allowedSenderIds.size === 1 ? [...config.allowedSenderIds][0] : undefined,
            reviewChannel:env.GITHUB_REVIEW_CHANNEL, selfChannel:env.GITHUB_REVIEW_SELF_CHANNEL,
            localEnabled:env.OWNER_LOCAL_TASKS === "true", businessChannel:env.BUSINESS_CHANNEL,
            businessSenders:env.BUSINESS_SENDERS, businessTestDm:env.BUSINESS_TEST_DM, key:env.GITHUB_REVIEW_SIGNING_KEY,
          });
          return taskTicket ? { ...context, githubReviewTicket: taskTicket } : context;
        },
        store: new UpstashThreadStore(
          config.kvRestApiUrl,
          config.kvRestApiToken,
          boundedFetch,
        ),
      },
      boundedFetch,
    );
    if (env.WORKER_THREAD_REPLIES === 'true' && result.issueId && ['created','comment_persisted','duplicate'].includes(result.action))
      await scheduleReply(replyJob(event,result.issueId),config,boundedFetch);
    console.info("relay_dispatch", {
      ...result,
      eventId: digest(messageKey(event)),
      durationMs: Date.now() - start,
    });
    return json(result);
  } catch (error) {
    const code = reason(error);
    console.warn("relay_dispatch", {
      eventId: digest(messageKey(event)),
      reason: code,
      durationMs: Date.now() - start,
    });
    // Acknowledged rejection stops retries for input that cannot fit unchanged.
    return consumerFailure(code);
  }
}
