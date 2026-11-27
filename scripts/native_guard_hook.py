#!/usr/bin/env python3
"""Codex PreToolUse policy for the native Slack runtime."""
import json
import os
import re
import shlex
import sys

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
OWNER = "U074Z9G01U6"
COLLEAGUE = "当前请求来自同事，外部写操作（push / merge / dbt run 等）已拦截；请仅提供只读审查、分析或本地验证结果。"
CONFIRM = "外部写操作需 Perry 在当前线程显式确认。请先完成本地修改与验证，在回复末尾列出「回『可以』就执行『<具体命令与目标分支/PR>』」，等待 Perry 回复「可以」后再执行。"
UNVERIFIED = "无法核验本次 Multica run 对应的 Slack 触发消息，已拦截外部写操作。"
PROTECTED = "禁止修改受保护的凭据、运行配置或 LaunchAgents 路径。"
UNKNOWN = "无法核验该工具或命令的操作范围。请使用明确的命令、参数及目标路径。"


def current_trigger():
    # Only the write path imports the CLI, envelope parser and signature verifier.
    import hashlib
    import importlib.util
    from pathlib import Path
    from uuid import UUID
    from slack_intent import verified

    issue, task, workspace = (str(UUID(os.environ[k])) for k in
                              ("MULTICA_ISSUE_ID", "MULTICA_TASK_ID", "MULTICA_WORKSPACE_ID"))
    server = os.environ.get("MULTICA_SERVER_URL") or os.environ["MULTICA_API_URL"]
    spec = importlib.util.spec_from_file_location("native_relay_source", Path(REPO) / "scripts/slack-reply.py")
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    cfg = {"workspaceId": workspace, "serverUrl": server}
    runs = relay.multica_json(cfg, ["issue", "runs", issue])
    run, = [r for r in runs if r["id"] == task and r["issue_id"] == issue and r["workspace_id"] == workspace]
    if run["status"] != "running":
        raise ValueError("inactive run")
    record = relay.multica_json(cfg, ["issue", "get", issue])
    if (record["id"] != issue or record["workspace_id"] != workspace or record["assignee_type"] != "agent"
            or record["assignee_id"] != run["agent_id"]):
        raise ValueError("issue scope")
    source = record["description"]
    root = relay.envelope_from_text(source)["eventPayload"]
    def digest(parts):
        return hashlib.sha256(":".join(parts).encode()).hexdigest()

    scope = digest((workspace, record["project_id"], run["agent_id"]))
    thread = (root["teamId"], root["channelId"], root["threadTs"])
    if source.splitlines()[0] != f"<!-- relay-thread:{scope}:{digest(thread)} -->":
        raise ValueError("issue marker")
    comment = run.get("trigger_comment_id")
    if comment is not None:
        comment = str(UUID(comment))
        rows = relay.multica_json(cfg, ["issue", "comment", "list", issue, "--thread", comment, "--tail", "0"])
        row, = [r for r in rows if r["id"] == comment and r["issue_id"] == issue and not r.get("source_task_id")]
        source = row["content"]
    envelope = relay.envelope_from_text(source)
    event = envelope["eventPayload"]
    if tuple(event[k] for k in ("teamId", "channelId", "threadTs")) != thread or event.get("sourceAppId") or event["mention"]["id"] != os.getenv("RELAY_OWNER_SLACK_USER_ID", OWNER):
        raise ValueError("trigger scope")
    if comment and source.splitlines()[0] != f"<!-- relay-message:{digest((*thread[:2], event['messageTs']))} -->":
        raise ValueError("comment marker")
    key = (Path(REPO) / ".private/runtime/review/key").read_text().strip().encode()
    if not key:
        raise ValueError("missing signing key")
    verified({"event": event, "ticket": envelope["context"]["githubReviewTicket"]}, key)
    return event["senderUserId"], event["text"], bool(comment)


