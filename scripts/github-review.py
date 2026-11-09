"""Local GitHub transport for a signed Slack review request. The model never receives credentials."""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
import review_evidence as evidence

ROOT = Path(__file__).resolve().parents[1] / ".private/runtime/review"
GH = "/opt/homebrew/bin/gh"
LOGIN = None
KEY = None
GH_CALLS = 0


class Stop(Exception):
    pass


class RevisionChanged(Stop):
    def __init__(self, before, latest):
        self.latest = latest
        after = revision(latest)
        super().__init__(f"审查期间版本变化（base {before[0][:8]} → {after[0][:8]}，head {before[1][:8]} → {after[1][:8]}）。")


def require_revision(expected, latest):
    if list(expected) != revision(latest):
        raise RevisionChanged(expected, latest)


def encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mac(text):
    return hmac.new(KEY, text.encode(), hashlib.sha256).hexdigest()


def authorized(ticket, event):
    data, sig = ticket.split(".")
    if not hmac.compare_digest(mac("request:" + data), sig):
        raise Stop("请求未通过 Slack 来源验证。")
    fields = json.loads(base64.urlsafe_b64decode(data + "==="))
    expected = [event[k] for k in ("teamId", "channelId", "threadTs", "messageTs", "senderUserId")]
    expected += [event["mention"]["id"], event["text"]]
    options = {"mode":"review"}
    if isinstance(fields, list) and len(fields) == 9:
        options = fields.pop()
        if (not isinstance(options, dict) or set(options) != {"mode","previous"} or
                options["mode"] != "inspect" or not isinstance(options["previous"], str) or len(options["previous"]) > 1200):
            raise Stop("无效的只读复核请求。")
    if fields[:-1] != expected or not -60 <= time.time() - float(event["messageTs"]) <= 3600:
        raise Stop("Slack 请求已过期或与原消息不一致。")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+/pull/[1-9]\d*", fields[-1], re.ASCII):
        raise Stop("无法确定唯一的 GitHub PR。")
    repo, number = fields[-1].rsplit("/pull/", 1)
    return repo, int(number), options


def api(path, *, pages=False, diff=False, write=None):
    global GH_CALLS
    GH_CALLS += 1
    args = [GH, "api", "--hostname", "github.com"]
    if pages:
        args += ["--paginate", "--slurp"]
    if diff:
        args += ["-H", "Accept: application/vnd.github.diff"]
    if write is not None:
        args += ["--method", "POST", "--input", "-"]
    for attempt in range(1 if write is not None else 3):
        result = subprocess.run(args + [path], input=json.dumps(write,ensure_ascii=False) if write is not None else None,
                                capture_output=True, encoding="utf-8", timeout=100,
                                env={"HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/bin:/bin", "GH_PROMPT_DISABLED": "1"})
        if not result.returncode:
            break
        status = re.search(r"HTTP (\d{3})", result.stderr)
        if status and status[1] == "404":
            raise Stop("GitHub 未找到请求资源（HTTP 404）。可能是文件或 API 路径不存在，不能仅凭此认定仓库无权限。")
        if status and status[1] in ("401","403","422"):
            raise Stop("GitHub 拒绝了这次请求（HTTP " + status[1] + "），请核对仓库访问和审查权限。")
        if write is not None or attempt == 2:
            category = next((word for word in ("timeout","EOF","reset","TLS","GraphQL") if word.lower() in result.stderr.lower()), "transport")
            raise Stop("GitHub " + ("写入" if write is not None else "读取") + "未取得回执（" + (status[1] if status else category) + "）。")
        time.sleep(attempt+1)
    if diff:
        return result.stdout
    rows = json.loads(result.stdout)
    return [r for page in rows for r in page] if pages else rows


def snapshot(repo, number, *, read_only=False):
    if api("user")["login"].lower() != LOGIN.lower():
        raise Stop("本机 GitHub 登录身份已改变。")
    pr = api(f"repos/{repo}/pulls/{number}")
    if not read_only:
        require_open_review(pr)
    return pr


