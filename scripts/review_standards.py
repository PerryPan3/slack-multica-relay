"""Host-owned approval requirements. Repository bytes are data, never executables."""
import base64
import hashlib
import json
from pathlib import Path
import posixpath
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
            def fetch_raw_blob(path, row, max_size=1048576):
                if row.get("size", max_size + 1) > max_size:
                    raise ValueError(f"Cannot safely read {path}")
                data = self.api(f"repos/{repo}/git/blobs/{row['sha']}")
                if data.get("encoding") != "base64":
                    raise ValueError(f"Expected text blob: {path}")
                raw = base64.b64decode(data["content"])
                if len(raw) > max_size or blob_digest(raw) != row["sha"]:
                    raise ValueError(f"Unverified blob: {path}")
                return raw

            def resolve_blob(path, row, paths, depth=0):
                if row.get("mode") == "120000":
                    if depth >= 5:
                        raise ValueError(f"Symlink depth exceeded: {path}")
                    target = fetch_raw_blob(path, row, max_size=4096).decode("utf-8").strip()
                    if not target or target.startswith("/") or "\\" in target:
                        raise ValueError(f"Cannot safely read {path}")
                    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                    if resolved in ("", ".") or resolved == ".." or resolved.startswith("../"):
                        raise ValueError(f"Cannot safely read {path}")
                    next_row = paths.get(resolved)
                    if next_row is None:
                        raise ValueError(f"Broken symlink: {path}")
                    return resolve_blob(resolved, next_row, paths, depth + 1)
                if row["sha"] not in blobs:
                    raw = fetch_raw_blob(path, row, max_size=1048576)
                    text = raw.decode("utf-8")
                    if "\0" in text:
                        raise UnicodeError("Binary blob")
                    blobs[row["sha"]] = text
                return blobs[row["sha"]], row["sha"], path

            def read_blob(path, row, paths):
                text, _sha, _resolved = resolve_blob(path, row, paths)
                return text

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
                checker_required = set(checker.input_paths(changed, paths) if checker else [])
                checker_required = {p for p in checker_required if not p.endswith(".sql") or p in changed}
                strict_paths = checker_required | documents
                optional_changed = [p for p in sorted(set(changed)) if p in paths and p not in strict_paths]
                files = {}
                budget = 8 * 1024 * 1024
                for path in sorted(strict_paths):
                    row = paths.get(path)
                    if row is None:
                        if path in documents:
                            raise ValueError(f"Required rule document missing at {side}: {path}")
                        continue
                    text, blob_sha, resolved_path = resolve_blob(path, row, paths)
                    files[path] = text
                    budget -= len(text.encode())
                    if budget < 0:
                        raise ValueError("Standards evidence exceeds the bounded reader capacity")
                    if path in documents:
                        doc = {"side": side, "path": path, "commit": sha,
                               "blob": blob_sha, "text": text}
                        if resolved_path != path:
                            doc["symlink_target"] = resolved_path
                        state["documents"].append(doc)
                for path in optional_changed:
                    row = paths[path]
                    if row.get("size", 1048577) > 1048576 or row.get("mode") == "120000":
                        continue
                    try:
                        text = read_blob(path, row, paths)
                    except UnicodeError:
                        # Binary changed assets (e.g. images) are inspected via review_artifact.
                        continue
                    size = len(text.encode())
                    if budget - size < 0:
                        continue
                    files[path] = text
                    budget -= size
                snapshots.append(files)
            if checker:
                selected = checker.affected_models(*snapshots, changed, set(trees[0]) | set(trees[1]))
                for paths, files in zip(trees, snapshots):
                    for path in selected:
                        if path in paths and path not in files:
                            files[path] = read_blob(path, paths[path], paths)
            state["files"] = snapshots[1]
            if checker:
                result = checker.check_change(*snapshots, changed)
                state.update({k: result[k] for k in ("status", "findings", "models", "existing_findings")})
                state["manual_rules"] = {**state["manual_rules"], **result["manual_rules"]}
                # Only a trusted checker can classify a limitation as resolvable.
                # Missing inputs, parser failures and confirmed violations stay blocking.
                for finding in state["findings"]:
                    if self.resolvable(finding):
                        ident = "static_resolution:" + hashlib.sha256(
                            json.dumps(finding, sort_keys=True).encode()).hexdigest()[:16]
                        finding["resolution_rule"] = ident
                        state["manual_rules"][ident] = {
                            "instruction": "Resolve this tool limitation with a concrete code review and any necessary compiled, CI or data evidence. Explain what the tool could not check and how the evidence establishes the actual rule. Cite pinned-head rules, the affected implementation and relevant schema/macros. Do not claim unperformed checks or waive an explicit repository requirement.",
                            "paths": [finding["path"]], "limitation": finding["detail"]}
            else:
                state["status"] = "pass"
        except Exception as error:
            state["status"] = "incomplete"
            state["error"] = f"{type(error).__name__}: {str(error)[:500]}"
        return self.public(state)

    @staticmethod
    def resolvable(finding):
        return (isinstance(finding, dict) and finding.get("incomplete") is True
                and finding.get("code") == "unsupported_template"
                and finding.get("rule") == "static-analysis"
                and isinstance(finding.get("path"), str) and bool(finding["path"])
                and isinstance(finding.get("detail"), str))

    @staticmethod
    def public(state):
        result = {k: v for k, v in state.items() if k not in ("files", "changed")}
        heads = {(d["path"], d["blob"]) for d in state["documents"] if d["side"] == "head"}
        seen_head_blobs = {}
        documents = []
        for doc in state["documents"]:
            if doc["side"] == "base" and (doc["path"], doc["blob"]) in heads:
                doc = {k: v for k, v in doc.items() if k != "text"}
                doc["same_as_head"] = True
            elif doc["side"] == "head" and "text" in doc:
                prior = seen_head_blobs.get(doc["blob"])
                if prior is not None:
                    doc = {k: v for k, v in doc.items() if k != "text"}
                    doc["same_as_path"] = prior
                else:
                    seen_head_blobs[doc["blob"]] = doc["path"]
            documents.append(doc)
        result["documents"] = documents
        return result

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
        resolvable = (state["status"] == "incomplete" and not state.get("error")
                      and bool(state["findings"])
                      and all(self.resolvable(f) and f.get("resolution_rule") in state["manual_rules"]
                              for f in state["findings"]))
        if state["status"] != "pass" and not resolvable:
            errors.append("Static check found a violation or an unresolved input/checker failure")
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
            if ident.startswith("static_resolution:") and item.get("status") != "pass":
                errors.append(f"{ident}: the tool limitation needs an affirmative evidence-based resolution")
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
            loaded_changed = set(state["changed"]) & set(state["files"])
            if loaded_changed and not matched & loaded_changed:
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
                "resolution": "evidence_review" if resolvable and not errors else None,
                "findings": state["findings"], "required_rules": state["manual_rules"],
                "base_head": revision, "policy_digest": state["policy_digest"],
                "checker_digest": state["checker_digest"]}
