import { createHmac, timingSafeEqual } from "node:crypto";

// Slack links may carry an arbitrary display label. Only inspect the link destination.
const urlTokens = /<https?:\/\/[^>]+>|https?:\/\/[^\s<>]+/gi;
const prPath = /^\/([A-Za-z0-9][\w.-]*\/[A-Za-z0-9_.-]+)\/pull\/([1-9]\d*)(?:\/(?:files|changes|commits|checks))?\/?$/;
export function targetsIn(text: string): string[] {
  const matches = new Set<string>();
  for (const token of text.match(urlTokens) || []) {
    const raw = token.startsWith("<") ? token.slice(1,-1).split("|")[0]! : token;
    try {
      const url = new URL(raw);
      const match = prPath.exec(url.pathname);
      if (url.origin === "https://github.com" && !url.username && !url.password && match)
        matches.add(match[1] + "/pull/" + match[2]);
    } catch { /* Invalid URLs cannot identify an approval target. */ }
  }
  return [...matches];
}
// The generic responder cannot start a review. A vague acknowledgement should
// not make a requester wait for a job that does not exist.
export function genericReviewReply(request: string, output: string) {
  const body = output.trim();
  const reviewRelated = targetsIn(request).length > 0 || /(?:\breview\b|\bcr\b|审查|审核|评审|复核)/i.test(request);
  const acknowledgement = /^(?:收到|好的|可以|(?:(?:好的|收到|可以|好)[，,。！!\s]*)?(?:我(?:来|会|先|马上)?|这就)?(?:看下|看看|看一下|审一下|review|cr|审核|审查)\s*(?:这个|这条|该)?\s*(?:PR|改动|代码)?)[。！!\s]*$/i;
  if (reviewRelated && body.length < 100 && acknowledgement.test(body)) {
    return "这条消息没有启动 GitHub 审查。请在本线程发「@Perry review」并附上目标 PR 链接。";
  }
  return body;
}

interface Presentation {
  internalFailure?: boolean;
  text:string; url?:string; prUrl:string; tools:{name:string;count:number;failed:number}[];
  seconds:number; repositoryLine:string; models?:string[];
}

function agentPresentation(output:string,ticket:string,key:string): Presentation|undefined {
  const parts=output.slice("relay-agent/1 ".length).split(".");
  if(parts.length!==2 || !/^[a-f0-9]{64}$/.test(parts[1])) return;
  const [body,mac]=parts;
  const expected=createHmac("sha256",key).update("agent-result/1:"+ticket+":"+body).digest();
  if(!timingSafeEqual(expected,Buffer.from(mac,"hex"))) return;
  try {
    const data=JSON.parse(Buffer.from(body,"base64url").toString());
    if(typeof data.text!=="string" || !data.text.trim() || data.text.length>32000 ||
       !Number.isSafeInteger(data.seconds) || data.seconds<0 || !Array.isArray(data.reviews) ||
       !Array.isArray(data.tools)) return;
    if(data.models!==undefined && (!Array.isArray(data.models) || data.models.some((m:unknown)=>typeof m!=="string" || !/^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,99}$/.test(m)))) return;
    if (data.internalFailure !== undefined && data.internalFailure !== true) return;
    if (data.internalFailure && data.reviews.length) return;
    const reviews:Presentation[]=[];
    for(const receipt of data.reviews) {
      if(typeof receipt!=="string" || !receipt.startsWith("relay-review/2 ")) return;
      const review=reviewPresentation(receipt,ticket,key);
      if(!review) return;
      reviews.push(review);
    }
    const tools=new Map<string,{name:string;count:number;failed:number}>();
    for(const call of [...data.tools,...reviews.flatMap(r=>r.tools)]) {
      if(!call || typeof call.name!=="string" || !/^[A-Za-z][A-Za-z0-9 .-]{0,39}$/.test(call.name) ||
         !Number.isSafeInteger(call.count) || call.count<1 || !Number.isSafeInteger(call.failed) ||
         call.failed<0 || call.failed>call.count) return;
      const sum=tools.get(call.name) || {name:call.name,count:0,failed:0};
      sum.count+=call.count; sum.failed+=call.failed; tools.set(call.name,sum);
    }
    return {text:data.text,seconds:data.seconds,tools:[...tools.values()],prUrl:"",models:data.models,internalFailure:data.internalFailure,
            repositoryLine:reviews.map(r=>r.repositoryLine).join("\n")};
  } catch { return; }
}

