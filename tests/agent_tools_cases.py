import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from agent_tools import readonly_sql, allowed_tool, AgentTools

class NativeToolsTests(unittest.TestCase):
    def test_review_planner_receives_interruption_context_and_durable_grants(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock
        agent=AgentTools.__new__(AgentTools)
        agent.root=Path('/unused');agent.args={'ticket':'signed'};agent.key=b'key'
        agent.claim={'binding':['are you ok?'],'history':[{'text':'review both PRs'}]}
        agent.previous_execution={'status':'interrupted','error_code':'model_capacity'}
        agent.reconcile_reviews=Mock(return_value={})
        targets=['team/a/pull/1','team/b/pull/2']
        tasks=[{'target':target,'status':'pending'} for target in targets]
        def prepare(classify):
            proposal=classify(targets,tasks)
            return {item['target']:'grant' for item in proposal['actions'] if item['action']=='continue'}
        ledger=SimpleNamespace(revoke_cancelled=Mock(),prepare=prepare,summary=lambda:tasks,planning_errors=[])
        def classify(prompt,policy,*,reasoning_effort):
            self.assertEqual(reasoning_effort,'low')
            payload=json.loads(prompt[prompt.index('{"current_request"'):])
            self.assertEqual(payload['previous_execution'],agent.previous_execution)
            self.assertEqual(payload['tasks'],tasks)
            return json.dumps({'actions':[{'target':t,'action':'continue'} for t in targets]})
        with patch('agent_tools.ReviewTasks',return_value=ledger),patch('review_evidence.isolated_turn',side_effect=classify):
            self.assertEqual(set(agent.prepare_reviews()['continue_to_verdict']),set(targets))

    def test_pr_material_includes_pinned_repository_knowledge_and_reports_read_failure(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        agent=AgentTools.__new__(AgentTools);agent.counts={};agent.packets={}
        agent.claim={'binding':['review'],'history':[]}
        pr={'state':'open','head':{'sha':'a'*40},'base':{'sha':'b'*40}}
        agent.review=SimpleNamespace(snapshot=Mock(return_value=pr),stable_packet=Mock(side_effect=lambda *args:{'file_manifest':[{'filename':'models/model.sql','status':'modified'}]}))
        agent.materials=SimpleNamespace(register=Mock(side_effect=lambda target,pr,packet:packet))
        agent.standards=SimpleNamespace(prepare=Mock(return_value={"status":"pass"}))
        agent.knowledge=Mock(return_value={'documents':[{'path':'CLAUDE.md','text':'Project rule'}]})
        result=agent.context('team/repo/pull/1')
        agent.knowledge.assert_called_with('team/repo','a'*40,changed_paths=['models/model.sql'],raw=True,exclude_blobs={})
        self.assertEqual(result['repository_knowledge']['documents'][0]['text'],'Project rule')
        agent.knowledge.side_effect=RuntimeError('HTTP 403')
        result=agent.context('team/repo/pull/1')
        self.assertIn('HTTP 403',result['repository_knowledge']['error'])
        self.assertEqual(result['repository_knowledge']['commit'],'a'*40)

    def test_large_chinese_read_result_has_complete_followup_pages(self):
        import json
        agent=AgentTools.__new__(AgentTools)
        original=json.dumps({'text':'完整结果'*20000},ensure_ascii=False)
        page=agent.paged_result(original);parts=[page['text']]
        while page['next_offset'] is not None:
            page=agent.read_result(page['result_id'],page['next_offset']);parts.append(page['text'])
        self.assertEqual(''.join(parts),original)
        with self.assertRaises(ValueError):agent.read_result('another-request')

    def test_unread_archive_blocks_approve_but_completed_evidence_reaches_submission(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from review_materials import ReviewMaterials,blob_digest
        import base64,json
        target='team/repo/pull/1';raw=b'{"flow":"checked"}'
        pr={'state':'open','head':{'sha':'head'},'base':{'sha':'base'}}
        packet={'file_manifest':[{'filename':'flow.tfl','status':'added','artifact_required':True}]}
        def api(path):
            if '/contents/' in path:return {'type':'file','sha':blob_digest(raw),'size':len(raw)}
            return {'encoding':'base64','content':base64.b64encode(raw).decode(),'size':len(raw)}
        a=AgentTools.__new__(AgentTools);a.claim={'review':True};a.args={'ticket':'signed'}
        a.review_plan={target:'task'};a.tasks=SimpleNamespace(require=Mock(return_value='task'),complete=Mock())
        a.packets={target:(pr,packet)};a.receipts=[];a.materials=ReviewMaterials(api);a.materials.register(target,pr,packet)
        a.standards=SimpleNamespace(evaluate=Mock(side_effect=lambda *args:{"ok":True,"base_head":["base","head"]}))
        receipt=base64.urlsafe_b64encode(json.dumps({'review':42,'sha':'head','status':'APPROVE'}).encode()).decode()
        with tempfile.TemporaryDirectory() as folder:
            a.review=SimpleNamespace(ROOT=Path(folder),snapshot=Mock(return_value=pr),require_revision=Mock(),
                                     revision=lambda p:['base','head'],finish=Mock(return_value='relay-review/2 '+receipt+'.signature'),
                                     validate_verdict_fields=Mock(return_value=None))
            blocked=a.submit(target,'head','APPROVE','checked','details')
            self.assertFalse(blocked['ok']);a.review.finish.assert_not_called()
            a.materials.inspect(target,'flow.tfl')
            self.assertEqual(a.submit(target,'head','APPROVE','checked','details')['review'],42)
            a.review.finish.assert_called_once();a.tasks.complete.assert_called_once()

    def test_unchecked_or_failed_standards_never_reach_publication(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        target='example/repo/pull/1'
        a=AgentTools.__new__(AgentTools)
        a.may_vote=Mock(return_value='task')
        a.packets={target:({'head':{'sha':'head'},'base':{'sha':'base'}},{'file_manifest':[]})}
        a.materials=SimpleNamespace(missing=Mock(return_value=[]))
        a.review=SimpleNamespace(finish=Mock(),snapshot=Mock(),validate_verdict_fields=Mock(return_value=None))
        self.assertFalse(a.submit(target,'head','APPROVE','verified','all good')['ok'])
        a.standards=SimpleNamespace(evaluate=Mock(return_value={'ok':False,'findings':[{'rule':'model-suffix'}]}))
        self.assertFalse(a.submit(target,'head','APPROVE','verified','all good',[{'rule_id':'naming','status':'pass'}])['ok'])
        a.review.finish.assert_not_called()
        a.review.snapshot.assert_not_called()

    def test_review_field_errors_are_actionable_before_github(self):
        import json
        from unittest.mock import Mock

        target='team/repo/pull/1'
        pr={'head':{'sha':'head'},'base':{'sha':'base'}}
        agent=AgentTools.__new__(AgentTools)
        agent.may_vote=Mock(return_value='task')
        agent.standards_check=Mock()
        agent.packets={target:(pr,{})}
        agent.args={'ticket':'test-ticket'}
        spec=importlib.util.spec_from_file_location('review_for_submit',Path(__file__).resolve().parents[1]/'scripts/github-review.py')
        agent.review=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent.review)
        receipt='relay-review/2 '+agent.review.encode(b'{"review":null}')+'.signature'
        cases=[
            ('审'*1001,'细节','summary','too_long',1001,1000,
             'summary 超过 1000 字符上限（当前 1001 字符），请精简后重试。'),
            ('结论','审'*20001,'details','too_long',20001,20000,
             'details 超过 20000 字符上限（当前 20001 字符），请精简后重试。'),
            ('','细节','summary','empty',0,1000,
             'summary 不能为空（当前 0 字符，上限 1000 字符），请填写简短中文审查结论。'),
            ('结论','  ','details','empty',2,20000,
             'details 不能为空（当前 2 字符，上限 20000 字符），请填写具体中文审查依据。'),
        ]
        with patch.object(agent.review,'snapshot',return_value=pr) as snapshot, \
             patch.object(agent.review,'finish',return_value=receipt) as finish, \
             patch.object(agent.review,'api') as api:
            for summary,details,field,reason,actual,limit,error in cases:
                with self.subTest(field=field,reason=reason):
                    self.assertEqual(agent.submit(target,'head','REQUEST_CHANGES',summary,details),
                        {'ok':False,'field':field,'reason':reason,'actual_chars':actual,'max_chars':limit,'error':error})
            snapshot.assert_not_called()
            finish.assert_not_called()
            api.assert_not_called()
            with tempfile.TemporaryDirectory() as folder, patch.object(agent.review,'ROOT',Path(folder)):
                summary,details='结'*1000,'审'*20000
                agent.submit(target,'head','REQUEST_CHANGES',summary,details)
                self.assertEqual(json.loads(finish.call_args.args[6]),
                    {'decision':'REQUEST_CHANGES','summary':summary,'details':details})
                self.assertIsNone(finish.call_args.kwargs['verified_standards'])
            agent.standards_check.assert_not_called()

    def test_git_object_cache_is_shared_by_all_review_readers(self):
        import base64
        from collections import Counter
        import json
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock
        from review_materials import blob_digest

        target='team/repo/pull/1'
        pr={'state':'open','head':{'sha':'a'*40},'base':{'sha':'b'*40}}
        files={'AGENTS.md':b'Project rule', 'README.md':b'Other project knowledge', 'code.py':b'changed code'}
        blobs={blob_digest(raw):raw for raw in files.values()}
        rows=[{'path':path,'sha':blob_digest(raw),'size':len(raw),'type':'blob'} for path,raw in files.items()]
        tree_paths={f'repos/team/repo/git/trees/{sha}?recursive=1' for sha in ('a'*40,'b'*40)}
        calls=Counter()
        def api(path):
            calls[path]+=1
            if path in tree_paths:
                return {'tree':rows}
            if '/blobs/' in path and path.rsplit('/',1)[-1] in blobs:
                raw=blobs[path.rsplit('/',1)[-1]]
                return {'encoding':'base64','content':base64.b64encode(raw).decode(),'size':len(raw)}
            if '/contents/code.py?' in path:
                return {'type':'file','sha':blob_digest(files['code.py']),'size':len(files['code.py'])}
            return {}
        def packet(*args):
            return {'file_manifest':[{'filename':'code.py','status':'modified','artifact_required':True}]}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'standards.json').write_text(json.dumps(
                {'default':{'documents':['AGENTS.md'],'manual_rules':{}},'repositories':{}}))
            agent=AgentTools.__new__(AgentTools)
            agent.counts={};agent.packets={};agent._git_objects={}
            agent._git_objects_lock=threading.Lock()
            agent.claim={'binding':['review'],'history':[]}
            agent.review=SimpleNamespace(ROOT=root,api=api,snapshot=Mock(return_value=pr),stable_packet=packet)
            material=json.loads(agent.context(target)['text'])
            knowledge={row['path']:row for row in material['repository_knowledge']['documents']}
            self.assertEqual(knowledge['AGENTS.md']['included_in'],'standards.documents')
            self.assertNotIn('text',knowledge['AGENTS.md'])
            self.assertEqual(knowledge['README.md']['text'],'Other project knowledge')
            self.assertEqual(agent.artifact(target,'code.py')['text'],'changed code')
            self.assertEqual(agent.artifact(target,'code.py','base')['text'],'changed code')
            agent.context(target)
            for path in tree_paths:
                self.assertEqual(calls[path],1)
            for sha in blobs:
                self.assertEqual(calls[f'repos/team/repo/git/blobs/{sha}'],1)
            for suffix in ('pulls/1','pulls/1/reviews?per_page=100','commits/main',
                           'git/trees/main?recursive=1','git/trees/'+'a'*40+'?recursive=0',
                           'git/blobs/'+'a'*39,'git/blobs/'+'a'*40+'?x=1'):
                path=f'repos/team/repo/{suffix}'
                agent.git_object(path)
                agent.git_object(path)
                self.assertEqual(calls[path],2)

    def test_stdio_launcher_finds_sibling_executable_with_daemon_path(self):
        import asyncio,os,subprocess
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock,Mock
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            (folder/'helper').write_text('#!/bin/sh\nexit 0\n')
            (folder/'helper').chmod(0o700)
            agent=AgentTools.__new__(AgentTools)
            agent.connections={'fixture':{'command':str(folder/'launcher'),'env':{'PATH':str(folder)}}}
            agent.claim={'local':True};agent.catalog={}
            agent._stdio_workers={};agent._closed=False
            async def discover_and_close():
                try:
                    return await agent.discover('fixture')
                finally:
                    await agent.aclose()
            @asynccontextmanager
            async def transport(params,**kwargs):
                subprocess.run(['/usr/bin/env','helper'],env=params.env,check=True,capture_output=True)
                yield (None,None)
            session=AsyncMock()
            session.list_tools.return_value=Mock(model_dump=Mock(return_value={'tools':[]}))
            session.__aenter__.return_value=session
            with patch.dict(os.environ,{'PATH':'/usr/bin:/bin'}), \
                 patch('mcp.client.stdio.stdio_client',transport),patch('mcp.ClientSession',return_value=session):
                self.assertEqual(asyncio.run(discover_and_close()),[])
            # Absolute launchers must also supply their directory for env shebangs.
            agent.connections['fixture'].pop('env')
            agent._closed=False
            with patch.dict(os.environ,{'PATH':'/usr/bin:/bin'}), \
                 patch('mcp.client.stdio.stdio_client',transport),patch('mcp.ClientSession',return_value=session):
                self.assertEqual(asyncio.run(discover_and_close()),[])

    def test_discovery_error_survives_mcp_result_validation(self):
        import asyncio,json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from agent_runtime import build_server
        with tempfile.TemporaryDirectory() as temp:
            server=build_server(SimpleNamespace(claim={},discover=AsyncMock(side_effect=RuntimeError('connection closed'))),Path(temp)/'audit')
            result=asyncio.run(server.call_tool('discover_tools',{'service':'fixture'}))
            value=result.model_dump(mode='json',by_alias=True)
            self.assertIn('connection closed',json.dumps(value))
            self.assertNotIn('Error executing tool',json.dumps(value))

    def test_failed_old_pr_does_not_block_other_reviews_or_business_work(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        rows={t:{"id":t,"origin":"signed","status":"pending","updated":1}
              for t in ["team/broken/pull/1","team/working/pull/2"]}
        agent=AgentTools.__new__(AgentTools); agent.counts={}
        agent.claim={"review":True,"binding":["T","C","root","2"]}
        agent.tasks=SimpleNamespace(rows=lambda:rows,origin_claim=Mock(),complete=Mock())
        with tempfile.TemporaryDirectory() as temp:
            agent.review=SimpleNamespace(ROOT=Path(temp),snapshot=Mock(side_effect=[
                RuntimeError("HTTP 403"),{"state":"closed","merged_at":"now","head":{"sha":"abc"}}]))
            errors=agent.reconcile_reviews()
            self.assertIn("team/broken/pull/1",errors)
            self.assertEqual(agent.tasks.complete.call_args.args[0],"team/working/pull/2")

    def test_draft_or_own_pr_remains_readable_without_write_permission(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        agent=AgentTools.__new__(AgentTools);agent.counts={};agent.packets={}
        agent.claim={"binding":["explain this PR"],"history":[]}
        agent.review=SimpleNamespace(snapshot=Mock(return_value={"state":"open","draft":True}),
                                    stable_packet=Mock(return_value={"full_diff":"code"}))
        agent.context("team/repo/pull/1")
        self.assertEqual(agent.review.stable_packet.call_args.args[-1]["request_mode"],"inspect")

    def test_github_read_accepts_rest_paths_and_uses_plural_pr_endpoint(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        agent=AgentTools.__new__(AgentTools);agent.counts={}
        agent.claim={"binding":["review"],"history":[]};agent.packets={}
        agent.review=SimpleNamespace(api=Mock(return_value={"state":"open"}),
            snapshot=Mock(return_value={"state":"open"}),stable_packet=Mock(return_value={"diff":"complete"}))
        agent.github("/repos/team/repo/pulls/696")
        agent.review.api.assert_called_with("repos/team/repo/pulls/696")
        agent.github("repos/team/repo")
        agent.review.api.assert_called_with("repos/team/repo")
        for search in ['search/repositories?q=repo+in:name','search/code?q=filename:AGENTS.md+repo:team/repo']:
            agent.github(search)
            agent.review.api.assert_called_with(search)
        agent.context("team/repo/pull/696")
        self.assertEqual(agent.review.stable_packet.call_args.args[2],"repos/team/repo/pulls/696")
        for path in ["https://elsewhere.test/repos/t/r","//repos/t/r","user","search/users?q=someone"]:
            with self.assertRaises(ValueError): agent.github(path)

    def test_submit_uses_durable_task_and_rest_endpoint_then_closes_grant(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import base64,json
        target="team/repo/pull/696"
        pr={"head":{"sha":"head"},"base":{"sha":"base"}}
        agent=AgentTools.__new__(AgentTools)
        agent.claim={"review":True};agent.args={"ticket":"follow-up-ticket"};agent.review_plan={target:"stable-task"}
        agent.tasks=SimpleNamespace(require=Mock(return_value="stable-task"),complete=Mock())
        agent.packets={target:(pr,{})};agent.receipts=[]
        agent.materials=SimpleNamespace(missing=Mock(return_value=[]))
        agent.standards=SimpleNamespace(evaluate=Mock(return_value={"ok":True,"base_head":["base","head"]}))
        body=base64.urlsafe_b64encode(json.dumps({"review":42,"sha":"head","status":"APPROVE"}).encode()).decode()
        with tempfile.TemporaryDirectory() as temp:
            agent.review=SimpleNamespace(ROOT=Path(temp),snapshot=Mock(return_value=pr),revision=lambda p:[p["base"]["sha"],p["head"]["sha"]],
                require_revision=Mock(),finish=Mock(return_value="relay-review/2 "+body+".signature"),
                validate_verdict_fields=Mock(return_value=None))
            result=agent.submit(target,"head","APPROVE","checked","evidence")
            self.assertEqual(agent.review.finish.call_args.args[2],"repos/team/repo/pulls/696")
            self.assertEqual(agent.review.finish.call_args.args[4:6],("stable-task","follow-up-ticket"))
            self.assertEqual(result["review"],42)
            self.assertEqual(agent.tasks.complete.call_args.args[:2],(target,"stable-task"))

    def test_comment_keeps_authorization_and_later_approval_uses_original_identity(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import base64,json
        target="team/repo/pull/696"
        pr={"head":{"sha":"head"},"base":{"sha":"base"}}
        agent=AgentTools.__new__(AgentTools)
        agent.args={"ticket":"follow-up-ticket"};agent.review_plan={target:"stable-task"}
        agent.tasks=SimpleNamespace(require=Mock(return_value="stable-task"),complete=Mock())
        agent.packets={target:(pr,{"file_manifest":[]})};agent.receipts=[]
        agent.materials=SimpleNamespace(missing=Mock(return_value=[{"material":"unavailable evidence"}]))
        agent.standards=SimpleNamespace(evaluate=Mock(return_value={"ok":True,"base_head":["base","head"]}))
        def response(*args,**kwargs):
            decision=json.loads(args[6])["decision"]
            body=base64.urlsafe_b64encode(json.dumps({"review":42,"sha":"head","status":decision}).encode()).decode()
            return "relay-review/2 "+body+".signature"
        with tempfile.TemporaryDirectory() as temp:
            agent.review=SimpleNamespace(ROOT=Path(temp),snapshot=Mock(return_value=pr),revision=lambda p:[p["base"]["sha"],p["head"]["sha"]],require_revision=Mock(),finish=Mock(side_effect=response),
                                         validate_verdict_fields=Mock(return_value=None))
            agent.submit(target,"head","COMMENT","Evidence gap","Need current compiled output")
            self.assertEqual(agent.review.finish.call_args.args[4],"stable-task-comment-head")
            agent.tasks.complete.assert_not_called()
            agent.materials.missing.return_value=[]
            agent.submit(target,"head","APPROVE","Verified","Evidence now checked")
            self.assertEqual(agent.review.finish.call_args.args[4],"stable-task")
            self.assertEqual(agent.tasks.complete.call_args.args[:2],(target,"stable-task"))

    def test_reconcile_accepted_review_after_process_interruption(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import sqlite3
        target="team/repo/pull/745"
        row={"id":"stable-task","origin":"signed-origin","status":"pending","updated":1}
        agent=AgentTools.__new__(AgentTools)
        agent.claim={"review":True,"binding":["T","D","root","2"]}
        agent.counts={}
        def complete(*args): row["status"]="completed"
        agent.tasks=SimpleNamespace(rows=lambda:{target:row},origin_claim=Mock(),complete=Mock(side_effect=complete))
        with tempfile.TemporaryDirectory() as temp:
            db=sqlite3.connect(Path(temp)/"reviews.sqlite")
            db.execute("create table jobs(id text,head text,attempting integer)")
            db.execute("insert into jobs values('stable-task','abc',1)");db.commit();db.close()
            receipt={"body":"Evidence\n<!-- perry-relay-review stable-task -->","commit_id":"abc",
                     "user":{"login":"owner"},"state":"APPROVED","id":17,"html_url":"https://github.com/review/17"}
            agent.review=SimpleNamespace(ROOT=Path(temp),LOGIN="owner",api=Mock(return_value=[receipt]),snapshot=Mock())
            agent.reconcile_reviews()
            self.assertEqual(row["status"],"completed")
            self.assertEqual(agent.tasks.complete.call_args.args[2]["review"],17)
            agent.review.snapshot.assert_not_called()

    def test_merged_pr_closes_grant_without_an_approval(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        target="team/repo/pull/745"
        agent=AgentTools.__new__(AgentTools);agent.counts={}
        agent.tasks=SimpleNamespace(rows=lambda:{target:{"id":"task","status":"pending"}},complete=Mock())
        agent.review=SimpleNamespace(snapshot=Mock(return_value={"merged_at":"now","state":"closed","head":{"sha":"abc"}}),stable_packet=Mock())
        result=agent.context(target)
        self.assertEqual(result["terminal"]["state"],"MERGED")
        agent.review.stable_packet.assert_not_called()
        self.assertNotIn("review",agent.tasks.complete.call_args.args[2])

    def test_interrupted_first_turn_preserves_session_identity(self):
        from agent_runtime import persist_session
        with tempfile.TemporaryDirectory() as temp:
            events=Path(temp)/"events"
            session=Path(temp)/"session"
            events.write_text('{"type":"thread.started","thread_id":"resume-this-thread"}\n{"partial"')
            persist_session(events,session)
            self.assertEqual(session.read_text(),"resume-this-thread")

    def test_sql_alias_is_validated_before_upstream(self):
        import asyncio
        from unittest.mock import AsyncMock
        agent=AgentTools.__new__(AgentTools)
        agent.catalog={"redshift":{"redshift_query":{"inputSchema":{"properties":{"sql":{},"max_rows":{}}}}}}
        agent.counts={}
        agent.upstream=AsyncMock(return_value={"ok":True})
        args={"sql":"DELETE FROM test","cluster_identifier":"cluster","database":"dbt_dw"}
        with self.assertRaises(ValueError): asyncio.run(agent.read("redshift","redshift_query",args))
        agent.upstream.assert_not_called()
        args["sql"]="SELECT COUNT(*) FROM test"
        args["max_rows"]=999
        asyncio.run(agent.read("redshift","redshift_query",args))
        sent=agent.upstream.call_args.args[-1]
        self.assertEqual(sent["max_rows"],200)
        self.assertIn("LIMIT 200",sent["sql"])

    def test_gateway_policy_grants_only_request_bound_tools(self):
        import tomllib
        from agent_runtime import tool_policy
        peer=tomllib.loads(tool_policy(False))["mcp_servers"]["relay"]["tools"]
        self.assertEqual(set(peer),{"discover_tools","read_tool","read_result","github_read","repository_knowledge","review_context","review_material","review_artifact","review_standards","aws_tools","aws_read","submit_review","refresh_reviews"})
        self.assertTrue(all(x["approval_mode"]=="approve" for x in peer.values()))
        self.assertIn("owner_shell",tomllib.loads(tool_policy(True))["mcp_servers"]["relay"]["tools"])

    def test_preparation_failure_keeps_read_tools_available_and_can_be_retried(self):
        from agent_runtime import prepare_assignments
        from types import SimpleNamespace
        from unittest.mock import Mock
        agent=SimpleNamespace(prepare_reviews=Mock(side_effect=[ValueError("invalid classifier JSON"),
                             {"continue_to_verdict":["team/repo/pull/1"]}]))
        failure=prepare_assignments(agent)
        self.assertIn("preparation_error",failure)
        self.assertEqual(agent.review_plan,{})
        self.assertEqual(prepare_assignments(agent)["continue_to_verdict"],["team/repo/pull/1"])

    def test_failure_reason_distinguishes_signal_timeout_and_exit(self):
        import subprocess
        from agent_runtime import execution_failure
        self.assertIn("信号 15",execution_failure(subprocess.CalledProcessError(-15,"codex")))
        self.assertIn("时限",execution_failure(subprocess.TimeoutExpired("codex",1800)))
        self.assertIn("退出码 1",execution_failure(subprocess.CalledProcessError(1,"codex")))

    def test_owner_local_replays_result_without_repeating_execution(self):
        import json
        import owner_local
        agent=AgentTools.__new__(AgentTools)
        agent.claim={"local":True,"binding":["T","D","1","2","U","U","check"]}
        agent.args={"ticket":"signed-test"}
        agent.counts={}
        with tempfile.TemporaryDirectory() as temp:
            agent.root=Path(temp)
            (agent.root/"config.json").write_text(json.dumps({"team":"T","owner":"U","repo":temp}))
            with patch.object(owner_local,"ROOT",agent.root), patch.object(owner_local,"shell",return_value={"exit_code":0}) as call:
                self.assertEqual(agent.local("pwd"),agent.local("pwd"))
                self.assertEqual(call.call_count,1)
                call.side_effect=RuntimeError("interrupted")
                with self.assertRaises(RuntimeError): agent.local("write-file")
                with self.assertRaisesRegex(RuntimeError,"unresolved"): agent.local("write-file")
                self.assertEqual(call.call_count,2)

    def test_select_joins_ctes_and_sql_mutations(self):
        queries=[
            "SELECT count(*) FROM dw.a a LEFT JOIN dw.b b ON a.business_id=b.business_id",
            "WITH a AS (SELECT id FROM dw.a) SELECT count(*) FROM a",
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='orders'",
        ]
        for sql in queries:
            self.assertIn("LIMIT 200",readonly_sql(sql))
        for sql in ["DELETE FROM x","SELECT 1; DROP TABLE x","SELECT * INTO backup FROM x",
                    "WITH t AS (DELETE FROM x RETURNING *) SELECT * FROM t","SELECT public.write_data(1)"]:
            with self.assertRaises(Exception):
                readonly_sql(sql)

    def test_tool_discovery_never_exposes_mutations(self):
        for name in ("jira_get_issue","list_columns","execute_query","run_soql_query"):
            self.assertTrue(allowed_tool({"name":name,"annotations":None}))
        for name in ("jira_update_issue","execute_command","get_or_create_user","delete_table"):
            self.assertFalse(allowed_tool({"name":name,"annotations":{"readOnlyHint":True}}))

    def test_colleague_cannot_run_local_commands(self):
        agent=AgentTools.__new__(AgentTools)
        agent.claim={"local":False}
        with self.assertRaises(ValueError):
            agent.local("touch file")

    def test_unknown_target_and_status_question_cannot_vote(self):
        agent=AgentTools.__new__(AgentTools)
        agent.claim={"targets":["team/repo/pull/1"],"review":True,"binding":["T","C","1","2","S","O","what happened?"],"history":[]}
        agent.authorized={}
        with self.assertRaises(ValueError):
            agent.may_vote("team/repo/pull/2")
        with patch("review_evidence.isolated_turn",return_value='{"authorized":false}'):
            with self.assertRaises(ValueError):
                agent.may_vote("team/repo/pull/1")

class NativeSessionTests(unittest.TestCase):
    def test_missing_end_record_does_not_block_followup_or_claim_no_side_effects(self):
        from agent_runtime import prior_execution,save_execution
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'last.json'
            self.assertIsNone(prior_execution(path))
            path.write_text('{incomplete')
            self.assertEqual(prior_execution(path)['status'],'unknown')
            save_execution(path,{'status':'running','request':'id'})
            self.assertIn('不假定未执行',prior_execution(path)['reason'])
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_interrupted_runtime_replays_once_and_followup_resumes_with_diagnostic(self):
        import agent_runtime as runtime
        import base64,hashlib,hmac,json,time
        from types import SimpleNamespace
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);private=root/'private';(private/'review').mkdir(parents=True)
            (private/'review/key').write_text('test-key')
            (private/'model-home').mkdir();(private/'model-home/auth.json').write_text('{}')
            (root/'.codex').mkdir();(root/'.codex/config.toml').write_text('model="test-model"')
            def request(ts):
                e={'teamId':'T','channelId':'D','threadTs':'1','messageTs':str(ts),'senderUserId':'O',
                   'mention':{'type':'user','id':'O'},'text':'为什么又停了'}
                c={'binding':['T','D','1',str(ts),'O','O',e['text']],'local':False,'review':True,
                   'targets':[],'history':[],'partial':False}
                b=base64.urlsafe_b64encode(json.dumps(c).encode()).decode().rstrip('=')
                t='intent1.'+b+'.'+hmac.new(b'test-key',('intent/1:'+b).encode(),hashlib.sha256).hexdigest()
                return {'event':e,'ticket':t},c
            first,claim=request(time.time());second,nextclaim=request(time.time()+1)
            def operations(args,key,directory):
                return SimpleNamespace(claim=claim if args==first else nextclaim,counts={},receipts=[],
                    prepare_reviews=lambda:{'continue_to_verdict':[]},services=lambda:[],aclose=AsyncMock())
            calls=[]
            def model(command,**kwargs):
                calls.append((command,json.loads(kwargs['input'].splitlines()[-1])))
                kwargs['stdout'].write('{"type":"thread.started","thread_id":"existing-session"}\n')
                kwargs['stdout'].flush()
                if len(calls)==1:return SimpleNamespace(returncode=-15)
                Path(command[command.index('-o')+1]).write_text('已核对并继续完成')
                return SimpleNamespace(returncode=0)
            with patch.object(runtime.review_evidence,'PRIVATE',private),patch.object(runtime,'ROOT',private/'runs/native-agent'), \
                 patch.object(runtime.Path,'home',return_value=root),patch.object(runtime,'AgentTools',side_effect=operations), \
                 patch.object(runtime,'run_model',side_effect=model):
                interrupted=runtime.execute(first)
                self.assertEqual(runtime.execute(first),interrupted)
                resumed=runtime.execute(second)
            self.assertEqual(len(calls),2)
            self.assertIn('resume',calls[1][0]);self.assertIn('existing-session',calls[1][0])
            self.assertIn('信号 15',calls[1][1]['previous_execution']['reason'])
            def decode(reply):return json.loads(base64.urlsafe_b64decode(reply['reply'].split(' ',1)[1].split('.')[0]+'==='))
            self.assertIn('信号 15',decode(interrupted)['text'])
            self.assertEqual(decode(resumed)['text'],'已核对并继续完成')

class StdioSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from textwrap import dedent

        code=dedent("""
            import json
            import os
            import sys
            import time

            initialized=0
            for line in sys.stdin:
                request=json.loads(line)
                if "id" not in request:
                    continue
                method=request["method"]
                if method=="initialize":
                    initialized+=1
                    result={"protocolVersion":request["params"]["protocolVersion"],
                            "capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}
                elif method=="tools/list":
                    result={"tools":[{"name":"get_status","inputSchema":{"type":"object"}}]}
                elif method=="tools/call":
                    action=request["params"].get("arguments",{}).get("action")
                    if action=="disconnect":
                        os._exit(3)
                    if action=="hang":
                        time.sleep(60)
                    result={"content":[],"structuredContent":{"pid":os.getpid(),"initialized":initialized,
                              "allow_write":os.environ.get("REDSHIFT_ALLOW_WRITE")}}
                else:
                    raise AssertionError(method)
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
        """)
        self.agent=AgentTools.__new__(AgentTools)
        self.agent.claim={'business':True}
        self.agent.catalog={}
        self.agent.counts={}
        self.agent._stdio_workers={}
        self.agent._closed=False
        self.service='redshift-fixture'
        self.agent.connections={self.service:{'command':sys.executable,'args':['-c',code],
                                        'env':{'REDSHIFT_ALLOW_WRITE':'true'}}}
        self.addAsyncCleanup(self.agent.aclose)

    async def read_status(self,action=None):
        args={} if action is None else {'action':action}
        return (await self.agent.read(self.service,'get_status',args))['result']['structuredContent']

    async def test_reuses_session_and_reaps_child_on_close(self):
        import asyncio
        import os

        expected=[{'name':'get_status','description':None,'parameters':{'type':'object'}}]
        self.assertEqual(await self.agent.discover(self.service),expected)
        first,second=await asyncio.gather(self.read_status(),self.read_status())
        self.assertEqual(first,second)
        self.assertEqual(first['initialized'],1)
        self.assertEqual(first['allow_write'],'false')
        self.assertEqual(await self.agent.discover(self.service),expected)
        self.assertEqual(first,await self.read_status())
        worker=self.agent._stdio_workers[self.service].task
        await self.agent.aclose()
        self.assertTrue(worker.done())
        self.assertFalse(self.agent._stdio_workers)
        with self.assertRaises(ProcessLookupError):
            os.kill(first['pid'],0)

    async def test_disconnect_drops_worker_and_next_read_reconnects(self):
        import asyncio
        import os
        from agent_runtime import tool_failure

        first=await self.read_status()
        with self.assertRaises(Exception) as failure:
            async with asyncio.timeout(5):
                await self.read_status('disconnect')
        self.assertTrue(tool_failure(failure.exception)['retryable'])
        self.assertNotIsInstance(failure.exception,TimeoutError)
        self.assertNotIn(self.service,self.agent._stdio_workers)
        with self.assertRaises(ProcessLookupError):
            os.kill(first['pid'],0)
        second=await self.read_status()
        self.assertNotEqual(first['pid'],second['pid'])
        self.assertEqual(second['initialized'],1)

    async def test_timeout_reaps_process_and_next_read_reconnects(self):
        import asyncio
        import os

        first=await self.read_status()
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.2):
                await self.read_status('hang')
        self.assertNotIn(self.service,self.agent._stdio_workers)
        with self.assertRaises(ProcessLookupError):
            os.kill(first['pid'],0)
        second=await self.read_status()
        self.assertNotEqual(first['pid'],second['pid'])


unittest.main()
