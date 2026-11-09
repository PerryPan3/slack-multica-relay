import { createHmac, timingSafeEqual } from "node:crypto";
import { isOwnerSelfMention } from "./owner-local.js";
import { isCancelCommand } from "./mentions.js";
import { targetsIn } from "./github-review.js";
import type { ThreadContext } from "./context-envelope.js";
import type { SlackThreadEvent } from "./thread-router.js";

interface Scope {cancelKeywords?:ReadonlySet<string>;team:string; owner?:string; reviewChannel?:string; selfChannel?:string; localEnabled:boolean; businessChannel?:string; businessSenders?:string; businessTestDm?:string; key?:string}
export interface BusinessScope {businessChannel?:string; businessSenders?:string; businessTestDm?:string}
export function businessAllowed(event:SlackThreadEvent, scope:BusinessScope, owner:string|undefined, team:string) {
  if (!scope.businessChannel || !owner || event.teamId!==team || event.mention.type!=="user" || event.mention.id!==owner) return false;
  if (event.senderUserId===owner)
    return isOwnerSelfMention(event,owner,team) && /^D[A-Z0-9]+$/.test(event.channelId) && event.channelId===scope.businessTestDm;
  return event.channelId===scope.businessChannel && (scope.businessSenders||"").split(",").map(s=>s.trim()).includes(event.senderUserId);
}
export function routeTicket(event:SlackThreadEvent, context:ThreadContext, scope:Scope) {
  if (!scope.key || scope.team !== event.teamId || event.mention.type !== "user" || event.mention.id !== scope.owner) return;
  const own = isOwnerSelfMention(event,scope.owner,scope.team);
  const local = own && scope.localEnabled;
  const review = event.channelId === scope.reviewChannel ||
    (own && /^D[A-Z0-9]+$/.test(event.channelId) && event.channelId === scope.selfChannel);
  const business = businessAllowed(event,scope,scope.owner,scope.team);
  if (!review && !local && !business) return;

  const root = context.timeline.messages.find(m => m.ts === event.threadTs);
  const thread = root ? [root,...(root.replies?.messages || [])].filter(m => m.ts < event.messageTs) : [];
  const direct = targetsIn(event.text);
  const found = [...new Set(thread.flatMap(m => targetsIn(m.text)))];
  const targets = direct.length ? direct : found;
  const history = thread.map(m=>({author:m.authorId,text:m.text}));
  const cancellations = thread.filter(m=>m.authorId===scope.owner && isCancelCommand(m.text,
    new Set([scope.owner!]),new Set(),scope.cancelKeywords ?? new Set(["cancel","取消"])))
    .map(m=>({author:m.authorId,ts:m.ts}));
  const partial = false;
  const binding = [event.teamId,event.channelId,event.threadTs,event.messageTs,event.senderUserId,event.mention.id,event.text];
  const payload = {binding,review,local,business,history,targets,cancellations,overflow:false,
    partial};
  const body=Buffer.from(JSON.stringify(payload)).toString("base64url");
  return "intent1."+body+"."+createHmac("sha256",scope.key).update("intent/1:"+body).digest("hex");
}

export function businessPresentation(output:string,ticket:unknown,key?:string) {
  if (typeof ticket!=="string" || !ticket.startsWith("intent1.") || !key || !output.startsWith("relay-business/1 ")) return;
  const pieces=output.slice("relay-business/1 ".length).split(".");
  if (pieces.length!==2 || !/^[a-f0-9]{64}$/.test(pieces[1])) return;
  const [body,mac]=pieces;
  const expected=createHmac("sha256",key).update("business-result/1:"+ticket+":"+body).digest();
  if (!timingSafeEqual(expected,Buffer.from(mac,"hex"))) return;
  try {
    const data=JSON.parse(Buffer.from(body,"base64url").toString());
    if (typeof data.text!=="string" || !data.text.trim() || data.text.length>4000 ||
        !Number.isInteger(data.seconds) || data.seconds<0 || !Array.isArray(data.tools) ||
        data.tools.length>7 || data.tools.some((t:{name:string;count:number;failed:number})=>!t ||
          typeof t.name!=="string" || !/^[A-Za-z][A-Za-z0-9 .-]{0,39}$/.test(t.name) ||
          !Number.isInteger(t.count) || t.count<1 || t.count>7 ||
          !Number.isInteger(t.failed) || t.failed<0 || t.failed>t.count)) return;
    return data as {text:string;seconds:number;tools:{name:string;count:number;failed:number}[]};
  } catch { return; }
}
