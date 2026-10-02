"""方案 §8 第 1、2、6–10、14、15 条及 multi-codex 移植用例：幂等、启动命令、环境变量与参数、
告警、路径绑定、冲突、共享资源、纳管、apply/remove、锁。"""

import json
import os
import shutil
import signal
import subprocess
import sys
import unittest

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import launcher  # noqa: E402


def case_insensitive_fs(directory: str) -> bool:
    probe = os.path.join(directory, "CaseProbe")
    os.makedirs(probe)
    try:
        return os.path.isdir(os.path.join(directory, "caseprobe"))
    finally:
        os.rmdir(probe)


class IdempotencyTest(CliTestCase):
    def test_every_write_command_is_idempotent(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(shared, "skills"))
        apply_file = self.write(os.path.join(self.tmp, "apply.json"), json.dumps({
            "version": 1, "root": "~/.cc", "bin_dir": "~/.local/bin",
            "shared": {"dir": shared, "items": ["skills"]},
            "defaults": {"env": {"A": "1"}, "args": ["--ide"]},
            "accounts": {"a": {"proxy": "7901", "shared": True, "env": {"B": "~/x"}}, "b": {}}}))
        commands = [
            ("init", "--shared-dir", shared),
            ("add", "a", "--proxy", "7901", "--shared"),
            ("proxy", "a", "https://127.0.0.1:1080"),
            ("env", "a", "K=v", "--unset", "Z"),
            ("env", "--defaults", "D=~/d"),
            ("args", "a", "--", "--settings", "~/s.json"),
            ("args", "--defaults", "--", "--ide"),
            ("apply", "-f", apply_file),
            ("remove", "b"),
        ]
        for command in commands:
            with self.subTest(command=command):
                self.ok(*command)
                before = self.snapshot()
                second = self.ok(*command)
                for word in ("create", "update", "delete"):
                    self.assertNotIn("] " + word, second.out, second)
                self.assertEqual(before, self.snapshot())


