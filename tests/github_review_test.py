import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
spec = importlib.util.spec_from_file_location("review",Path(__file__).resolve().parents[1]/"scripts/github-review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ApprovalTests(unittest.TestCase):
    def test_missing_resource_does_not_claim_missing_repository_permission(self):
        from types import SimpleNamespace
        with patch.object(review,"GH","gh"), patch.object(review.subprocess,"run",return_value=SimpleNamespace(returncode=1,stderr="HTTP 404",stdout="")):
            with self.assertRaisesRegex(review.Stop,"不能仅凭此认定仓库无权限"):
                review.api("repos/team/repo/contents/AGENTS.md")

    def setUp(self):
        identity = patch.object(review,"LOGIN","test-reviewer")
        identity.start()
        self.addCleanup(identity.stop)
        self.pr = {"node_id":"node","base":{"sha":"base"},"head":{"sha":"head"}}
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("create table jobs(id text,base text,head text,started integer,attempting integer)")
        self.db.execute("insert into jobs values('state','base','head',0,0)")
        self.verdict = json.dumps({"decision":"REQUEST_CHANGES","summary":"有回归","details":"旧记录未删除。"})
        self.record = {"id":1,"user":{"login":review.LOGIN},"state":"CHANGES_REQUESTED","commit_id":"head","body":"有回归\n<!-- perry-relay-review state -->"}

    def finish(self, pr=None):
        return review.finish("owner/repo",1,"endpoint",pr or self.pr,"state","ticket",self.verdict,self.db)

    def test_inspect_cannot_vote_even_if_model_proposes_approval(self):
        import base64
        with patch.object(review,"KEY",b"key"), patch.object(review,"api") as api:
            for decision in ("APPROVE","REQUEST_CHANGES","INCOMPLETE"):
                output = json.dumps({"decision":decision,"summary":"说明","details":"核验结果"})
                raw = review.finish("team/repo",1,"ep",self.pr,"state","ticket",output,self.db,mode="inspect")
                payload = json.loads(base64.urlsafe_b64decode(raw.split(" ",1)[1].split(".")[0]+"==="))
                self.assertEqual(payload["status"],"INSPECTED")
                self.assertIsNone(payload["review"])
            api.assert_not_called()

    def test_changed_revision(self):
        with self.assertRaises(review.Stop):
            self.finish({"base":{"sha":"base"},"head":{"sha":"new"}})

    def replay_after_push(self, prior_attempt=False, unstable=False):
        import base64
        self.db.execute("create table review_context(thread text,ts real,report text,primary key(thread,ts))")
        self.db.execute("update jobs set attempting=?", (int(prior_attempt),))
        self.pr["head"]["sha"] = "new-head"
        packet = {"full_diff":"new code"}
        output = {"decision":"REQUEST_CHANGES","summary":"最新版本仍有问题","details":"新版本修改的条件不完整。"}
        args = {"event":{"text":"CR下","messageTs":"10"},"output":'{"decision":"APPROVE"}'}
        with patch.object(review,"KEY",b"test"), patch.object(review.evidence,"POLICY") as policy, \
                patch.object(review,"stable_packet",return_value=packet) as materials, \
                patch.object(review.evidence,"prompt",return_value="review new code"), \
                patch.object(review.evidence,"isolated_turn",return_value=json.dumps(output)) as model, \
                patch.object(review,"snapshot",return_value=self.pr), patch.object(review,"finish") as finish:
            if unstable:
                materials.side_effect = review.RevisionChanged(["base","old"],self.pr)
            policy.read_text.return_value='{"operations":{}}'
            finish.return_value="success"
            result = review.complete_review(args,"team/repo",1,"ep",self.pr,"state","ticket",self.db,
                                            {"request_mode":"review"},"thread")
            if prior_attempt or unstable:
                receipt = json.loads(base64.urlsafe_b64decode(result["reply"].split(" ",1)[1].split(".")[0]+"==="))
                self.assertEqual(receipt["status"],"INCOMPLETE")
                finish.assert_not_called()
                model.assert_not_called()
                report = self.db.execute("select report from review_context").fetchone()[0]
                self.assertIn("未确认 GitHub 审批",report)
                self.assertEqual(materials.call_count, 0 if prior_attempt else 3)
            else:
                self.assertEqual(result["reply"],"success")
                model.assert_called_once()
                self.assertEqual(json.loads(finish.call_args.args[6]),output)
                self.assertEqual(finish.call_args.args[4],"state:base:new-head")
                self.assertEqual(self.db.execute("select head from jobs where id='state'").fetchone()[0],"head")

    def test_new_revision_requires_fresh_verdict(self):
        self.replay_after_push()

    def test_new_revision_after_uncertain_write_never_votes_again(self):
        self.replay_after_push(prior_attempt=True)

    def test_continuous_pushes_record_a_bounded_failure(self):
        self.replay_after_push(unstable=True)

    def test_closed_review_records_why_it_stopped_for_the_next_thread_question(self):
        import tempfile
        closed = {**self.pr,"state":"closed","merged_at":None}
        event = {"teamId":"T","channelId":"D","threadTs":"91","messageTs":"92","text":"review"}
        with tempfile.TemporaryDirectory() as folder, patch.object(review,"ROOT",Path(folder)), \
                patch.object(review,"KEY",b"test"), patch.object(review,"snapshot",return_value=closed), \
                patch.object(review,"prepare") as prepare:
            result = review.run_review("prepare",{"event":event,"ticket":"ticket"},"team/repo",1,{"mode":"review"})
            prepare.assert_not_called()
            self.assertTrue(result["reply"].startswith("relay-review/2 "))
            with sqlite3.connect(Path(folder)/"reviews.sqlite") as audit:
                row = audit.execute("select ts,report from review_context").fetchone()
                self.assertEqual(row[0],92)
                self.assertIn("PR 已关闭",row[1])
                self.assertIn("本次没有发起 GitHub 审批写入",row[1])
            import base64
            existing = {**self.record,"submitted_at":"2026-01-01T01:00:00Z"}
            with patch.object(review,"api",return_value=[existing]) as api:
                status = review.run_review("prepare",{"event":{**event,"messageTs":"93"},"ticket":"next"},
                                           "team/repo",1,{"mode":"inspect","status_only":True})
                api.assert_called_once_with("repos/team/repo/pulls/1/reviews?per_page=100",pages=True)
            payload = json.loads(base64.urlsafe_b64decode(status["reply"].split(" ",1)[1].split(".")[0]+"==="))
            self.assertEqual(payload["status"],"INSPECTED")
            self.assertIsNone(payload["review"])
            self.assertIn("本次没有发起 GitHub 审批写入",payload["text"])
            self.assertIn("#pullrequestreview-1",payload["text"])

    def test_replayed_prepare_cannot_overwrite_initial_pin(self):
        latest = {**self.pr,"head":{"sha":"second"}}
        with patch.object(review,"collect") as collect:
            prompt = review.prepare("team/repo",1,"ep",latest,"state",self.db)
        collect.assert_not_called()
        self.assertIn("REFRESH",prompt)
        self.assertEqual(self.db.execute("select head from jobs where id='state'").fetchone()[0],"head")

    def test_missing_pin_is_distinct_from_changed_revision(self):
        self.db.execute("delete from jobs")
        with self.assertRaisesRegex(review.Stop,"找不到"):
            self.finish()

    def test_frustrated_followup_checks_previous_failure_without_voting(self):
        import slack_intent
        import tempfile
        claim = {"binding":["T","C","123","125","OWNER","OWNER","What happened there?"],
                 "review":True,"overflow":False,"targets":["team/repo/pull/19"],"history":[]}
        with tempfile.TemporaryDirectory() as root, patch.object(slack_intent.review_evidence,"PRIVATE",Path(root)), \
                patch.object(slack_intent,"classify",return_value={"route":"answer","answer":"让我解释"}) :
            folder=Path(root)/"review"
            folder.mkdir()
            with sqlite3.connect(folder/"reviews.sqlite") as audit:
                audit.execute("create table review_context(thread text,ts real,report text)")
                audit.execute("insert into review_context values(?,?,?)",(json.dumps(["T","C","123","team/repo",19]),124,"未确认 GitHub 审批 · abc。"))
            self.assertEqual(slack_intent.frozen_plan("ticket","prepare",claim),{"route":"status","target":"team/repo/pull/19"})

    def test_retry_recovers_exact_review_no_second_post(self):
        with patch.object(review,"KEY",b"key"), patch.object(review,"api",side_effect=[[self.record],self.record,self.pr]) as api:
            self.assertTrue(self.finish().startswith("relay-review/2 "))
            self.assertTrue(all("write" not in call.kwargs for call in api.call_args_list))

    def test_request_changes_is_bound_to_sha_and_read_back(self):
        draft = {**self.record,"state":"PENDING","body":""}
        answers = [[],draft,draft,[],self.record,self.record,self.pr]
        with patch.object(review,"KEY",b"key"), patch.object(review,"snapshot",return_value=self.pr), patch.object(review,"api",side_effect=answers) as api:
            self.finish()
            self.assertEqual(api.call_args_list[1].kwargs["write"]["commit_id"],"head")
            self.assertEqual(api.call_args_list[4].kwargs["write"]["event"],"REQUEST_CHANGES")
            self.assertIn("旧记录未删除",api.call_args_list[4].kwargs["write"]["body"])

    def test_incomplete_has_signed_footer_without_github_write(self):
        import base64
        self.verdict = json.dumps({"decision":"INCOMPLETE","summary":"需要验证字段","details":"查询未完成"})
        with patch.object(review,"KEY",b"key"), patch.object(review,"api") as api:
            result = self.finish()
        api.assert_not_called()
        body = result.split(" ",1)[1].split(".")[0]
        receipt = json.loads(base64.urlsafe_b64decode(body+"==="))
        self.assertEqual(receipt["status"],"INCOMPLETE")
        self.assertIsNone(receipt["review"])
        self.assertTrue(receipt["text"].startswith("暂缓审批"))

    def test_tool_counts_come_from_host_history(self):
        import base64
        history = [{"name":"column_lookup","result":{"ok":True}}, {"name":"column_lookup","result":{"ok":False}}]
        policy = {"operations":{"column_lookup":{"display_name":"Redshift"}}}
        with patch.object(review,"KEY",b"key"), patch.object(review,"GH_CALLS",5):
            result = review.signed_result("owner/repo",1,self.pr,"ticket","body",0,"INCOMPLETE",history=history,policy=policy,prior_calls=10)
        receipt = json.loads(base64.urlsafe_b64decode(result.split(" ",1)[1].split(".")[0]+"==="))
        self.assertEqual(receipt["tools"],[{"name":"GitHub","count":15,"failed":0},{"name":"Redshift","count":2,"failed":1}])

    def test_formal_mode_does_not_inherit_readonly_history(self):
        packet={"full_diff":"diff","request_mode":"review","prior_report":"只读复核，未投票"}
        formal=review.evidence.prompt(packet,{},[])
        self.assertIn("正式 GitHub 代码审查",formal)
        self.assertNotIn("本轮是只读追问",formal)
        self.assertIn("本轮是只读追问",review.evidence.prompt({**packet,"request_mode":"inspect"},{},[]))

    def test_review_body_keeps_technical_finding_and_removes_false_delivery_notice(self):
        statement="此结论为只读审查意见；当前授权操作不包含 GitHub 审批提交，未提交正式审批，也没有审批链接。"
        details="只读账号无法写表，迁移会失败。"
        self.assertEqual(review.finding_text(details+"\n\n"+statement),details)

    def test_ambiguous_write_is_not_repeated(self):
        self.db.execute("update jobs set attempting=1")
        with patch.object(review,"api",return_value=[]) as api, self.assertRaises(review.Stop):
            self.finish()
        self.assertEqual(api.call_count,1)

    def test_forgery(self):
        with patch.object(review,"KEY",b"test"), self.assertRaises(review.Stop):
            review.authorized("abc.bad",{})