def require_open_review(pr):
    if pr["state"] != "open":
        reason = "已合并" if pr.get("merged_at") else "已关闭"
        raise Stop(f"PR {reason}，无法再提交正式审批。")
    if pr["draft"]:
        raise Stop("PR 仍为草稿，无法提交正式审批。")
    if pr["user"]["login"].lower() == LOGIN.lower():
        raise Stop("审查账号是此 PR 的作者，无法审批自己的代码。")


def revision(pr):
    return [pr["base"]["sha"], pr["head"]["sha"]]


def received(rows, attempt, head, decision=None):
    for r in reversed(rows):
        if r["user"]["login"].lower() != LOGIN.lower() or r["commit_id"] != head:
            continue
        if r["state"] not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        body = r.get("body") or ""
        if attempt in body or (decision == r["state"] and "<!-- perry-relay-review " in body):
            return r
        break


def collect(endpoint, pr):
    files = api(endpoint + "/files?per_page=100", pages=True)
    patch = api(endpoint, diff=True)
    if len(files) != pr["changed_files"] or len(re.findall(r"^diff --git ", patch, re.M)) != len(files):
        raise Stop("GitHub 未返回完整的文件差异，无法给出正式审批。")
    counts = Counter()
    inside = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            inside = False
        elif line.startswith("@@"):
            inside = True
        elif inside and line[:1] in ("+", "-"):
            counts[line[0]] += 1
    if (counts["+"], counts["-"]) != (pr["additions"], pr["deletions"]):
        raise Stop("GitHub 返回的 diff 行数与 PR 不一致，无法完整审查。")
    if re.search(r"^(Binary files .* differ|GIT binary patch)$", patch, re.M):
        raise Stop("PR 包含未能验证的二进制文件，未提交审批。")
    reviews = api(endpoint + "/reviews?per_page=100", pages=True)
    inline = api(endpoint + "/comments?per_page=100", pages=True)
    notes = [{k:r.get(k) for k in ("state","body","commit_id","html_url")} | {"author":r["user"]["login"]} for r in reviews if r.get("body") or r.get("state") in ("APPROVED","CHANGES_REQUESTED","DISMISSED")]
    comments = [{k:r.get(k) for k in ("id","path","line","original_line","body","in_reply_to_id","commit_id")} for r in inline]
    packet = {"title":pr["title"], "description":pr["body"], "state":pr["state"], "merged_at":pr.get("merged_at"), "base_head":revision(pr), "previous_reviews":notes,
              "inline_discussion":comments, "full_diff":patch}
    if len(json.dumps(packet,ensure_ascii=False).encode()) > 480000:
        raise Stop("本轮完整 diff 和历史意见超过单次审查容量，未提交审批。")
    # Supply surrounding source where feasible, at exact blobs from the changed-file manifest.
    source = {}
    budget = 90000
    for f in files[:32]:
        if f["status"] == "removed" or budget < 2000:
            continue
        if not f.get("patch") or len(f["patch"]) > 32000:
            continue
        blob = api(endpoint.split("/pulls/")[0] + "/git/blobs/" + f["sha"])
        if blob.get("size",budget+1) <= min(28000,budget):
            raw = base64.b64decode(blob["content"])
            source[f["filename"]] = raw.decode("utf-8",errors="replace")
            budget -= len(raw)
    packet["available_head_files"] = source
    return packet


def stable_packet(repo, number, endpoint, pr, focus, context):
    try:
        packet = collect(endpoint, pr)
    except Stop:
        require_revision(revision(pr), snapshot(repo, number, read_only=context.get("request_mode") == "inspect"))
        raise
    require_revision(revision(pr), snapshot(repo, number, read_only=context.get("request_mode") == "inspect"))
    return {**packet, **context, "requested_review_focus": focus[:2000]}