class LauncherTest(CliTestCase):
    def test_dir_identity_arguments_and_proxy(self):
        self.ok("add", "work", "--proxy", "7901")
        code, args, env, _ = self.run_launcher("work", "--x", "a b", env={"NO_PROXY": "10.0.0.0/8"})
        self.assertEqual(code, 0)
        self.assertEqual(args, ["--x", "a b"])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "work"))
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            self.assertEqual(env[name], "http://127.0.0.1:7901")
        self.assertNotIn("ALL_PROXY", env)
        # NO_PROXY 在原值后追加；no_proxy 原来为空，不能留前导逗号。
        self.assertEqual(env["NO_PROXY"], "10.0.0.0/8,localhost,127.0.0.1,::1")
        self.assertEqual(env["no_proxy"], "localhost,127.0.0.1,::1")

    def test_config_dir_string_is_not_resolved(self):
        """§8 第 2 条：根目录经软链访问时仍写原字符串，不做 realpath，不带尾部斜杠。"""
        real = os.path.join(self.tmp, "real")
        os.makedirs(real)
        link = os.path.join(self.tmp, "via-link")
        os.symlink(real, link)
        self.ok("init", "--root", link + "/cc/")
        self.ok("add", "work")
        _, _, env, _ = self.run_launcher("work")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(link, "cc", "work"))

    def test_inherited_identity_variables_are_replaced(self):
        self.ok("add", "work")
        _, _, env, _ = self.run_launcher("work", env={"CLAUDE_CONFIG_DIR": "/elsewhere",
                                                      "CLAUDE_SECURESTORAGE_CONFIG_DIR": "x"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "work"))
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", env)

    def test_argument_order(self):
        self.ok("add", "work")
        self.ok("args", "--defaults", "--", "--ide")
        self.ok("args", "work", "--", "--settings", "s")
        _, args, _, _ = self.run_launcher("work", "a b", "-p", "--")
        self.assertEqual(args, ["--ide", "--settings", "s", "a b", "-p", "--"])

    def test_off_and_inherit_with_proxy_in_parent_environment(self):
        parent = {name: "http://parent:1" for name in
                  ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
                   "https_proxy", "http_proxy", "all_proxy", "no_proxy")}
        self.ok("add", "work", "--proxy", "off")
        _, _, env, _ = self.run_launcher("work", env=parent)
        self.assertFalse(set(parent) & set(env), env)
        self.ok("proxy", "work", "inherit")
        _, _, env, _ = self.run_launcher("work", env=parent)
        for name, value in parent.items():
            self.assertEqual(env[name], value)

    def test_http_proxy_clears_inherited_all_proxy(self):
        self.ok("add", "work", "--proxy", "7901")
        _, _, env, _ = self.run_launcher("work", env={"ALL_PROXY": "http://parent:1",
                                                      "all_proxy": "http://parent:1"})
        self.assertNotIn("ALL_PROXY", env)
        self.assertNotIn("all_proxy", env)

    def test_missing_account_dir(self):
        self.ok("add", "work")
        shutil.rmtree(os.path.join(self.root, "work"))
        code, args, _, stderr = self.run_launcher("work")
        self.assertEqual(code, 1)
        self.assertEqual(args, [])
        self.assertIn("account directory does not exist", stderr)
        self.assertIn("missing-dir", self.ok("list", "--verbose").out)

    def test_missing_dir_message_keeps_backslash(self):
        root = os.path.join(self.tmp, "back\\slash")
        self.ok("init", "--root", root)
        self.ok("add", "work")
        shutil.rmtree(os.path.join(root, "work"))
        _, _, _, stderr = self.run_launcher("work")
        self.assertIn(os.path.join(root, "work"), stderr)

    def test_root_with_space_and_quote(self):
        root = os.path.join(self.tmp, "my root's dir")
        self.ok("init", "--root", root)
        self.ok("add", "work")
        code, _, env, _ = self.run_launcher("work")
        self.assertEqual(code, 0)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(root, "work"))

    def test_claude_not_on_path(self):
        self.ok("add", "work")
        code, _, _, stderr = self.run_launcher("work", env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(code, 127)
        self.assertIn("claude not found", stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck_generated_launchers(self):
        self.ok("add", "a", "--proxy", "7901")
        self.ok("add", "b", "--proxy", "off")
        self.ok("add", "c", "--proxy", "https://127.0.0.1:1080")
        # 不放含引号的值：shellcheck 对任何含引号字符的赋值都报 SC2089（启发式误报），
        # 这类值的正确性由 EnvArgsTest.test_value_with_dollar_is_not_expanded 覆盖。
        self.ok("env", "c", "A=~/x", "B=a b")
        self.ok("args", "c", "--", "--settings", "~/s.json", "a b")
        self.ok("add", "d")
        paths = [os.path.join(self.bin, "claude-" + name) for name in "abcd"]
        default = self.write(os.path.join(self.tmp, "claude-main"), launcher.render(
            "main", "default", os.path.join(self.root, "main"), os.path.join(self.home, ".claude"),
            "7901", {"A": "1"}, ["--ide"]), 0o755)
        for path in paths + [default]:
            proc = subprocess.run(["shellcheck", path], stdout=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(proc.returncode, 0, proc.stdout)


class AuthOverrideTest(CliTestCase):
    """v0.2 §8 第 4 条：启动命令清除会让所有账号共用一份凭据的环境变量。"""

    AUTH = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN")

    def test_cleared_for_dir_and_default_identity(self):
        self.ok("add", "work")
        self.ok("env", "work", "MAX_THINKING_TOKENS=8000")
        parent = {name: "leaked" for name in self.AUTH}
        _, _, env, _ = self.run_launcher("work", env=parent)
        self.assertFalse(set(self.AUTH) & set(env), env)
        self.assertEqual(env["MAX_THINKING_TOKENS"], "8000")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main")
        code, _, env, _ = self.run_launcher("main", env=parent)
        self.assertEqual(code, 0)
        self.assertFalse(set(self.AUTH) & set(env), env)

    def test_upgrade_from_old_template(self):
        self.ok("add", "work")
        path = os.path.join(self.bin, "claude-work")
        with open(path) as handle:
            old = "".join(line for line in handle.read().splitlines(True) if not line.startswith("unset ANTHROPIC_"))
        self.write(path, old, 0o755)
        self.assertRegex(self.ok("list", "--verbose").out, r"work\s+dir\s+ok\s+inherit\s+no\s+stale")
        result = self.ok("apply")
        self.assertIn("update launcher", result.out)
        self.assertNotIn("login-bound", result.err)
        self.assertRegex(self.ok("list", "--verbose").out, r"work\s+dir\s+ok\s+inherit\s+no\s+ok")

    def test_warning_text(self):
        self.ok("add", "work")
        err = self.ok("list", env={"ANTHROPIC_API_KEY": "secret-value", "ANTHROPIC_PROFILE": "p"}).err
        self.assertIn("ANTHROPIC_API_KEY is set in the environment; launchers clear it", err)
        self.assertIn("ANTHROPIC_PROFILE is set in the environment; every account", err)
        self.assertNotIn("secret-value", err)


class ListOutputTest(CliTestCase):
    """v0.2 §8 第 5 条：list --json 与 --names。"""

    def test_json(self):
        self.ok("add", "work", "--proxy", "7901")
        data = json.loads(self.ok("list", "--json").out)
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["configured"])
        entry = data["accounts"][0]
        self.assertEqual(entry["name"], "work")
        self.assertEqual(entry["dir"], os.path.join(self.root, "work"))
        self.assertEqual(entry["proxy"], "http://127.0.0.1:7901")
        self.assertIs(entry["shared"], False)
        self.assertEqual(entry["launcher"], "ok")
        self.assertIsNone(entry["link"])
        self.assertTrue(entry["keychain_service"].startswith("Claude Code-credentials-"))
        self.assertEqual(entry["usage"]["status"], "no-data")

    def test_unconfigured_and_names(self):
        self.assertEqual(json.loads(self.ok("list", "--json").out),
                         {"schema_version": 1, "configured": False, "accounts": [],
                          "routes": {"default": None, "rules": []}})
        self.assertEqual(self.ok("list", "--names").out, "")
        self.ok("add", "b@x.com")
        self.ok("add", "a")
        self.assertEqual(self.ok("list", "--names").out, "b@x.com\na\n")
        self.assertEqual(self.run_cli("list", "--json", "--names").code, 2)

    def test_read_only_commands_during_migration(self):
        self.ok("add", "work")
        self.write(os.path.join(self.state, "migrate-journal.json"), json.dumps(
            {"name": "main", "source": "/x", "target": "/y", "backup": "/z", "mode": "rename", "phase": "planned"}))
        self.ok("usage")
        self.ok("list", "--json")
        self.ok("completion", "bash")
        self.assertEqual(self.run_cli("doctor").code, 1)
        self.write(os.path.join(self.state, "migrate-journal.json"), "{broken")
        self.assertEqual(self.run_cli("list", "--json").code, 1)
        self.assertEqual(self.run_cli("usage").code, 1)
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.code, 1)
        json.loads(result.out)


class EnvArgsTest(CliTestCase):
    """§8 第 8 条：额外环境变量与固定参数。"""

    def test_env_expansion_and_override(self):
        self.ok("add", "work")
        self.ok("env", "--defaults", "K=default", "D=~/d")
        self.ok("env", "work", "K=account", "T=~", "S=--settings=~/x", "P=a~/b")
        _, _, env, _ = self.run_launcher("work")
        self.assertEqual(env["K"], "account")
        self.assertEqual(env["D"], os.path.join(self.home, "d"))
        self.assertEqual(env["T"], self.home)
        self.assertEqual(env["S"], "--settings=~/x")
        self.assertEqual(env["P"], "a~/b")
        self.ok("env", "work", "--unset", "K")
        _, _, env, _ = self.run_launcher("work")
        self.assertEqual(env["K"], "default")

    def test_value_with_dollar_is_not_expanded(self):
        self.ok("add", "work")
        self.ok("env", "work", "V=$HOME `x` it's \"q\"")
        _, _, env, _ = self.run_launcher("work")
        self.assertEqual(env["V"], "$HOME `x` it's \"q\"")

    def test_unset_missing_key_is_unchanged(self):
        self.ok("add", "work")
        result = self.ok("env", "work", "--unset", "NOPE")
        self.assertNotIn("] update", result.out)

    def test_args_parsing(self):
        self.ok("add", "work")
        self.ok("args", "--defaults", "--", "--ide", "--settings", "s")
        self.assertEqual(self.config_data()["defaults"]["args"], ["--ide", "--settings", "s"])
        self.ok("args", "work", "--", "--resume", "x")
        self.assertEqual(self.config_data()["accounts"]["work"]["args"], ["--resume", "x"])
        _, args, _, _ = self.run_launcher("work", "p")
        self.assertEqual(args, ["--ide", "--settings", "s", "--resume", "x", "p"])
        self.ok("args", "work", "--")
        self.assertEqual(self.config_data()["accounts"]["work"]["args"], [])
        self.ok("args", "work", "--", "~/a", "--settings=~/b")
        _, args, _, _ = self.run_launcher("work")
        self.assertEqual(args, ["--ide", "--settings", "s", os.path.join(self.home, "a"), "--settings=~/b"])

    def test_args_usage_errors(self):
        self.ok("add", "work")
        before = self.snapshot()
        for command in (("args", "work"), ("args", "--defaults"), ("args", "work", "--defaults", "--"),
                        ("args", "--"), ("env", "work"), ("env", "--defaults", "work"), ("env", "work", "A")):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(*command).code, 2)
        self.assertEqual(before, self.snapshot())

    def test_unregistered_account_returns_1(self):
        self.ok("add", "work")
        before = self.snapshot()
        self.assertEqual(self.run_cli("env", "other", "A=1").code, 1)
        self.assertEqual(self.run_cli("args", "other", "--", "x").code, 1)
        self.assertEqual(self.run_cli("proxy", "other", "off").code, 1)
        self.assertEqual(before, self.snapshot())

    def test_variadic_warning(self):
        """§8 第 9 条。"""
        self.ok("add", "work")
        cases = [
            (("--allowedTools", "a", "b"), True),
            (("--allowedTools", "a", "--settings", "s"), False),
            (("--allowed-tools=a",), True),
            (("--resume",), True),
            (("--cloud",), True),
            (("--resume", "x"), False),
            (("--ide",), False),
        ]
        for extra, warns in cases:
            with self.subTest(args=extra):
                result = self.ok("args", "work", "--", *extra)
                self.assertEqual("put another option after it" in result.err, warns, result)


