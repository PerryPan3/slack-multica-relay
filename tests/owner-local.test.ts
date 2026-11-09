import {it,expect} from "vitest";
import {isOwnerSelfMention} from "../src/owner-local.js";
import type {SlackThreadEvent} from "../src/thread-router.js";
const e: SlackThreadEvent={teamId:"T1",channelId:"C1",senderUserId:"U1",threadTs:"10.01",messageTs:"11.01",mention:{type:"user",id:"U1"},text:"<@U1> 查本机"};
it("only issues a workspace capability to the owner who mentions themselves",()=>{
  expect(isOwnerSelfMention(e,"U1","T1")).toBe(true);
  for(const event of [{...e,senderUserId:"U2"},{...e,teamId:"T2"},{...e,text:"U1 查本机"},
    {...e,mention:{type:"subteam" as const,id:"U1"}}]){
    expect(isOwnerSelfMention(event,"U1","T1")).toBe(false);
  }
});
