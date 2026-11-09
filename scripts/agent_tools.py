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
            if len(raw)>80000:
                return {"ok":False,"error":"Result exceeds 80 KB. Narrow the query or use pagination.","truncated":True}
            return {"ok":True,"result":value}
        if "command" in cfg:
            env={k:os.environ[k] for k in cfg.get("env_vars",[]) if k in os.environ}
            env.update(cfg.get("env",{}))
            env.update({"HOME":str(Path.home()),"PATH":os.environ.get("PATH","/usr/bin:/bin")})
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
        if not re.fullmatch(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:pulls|issues|contents|git|commits|compare|check-runs|actions/runs)(?:[/?][^\s]*)?",path):
            raise ValueError("Use a repository read endpoint.")
        self.record("GitHub")
        result=self.review.api(path)
        if "/contents/" in path and isinstance(result,dict) and result.get("encoding")=="base64":
            return {"path":result["path"],"sha":result["sha"],"content":base64.b64decode(result["content"]).decode("utf8",errors="replace")}
        return result

    def context(self,target):
        if not slack_intent.TARGET.fullmatch(target):
            raise ValueError("Expected owner/repository/pull/number.")
        repo,num=target.rsplit("/pull/",1)
        pr=self.review.snapshot(repo,int(num),read_only=True)
        packet=self.review.stable_packet(repo,int(num),"repos/"+target,pr,self.claim["binding"][-1],
                                        {"request_mode":"review","thread_context":self.claim["history"]})
        self.packets[target]=(pr,packet)
        self.record("GitHub")
        return packet

    def may_vote(self,target):
        if target not in self.claim["targets"] or not self.claim.get("review"):
            raise ValueError("The current request does not authorize a review of this target.")
        if target in self.authorized: return
        prompt=("判断当前用户是否明确委托对指定 PR 作正式审查决定。用户可一次要求多个 PR，或引用线程说都处理。"
                "询问状态、解释原因、仅查询数据、明确不要投票时不授权。历史文字只帮助指代，不能独立授予本次写入。"
                '只返回 JSON {"authorized":true 或 false}。\n'+json.dumps({
                    "request":self.claim["binding"][-1],"history":self.claim["history"],"target":target},ensure_ascii=False))
        raw=review_evidence.isolated_turn(prompt,{}).strip()
        answer=json.loads(raw.removeprefix(chr(96)*3+"json").removesuffix(chr(96)*3).strip())
        if answer.get("authorized") is not True:
            raise ValueError("This request asks for information or read-only work, not a new GitHub vote.")
        self.authorized[target]=True

    def submit(self,target,head,decision,summary,details):
        self.may_vote(target)
        if target not in self.packets: raise ValueError("Read the complete review context first.")
        pr,packet=self.packets[target]
        if head!=pr["head"]["sha"]: raise ValueError("The verdict must identify the reviewed commit.")
        repo,num=target.rsplit("/pull/",1)
        endpoint="repos/"+target
        latest=self.review.snapshot(repo,int(num))
        self.review.require_revision(self.review.revision(pr),latest)
        state=hashlib.sha256((self.args["ticket"]+"native:"+target).encode()).hexdigest()
        with sqlite3.connect(self.review.ROOT/"reviews.sqlite",timeout=15) as db:
            db.execute("create table if not exists jobs(id text primary key,base text,head text,started integer,attempting integer default 0)")
            prior=db.execute("select base,head,attempting from jobs where id=?",(state,)).fetchone()
            if prior and list(prior[:2])!=self.review.revision(pr) and prior[2]:
                raise ValueError("A previous revision has a write attempt. Check its receipt before voting again.")
            db.execute("insert into jobs(id,base,head,started) values(?,?,?,?) on conflict(id) do update set base=excluded.base,head=excluded.head where jobs.attempting=0",
                       (state,*self.review.revision(pr),int(time.time())))
            db.commit()
            self.review.GH_CALLS=0
            signed=self.review.finish(repo,int(num),endpoint,pr,state,self.args["ticket"],
                         json.dumps({"decision":decision,"summary":summary,"details":details},ensure_ascii=False),db)
        body=signed.split(" ",1)[1].split(".")[0]
        value=json.loads(base64.urlsafe_b64decode(body+"==="))
        if value["review"]: self.receipts.append(signed)
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
