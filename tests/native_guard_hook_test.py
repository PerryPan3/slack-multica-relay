import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_guard_hook as guard


def shell(command, **kwargs):
    return {"tool_name": "Bash", "tool_input": {"command": command, **kwargs}, "cwd": guard.REPO}


def mcp(name, **kwargs):
    return {"tool_name": "mcp__" + name, "tool_input": kwargs, "cwd": guard.REPO}


class GuardTests(unittest.TestCase):
    def test_reads_and_review_never_resolve_identity(self):
        cases = [shell(c) for c in ("git diff", "git clone --depth 1 https://github.com/MoeGolibrary/Boarding_Desktop /tmp/bd",
                 "rg -n foo src", "git status && git diff --stat",
                 "python3 -m unittest discover", "python3 -c 'import yaml, collections; print(collections.Counter([1, 1]))'",
                 "dbt parse", "dbt compile --select model", "gh pr view 915",
                 "gh pr diff 915", "gh pr checks 915", "gh pr review 915 --approve",
                 "gh pr review 915 --request-changes --body 'fix the join'", "gh api -X GET repos/team/repo",
                 "gh run view 12345 --log-failed", "gh run list --limit 5", "gh issue view 42",
                 "gh repo view MoeGolibrary/moego-dbt-dw", "gh search prs --repo MoeGolibrary/moego-dbt-dw",
                 "rm -rf /tmp/native-guard-fixture", "rm -rf ./target", "cat ~/.ssh/config")]
        cases += [mcp("redshift__redshift_check_table", table="demo"), mcp("mcp_atlassian__jira_get_issue")]
        with patch.object(guard, "current_trigger") as identity:
            for event in cases:
                with self.subTest(event=event):
                    self.assertIsNone(guard.decide(event))
            identity.assert_not_called()

    def test_unknown_tools_and_opaque_shells_cannot_silently_write(self):
        for event in [shell("python3 -c \"open('~/.ssh/config', 'w')\""),
                      shell("env -i git push"), shell("bash"), shell("gh alias run deploy"),
                      mcp("aws_dataprocessing__manage_aws_glue_jobs", operation="start-job-run")]:
            with self.subTest(event=event):
                self.assertEqual(guard.decide(event), guard.UNKNOWN)
        self.assertIsNone(guard.decide(mcp("aws_dataprocessing__manage_aws_glue_jobs", operation="get-jobs")))

    def test_sql_dialects_and_parser_fallback(self):
        good = ["SELECT format_type(id, NULL), isnull(x,0), GREATEST(a,b), convert_timezone('UTC', ts) FROM t",
                "/* DELETE FROM t */ WITH a AS (SELECT 'DROP; ''INSERT' AS value) SELECT * FROM a;",
                "SELECT \"delete\" FROM t -- drop table ignored", "EXPLAIN SELECT 1", "SHOW TABLES", "DESCRIBE demo"]
        bad = ["DROP TABLE t", "DELETE FROM t", "GRANT SELECT ON t TO u", "WITH x AS (DELETE FROM t) SELECT 1",
               "SELECT 1; UPDATE t SET a=2", "EXPLAIN INSERT INTO t SELECT 1", "SELECT * INTO t2 FROM t",
               "SELECT 1; SELECT 2", "CALL proc()", "/* comment", "", None]
        for fallback in (False, True):
            with patch.dict(sys.modules, {"sqlglot": None} if fallback else {}):
                for op in ("redshift__redshift_query", "redshift__redshift_explain", "redshift_mcp_server__execute_query",
                           "clickhouse_prod__query"):
                    for sql in good + bad:
                        with self.subTest(op=op, fallback=fallback, sql=sql):
                            result = guard.decide(mcp(op, sql=sql))
                            self.assertEqual(result is None, sql in good)

    def test_mcp_writes_never_reach_owner_gate(self):
        names = ["redshift__redshift_drop_table", "redshift__redshift_grant_permissions",
                 "mwaa_airflow__clear_task_instance", "aws_dataprocessing__upload_to_s3",
                 "aws_dataprocessing__create_data_processing_role", "aws_dataprocessing__add_inline_policy",
                 "slack__conversations_add_message", "slack__get_users", "mcp_atlassian__jira_create_issue",
                 "mcp_atlassian__jira_add_comment", "mcp_atlassian__confluence_set_page_restrictions"]
        with patch.object(guard, "current_trigger") as identity:
            for name in names:
                with self.subTest(name=name):
                    self.assertIsNotNone(guard.decide(mcp(name)))
            identity.assert_not_called()

    def test_protected_paths_redirects_edits_and_patch_moves(self):
        cases = [shell(c) for c in ("echo new >> ~/.ssh/config", "touch ~/.aws/credentials", "rm -rf .private/",
                 "cd .private && touch settings", "cp profile ~/Library/LaunchAgents/sample.plist",
                 "echo data | tee \"$HOME/.codex/auth.json\"", "rm -rf ~/.codex")]
        cases += [{"tool_name": name, "tool_input": args, "cwd": guard.REPO} for name, args in [
            ("Write", {"file_path": ".private/settings"}), ("Edit", {"file_path": "~/.ssh/config"}),
            ("apply_patch", {"command": "*** Begin Patch\n*** Update File: a\n*** Move to: .private/b\n*** End Patch"})]]
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "settings"
            link.symlink_to(Path.home() / ".ssh/config")
            cases.append(shell("touch " + str(link)))
            for event in cases:
                with self.subTest(event=event):
                    self.assertEqual(guard.decide(event), guard.PROTECTED)

    def test_external_command_gate(self):
        commands = ["git push origin main", "git -C repo push", "gh pr merge 915", "gh pr close 915",
                    "gh pr ready 915", "gh pr create", "gh pr edit 915", "gh api -XPOST endpoint",
                    "gh api endpoint --method=DELETE", "gh api endpoint -fa=b", "gh api endpoint -F k=v",
                    "gh api endpoint --raw-field=k=v", "gh api endpoint --input data.json",
                    "dbt run --select example", "dbt build", "wrangler deploy", "pnpm exec wrangler deploy",
                    "launchctl list", "rm -rf /opt/old", "git diff && git push", "pnpm exec git push", "/bin/zsh -lc 'git push origin main'"]
        for sender, text, want in [("UOTHER", "可以", guard.COLLEAGUE), (guard.OWNER, "帮我看看需要改啥", guard.CONFIRM),
                                   (guard.OWNER, "不可以", guard.CONFIRM), (guard.OWNER, "<@" + guard.OWNER + "> 可以", None)]:
            with patch.object(guard, "current_trigger", return_value=(sender, text, True)):
                for command in commands:
                    with self.subTest(sender=sender, text=text, command=command):
                        self.assertEqual(guard.decide(shell(command)), want)
        with patch.object(guard, "current_trigger", side_effect=ValueError):
            self.assertEqual(guard.decide(shell("git push")), guard.UNVERIFIED)

    def test_explicit_command_is_bound_to_action_and_target(self):
        with patch.object(guard, "current_trigger", return_value=(guard.OWNER, "直接 push 并合并 #915", False)):
            self.assertIsNone(guard.decide(shell("gh pr merge 915")))
            with patch.object(guard, "push_matches_pr", return_value=True) as match:
                self.assertIsNone(guard.decide(shell("git push && gh pr merge 915")))
                match.assert_called_once_with("915", [], guard.REPO + "/")
            with patch.object(guard, "push_matches_pr", return_value=False):
                self.assertEqual(guard.decide(shell("git push origin wrong")), guard.CONFIRM)
            self.assertEqual(guard.decide(shell("gh pr merge 916")), guard.CONFIRM)
            self.assertEqual(guard.decide(shell("dbt run")), guard.CONFIRM)
        for msg in ("可以", "是否可以直接合并 #915？", "不要直接合并 #915", "他说「直接合并 #915」"):
            with patch.object(guard, "current_trigger", return_value=(guard.OWNER, msg, False)):
                self.assertEqual(guard.decide(shell("gh pr merge 915")), guard.CONFIRM)

    def test_pr_scoped_push_verifies_remote_and_branch(self):
        def read(argv, **kwargs):
            if argv[0] == "git":
                out = "git@github.com:team/repo.git"
            else:
                out = json.dumps({"number": 915, "headRefName": "feature", "headRepository": {"name": "repo"},
                                  "headRepositoryOwner": {"login": "team"}})
            return subprocess.CompletedProcess(argv, 0, out)
        with patch("subprocess.run", side_effect=read):
            self.assertTrue(guard.push_matches_pr("915", ["origin", "feature"], guard.REPO))
            self.assertFalse(guard.push_matches_pr("915", ["origin", "wrong"], guard.REPO))
            self.assertFalse(guard.push_matches_pr("915", ["origin", ":feature"], guard.REPO))
            self.assertFalse(guard.push_matches_pr("915", ["--all"], guard.REPO))

    def test_subprocess_contract_and_invalid_input(self):
        for tool, data, expected in (("exec_command", {"cmd": "git diff"}, "allow"),
                                     ("shell_command", {"command": "git status"}, "allow"),
                                     ("Bash", {}, "deny"), ("apply_patch", {"command": ""}, "deny")):
            p = subprocess.run([sys.executable, guard.__file__], text=True, capture_output=True, check=True,
                               input=json.dumps({"tool_name": tool, "tool_input": data, "cwd": guard.REPO}))
            output = json.loads(p.stdout)["hookSpecificOutput"]
            self.assertEqual((output["permissionDecision"], output["hookEventName"]), (expected, "PreToolUse"))
            self.assertTrue(output["permissionDecisionReason"])


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.issue, self.task, self.workspace, self.comment = (str(UUID(int=i)) for i in range(1, 5))
        self.env = {"MULTICA_ISSUE_ID": self.issue, "MULTICA_TASK_ID": self.task,
                    "MULTICA_WORKSPACE_ID": self.workspace, "MULTICA_API_URL": "https://fixture.invalid"}
        self.run = {"id": self.task, "issue_id": self.issue, "workspace_id": self.workspace,
                    "agent_id": "agent", "status": "running", "trigger_comment_id": None}
        self.ts = str(int(time.time())) + ".001"
        self.root = self.source(False, "帮我看看需要改啥")
        self.reply = self.source(True, "可以")

    def source(self, followup, text):
        def digest(parts):
            return hashlib.sha256(":".join(parts).encode()).hexdigest()
        message = self.ts[:-1] + "2" if followup else self.ts
        event = dict(teamId="T", channelId="C", threadTs=self.ts, messageTs=message, senderUserId=guard.OWNER,
                     mention={"type": "user", "id": guard.OWNER}, text=text)
        binding = ["T", "C", self.ts, message, guard.OWNER, guard.OWNER, text]
        body = base64.urlsafe_b64encode(json.dumps({"binding": binding}).encode()).decode().rstrip("=")
        ticket = "intent1." + body + "." + hmac.new(b"fixture", ("intent/1:" + body).encode(), hashlib.sha256).hexdigest()
        marker = ("relay-message:" + digest(("T", "C", message)) if followup else
                  "relay-thread:" + digest((self.workspace, "project", "agent")) + ":" + digest(("T", "C", self.ts)))
        envelope = {"eventPayload": event, "context": {"githubReviewTicket": ticket}}
        return "<!-- " + marker + " -->\nquote\n<!-- relay-payload:v1 -->\n```json\n" + json.dumps(envelope) + "\n```\n<!-- /relay-payload -->"

    def cli(self, args, **kwargs):
        if "runs" in args:
            rows = [dict(self.run, id="previous"), self.run]
        elif "comment" in args:
            self.assertIn(self.comment, args)
            rows = [{"id": self.comment, "issue_id": self.issue, "content": self.reply}]
        else:
            rows = {"id": self.issue, "workspace_id": self.workspace, "project_id": "project",
                    "assignee_type": "agent", "assignee_id": "agent", "description": self.root}
        return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")

    def test_uses_exact_signed_trigger_and_fails_closed_on_mismatch(self):
        with patch.dict(os.environ, self.env), patch("subprocess.run", side_effect=self.cli), patch.object(Path, "read_text", return_value="fixture"):
            self.assertEqual(guard.decide(shell("git push")), guard.CONFIRM)
            self.run["trigger_comment_id"] = self.comment
            self.assertIsNone(guard.decide(shell("git push")))
            self.reply = self.reply.replace("senderUserId", "forged")
            self.assertEqual(guard.decide(shell("git push")), guard.UNVERIFIED)
            self.reply = self.source(True, "可以")
            self.run["issue_id"] = "unrelated"
            self.assertEqual(guard.decide(shell("git push")), guard.UNVERIFIED)
