"""feature-rename-mcp 方案 §8 第 3 条：mcp 以账号环境运行 claude mcp，且与启动命令的环境一致。"""

import json
import os
import sys

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

RELEVANT = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "HTTPS_PROXY", "HTTP_PROXY",
            "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "EXTRA_VAR")
PARENT = {"CLAUDE_CONFIG_DIR": "/parent", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "x", "ANTHROPIC_API_KEY": "k",
          "ALL_PROXY": "http://parent:1", "NO_PROXY": "10.0.0.0/8", "HTTPS_PROXY": "http://parent:2"}


class McpTest(CliTestCase):
    def read_fake(self):
        with open(self.fake_out + ".args") as handle:
            args = handle.read().split("\n")[:-1]
        env = {}
        with open(self.fake_out + ".env") as handle:
            for line in handle.read().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    env[key] = value
        return args, env

    def relevant(self, env):
        return {key: env.get(key) for key in RELEVANT}

    def compare_with_launcher(self, name):
        result = self.run_cli("mcp", name, "list", env=PARENT)
        self.assertEqual(result.code, 0, result)
        args, mcp_env = self.read_fake()
        self.assertEqual(args, ["mcp", "list"])
        _, _, launcher_env, _ = self.run_launcher(name, env=PARENT)
        self.assertEqual(self.relevant(mcp_env), self.relevant(launcher_env))
        return mcp_env

    def test_env_matches_launcher_for_each_proxy_mode(self):
        self.ok("args", "--defaults", "--", "--allowedTools", "Read")
        self.ok("env", "--defaults", "EXTRA_VAR=~/x")
        for proxy in ("7901", "off", "inherit"):
            with self.subTest(proxy=proxy):
                self.ok("add", "a" + proxy, "--proxy", proxy)
                env = self.compare_with_launcher("a" + proxy)
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "a" + proxy))
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertEqual(env["EXTRA_VAR"], os.path.join(self.home, "x"))

    def test_default_identity(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main", "--proxy", "7901")
        env = self.compare_with_launcher("main")
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        os.unlink(os.path.join(self.home, ".claude"))
        os.unlink(self.fake_out + ".args")
        result = self.run_cli("mcp", "main", "list")
        self.assertEqual(result.code, 1)
        self.assertIn("no longer points to", result.err)
        self.assertFalse(os.path.exists(self.fake_out + ".args"))

    def test_arguments_and_exit_codes(self):
        self.ok("add", "work")
        self.assertEqual(self.run_cli("mcp", "work", "add", "x", "--", "npx", "foo", "--flag").code, 0)
        self.assertEqual(self.read_fake()[0], ["mcp", "add", "x", "--", "npx", "foo", "--flag"])
        self.assertEqual(self.run_cli("mcp", "work", "list", env={"FAKE_CLAUDE_RC": "5"}).code, 5)
        self.assertEqual(self.run_cli("mcp", "work", "list", env={"PATH": "/usr/bin:/bin"}).code, 127)
        self.assertEqual(self.run_cli("mcp", "nobody", "list").code, 1)
        self.assertEqual(self.run_cli("mcp").code, 2)
        self.assertEqual(self.run_cli("mcp", "--x").code, 2)
        self.assertEqual(self.run_cli("mcp", "--help").code, 0)

    def test_refused_during_unfinished_migration(self):
        self.ok("add", "work")
        self.write(os.path.join(self.state, "migrate-journal.json"), json.dumps(
            {"name": "main", "source": "/x", "target": "/y", "backup": "/z", "mode": "rename", "phase": "planned"}))
        result = self.run_cli("mcp", "work", "list")
        self.assertEqual(result.code, 1)
        self.assertIn("unfinished migration", result.err)
        self.assertFalse(os.path.exists(self.fake_out + ".args"))
