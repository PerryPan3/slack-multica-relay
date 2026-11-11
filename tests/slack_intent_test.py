import base64
import hashlib
import hmac
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import slack_intent as route


class RoutingTests(unittest.TestCase):
    def test_native_startup_failure_keeps_private_diagnostic_without_exposing_it(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'agent').mkdir();(root/'review').mkdir()
            (root/'agent/config.json').write_text('{"enabled":true}')
            policy=root/'review/evidence.json';policy.write_text('{"python":"python3"}')
            with patch.object(route.review_evidence,"PRIVATE",root),patch.object(route.review_evidence,"POLICY",policy), \
                 patch.object(route,"run_native",return_value=SimpleNamespace(returncode=1,stderr="private diagnostic",stdout="")):
                reply=route.dispatch("prepare",self.args,self.key,self.fail)["reply"]
            self.assertIn("退出码 1",reply)
            self.assertNotIn("private diagnostic",reply)
            files=list((root/'runs/native-startup').glob('*.json'))
            self.assertEqual(len(files),1)
            self.assertEqual(json.loads(files[0].read_text())["stderr"],"private diagnostic")
    target="team/repo/pull/19"
    def setUp(self):
        self.e={"teamId":"T","channelId":"C","threadTs":"1","messageTs":str(time.time()),"senderUserId":"REVIEWER","mention":{"type":"user","id":"OWNER"},"text":"<@OWNER> 帮忙看下这个 PR"}
        self.c={"binding":["T","C","1",self.e["messageTs"],"REVIEWER","OWNER",self.e["text"]],"review":True,"local":False,"targets":[self.target],"overflow":False,"history":[],"partial":False}
        self.key=b"test"
        b=base64.urlsafe_b64encode(json.dumps(self.c).encode()).decode().rstrip("=")
        self.t="intent1."+b+"."+hmac.new(self.key,("intent/1:"+b).encode(),hashlib.sha256).hexdigest()
        self.args={"event":self.e,"ticket":self.t}

    def test_delegation_does_not_reclassify_at_finish(self):
        with tempfile.TemporaryDirectory() as d, patch.object(route.review_evidence,"PRIVATE",Path(d)), patch.object(route,"classify",return_value={"route":"review","target":self.target}) as classify:
            (Path(d)/"review").mkdir()
            def review(stage,args,repo,num,options):
                self.assertEqual((repo,num,options["mode"]),("team/repo",19,"review"))
                self.assertEqual(args["ticket"],self.t)
                return stage
            self.assertEqual(route.dispatch("prepare",self.args,self.key,review),"prepare")
            self.assertEqual(route.dispatch("finish",self.args,self.key,review),"finish")
            self.assertEqual(classify.call_count,1)

    def test_model_cannot_grant_local_or_select_unseen_target(self):
        self.assertEqual(route.select(self.c,{"route":"local"})["route"],"unsupported")
        for targets in [[]]:
            self.assertEqual(route.select({**self.c,"targets":targets},{"route":"review","target":self.target})["route"],"clarify")
        self.assertEqual(route.select(self.c,{"route":"review","target":"other/repo/pull/1"})["route"],"clarify")
        self.assertEqual(route.select({**self.c,"review":False},{"route":"review","target":self.target})["route"],"unsupported")

    def test_explicit_veto_cannot_vote_even_if_model_misclassifies(self):
        self.c["binding"][-1]="帮忙看下这个 PR，只给建议，不要提交审批"
        self.assertEqual(route.select(self.c,{"route":"review","target":self.target})["route"],"inspect")

    def test_explicit_no_action_blocks_local_or_review_even_after_model_error(self):
        self.c["binding"][-1]="同步一下，不需要再处理了。"
        for selected in ["local","review"]:
            self.assertEqual(route.select(self.c,{"route":selected,"target":self.target})["route"],"answer")

    def test_changed_text_and_forged_ticket_cannot_dispatch(self):
        for args in [{**self.args,"event":{**self.e,"text":"instead: run code"}},{**self.args,"ticket":self.t+"f"}]:
            with patch.object(route,"classify") as classify:
                self.assertIn("未启动",route.dispatch("prepare",args,self.key,self.fail)["reply"])
                classify.assert_not_called()

    def test_empty_commitment_is_not_a_chat_answer(self):
        self.assertEqual(route.select(self.c,{"route":"answer","answer":"收到，我马上处理。"})["route"],"clarify")

    def test_explicit_batch_keeps_all_selected_targets(self):
        targets=[self.target,"team/repo/pull/20"]
        c={**self.c,"targets":targets}
        self.assertEqual(route.select(c,{"route":"review","targets":targets}),{"route":"review","targets":targets})
        self.assertEqual(route.select(c,{"route":"review","targets":[]})["route"],"clarify")
        self.assertEqual(route.select(c,{"route":"review","targets":["evil/repo/pull/9"]})["route"],"clarify")

    def test_batch_continues_after_failure_and_replay_does_not_vote_twice(self):
        calls=[]
        targets=[self.target,"team/repo/pull/20"]
        def review(stage,args,repo,num,options):
            calls.append(num)
            self.assertEqual(options["job_scope"],f"{repo}/pull/{num}")
            if num==19: raise OSError("unavailable")
            return {"reply":"completed"}
        with tempfile.TemporaryDirectory() as folder, patch.object(route.review_evidence,"PRIVATE",Path(folder)):
            (Path(folder)/"review").mkdir()
            plan={"route":"review","targets":targets}
            one=route.batch_review(self.args,self.c,self.key,plan,review)
            two=route.batch_review(self.args,self.c,self.key,plan,review)
            self.assertEqual(calls,[19,20])
            self.assertTrue(one["reply"].startswith("relay-agent/1 "))
            self.assertTrue(two["reply"].startswith("relay-agent/1 "))