def prepare(repo, number, endpoint, pr, state, db, focus="", context=None):
    started = int(time.time())
    refresh_instruction = '只返回 JSON {"decision":"REFRESH"}。本机宿主会重新读取最新代码。'
    existing = db.execute("select base,head from jobs where id=?", (state,)).fetchone()
    if existing and list(existing) != revision(pr):
        return refresh_instruction
    for retry in range(3):
        try:
            packet = stable_packet(repo, number, endpoint, pr, focus, context or {})
            break
        except RevisionChanged as change:
            if retry == 2:
                raise Stop(f"{change} 连续三次读取都遇到更新，未取得稳定的代码版本。") from change
            pr = change.latest
    db.execute("insert or ignore into jobs(id,base,head,started) values(?,?,?,?)",
               (state,*revision(pr),started))
    # Every external model answer for this ticket must correspond to its immutable first pin.
    pin = db.execute("select base,head from jobs where id=?", (state,)).fetchone()
    if list(pin) != revision(pr):
        return refresh_instruction
    db.execute("create table if not exists job_metrics(id text primary key, github_calls integer not null)")
    db.execute("insert or replace into job_metrics values(?,?)", (state, GH_CALLS))
    db.commit()
    policy = json.loads(evidence.POLICY.read_text())
    return evidence.prompt(packet,evidence.operations(policy,repo),[])


def signed_result(repo, number, pr, ticket, text, started, decision, review=None, history=(), policy=None, prior_calls=0):
    groups = {}
    for call in history:
        op = (policy or {}).get("operations", {}).get(call["name"], {})
        label = op.get("display_name", "MCP")
        group = groups.setdefault(label, {"name":label, "count":0, "failed":0})
        group["count"] += 1
        group["failed"] += int(not call["result"].get("ok"))
    counts = [{"name":"GitHub", "count":prior_calls+GH_CALLS, "failed":0}] if prior_calls+GH_CALLS else []
    result = {"text":text, "repo":repo, "number":number, "review":review, "sha":pr["head"]["sha"],
              "branch":pr["head"].get("ref", ""), "status":decision,
              "seconds":max(0,int(time.time())-started), "tools":counts+list(groups.values())}
    body = encode(json.dumps(result,ensure_ascii=False).encode())
    return "relay-review/2 "+body+"."+mac("receipt:"+ticket+":"+body)


# These are transport claims, not review findings. Only GitHub can confirm delivery.
_DELIVERY_DISCLAIMER = re.compile(
    r"^(?:(?:此结论为|本轮为|本次为)只读审查意见[；;，,]\s*)?"
    r"(?:当前授权操作不包含\s*GitHub\s*审批提交[；;，,]\s*)?"
    r"(?:尚未|未)提交(?:正式|\s*GitHub\s*)?审批(?:[，,]也没有审批链接)?[。.]?$"
)


def finding_text(value):
    return "\n".join(line for line in value.splitlines()
                     if not _DELIVERY_DISCLAIMER.fullmatch(line.strip())).strip()


