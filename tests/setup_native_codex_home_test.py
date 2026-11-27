from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_native_codex_home as setup


class NativeCodexHomeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "user/config.toml"
        self.source.parent.mkdir()
        self.target = self.root / "native"
        self.source.write_text('model="old"\n[hooks.state]\nuser="unused"\n' + "\n".join(
            f'[mcp_servers."{name}"]\ncommand="srv"\nargs=["a b", "🦊"]\n'
            f'env={{ VALUE="{name}-secret", REDSHIFT_ALLOW_WRITE="true" }}'
            for name in (*setup.SERVERS, "slack", "chrome-devtools", "sana", "magpie-artifacts", "node_repl")))
        self.source.with_name("auth.json").write_text("fixture")

    def test_isolation_permissions_and_repeatability(self):
        before = self.source.read_bytes()
        setup.generate(self.source, self.target)
        file = self.target / "config.toml"
        cfg = tomllib.loads(file.read_text())
        self.assertEqual(set(cfg["mcp_servers"]), set(setup.SERVERS))
        for name in ("slack", "chrome-devtools", "sana", "magpie-artifacts", "node_repl"):
            self.assertNotIn(name + "-secret", file.read_text())
        self.assertNotIn("user=", file.read_text())
        for name in ("redshift", "redshift-mcp-server"):
            self.assertEqual(cfg["mcp_servers"][name]["env"], {"VALUE": name + "-secret", "REDSHIFT_ALLOW_WRITE": "false"})
        self.assertEqual(cfg["features"], {"shell_tool": True, "multi_agent_v2": False, "network_proxy": True})
        self.assertEqual(cfg["agents"], {"enabled": False})
        self.assertEqual(
            cfg["permissions"][cfg["default_permissions"]]["network"]["domains"],
            {
                "github.com": "allow",
                "api.github.com": "allow",
                "raw.githubusercontent.com": "allow",
                "objects.githubusercontent.com": "allow",
                "multica.devops.moego.dev": "allow",
            },
        )
        self.assertEqual([cfg[k] for k in ("model", "model_reasoning_effort", "service_tier", "approval_policy", "web_search")],
                         ["gpt-6.2-astra", "high", "default", "never", "disabled"])
        self.assertFalse(cfg["analytics"]["enabled"])
        filesystem = cfg["permissions"][cfg["default_permissions"]]["filesystem"]
        self.assertEqual(filesystem[str(Path.home() / ".ssh")], "read")
        self.assertEqual(filesystem[str(setup.REPO / ".private")], "read")
        hook, = cfg["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(hook, {"type": "command", "command": f"{setup.PYTHON} {setup.REPO}/scripts/native_guard_hook.py"})
        self.assertEqual((self.target / "auth.json").readlink(), self.source.with_name("auth.json").resolve())
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o700)
        mtime = file.stat().st_mtime_ns
        setup.generate(self.source, self.target)
        self.assertEqual(file.stat().st_mtime_ns, mtime)
        self.assertEqual(self.source.read_bytes(), before)

    def test_keeps_target_hook_trust_only(self):
        setup.generate(self.source, self.target)
        file = self.target / "config.toml"
        with file.open("a") as handle:
            handle.write('\n[hooks.state."local-hook"]\nenabled=true\n')
        setup.generate(self.source, self.target)
        self.assertEqual(tomllib.loads(file.read_text())["hooks"]["state"], {"local-hook": {"enabled": True}})

    def test_resolves_missing_env_vars_from_fallback_claude_json(self):
        self.source.write_text(
            '[mcp_servers."datadog"]\ncommand="dd"\nenv_vars=["DD_API_KEY", "DD_APP_KEY"]\nenv={ DD_SITE="datadoghq.com" }\n'
            '[mcp_servers."slack"]\ncommand="sl"\nenv_vars=["SLACK_TOKEN"]\n'
        )
        claude = self.root / "claude.json"
        claude.write_text(
            '{"mcpServers": {"datadog": {"env": {"DD_API_KEY": "k1", "DD_APP_KEY": "k2"}}, '
            '"slack": {"env": {"SLACK_TOKEN": "should-not-copy"}}}}'
        )
        setup.generate(self.source, self.target, fallback_claude_json=claude)
        cfg = tomllib.loads((self.target / "config.toml").read_text())
        self.assertEqual(cfg["mcp_servers"]["datadog"]["env"], {"DD_SITE": "datadoghq.com", "DD_API_KEY": "k1", "DD_APP_KEY": "k2"})
        self.assertNotIn("env_vars", cfg["mcp_servers"]["datadog"])
        self.assertNotIn("slack", cfg["mcp_servers"])
        self.assertNotIn("should-not-copy", (self.target / "config.toml").read_text())

    def test_rejects_source_overwrite_and_conflicting_identity(self):
        for target in (self.source.parent, self.root):
            with self.assertRaises(ValueError):
                setup.generate(self.source, target)
        self.target.mkdir()
        (self.target / "config.toml").symlink_to(self.source)
        with self.assertRaises(ValueError):
            setup.generate(self.source, self.target)
        (self.target / "config.toml").unlink()
        (self.target / "auth.json").write_text("other")
        with self.assertRaises(ValueError):
            setup.generate(self.source, self.target)
        self.assertEqual((self.target / "auth.json").read_text(), "other")