SQL_WRITES = re.compile(r"\b(?:insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|unload|merge|vacuum|call|into|attach|detach|optimize|outfile)\b", re.I)
SQL_TOKENS = re.compile(r"""--[^\r\n]*|/\*(?:(?!/\*|\*/)[\s\S])*\*/|'(?:''|[^'])*'|"(?:""|[^"])*" """.strip())


def read_sql(sql, dialect):
    if not isinstance(sql, str) or not sql.strip():
        return False
    stripped = SQL_TOKENS.sub(" ", sql).strip().removesuffix(";").strip()
    if (not re.match(r"^(?:select|with|explain|show|describe)\b", stripped, re.I)
            or SQL_WRITES.search(stripped) or re.search(r"""[;'"\\]|/\*|\*/|\$\w*\$""", stripped)):
        return False
    try:
        import sqlglot
    except ImportError:
        return True
    import logging
    logger = logging.getLogger("sqlglot")
    was_disabled = logger.disabled
    try:
        # sqlglot otherwise logs full SQL when a dialect uses its Command fallback.
        logger.disabled = True
        from sqlglot import exp
        statements = sqlglot.parse(sql, read=dialect)
        if len(statements) != 1 or statements[0] is None:
            return False
        # Some dialects represent SHOW/EXPLAIN as Command. The lexical check above
        # also covers Command payloads that sqlglot intentionally leaves unparsed.
        return not any(isinstance(node, (exp.DDL, exp.DML, exp.Into)) for node in statements[0].walk())
    except (ValueError, RecursionError, sqlglot.errors.SqlglotError):
        return False
    finally:
        logger.disabled = was_disabled


def protected(path, cwd, ancestors=False):
    path = os.path.expanduser(path.replace("${HOME}", os.path.expanduser("~")).replace("$HOME/", "~/"))
    absolute = os.path.realpath(os.path.join(cwd, path))
    home = os.path.expanduser("~")
    roots = (home + "/.ssh", home + "/.aws", home + "/.codex/auth.json", home + "/Library/LaunchAgents",
             REPO + "/.private", REPO + "/scripts")
    return any(absolute == root or absolute.startswith(root + "/") or
               ancestors and root.startswith(absolute.rstrip("/") + "/") for root in roots)


def push_matches_pr(number, args, cwd):
    """For a PR-scoped push, resolve its single destination before granting the write."""
    import subprocess

    def read(*argv):
        return subprocess.run(argv, cwd=cwd, timeout=5, check=True, capture_output=True, text=True).stdout.strip()

    try:
        if not args:
            modes = subprocess.run(["git", "config", "--get-all", "push.default"], cwd=cwd,
                                   timeout=5, capture_output=True, text=True)
            if modes.returncode not in (0, 1) or modes.stdout.strip() not in ("", "simple", "current", "upstream"):
                return False
        if len(args) > 2 or any(arg.startswith(("-", "+")) or "*" in arg for arg in args):
            return False
        if len(args) == 2:
            remote, ref = args
            if ref.startswith(":") or not re.fullmatch(r"[a-zA-Z0-9._/-]+(?::[a-zA-Z0-9._/-]+)?", ref):
                return False
            branch = ref.split(":")[-1].removeprefix("refs/heads/")
            if branch == "HEAD":
                branch = read("git", "branch", "--show-current")
        elif not args:
            target = read("git", "rev-parse", "--symbolic-full-name", "@{push}")
            if not target.startswith("refs/remotes/"):
                return False
            remote, branch = target.removeprefix("refs/remotes/").split("/", 1)
        else:
            return False
        urls = read("git", "remote", "get-url", "--push", "--all", remote).splitlines()
        pr = json.loads(read("gh", "pr", "view", number, "--json", "number,headRefName,headRepository,headRepositoryOwner"))
        expected = pr["headRepositoryOwner"]["login"] + "/" + pr["headRepository"]["name"]
        return (len(urls) == 1 and urls[0].removesuffix(".git") in
                ("https://github.com/" + expected, "git@github.com:" + expected, "ssh://git@github.com/" + expected)
                and pr["number"] == int(number) and pr["headRefName"] == branch)
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        return False


