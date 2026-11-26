import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import review_evidence as evidence


class EvidenceTests(unittest.TestCase):
    repo = "our-org/repo"
    packet = {"full_diff": "select count(*) from core.sales"}
    status = {
        "decision": "APPROVE",
        "summary": "verified",
        "details": "details",
        "evidence_ids": ["E1"],
    }
    op = {
        "params": {"table": "identifier"},
        "purpose": "metadata",
        "args": {"name": "{table}"},
    }
    config = {"owners": ["our-org"], "operations": {"metadata": op}}
    first = json.dumps(
        {
            "decision": "EVIDENCE",
            "requests": [
                {
                    "name": "metadata",
                    "arguments": {"table": "sales"},
                    "question": "Does sales exist?",
                }
            ],
        }
    )

    def test_isolated_reasoning_override_preserves_cli_and_environment_isolation(self):
        process=Mock(returncode=0,pid=4321,communicate=Mock(return_value=('verdict','')))
        with patch.object(evidence,'PRIVATE',Path('/unused/runtime')), \
             patch('network_recovery.enabled',return_value=False), \
             patch.object(evidence.subprocess,'Popen',return_value=process) as spawn, \
             patch.object(evidence.os,'killpg'):
            self.assertEqual(evidence.isolated_turn('classify',{},reasoning_effort='low'),'verdict')
            self.assertEqual(spawn.call_args.args[0],[
                '/unused/runtime/bin/codex','exec','--ephemeral','--skip-git-repo-check','--color','never',
                '-c','model_reasoning_effort="low"','-',
            ])
            self.assertEqual(spawn.call_args.kwargs['env'],{'CODEX_HOME':'/unused/runtime/model-home'})
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            evidence.isolated_turn('review',{})
            self.assertNotIn('-c',spawn.call_args.args[0])
            spawn.reset_mock()
            with self.assertRaisesRegex(ValueError,'Unsupported reasoning effort'):
                evidence.isolated_turn('classify',{},reasoning_effort='invalid')
            spawn.assert_not_called()

    def test_decodes_both_sdk_result_shapes_and_rejects_missing_status(self):
        from types import SimpleNamespace
        for r in (SimpleNamespace(isError=False,structuredContent={"result":[1]}),
                  SimpleNamespace(is_error=False,structured_content={"result":[1]})):
            self.assertEqual(evidence.decode(r),[1])
        for r in (SimpleNamespace(is_error=True),SimpleNamespace(structured_content={"result":[1]})):
            with self.assertRaises(evidence.Unavailable):evidence.decode(r)

    def test_new_capability_without_code_changes(self):
        self.assertEqual(
            evidence.arguments(self.op, {"table": "sales"}, self.packet["full_diff"]),
            {"name": "sales"},
        )
        self.assertEqual(
            evidence.catalog(evidence.operations(self.config, self.repo))[0]["name"],
            "metadata",
        )
        self.assertFalse(evidence.operations(self.config, "external/repo"))

    def test_rejects_unrelated_identifiers_and_sql_injection(self):
        for value in ["customers", "sales; drop table sales", "x.y"]:
            with self.assertRaises(evidence.Unavailable):
                evidence.arguments(self.op, {"table": value}, self.packet["full_diff"])

    def test_scope_includes_exact_head_source_but_not_slack_or_history(self):
        packet = {"full_diff":"+ select new_field", "available_head_files":{"m.sql":"from core.sales"},
                  "prior_report":"from core.private", "requested_review_focus":"query core.private"}
        def call(policy, repo, material, req):
            evidence.arguments(self.op, req["arguments"], material)
            return {"ok":True}
        evidence.resolve(self.repo,packet,self.first,self.config,turn=lambda *_:json.dumps(self.status),call=call)
        with self.assertRaises(evidence.Unavailable):
            evidence.resolve(self.repo,packet,self.first.replace("sales","private"),self.config,call=call)

    def test_denies_arbitrary_tool_even_if_requested_by_model(self):
        cmd = self.first.replace("metadata", "delete_table")
        with self.assertRaises(evidence.Unavailable):
            evidence.resolve(
                self.repo, self.packet, cmd, self.config, call=lambda *args: self.fail()
            )

    def test_critical_failure_cannot_be_upgraded_to_approve(self):
        verdict, _ = evidence.resolve(
            self.repo,
            self.packet,
            self.first,
            self.config,
            turn=lambda *a: json.dumps(self.status),
            call=lambda *a: {"ok": False},
        )
        self.assertEqual(verdict["decision"], "INCOMPLETE")

    def test_requires_actual_tool_receipt(self):
        with self.assertRaises(evidence.Unavailable):
            evidence.resolve(
                self.repo, self.packet, json.dumps(self.status), self.config
            )
        verdict, history = evidence.resolve(
            self.repo,
            self.packet,
            self.first,
            self.config,
            turn=lambda *a: json.dumps(self.status),
            call=lambda *a: {"ok": True, "data": {"records": []}},
        )
        self.assertEqual(history[0]["id"], "E1")
        self.assertEqual(verdict["details"], "details")

    def test_projects_only_registered_fields(self):
        self.assertEqual(
            evidence.project(
                {"values": [{"id": 1, "email": "private"}]},
                {"path": ["values"], "fields": {"id": ["id"]}},
            ),
            {"records": [{"id": 1}], "truncated": False},
        )

    def test_metadata_after_hundredth_column_is_preserved(self):
        rows = [{"name": f"column_{i}"} for i in range(130)]
        rows.append({"name": "dw_domain_update_time"})
        result = evidence.project(rows, {
            "fields": {"column": ["name"]}, "result_limit": 2000,
            "require_complete": True,
        })
        self.assertFalse(result["truncated"])
        self.assertEqual(result["records"][-1]["column"], "dw_domain_update_time")

    def test_complete_metadata_overflow_is_not_success(self):
        with self.assertRaises(evidence.Unavailable):
            evidence.project([{"name": "x"}] * 2001, {
                "fields": {"column": ["name"]}, "result_limit": 2000,
                "require_complete": True,
            })

    def test_default_projection_limit_still_bounds_nonmetadata(self):
        result = evidence.project([{"count": 1}] * 101, {"fields": {"count": ["count"]}})
        self.assertEqual(len(result["records"]), 100)
        self.assertTrue(result["truncated"])
