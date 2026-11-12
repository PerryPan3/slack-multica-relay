import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_standards import ReviewStandards
from review_materials import blob_digest


class StandardsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = "example/repo/pull/1"
        self.pr = {"base": {"sha": "b" * 40}, "head": {"sha": "a" * 40}}
        self.files = {"CLAUDE.md": "Every changed function needs a documented contract.",
                      "code.py": "def changed_function(): return 42"}
        self.policy = {"documents": ["CLAUDE.md"], "manual_rules": {"contract": "Check function contracts"}}
        self.save_policy()
        self.gate = ReviewStandards(self.api, self.root)
        self.packet = {"file_manifest": [{"filename": "code.py", "status": "modified"}]}

    def save_policy(self):
        (self.root / "standards.json").write_text(json.dumps({"default": self.policy, "repositories": {}}))

    def api(self, path):
        if "/git/trees/" in path:
            return {"tree": [{"path": p, "sha": blob_digest(t.encode()), "size": len(t.encode()),
                              "type": "blob", "mode": "100644"} for p, t in self.files.items()]}
        sha = path.rsplit("/", 1)[-1]
        text = next(t for t in self.files.values() if blob_digest(t.encode()) == sha)
        return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}

    def assessments(self):
        return [{"rule_id": "contract", "status": "pass",
                 "reason": "The changed function returns the documented constant and has no side effects.",
                 "evidence": [{"path": p, "quote": t} for p, t in self.files.items()]}]

    def evaluate(self, entries=None, head=None):
        return self.gate.evaluate(self.target, ["b" * 40, head or "a" * 40], entries)

    def test_missing_assessment_and_rule_quote_block_approval(self):
        self.gate.prepare(self.target, self.pr, self.packet)
        self.assertFalse(self.evaluate()["ok"])
        entries = self.assessments()
        entries[0]["evidence"] = [entries[0]["evidence"][1]]
        self.assertFalse(self.evaluate(entries)["ok"])
        self.assertTrue(self.evaluate(self.assessments())["ok"])

    def test_stale_revision_and_policy_are_rejected(self):
        self.gate.prepare(self.target, self.pr, self.packet)
        self.assertFalse(self.evaluate(self.assessments(), head="c" * 40)["ok"])
        self.policy["manual_rules"]["new_rule"] = "Additional requirement"
        self.save_policy()
        self.assertFalse(self.evaluate(self.assessments())["ok"])

    def test_assessments_must_cover_each_required_implementation(self):
        self.files["other.py"] = "def another_function(): return 43"
        self.policy["manual_rules"]["contract"] = {"instruction":"Check both implementations", "paths":["code.py","other.py"]}
        self.save_policy()
        self.packet["file_manifest"].append({"filename":"other.py","status":"modified"})
        self.gate.prepare(self.target, self.pr, self.packet)
        entries = self.assessments()
        entries[0]["evidence"] = [e for e in entries[0]["evidence"] if e["path"] != "other.py"]
        self.assertFalse(self.evaluate(entries)["ok"])
        self.assertTrue(self.evaluate(self.assessments())["ok"])

    def test_missing_policy_and_rule_document_fail_closed(self):
        (self.root / "standards.json").unlink()
        self.assertEqual(self.gate.prepare(self.target, self.pr, self.packet)["status"], "incomplete")
        self.assertFalse(self.evaluate(self.assessments())["ok"])
        self.save_policy()
        del self.files["CLAUDE.md"]
        self.assertEqual(self.gate.prepare(self.target, self.pr, self.packet)["status"], "incomplete")

    def test_failed_forged_duplicate_or_unexplained_assessments_are_rejected(self):
        self.gate.prepare(self.target, self.pr, self.packet)
        for mutation in (lambda a: a[0].update(status="fail"),
                         lambda a: a[0]["evidence"][0].update(quote="invented rule text"),
                         lambda a: a.append(a[0]),
                         lambda a: a[0].update(status="not_applicable", reason="n/a")):
            entries = self.assessments()
            mutation(entries)
            self.assertFalse(self.evaluate(entries)["ok"])

    def test_static_failure_cannot_be_waived_by_model(self):
        checker = ("def input_paths(changed, paths): return []\n"
                   "def affected_models(base, head, changed, paths): return []\n"
                   "def check_change(base, head, changed): return "
                   "{'status':'fail','findings':[{'rule':'naming'}], 'models':[], 'existing_findings':0, 'manual_rules':{}}\n")
        raw = checker.encode()
        (self.root / "checker.py").write_bytes(raw)
        self.policy["checker"] = {"file": "checker.py", "sha256": hashlib.sha256(raw).hexdigest()}
        self.save_policy()
        self.assertEqual(self.gate.prepare(self.target, self.pr, self.packet)["status"], "fail")
        self.assertFalse(self.evaluate(self.assessments())["ok"])
        (self.root / "checker.py").write_text("raise RuntimeError('untrusted code')")
        result = self.gate.prepare(self.target, self.pr, self.packet)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("digest mismatch", result["error"])

    def test_partial_tree_and_blob_mismatch_are_incomplete(self):
        original = self.api
        self.gate.api = lambda path: {"truncated": True, "tree": []}
        self.assertEqual(self.gate.prepare(self.target, self.pr, self.packet)["status"], "incomplete")
        self.gate.api = lambda path: original(path) if "/trees/" in path else {"encoding":"base64","content":"YmFk"}
        self.assertEqual(self.gate.prepare(self.target, self.pr, self.packet)["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
