"""方案 feature-easier-onboarding §8：上手说明、拼错建议、代理报错、login、下一步提示、只打印有变化的动作。"""

import json
import os
import sys

from helpers import CliTestCase


class OnboardingTestCase(CliTestCase):
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

    def logged_in(self, name):
        """构造“已登录”：macOS 看钥匙串（假 security 返回 0），Linux 看凭据文件。"""
        if sys.platform == "darwin":
            return {"FAKE_SECURITY_RC": "0"}
        self.write(os.path.join(self.root, name, ".credentials.json"), "{}", 0o600)
        return {}


class EntryTest(OnboardingTestCase):
    def test_t1_no_args_without_config(self):
        result = self.ok()
        self.assertIn("Get started:", result.out)
        self.assertIn("multi-claude login work", result.out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "config.json")))

    def test_t2_no_args_with_config(self):
        self.ok("add", "work")
        overview = self.ok()
        listing = self.ok("list")
        self.assertEqual(overview.out, listing.out + "All commands: multi-claude --help\n")
        self.write(os.path.join(self.state, "config.json"), "{broken")
        broken = self.run_cli()
        self.assertEqual(broken.code, 1)
        self.assertNotIn("All commands", broken.out)

    def test_t3_unknown_command(self):
        result = self.run_cli("lsit")
        self.assertEqual(result.code, 2)
        self.assertIn("did you mean 'list'", result.err)
        result = self.run_cli("xyz")
        self.assertEqual(result.code, 2)
        self.assertIn("run multi-claude --help", result.err)
        # 合法命令、选项与参数分隔都不受影响。
        self.assertEqual(self.run_cli("--version").code, 0)
        self.ok("args", "--defaults", "--", "--ide")

    def test_t4_proxy_messages(self):
        self.ok("add", "work")
        result = self.run_cli("proxy", "work", "7891x")
        self.assertEqual(result.code, 2)
        self.assertIn("got '7891x'", result.err)
        result = self.run_cli("proxy", "work", "socks5://h:1")
        self.assertEqual(result.code, 2)
        self.assertIn("SOCKS proxies are not supported", result.err)


class LoginTest(OnboardingTestCase):
    def test_t5_runs_auth_login_with_account_env(self):
        self.ok("add", "work", "--proxy", "7901")
        self.ok("args", "--defaults", "--", "--allowedTools", "Read")
        result = self.run_cli("login", "work", env={"ANTHROPIC_API_KEY": "secret"})
        self.assertEqual(result.code, 0, result)
        args, env = self.read_fake()
        self.assertEqual(args, ["auth", "login"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "work"))
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7901")
        self.assertEqual(self.run_cli("login", "work", env={"FAKE_CLAUDE_RC": "5"}).code, 5)

    def test_t6_email_prefill_and_passthrough(self):
        self.ok("add", "me@example.com")
        self.ok("add", "work")
        self.ok("login", "me@example.com")
        self.assertEqual(self.read_fake()[0], ["auth", "login", "--email", "me@example.com"])
        self.ok("login", "me@example.com", "--email", "x@y.z")
        self.assertEqual(self.read_fake()[0], ["auth", "login", "--email", "x@y.z"])
        self.ok("login", "me@example.com", "--email=x@y.z")
        self.assertEqual(self.read_fake()[0], ["auth", "login", "--email=x@y.z"])
        self.ok("login", "work", "--console")
        self.assertEqual(self.read_fake()[0], ["auth", "login", "--console"])

    def test_t7_refusals(self):
        self.ok("add", "work")
        self.assertEqual(self.run_cli("login", "nobody").code, 1)
        self.assertEqual(self.run_cli("login").code, 2)
        self.assertEqual(self.run_cli("login", "--help").code, 0)
        self.write(os.path.join(self.state, "migrate-journal.json"), json.dumps(
            {"name": "main", "source": "/x", "target": "/y", "backup": "/z", "mode": "rename", "phase": "planned"}))
        result = self.run_cli("login", "work")
        self.assertEqual(result.code, 1)
        self.assertIn("unfinished migration", result.err)
        self.assertFalse(os.path.exists(self.fake_out + ".args"))

    def test_t8_next_step_hint(self):
        result = self.ok("add", "work")
        self.assertIn("next: multi-claude login work", result.out)
        dry = self.ok("add", "other", "--dry-run")
        self.assertNotIn("next:", dry.out)
        os.makedirs(os.path.join(self.root, "done"))
        env = self.logged_in("done")
        result = self.ok("add", "done", env=env)
        self.assertNotIn("next:", result.out)


class OutputTest(OnboardingTestCase):
    def test_t9_only_changes_by_default(self):
        self.ok("add", "work")
        self.ok("add", "other")
        result = self.ok("add", "work")
        self.assertNotIn("unchanged", result.out)
        self.assertIn("nothing to change", result.out)
        verbose = self.ok("add", "work", "--verbose")
        self.assertIn("unchanged launcher", verbose.out)
        self.assertNotIn("nothing to change", verbose.out)
        changed = self.ok("proxy", "work", "7901")
        self.assertIn("update launcher", changed.out)
        self.assertNotIn("unchanged", changed.out)
        self.assertNotIn("claude-other", changed.out)