class SettingsOverrideTest(CliTestCase):
    """§8 第 7 条：settings 文件的 env 覆盖启动命令的代理时告警，告警中不出现值。"""

    def setUp(self):
        super().setUp()
        self.ok("add", "work")
        self.account = os.path.join(self.root, "work")

    def test_account_settings_file(self):
        self.write(os.path.join(self.account, "settings.json"), json.dumps({"env": {"HTTPS_PROXY": "x-value"}}))
        self.assertNotIn("overrides the proxy", self.ok("list").err)
        for command in (("proxy", "work", "7901"), ("apply",), ("list",)):
            with self.subTest(command=command):
                result = self.ok(*command)
                self.assertIn("sets HTTPS_PROXY in env, which overrides the proxy set by claude-work", result.err)
                self.assertNotIn("x-value", result.err + result.out)

    def test_settings_argument_and_inline(self):
        shared = self.write(os.path.join(self.tmp, "s.json"), json.dumps({"env": {"https_proxy": "x-value"}}))
        self.ok("args", "--defaults", "--", "--settings", shared)
        self.ok("args", "work", "--", "--settings", '{"env":{"HTTP_PROXY":"x-value"}}')
        result = self.ok("proxy", "work", "7901")
        self.assertIn("{} sets https_proxy".format(shared), result.err)
        self.assertIn("inline --settings sets HTTP_PROXY", result.err)
        self.assertNotIn("x-value", result.err + result.out)

    def test_invalid_json_is_not_an_error(self):
        self.write(os.path.join(self.account, "settings.json"), "{not json")
        self.ok("proxy", "work", "7901")
        listing = self.ok("list")
        self.assertIn("is not valid JSON", listing.out)


