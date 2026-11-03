"""方案 feature-windows-support §8：Windows 专属行为。

两类用例：
- 纯逻辑（模板、命令行解析、包装前缀、环境还原）：用 mock 把 IS_WINDOWS 打开，任何平台都能跑，
  保证 macOS / Linux 的开发者改这些代码时本机就能发现问题；
- 依赖 Windows 系统调用（junction、msvcrt 锁、OpenProcess、.cmd 启动、PowerShell）：只在 Windows 上跑。
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import SRC, TEXT_KWARGS, CliTestCase, windows_only

sys.path.insert(0, SRC)

from multi_claude import (__version__, cli, completion, config, fsutil, hook, launcher, migrate, platform,  # noqa: E402
                          routes, statusline)


class CmdTemplateTest(unittest.TestCase):
    def test_render_windows(self):
        text = launcher.render_windows("work", r"C:\Users\me\.cc\work", r"C:\Py\python.exe", r"C:\pkg\_entry.py")
        self.assertEqual(text.split("\n"), [
            "@echo off",
            "@rem managed-by: multi-claude account=work",
            r"@rem account_dir=C:\Users\me\.cc\work",
            r'"C:\Py\python.exe" -I "C:\pkg\_entry.py" launch work -- %*',
            "exit /b %ERRORLEVEL%",
            "",
        ])

    def test_unsafe_chars(self):
        self.assertIsNone(launcher.cmd_unsafe_reason([r"C:\Users\me", r"C:\a b\(x)"]))
        for char in ('"', "%", "!"):
            self.assertIn(repr(char), launcher.cmd_unsafe_reason([r"C:\ok", "C:\\a" + char + "b"]))

    def test_non_ascii_command_paths_rejected(self):
        # 账号目录只在 @rem 行里，可以有中文；cmd 真正执行的路径不行
        self.assertIsNone(launcher.cmd_unsafe_reason(["C:\\用户\\.cc\\work"], ["C:\\Py\\python.exe"]))
        reason = launcher.cmd_unsafe_reason(["C:\\ok"], ["C:\\用户\\python.exe"])
        self.assertIn("non-ASCII", reason)

    def test_non_ascii_user_dir_written_as_variable(self):
        # 中文代码页下“张三”这类目录没有 8.3 短名，只能写成 %USERPROFILE% 开头（2026-11-03 实机）
        env = {"USERPROFILE": "C:\\Users\\张三", "LOCALAPPDATA": "C:\\Users\\张三\\AppData\\Local",
               "APPDATA": "C:\\Users\\张三\\AppData\\Roaming"}
        with mock.patch.dict(os.environ, env), mock.patch.object(platform, "short_path") as short:
            self.assertEqual(launcher._cmd_path("C:\\Users\\张三\\pipx\\venvs\\m\\Scripts\\python.exe"),
                             "%USERPROFILE%\\pipx\\venvs\\m\\Scripts\\python.exe")
            # 取最长的匹配目录，且不分大小写
            self.assertEqual(launcher._cmd_path("c:\\users\\张三\\appdata\\local\\pipx\\x.py"),
                             "%LOCALAPPDATA%\\pipx\\x.py")
            short.assert_not_called()
            # 中文不只在用户名里时，变量写法不成立，退回短名
            short.return_value = "C:\\SHORT~1\\x"
            self.assertEqual(launcher._cmd_path("C:\\Users\\张三\\工具\\x"), "C:\\SHORT~1\\x")
            # 只是前缀相同的兄弟目录不算
            self.assertEqual(launcher._cmd_path("C:\\Users\\张三2\\x"), "C:\\SHORT~1\\x")
        # 含 ! 的变量值不用：打开延迟展开的 cmd 会再解析展开结果里的 !
        with mock.patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\张!三", "LOCALAPPDATA": "", "APPDATA": ""}), \
                mock.patch.object(platform, "short_path", return_value="C:\\SHORT~1\\x"):
            self.assertEqual(launcher._cmd_path("C:\\Users\\张!三\\x"), "C:\\SHORT~1\\x")
        # 转小写后变长的字符（İ → i + 附加点）不能让余下部分被多切
        with mock.patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\İ", "LOCALAPPDATA": "", "APPDATA": ""}), \
                mock.patch.object(platform, "short_path") as short:
            self.assertEqual(launcher._cmd_path("C:\\Users\\İ\\pipx\\x.py"), "%USERPROFILE%\\pipx\\x.py")
            short.assert_not_called()
        command = "%USERPROFILE%\\pipx\\python.exe"
        self.assertIsNone(launcher.cmd_unsafe_reason([], [command]))
        self.assertIn(repr("%"), launcher.cmd_unsafe_reason([], ["%USERPROFILE%\\a%b"]))
        self.assertIn(repr("%"), launcher.cmd_unsafe_reason([], ["%PATH%\\x"]))

    def test_short_name_only_for_non_ascii(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": "D:\\home", "LOCALAPPDATA": "", "APPDATA": ""}), \
                mock.patch.object(platform, "short_path", return_value="C:\\SHORT~1\\x") as short:
            self.assertEqual(launcher._cmd_path("C:\\Users\\me\\site-packages\\x"),
                             "C:\\Users\\me\\site-packages\\x")
            short.assert_not_called()
            self.assertEqual(launcher._cmd_path("C:\\用户\\x"), "C:\\SHORT~1\\x")

    def test_marker_recognised_only_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "claude-work.cmd")
            content = launcher.render_windows("work", r"C:\cc\work", "py", "entry")
            with open(path, "w", newline="") as handle:
                handle.write(content.replace("\n", "\r\n"))
            with mock.patch.object(platform, "IS_WINDOWS", True):
                self.assertEqual(launcher.managed_account(path), "work")
                self.assertEqual(launcher.rendered_account_dir(content.replace("\n", "\r\n")), r"C:\cc\work")
            with mock.patch.object(platform, "IS_WINDOWS", False):
                # POSIX 的识别规则不变：.cmd 标记不算本工具管理的启动命令
                self.assertIsNone(launcher.managed_account(path))


class ProcessParsingTest(unittest.TestCase):
    def test_is_claude_process_windows_forms(self):
        cases = [
            ([r"C:\Users\me\.local\bin\claude.exe"], r"C:\Users\me\.local\bin\claude.exe", True),
            (["claude"], r"C:\Users\me\AppData\Local\claude\versions\2.1.306\claude.exe", True),
            ([r"C:\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe", "--print"], "", True),
            (["node", r"C:\npm\node_modules\@anthropic-ai\claude-code\cli.js"], r"C:\node\node.exe", True),
            (["ssh", "claude"], r"C:\Windows\System32\OpenSSH\ssh.exe", False),
            (["notepad.exe"], r"C:\Windows\notepad.exe", False),
        ]
        with mock.patch.object(platform, "IS_WINDOWS", True):
            for argv, exe, expected in cases:
                self.assertEqual(platform.is_claude_process(argv, exe), expected, (argv, exe))

    def test_parse_process_json(self):
        one = '{"ProcessId": 7, "ExecutablePath": "C:\\\\x\\\\claude.exe", "CommandLine": "claude --resume"}'
        processes = platform.parse_win32_processes("\ufeff" + one, set())
        self.assertEqual([(p.pid, p.exe, p.env) for p in processes], [(7, "C:\\x\\claude.exe", None)])
        many = '[{"ProcessId": 1, "ExecutablePath": null, "CommandLine": null}, {"ProcessId": 2}, 5, ' \
               '{"ProcessId": true}, {"ProcessId": 3, "CommandLine": "a b"}]'
        self.assertEqual([p.pid for p in platform.parse_win32_processes(many, {2})], [1, 3])
        self.assertEqual(platform.parse_win32_processes("", set()), [])
        for bad in ("not json", '"text"'):
            with self.assertRaises(platform.BusyCheckError):
                platform.parse_win32_processes(bad, set())


class StatuslineWrapTest(unittest.TestCase):
    def test_wrap_round_trip(self):
        exe = r"C:\tools\bin\multi-claude.exe"
        original = "bash ~/.claude/statusline.sh 'a b'"
        with mock.patch.object(platform, "IS_WINDOWS", True):
            wrapped = statusline.wrap(exe, original)
            self.assertTrue(wrapped.startswith(statusline.WINDOWS_PREFIX))
            self.assertIn("C:/tools/bin/multi-claude.exe", wrapped)
            self.assertEqual(statusline.unwrap(wrapped), ("C:/tools/bin/multi-claude.exe", original))
            if sys.platform == "win32":
                # 不分大小写靠 ntpath.normcase，只有在 Windows 上才成立
                self.assertTrue(statusline._same_executable("C:/TOOLS/bin/multi-claude.exe", exe))

    def test_prefix_preserves_original_variable(self):
        self.assertEqual(statusline.WINDOWS_PREFIX,
                         'MULTI_CLAUDE_MSYS_ORIG="${MSYS2_ARG_CONV_EXCL-__unset__}" MSYS2_ARG_CONV_EXCL=\'*\' ')
        env = hook.original_command_env({"MULTI_CLAUDE_MSYS_ORIG": "__unset__", "MSYS2_ARG_CONV_EXCL": "*", "A": "1"})
        self.assertEqual(env, {"A": "1"})
        env = hook.original_command_env({"MULTI_CLAUDE_MSYS_ORIG": "/x", "MSYS2_ARG_CONV_EXCL": "*"})
        self.assertEqual(env, {"MSYS2_ARG_CONV_EXCL": "/x"})
        self.assertEqual(hook.original_command_env({"MSYS2_ARG_CONV_EXCL": "*"}), {"MSYS2_ARG_CONV_EXCL": "*"})

    def test_posix_wrap_unchanged(self):
        with mock.patch.object(platform, "IS_WINDOWS", False):
            self.assertEqual(statusline.wrap("/usr/local/bin/multi-claude", "a 'b'"),
                             "/usr/local/bin/multi-claude statusline-hook 'a '\"'\"'b'\"'\"''")


class CaptureProcessFlagsTest(unittest.TestCase):
    def test_hidden_console_not_detached(self):
        # DETACHED_PROCESS 会让 pipx 转接出的真正 python.exe 弹出可见控制台，Claude 每回复一次闪一下（2026-11-03 实机）
        fake = mock.Mock(CREATE_NO_WINDOW=0x08000000, DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200,
                         PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
        hook._start_capture_process(b"{}", fake)
        flags = fake.Popen.call_args[1]["creationflags"]
        self.assertEqual(flags, 0x08000000 | 0x200)
        fake.Popen.return_value.stdin.write.assert_called_once_with(b"{}")


class PowerShellCompletionTest(unittest.TestCase):
    def test_script_is_ascii(self):
        # Windows PowerShell 5.1 按 ANSI 代码页读不带 BOM 的 $PROFILE，脚本里出现中文会读成乱码
        script = completion.render("powershell", cli.build_parser())
        self.assertEqual([c for c in script if ord(c) > 127], [])


class BusyCheckMessageTest(unittest.TestCase):
    def test_windows_skips_unreadable_environment_note(self):
        users = platform.ClaudeUsers([], [], 377, [])
        # 在 Claude 会话里跑测试时，占用检查会先因“身处 Claude 会话”直接返回
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(platform.CHILD_SESSION_ENV, None)
        for windows, expected in ((True, False), (False, True)):
            with mock.patch.object(platform, "IS_WINDOWS", windows), \
                    mock.patch.object(platform, "find_busy_processes", return_value=[]), \
                    mock.patch.object(platform, "find_claude_users", return_value=users), \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(migrate._busy_check(os.path.join(tempfile.gettempdir(), "x")), 0)
            self.assertEqual("could not be read" in out.getvalue(), expected, out.getvalue())


class ResumeCommandTest(unittest.TestCase):
    def test_cd_and_run(self):
        with mock.patch.object(platform, "IS_WINDOWS", True):
            self.assertEqual(cli._cd_and_run(r"C:\it's here", "claude-work --resume x"),
                             r"Set-Location 'C:\it''s here'; claude-work --resume x")
        with mock.patch.object(platform, "IS_WINDOWS", False):
            self.assertEqual(cli._cd_and_run("/a b", "claude"), "cd '/a b' && claude")


class EntryIsolationTest(unittest.TestCase):
    """_entry.py 用 -I 启动：当前目录与包目录里的同名模块都不能顶替标准库（方案 D2）。"""

    def test_cwd_modules_do_not_shadow_stdlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("json", "platform", "runpy"):
                with open(os.path.join(tmp, name + ".py"), "w") as handle:
                    handle.write("raise SystemExit('shadowed by the current directory')\n")
            entry = os.path.join(SRC, "multi_claude", "_entry.py")
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            env["PYTHONPATH"] = tmp
            proc = subprocess.run([sys.executable, "-I", entry, "--version"], cwd=tmp, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                  **TEXT_KWARGS)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(__version__, proc.stdout)
            self.assertNotIn("shadowed", proc.stderr)


class SharingRetryTest(unittest.TestCase):
    def _flaky(self, failures, winerror):
        calls = []

        def func():
            calls.append(1)
            if len(calls) <= failures:
                exc = PermissionError(13, "in use")
                exc.winerror = winerror
                raise exc
            return "done"
        return func, calls

    def test_retries_then_succeeds(self):
        func, calls = self._flaky(3, 32)
        with mock.patch.object(platform, "IS_WINDOWS", True), mock.patch.object(platform.time, "sleep") as sleep:
            self.assertEqual(platform.retry_on_sharing_violation(func), "done")
        self.assertEqual(len(calls), 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.05, 0.1, 0.2])

    def test_gives_up_after_all_delays(self):
        func, calls = self._flaky(100, 5)
        with mock.patch.object(platform, "IS_WINDOWS", True), mock.patch.object(platform.time, "sleep"):
            with self.assertRaises(PermissionError):
                platform.retry_on_sharing_violation(func)
        self.assertEqual(len(calls), 6, "five delayed retries plus the final attempt")

    def test_other_errors_and_posix_do_not_retry(self):
        func, calls = self._flaky(1, 1314)
        with mock.patch.object(platform, "IS_WINDOWS", True), mock.patch.object(platform.time, "sleep"):
            with self.assertRaises(PermissionError):
                platform.retry_on_sharing_violation(func)
        self.assertEqual(len(calls), 1)
        func, calls = self._flaky(1, 32)
        with mock.patch.object(platform, "IS_WINDOWS", False):
            with self.assertRaises(PermissionError):
                platform.retry_on_sharing_violation(func)
        self.assertEqual(len(calls), 1)


@windows_only
class WindowsPathRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mcw-"))
        self.addCleanup(lambda: subprocess.call(["cmd", "/c", "rmdir", "/s", "/q", self.tmp]))

    def test_route_path_validation(self):
        self.assertEqual(config.validate_route_path("C:\\work"), "C:\\work")
        for bad in ("C:\\", "C:/", "C:", "work", "\\work"):
            with self.assertRaises(ValueError, msg=bad):
                config.validate_route_path(bad)

    def test_is_under_ignores_case(self):
        self.assertTrue(fsutil.is_under("C:\\Users\\Me\\.Claude\\x", "c:\\users\\me\\.claude"))
        self.assertFalse(fsutil.is_under("C:\\Users\\me\\.claude-shared", "C:\\Users\\me\\.claude"))

    def test_resolve_matches_case_insensitively_and_by_segment(self):
        work = os.path.join(self.tmp, "Work")
        os.makedirs(os.path.join(work, "proj"))
        os.makedirs(os.path.join(self.tmp, "Workshop"))
        cfg = config.Config(self.tmp, self.tmp, None, [], {"w": config.Account("w")},
                            route_rules=[config.RouteRule(work.lower(), "w")])
        account, rule = routes.resolve(cfg, os.path.join(work, "proj"))
        self.assertEqual(account.name if account else None, "w")
        account, rule = routes.resolve(cfg, os.path.join(self.tmp, "Workshop"))
        self.assertIsNone(account)

    def test_copy_tree_and_manifest_keep_junctions_as_links(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "big.bin"), "w") as handle:
            handle.write("x")
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        platform.create_link(outside, os.path.join(src, "j"))
        manifest, _ = fsutil.build_manifest(src)
        self.assertFalse(any("big.bin" in key for key in manifest), manifest)
        dst = os.path.join(self.tmp, "dst")
        fsutil.copy_tree(src, dst)
        self.assertTrue(platform.is_junction(os.path.join(dst, "j")))


class MigrateFileLinkPrecheckTest(unittest.TestCase):
    """迁移前只预检登记账号时真正要新建的文件链接（migrate._file_links_after_move）。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mcw-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(self.shared, "skills"))
        for item in ("settings.json", "CLAUDE.md"):
            with open(os.path.join(self.shared, item), "w") as handle:
                handle.write("{}")
        self.source = os.path.join(self.tmp, "home", ".claude")
        os.makedirs(self.source)
        with open(os.path.join(self.source, "settings.json"), "w") as handle:
            handle.write("{}")
        self.root = os.path.join(self.tmp, "cc")
        self.journal = {"name": "main", "source": self.source, "target": os.path.join(self.root, "main")}

    def _config(self, accounts):
        return config.Config(self.root, os.path.join(self.tmp, "bin"), self.shared,
                             ["skills", "settings.json", "CLAUDE.md"], accounts)

    def test_new_unshared_account_needs_no_file_links(self):
        self.assertEqual(migrate._file_links_after_move(self._config({}), True, self.journal), [])

    def test_items_already_in_source_are_excluded(self):
        cfg = self._config({"main": config.Account("main", shared=True)})
        self.assertEqual(migrate._file_links_after_move(cfg, True, self.journal), ["CLAUDE.md"])


