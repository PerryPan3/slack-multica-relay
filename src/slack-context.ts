import { Buffer } from 'node:buffer';
import { clip, compareTs, markTruncated, projectFiles, sourceMessageFingerprint, timestampValid, updateCoverage, type ContextSection, type ContextMessage, type ThreadContext } from './context-envelope.js';
import type { SlackThreadEvent } from './thread-router.js';
import { agentBody } from './slack-message-text.js';

type RawMessage = Record<string, unknown>;
const permissionErrors = new Set(['missing_scope', 'not_in_channel', 'channel_not_found', 'access_denied', 'token_revoked', 'invalid_auth', 'account_inactive', 'thread_not_found']);
const MAX_CONTEXT_CALLS = 20;
const MAX_OPTIONAL_ROOTS = 2;
const MAX_NAME_LOOKUPS = 10;
const project = (raw: RawMessage, optional = false): ContextMessage => {
  const body=agentBody(raw);
  const text = body ?? (typeof raw.text === 'string' ? raw.text : '');
  return { ts: String(raw.ts), authorId: typeof raw.user === 'string' ? raw.user : typeof raw.bot_id === 'string' ? raw.bot_id : '',
    origin: raw.bot_id || raw.app_id ? 'bot_or_app' : 'unknown', text: optional ? clip(text,4096) : text, files: projectFiles(raw.files, optional ? 5 : Number.MAX_SAFE_INTEGER),
    ...(body!==undefined?{footerOmitted:true}:{}),
    sourceFingerprint: typeof raw.sourceFingerprint === 'string' && /^[a-f0-9]{64}$/u.test(raw.sourceFingerprint)
      ? raw.sourceFingerprint : sourceMessageFingerprint(raw),
    ...(optional&&Buffer.byteLength(text)>4096 ? {textTruncated:true} : {}),
    ...(optional&&Array.isArray(raw.files)&&raw.files.length>5 ? {filesTruncated:true} : {}) };
};
export interface ContextReadOptions { currentThreadOnly?: boolean; sinceTs?: string; includeNearby?: boolean; referenceTexts?: string[] }
export async function readContext(event: SlackThreadEvent, token: string, fetchImpl: typeof fetch = fetch, options: ContextReadOptions = {}): Promise<ThreadContext> {
  const readStartedAt = Date.now();
  if (!timestampValid(event.threadTs) || !timestampValid(event.messageTs) || compareTs(event.threadTs,event.messageTs)>0) throw new Error('invalid_context_scope');
  const deadline = AbortSignal.timeout(20_000);
  if(options.sinceTs&&(!timestampValid(options.sinceTs)||compareTs(options.sinceTs,event.threadTs)<0||compareTs(options.sinceTs,event.messageTs)>=0))throw new Error('invalid_context_scope');
  // Nearby discussion precedes the first mention, not the later follow-up.
  const [seconds,fraction] = event.messageTs.split('.');
  const oldest = `${BigInt(seconds!)>1800n ? BigInt(seconds!)-1800n : 0n}.${fraction}`;
  const threadRoots = new Map<string,string>();
  const parents = new Map<string,string>();
  let requests = 0, rawMessages = 0, sideStopped = false;
  const read = async (method: 'history'|'replies', rootTs = event.threadTs, optional = false, referenceTs?: string): Promise<ContextSection> => {
    const section: ContextSection = {status:'complete',messages:[]};
    let cursor = ''; const cursors = new Set<string>();
    try {
      const pageLimit = method === 'history' ? 5 : optional ? 3 : 10;
      let reachedEnd = false;
      for (let page=0;page<pageLimit;page++) {
        if (requests>=MAX_CONTEXT_CALLS || (optional && (sideStopped || deadline.aborted))) { markTruncated(section,'read_budget'); break; }
        deadline.throwIfAborted(); requests++;
        const params = new URLSearchParams({channel:event.channelId,limit:referenceTs&&method==='history'?'1':optional?'100':'200',latest:referenceTs&&method==='history'?referenceTs:event.messageTs,inclusive:'true',
          ...(method==='history'?{oldest:referenceTs??oldest}:{ts:rootTs,...(!optional&&options.sinceTs?{oldest:options.sinceTs}:{})}),...(cursor?{cursor}:{})});
        const response = await fetchImpl(`https://slack.com/api/conversations.${method}?${params}`,{headers:{authorization:`Bearer ${token}`},signal:deadline});
        if (response.status===429) throw new Error('context_rate_limited');
        if (!response.ok) throw new Error('context_upstream_failed');
        const reader=response.body?.getReader(); if(!reader)throw new Error('context_invalid_response');
        const chunks:Uint8Array[]=[];let bytes=0;
        try { for (;;) {const part=await reader.read();if(part.done)break;bytes+=part.value.byteLength;if(bytes>2*1024*1024)throw new Error('context_response_too_large');chunks.push(part.value);} }
        finally {await reader.cancel().catch(()=>{});}
        const body=JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if(body.ok!==true) {if(permissionErrors.has(body.error))return {status:'unavailable',reason:body.error,messages:[]};throw new Error('context_upstream_failed');}
        if(!Array.isArray(body.messages))throw new Error('context_invalid_response');
        rawMessages += body.messages.length;
        for(const raw of body.messages as RawMessage[]) {
          if(!timestampValid(raw.ts))throw new Error('context_invalid_response');
          if(raw.channel&&raw.channel!==event.channelId)throw new Error('invalid_context_scope');
          if(method==='replies'&&raw.thread_ts&&raw.thread_ts!==rootTs)throw new Error('invalid_context_scope');
          if(compareTs(raw.ts,event.messageTs)>0)continue;
          if(method==='history'&&(referenceTs?raw.ts!==referenceTs:compareTs(raw.ts,oldest)<0||compareTs(raw.ts,event.messageTs)>=0||(raw.thread_ts&&raw.thread_ts!==raw.ts)))continue;
          if(method==='history'&&!referenceTs&&raw.ts===event.threadTs)continue;
          if(method==='replies'&&compareTs(raw.ts,rootTs)<0)continue;
          if(method==='replies'&&!optional&&options.sinceTs&&raw.ts!==rootTs&&compareTs(raw.ts,options.sinceTs)<0)continue;
          if(optional&&raw.subtype&&!['bot_message','file_share','thread_broadcast','me_message'].includes(String(raw.subtype)))continue;
          if(timestampValid(raw.thread_ts))parents.set(raw.ts,raw.thread_ts);
          if(method==='history'&&typeof raw.reply_count==='number'&&raw.reply_count>0)
            threadRoots.set(raw.ts,timestampValid(raw.latest_reply)&&compareTs(raw.latest_reply,event.messageTs)<=0?raw.latest_reply:raw.ts);
          if(!section.messages.some(m=>m.ts===raw.ts))section.messages.push(project(raw,optional));
        }
        cursor=body.response_metadata?.next_cursor?.trim()||'';
        if(!cursor&&body.has_more===true)throw new Error('context_invalid_cursor');
        if(!cursor||method==='history'&&referenceTs){reachedEnd=true;break;}
        if(cursors.has(cursor))throw new Error('context_invalid_cursor');cursors.add(cursor);
        if(method==='history'&&section.messages.length>=12){markTruncated(section,'timeline_root_limit');break;}
        if(page===pageLimit-1)markTruncated(section,'page_limit');
      }
      if(method==='replies'&&!reachedEnd){
        if(!optional)throw new Error('context_required_page_limit');
        section.messages=section.messages.filter(m=>m.ts===rootTs);
        markTruncated(section,'latest_suffix_unavailable');
      }
    } catch(error) {
      if(!optional || (error instanceof Error && error.message==='invalid_context_scope'))throw error;
      sideStopped=true;
      if(method==='replies')section.messages=section.messages.filter(m=>m.ts===rootTs);
      markTruncated(section,error instanceof Error&&error.message==='context_rate_limited'?'rate_limited':'read_failed');
    }
    if (method==='replies' && section.status==='complete' && !section.messages.some(m=>m.ts===rootTs)&&!(options.sinceTs&&!optional)) {
      if(!optional)throw new Error('context_required_unavailable');
      return {status:'unavailable',reason:'thread_root_missing',messages:[]};
    }
    section.messages.sort((a,b)=>compareTs(a.ts,b.ts));
    if(referenceTs&&method==='replies'){
      const replies=section.messages.filter(m=>m.ts!==rootTs);
      const index=replies.findIndex(m=>m.ts===referenceTs);
      const chosen=index>=0?replies.slice(Math.max(0,index-2),index+3):referenceTs===rootTs?replies.slice(0,2):[];
      section.messages=[...section.messages.filter(m=>m.ts===rootTs),...chosen];
      for(const message of section.messages)message.change=message.ts===referenceTs?'referenced':'context';
      if(chosen.length<replies.length)markTruncated(section,'reference_window');
      if(index<0&&referenceTs!==rootTs)markTruncated(section,'reference_target_missing');
    }
    const limit=method==='history'?12:optional?6:Number.MAX_SAFE_INTEGER;
    if(section.messages.length>limit){
      const root=method==='replies'?section.messages.find(m=>m.ts===rootTs):undefined;
      section.messages=[...(root?[root]:[]),...section.messages.filter(m=>!root||m.ts!==root.ts).slice(-(limit-(root?1:0)))];
      markTruncated(section,method==='history'?'timeline_root_limit':'thread_message_limit');
    }
    if(section.messages.some(m=>m.textTruncated||m.filesTruncated))markTruncated(section,section.reason||'message_projection_limit');
    updateCoverage(section); return section;
  };
  // Reserve the first reads for the current thread before expanding optional branches.
  const current = event.messageTs===event.threadTs ? {status:'complete' as const,messages:[project({ts:event.messageTs,user:event.senderUserId,text:event.text,files:event.files,sourceFingerprint:event.sourceFingerprint})]} : await read('replies');
  if(current.status!=='complete')throw new Error('context_required_unavailable');
  let currentRoot=current.messages.find(m=>m.ts===event.threadTs);
  if(!currentRoot&&options.sinceTs){
    const root=await read('history',event.threadTs,false,event.threadTs);
    currentRoot=root.messages.find(m=>m.ts===event.threadTs);
  }
  if(!currentRoot)throw new Error('context_required_unavailable');
  const currentReplies:ContextSection={...current,messages:current.messages.filter(m=>m.ts!==event.threadTs)};
  if(event.messageTs!==event.threadTs&&!currentReplies.messages.some(m=>m.ts===event.messageTs))currentReplies.messages.push(project({ts:event.messageTs,user:event.senderUserId,text:event.text,files:event.files,sourceFingerprint:event.sourceFingerprint}));
  currentReplies.messages.sort((a,b)=>compareTs(a.ts,b.ts));
  updateCoverage(currentReplies);currentRoot.replies=currentReplies;
  const timeline:ContextSection=options.includeNearby===false?{status:'complete',messages:[]}:await read('history',event.threadTs,true);
  timeline.messages=timeline.messages.filter(m=>m.ts!==event.threadTs);
  const optionalRoots=timeline.messages.filter(m=>threadRoots.has(m.ts))
    .sort((a,b)=>compareTs(threadRoots.get(b.ts)!,threadRoots.get(a.ts)!)).slice(0,MAX_OPTIONAL_ROOTS);
  let next=0;
  await Promise.all(Array.from({length:Math.min(4,optionalRoots.length)},async()=>{
    while(next<optionalRoots.length){const root=optionalRoots[next++]!;const section=await read('replies',root.ts,true);section.messages=section.messages.filter(m=>m.ts!==root.ts);updateCoverage(section);root.replies=section;}
  }));
  // Explicit same-conversation links have a deterministic window; never recurse
  // through retrieved messages or read a linked conversation with wider access.
  const references=new Map<string,string|undefined>();
  const texts=options.currentThreadOnly ? [] : [event.text,...(options.referenceTexts??[]),currentRoot.text,...current.messages.map(m=>m.text)];
  for(const text of texts)for(const match of text.matchAll(/https:\/\/[^\s<>|]+/g)){
    let url:URL;try{url=new URL(match[0]);}catch{continue;}
    const target=url.pathname.match(/^\/archives\/([A-Z0-9]+)\/p(\d{7,})$/);
    if(!url.hostname.endsWith('.slack.com')||!target||target[1]!==event.channelId)continue;
    const digits=target[2]!;const ts=digits.slice(0,-6)+'.'+digits.slice(-6);
    if(compareTs(ts,event.messageTs)>0)continue;
    const parent=url.searchParams.get('thread_ts');
    references.set(ts,timestampValid(parent)&&compareTs(parent,ts)<=0?parent:undefined);
  }
  for(const [ts,hint] of [...references].slice(0,3)){
    if(currentRoot.ts===ts||currentReplies.messages.some(m=>m.ts===ts))continue;
    let rootTs=hint;
    if(!rootTs){
      const exact=await read('history',event.threadTs,true,ts);
      if(!exact.messages.length){timeline.messages.push({ts,authorId:'',origin:'unknown',text:'',files:[],contentStatus:'unavailable',change:'referenced'});continue;}
      rootTs=parents.get(ts)??ts;
    }
    const section=await read('replies',rootTs,true,ts);
    const root=section.messages.find(m=>m.ts===rootTs);
    if(!root){markTruncated(timeline,'reference_unavailable');continue;}
    if(rootTs===event.threadTs){
      currentReplies.messages=[...new Map([...section.messages.filter(m=>m.ts!==rootTs),...currentReplies.messages].map(m=>[m.ts,m])).values()].sort((a,b)=>compareTs(a.ts,b.ts));
      updateCoverage(currentReplies);
    }else{
      root.change='referenced';root.replies={...section,messages:section.messages.filter(m=>m.ts!==rootTs)};updateCoverage(root.replies);
      const existing=timeline.messages.find(m=>m.ts===rootTs);
      if(existing){existing.replies={...root.replies,messages:[...new Map([...(existing.replies?.messages??[]),...root.replies.messages].map(m=>[m.ts,m])).values()].sort((a,b)=>compareTs(a.ts,b.ts))};existing.change='referenced';updateCoverage(existing.replies);}
      else timeline.messages.push(root);
    }
  }
  if(references.size>3)markTruncated(timeline,'reference_limit');
  timeline.messages.push(currentRoot);timeline.messages.sort((a,b)=>compareTs(a.ts,b.ts));updateCoverage(timeline);
  return {anchorTs:event.threadTs,cutoffTs:event.messageTs,capturedAt:new Date().toISOString(),timeline,...(options.sinceTs?{sinceTs:options.sinceTs}:{}),
    readStats:{slackCalls:requests,rawMessages,messageReadMs:Date.now()-readStartedAt}};
}

