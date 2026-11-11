from pathlib import Path
import json
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import business_investigation as b
import slack_intent as route

class BusinessTests(unittest.TestCase):
    config={"team":"T","channel":"C","owner":"U0","senders":["U1","U2"],"self_dm":"D1","jira_projects":["RPT"]}
    def claim(self,sender,channel):
        return {"business":True,"review":True,"local":False,"targets":[],"overflow":False,"partial":False,
                "binding":["T",channel,"1","2",sender,"U0","查 RPT-481"],"history":[]}
    def test_extra_channel_grants_do_not_form_a_sender_channel_cross_product(self):
        policy={**self.config,"channel_sender_grants":["C:U3","C:U4","C2:U4"]}
        for sender,channel,allowed in [("U3","C",True),("U4","C",True),("U4","C2",True),
                                     ("U3","C2",False),("U1","C2",False),("U4","D9",False)]:
            self.assertEqual(b.permitted(self.claim(sender,channel),policy),allowed)
    def test_receiver_constraints_are_rechecked_locally(self):
        for sender,chan,allowed in [("U1","C",True),("U2","C",True),("U9","C",False),("U0","C",False),
                                   ("U1","D1",False),("U0","D1",True),("U0","D9",False),("U2","C2",False)]:
            self.assertIs(b.permitted(self.claim(sender,chan),self.config),allowed)
        c=self.claim("U1","C"); c["business"]=False
        self.assertFalse(b.permitted(c,self.config))
        self.assertEqual(route.select(c,{"route":"business"})["route"],"unsupported")
        self.assertEqual(route.select(self.claim("U1","C"),{"route":"business"})["route"],"business")
        self.assertEqual(route.select(self.claim("U2","C"),{"route":"local"})["route"],"unsupported")

    def test_fixed_sql_templates_and_bounded_arguments(self):
        columns={"server":"redshift-mcp-server","tool":"list_columns","args":{"cluster_identifier":"cluster","column_database_name":"dw"},
                 "choices":{"schema":["dwd"]},"purpose":"cols"}
        ops=b.operations({"redshift_flow_dw_columns":columns})
        op=ops["redshift_flow_dw_daily_count"]
        args={"schema":"dwd","table":"sales","date_column":"pt","start":"2026-10-01","end":"2026-10-02"}
        data=b.args_for(op,args,"dwd.sales",self.config)
        self.assertIn("COUNT(*)",data["sql"]);self.assertIn("2026-10-03",data["sql"])
        for change in [{"table":"sales;DELETE"},{"schema":"private"},{"end":"2026-12-01"},{"sql":"DELETE FROM dwd.sales"},{"table":"unrelated"}]:
            with self.assertRaises(b.evidence.Unavailable):
                b.args_for(op,{**args,**change},"dwd.sales",self.config)
        with self.assertRaises(b.evidence.Unavailable):
            b.args_for(ops["jira_issue"],{"issue":"HR-7"},"HR-7",self.config)
        self.assertFalse(any(v["tool"] in ("jira_update_issue","run_shell") for v in ops.values()))

    def test_final_requires_successful_host_receipt(self):
        c=self.claim("U1","C")
        conf={**self.config,"operations":b.operations({})}
        first=json.dumps({"action":"query","requests":[{"name":"jira_issue","arguments":{"issue":"RPT-481"}}]})
        def final(*_):return json.dumps({"action":"done","text":"已查实","evidence_ids":["B1"]})
        import base64
        for ok in (True,False):
            raw=b.investigate({"ticket":"t","output":first},c,b"k",conf,
                              run=lambda *a:{"ok":ok},turn=final)
            packet=json.loads(base64.urlsafe_b64decode(raw.split(" ",1)[1].split(".")[0]+"==="))
            self.assertEqual("已查实" in packet["text"],ok)
            self.assertEqual(packet["tools"][0]["failed"],int(not ok))

    def test_duplicate_request_reads_once_and_returns_verified_facts(self):
        c=self.claim("U1","C"); conf={**self.config,"operations":b.operations({})}
        first=json.dumps({"action":"query","requests":[{"name":"jira_issue","arguments":{"issue":"RPT-481"}}]})
        calls=[]
        def query(*_):
            calls.append(1)
            return {"ok":True,"data":{"records":[{"key":"RPT-481","status":"Closed"}]}}
        import base64
        raw=b.investigate({"ticket":"t","output":first},c,b"k",conf,run=query,turn=lambda *_:first)
        packet=json.loads(base64.urlsafe_b64decode(raw.split(" ",1)[1].split(".")[0]+"==="))
        self.assertEqual(len(calls),1)
        self.assertIn("Closed",packet["text"])
        self.assertEqual(packet["tools"][0]["count"],1)

    def test_corrected_local_parameter_error_is_not_reported_as_missing_evidence(self):
        policy={"operations":{"lookup":{"display_name":"Redshift"}}}
        history=[{"name":"lookup","arguments":{"table":"typo"},"result":{"ok":False,"called":False}},
                 {"name":"lookup","arguments":{"table":"sales"},"result":{"ok":True,"called":True}}]
        self.assertEqual(b.unresolved(history,policy),[])
        history[0]["result"]["called"]=True
        self.assertEqual(b.unresolved(history,policy),["Redshift"])

    def test_jira_projection_drops_contact_info_and_extraneous_fields(self):
        out=b.project({"key":"RPT-1","status":{"name":"Done"},"summary":"a","description":"mail user@example.com",
                       "assignee":{"name":"private"}},{"kind":"jira"})
        self.assertNotIn("user@example",str(out));self.assertNotIn("assignee",str(out))
