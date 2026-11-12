"""Request-bound tools for the native Slack agent. Credentials stay in the host."""
import asyncio
from contextlib import closing
import base64
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import time
import tomllib
import httpx2
from pathlib import Path

import review_evidence
import slack_intent
from review_tasks import ReviewTasks
from review_materials import ReviewMaterials
from repository_knowledge import RepositoryKnowledge
from review_standards import ReviewStandards

READ_NAME = re.compile(r"^(?:[a-z]+_)*(?:get|list|search|describe|read|fetch|query|explain)(?:_|$)")
WRITE_NAME = re.compile(r"(?:^|_)(?:create|update|delete|remove|insert|put|post|set|start|stop|trigger|run|restart|cancel|submit|upload|send|execute|grant|revoke|rotate|write|mark|transition|assign)(?:_|$)")
SQL_TOOLS = {"execute_query", "query", "run_query", "execute_sql", "redshift_query", "clickhouse_query"}
SQL_FIELDS = ("sql", "query", "sql_query", "statement")


def readonly_sql(value, dialect="redshift"):
    """Allow bounded SELECTs, joins and CTEs without write expressions."""
    import sqlglot
    from sqlglot import exp
    trees=sqlglot.parse(value,read=dialect)
    if len(trees)!=1 or not isinstance(trees[0],exp.Query):
        raise ValueError("Only one SELECT or WITH query is permitted.")
    tree=trees[0]
    if any(isinstance(node,(exp.DDL,exp.DML,exp.Command,exp.Into,exp.Lock)) for node in tree.walk()):
        raise ValueError("SQL writes and locking clauses are not permitted.")
    if any(isinstance(node,exp.Anonymous) for node in tree.walk()):
        raise ValueError("Use built-in SQL functions. User-defined functions are unavailable.")
    bound=tree.args.get("limit")
    if bound:
        try:
            count=int(bound.expression.name)
        except (ValueError,AttributeError):
            raise ValueError("LIMIT must be an integer.")
        if count<0: raise ValueError("LIMIT must be nonnegative.")
        return tree.limit(min(count,200)).sql(dialect=dialect)
    return tree.limit(200).sql(dialect=dialect)


def allowed_tool(tool):
    if tool["name"] in SQL_TOOLS or tool["name"]=="run_soql_query": return True
    if WRITE_NAME.search(tool["name"]): return False
    return bool(READ_NAME.search(tool["name"]) or (tool.get("annotations") or {}).get("readOnlyHint") is True)