class ListTest(CliTestCase):
    def test_columns_and_verbose(self):
        self.ok("add", "work")
        listing = self.ok("list", "--verbose").out
        self.assertIn("IDENTITY", listing)
        self.assertIn("LOGIN", listing)
        self.assertIn("Claude Code-credentials-", listing)
        self.assertIn(os.path.join(self.root, "work", ".credentials.json"), listing)

    def test_login_column(self):
        self.ok("add", "work")
        self.write(os.path.join(self.root, "work", ".credentials.json"), "{}", 0o600)
        self.assertRegex(self.ok("list", "--verbose").out, r"work\s+dir\s+ok\s+inherit\s+no\s+ok\s+file")

    @unittest.skipUnless(sys.platform == "darwin", "the keychain exists only on macOS")
    def test_keychain_login_column(self):
        self.ok("add", "work")
        self.assertRegex(self.ok("list", "--verbose", env={"FAKE_SECURITY_RC": "0"}).out, r"work .* keychain")
        self.assertRegex(self.ok("list", "--verbose", env={"FAKE_SECURITY_RC": "1"}).out, r"work .* unknown")

    def test_add_existing_dir_without_login_warns(self):
        os.makedirs(os.path.join(self.root, "old"))
        result = self.ok("add", "old")
        if sys.platform == "darwin":
            self.assertIn("no login found", result.err)
        else:
            self.assertNotIn("no login found", result.err)
        # 新建的目录本来就没有登录，不提示。
        self.assertNotIn("no login found", self.ok("add", "new").err)


