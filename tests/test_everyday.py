"""v0.5.0 常用命令：run、path、restore、migrate-default 省略名称、add --config-from
（方案 docs/feature/feature-everyday-commands.md §8 的 T1–T13；T14 为全量回归）。"""

import json
import os
import stat
import subprocess
import unittest

from helpers import CliTestCase

CRASH = 137


def _env_lines(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class RunPathTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "work", "--proxy", "7901")
        self.ok("args", "work", "--", "--settings", "~/x.json", "--model", "opus")
        self.work_dir = os.path.join(self.root, "work")

    def test_t1_run_without_command_matches_launcher(self):
        code, launcher_args, launcher_env, _ = self.run_launcher("work")
        self.assertEqual(code, 0)
        self.ok("run", "work")
        with open(self.fake_out + ".args") as handle:
            run_args = handle.read().split("\n")[:-1]
        with open(self.fake_out + ".env") as handle:
            run_env = _env_lines(handle.read())
        self.assertEqual(run_args, launcher_args)
        self.assertEqual(run_args[1], os.path.join(self.home, "x.json"))
        for key in ("CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"):
            self.assertEqual(run_env.get(key), launcher_env.get(key), key)

    def test_t2_run_command_and_exit_code(self):
        result = self.ok("run", "work", "--", "env")
        env = _env_lines(result.out)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.work_dir)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7901")
        self.assertEqual(self.run_cli("run", "work", "--", "sh", "-c", "exit 7").code, 7)
        # `--` 后为空：与不给命令相同，运行 claude
        self.ok("run", "work", "--")
        self.assertTrue(os.path.exists(self.fake_out + ".args"))

    def test_t3_run_without_name_uses_routes(self):
        self.ok("add", "personal")
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project)
        self.ok("route", project, "personal")
        result = self.ok("run", "--", "env", cwd=project)
        self.assertEqual(_env_lines(result.out)["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "personal"))
        self.assertIn("using account personal (route", result.err)
        self.assertNotIn("using account", result.out)
        elsewhere = os.path.join(self.tmp, "other")
        os.makedirs(elsewhere)
        no_route = self.run_cli("run", "--", "env", cwd=elsewhere)
        self.assertEqual(no_route.code, 2, no_route)
        self.ok("route", "--default", "work")
        result = self.ok("run", "--", "env", cwd=elsewhere)
        self.assertEqual(_env_lines(result.out)["CLAUDE_CONFIG_DIR"], self.work_dir)
        self.assertIn("(default)", result.err)
        self.assertEqual(self.run_cli("run", "work", "env").code, 2)

    def test_t4_run_failures(self):
        self.assertEqual(self.run_cli("run", "nosuch").code, 1)
        self.assertEqual(self.run_cli("run", "work", "--", "no-such-cmd-xyz").code, 127)
        with open(os.path.join(self.state, "migrate-journal.json"), "w") as handle:
            json.dump({"name": "main", "source": "s", "target": "t", "backup": "b", "mode": "rename",
                       "phase": "planned"}, handle)
        for command in (("run", "work"), ("path", "work")):
            result = self.run_cli(*command)
            self.assertEqual(result.code, 1, result)
            self.assertIn("unfinished migration", result.err)

    def test_t5_path(self):
        result = self.ok("path", "work")
        self.assertEqual(result.out, self.work_dir + "\n")
        self.assertEqual(self.run_cli("path", "nosuch").code, 1)
        os.rmdir(self.work_dir)
        result = self.run_cli("path", "work")
        self.assertEqual((result.code, result.out), (1, self.work_dir + "\n"))


class MigrateDefaultBase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.source = os.path.join(self.home, ".claude")
        self.write(os.path.join(self.source, ".credentials.json"), "{}", 0o600)
        self.write(os.path.join(self.source, "projects", "p", "a.jsonl"), "line\n")

    def claude_json(self, data):
        path = os.path.join(self.home, ".claude.json")
        with open(path, "w") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))
        return path


