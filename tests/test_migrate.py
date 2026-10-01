"""方案 §8 第 3、11、12、13 条及 multi-codex 移植用例：默认目录迁移、default 身份启动命令、占用检查。

中断用 MULTI_CLAUDE_TEST_CRASH_AT 让子进程在注入点 os._exit(137)，模拟进程被杀；
回滚用 MULTI_CLAUDE_TEST_FAIL_AT 抛出可捕获异常。两者可以同时设置。
需要在同一次运行中改动文件系统的场景（S 被重建、EXDEV、校验失败）改为进程内调用并替换函数。
"""

import errno
import io
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import migrate  # noqa: E402

CRASH = 137


class MigrateBase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.source = os.path.join(self.home, ".claude")
        self.target = os.path.join(self.root, "main")
        self.write(os.path.join(self.source, ".credentials.json"), "{}", 0o600)
        self.write(os.path.join(self.source, "history.jsonl"), "db" * 1000)
        self.write(os.path.join(self.source, "projects", "p", "a.jsonl"), "line\n")
        os.symlink("projects", os.path.join(self.source, "projects-link"))
        # 默认账号的全局状态文件在家目录，迁移必须原地不动（方案 §5.1.6）。
        self.global_state = self.write(os.path.join(self.home, ".claude.json"), '{"numStartups": 1}', 0o600)
        self.global_stat = os.stat(self.global_state)
        self.original = self._tree(self.source)

    def _tree(self, root):
        """以相对路径表示的目录树内容，用于比较迁移前后数据是否一致。"""
        snap = self.snapshot(root)
        result = {}
        for path, value in snap.items():
            rel = os.path.relpath(path, root)
            if value[0] == "file":
                value = ("file", value[1], value[3])  # 去掉 mtime：copy2 会保留，但 rename 前后都一致
            result[rel] = value
        return result

    def backups(self):
        return [name for name in os.listdir(self.home) if ".multi-claude-bak." in name]

    def assert_migrated(self):
        self.assertTrue(os.path.islink(self.source), "source should be a link")
        self.assertEqual(os.path.realpath(self.source), os.path.realpath(self.target))
        self.assertEqual(self.original, self._tree(self.target))
        self.assertIsNone(self.journal())
        with open(os.path.join(self.bin, "claude-main")) as handle:
            content = handle.read()
        self.assertIn("default_dir=", content)
        self.assertNotIn('CLAUDE_CONFIG_DIR="$account_dir"', content)
        self.assertEqual(self.config_data()["accounts"]["main"]["identity"], "default")
        after = os.stat(self.global_state)
        self.assertEqual((after.st_ino, after.st_mtime_ns), (self.global_stat.st_ino, self.global_stat.st_mtime_ns))
        with open(self.global_state) as handle:
            self.assertEqual(handle.read(), '{"numStartups": 1}')

    def migrate(self, *extra, env=None):
        return self.run_cli("migrate-default", "main", *extra, env=env)