def finish(repo, number, endpoint, pr, state, ticket, output, db, history=(), policy=None, prior_calls=0, mode="review"):
    row = db.execute("select base,head,started,attempting from jobs where id=?", (state,)).fetchone()
    if not row:
        raise Stop("找不到本次审查读取代码的记录。")
    require_revision(row[:2], pr)
    verdict = json.loads(output.strip().removeprefix("```json").removesuffix("```").strip())
    decision, summary, details = (verdict[k] for k in ("decision","summary","details"))
    if not all(isinstance(x,str) and x.strip() for x in (summary,details)) or len(summary)>500 or len(details)>2500:
        raise Stop("审查结果不完整，未提交 GitHub。")
    if mode == "inspect":
        text = f"只读复核 · {repo.split('/')[-1]} #{number} · {row[1][:8]}"
        return signed_result(repo,number,pr,ticket,text+"\n\n"+summary+"\n\n"+details,row[2],"INSPECTED",
                             history=history,policy=policy,prior_calls=prior_calls)
    if mode != "review":
        raise Stop("未知审查模式。")
    if decision == "INCOMPLETE":
        text = "暂缓审批 · " + repo.split("/")[1] + " #" + str(number) + " · " + row[1][:8]
        return signed_result(repo,number,pr,ticket,text+"\n\n"+summary+"\n\n"+details,row[2],decision,
                             history=history,policy=policy,prior_calls=prior_calls)
    states = {"APPROVE":"APPROVED", "REQUEST_CHANGES":"CHANGES_REQUESTED"}
    if decision not in states:
        raise Stop("Agent 未给出明确的审查决定。")
    summary, details = finding_text(summary), finding_text(details)
    if not summary and not details:
        raise Stop("缺少可发布的审查依据，未提交审批。")
    review_body = "\n\n".join(part for part in (summary,details) if part)
    marker = "<!-- perry-relay-review " + state + " -->"
    reviews = api(endpoint + "/reviews?per_page=100", pages=True)
    record = received(reviews, marker, row[1], states[decision])
    if not record:
        db.execute("create table if not exists pending_reviews(target text primary key, review integer not null)")
        target = f"{repo.lower()}/{number}@{row[0]}..{row[1]}"
        saved = db.execute("select review from pending_reviews where target=?", (target,)).fetchone()
        if row[3] and not saved:
            raise Stop("GitHub 上次提交结果待核对，已阻止重复审批。")
        require_revision(revision(pr), snapshot(repo,number))
        if not saved:
            if db.execute("update jobs set attempting=1 where id=? and attempting=0",(state,)).rowcount != 1:
                raise Stop("同一请求已有审批在处理，请稍后核对 GitHub。")
            db.commit()
            draft = api(endpoint + "/reviews",write={"commit_id":row[1]})
            saved = (int(draft["id"]),)
            db.execute("insert into pending_reviews values(?,?)",(target,saved[0]))
            db.commit()
        # A retry may adopt an existing draft for this target. Record the attempt before submitting it.
        db.execute("update jobs set attempting=1 where id=? and attempting=0", (state,))
        db.commit()
        review_path = endpoint + "/reviews/" + str(saved[0])
        draft = api(review_path)
        if (draft["state"] != "PENDING" or draft["commit_id"] != row[1] or
                draft["user"]["login"].lower() != LOGIN.lower() or draft.get("body") or api(review_path + "/comments")):
            raise Stop("GitHub 审批草稿已变化，未提交。")
        require_revision(revision(pr), snapshot(repo,number))
        try:
            api(review_path + "/events",write={"event":decision,"body":review_body+"\n\n"+marker})
        except (Stop,subprocess.TimeoutExpired):
            check = api(review_path)
            if check["state"] != states[decision] or marker not in (check.get("body") or ""):
                raise
        record = {"id":saved[0]}
        db.execute("delete from pending_reviews where target=? and review=?", (target,saved[0]))
        db.commit()
    receipt = api(endpoint + "/reviews/" + str(int(record["id"])))
    if receipt["commit_id"] != row[1] or receipt["state"] != states[decision] or receipt["user"]["login"].lower() != LOGIN.lower():
        raise Stop("GitHub 回执与预期不一致，未确认审批。")
    fresh = api(endpoint)
    lead = ("已 Approve" if decision=="APPROVE" else "已 Request changes") + " · " + repo.split("/")[1] + " #" + str(number) + " · " + row[1][:8]
    if revision(fresh) != revision(pr):
        lead += "（审批后 PR 已更新，需要重审）"
    # If GitHub already had this verdict on this SHA, show the actual recorded reasoning.
    reason = receipt["body"].split("<!-- perry-relay-review ")[0].strip()
    return signed_result(repo,number,pr,ticket,lead+"\n\n"+reason,row[2],decision,receipt["id"],
                         history,policy,prior_calls)


def main():
    global KEY, LOGIN, GH
    config = json.loads((ROOT/"config.json").read_text())
    LOGIN, GH = config["login"], config["gh"]
    args = json.load(sys.stdin)
    KEY = (ROOT/"key").read_text().strip().encode()
    ticket = args["ticket"]
    if isinstance(ticket, str) and ticket.startswith("owner1."):
        try:
            import owner_local
        except ModuleNotFoundError as error:
            if error.name != 'owner_local':
                raise
            raise Stop('此运行环境未配置本机任务。') from error
        return owner_local.dispatch(sys.argv[1],args,KEY)
    if ticket.startswith("intent1."):
        import slack_intent
        return slack_intent.dispatch(sys.argv[1], args, KEY, run_review)
    repo, number, options = authorized(ticket, args["event"])
    return run_review(sys.argv[1],args,repo,number,options)


