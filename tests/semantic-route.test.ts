import {expect,it} from "vitest";
import {routeTicket,businessAllowed} from "../src/semantic-route.js";
import {channelSenderAllowed} from "../src/worker-reply.js";
import type {RelayConfig} from "../src/config.js";
import type {ThreadContext} from "../src/context-envelope.js";
import type {SlackThreadEvent} from "../src/thread-router.js";
const e={teamId:"T1",channelId:"C2",senderUserId:"KEVEN",messageTs:"20.1",threadTs:"10.1",text:"<@PERRY> 帮忙过一眼",mention:{type:"user",id:"PERRY"}} as SlackThreadEvent;
const scope={key:"test",team:"T1",owner:"PERRY",reviewChannel:"C2",selfChannel:"D1",localEnabled:true};
const context={timeline:{messages:[{ts:"10.1",authorId:"KEVEN",text:"https://github.com/team/repo/pull/19"},
  {ts:"9.1",authorId:"OTHER",text:"https://github.com/team/repo/pull/20"}]}} as ThreadContext;

it("keeps multi-channel colleague grants paired through admission and signed capabilities",()=>{
  const grants="C2:KEVEN,C2:MINGHO,C2:PEER3,C2:PEER4,C3:PEER4";
  const config={allowedChannelIds:new Set(["C2","C3"]),allowedSenderIds:new Set(["PERRY"]),
    blockedSenderIds:new Set(),allowAllSenders:false} as RelayConfig;
  const enabled={...scope,reviewGrants:grants,businessGrants:grants};
  for(const sender of ["KEVEN","MINGHO","PEER3","PEER4","OTHER"]){
    for(const channel of ["C2","C3","C4","D9"]){
      const event={...e,senderUserId:sender,channelId:channel};
      const allowed=(channel==="C2" && sender!=="OTHER") || (channel==="C3" && sender==="PEER4");
      expect(channelSenderAllowed(event,config,grants)).toBe(allowed);
      expect(businessAllowed(event,enabled,"PERRY","T1")).toBe(allowed);
      if(allowed){
        const ticket=routeTicket(event,context,enabled)!;
        const signed=JSON.parse(Buffer.from(ticket.split(".")[1],"base64url").toString());
        expect(signed).toMatchObject({review:true,business:true,local:false});
      }
    }
  }
  expect(routeTicket({...e,senderUserId:"PEER3",channelId:"C3"},context,enabled)).toBeUndefined();
  expect(businessAllowed({...e,teamId:"OTHER"},enabled,"PERRY","T1")).toBe(false);
  expect(channelSenderAllowed({...e,senderUserId:"PEER4",channelId:"C3"},
    {...config,blockedSenderIds:new Set(["PEER4"])},grants)).toBe(false);
});
function claim(event:SlackThreadEvent){const t=routeTicket(event,context,scope)!;return JSON.parse(Buffer.from(t.split(".")[1],"base64url").toString());}
it("allows four colleagues in both selected channels and only their own direct messages",()=>{
  const peers=["KEVEN","MINGHO","PEER3","PEER4"];
  const dms=["D2","D3","D4","D5"];
  const grants=peers.flatMap((sender,i)=>["C2:"+sender,"C3:"+sender,dms[i]+":"+sender]).join(",");
  const config={allowedChannelIds:new Set(["C2","C3",...dms]),allowedSenderIds:new Set(["PERRY"]),
    blockedSenderIds:new Set(),allowAllSenders:false} as RelayConfig;
  const enabled={...scope,reviewGrants:grants,businessGrants:grants};
  for(const sender of [...peers,"OTHER"]){
    for(const channel of ["C2","C3","C4","G9","D1",...dms,"D9"]){
      const event={...e,senderUserId:sender,channelId:channel};
      const allowed=peers.includes(sender) && (["C2","C3"].includes(channel) || channel===dms[peers.indexOf(sender)]);
      expect(channelSenderAllowed(event,config,grants),sender+":"+channel).toBe(allowed);
      expect(businessAllowed(event,enabled,"PERRY","T1")).toBe(allowed);
      if(allowed){
        const ticket=routeTicket(event,context,enabled)!;
        expect(JSON.parse(Buffer.from(ticket.split(".")[1],"base64url").toString()))
          .toMatchObject({review:true,business:true,local:false});
      }
    }
  }
  const dm={...e,channelId:"D2"};
  expect(routeTicket({...dm,mention:{type:"user",id:"OTHER"}},context,enabled)).toBeUndefined();
  expect(routeTicket({...dm,teamId:"T2"},context,enabled)).toBeUndefined();
  expect(routeTicket(dm,context,{...enabled,reviewGrants:"",businessGrants:""})).toBeUndefined();
  expect(channelSenderAllowed(dm,{...config,blockedSenderIds:new Set(["KEVEN"])},grants)).toBe(false);
});
it.each(["D9","G9","C9"])("preserves the owner's review capability for an admitted self-assignment in %s",channelId=>{
  expect(claim({...e,senderUserId:"PERRY",channelId})).toMatchObject({local:true,review:true});
  expect(routeTicket({...e,senderUserId:"KEVEN",channelId},context,scope)).toBeUndefined();
});
it("passes unfamiliar phrases and the actual root to the classifier without signing a local grant for colleagues",()=>{
  expect(claim(e)).toMatchObject({review:true,local:false,targets:["team/repo/pull/19"]});
  expect(claim(e).history[0].text).toContain("pull/19");
  expect(claim({...e,text:"<@PERRY> 为什么没有处理？"}).binding.at(-1)).toContain("为什么");
});
it("restricts eligible scopes and preserves both current and referenced thread candidates",()=>{
  expect(claim({...e,senderUserId:"PERRY",channelId:"D1"})).toMatchObject({local:true,review:true});
  expect(claim({...e,text:"<@PERRY> 这个和上面的都看下 https://github.com/team/repo/pull/21"}).targets).toEqual(["team/repo/pull/21","team/repo/pull/19"]);
  expect(routeTicket({...e,channelId:"D2"},context,scope)).toBeUndefined();
  expect(routeTicket({...e,teamId:"T2"},context,scope)).toBeUndefined();
});