class AgentTools:
    def __init__(self,args,key,root):
        self.args=args
        self.key=key
        self.root=root
        self.claim=slack_intent.verified(args,key)
        self.connections=tomllib.loads((Path.home()/".codex/config.toml").read_text()).get("mcp_servers",{})
        self.catalog={}
        self.results={}
        self.counts={}
        self.receipts=[]
        self.packets={}
        self.authorized={}
        spec=importlib.util.spec_from_file_location("agent_review",Path(__file__).with_name("github-review.py"))
        self.review=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.review)
        config=json.loads((self.review.ROOT/"config.json").read_text())
        self.review.KEY=key
        self.review.LOGIN=config["login"]
        self.review.GH=config["gh"]
        self.materials=ReviewMaterials(self.review.api)

    def record(self,label,ok=True):
        value=self.counts.setdefault(label,{"name":label,"count":0,"failed":0})
        value["count"]+=1
        value["failed"]+=int(not ok)

    def services(self):
        return [name for name,c in self.connections.items() if c.get("enabled",True)
                and not any(word in name.lower() for word in ("chrome","browser","node_repl","computer"))]

    async def upstream(self,server,method,tool=None,arguments=None):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        if server not in self.services():
            raise ValueError("This service is not available for this request.")
        if not (self.claim.get("business") or self.claim.get("review") or self.claim.get("local")):
            raise ValueError("This request has no read-tool capability.")
        cfg=self.connections[server]
        async def run(session):
            await session.initialize()
            if method=="list":
                result=(await session.list_tools()).model_dump(mode="json",by_alias=True)
                found=list(result["tools"])
                while result.get("nextCursor"):
                    from mcp import types
                    result=(await session.list_tools(params=types.PaginatedRequestParams(cursor=result["nextCursor"]))).model_dump(mode="json",by_alias=True)
                    found.extend(result["tools"])
                allowed={item["name"]:item for item in found if allowed_tool(item)}
                self.catalog[server]=allowed
                return [{"name":item["name"],"description":item.get("description"),"parameters":item["inputSchema"]} for item in allowed.values()]
            result=await session.call_tool(tool,arguments)
            value=result.model_dump(mode="json",by_alias=True,exclude_none=True)
            if value.get("isError"):
                return {"ok":False,"error":str(value.get("content",[]))[:6000]}
            raw=json.dumps(value,ensure_ascii=False)
            if len(raw.encode())>80000:
                return self.paged_result(raw)
            return {"ok":True,"result":value}
        if "command" in cfg:
            env={k:os.environ[k] for k in cfg.get("env_vars",[]) if k in os.environ}
            env.update(cfg.get("env",{}))
            env["HOME"]=str(Path.home())
            # Background daemons may omit the directory needed by env shebangs.
            if not env.get("PATH"):
                command=Path(cfg["command"])
                paths=([str(command.parent)] if command.is_absolute() else [])
                paths.extend(os.environ.get("PATH","/usr/bin:/bin").split(os.pathsep))
                env["PATH"]=os.pathsep.join(dict.fromkeys(p for p in paths if p))
            if "redshift" in server: env["REDSHIFT_ALLOW_WRITE"]="false"
            params=StdioServerParameters(command=cfg["command"],args=cfg.get("args",[]),cwd=cfg.get("cwd"),env=env)
            with open(os.devnull,"w") as quiet:
                async with stdio_client(params,errlog=quiet) as (reader,writer), ClientSession(reader,writer) as session:
                    return await run(session)
        headers=dict(cfg.get("http_headers",{}))
        headers.update({k:os.environ[v] for k,v in cfg.get("env_http_headers",{}).items() if v in os.environ})
        token=cfg.get("bearer_token_env_var")
        if token and token in os.environ: headers["Authorization"]="Bearer "+os.environ[token]
        async with httpx2.AsyncClient(headers=headers, timeout=120) as client, streamable_http_client(cfg["url"],http_client=client) as (reader,writer,_), ClientSession(reader,writer) as session:
            return await run(session)

    async def discover(self,server):
        return await asyncio.wait_for(self.upstream(server,"list"),90)

    def paged_result(self,raw):
        if len(raw.encode())>8*1024*1024:
            return {"ok":False,"error":"Result exceeds the 8 MiB read buffer. Narrow the upstream query or use its pagination.","truncated":True}
        if not hasattr(self,"results"):self.results={}
        result_id=hashlib.sha256(raw.encode()).hexdigest()
        self.results[result_id]=raw
        return self.read_result(result_id)

    def read_result(self,result_id,offset=0):
        if result_id not in getattr(self,"results",{}):
            raise ValueError("Use a result_id returned by a read in this execution.")
        text=self.results[result_id]
        if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=len(text):
            raise ValueError("Invalid result offset.")
        end=min(offset+16000,len(text))
        return {"ok":True,"result_id":result_id,"text":text[offset:end],"offset":offset,
                "next_offset":end if end<len(text) else None,"total_chars":len(text),
                "instruction":"Continue with read_result until next_offset is null; the upstream result is not truncated."}

    async def read(self,server,tool,arguments):
        if server not in self.catalog: await self.discover(server)
        descriptor=self.catalog[server].get(tool)
        if not descriptor: raise ValueError("This tool is not an approved read operation.")
        args=dict(arguments)
        props=descriptor["inputSchema"].get("properties",{})
        sql_operation=tool in SQL_TOOLS or (any(x in server.lower() for x in ("redshift","clickhouse","postgres","mysql")) and any(k in props for k in SQL_FIELDS))
        if tool=="run_soql_query":
            field=next((k for k in ("query","soql","soql_query") if k in args),None)
            if not field or not isinstance(args[field],str) or not re.match(r"^\s*SELECT\b",args[field],re.I) or ";" in args[field]:
                raise ValueError("A Salesforce SELECT query is required.")
        if sql_operation:
            field=next((k for k in SQL_FIELDS if k in args and isinstance(args[k],str)),None)
            if field is None: raise ValueError("A SELECT query is required.")
            args[field]=readonly_sql(args[field],"clickhouse" if "clickhouse" in server else "redshift")
            props=descriptor["inputSchema"].get("properties",{})
            for key in ("max_rows","limit"):
                if key in props: args[key]=min(int(args.get(key,200)),200)
        if "redshift" in server:
            cluster=args.get("cluster_identifier")
            database=args.get("database_name") or args.get("column_database_name") or args.get("database")
            if sql_operation and (not cluster or not database):
                raise ValueError("Choose the Redshift cluster and database explicitly before querying.")
        label={"mcp-atlassian":"Jira","redshift":"Redshift","redshift-mcp-server":"Redshift","salesforce":"Salesforce"}.get(server,server.replace("_"," ")[:39])
        try:
            result=await asyncio.wait_for(self.upstream(server,"call",tool,args),120)
            self.record(label,result.get("ok",False))
            return result
        except Exception:
            self.record(label,False)
            raise

    def github(self,path):
        path=path.removeprefix("/")
        if not re.fullmatch(r"(?:search/(?:repositories|code)\?[^\s]+|repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/(?:pulls|issues|contents|git|commits|compare|check-runs|actions/runs)(?:[/?][^\s]*)?)?)",path):
            raise ValueError("Use a repository read endpoint.")
        self.record("GitHub")
        result=self.review.api(path)
        if "/contents/" in path and isinstance(result,dict) and result.get("encoding")=="base64":
            return {"path":result["path"],"sha":result["sha"],"content":base64.b64decode(result["content"]).decode("utf8",errors="replace")}
        return result

    def knowledge(self,repo,ref='',paths=None,changed_paths=None,raw=False):
        if not hasattr(self,'repository_reader'):
            def read(path):
                try:result=self.review.api(path)
                except Exception:
                    self.record('GitHub',False)
                    raise
                self.record('GitHub')
                return result
            self.repository_reader=RepositoryKnowledge(read)
        result=self.repository_reader.read(repo,ref,paths,changed_paths)
        if raw:return result
        text=json.dumps(result,ensure_ascii=False)
        return self.paged_result(text) if len(text.encode())>24000 else result

    def context(self,target,include_closed=False):
        if not slack_intent.TARGET.fullmatch(target):
            raise ValueError("Expected owner/repository/pull/number.")
        repo,num=target.rsplit("/pull/",1)
        pr=self.review.snapshot(repo,int(num),read_only=True)
        terminal=None
        if pr.get("merged_at") or pr.get("state")=="closed":
            receipt={"state":"MERGED" if pr.get("merged_at") else "CLOSED","sha":pr["head"]["sha"],
                     "url":"https://github.com/"+target}
            row=self.tasks.rows().get(target) if hasattr(self,"tasks") else None
            if row and row["status"]=="pending": self.tasks.complete(target,row["id"],receipt)
            self.record("GitHub")
            terminal={"terminal":receipt,"instruction":"This PR is closed. Do not submit a new vote. For a retrospective, call review_context with include_closed=true."}
            if not include_closed:return terminal
        packet=self.review.stable_packet(repo,int(num),f"repos/{repo}/pulls/{num}",pr,self.claim["binding"][-1],
                                        {"request_mode":"review" if target in getattr(self,"review_plan",{}) else "inspect",
                                         "thread_context":self.claim["history"],"native_materials":True})
        self.packets[target]=(pr,packet)
        if terminal:packet.update(terminal)
        self.record("GitHub")
        if "file_manifest" in packet:
            if not hasattr(self, "standards"):
                self.standards = ReviewStandards(self.review.api, self.review.ROOT)
            packet["standards"] = self.standards.prepare(target, pr, packet)
            try:
                packet['repository_knowledge']=self.knowledge(repo,pr['head']['sha'],
                    changed_paths=[f['filename'] for f in packet['file_manifest']],raw=True)
            except Exception as error:
                packet['repository_knowledge']={'repo':repo,'commit':pr['head']['sha'],
                    'error':str(error)[:600],'instruction':'Retry repository_knowledge or use github_read for project rules and relevant docs. This read failure does not revoke review permission.'}
            if not hasattr(self,"materials"): self.materials=ReviewMaterials(self.review.api)
            return self.materials.register(target,pr,packet)
        return packet

    def material(self,target,offset=0):
        return self.materials.page(target,offset)

    def artifact(self,target,path,side="head",members=None,offset=0):
        self.record("GitHub")
        return self.materials.inspect(target,path,side,members,offset)

    def aws_tools(self):
        import aws_evidence
        if not any(self.claim.get(k) for k in ("local","review","business")):
            raise ValueError("This request has no read-tool capability.")
        return aws_evidence.discover()

    def aws_read(self,service,operation,arguments,profile=None,region=None):
        import aws_evidence
        if not any(self.claim.get(k) for k in ("local","review","business")):
            raise ValueError("This request has no read-tool capability.")
        try:
            result=aws_evidence.read(service,operation,arguments,profile=profile,region=region)
            self.record("AWS",result.get("ok",True))
            return result
        except Exception:
            self.record("AWS",False)
            raise

    def prepare_reviews(self):
        self.tasks=ReviewTasks(self.root/"reviews.sqlite",self.claim,self.args["ticket"],self.key)
        self.review_plan={}
        self.tasks.revoke_cancelled()
        errors=self.reconcile_reviews()
        def classify(candidates,tasks):
            prompt=("根据当前消息判断每个 PR 的行动。只返回 JSON {\"actions\":[{\"target\":\"owner/repo/pull/1\",\"action\":\"new|continue|inspect|cancel|none\"}]}。"
                    "new 表示当前明确委托正式审查，简短的 CR、review、帮忙看下 PR 都可以。"
                    "宿主 tasks 中 pending 是此前已获得且尚未完成的正式审查授权。对该任务追问为什么暂缓、怎么没做、怎么样了、进展如何或要求继续时选择 continue，继续查证并完成审批，不需要重新说 review。"
                    "如果只是引用历史指令、无关问题或没有宿主持久化授权，不得凭聊天记录创建授权。"
                    "明确只查状态、不要继续、不投票、本次只读选择 inspect，不新增投票；取消审查或撤回委托选择 cancel。"
                    "completed/cancelled 不允许 continue；只有当前新的明确审查委托才可 new。多目标只选择本次实际指向的目标，无法确定时不猜。\n"+
                    "当前明确批量委派时（如这两个都处理），逐项给出对应行动；多个候选本身不是歧义，也不能因此返回空计划。\n"+
                    json.dumps({"current_request":self.claim["binding"][-1],"history":self.claim["history"],
                                "candidates":candidates,"tasks":tasks},ensure_ascii=False))
            raw=review_evidence.isolated_turn(prompt,{}).strip()
            return json.loads(raw.removeprefix("```json").removesuffix("```").strip())
        self.review_plan=self.tasks.prepare(classify)
        return {"continue_to_verdict":list(self.review_plan),"tasks":self.tasks.summary(),
                "reconciliation_errors":errors,"planning_errors":self.tasks.planning_errors}

    def reconcile_reviews(self):
        errors={}
        if not self.claim.get("review"): return errors
        path=self.review.ROOT/"reviews.sqlite"
        for target,row in self.tasks.rows().items():
            if row["status"]!="pending" or row["updated"]>float(self.claim["binding"][3]): continue
            try:
                self.reconcile_review(target,row,path)
            except Exception as error:
                self.record("GitHub",False)
                errors[target]=type(error).__name__+": "+str(error)[:1000]
        return errors

    def reconcile_review(self,target,row,path):
        self.tasks.origin_claim(row["origin"],target)
        job=None
        if path.exists():
            with closing(sqlite3.connect(path)) as db:
                if db.execute("select 1 from sqlite_master where name='jobs'").fetchone():
                    job=db.execute("select head,attempting from jobs where id=?",(row["id"],)).fetchone()
        if job and job[1]:
            reviews=self.review.api("repos/"+target.replace("/pull/","/pulls/")+"/reviews?per_page=100",pages=True)
            marker="<!-- perry-relay-review "+row["id"]+" -->"
            for receipt in reviews:
                if (marker in (receipt.get("body") or "") and receipt.get("commit_id")==job[0] and
                        receipt.get("user",{}).get("login","").lower()==self.review.LOGIN.lower() and
                        receipt.get("state") in ("APPROVED","CHANGES_REQUESTED","DISMISSED")):
                    self.tasks.complete(target,row["id"],{"review":receipt["id"],"sha":job[0],
                                        "state":receipt["state"],"url":receipt["html_url"]})
                    self.record("GitHub")
                    break
            if self.tasks.rows()[target]["status"]=="completed": return
        repo,num=target.rsplit("/pull/",1)
        current=self.review.snapshot(repo,int(num),read_only=True)
        self.record("GitHub")
        if current.get("merged_at") or current.get("state")=="closed":
            self.tasks.complete(target,row["id"],{"state":"MERGED" if current.get("merged_at") else "CLOSED",
                "sha":current["head"]["sha"],"url":"https://github.com/"+target})

    def may_vote(self,target):
        if not hasattr(self,"tasks"):
            raise ValueError("Review authorization has not been prepared.")
        return self.tasks.require(target,self.review_plan)

    def standards_check(self,target,assessments=None):
        if target not in self.packets or not hasattr(self, "standards"):
            return {"ok":False,"error":"Read review_context before checking approval standards."}
        pr,packet=self.packets[target]
        missing=self.materials.missing(target)
        result=self.standards.evaluate(target,[pr["base"]["sha"],pr["head"]["sha"]],assessments)
        if missing:
            result.update(ok=False,unread_material=missing)
        return result

    def submit(self,target,head,decision,summary,details,standards=None):
        state=self.may_vote(target)
        if target not in self.packets: raise ValueError("Read the complete review context first.")
        pr,packet=self.packets[target]
        if head!=pr["head"]["sha"]: raise ValueError("The verdict must identify the reviewed commit.")
        if decision=="APPROVE":
            checked=self.standards_check(target,standards)
            if not checked["ok"]:
                return {"ok":False,"error":"Approval standards are not satisfied. Complete the mandatory checks; do not override failed checks with a narrative verdict.","standards":checked}
        if "file_manifest" in packet:
            missing=self.materials.missing(target)
            required=[m for m in missing if decision=="APPROVE" or m.get("material")]
            if required:
                return {"ok":False,"error":"Review evidence is incomplete. Continue reading the listed material; this is not a repository permission failure.",
                        "missing_count":len(required),"missing":required[:30]}
        repo,num=target.rsplit("/pull/",1)
        endpoint=f"repos/{repo}/pulls/{num}"
        latest=self.review.snapshot(repo,int(num))
        self.review.require_revision(self.review.revision(pr),latest)
        with closing(sqlite3.connect(self.review.ROOT/"reviews.sqlite",timeout=15)) as db:
            db.execute("create table if not exists jobs(id text primary key,base text,head text,started integer,attempting integer default 0)")
            prior=db.execute("select base,head,attempting from jobs where id=?",(state,)).fetchone()
            if prior and list(prior[:2])!=self.review.revision(pr) and prior[2]:
                raise ValueError("A previous revision has a write attempt. Check its receipt before voting again.")
            db.execute("insert into jobs(id,base,head,started) values(?,?,?,?) on conflict(id) do update set base=excluded.base,head=excluded.head where jobs.attempting=0",
                       (state,*self.review.revision(pr),int(time.time())))
            db.commit()
            self.review.GH_CALLS=0
            signed=self.review.finish(repo,int(num),endpoint,pr,state,self.args["ticket"],
                         json.dumps({"decision":decision,"summary":summary,"details":details},ensure_ascii=False),db,
                         verified_standards=checked if decision=="APPROVE" else None)
        body=signed.split(" ",1)[1].split(".")[0]
        value=json.loads(base64.urlsafe_b64decode(body+"==="))
        if value["review"]:
            self.receipts.append(signed)
            self.tasks.complete(target,state,{"review":value["review"],"sha":value["sha"],"state":value["status"],
                                "url":"https://github.com/"+target+"#pullrequestreview-"+str(value["review"])})
        return value

    def local(self,command,action="shell"):
        if action not in ("shell","update_main","restart_claude"):
            raise ValueError("Unknown owner operation.")
        if not self.claim.get("local") or self.claim["binding"][4]!=self.claim["binding"][5]:
            raise ValueError("Local execution is available only for the account owner.")
        import owner_local
        cfg=json.loads((owner_local.ROOT/"config.json").read_text())
        if self.claim["binding"][0]!=cfg["team"] or self.claim["binding"][4]!=cfg["owner"]:
            raise ValueError("Owner identity mismatch.")
        identity=hashlib.sha256((self.args["ticket"]+"\n"+action+"\n"+command).encode()).hexdigest()
        with closing(sqlite3.connect(self.root/"local.sqlite",timeout=15)) as db:
            db.execute("create table if not exists commands(id text primary key,result text)")
            prior=db.execute("select result from commands where id=?",(identity,)).fetchone()
            if prior:
                if prior[0] is None:
                    raise RuntimeError("This command has an unresolved earlier attempt. Inspect its effects before retrying.")
                return json.loads(prior[0])
            db.execute("insert into commands values(?,NULL)",(identity,))
            db.commit()
            self.record("Local")
            if action=="shell": result=owner_local.shell(command,Path(cfg["repo"]))
            elif action=="update_main": result=owner_local.update_main(Path(cfg["repo"]),cfg)
            else: result=owner_local.restart_claude(self.claim["binding"][-1])
            db.execute("update commands set result=? where id=?",(json.dumps(result),identity))
            db.commit()
            return result