class ConflictTest(CliTestCase):
    def assert_conflict_without_changes(self, *command, env=None):
        before = self.snapshot()
        result = self.run_cli(*command, env=env)
        self.assertEqual(result.code, 3, result)
        self.assertEqual(before, self.snapshot())
        return result

    def test_unmanaged_launcher(self):
        self.write(os.path.join(self.bin, "claude-hud"), "#!/bin/sh\necho mine\n", 0o755)
        result = self.assert_conflict_without_changes("add", "hud")
        self.assertIn("not managed by multi-claude", result.err)

    def test_shared_item_is_real_directory_or_other_link(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(shared, "skills"))
        os.makedirs(os.path.join(shared, "rules"))
        self.ok("init", "--shared-dir", shared, "--shared-items", "skills,rules")
        self.ok("add", "work")
        os.makedirs(os.path.join(self.root, "work", "skills", "synced", ".bucket-x"))
        result = self.assert_conflict_without_changes("add", "work", "--shared")
        self.assertIn("real directory", result.err)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "work", "skills", "synced", ".bucket-x")))
        shutil.rmtree(os.path.join(self.root, "work", "skills"))
        os.symlink(self.tmp, os.path.join(self.root, "work", "rules"))
        self.assert_conflict_without_changes("add", "work", "--shared")

    def test_change_root_with_accounts(self):
        self.ok("add", "work")
        self.assert_conflict_without_changes("init", "--root", os.path.join(self.tmp, "other"))

    def test_home_change_would_change_login_bound_path(self):
        """§8 第 10 条：状态目录与 bin_dir 为绝对路径时 HOME 变了，不得改写启动命令里的账号路径。"""
        bin_dir = os.path.join(self.tmp, "bin")
        self.ok("init", "--bin-dir", bin_dir)
        self.ok("add", "work")
        launcher_file = os.path.join(bin_dir, "claude-work")
        with open(launcher_file) as handle:
            before = handle.read()
        other_home = os.path.join(self.tmp, "h2")
        os.makedirs(other_home)
        result = self.run_cli("apply", env={"HOME": other_home})
        self.assertEqual(result.code, 3, result)
        self.assertIn("login-bound", result.err)
        with open(launcher_file) as handle:
            self.assertEqual(before, handle.read())
        self.assertFalse(os.path.exists(os.path.join(other_home, ".cc")))

    def test_directory_differs_only_in_case(self):
        os.makedirs(self.root)
        if not case_insensitive_fs(self.root):
            os.makedirs(os.path.join(self.root, "Work"))
            self.ok("add", "work")
            self.assertTrue(os.path.isdir(os.path.join(self.root, "work")))
            return
        os.makedirs(os.path.join(self.root, "Work"))
        result = self.assert_conflict_without_changes("add", "work")
        self.assertIn("differs only in case", result.err)
        self.ok("add", "Work")


class SharedTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(self.shared, "skills"))
        self.write(os.path.join(self.shared, "skills", "s.md"))
        self.write(os.path.join(self.shared, "CLAUDE.md"))
        self.ok("init", "--shared-dir", self.shared, "--shared-items", "CLAUDE.md,skills,rules")

    def managed_links(self, name):
        return self.config_data()["accounts"][name]["managed_links"]

    def test_toggle_shared(self):
        self.ok("add", "work")
        account = os.path.join(self.root, "work")
        result = self.ok("add", "work", "--shared")
        # rules 不在共享目录里：这类跳过提示默认不打印，--verbose 才列出（方案 feature-set-command §5.1.4）。
        self.assertNotIn("not present in shared dir", result.out)
        self.assertIn("not present in shared dir", self.ok("add", "work", "--verbose").out)
        self.assertTrue(os.path.islink(os.path.join(account, "skills")))
        self.assertEqual(self.managed_links("work"), ["CLAUDE.md", "skills"])
        self.ok("add", "work", "--no-shared")
        self.assertFalse(os.path.lexists(os.path.join(account, "skills")))
        self.assertEqual(self.managed_links("work"), [])
        self.assertTrue(os.path.exists(os.path.join(self.shared, "skills", "s.md")))
        self.ok("add", "work", "--shared")
        self.assertTrue(os.path.islink(os.path.join(account, "CLAUDE.md")))

    def test_user_link_is_not_removed(self):
        self.ok("add", "work")
        account = os.path.join(self.root, "work")
        os.symlink(os.path.join(self.shared, "skills"), os.path.join(account, "skills"))
        result = self.ok("add", "work", "--shared", "--verbose")
        self.assertIn("unchanged shared-link {}".format(os.path.join(account, "skills")), result.out)
        self.assertEqual(self.managed_links("work"), ["CLAUDE.md"])
        self.ok("add", "work", "--no-shared")
        self.assertTrue(os.path.islink(os.path.join(account, "skills")))
        self.assertFalse(os.path.lexists(os.path.join(account, "CLAUDE.md")))

    def _user_links(self, items):
        account = os.path.join(self.root, "work")
        for item in items:
            os.symlink(os.path.join(self.shared, item), os.path.join(account, item))
        return account

    def test_adopt_existing_links(self):
        """接管用户自建的软链，不重建；再次执行全部 unchanged；接管后关闭共享会删除它们。"""
        self.ok("add", "work")
        account = self._user_links(["CLAUDE.md", "skills"])
        before = {item: os.lstat(os.path.join(account, item)).st_ino for item in ("CLAUDE.md", "skills")}
        result = self.ok("add", "work", "--shared", "--adopt")
        self.assertIn("update shared-link {} (adopted)".format(os.path.join(account, "skills")), result.out)
        self.assertEqual(self.managed_links("work"), ["CLAUDE.md", "skills"])
        after = {item: os.lstat(os.path.join(account, item)).st_ino for item in ("CLAUDE.md", "skills")}
        self.assertEqual(before, after, "adopted links must not be recreated")
        again = self.ok("add", "work", "--shared", "--adopt")
        self.assertNotIn("] update", again.out)
        self.ok("add", "work", "--no-shared")
        self.assertFalse(os.path.lexists(os.path.join(account, "skills")))
        self.assertFalse(os.path.lexists(os.path.join(account, "CLAUDE.md")))
        self.assertTrue(os.path.exists(os.path.join(self.shared, "skills", "s.md")))

    def test_adopt_on_already_shared_account(self):
        self.ok("add", "work")
        account = self._user_links(["skills"])
        self.ok("add", "work", "--shared")
        self.assertEqual(self.managed_links("work"), ["CLAUDE.md"])
        self.ok("add", "work", "--adopt")
        self.assertEqual(self.managed_links("work"), ["CLAUDE.md", "skills"])
        self.assertTrue(os.path.islink(os.path.join(account, "skills")))

    def test_adopt_requires_sharing(self):
        self.ok("add", "work")
        self._user_links(["skills"])
        before = self.snapshot()
        result = self.run_cli("add", "work", "--adopt")
        self.assertEqual(result.code, 2, result)
        self.assertIn("--adopt requires sharing", result.err)
        self.assertEqual(before, self.snapshot())

    def test_adopt_does_not_bypass_conflicts(self):
        """其它项冲突时什么都不接管；指向别处的软链仍是冲突。"""
        self.ok("add", "work")
        account = self._user_links(["skills"])
        os.makedirs(os.path.join(self.shared, "rules"))
        os.symlink(self.tmp, os.path.join(account, "rules"))
        before = self.snapshot()
        result = self.run_cli("add", "work", "--shared", "--adopt")
        self.assertEqual(result.code, 3, result)
        self.assertEqual(before, self.snapshot())
        os.unlink(os.path.join(account, "rules"))
        os.makedirs(os.path.join(account, "rules"))
        self.assertEqual(self.run_cli("add", "work", "--shared", "--adopt").code, 3)
        self.assertEqual(self.managed_links("work"), [])

    def test_item_removed_from_shared_items(self):
        self.ok("add", "work", "--shared")
        self.ok("init", "--shared-items", "CLAUDE.md")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "work", "skills")))
        self.assertTrue(os.path.islink(os.path.join(self.root, "work", "CLAUDE.md")))
        self.assertTrue(os.path.isdir(os.path.join(self.shared, "skills")))

    def test_change_shared_dir(self):
        self.ok("add", "work", "--shared")
        other = os.path.join(self.tmp, "shared2")
        shutil.copytree(self.shared, other)
        self.ok("init", "--shared-dir", other)
        link = os.path.join(self.root, "work", "skills")
        self.assertEqual(os.path.realpath(link), os.path.join(other, "skills"))


