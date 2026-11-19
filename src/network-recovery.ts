import { createHmac, timingSafeEqual } from 'node:crypto';
import { loadRelayConfig } from './config.js';
import { digest, messageKey, threadKey, type SlackThreadEvent } from './thread-router.js';
import { UpstashThreadStore } from './thread-store.js';
import { getIssue, findComment, listIssueRuns, isActiveRun } from './multica-api.js';
import { reviewPresentation } from './github-review.js';
import { readTaskEnvelope } from './task-presentation.js';
import { compareTs } from './context-envelope.js';
import { allowedSourceApp, findTargetMention } from './mentions.js';
import { channelSenderAllowed, workerReadToken } from './worker-reply.js';
import { notifyOwnerFailure } from './failure-notification.js';

export const NETWORK_WAIT_CODES = new Set(['model_network_wait','model_stalled_wait']);

// A local non-model health probe asks for a permitted rerun. It cannot choose an arbitrary task.
export async function networkRecovery(request: Request, env: Record<string,string|undefined>, send: typeof fetch = fetch): Promise<Response> {
  const result=(action:string, status=200, extra={})=>Response.json({action,...extra},{status});
  if (request.method !== 'POST') return result('rejected',405);
  const key=env.GITHUB_REVIEW_SIGNING_KEY, stamp=request.headers.get('X-Relay-Time') || '';
  const mac=request.headers.get('X-Relay-Signature') || '';
  if (!key || !/^\d{10}$/.test(stamp) || Math.abs(Date.now()/1000-Number(stamp))>60 || !/^[a-f0-9]{64}$/.test(mac)) return result('rejected',401);
  const body=await request.text();
  if (body.length>1024) return result('rejected',413);
  const expected=createHmac('sha256',key).update('network-recovery/1:'+stamp+':'+body).digest();
  if (!timingSafeEqual(expected,Buffer.from(mac,'hex'))) return result('rejected',401);
  const input=JSON.parse(body);
  if (![input.thread,input.message].every(x=>typeof x==='string' && /^[a-f0-9]{64}$/.test(x))) return result('rejected',400);
  const config=loadRelayConfig(env);
  const scope=digest([config.multicaWorkspaceId,config.multicaProjectId,config.multicaAgentId].join(':'));
  const prefix='relay:'+scope, store=new UpstashThreadStore(config.kvRestApiUrl,config.kvRestApiToken,send);
  const state=JSON.parse(await store.get(prefix+':thread:'+input.thread) || 'null');
  const frozen=JSON.parse(await store.get(prefix+':message:'+input.message+':envelope') || 'null');
  const event:SlackThreadEvent|undefined=frozen?.eventPayload;
  const receipt=await store.get(prefix+':reply:'+input.message);
  if (!event || !state?.issueId || receipt?.startsWith('sent:') ||
      state.cancellation && state.cancellation.phase!=='done' ||
      state.ignoredThrough && compareTs(event.messageTs,state.ignoredThrough)<=0) return result('cancelled');
  if (digest(threadKey(event))!==input.thread || digest(messageKey(event))!==input.message ||
      event.teamId!==config.teamId || Date.now()/1000-Number(event.messageTs)>7*86400 ||
      !allowedSourceApp(event.sourceAppId,config.allowedUserAppIds) ||
      !workerReadToken(event,config,env.SLACK_CHANNEL_READ_TOKEN) ||
      !channelSenderAllowed(event,config,env.SLACK_CHANNEL_SENDER_GRANTS) ||
      !findTargetMention(event.text,config.targetUserIds,new Set())) return result('cancelled');
  const issue=await getIssue(config,state.issueId,send);
  if (!issue.description?.startsWith('<!-- relay-thread:'+scope+':'+input.thread+' -->\n')) return result('cancelled');
  const root=readTaskEnvelope(issue.description).eventPayload;
  let trigger:string|undefined;
  if (digest(messageKey(root))!==input.message) {
    trigger=(await findComment(config,issue.id,'<!-- relay-message:'+input.message+' -->',send))?.id;
    if (!trigger) return result('waiting');
  }
  const all=(await listIssueRuns(config,issue.id,send)).filter(r=>r.agent_id===config.multicaAgentId);
  if (all.some(isActiveRun)) return result('waiting');
  const runs=all.filter(r=>(r.trigger_comment_id||undefined)===trigger);
  if (!runs.length) return result('waiting');
  // API returns newest first. A later cancellation must not resurrect an older paused turn.
  if (runs[0].status==='cancelled') return result('cancelled');
  for (const run of runs) {
    if (run.status!=='completed' || typeof run.result?.output!=='string') continue;
    const packet=reviewPresentation(run.result.output,frozen.context?.githubReviewTicket,key);
    if (!packet) return result('cancelled');
    if (!NETWORK_WAIT_CODES.has(packet.failureCode || '')) return result('cancelled');
    if (input.dispatch_unknown===run.id) {
      const notified=await notifyOwnerFailure(event,input.message,'recovery_dispatch_unknown',env.GITHUB_REVIEW_SELF_CHANNEL,config,send);
      return result(notified==='failure_owner_notified' ? 'notified' : 'waiting');
    }
    return result('resume',200,{runId:run.id});
  }
  return result('waiting');
}