@windows_only
class EntryShortPathTest(unittest.TestCase):
    """启动命令里的 _entry.py 是 8.3 短名、包却从长路径加载时，仍判为同一个包（第 2 轮评审 N1）。"""

    def test_short_entry_with_long_package_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = os.path.join(tmp, "venv")
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True)
            python = os.path.join(venv, "Scripts", "python.exe")
            site = subprocess.run([python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                  stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
            # .pth 让解释器从长路径的源码目录加载包，模拟 pip 安装后 site-packages 里的包
            with open(os.path.join(site, "mcw-src.pth"), "w") as handle:
                handle.write(os.path.realpath(SRC) + "\n")
            long_entry = os.path.join(os.path.realpath(SRC), "multi_claude", "_entry.py")
            entry = platform.short_path(long_entry)
            if entry == long_entry:
                self.skipTest("8.3 short names are disabled on this volume")
            proc = subprocess.run([python, "-I", entry, "--version"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, universal_newlines=True, **TEXT_KWARGS)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(__version__, proc.stdout)


@windows_only
class LinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcw-")
        self.addCleanup(lambda: subprocess.call(["cmd", "/c", "rmdir", "/s", "/q", self.tmp]))

    def test_junction_create_detect_remove(self):
        target = os.path.join(self.tmp, "target")
        os.makedirs(os.path.join(target, "sub"))
        with open(os.path.join(target, "keep.txt"), "w") as handle:
            handle.write("x")
        link = os.path.join(self.tmp, "link")
        platform.create_link(target, link)
        self.assertTrue(platform.is_junction(link))
        self.assertTrue(platform.is_link(link))
        self.assertEqual(os.path.normcase(platform.read_link(link)), os.path.normcase(target))
        from multi_claude import fsutil
        self.assertEqual(fsutil.entry_kind(link), fsutil.KIND_LINK)
        platform.remove_link(link)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(os.path.join(target, "keep.txt")), "removing the link must keep the target")

    def test_file_link_needs_privilege(self):
        target = os.path.join(self.tmp, "f.json")
        with open(target, "w") as handle:
            handle.write("{}")
        link = os.path.join(self.tmp, "f-link.json")
        try:
            platform.create_link(target, link)
        except platform.LinkPermissionError as exc:
            self.assertIn("Developer Mode", str(exc))
            self.assertFalse(platform.can_create_file_link())
        else:
            self.assertTrue(os.path.islink(link))
            self.assertTrue(platform.can_create_file_link())


@windows_only
class WindowsSystemTest(unittest.TestCase):
    def test_which_ignores_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "mcw-probe.cmd"), "w") as handle:
                handle.write("@exit /b 0\r\n")
            old = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertIsNone(platform.which("mcw-probe", os.environ.get("PATH", "")))
                self.assertEqual(os.path.normcase(platform.which("mcw-probe", tmp) or ""),
                                 os.path.normcase(os.path.join(tmp, "mcw-probe.cmd")))
            finally:
                os.chdir(old)

    def test_pid_alive_does_not_kill(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.kill)
        self.assertTrue(platform._pid_alive(child.pid))
        self.assertIsNone(child.poll(), "probing a process must not terminate it")
        child.kill()
        child.wait()
        self.assertFalse(platform._pid_alive(child.pid))

    def test_lock_excludes_other_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "lock")
            fd = os.open(path, os.O_RDWR | os.O_CREAT)
            try:
                platform.lock_fd(fd, blocking=False)
                os.write(fd, b"123")
                probe = ("import os, sys; sys.path.insert(0, sys.argv[2]); from multi_claude import platform\n"
                         "fd = os.open(sys.argv[1], os.O_RDWR)\n"
                         "try:\n    platform.lock_fd(fd, blocking=False)\nexcept BlockingIOError:\n"
                         "    print('busy', os.read(fd, 10).decode())\nelse:\n    print('free')\n")
                out = subprocess.run([sys.executable, "-c", probe, path, SRC], stdout=subprocess.PIPE,
                                     universal_newlines=True, check=True).stdout.strip()
                self.assertEqual(out, "busy 123", "the holder pid must stay readable while locked")
                platform.unlock_fd(fd)
                out = subprocess.run([sys.executable, "-c", probe, path, SRC], stdout=subprocess.PIPE,
                                     universal_newlines=True, check=True).stdout.strip()
                self.assertEqual(out, "free")
            finally:
                os.close(fd)