def interrupted_report(db, state, pr, reason):
    attempted = db.execute("select 1 from jobs where (id=? or id glob ?) and attempting=1 limit 1",
                           (state, state + ":*")).fetchone()
    outcome = "本次曾尝试写入，需核对 GitHub 回执。" if attempted else "本次没有发起 GitHub 审批写入。"
    return f'未确认 GitHub 审批 · {pr["head"]["sha"][:8]}。{reason} {outcome}'


def status_reply(repo, number, endpoint, pr, ticket, prior, started):
    rows = api(endpoint + "/reviews?per_page=100", pages=True)
    head = pr["head"]["sha"]
    current = [row for row in rows if row["commit_id"] == head and row["user"]["login"].lower() == LOGIN.lower()
               and row["state"] in ("APPROVED","CHANGES_REQUESTED","DISMISSED")]
    state = "已合并" if pr.get("merged_at") else ("已关闭" if pr["state"] == "closed" else "开放中")
    lines = [f'#{number} {state}，当前提交为 {head[:8]}。']
    if prior:
        lines.append("同一线程上一次正式审查的记录：" + prior)
    else:
        lines.append("此线程里没有找到更早的正式审查记录。")
    if current:
        receipt = current[-1]
        link = f"https://github.com/{repo}/pull/{number}#pullrequestreview-{int(receipt['id'])}"
        lines.append(f"GitHub 上 {LOGIN} 对当前提交的最近记录为 {receipt['state']}（{receipt['submitted_at']}）：{link}")
    else:
        lines.append(f"GitHub 上没有 {LOGIN} 对当前提交的正式审批记录。")
    return {"reply":signed_result(repo,number,pr,ticket,"\n\n".join(lines),started,"INSPECTED")}


def run_review(stage,args,repo,number,options):
    global GH_CALLS
    GH_CALLS = 0
    started = int(time.time())
    ticket = args["ticket"]
    endpoint = f"repos/{repo}/pulls/{number}"
    pr = snapshot(repo,number,read_only=stage == "prepare" or options["mode"] == "inspect")
    state = hashlib.sha256((ticket + options.get("job_scope","")).encode()).hexdigest()
    with sqlite3.connect(ROOT/"reviews.sqlite",timeout=15) as db:
        db.execute("create table if not exists jobs(id text primary key,base text,head text,started integer,attempting integer default 0)")
        event = args["event"]
        thread_key = json.dumps([event[k] for k in ("teamId","channelId","threadTs")] + [repo.lower(),number])
        db.execute("create table if not exists review_context(thread text, ts real, report text, primary key(thread,ts))")
        previous = db.execute("select report from review_context where thread=? and ts<? order by ts desc limit 1",
                              (thread_key,float(event["messageTs"]))).fetchone()
        if stage == "prepare" and options.get("status_only") and options["mode"] == "inspect":
            formal = db.execute("select report from review_context where thread=? and ts<? "
                                "and report not like '%只读复核，未投票%' order by ts desc limit 1",
                                (thread_key,float(event["messageTs"]))).fetchone()
            return status_reply(repo,number,endpoint,pr,ticket,formal[0] if formal else None,started)
        context = {"request_mode": options["mode"], "prior_report": previous[0] if previous else options.get("previous",""),
                   "thread_context":options.get("thread_context",[])}
        if stage == "prepare":
            started = int(time.time())
            try:
                if options["mode"] != "inspect":
                    require_open_review(pr)
                return {"prompt":prepare(repo,number,endpoint,pr,state,db,event["text"],context)}
            except (Stop, subprocess.TimeoutExpired) as error:
                reason = "读取 GitHub 超时。" if isinstance(error,subprocess.TimeoutExpired) else str(error)
                report = interrupted_report(db,state,pr,reason)
                if options["mode"] == "inspect":
                    report = "只读复核，未投票 · " + report
                db.execute("insert or replace into review_context values(?,?,?)",
                           (thread_key,float(event["messageTs"]),report[:2000]))
                return {"reply":signed_result(repo,number,pr,ticket,report,started,"INCOMPLETE")}
        if stage != "finish":
            raise Stop("未知审查阶段。")
        return complete_review(args,repo,number,endpoint,pr,state,ticket,db,context,thread_key)