class DefaultSharedItemsTest(CliTestCase):
    """§8 第 14 条：默认条目，以及共享目录里不在名单中的内容不会被链接。"""

    def test_default_items(self):
        shared = os.path.join(self.tmp, "shared")
        for item in ("agents", "commands", "skills", "todo", "playbook"):
            os.makedirs(os.path.join(shared, item))
        self.write(os.path.join(shared, "CLAUDE.md"))
        self.ok("init", "--shared-dir", shared)
        self.assertEqual(self.config_data()["shared"]["items"], ["agents", "commands", "skills", "CLAUDE.md"])
        self.ok("add", "work", "--shared")
        account = os.path.join(self.root, "work")
        self.assertEqual(sorted(name for name in os.listdir(account) if os.path.islink(os.path.join(account, name))),
                         ["CLAUDE.md", "agents", "commands", "skills"])
        self.assertFalse(os.path.lexists(os.path.join(account, "todo")))


class AdoptExistingTest(CliTestCase):
    def test_existing_cc_layout(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(shared, "skills"))
        # ~/.cc 下以 `.` 开头的目录（如在 ~/.cc 里运行 claude 留下的 .claude/）不当账号。
        os.makedirs(os.path.join(self.root, ".claude"))
        gmail = os.path.join(self.root, "a@gmail.com")
        outlook = os.path.join(self.root, "b@outlook.com")
        self.write(os.path.join(gmail, ".claude.json"), "{}")
        os.symlink(os.path.join(shared, "skills"), os.path.join(gmail, "skills"))
        os.makedirs(os.path.join(outlook, "skills", "synced"))
        before = self.snapshot(self.root)
        self.ok("init", "--shared-dir", shared, "--shared-items", "skills")
        self.ok("add", "a@gmail.com", "--proxy", "7901")
        self.ok("add", "b@outlook.com")
        self.assertEqual(before, self.snapshot(self.root))
        listing = self.ok("list", "--verbose").out
        self.assertNotIn(".claude ", listing)
        self.ok("add", "a@gmail.com", "--shared")
        before = self.snapshot(self.root)
        self.assertEqual(self.run_cli("add", "b@outlook.com", "--shared").code, 3)
        self.assertEqual(before, self.snapshot(self.root))


