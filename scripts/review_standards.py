"""Host-owned approval requirements. Repository bytes are data, never executables."""
import base64
import hashlib
import json
from pathlib import Path
import types

from review_materials import blob_digest


class ReviewStandards:
    def __init__(self, api, directory):
        self.api = api
        self.directory = Path(directory)
        self.reviews = {}

    def prepare(self, target, pr, packet):
        revision = [pr["base"]["sha"], pr["head"]["sha"]]
        state = {"revision": revision, "status": "incomplete", "findings": [],
                 "documents": [], "manual_rules": {}, "files": {}, "changed": [],
                 "policy_digest": None, "checker_digest": None}
        self.reviews[target] = state
        try:
            policy_bytes = (self.directory / "standards.json").read_bytes()
            state["policy_digest"] = hashlib.sha256(policy_bytes).hexdigest()
            registry = json.loads(policy_bytes)
            repo = target.split("/pull/")[0]
            policy = registry["repositories"].get(repo.lower(), registry["default"])
            if not isinstance(policy, dict):
                raise ValueError("Missing repository standards policy")
            state["manual_rules"] = policy["manual_rules"]
            changed = [f["filename"] for f in packet["file_manifest"]]
            state["changed"] = changed
            checker = None
            if policy.get("checker"):
                entry = policy["checker"]
                path = (self.directory / entry["file"]).resolve()
                path.relative_to(self.directory.resolve())
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest != entry["sha256"]:
                    raise ValueError("Trusted checker digest mismatch")
                state["checker_digest"] = digest
                checker = types.ModuleType("trusted_review_contracts")
                exec(compile(raw, str(path), "exec"), checker.__dict__)
            snapshots = []
            trees = []
            blobs = {}
            def read_blob(path, row):
                if row.get("mode") == "120000" or row.get("size", 1048577) > 1048576:
                    raise ValueError(f"Cannot safely read {path}")
                if row["sha"] not in blobs:
                    data = self.api(f"repos/{repo}/git/blobs/{row['sha']}")
                    if data.get("encoding") != "base64":
                        raise ValueError(f"Expected text blob: {path}")
                    raw = base64.b64decode(data["content"])
                    if len(raw) > 1048576 or blob_digest(raw) != row["sha"]:
                        raise ValueError(f"Unverified blob: {path}")
                    blobs[row["sha"]] = raw.decode("utf-8")
                return blobs[row["sha"]]
            for side, sha in zip(("base", "head"), revision):
                tree = self.api(f"repos/{repo}/git/trees/{sha}?recursive=1")
                if tree.get("truncated"):
                    raise ValueError("Repository tree is incomplete")
                paths = {r["path"]: r for r in tree["tree"] if r["type"] == "blob"}
                trees.append(paths)
                documents = set(policy.get("documents", []))
                # Include directory rules for every changed path, including overrides.
                for name in changed:
                    for parent in [Path("."), *Path(name).parents]:
                        prefix = "" if str(parent) == "." else str(parent) + "/"
                        documents.update(prefix + f for f in ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md")
                                         if prefix + f in paths)
                required = set(checker.input_paths(changed, paths) if checker else [])
                required = {p for p in required if not p.endswith(".sql") or p in changed}
                required.update(p for p in changed if p in paths and p.endswith((".sql", ".py", ".ts", ".yml", ".yaml", ".md")))
                required.update(documents)
                files = {}
                budget = 8 * 1024 * 1024
                for path in sorted(required):
                    row = paths.get(path)
                    if row is None:
                        if path in documents:
                            raise ValueError(f"Required rule document missing at {side}: {path}")
                        continue
                    files[path] = read_blob(path, row)
                    budget -= len(files[path].encode())
                    if budget < 0:
                        raise ValueError("Standards evidence exceeds the bounded reader capacity")
                    if path in documents:
                        state["documents"].append({"side": side, "path": path, "commit": sha,
                                                   "blob": row["sha"], "text": files[path]})
                snapshots.append(files)
            if checker:
                selected = checker.affected_models(*snapshots, changed, set(trees[0]) | set(trees[1]))
                for paths, files in zip(trees, snapshots):
                    for path in selected:
                        if path in paths and path not in files:
                            files[path] = read_blob(path, paths[path])
            state["files"] = snapshots[1]
            if checker:
                result = checker.check_change(*snapshots, changed)
                state.update({k: result[k] for k in ("status", "findings", "models", "existing_findings")})
                state["manual_rules"] = {**state["manual_rules"], **result["manual_rules"]}
            else:
                state["status"] = "pass"
        except Exception as error:
            state["status"] = "incomplete"
            state["error"] = f"{type(error).__name__}: {str(error)[:500]}"
        return self.public(state)

    @staticmethod
    def public(state):
        return {k: v for k, v in state.items() if k not in ("files", "changed")}

    def evaluate(self, target, revision, assessments=None):
        state = self.reviews.get(target)
        errors = []
        if not state or state["revision"] != revision:
            return {"ok": False, "error": "Standards were not checked for this base/head. Read review_context again."}
        try:
            current = hashlib.sha256((self.directory / "standards.json").read_bytes()).hexdigest()
            if current != state["policy_digest"]:
                errors.append("Standards policy changed; reread review_context")
        except OSError:
            errors.append("Standards policy is unavailable")
        if state["status"] != "pass":
            errors.append("Static standards check failed or is incomplete")
        entries = assessments if isinstance(assessments, list) else []
        by_id = {}
        rule_paths = {d["path"] for d in state["documents"]}
        for item in entries:
            if not isinstance(item, dict):
                errors.append("Invalid standards assessment")
                continue
            ident = item.get("rule_id")
            if not isinstance(ident, str) or ident not in state["manual_rules"] or ident in by_id:
                errors.append("Unknown or duplicate standards rule")
                continue
            by_id[ident] = item
            if item.get("status") not in ("pass", "not_applicable"):
                errors.append(f"{ident}: unresolved or failed")
            if not isinstance(item.get("reason"), str) or len(item["reason"].strip()) < 20:
                errors.append(f"{ident}: explain the actual check or why it is inapplicable")
            references = item.get("evidence", [])
            matched = set()
            if not isinstance(references, list):
                references = []
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                path, quote = reference.get("path"), reference.get("quote")
                if (isinstance(path, str) and isinstance(quote, str) and len(quote.strip()) >= 12
                        and quote in state["files"].get(path, "")):
                    matched.add(path)
                else:
                    errors.append(f"{ident}: evidence is not an exact quote from the pinned head")
            if not matched & rule_paths:
                errors.append(f"{ident}: cite the applicable rule document")
            if not matched & set(state["changed"]):
                errors.append(f"{ident}: cite the changed implementation")
            requirement = state["manual_rules"][ident]
            if isinstance(requirement, dict):
                for path in requirement.get("paths", []):
                    if path not in matched:
                        errors.append(f"{ident}: missing implementation evidence for {path}")
        for ident in state["manual_rules"]:
            if ident not in by_id:
                errors.append(f"Missing mandatory assessment: {ident}")
        return {"ok": not errors, "errors": errors, "static_status": state["status"],
                "findings": state["findings"], "required_rules": state["manual_rules"],
                "base_head": revision, "policy_digest": state["policy_digest"],
                "checker_digest": state["checker_digest"]}