class RenameMigrationTest(MigrateBase):
    def test_rename_and_rerun(self):
        result = self.migrate()
        self.assertEqual(result.code, 0, result)
        self.assert_migrated()
        self.assertEqual(os.stat(os.path.join(self.target, ".credentials.json")).st_mode & 0o777, 0o600)
        again = self.migrate()
        self.assertEqual(again.code, 0, again)
        self.assertIn("already migrated", again.out)

    def test_source_link_without_registration(self):
        os.makedirs(self.root)
        os.rename(self.source, self.target)
        os.symlink(self.target, self.source)
        result = self.migrate()
        self.assertEqual(result.code, 0, result)
        self.assert_migrated()

    def test_conflicts_before_moving(self):
        cases = []
        os.makedirs(self.target)
        cases.append("target exists")
        before = self.snapshot()
        self.assertEqual(self.migrate().code, 3)
        self.assertEqual(before, self.snapshot())
        os.rmdir(self.target)
        os.symlink("/nonexistent", self.target)  # 断开的软链也算存在
        self.assertEqual(self.migrate().code, 3)
        os.unlink(self.target)
        self.write(os.path.join(self.bin, "claude-main"), "#!/bin/sh\n", 0o755)
        before = self.snapshot()
        result = self.migrate()
        self.assertEqual(result.code, 3, result)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(os.path.isdir(self.source) and not os.path.islink(self.source))

    def test_case_insensitive_registered_name(self):
        self.ok("add", "Main")
        self.assertEqual(self.migrate().code, 3)

    def test_config_dir_warning(self):
        result = self.migrate(env={"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "elsewhere"),
                                   "ANTHROPIC_API_KEY": "secret-value", "CLAUDE_SECURESTORAGE_CONFIG_DIR": "x"})
        self.assertEqual(result.code, 0, result)
        self.assertIn("CLAUDE_CONFIG_DIR is set", result.err)
        self.assertIn("ANTHROPIC_API_KEY", result.err)
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR is set", result.err)
        self.assertNotIn("secret-value", result.err + result.out)
        self.assert_migrated()

    def test_config_dir_pointing_to_source(self):
        before = self.snapshot()
        result = self.migrate(env={"CLAUDE_CONFIG_DIR": self.source})
        self.assertEqual(result.code, 4, result)
        self.assertIn("unset CLAUDE_CONFIG_DIR", result.err)
        self.assertIn("usage=config-dir", result.err)
        self.assertEqual(before, self.snapshot())

    def test_another_default_account(self):
        os.makedirs(os.path.join(self.root, "other"))
        self.write(os.path.join(self.state, "config.json"), json.dumps(
            {"version": 1, "accounts": {"other": {"identity": "default"}}}))
        before = self.snapshot()
        result = self.migrate()
        self.assertEqual(result.code, 3, result)
        self.assertIn("another account already has the default identity", result.err)
        self.assertEqual(before, self.snapshot())

    def test_source_option_removed(self):
        self.assertEqual(self.migrate("--source", self.source).code, 2)

    def test_apply_file_keeps_identity(self):
        self.assertEqual(self.migrate().code, 0)
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(
            {"version": 1, "accounts": {"main": {"identity": "dir", "proxy": "7901"}}}))
        self.ok("apply", "-f", path)
        self.assertEqual(self.config_data()["accounts"]["main"]["proxy"], "http://127.0.0.1:7901")
        self.assert_migrated()


class ApplyDefaultIdentityTest(MigrateBase):
    def test_unregistered_default_account_is_skipped(self):
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(
            {"version": 1, "accounts": {"main": {"identity": "default", "proxy": "7901"}, "work": {}}}))
        result = self.ok("apply", "-f", path)
        self.assertIn("skip account main (identity is default; run multi-claude migrate-default main", result.out)
        self.assertEqual(list(self.config_data()["accounts"]), ["work"])
        self.assertFalse(os.path.lexists(self.target))
        self.assertFalse(os.path.lexists(os.path.join(self.bin, "claude-main")))
        self.assertEqual(self.migrate().code, 0)
        self.assert_migrated()
        self.assertEqual(self.config_data()["accounts"]["main"]["proxy"], "inherit")