class MigrateNameTest(MigrateDefaultBase):
    def test_t6_name_from_email(self):
        self.claude_json({"oauthAccount": {"emailAddress": "me@example.com"}})
        result = self.ok("migrate-default")
        self.assertIn("using the name me@example.com", result.out)
        self.assertTrue(os.path.islink(self.source))
        self.assertEqual(self.config_data()["accounts"]["me@example.com"]["identity"], "default")
        # 已迁移后再执行：沿用已有 default 账号的名称
        again = self.ok("migrate-default")
        self.assertIn("already migrated", again.out)

    def test_t6_missing_or_invalid(self):
        for data in ({"numStartups": 1}, "not json", {"oauthAccount": {"emailAddress": "a b@x.com"}}):
            with self.subTest(data=data):
                self.claude_json(data)
                result = self.run_cli("migrate-default")
                self.assertEqual(result.code, 2, result)
                self.assertIn("needs a NAME", result.err)
                self.assertFalse(os.path.islink(self.source))
        os.unlink(os.path.join(self.home, ".claude.json"))
        self.assertEqual(self.run_cli("migrate-default").code, 2)

    def test_t6_resume_uses_journal_name(self):
        self.claude_json({"oauthAccount": {"emailAddress": "me@example.com"}})
        crashed = self.run_cli("migrate-default", "main", env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-moved"})
        self.assertEqual(crashed.code, CRASH, crashed)
        self.ok("migrate-default")
        self.assertIn("main", self.config_data()["accounts"])
        self.assertNotIn("me@example.com", self.config_data()["accounts"])


class RunRedirectedLinkTest(MigrateDefaultBase):
    def test_t4_run_refuses_redirected_default_link(self):
        self.ok("migrate-default", "main")
        os.unlink(self.source)
        os.makedirs(self.source)
        result = self.run_cli("run", "main", "--", "env")
        self.assertEqual(result.code, 1, result)
        self.assertIn("no longer points to", result.err)


class ConfigFromTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "work")
        self.settings = self.write(os.path.join(self.root, "work", "settings.json"), '{"model": "opus"}\n', 0o600)

    def target(self, name="new"):
        return os.path.join(self.root, name, "settings.json")

    def test_t7_copy_and_rerun(self):
        result = self.ok("add", "new", "--config-from", "work")
        self.assertIn("copied settings.json from work", result.out)
        with open(self.target()) as handle:
            self.assertEqual(handle.read(), '{"model": "opus"}\n')
        self.assertEqual(stat.S_IMODE(os.stat(self.target()).st_mode), 0o600)
        again = self.ok("add", "new", "--config-from", "work")
        self.assertNotIn("copied", again.out)

    def test_t7_conflict_registers_nothing(self):
        self.write(self.target(), "{}\n")
        result = self.run_cli("add", "new", "--config-from", "work")
        self.assertEqual(result.code, 3, result)
        self.assertNotIn("new", self.config_data()["accounts"])
        with open(self.target()) as handle:
            self.assertEqual(handle.read(), "{}\n")

    def test_t7_bad_sources(self):
        self.assertEqual(self.run_cli("add", "new", "--config-from", "nosuch").code, 1)
        self.assertEqual(self.run_cli("add", "work", "--config-from", "work").code, 2)
        self.ok("add", "empty")
        self.assertEqual(self.run_cli("add", "new", "--config-from", "empty").code, 1)
        os.unlink(self.settings)
        os.symlink(os.path.join(self.tmp, "elsewhere.json"), self.settings)
        self.assertEqual(self.run_cli("add", "new", "--config-from", "work").code, 1)
        self.assertNotIn("new", self.config_data()["accounts"])

    def test_t7_shared_settings_refused(self):
        self.ok("init", "--shared-items", "skills,settings.json")
        result = self.run_cli("add", "new", "--shared", "--config-from", "work")
        self.assertEqual(result.code, 3, result)
        self.assertNotIn("new", self.config_data()["accounts"])

    def test_t7_dry_run_and_misplaced_name(self):
        result = self.ok("add", "new", "--config-from", "work", "--dry-run")
        self.assertIn("would copy", result.out)
        self.assertFalse(os.path.exists(self.target()))
        misplaced = self.run_cli("add", "--shared", "new", "--config-from", "work")
        self.assertEqual(misplaced.code, 2)
        self.assertIn("put the account name before the options", misplaced.err)