class ApplyRemoveInitTest(CliTestCase):
    def test_bin_dir_change_moves_launchers(self):
        self.ok("add", "work")
        new_bin = os.path.join(self.tmp, "bin2")
        self.ok("init", "--bin-dir", new_bin)
        self.assertFalse(os.path.exists(os.path.join(self.bin, "claude-work")))
        self.assertTrue(os.path.exists(os.path.join(new_bin, "claude-work")))

    def test_apply_file_drops_account(self):
        self.ok("add", "a")
        self.ok("add", "b")
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps({"version": 1, "accounts": {"a": {}}}))
        self.ok("apply", "-f", path)
        self.assertFalse(os.path.exists(os.path.join(self.bin, "claude-b")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "b")))

    def test_remove_unregistered_cleans_orphan(self):
        self.ok("add", "a")
        data = self.config_data()
        data["accounts"] = {}
        self.write(os.path.join(self.state, "config.json"), json.dumps(data))
        result = self.ok("remove", "a")
        self.assertIn("not registered", result.out)
        self.assertFalse(os.path.exists(os.path.join(self.bin, "claude-a")))

    def test_apply_conflict_leaves_config_untouched(self):
        self.ok("add", "a")
        self.write(os.path.join(self.bin, "claude-b"), "#!/bin/sh\n", 0o755)
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(
            {"version": 1, "accounts": {"a": {}, "b": {}}}))
        before = self.snapshot()
        self.assertEqual(self.run_cli("apply", "-f", path).code, 3)
        self.assertEqual(before, self.snapshot())

    def test_apply_root_change_conflict(self):
        self.ok("add", "a")
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(
            {"version": 1, "root": os.path.join(self.tmp, "r2"), "accounts": {"a": {}}}))
        self.assertEqual(self.run_cli("apply", "-f", path).code, 3)

    def test_apply_case_only_rename_is_conflict(self):
        self.ok("add", "work")
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps({"version": 1, "accounts": {"Work": {}}}))
        before = self.snapshot()
        self.assertEqual(self.run_cli("apply", "-f", path).code, 3)
        self.assertEqual(before, self.snapshot())

    def test_apply_case_duplicate(self):
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(
            {"version": 1, "accounts": {"Work": {}, "work": {}}}))
        self.assertEqual(self.run_cli("apply", "-f", path).code, 1)

    def test_apply_ignores_managed_links_in_file(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(shared, "skills"))
        self.ok("init", "--shared-dir", shared, "--shared-items", "skills")
        self.ok("add", "a")
        user_link = os.path.join(self.root, "a", "skills")
        os.symlink(os.path.join(shared, "skills"), user_link)
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps({
            "version": 1, "shared": {"dir": shared, "items": ["skills"]},
            "accounts": {"a": {"shared": True, "managed_links": ["skills"]}}}))
        self.ok("apply", "-f", path)
        self.ok("add", "a", "--no-shared")
        self.assertTrue(os.path.islink(user_link), "a link the user created must survive")

    def test_corrupted_journal(self):
        self.write(os.path.join(self.state, "migrate-journal.json"), "{not json")
        for command in (("list",), ("add", "a"), ("migrate-default", "main")):
            with self.subTest(command=command):
                result = self.run_cli(*command)
                self.assertEqual(result.code, 1, result)
                self.assertIn("delete the journal file", result.err)
                self.assertNotIn("Traceback", result.err)

    def test_pending_journal_blocks_other_writes(self):
        self.write(os.path.join(self.state, "migrate-journal.json"), json.dumps(
            {"name": "main", "source": "/nonexistent", "target": "/x", "backup": "/y",
             "mode": "rename", "phase": "planned"}))
        self.assertEqual(self.run_cli("add", "a").code, 1)
        self.assertEqual(self.run_cli("env", "--defaults", "A=1").code, 1)
        self.ok("list")


class LockTest(CliTestCase):
    def test_lock_busy_and_released_after_kill(self):
        holder_code = ("import os, time, sys; sys.path.insert(0, {!r}); "
                       "from multi_claude.lock import WriteLock\n"
                       "with WriteLock():\n    print('locked', flush=True); time.sleep(60)").format(SRC)
        holder = subprocess.Popen([sys.executable, "-c", holder_code], env=dict(self.env),
                                  stdout=subprocess.PIPE, universal_newlines=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            result = self.run_cli("add", "a")
            self.assertEqual(result.code, 1)
            self.assertIn("pid {}".format(holder.pid), result.err)
        finally:
            holder.send_signal(signal.SIGKILL)
            holder.wait()
            holder.stdout.close()
        self.ok("add", "a")

    def test_stale_pid_in_lock_file(self):
        os.makedirs(self.state, exist_ok=True)
        self.write(os.path.join(self.state, "lock"), "1")  # PID 1 永远存活，但并不持有锁
        self.ok("add", "a")


class EnvironmentWarningTest(CliTestCase):
    def test_isolation_breaking_variables(self):
        self.ok("add", "a")
        result = self.ok("list", env={"ANTHROPIC_API_KEY": "secret-value"})
        self.assertIn("ANTHROPIC_API_KEY", result.err)
        self.assertNotIn("secret-value", result.err + result.out)

    def test_unmanaged_proxy_variables(self):
        self.ok("add", "a")
        self.assertIn("CLAUDE_CODE_HTTPS_PROXY is set", self.ok("list", env={"CLAUDE_CODE_HTTPS_PROXY": "x"}).err)


if __name__ == "__main__":
    unittest.main()