class DefaultLauncherTest(MigrateBase):
    """§8 第 3 条：default 身份启动命令。"""

    def test_does_not_set_config_dir(self):
        self.assertEqual(self.migrate("--proxy", "7901").code, 0)
        code, args, env, _ = self.run_launcher("main", "hi", env={"CLAUDE_CONFIG_DIR": "/elsewhere",
                                                                  "CLAUDE_SECURESTORAGE_CONFIG_DIR": "x"})
        self.assertEqual(code, 0)
        self.assertEqual(args, ["hi"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("CLAUDE_SECURESTORAGE_CONFIG_DIR", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7901")
        listing = self.ok("list").out
        self.assertRegex(listing, r"main\s+default\s+ok\s+\S+\s+no\s+ok\s+\S+\s+ok")
        self.assertIn(os.path.join(self.home, ".claude.json"), listing)

    def test_broken_or_redirected_link(self):
        self.assertEqual(self.migrate().code, 0)
        os.unlink(self.source)
        code, args, _, stderr = self.run_launcher("main")
        self.assertEqual(code, 1)
        self.assertEqual(args, [])
        self.assertIn("no longer points to", stderr)
        self.assertRegex(self.ok("list").out, r"main .* missing")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.source)
        code, args, _, _ = self.run_launcher("main")
        self.assertEqual(code, 1)
        self.assertEqual(args, [])
        self.assertRegex(self.ok("list").out, r"main .* broken")

    def test_paths_with_space_and_quote(self):
        home = os.path.join(self.tmp, "my h'ome")
        os.makedirs(home)
        os.rename(self.source, os.path.join(home, ".claude"))
        env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}
        self.ok("init", "--root", os.path.join(self.tmp, "r o'ot"), env=env)
        self.ok("migrate-default", "main", env=env)
        proc = subprocess.run([os.path.join(home, ".local", "bin", "claude-main"), "x"], env=self._merged(env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.fake_out + ".args") as handle:
            self.assertEqual(handle.read(), "x\n")


class CopyMigrationTest(MigrateBase):
    def test_copy_skips_special_files_and_removes_backup(self):
        cwd = os.getcwd()
        os.chdir(self.source)
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.bind("ipc.sock")
        finally:
            os.chdir(cwd)
        os.mkfifo(os.path.join(self.source, "fifo"))
        self.original = self._tree(self.source)
        del self.original["ipc.sock"], self.original["fifo"]
        try:
            result = self.migrate("--copy")
        finally:
            sock.close()
        self.assertEqual(result.code, 0, result)
        self.assertIn("skipped special file", result.out)
        self.assert_migrated()
        self.assertEqual(self.backups(), [])

    def test_unreadable_subdirectory_aborts_copy(self):
        locked = os.path.join(self.source, "locked")
        self.write(os.path.join(locked, "data.txt"), "data")
        os.chmod(locked, 0)
        self.addCleanup(lambda: os.path.isdir(locked) and os.chmod(locked, 0o755))
        result = self.migrate("--copy", "--skip-process-check")
        os.chmod(locked, 0o755)
        self.assertEqual(result.code, 1, result)
        self.assertTrue(os.path.isdir(self.source) and not os.path.islink(self.source))
        with open(os.path.join(locked, "data.txt")) as handle:
            self.assertEqual(handle.read(), "data")
        self.assertFalse(os.path.lexists(self.target))
        self.assertIsNone(self.journal())
        self.assertEqual(self.backups(), [])

    def test_resume_reverify_detects_changed_source(self):
        """续跑表第 8 行的失败分支：复制后 S 又变了，重新校验不通过，删掉 T 重新复制。"""
        self.assertEqual(self.migrate("--copy", env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-moved"}).code, CRASH)
        self.write(os.path.join(self.source, "history.jsonl"), "changed after copy")
        self.original = self._tree(self.source)
        result = self.migrate()
        self.assertEqual(result.code, 0, result)
        self.assertIn("copying again", result.out)
        self.assert_migrated()
        self.assertEqual(self.backups(), [])

    def test_keep_backup(self):
        result = self.migrate("--copy", "--keep-backup")
        self.assertEqual(result.code, 0, result)
        self.assert_migrated()
        self.assertEqual(len(self.backups()), 1)

    def test_rollback_after_park(self):
        result = self.migrate("--copy", env={"MULTI_CLAUDE_TEST_FAIL_AT": "journal-parked"})
        self.assertEqual(result.code, 1, result)
        self.assertTrue(os.path.isdir(self.source) and not os.path.islink(self.source))
        self.assertEqual(self.original, self._tree(self.source))
        self.assertFalse(os.path.exists(self.target))
        self.assertIsNone(self.journal())
        self.assertEqual(self.migrate("--copy").code, 0)
        self.assert_migrated()

    def test_rollback_interrupted_at_each_step(self):
        for point in ("journal-rolled-back", "rollback-restore", "rollback-remove-target"):
            with self.subTest(point=point):
                result = self.migrate("--copy", env={"MULTI_CLAUDE_TEST_FAIL_AT": "journal-parked",
                                                     "MULTI_CLAUDE_TEST_CRASH_AT": point})
                self.assertEqual(result.code, CRASH, result)
                self.assertIsNotNone(self.journal())
                rerun = self.migrate("--copy")
                self.assertEqual(rerun.code, 0, rerun)
                self.assert_migrated()
                self._undo()

    def _undo(self):
        """把已迁移的状态还原成初始状态，供下一个子用例使用。"""
        os.unlink(self.source)
        os.rename(self.target, self.source)
        os.unlink(os.path.join(self.bin, "claude-main"))
        os.unlink(os.path.join(self.state, "config.json"))

    def test_source_busy_before_park(self):
        # 复制完成后、park 前被占用：第一次在 copy-done 处崩溃模拟，然后占用 S 再续跑。
        self.assertEqual(self.migrate("--copy", env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-moved"}).code, CRASH)
        holder = subprocess.Popen(["sleep", "30"], cwd=self.source)
        try:
            time.sleep(0.2)
            result = self.migrate()
            self.assertEqual(result.code, 4, result)
            self.assertIn('"phase": "moved"', self.journal())
        finally:
            holder.kill()
            holder.wait()
        result = self.migrate()
        self.assertEqual(result.code, 0, result)
        self.assert_migrated()


class InterruptionTest(MigrateBase):
    RENAME_POINTS = ["journal-planned", "rename-target", "journal-moved", "link", "journal-linked",
                     "register", "journal-registered"]
    COPY_POINTS = ["journal-planned", "journal-copying", "copy-midway", "copy-done", "journal-moved",
                   "rename-backup", "journal-parked", "link", "journal-linked", "register",
                   "journal-registered", "remove-backup"]

    def _check_points(self, points, extra):
        for point in points:
            with self.subTest(point=point):
                result = self.migrate(*extra, env={"MULTI_CLAUDE_TEST_CRASH_AT": point})
                self.assertEqual(result.code, CRASH, result)
                rerun = self.migrate(*extra)
                self.assertEqual(rerun.code, 0, rerun)
                self.assert_migrated()
                self.assertEqual(self.backups(), [])
                os.unlink(self.source)
                os.rename(self.target, self.source)
                os.unlink(os.path.join(self.bin, "claude-main"))
                os.unlink(os.path.join(self.state, "config.json"))

    def test_rename_mode(self):
        self._check_points(self.RENAME_POINTS, [])

    def test_copy_mode(self):
        self._check_points(self.COPY_POINTS, ["--copy"])

    def test_rebuilt_source_after_park_row4(self):
        self.assertEqual(self.migrate("--copy", env={"MULTI_CLAUDE_TEST_CRASH_AT": "rename-backup"}).code, CRASH)
        os.makedirs(self.source)
        before = self.snapshot()
        result = self.migrate()
        self.assertEqual(result.code, 1, result)
        self.assertIn("was recreated", result.err)
        self.assertEqual(before, self.snapshot())
        os.rmdir(self.source)
        self.assertEqual(self.migrate().code, 0)
        self.assert_migrated()

    def test_rebuilt_source_after_rename_row9(self):
        self.assertEqual(self.migrate(env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-moved"}).code, CRASH)
        os.makedirs(self.source)
        result = self.migrate()
        self.assertEqual(result.code, 1, result)
        self.assertIn("to abandon this migration", result.err)
        os.rmdir(self.source)
        self.assertEqual(self.migrate().code, 0)
        self.assert_migrated()

    def test_unknown_state_row10(self):
        self.assertEqual(self.migrate(env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-moved"}).code, CRASH)
        os.rename(self.target, os.path.join(self.tmp, "moved-away"))
        before = self.snapshot()
        result = self.migrate()
        self.assertEqual(result.code, 1, result)
        self.assertIn("cannot determine", result.err)
        self.assertEqual(before, self.snapshot())

    def test_resume_uses_recorded_options(self):
        result = self.migrate("--copy", "--keep-backup", env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-parked"})
        self.assertEqual(result.code, CRASH)
        rerun = self.migrate()
        self.assertEqual(rerun.code, 0, rerun)
        self.assertIn("options differ", rerun.err)
        self.assertEqual(len(self.backups()), 1)

    def test_resume_with_other_name(self):
        self.assertEqual(self.migrate(env={"MULTI_CLAUDE_TEST_CRASH_AT": "journal-moved"}).code, CRASH)
        self.assertEqual(self.run_cli("migrate-default", "other").code, 2)


class BusyCheckTest(MigrateBase):
    def _assert_busy(self, holder, usage_and_path):
        try:
            time.sleep(0.3)
            before = self.snapshot()
            result = self.migrate()
            self.assertEqual(result.code, 4, result)
            line = [row for row in result.err.splitlines() if "pid={}".format(holder.pid) in row]
            self.assertEqual(len(line), 1, result)
            self.assertIn(usage_and_path, line[0])
            self.assertEqual(before, self.snapshot())
        finally:
            holder.kill()
            holder.wait()

    def test_open_file(self):
        code = "import time; f = open({!r}); time.sleep(30)".format(os.path.join(self.source, "history.jsonl"))
        self._assert_busy(subprocess.Popen([sys.executable, "-c", code]),
                          "path=" + os.path.join(self.source, "history.jsonl"))

    def test_working_directory(self):
        self._assert_busy(subprocess.Popen(["sleep", "30"], cwd=self.source), "usage=cwd path=" + self.source)

    def test_same_prefix_is_not_busy(self):
        sibling = os.path.join(self.home, ".claude-shared")
        os.makedirs(sibling)
        holder = subprocess.Popen(["sleep", "30"], cwd=sibling)
        try:
            time.sleep(0.3)
            self.assertEqual(self.migrate().code, 0)
        finally:
            holder.kill()
            holder.wait()

    def test_check_failure_and_skip(self):
        fake_lsof = self.write(os.path.join(self.fakebin, "lsof"), "#!/bin/sh\necho broken >&2\nexit 1\n", 0o755)
        self.assertTrue(os.path.exists(fake_lsof))
        result = self.migrate()
        self.assertEqual(result.code, 1, result)
        self.assertIn("--skip-process-check", result.err)
        self.assertEqual(self.migrate("--skip-process-check").code, 0)
        self.assert_migrated()

    @unittest.skipUnless(sys.platform.startswith("linux"), "the /proc fallback exists only on Linux")
    def test_proc_fallback_without_lsof(self):
        env = {"PATH": self.fakebin + ":/nonexistent"}
        holder = subprocess.Popen(["sleep", "30"], cwd=self.source)
        try:
            time.sleep(0.3)
            result = self.migrate(env=env)
            self.assertEqual(result.code, 4, result)
        finally:
            holder.kill()
            holder.wait()


class ClaudeBusyCheckTest(MigrateBase):
    """§8 第 12 条：Claude 进程、继承了配置目录的进程、IDE 锁、后台服务、Claude 会话内运行。"""

    def setUp(self):
        super().setUp()
        self.script = self.write(os.path.join(self.tmp, "x", "claude"),
                                 "import time\nwhile True:\n    time.sleep(1)\n")
        self.other_home = os.path.join(self.tmp, "other-home")
        os.makedirs(self.other_home)
        self.python = _real_interpreter()

    def spawn(self, argv0, env=None, args=None):
        """以 argv[0]=argv0 启动一个常驻进程（默认运行伪 claude 脚本）。"""
        full_env = {"HOME": self.home, "PATH": "/usr/bin:/bin"}
        full_env.update(env or {})
        command = " ".join(_quote(part) for part in (args or [self.python, self.script]))
        holder = subprocess.Popen(["bash", "-c", "exec -a {} {}".format(_quote(argv0), command)],
                                  env=full_env, cwd=self.tmp)
        self.addCleanup(_stop, holder)
        time.sleep(0.4)
        # 伪装失败时（解释器又 exec 了一次，argv[0] 被换掉）后面的断言会莫名其妙，这里先说清楚。
        shown = subprocess.run(["ps", "-o", "command=", "-p", str(holder.pid)], stdout=subprocess.PIPE,
                               universal_newlines=True).stdout.strip()
        self.assertTrue(shown.startswith(argv0), "argv[0] was not kept: {!r} (interpreter {})".format(
            shown, self.python))
        return holder

    def assert_busy(self, holder, usage):
        before = self.snapshot()
        result = self.migrate()
        self.assertEqual(result.code, 4, result)
        lines = [row for row in result.err.splitlines() if "pid={} ".format(holder.pid) in row]
        self.assertEqual(len(lines), 1, result)
        self.assertIn("usage=" + usage, lines[0])
        self.assertEqual(before, self.snapshot())
        return result

    def test_claude_process_of_any_account_under_same_home(self):
        other_dir = os.path.join(self.tmp, "acct dir")
        holder = self.spawn("claude", {"CLAUDE_CONFIG_DIR": other_dir})
        result = self.assert_busy(holder, "claude-process path=" + other_dir)
        self.assertIn("CLAUDE_CONFIG_DIR='{}' claude daemon stop --any".format(other_dir), result.err)

    def test_claude_process_under_other_home_is_ignored(self):
        self.spawn("claude", {"HOME": self.other_home})
        self.assertEqual(self.migrate().code, 0)
        self.assert_migrated()

    def test_process_forms(self):
        forms = [
            ("node", True),
            (os.path.join(self.tmp, "claude", "versions", "9.9.9"), True),
            ("claude bg-pty-host", True),
            (os.path.join(self.tmp, "lib", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"), True),
            ("ssh", False),
        ]
        for argv0, busy in forms:
            with self.subTest(argv0=argv0):
                holder = self.spawn(argv0)
                if busy:
                    result = self.assert_busy(holder, "claude-process")
                    self.assertIn("env -u CLAUDE_CONFIG_DIR claude daemon stop --any", result.err)
                else:
                    result = self.migrate("--dry-run")
                    self.assertEqual(result.code, 0, result)
                _stop(holder)

    def test_inherited_config_dir(self):
        # 用 python 而不是 /bin/sleep：macOS 对 Apple 自带程序隐去环境，读不到这个变量。
        sleeper = [self.python, "-c", "import time; time.sleep(60)"]
        holder = self.spawn("sleeper", {"CLAUDE_CONFIG_DIR": self.source}, args=sleeper)
        self.assert_busy(holder, "config-dir path=" + self.source)
        _stop(holder)
        self.spawn("sleeper", {"CLAUDE_CONFIG_DIR": self.other_home}, args=sleeper)
        self.assertEqual(self.migrate().code, 0)
        self.assert_migrated()

    def test_ide_lock(self):
        holder = self.spawn("sleep", {"HOME": self.other_home}, args=["sleep", "60"])
        lock = self.write(os.path.join(self.source, "ide", "1.lock"), json.dumps(
            {"pid": holder.pid, "ideName": "Editor", "authToken": "token-value"}))
        result = self.assert_busy(holder, "ide-lock")
        self.assertNotIn("token-value", result.err + result.out)
        _stop(holder)
        self.write(lock, "{broken")
        self.original = self._tree(self.source)
        result = self.migrate()
        self.assertEqual(result.code, 0, result)
        self.assertIn("cannot parse IDE lock file", result.err)

    def test_ide_lock_with_dead_pid(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write(os.path.join(self.source, "ide", "1.lock"), json.dumps({"pid": dead.pid}))
        self.original = self._tree(self.source)
        self.assertEqual(self.migrate().code, 0)
        self.assert_migrated()

    def test_daemon_service(self):
        # macOS 是 LaunchAgent；Linux 是 systemd user unit，位置随 XDG_CONFIG_HOME（基础环境里指向临时目录）。
        if sys.platform == "darwin":
            service = os.path.join(self.home, "Library", "LaunchAgents", "com.anthropic.claude-daemon.plist")
        else:
            service = os.path.join(self.env["XDG_CONFIG_HOME"], "systemd", "user", "com.anthropic.claude-daemon.service")
        self.write(service)
        before = self.snapshot(self.source)
        result = self.migrate()
        self.assertEqual(result.code, 4, result)
        self.assertIn("usage=daemon-service path=" + service, result.err)
        self.assertIn("claude daemon uninstall", result.err)
        self.assertEqual(before, self.snapshot(self.source))

    def test_inside_claude_session(self):
        before = self.snapshot()
        result = self.migrate(env={"CLAUDE_CODE_CHILD_SESSION": "1"})
        self.assertEqual(result.code, 4, result)
        self.assertIn("usage=inside-claude-session", result.err)
        self.assertIn("env -u CLAUDE_CODE_CHILD_SESSION", result.err)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.migrate("--dry-run", env={"CLAUDECODE": "1"}).code, 0)

    def test_skip_process_check_skips_everything(self):
        self.spawn("claude")
        result = self.migrate("--skip-process-check", env={"CLAUDE_CODE_CHILD_SESSION": "1"})
        self.assertEqual(result.code, 0, result)
        self.assert_migrated()

    @unittest.skipUnless(sys.platform.startswith("linux"), "/proc/1/comm exists only on Linux")
    def test_bwrap_pid_namespace(self):
        result = self.migrate(env={"MULTI_CLAUDE_TEST_PROC1_COMM": "bwrap\n"})
        self.assertEqual(result.code, 1, result)
        self.assertIn("--skip-process-check", result.err)
        self.assertEqual(self.migrate("--dry-run", env={"MULTI_CLAUDE_TEST_PROC1_COMM": "systemd\n"}).code, 0)
        hook_off = {"MULTI_CLAUDE_TEST_PROC1_COMM": "bwrap\n", "MULTI_CLAUDE_TEST_MODE": None}
        self.assertEqual(self.migrate("--dry-run", env=hook_off).code, 0)


def _real_interpreter():
    """能保住 `exec -a` 所设 argv[0] 的 Python 解释器。

    macOS 上 /usr/bin/python3 是 xcrun 跳转程序；python.org 与 setup-python 的 framework 版本里，
    bin/python3.x 也只是跳板，会再 exec 到 Python.app 里的真实解释器。两者都会把 argv[0] 换掉，
    所以优先用 framework 内的真实解释器。
    """
    framework_python = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
    if sys.platform == "darwin" and os.path.isfile(framework_python):
        return framework_python
    return os.path.realpath(sys.executable)


def _quote(value):
    return "'" + value.replace("'", "'\\''") + "'"


def _stop(process):
    if process.poll() is None:
        process.kill()
        process.wait()


class InProcessTest(MigrateBase):
    """需要在同一次运行中改动文件系统的场景。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, self.env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("MULTI_CLAUDE_TEST_CRASH_AT", "MULTI_CLAUDE_TEST_FAIL_AT"):
            os.environ.pop(name, None)
        # 在 Claude 会话里跑测试时，本进程环境带着 CLAUDE_* 变量；进程内调用不能让它们影响迁移。
        for name in [key for key in os.environ if key.startswith("CLAUDE")]:
            os.environ.pop(name)

    def call(self, copy=False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = migrate.migrate_default("main", None, copy, False, None, True, False)
        return code, out.getvalue(), err.getvalue()

    def test_source_rebuilt_before_link(self):
        real_hook = migrate._test_hook

        def hook(point):
            if point == "journal-moved":
                os.makedirs(self.source)
            real_hook(point)

        with mock.patch.object(migrate, "_test_hook", hook):
            code, _, err = self.call()
        self.assertEqual(code, 1, err)
        self.assertIn("was recreated", err)
        self.assertIn('"phase": "moved"', self.journal())
        self.assertEqual(self.original, self._tree(self.target))
        os.rmdir(self.source)
        code, out, err = self.call()
        self.assertEqual(code, 0, err)
        self.assert_migrated()

    def test_exdev_switches_to_copy_and_resumes(self):
        real_rename = os.rename

        def rename(src, dst):
            if src == self.source and dst == self.target:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(src, dst)

        class Killed(BaseException):
            pass

        real_hook = migrate._test_hook

        def hook(point):
            if point == "copy-midway":
                raise Killed()
            real_hook(point)

        with mock.patch.object(migrate.os, "rename", rename), mock.patch.object(migrate, "_test_hook", hook):
            with self.assertRaises(Killed):
                self.call()
        journal = json.loads(self.journal())
        self.assertEqual(journal["mode"], "copy")
        self.assertEqual(journal["phase"], "copying")
        code, _, err = self.call()
        self.assertEqual(code, 0, err)
        self.assert_migrated()

    def test_backup_cleanup_failure_only_warns(self):
        real_remove = migrate.remove_path

        def remove(path):
            if ".multi-claude-bak." in path:
                raise TypeError("simulated rmtree failure")
            return real_remove(path)

        with mock.patch.object(migrate, "remove_path", remove):
            code, _, err = self.call(copy=True)
        self.assertEqual(code, 0, err)
        self.assertIn("could not remove backup", err)
        self.assert_migrated()

    def test_rollback_failure_keeps_journal(self):
        """回滚中删除 T 失败（可捕获异常）：事务记录停在 rolled-back，重跑先完成回滚再迁移。"""
        real_remove = migrate.remove_path

        def remove(path):
            if path == self.target:
                raise OSError("simulated failure removing the copy")
            return real_remove(path)

        with mock.patch.dict(os.environ, {"MULTI_CLAUDE_TEST_FAIL_AT": "journal-parked"}), \
                mock.patch.object(migrate, "remove_path", remove):
            code, _, err = self.call(copy=True)
        self.assertEqual(code, 1, err)
        self.assertIn("rollback failed", err)
        self.assertIn('"phase": "rolled-back"', self.journal())
        self.assertEqual(self.original, self._tree(self.source))
        code, _, err = self.call(copy=True)
        self.assertEqual(code, 0, err)
        self.assert_migrated()

    def test_verification_failure(self):
        with mock.patch.object(migrate, "diff_manifests", lambda a, b: ["x"]):
            code, _, err = self.call(copy=True)
        self.assertEqual(code, 1, err)
        self.assertIn("verification failed", err)
        self.assertFalse(os.path.exists(self.target))
        self.assertEqual(self.original, self._tree(self.source))
        self.assertIsNone(self.journal())


if __name__ == "__main__":
    unittest.main()