// Names are display metadata only. Lookup failures must not block delivery of the request.
export async function enrichParticipantNames(event: SlackThreadEvent, context: ThreadContext, token: string, fetchImpl: typeof fetch = fetch): Promise<ThreadContext> {
  const nameStartedAt = Date.now();
  const ids = [...new Set([event.senderUserId, ...(event.mention.type === 'user' ? [event.mention.id] : []),
    ...context.timeline.messages.flatMap(m => [m.authorId, ...(m.replies?.messages.map(r => r.authorId) ?? [])])])].filter(Boolean);
  const participants: NonNullable<ThreadContext['participants']> = ids.map(id => ({ id }));
  const pending = participants.filter(p => /^[UW][A-Z0-9]+$/.test(p.id)).slice(0, MAX_NAME_LOOKUPS);
  const deadline = AbortSignal.timeout(3_000);
  let next = 0, stopped = false, lookups = 0;
  await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
    while (!stopped && !deadline.aborted && next < pending.length) {
      const person = pending[next++]!;
      try {
        lookups++;
        const response = await fetchImpl(`https://slack.com/api/users.info?${new URLSearchParams({ user: person.id })}`, {
          headers: { authorization: `Bearer ${token}` }, signal: deadline,
        });
        if (!response.ok) { stopped = true; continue; }
        const reader = response.body?.getReader();
        if (!reader) continue;
        const chunks: Uint8Array[] = []; let bytes = 0;
        try {
          for (;;) {
            const part = await reader.read(); if (part.done) break;
            bytes += part.value.byteLength;
            if (bytes > 128 * 1024) throw new Error('profile_response_too_large');
            chunks.push(part.value);
          }
        } finally { await reader.cancel().catch(() => {}); }
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (!body.ok) { if (body.error !== 'user_not_found') stopped = true; continue; }
        if (body.user?.id !== person.id) continue;
        const profile = body.user.profile;
        const name = [profile?.display_name, profile?.real_name, body.user.name]
          .find(value => typeof value === 'string' && value.trim());
        if (name) person.name = clip(name.trim(), 256);
      } catch { stopped = true; }
    }
  }));
  return { ...context, participants, readStats: {
    ...(context.readStats ?? {slackCalls:0,rawMessages:0}), nameLookupCalls:lookups, nameReadMs:Date.now()-nameStartedAt,
  } };
}
