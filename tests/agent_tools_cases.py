import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from agent_tools import readonly_sql, allowed_tool, AgentTools

class NativeToolsTests(unittest.TestCase):
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
        self.assertEqual(set(peer),{"discover_tools","read_tool","github_read","review_context","submit_review"})
        self.assertTrue(all(x["approval_mode"]=="approve" for x in peer.values()))
        self.assertIn("owner_shell",tomllib.loads(tool_policy(True))["mcp_servers"]["relay"]["tools"])

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

unittest.main()