export function reviewPresentation(output: string, ticket: unknown, key?: string): Presentation|undefined {
  if(key && typeof ticket==="string" && output.startsWith("relay-agent/1 "))
    return agentPresentation(output,ticket,key);
  if (!key || typeof ticket !== "string" || !/^relay-review\/[12] /.test(output)) return;
  const [body, mac] = output.slice(15).split(".");
  if (!body || !mac || !/^[a-f0-9]{64}$/.test(mac)) return;
  const expected = createHmac("sha256", key).update("receipt:" + ticket + ":" + body).digest();
  if (!timingSafeEqual(expected, Buffer.from(mac, "hex"))) return;
  try {
    const r = JSON.parse(Buffer.from(body, "base64url").toString());
    if (!r || typeof r.text !== "string" || !r.text.trim() || typeof r.repo !== "string" ||
      !/^[A-Za-z0-9][\w.-]*\/[A-Za-z0-9_.-]+$/.test(r.repo) ||
      !Number.isSafeInteger(r.number) || r.number < 1 || typeof r.sha !== "string" ||
      !/^[a-f0-9]{8,40}$/.test(r.sha) || !Number.isSafeInteger(r.seconds) || r.seconds < 0) return;
    const modern = output.startsWith("relay-review/2 ");
    if (modern && !["APPROVE", "REQUEST_CHANGES", "INCOMPLETE", "INSPECTED"].includes(r.status)) return;
    const incomplete = modern && ["INCOMPLETE", "INSPECTED"].includes(r.status);
    if (incomplete ? r.review !== null : (!Number.isSafeInteger(r.review) || r.review < 1)) return;
    const tools: {name:string; count:number; failed:number}[] = modern ? r.tools : [];
    if (!Array.isArray(tools) || tools.length > 20 || tools.some(t => !t ||
      typeof t.name !== "string" || !/^[A-Za-z][A-Za-z0-9 .-]{0,39}$/.test(t.name) ||
      !Number.isSafeInteger(t.count) || t.count < 1 || t.count > 10000 ||
      !Number.isSafeInteger(t.failed) || t.failed < 0 || t.failed > t.count)) return;
    const prUrl = "https://github.com/" + r.repo + "/pull/" + r.number;
    const url = incomplete ? undefined : prUrl + "#pullrequestreview-" + r.review;
    const branch = typeof r.branch === "string" && r.branch.length <= 200 && !/[\s`<>|&]/.test(r.branch) ? r.branch : "";
    return { text: r.text as string, url, prUrl, tools, seconds: r.seconds as number,
      repositoryLine: "🔗 " + r.repo.split("/")[1] + (branch ? " · `" + branch + "`" : "") +
        " · <" + prUrl + "|PR #" + r.number + "> · `" + r.sha.slice(0,8) + "`" +
        (url ? " · <" + url + "|" + (r.status === "REQUEST_CHANGES" ? "Request changes" : modern ? "Approve" : "GitHub 审批") + ">" : r.status === "INSPECTED" ? " · 只读复核" : " · 暂缓审批") };
  } catch { return; }
}

export function reviewStatistics(review: {seconds:number;tools:{name:string;count:number;failed:number}[]}, models: string[]) {
  const minutes = Math.floor(review.seconds / 60), seconds = review.seconds % 60;
  const duration = (minutes ? minutes + "m " : "") + seconds + "s";
  const verifiedModels = [...new Set(models.filter(m => /^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,99}$/.test(m)))];
  return ["⏱ " + duration, ...(verifiedModels.length ? ["🤖 " + verifiedModels.join(" / ")] : []),
    ...review.tools.map(t => "🔧 " + t.name + " ×" + t.count + (t.failed ? "（" + t.failed + " 次未取得结果）" : ""))].join(" · ");
}
