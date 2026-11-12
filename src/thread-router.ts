import { Buffer } from 'node:buffer';
import {formatTaskTitle,formatTaskDescription,readTaskEnvelope} from './task-presentation.js';
import { createHash, randomUUID } from "node:crypto";
import {
  ApiError,
  createIssue,
  findIssue,
  createComment,
  findComment,
  getSlackReplyContext,
  getIssue,
  listRelayMessageContents,
  type ApiConfig,
} from "./multica-api.js";
import type { MentionMatch } from "./mentions.js";
import { addSlackReaction } from "./reaction.js";
import {
  cancelThread,
  maxTimestamp,
  type CancellationState,
} from "./cancellation.js";
import { type ThreadStore } from "./thread-store.js";
import { buildEnvelope, compactInitialContext, projectFiles, compareTs, timestampValid, markTruncated, updateCoverage, type ThreadContext } from './context-envelope.js';
import type { ContextReadOptions } from './slack-context.js';

export interface SlackThreadEvent {
  teamId: string;
  channelId: string;
  messageTs: string;
  threadTs: string;
  senderUserId: string;
  text: string;
  mention: MentionMatch;
  files?: unknown;
  filesTruncated?: boolean;
  sourceFingerprint?: string;
  sourceAppId?: string;
  operation?: "dispatch" | "cancel";
}
export interface ThreadRouterConfig extends ApiConfig {
  store: ThreadStore;
  readContext: (event: SlackThreadEvent, options?: ContextReadOptions) => Promise<ThreadContext>;
  slackReactionToken?: string;
  slackReactionName?: string;
}
export interface ThreadState {
  version: 2;
  rootMessageKey: string;
  issueId?: string;
  creating: boolean;
  lastMessageTs?: string;
  ignoredThrough?: string;
  reactionMessages?: string[];
  reactionHistoryKnown?: boolean;
  cancellation?: CancellationState;
}
interface MessageState {
  phase: "writing" | "done" | "rejected";
}
export interface ThreadRouteResult {
  action:
    | "created"
    | "comment_persisted"
    | "duplicate"
    | "cancelled"
    | "no_active_run"
    | "ignored";
  issueId?: string;
}
export const STATE_TTL_SECONDS = 90 * 24 * 60 * 60;
export function threadKey(event: SlackThreadEvent): string {
  return `${event.teamId}:${event.channelId}:${event.threadTs}`;
}
export function messageKey(event: SlackThreadEvent): string {
  return `${event.teamId}:${event.channelId}:${event.messageTs}`;
}
export function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export async function routeSlackThreadEvent(
  event: SlackThreadEvent,
  config: ThreadRouterConfig,
  fetchImpl: typeof fetch = fetch,
): Promise<ThreadRouteResult> {
  const scope = digest(
    config.multicaWorkspaceId +
      ":" +
      config.multicaProjectId +
      ":" +
      config.multicaAgentId,
  );
  const key = `relay:${scope}:thread:${digest(threadKey(event))}`;
  const msgKey = `relay:${scope}:message:${digest(messageKey(event))}`;
  const lockKey = key + ":lock",
    owner = randomUUID();
  if (!(await config.store.setIfAbsent(lockKey, owner, 120)))
    throw new Error("thread_lock_busy");
  try {
    const raw = await config.store.get(key);
    let state: ThreadState;
    if (raw) {
      state = JSON.parse(raw) as ThreadState;
      if (
        state.version !== 2 ||
        typeof state.rootMessageKey !== "string" ||
        typeof state.creating !== "boolean"
      )
        throw new Error("invalid_thread_state");
    } else
      state = {
        version: 2,
        rootMessageKey: messageKey(event),
        creating: false,
      };
    const marker = `<!-- relay-thread:${scope}:${digest(threadKey(event))} -->`;
    const save = () =>
      config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
    if (event.operation === "cancel")
      return await cancelThread(event, state, marker, config, save, fetchImpl);
    if (state.cancellation && state.cancellation.phase !== "done") {
      state.ignoredThrough = maxTimestamp(
        state.ignoredThrough,
        event.messageTs,
      );
      await save();
      return { action: "ignored", issueId: state.issueId };
    }
    if (
      state.ignoredThrough &&
      compareTs(event.messageTs, state.ignoredThrough) <= 0
    )
      return { action: "ignored", issueId: state.issueId };
    state.lastMessageTs = maxTimestamp(state.lastMessageTs, event.messageTs);
    // 保存触发消息，再尝试外部写入，取消恢复才能找到响应丢失的消息。
    state.reactionMessages ??= raw
      ? [state.rootMessageKey.split(":").at(-1)!]
      : [];
    if (!state.reactionMessages.includes(event.messageTs))
      state.reactionMessages.push(event.messageTs);
    await save();
    const finish = async (
      action: "created" | "comment_persisted" | "duplicate",
    ): Promise<ThreadRouteResult> => {
      // 添加和取消清理共用线程锁，避免迟到的启动 reaction 出现在已取消任务上。
      if (config.slackReactionToken && config.slackReactionName) {
        try {
          await addSlackReaction(
            config.slackReactionToken,
            event.channelId,
            event.messageTs,
            config.slackReactionName,
            fetchImpl,
          );
        } catch {
          console.warn("relay_reaction", {
            eventId: digest(messageKey(event)),
            reason: "reaction_failed",
          });
        }
      }
      return { action, issueId: state.issueId };
    };
    const selectionKey = key + ':sent-context-index';
    type SelectionIndex = { version: 3; cutoffTs: string; initialContext: ThreadContext; request: SlackThreadEvent };
    let recoveredDescription: string | undefined;
    const advanceSelection = async (): Promise<void> => {
      const pending=await config.store.get(msgKey+':selection-index');
      if(!pending)return;
      const next=JSON.parse(pending) as SelectionIndex;
      const old=await config.store.get(selectionKey);
      if(old&&compareTs(JSON.parse(old).cutoffTs,next.cutoffTs)>=0)return;
      await config.store.set(selectionKey,pending,24*60*60);
    };
    const prepare = async (): Promise<string> => {
      const preparedKey = msgKey + ':envelope';
      const frozen = await config.store.get(preparedKey);
      if (frozen) {
        console.info('relay_context',{eventId:digest(messageKey(event)),snapshot:'reused',envelopeBytes:Buffer.byteLength(frozen)});
        return frozen;
      }
      const savedIndex=await config.store.get(selectionKey);
      let index:SelectionIndex|undefined=savedIndex?JSON.parse(savedIndex):undefined;
      if(index?.version===3&&(!index.request||threadKey(index.request)!==threadKey(event)||!timestampValid(index.cutoffTs)||index.request.messageTs!==index.cutoffTs||index.initialContext?.anchorTs!==event.threadTs||!Array.isArray(index.initialContext.timeline?.messages)))throw new Error('invalid_thread_state');
      const followup=!!state.issueId&&messageKey(event)!==state.rootMessageKey;
      // Initial background belongs to the Issue, independently of whether A exists before B.
      let initialContext=index?.version===3?index.initialContext:undefined;
      // A is a persisted mention, never a read attempt or an unconfirmed write.
      // Cache loss and out-of-order events recover A from the scoped Issue/comments.
      if(followup&&(!index||index.version!==3||compareTs(index.cutoffTs,event.messageTs)>=0)){
        const description=recoveredDescription??(await getIssue(config,state.issueId!,fetchImpl)).description;
        if(!description?.startsWith(marker+'\n'))throw new Error('invalid_thread_state');
        const first=readTaskEnvelope(description);
        if(threadKey(first.eventPayload)!==threadKey(event))throw new Error('invalid_thread_state');
        const history=[first];
        for(const content of await listRelayMessageContents(config,state.issueId!,fetchImpl)){
          const item=readTaskEnvelope(content);
          if(threadKey(item.eventPayload)!==threadKey(event)||!content.startsWith(`<!-- relay-message:${digest(messageKey(item.eventPayload))} -->\n`))continue;
          history.push(item);
        }
        const previous=history.filter(item=>compareTs(item.eventPayload.messageTs,event.messageTs)<0)
          .sort((a,b)=>compareTs(b.eventPayload.messageTs,a.eventPayload.messageTs))[0];
        const original=first.context as ThreadContext|undefined;
        // Legacy envelopes may not have a tree. Their request still bounds A.
        initialContext=original?.timeline?(first.schemaVersion===5?structuredClone(original):compactInitialContext(original)):{
          anchorTs:event.threadTs,cutoffTs:first.eventPayload.messageTs,capturedAt:'',timeline:{status:'unavailable',reason:'legacy_background_unavailable',messages:[]},
        };
        initialContext.timeline.messages=initialContext.timeline.messages.filter(m=>m.ts!==event.threadTs);
        const latest=history.sort((a,b)=>compareTs(b.eventPayload.messageTs,a.eventPayload.messageTs))[0]!;
        // A late event after cache loss must not replace a newer persisted cursor.
        await config.store.set(selectionKey,JSON.stringify({version:3,cutoffTs:latest.eventPayload.messageTs,initialContext,request:latest.eventPayload}),24*60*60);
        if(previous)index={version:3,cutoffTs:previous.eventPayload.messageTs,initialContext,request:previous.eventPayload};
        else index=undefined;
      }
      const baseline=index?.version===3&&compareTs(index.cutoffTs,event.messageTs)<0?index:undefined;
      const source=await config.readContext(event,{sinceTs:baseline?.cutoffTs,includeNearby:!followup,referenceTexts:typeof baseline?.request.text==='string'?[baseline.request.text]:[]});
      if(initialContext){
        const sides=structuredClone(initialContext.timeline.messages).filter(m=>compareTs(m.ts,event.messageTs)<=0);
        for(const root of sides){
          if(!root.replies)continue;
          const before=root.replies.messages.length;
          root.replies.messages=root.replies.messages.filter(m=>compareTs(m.ts,event.messageTs)<=0);
          if(root.replies.messages.length<before)markTruncated(root.replies,'request_cutoff');
          updateCoverage(root.replies);
        }
        const fresh=source.timeline.messages;
        const combined=new Map(sides.map(m=>[m.ts,m]));
        for(const message of fresh){
          const previous=combined.get(message.ts);
          if(previous?.replies&&message.replies){
            message.replies.messages=[...new Map([...previous.replies.messages,...message.replies.messages].map(m=>[m.ts,m])).values()].sort((a,b)=>compareTs(a.ts,b.ts));
          }
          combined.set(message.ts,message);
        }
        source.timeline.messages=[...combined.values()].sort((a,b)=>compareTs(a.ts,b.ts));
        source.initialCutoffTs=initialContext.cutoffTs;
        if(initialContext.timeline.status!=='complete'){
          source.initialStatus=initialContext.timeline.status;source.initialReason=initialContext.timeline.reason;
        }
        source.participants=[...new Map([...(source.participants??[]),...(initialContext.participants??[])].map(p=>[p.id,p])).values()];
      }else source.initialCutoffTs=event.messageTs;
      const anchor=source.timeline.messages.find(m=>m.ts===event.threadTs);
      if(baseline&&baseline.cutoffTs!==event.threadTs&&anchor?.replies&&!anchor.replies.messages.some(m=>m.ts===baseline.cutoffTs)){
        if(typeof baseline.request.text!=='string')throw new Error('context_required_unavailable');
        anchor.replies.messages.unshift({ts:baseline.cutoffTs,authorId:baseline.request.senderUserId,origin:'unknown',text:baseline.request.text,files:projectFiles(baseline.request.files,Number.MAX_SAFE_INTEGER),restoredFrom:'persisted_request',...(baseline.request.filesTruncated?{filesTruncated:true}:{})});
        anchor.replies.messages.sort((a,b)=>compareTs(a.ts,b.ts));
      }
      const agentConfigStartedAt=Date.now();
      const replyContext=await getSlackReplyContext(config,fetchImpl);
      const agentConfigMs=Date.now()-agentConfigStartedAt;
      const assemblyStart=Date.now();
      // First nearby discussion is immutable background; no unrelated new timeline on follow-up.
      const selected=source;
      if(followup){
        selected.sinceTs=baseline?.cutoffTs;
        selected.selection={mode:'focused',baseline:baseline?'available':'unavailable',omittedRoots:0,omittedCurrentReplies:0};
        if(anchor?.replies&&baseline)anchor.replies.messages=anchor.replies.messages.filter(m=>compareTs(m.ts,baseline.cutoffTs)>=0||m.change==='referenced'||m.change==='context');
      }
      const body=buildEnvelope(event,selected,replyContext);
      const output=JSON.parse(body).context as ThreadContext;
      const delivered=output.timeline.messages;
      const count=(roots:ThreadContext['timeline']['messages'])=>roots.length+roots.reduce((n,m)=>n+(m.replies?.messages.length??0),0);
      const reasons:Record<string,number>={};
      const readReasons:Record<string,number>={};
      for(const section of [source.timeline,...source.timeline.messages.flatMap(m=>m.replies?[m.replies]:[])])if(section.reason)readReasons[section.reason]=(readReasons[section.reason]??0)+1;
      const sections=[output.timeline,...delivered.flatMap(m=>m.replies?[m.replies]:[])];
      for(const section of sections)if(section.reason)reasons[section.reason]=(reasons[section.reason]??0)+1;
      console.info('relay_context',{
        eventId:digest(messageKey(event)),snapshot:'prepared',mode:followup?'focused':'full',baseline:baseline?'available':'unavailable',
        slackCalls:source.readStats?.slackCalls,rawMessages:source.readStats?.rawMessages,
        candidateRoots:source.timeline.messages.length,candidateMessages:count(source.timeline.messages),
        retainedRoots:delivered.length,retainedMessages:count(delivered),omittedMessages:count(source.timeline.messages)-count(delivered),
        added:output.selection?.added??0,updated:output.selection?.updated??0,referenced:output.selection?.referenced??0,
        selectionReasons:selected.selectionStats,byteBudgetOmissions:count(selected.timeline.messages)-count(delivered),
        readReasons,reasons,messageReadMs:source.readStats?.messageReadMs,nameLookupCalls:source.readStats?.nameLookupCalls,
        nameReadMs:source.readStats?.nameReadMs,agentConfigMs,assemblyMs:Date.now()-assemblyStart,envelopeBytes:Buffer.byteLength(body),
      });
      await config.store.setIfAbsent(preparedKey, body, 24 * 60 * 60);
      const saved = await config.store.get(preparedKey);
      if (!saved) throw new Error('invalid_thread_state');
      const firstContext=initialContext??structuredClone(output);
      firstContext.timeline.messages=firstContext.timeline.messages.filter(m=>m.ts!==event.threadTs);
      if(saved===body)await config.store.set(msgKey+':selection-index',JSON.stringify({version:3,cutoffTs:event.messageTs,initialContext:firstContext,request:JSON.parse(body).eventPayload}),24*60*60);
      return saved;
    };
    if (!state.issueId) {
      // Recover by immutable description marker before any write. A POST whose
      // result is unknown must never be repeated blindly.
      const existing = await findIssue(config, marker, fetchImpl);
      if (existing) {
        state.issueId = existing.id;
        recoveredDescription=existing.description??undefined;
        if (!raw) {
          const original = readTaskEnvelope(existing.description!);
          if (
            !original.eventPayload ||
            threadKey(original.eventPayload) !== threadKey(event)
          )
            throw new Error("invalid_thread_state");
          state.rootMessageKey = messageKey(original.eventPayload);
          if (!state.reactionMessages!.includes(original.eventPayload.messageTs))
            state.reactionMessages!.push(original.eventPayload.messageTs);
        }
      } else {
        if (state.creating) throw new Error("ambiguous_issue_create");
        const envelope = await prepare();
        state.rootMessageKey = messageKey(event);
        state.creating = true;
        await config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
        try {
          const created = await createIssue(
            config,
            formatTaskTitle(event,scope),
            formatTaskDescription(envelope,marker),
            fetchImpl,
          );
          state.issueId = created.id;
          state.reactionHistoryKnown = true;
        } catch (error) {
          // Definite request rejection permits a later retry; 5xx/transport or
          // malformed success may have committed, so retain the write intent.
          if (
            error instanceof ApiError &&
            error.status >= 400 &&
            error.status < 500
          ) {
            state.creating = false;
            await config.store.set(
              key,
              JSON.stringify(state),
              STATE_TTL_SECONDS,
            );
          }
          throw error;
        }
      }
      state.creating = false;
      await config.store.set(key, JSON.stringify(state), STATE_TTL_SECONDS);
    }
    const previous = await config.store.get(msgKey);
    if (previous && (JSON.parse(previous) as MessageState).phase === "done")
      return await finish("duplicate");
    if (messageKey(event) === state.rootMessageKey) {
      await advanceSelection();
      await config.store.set(
        msgKey,
        JSON.stringify({ phase: "done" }),
        STATE_TTL_SECONDS,
      );
      return await finish("created");
    }
    const messageMarker = `<!-- relay-message:${digest(messageKey(event))} -->`;
    const existingComment = await findComment(
      config,
      state.issueId,
      messageMarker,
      fetchImpl,
    );
    if (!existingComment) {
      if (
        previous &&
        (JSON.parse(previous) as MessageState).phase === "writing"
      )
        throw new Error("ambiguous_comment_create");
      const envelope = await prepare();
      await config.store.set(
        msgKey,
        JSON.stringify({ phase: "writing" }),
        STATE_TTL_SECONDS,
      );
      try {
        await createComment(
          config,
          state.issueId,
          formatTaskDescription(envelope,messageMarker,true),
          fetchImpl,
        );
      } catch (error) {
        // Do not clear ambiguous writes. Explicit rejections are retried by
        // the queue after storing a non-writing phase.
        if (
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500
        )
          await config.store.set(
            msgKey,
            JSON.stringify({ phase: "rejected" }),
            STATE_TTL_SECONDS,
          );
        throw error;
      }
    }
    await advanceSelection();
    await config.store.set(
      msgKey,
      JSON.stringify({ phase: "done" }),
      STATE_TTL_SECONDS,
    );
    return await finish("comment_persisted");
  } finally {
    await config.store.releaseIfOwner(lockKey, owner);
  }
}