class RestoreTest(MigrateDefaultBase):
    def setUp(self):
        super().setUp()
        self.global_state = self.write(os.path.join(self.home, ".claude.json"), '{"numStartups": 1}', 0o600)
        self.shared = os.path.join(self.home, "shared")
        self.write(os.path.join(self.shared, "skills", "s.md"), "s")
        self.ok("init", "--shared-dir", self.shared)
        self.ok("migrate-default", "main")
        self.ok("set", "main", "--shared")
        self.target = os.path.join(self.root, "main")
        self.launcher = os.path.join(self.bin, "claude-main")
        self.assertTrue(os.path.islink(self.source))
        self.security_before = len(self.security_calls())

    def assert_restored(self):
        self.assertFalse(os.path.islink(self.source))
        self.assertTrue(os.path.isdir(self.source))
        self.assertFalse(os.path.lexists(self.target))
        self.assertTrue(os.path.isfile(os.path.join(self.source, ".credentials.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.source, "projects", "p", "a.jsonl")))
        self.assertTrue(os.path.islink(os.path.join(self.source, "skills")))
        self.assertNotIn("main", self.config_data()["accounts"])
        self.assertFalse(os.path.lexists(self.launcher))
        with open(self.global_state) as handle:
            self.assertEqual(handle.read(), '{"numStartups": 1}')

    def test_t8_restore_and_rerun(self):
        result = self.ok("restore", "main")
        self.assertIn("moved {} to {}".format(self.target, self.source), result.out)
        self.assert_restored()
        self.assertEqual(len(self.security_calls()), self.security_before)
        again = self.ok("restore", "main")
        self.assertIn("nothing to restore", again.out)

    def test_t9_resume_after_crash(self):
        for point in ("restore-unlinked", "restore-moved"):
            with self.subTest(point=point):
                if not os.path.islink(self.source):
                    self.ok("migrate-default", "main")
                    self.ok("set", "main", "--shared")
                crashed = self.run_cli("restore", "main", env={"MULTI_CLAUDE_TEST_CRASH_AT": point})
                self.assertEqual(crashed.code, CRASH, crashed)
                self.ok("restore", "main")
                self.assert_restored()

    def test_t9_leftover_launcher_is_removed(self):
        os.unlink(self.source)
        os.rename(self.target, self.source)
        data = self.config_data()
        del data["accounts"]["main"]
        with open(os.path.join(self.state, "config.json"), "w") as handle:
            json.dump(data, handle)
        self.assertTrue(os.path.exists(self.launcher))
        self.ok("restore", "main")
        self.assertFalse(os.path.lexists(self.launcher))

    def test_t10_refusals(self):
        self.ok("add", "work")
        self.assertEqual(self.run_cli("restore", "work").code, 2)
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project)
        self.ok("route", project, "main")
        self.assertEqual(self.run_cli("restore", "main").code, 3)
        self.ok("route", project, "--remove")
        result = self.run_cli("restore", "main", env={"MULTI_CLAUDE_TEST_RESTORE_CROSS_DEVICE": "1"})
        self.assertEqual(result.code, 3, result)
        self.assertIn("different file systems", result.err)
        self.assertTrue(os.path.islink(self.source))
        # S 指向别处
        os.unlink(self.source)
        os.symlink(os.path.join(self.root, "work"), self.source)
        self.assertEqual(self.run_cli("restore", "main").code, 3)
        # S 与 T 都是目录
        os.unlink(self.source)
        os.makedirs(self.source)
        self.assertEqual(self.run_cli("restore", "main").code, 3)
        self.assertTrue(os.path.isdir(self.target))

    def test_t10_target_is_link_and_journal(self):
        real = os.path.join(self.tmp, "real-main")
        os.rename(self.target, real)
        os.symlink(real, self.target)
        self.assertEqual(self.run_cli("restore", "main").code, 3)
        os.unlink(self.target)
        os.rename(real, self.target)
        with open(os.path.join(self.state, "migrate-journal.json"), "w") as handle:
            json.dump({"name": "main", "source": "s", "target": "t", "backup": "b", "mode": "rename",
                       "phase": "planned"}, handle)
        for extra in ((), ("--dry-run",)):
            result = self.run_cli("restore", "main", *extra)
            self.assertEqual(result.code, 1, result)
        self.assertTrue(os.path.islink(self.source))

    def test_t11_busy_and_skip(self):
        result = self.run_cli("restore", "main", env={"CLAUDE_CODE_CHILD_SESSION": "1"})
        self.assertEqual(result.code, 4, result)
        self.assertIn("run restore from a plain terminal", result.err)
        self.assertTrue(os.path.islink(self.source))
        self.ok("restore", "main", "--skip-process-check", env={"CLAUDE_CODE_CHILD_SESSION": "1"})
        self.assert_restored()

    def test_t11_busy_directory(self):
        holder = subprocess.Popen(["sleep", "30"], cwd=self.target)
        try:
            result = self.run_cli("restore", "main")
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(result.code, 4, result)
        self.assertTrue(os.path.islink(self.source))

    def test_t11_busy_after_unlink(self):
        # 状态 B（软链已删、目录未搬）：S 不存在，占用检查必须按账号目录查，否则会漏掉
        crashed = self.run_cli("restore", "main", env={"MULTI_CLAUDE_TEST_CRASH_AT": "restore-unlinked"})
        self.assertEqual(crashed.code, CRASH, crashed)
        holder = subprocess.Popen(["sleep", "30"], cwd=self.target)
        try:
            result = self.run_cli("restore", "main")
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(result.code, 4, result)
        self.assertTrue(os.path.isdir(self.target))
        self.ok("restore", "main")
        self.assert_restored()

    def test_t12_dry_run(self):
        before = self.snapshot(self.home)
        result = self.ok("restore", "main", "--dry-run")
        for text in ("would remove the link", "would move", "would unregister main"):
            self.assertIn(text, result.out)
        self.assertEqual(before, self.snapshot(self.home))


class HelpCompletionTest(CliTestCase):
    def test_t13_help_and_completion(self):
        help_text = self.ok("--help").out
        self.assertIn("run, path", help_text)
        self.assertIn("migrate-default, restore", help_text)
        for shell in ("bash", "zsh", "fish"):
            script = self.ok("completion", shell).out
            for word in ("run", "path", "restore", "--config-from"):
                self.assertIn(word, script, (shell, word))


if __name__ == "__main__":
    unittest.main()
