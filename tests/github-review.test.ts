import { it, expect } from "vitest";
import { createHmac } from "node:crypto";
import { genericReviewReply, reviewPresentation, reviewStatistics, targetsIn } from "../src/github-review.js";
it.each(["","/files","/changes","/commits","/checks","/changes?diff=split#diff-abc"])("recognizes the PR represented by GitHub view %s", suffix=>{
  expect(targetsIn(`<https://github.com/team/repo/pull/750${suffix}|code changes> https://github.com/team/other/pull/696`))
    .toEqual(["team/repo/pull/750","team/other/pull/696"]);
});
it("rejects signed but malformed metadata and hides missing model data",()=>{
  function signed(value:unknown){
    const body=Buffer.from(JSON.stringify(value)).toString("base64url");
    return "relay-review/2 "+body+"."+createHmac("sha256","key").update("receipt:ticket:"+body).digest("hex");
  }
  const value={repo:"team/repo",number:19,review:null,sha:"a".repeat(40),seconds:25,text:"暂缓审批",status:"INCOMPLETE",tools:[]};
  expect(reviewPresentation(signed({...value,review:123}),"ticket","key")).toBeUndefined();
  expect(reviewPresentation(signed({...value,repo:"evil.com|@everyone"}),"ticket","key")).toBeUndefined();
  const parsed=reviewPresentation(signed(value),"ticket","key")!;
  expect(parsed.url).toBeUndefined();
  expect(reviewStatistics(parsed,[])).toBe("⏱ 25s");
});
it("rejects forged results and receipts copied from another message",()=>{
  const ticket="ticket";
  const payload=Buffer.from(JSON.stringify({repo:"team/repo",number:19,review:123,sha:"abcd1234",seconds:25,text:"已 Approve"})).toString("base64url");
  const mac=createHmac("sha256","key").update("receipt:"+ticket+":"+payload).digest("hex");
  const output="relay-review/1 "+payload+"."+mac;
  expect(reviewPresentation(output,ticket,"key")?.url).toContain("pullrequestreview-123");
  expect(reviewPresentation(output,"other","key")).toBeUndefined();
  expect(reviewPresentation(output,ticket,"wrong")).toBeUndefined();
});

it("does not promise an unstarted review in a text-only acknowledgement",()=>{
  expect(genericReviewReply("请瞧瞧 https://github.com/team/repo/pull/19","收到，我看下这个 PR。")).toContain("没有启动 GitHub 审查");
  expect(genericReviewReply("上次 review 的结果？","看起来缺少测试覆盖。")).toBe("看起来缺少测试覆盖。");
  expect(genericReviewReply("活动几点开始？","我看下日历。")).toBe("我看下日历。");
  expect(genericReviewReply("为什么上次没有 review","上一次缺少 PR 链接。")).toBe("上一次缺少 PR 链接。");
});

it("validates batch receipts and aggregates only verified child approvals",()=>{
  const sign=(value:unknown,prefix:string,scope:string)=>{
    const body=Buffer.from(JSON.stringify(value)).toString("base64url");
    return prefix+body+"."+createHmac("sha256","key").update(scope+"ticket:"+body).digest("hex");
  };
  const child=sign({repo:"team/repo",number:19,sha:"a".repeat(40),text:"approved",seconds:2,review:42,
    status:"APPROVE",tools:[{name:"GitHub",count:3,failed:0}]},"relay-review/2 ","receipt:");
  const value={text:"both reviewed",seconds:5,tools:[],reviews:[child]};
  const parsed=reviewPresentation(sign(value,"relay-agent/1 ","agent-result/1:"),"ticket","key")!;
  expect(parsed.repositoryLine).toContain("pullrequestreview-42");
  expect(parsed.tools[0].count).toBe(3);
  expect(reviewPresentation(sign({...value,reviews:[child+"bad"]},"relay-agent/1 ","agent-result/1:"),"ticket","key")).toBeUndefined();
});