def confirmed(text, owner, followup, actions):
    body = re.sub(r"^\s*<@" + re.escape(owner) + r"(?:\|[^>\n]+)?>[\s，,:：]*", "", text).strip().rstrip("。！!").strip()
    if followup and re.fullmatch(r"可以|确认(?:执行)?|同意|直接执行|approve|proceed|go ahead|lgtm", body, re.I):
        return True
    scoped = {"直接推": "push", "直接合": "merge"}
    if followup and body in scoped:
        return all(verb == scoped[body] for verb, _, _ in actions)
    # Full imperatives only. Quoted examples, conditions, questions and negations
    # must never authorize writes by containing an affirmative substring.
    if re.search(r"[？?\n`「」“”]|(?:不要|暂不|别|先不|如果|除非|暂缓|是否|能否|能不能|可以吗|先等|不要)", body):
        return False
    verbs = {"push": r"(?:git\s+push|push|推送|推)", "merge": r"(?:gh\s+pr\s+merge|merge|合并|合)"}
    match = re.fullmatch(r"(?:(?:请)?直接\s*)?(" + verbs["push"] + "|" + verbs["merge"] +
                         r")(?:\s*(?:并|然后|and)\s*(" + verbs["merge"] + r"))?\s*"
                         r"((?:PR\s*)?#?\d+|[a-zA-Z][\w./-]*(?:\s+[\w./-]+)?)", body, re.I)
    if not match:
        return False
    named = match.group(3).strip()
    number = re.fullmatch(r"(?:PR\s*)?#?(\d+)", named, re.I)
    for verb, args, cwd in actions:
        if verb not in verbs or not any(re.fullmatch(verbs[verb], token or "", re.I) for token in match.group(1, 2)):
            return False
        if verb == "merge":
            if not number or not args or not (args[0] == number[1] or
                                             re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/" + number[1], args[0])):
                return False
        elif number:
            if not match.group(2) or not push_matches_pr(number[1], args, cwd):
                return False
        elif named.split() not in (args, args[1:] if len(args) == 2 and not args[0].startswith("-") else []):
            return False
    return True


def mcp_policy(name, data):
    _, server, operation = name.split("__", 2)
    if ("slack" in server or operation in {"redshift_drop_table", "redshift_grant_permissions", "clear_task_instance",
                                          "add_inline_policy", "create_data_processing_role", "upload_to_s3"}
            or re.match(r"(?:jira_|confluence_)(?:batch_)?(?:create|update|delete|transition|assign|add|edit|move|copy|set|upload|remove|link|reply)_", operation)):
        return "该 MCP 工具禁止执行写操作。"
    if operation in {"redshift_query", "execute_query", "redshift_explain", "query", "query_fallback"}:
        sql = data.get("sql", data.get("query"))
        if operation == "query_fallback" and sql == "":
            return None
        return None if read_sql(sql, "clickhouse" if "clickhouse" in server else "redshift") else "仅允许单条只读 SQL。"
    if server == "aws_dataprocessing" and operation.startswith("manage_aws_"):
        return None if re.match(r"^(?:get|list|describe|batch-get)-", data.get("operation", "")) else UNKNOWN
    if re.match(r"(?:(?:jira|confluence)_(?:batch_)?)?(?:get|list|search|check|describe|review|aggregate|resolve|analyze)_", operation) or operation in {
        "jira_search", "confluence_search", "connection_check", "redshift_check_table", "run_soql_query",
        "answer_metric", "smoke_check",
    }:
        return None
    return UNKNOWN


def shell_segments(command):
    # Preserve quoted shell arguments while distinguishing actual command boundaries.
    word = r"""(?:'(?:[^']*)'|"(?:\\.|[^"\\])*"|\\.|[^\s;&|<>()'"\\])+"""
    pattern = re.compile(r"[ \t\r]+|\d*>&\d+|\n|&&|\|\||>>|[;&|<>()>]|" + word)
    args = []
    end = 0
    for match in pattern.finditer(command):
        if match.start() != end:
            raise ValueError("invalid shell quoting")
        end = match.end()
        token = match.group()
        if token in (";", "&&", "||", "|", "&", "\n"):
            if args:
                yield args
            args = []
        elif token in ("(", ")"):
            raise ValueError("dynamic shell group")
        elif token.strip() and not re.fullmatch(r"\d*>&\d+", token):
            args.append(shlex.split(token)[0])
    if end != len(command):
        raise ValueError("invalid shell syntax")
    if args:
        yield args


LOCAL_COMMANDS = frozenset("""
    ls pwd cat head tail grep rg sed cut wc sort uniq tr diff echo printf true false test [ stat file
    du dirname basename realpath readlink jq git dbt mkdir touch cp mv chmod chown tee ln install
    truncate rmdir dd python python3 pytest ruff vitest tsc pnpm npm npx wrangler date find which nl
""".split())


def python_snippet(words):
    import ast
    index = words.index("-c")
    if index + 1 >= len(words):
        return False
    try:
        tree = ast.parse(words[index + 1])
    except SyntaxError:
        return False
    # Arbitrary subprocesses and dynamic evaluation cannot be classified from their source.
    allowed = {"print", "len", "str", "int", "float", "bool", "abs", "round", "list", "tuple", "dict",
               "set", "sum", "sorted", "range", "enumerate", "zip", "any", "all", "min", "max",
               "isinstance", "Path", "loads", "dumps", "safe_load", "get", "items", "keys", "values",
               "read", "read_text", "read_bytes", "split", "splitlines", "strip", "lstrip", "rstrip",
               "join", "startswith", "endswith", "lower", "upper", "replace", "format", "isdigit",
               "append", "extend", "glob", "rglob", "exists", "resolve", "expanduser", "is_file",
               "is_dir", "cwd", "Counter", "defaultdict", "search", "match", "findall", "sub",
               "sha256", "hexdigest", "urlparse", "quote", "unquote", "reader", "DictReader",
               "fromtimestamp", "now", "strftime"}
    allowed_modules = ("json", "pathlib", "math", "re", "sys", "collections", "datetime",
                       "csv", "hashlib", "urllib.parse", "yaml")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [node.module] if isinstance(node, ast.ImportFrom) else [n.name for n in node.names]
            if not all(m in allowed_modules for m in modules):
                return False
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "open":
                mode = next((k.value for k in node.keywords if k.arg == "mode"), None)
                if len(node.args) >= 2:
                    mode = node.args[1]
                if mode is None or isinstance(mode, ast.Constant) and mode.value in ("r", "rb", "rt"):
                    continue
            if name not in allowed:
                return False
    return True


def shell_policy(command, cwd, workdir, actions):
    if not isinstance(command, str) or not command.strip() or "`" in command or "$(" in command:
        return UNKNOWN
    for args in shell_segments(command):
        while args and (re.match(r"^[\w]+=", args[0]) or args[0] in ("env", "command", "exec")):
            args = args[1:]
        if not args:
            continue
        if not args[0] or args[0].startswith("-"):
            return UNKNOWN
        name = os.path.basename(args[0]).lower()
        words = args[1:]
        if name in {"sh", "bash", "zsh"} and len(words) == 2 and words[0] in ("-c", "-lc"):
            denied = shell_policy(words[1], cwd, workdir, actions)
            if denied:
                return denied
            continue
        if name == "cd":
            if len(words) != 1 or re.search(r"[\$`*?]", words[0]):
                return UNKNOWN
            cwd = os.path.realpath(os.path.join(cwd, os.path.expanduser(words[0])))
            continue
        for i, word in enumerate(args):
            if word in (">", ">>"):
                if i + 1 >= len(args) or re.search(r"[\$`*?]", args[i+1].replace("$HOME/", "").replace("${HOME}/", "")):
                    return UNKNOWN
                if protected(args[i+1], cwd):
                    return PROTECTED
        if name in {"find", "rg", "sort", "git"}:
            if name == "find" and any(w in words for w in ("-exec", "-execdir", "-delete", "-ok", "-okdir")):
                return UNKNOWN
            if any(w == "--pre" or w.startswith(("--pre=", "--output=")) for w in words):
                return UNKNOWN
        if name in {"rm", "mv", "cp", "tee", "chmod", "chown", "mkdir", "touch", "ln", "install", "truncate", "rmdir", "sed", "perl", "dd"}:
            for word in words:
                if word.startswith("-") or word in (">", ">>"):
                    continue
                if any(c in word.replace("$HOME/", "").replace("${HOME}/", "") for c in "$*?{}"):
                    return UNKNOWN
                if protected(word.removeprefix("of="), cwd, ancestors=name in {"mv", "rm", "rmdir", "chmod", "chown"}):
                    return PROTECTED
        if name in ("bash", "sh", "zsh", "eval", "sudo", "xargs") or name.startswith("$"):
            return UNKNOWN
        if name == "git":
            git_cwd = cwd
            # Git global options may precede the subcommand.
            while words and words[0].startswith("-"):
                if words[0].startswith(("-c", "--config-env", "--exec-path")):
                    return UNKNOWN
                if words[0] == "-C" and len(words) > 1:
                    git_cwd = os.path.realpath(os.path.join(git_cwd, words[1]))
                    words = words[2:]
                elif words[0] in ("--git-dir", "--work-tree"):
                    return UNKNOWN
                else:
                    words = words[1:]
            if words[:1] == ["push"]:
                actions.append(("push", words[1:], git_cwd))
            elif words and words[0] not in "status diff show log branch ls-remote rev-parse rev-list ls-files ls-tree grep blame remote fetch pull clone checkout switch add commit restore stash merge rebase worktree tag describe cat-file diff-tree merge-base shortlog version help".split():
                return UNKNOWN
        elif name == "gh":
            if words[:1] == ["pr"] and words[1:2] in (["merge"], ["close"], ["ready"], ["create"], ["edit"]):
                actions.append((words[1], words[2:], cwd))
            elif words[:1] == ["api"]:
                for i, word in enumerate(words[1:], 1):
                    method = None
                    if word in ("-X", "--method"):
                        method = words[i+1] if i+1 < len(words) else ""
                    elif word.startswith(("-X", "--method=")):
                        method = word.removeprefix("-X").removeprefix("--method=").lstrip("=")
                    if (method is not None and method.upper() != "GET" or word.startswith(("-f", "-F"))
                            or word.split("=")[0] in ("--field", "--raw-field", "--input")):
                        actions.append(("gh api", [], cwd))
                        break
            elif not (
                (words[:1] == ["pr"] and words[1:2] in (["view"], ["diff"], ["checks"], ["review"], ["list"], ["status"]))
                or (words[:1] == ["run"] and words[1:2] in (["view"], ["list"]))
                or (words[:1] == ["issue"] and words[1:2] in (["view"], ["list"], ["status"]))
                or (words[:1] == ["repo"] and words[1:2] in (["view"], ["list"]))
                or (words[:1] == ["auth"] and words[1:2] == ["status"])
                or words[:1] == ["search"]
            ):
                return UNKNOWN
        elif name == "dbt":
            if any(word in ("run", "build", "run-operation", "seed", "snapshot", "retry", "clone") for word in words):
                actions.append(("dbt", words, cwd))
        elif name == "launchctl" or name == "wrangler" and "deploy" in words and "--dry-run" not in words:
            actions.append((name, words, cwd))
        elif name in ("pnpm", "npx", "npm"):
            nested = words[1:] if words[:1] == ["exec"] else words
            if nested[:1] == ["--"]:
                nested = nested[1:]
            if nested[:1] in (["wrangler"], ["git"], ["gh"], ["dbt"]):
                denied = shell_policy(shlex.join(nested), cwd, workdir, actions)
                if denied:
                    return denied
            elif not nested or nested[0] not in {"test", "lint", "tsc", "vitest", "build:cf"}:
                return UNKNOWN
        elif name in ("curl", "wget"):
            # Requests carrying a body and file uploads need an authenticated owner.
            method = next((words[i + 1].upper() for i, w in enumerate(words[:-1]) if w in ("-X", "--request")), "GET")
            if name == "wget" or method not in ("GET", "HEAD") or any(
                    w.startswith(("-d", "-F", "-T", "--data", "--form", "--upload-file", "--json")) or
                    w.startswith(("-X", "--request=")) and w not in ("-X", "-XGET", "-XHEAD", "--request=GET", "--request=HEAD")
                    for w in words):
                actions.append(("http", words, cwd))
            elif any(w in ("-o", "--output", "-K", "--config") or w.startswith(("--output=", "-o")) for w in words):
                return UNKNOWN
        elif name == "rm":
            flags = "".join(w for w in words if w.startswith("-"))
            if ("r" in flags.lower() or "--recursive" in flags) and ("f" in flags or "--force" in flags):
                for w in words:
                    if w.startswith("-"):
                        continue
                    path = os.path.realpath(os.path.join(cwd, os.path.expanduser(w)))
                    roots = (os.path.realpath("/tmp"), workdir)
                    if re.search(r"[$*?{}]", w) or not any(path.startswith(root.rstrip("/") + "/") for root in roots):
                        actions.append(("rm", words, cwd))
                        break
        elif name in ("python", "python3") and "-c" in words:
            # A project test suite runs under the native filesystem sandbox. For
            # inline snippets, admit common read expressions.
            if not python_snippet(words):
                return UNKNOWN
        elif name not in LOCAL_COMMANDS:
            return UNKNOWN
    return None


def decide(event):
    name = event["tool_name"].lower()
    data = event["tool_input"]
    if not isinstance(data, dict):
        return UNKNOWN
    if name.startswith("mcp__"):
        return mcp_policy(name, data)
    cwd = event["cwd"]
    if not os.path.isabs(cwd):
        return UNKNOWN
    actions = []
    if name in ("exec_command", "shell_command", "shell", "bash"):
        command = data.get("command", data.get("cmd"))
        if isinstance(command, list):
            command = shlex.join(command)
        if data.get("tty"):
            return UNKNOWN
        denied = shell_policy(command, os.path.join(cwd, data.get("workdir", "")), os.path.realpath(cwd), actions)
        if denied:
            return denied
    elif name in ("apply_patch", "edit", "write", "multiedit", "write_file", "edit_file"):
        if name == "apply_patch":
            paths = re.findall(r"^\*\*\* (?:Add File|Delete File|Update File|Move to): (.+)$",
                               data.get("command", data.get("patch", "")), re.M)
        else:
            paths = [data.get("file_path", data.get("path"))]
        if not paths or not all(isinstance(p, str) and p for p in paths):
            return UNKNOWN
        if any(protected(p, cwd) for p in paths):
            return PROTECTED
    elif name == "write_stdin" and not data.get("chars"):
        return None
    elif name not in ("read", "read_file", "glob", "grep", "view_image", "update_plan"):
        return UNKNOWN
    if actions:
        try:
            sender, body, followup = current_trigger()
        except Exception:
            return UNVERIFIED
        owner = os.environ.get("RELAY_OWNER_SLACK_USER_ID", OWNER)
        if sender != owner:
            return COLLEAGUE
        if not confirmed(body, owner, followup, actions):
            return CONFIRM
    return None


if __name__ == "__main__":
    try:
        reason = decide(json.load(sys.stdin))
    except Exception:
        reason = UNKNOWN
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                      "permissionDecision": "deny" if reason else "allow",
                      "permissionDecisionReason": reason or "操作在允许范围内。"}}, ensure_ascii=False))