@windows_only
class WindowsCliTest(CliTestCase):
    def test_launcher_runs_claude_with_account_env(self):
        self.ok("add", "work", "--proxy", "7901")
        self.ok("args", "work", "--", "--model", "opus")
        self.assertTrue(os.path.isfile(os.path.join(self.bin, "claude-work.cmd")))
        code, args, env, err = self.run_launcher("work", "-p", "hi there")
        self.assertEqual(code, 0, err)
        self.assertEqual(args, ["--model", "opus", "-p", "hi there"])
        self.assertEqual(os.path.normcase(env["CLAUDE_CONFIG_DIR"]), os.path.normcase(os.path.join(self.root, "work")))
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7901")
        code, _, _, _ = self.run_launcher("work", env={"FAKE_CLAUDE_RC": "3"})
        self.assertEqual(code, 3, "the launcher must pass the exit code through")

    def test_list_recognises_cmd_launcher(self):
        self.ok("add", "work")
        entry = json.loads(self.ok("list", "--json").out)["accounts"][0]
        self.assertEqual(entry["launcher"], "ok")
        result = self.ok("doctor")
        # 只看 launcher 检查项本身；bin-on-path 提示里也会出现 launcher 一词
        problems = [line for line in (result.out + result.err).splitlines()
                    if line.startswith("[") and line.split("] ", 1)[-1].startswith("launcher")]
        self.assertEqual(problems, [], result.out)
        self.assertIn("0 error(s)", result.out + result.err)

    def test_migrate_reports_busy_when_a_file_is_open(self):
        source = os.path.join(self.home, ".claude")
        held = self.write(os.path.join(source, "history.jsonl"), "x")
        before = self.snapshot(source)
        with open(held):
            # 本用例只测改名时文件被占用；进程检查由 test_migrate_then_restore 覆盖
            result = self.run_cli("migrate-default", "main", "--skip-process-check")
        self.assertEqual(result.code, 4, result)
        self.assertIn("in use", result.err)
        self.assertEqual(before, self.snapshot(source))
        self.assertIsNone(self.journal())
        self.assertFalse(os.path.lexists(os.path.join(self.root, "main")))

    def test_migrate_then_restore(self):
        source = os.path.join(self.home, ".claude")
        # Windows 上任何 Claude 进程都会让迁移被拒绝；开发机上开着 Claude 时跳过，CI 机器上没有 Claude 照常运行
        if platform.find_claude_users(source).busy:
            self.skipTest("a Claude process is running on this machine")
        self.write(os.path.join(source, ".credentials.json"), "{}")
        self.write(os.path.join(source, "projects", "p", "a.jsonl"), "line\n")
        self.ok("migrate-default", "main")
        self.assertTrue(platform.is_junction(source))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "main", ".credentials.json")))
        self.ok("restore", "main")
        self.assertFalse(platform.is_link(source))
        self.assertTrue(os.path.isfile(os.path.join(source, "projects", "p", "a.jsonl")))

    def test_powershell_completion_parses(self):
        script = self.ok("completion", "powershell").out
        path = self.write(os.path.join(self.tmp, "complete.ps1"), script)
        check = ("$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                 "'{}', [ref]$null, [ref]$errors) | Out-Null; $errors.Count").format(path.replace("'", "''"))
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", check],
                             stdout=subprocess.PIPE, universal_newlines=True, **TEXT_KWARGS).stdout.strip()
        self.assertEqual(out, "0", script)


if __name__ == "__main__":
    unittest.main()
