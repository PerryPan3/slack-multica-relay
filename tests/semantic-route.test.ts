import {expect,it} from "vitest";
import {routeTicket} from "../src/semantic-route.js";
import type {ThreadContext} from "../src/context-envelope.js";
import type {SlackThreadEvent} from "../src/thread-router.js";
const e={teamId:"T1",channelId:"C2",senderUserId:"KEVEN",messageTs:"20.1",threadTs:"10.1",text:"<@PERRY> 帮忙过一眼",mention:{type:"user",id:"PERRY"}} as SlackThreadEvent;
const scope={key:"test",team:"T1",owner:"PERRY",reviewChannel:"C2",selfChannel:"D1",localEnabled:true};
const context={timeline:{messages:[{ts:"10.1",authorId:"KEVEN",text:"https://github.com/team/repo/pull/19"},
  {ts:"9.1",authorId:"OTHER",text:"https://github.com/team/repo/pull/20"}]}} as ThreadContext;
function claim(event:SlackThreadEvent){const t=routeTicket(event,context,scope)!;return JSON.parse(Buffer.from(t.split(".")[1],"base64url").toString());}
it("passes unfamiliar phrases and the actual root to the classifier without signing a local grant for colleagues",()=>{
  expect(claim(e)).toMatchObject({review:true,local:false,targets:["team/repo/pull/19"]});
  expect(claim(e).history[0].text).toContain("pull/19");
  expect(claim({...e,text:"<@PERRY> 为什么没有处理？"}).binding.at(-1)).toContain("为什么");
});
it("restricts eligible scopes and prioritizes a current target over thread history",()=>{
  expect(claim({...e,senderUserId:"PERRY",channelId:"D1"})).toMatchObject({local:true,review:true});
  expect(claim({...e,text:"<@PERRY> 麻烦看看 https://github.com/team/repo/pull/21"}).targets).toEqual(["team/repo/pull/21"]);
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
