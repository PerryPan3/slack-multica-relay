#!/usr/bin/env python3
"""Generate the isolated Multica Codex configuration (Python 3.11+)."""
import argparse
import json
import os
from pathlib import Path
import shlex
import tempfile
import tomllib

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = REPO / ".private/runtime/native-codex-home"
PYTHON = Path("/Users/perrypan/dbt_dw/moego-dbt-dw/tools/moego-data-agent/.venv/bin/python")
SERVERS = (
    "redshift", "redshift-mcp-server", "mwaa-airflow", "mwaa-airflow-t2",
    "clickhouse-prod", "clickhouse-t2", "mcp-atlassian", "datadog",
    "salesforce", "aws-dataprocessing", "moego-data-agent",
)


def toml_value(value):
    """MCP configuration values are TOML scalars, arrays or inline tables."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{toml_value(k)} = {toml_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_value, value)) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("\x7f", r"\u007f")


def render(source, trust, fallback_mcp_servers=None):
    command = shlex.join([str(PYTHON), str(REPO / "scripts/native_guard_hook.py")])
    lines = ['model = "gpt-6.2-astra"', 'model_reasoning_effort = "high"',
             'service_tier = "default"', 'approval_policy = "never"', 'web_search = "disabled"',
             'default_permissions = "native-relay"',
             '', '[permissions.native-relay]', 'extends = ":workspace"',
             '', '[permissions.native-relay.filesystem]',
             *(toml_value(str(path)) + ' = "read"' for path in (
                 Path.home() / ".ssh", Path.home() / ".aws", Path.home() / ".codex/auth.json",
                 Path.home() / "Library/LaunchAgents", REPO / ".private", REPO / "scripts")),
             # The host hook verifies signatures. Shells and their children do not need this key.
             toml_value(str(REPO / ".private/runtime/review/key")) + ' = "deny"',
             '', '[permissions.native-relay.network]', 'enabled = true',
             '', '[permissions.native-relay.network.domains]',
             '"github.com" = "allow"', '"api.github.com" = "allow"',
             '"raw.githubusercontent.com" = "allow"', '"objects.githubusercontent.com" = "allow"',
             '"multica.devops.moego.dev" = "allow"',
             '', '[features]', 'network_proxy = true', 'shell_tool = true', 'multi_agent_v2 = false',
             '', '[agents]', 'enabled = false',
             '', '[analytics]', 'enabled = false',
             '', '[[hooks.PreToolUse]]', '[[hooks.PreToolUse.hooks]]', 'type = "command"',
             'command = ' + toml_value(command)]
    if trust:
        lines.extend(["", "[hooks.state]", *(f"{toml_value(k)} = {toml_value(v)}" for k, v in trust.items())])
    servers = source.get("mcp_servers", {})
    fallback_servers = fallback_mcp_servers or {}
    for name in SERVERS:
        if name not in servers:
            continue
        server = dict(servers[name])
        env = dict(server.get("env", {}))
        env_vars = list(server.get("env_vars", []))
        if env_vars:
            fallback_env = fallback_servers.get(name, {}).get("env", {}) if isinstance(fallback_servers.get(name), dict) else {}
            remaining_env_vars = []
            for key in env_vars:
                if key in env:
                    continue
                value = fallback_env.get(key)
                if isinstance(value, str) and value:
                    env[key] = value
                else:
                    remaining_env_vars.append(key)
            if remaining_env_vars:
                server["env_vars"] = remaining_env_vars
            else:
                server.pop("env_vars", None)
        if name in ("redshift", "redshift-mcp-server"):
            env["REDSHIFT_ALLOW_WRITE"] = "false"
        if env:
            server["env"] = env
        lines.extend(["", f'[mcp_servers."{name}"]'])
        lines.extend(f"{toml_value(k)} = {toml_value(v)}" for k, v in server.items())
    return "\n".join(lines) + "\n"


def generate(source, target, fallback_claude_json=None):
    source = Path(source).expanduser().resolve(strict=True)
    target = Path(target).expanduser()
    if target.is_symlink() or target.resolve() in (source.parent, *source.parents):
        raise ValueError("Select a separate, private target directory")
    config = target / "config.toml"
    auth = target / "auth.json"
    origin_auth = source.with_name("auth.json")
    if config.is_symlink() or config.exists() and config.samefile(source):
        raise ValueError("Target config must be independent of the source")
    if auth.is_symlink() or auth.exists():
        if not auth.is_symlink() or auth.resolve() != origin_auth.resolve():
            raise ValueError("Existing auth.json does not link to the source identity")
    current = config.read_text() if config.exists() else ""
    # Codex owns the approval hashes in the target home. Keep its decisions on regeneration.
    trust = tomllib.loads(current).get("hooks", {}).get("state", {})
    fallback_mcp_servers = None
    if fallback_claude_json is not None:
        fallback_path = Path(fallback_claude_json).expanduser()
        if fallback_path.is_file():
            loaded = json.loads(fallback_path.read_text())
            if isinstance(loaded, dict) and isinstance(loaded.get("mcpServers"), dict):
                fallback_mcp_servers = loaded["mcpServers"]
    output = render(tomllib.loads(source.read_text()), trust, fallback_mcp_servers=fallback_mcp_servers)
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    target.chmod(0o700)
    if not auth.is_symlink():
        try:
            auth.symlink_to(origin_auth)
        except FileExistsError:
            if not auth.is_symlink() or auth.resolve() != origin_auth.resolve():
                raise
    if current == output:
        config.chmod(0o600)
        return
    with tempfile.NamedTemporaryFile(mode="w", dir=target, delete=False, encoding="utf-8") as handle:
        temporary = Path(handle.name)
        try:
            handle.write(output)
            handle.flush()
            os.replace(temporary, config)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--fallback-claude-json", type=Path, default=Path.home() / ".claude.json")
    args = parser.parse_args()
    generate(args.source, args.target, fallback_claude_json=args.fallback_claude_json)