def complete_review(args,repo,number,endpoint,pr,state,ticket,db,context,thread_key):
    history = []
    policy = json.loads(evidence.POLICY.read_text())
    db.execute("create table if not exists job_metrics(id text primary key, github_calls integer not null)")
    saved = db.execute("select github_calls from job_metrics where id=?",(state,)).fetchone()
    prior_calls = saved[0] if saved else 0
    row = db.execute("select started from jobs where id=?",(state,)).fetchone()
    started = row[0] if row else int(time.time())
    try:
        pin = db.execute("select base,head from jobs where id=?", (state,)).fetchone()
        if not pin:
            raise Stop("缺少本次请求的初次审查记录，无法确认模型读过哪个版本。")
        first = args["output"]
        inspect = context["request_mode"] == "inspect"
        focus = args["event"]["text"]
        refresh = list(pin) != revision(pr)
        for retry in range(3):
            active = state
            try:
                if not refresh:
                    initial = json.loads(first.strip().removeprefix("```json").removesuffix("```").strip())
                    if not isinstance(initial, dict):
                        raise Stop("审查输出格式无效，未提交审批。")
                    refresh = initial.get("decision") == "REFRESH"
                if refresh:
                    # A previous POST may have succeeded. Never start another vote across that boundary.
                    writing = db.execute("select base,head from jobs where (id=? or id glob ?) and attempting=1",
                                         (state, state + ":*")).fetchall()
                    if any(list(attempt) != revision(pr) for attempt in writing):
                        raise Stop("旧版本已有 GitHub 写入尝试，需要先核对该审批的回执。")
                    packet = stable_packet(repo,number,endpoint,pr,focus,context)
                    active = state + ":" + ":".join(revision(pr))
                    db.execute("insert or ignore into jobs(id,base,head,started) values(?,?,?,?)",
                               (active,*revision(pr),started))
                    db.commit()
                    first = evidence.isolated_turn(evidence.prompt(packet,evidence.operations(policy,repo),[]), policy)
                elif initial.get("decision") == "EVIDENCE":
                    packet = stable_packet(repo,number,endpoint,pr,focus,context)
                else:
                    packet = {"full_diff":"", **context}
                # Evidence from a superseded revision cannot support a new verdict.
                round_history = []
                try:
                    answer,_ = evidence.resolve(repo,packet,first,policy,evidence_history=round_history)
                finally:
                    history.extend(round_history)
                final_pr = snapshot(repo,number,read_only=inspect)
                require_revision(revision(pr), final_pr)
                reply = finish(repo,number,endpoint,final_pr,active,ticket,json.dumps(answer,ensure_ascii=False),db,
                               history,policy,prior_calls,mode=context["request_mode"])
                break
            except RevisionChanged as change:
                pr = change.latest
                refresh = True
                if retry == 2:
                    raise Stop(f"{change} 达到本轮重新审查次数上限，未对新版本投票。") from change
        label = "只读复核，未投票" if context["request_mode"] == "inspect" else answer["decision"]
        report = f'{pr["head"]["sha"][:8]} {label}\n{answer["summary"]}\n{answer["details"]}'
    except (Stop,evidence.Unavailable,subprocess.TimeoutExpired) as error:
        reason = "审查进程超时。" if isinstance(error,subprocess.TimeoutExpired) else str(error)
        report = interrupted_report(db,state,pr,reason)
        if context["request_mode"] == "inspect":
            report = "只读复核，未投票 · " + report
        reply = signed_result(repo,number,pr,ticket,"本轮未确认 GitHub 审批\n\n"+reason,started,
                              "INCOMPLETE",history=history,policy=policy,prior_calls=prior_calls)
    db.execute("insert or replace into review_context values(?,?,?)",
               (thread_key,float(args["event"]["messageTs"]),report[:2000]))
    db.commit()
    return {"reply":reply}



if __name__ == "__main__":
    try:
        result = main()
    except evidence.Unavailable as error:
        result = {"reply":"证据不足，本轮暂缓审批。"+str(error)}
    except Stop as error:
        result = {"reply":str(error)}
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired, sqlite3.Error):
        result = {"reply":"本轮审查未取得可确认的结果，请核对 GitHub 审批记录后再试。"}
    print(json.dumps(result,ensure_ascii=False))