it("grants business reads only to the named colleagues in the designated channel or the owner in the test self-DM",()=>{
  const enabled={...scope,businessChannel:"C2",businessSenders:"KEVEN,MINGHO",businessTestDm:"D1"};
  function permissions(senderUserId:string,channelId:string,teamId="T1"){
    const t=routeTicket({...e,senderUserId,channelId,teamId},context,enabled);
    return t && JSON.parse(Buffer.from(t.split(".")[1],"base64url").toString());
  }
  for(const who of ["KEVEN","MINGHO"]) {
    expect(permissions(who,"C2")).toMatchObject({business:true,local:false});
    expect(permissions(who,"D1")).toBeUndefined();
    expect(permissions(who,"C3")).toBeUndefined();
  }
  expect(permissions("OTHER","C2").business).toBe(false);
  expect(permissions("PERRY","C2").business).toBe(false);
  expect(permissions("PERRY","D1").business).toBe(true);
  expect(permissions("PERRY","D9").business).toBe(false);
  expect(permissions("KEVEN","C2","T2")).toBeUndefined();
});


it("carries only authenticated owner cancellation events from the same thread",()=>{
  const input={timeline:{messages:[{ts:"10.1",authorId:"KEVEN",text:"https://github.com/team/repo/pull/19",
    replies:{messages:[{ts:"11.1",authorId:"PERRY",text:"<@PERRY> 取消"},
      {ts:"12.1",authorId:"OTHER",text:"<@PERRY> 取消"},
      {ts:"13.1",authorId:"PERRY",text:"引用：<@PERRY> 取消"}]}}]}} as ThreadContext;
  const ticket=routeTicket(e,input,scope)!;
  expect(JSON.parse(Buffer.from(ticket.split(".")[1],"base64url").toString()).cancellations)
    .toEqual([{author:"PERRY",ts:"11.1"}]);
});
