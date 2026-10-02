"""方案 feature-statusline-usage §8：statusLine 包装、钩子采集与用量合并。

“模拟 Claude 执行”一律用 `/bin/sh -c <包装后的命令>` 并把 JSON 写进 stdin：
Claude Code 2.1.286 在非 Windows 上以 spawn(命令, [], {shell: true}) 执行 statusLine（方案 §3）。
"""

import json
import os
import stat
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import hook, statusline, usage  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"
# 本机实际在用的 claude-hud statusLine：含 '"'"'、$、\t，是引号往返最难的形态。
HUD_COMMAND = ("bash -c 'plugin_dir=$(ls -d \"${CLAUDE_CODE_PLUGIN_CACHE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
               "/plugins}\"/cache/claude-hud/claude-hud/*/ 2>/dev/null | awk -F/ '\"'\"'{ print $(NF-1) \"\\t\" $(0) }"
               "'\"'\"' | sort -t. -k1,1n | tail -1 | cut -f2-); printf \"[%s]\" \"$plugin_dir\"'")


def payload(five=12.5, seven=40, five_reset=None, seven_reset=None, extra=True):
    now = time.time()
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": five_reset or int(now + 3600)}
    if seven is not None:
        limits["seven_day"] = {"used_percentage": seven, "resets_at": seven_reset or int(now + 86400)}
    doc = {"rate_limits": limits}
    if extra:
        doc.update({"model": {"id": "claude-x"}, "cwd": "/secret/project", "cost": {"total_cost_usd": 1.5}})
    return json.dumps(doc).encode()


class StatuslineTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        # 包装命令写的是 PATH 上 multi-claude 的绝对路径；用转调当前源码的小脚本代替安装版。
        self.exe = self.write(os.path.join(self.fakebin, "multi-claude"),
                              '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        self.settings = os.path.join(self.tmp, "settings.json")

    def write_settings(self, command, **extra):
        status_line = {"type": "command", "command": command}
        status_line.update(extra)
        return self.write(self.settings, json.dumps({"model": "opus", "statusLine": status_line}, indent=2) + "\n")

    def settings_command(self, path=None):
        with open(path or self.settings) as handle:
            return json.load(handle)["statusLine"]["command"]

    def claude_runs(self, command, data, env=None, sync=False):
        """按 Claude 的方式执行 statusLine：/bin/sh -c，JSON 从 stdin 进。

        sync=True 时钩子在本进程同步采集，用于判定“不写快照”；默认走真实的后台采集，
        写入一侧用 wait_for 等快照出现。
        """
        env = dict(env or {})
        if sync:
            env[hook.SYNC_ENV] = "1"
        return subprocess.run(["/bin/sh", "-c", command], input=data, env=self._merged(env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.tmp, timeout=60)

    def wait_for(self, path, predicate=None, timeout=10.0):
        """等后台采集写出快照（可附加内容条件）；超时则用例失败。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path):
                try:
                    with open(path) as handle:
                        data = json.load(handle)
                except ValueError:
                    data = None
                if data is not None and (predicate is None or predicate(data)):
                    return data
            time.sleep(0.05)
        self.fail("{} was not written in time".format(path))

    def snapshot_file(self, name):
        return os.path.join(self.state, "usage", name + ".json")

    def read_snapshot(self, name):
        with open(self.snapshot_file(name)) as handle:
            return json.load(handle)


class WrapTest(unittest.TestCase):
    def test_round_trip(self):
        for original in (HUD_COMMAND, "printf 'a\nb'\necho \\ $HOME", "x'y\"z", "plain"):
            with self.subTest(original=original):
                command = statusline.wrap("/opt/my tools/multi-claude", original)
                self.assertEqual(statusline.unwrap(command), ("/opt/my tools/multi-claude", original))

    def test_not_wrapped(self):
        for command in ("plain", "other statusline-hook x", "multi-claude statusline-hook", "'unterminated"):
            with self.subTest(command=command):
                self.assertIsNone(statusline.unwrap(command))


class HookTest(StatuslineTestCase):
    def wrapped(self, original):
        return statusline.wrap(self.exe, original)

    def test_t1_passthrough(self):
        data = payload()
        proc = self.claude_runs(self.wrapped("cat; printf x >&2; exit 7"), data)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stdout, data)
        self.assertEqual(proc.stderr, b"x")

    def test_t2_capture(self):
        self.ok("add", "work")
        account_dir = os.path.join(self.root, "work")
        self.write(os.path.join(account_dir, ".claude.json"), json.dumps({"oauthAccount": {"accountUuid": UUID}}))
        link = os.path.join(self.tmp, "work-link")
        os.symlink(account_dir, link)
        for config_dir in (account_dir, link):
            with self.subTest(config_dir=config_dir):
                if os.path.exists(self.snapshot_file("work")):
                    os.unlink(self.snapshot_file("work"))
                proc = self.claude_runs(self.wrapped("echo shown"), payload(), env={"CLAUDE_CONFIG_DIR": config_dir})
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"shown\n", b""))
                self.wait_for(self.snapshot_file("work"))
                self.assertEqual(stat.S_IMODE(os.stat(self.snapshot_file("work")).st_mode), 0o600)
                snap = self.read_snapshot("work")
                self.assertEqual(snap["config_dir"], os.path.realpath(account_dir))
                self.assertEqual(snap["account_uuid"], UUID)
                self.assertEqual(snap["rate_limits"]["five_hour"]["used_percentage"], 12.5)
                self.assertEqual(snap["rate_limits"]["seven_day"]["used_percentage"], 40)
                text = json.dumps(snap)
                for leaked in ("claude-x", "/secret/project", "total_cost_usd"):
                    self.assertNotIn(leaked, text)

    def test_t3_default_identity(self):
        command = self.wrapped("true")
        self.ok("add", "work")
        self.claude_runs(command, payload(), sync=True)
        self.assertFalse(os.path.exists(os.path.join(self.state, "usage")))
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main")
        self.claude_runs(command, payload())
        self.wait_for(self.snapshot_file("main"))
        self.assertFalse(os.path.exists(self.snapshot_file("work")))

    def test_t4_failures_do_not_touch_output(self):
        self.ok("add", "work")
        work = {"CLAUDE_CONFIG_DIR": os.path.join(self.root, "work")}
        original = "cat; exit 3"
        cases = [
            ("no rate_limits", b'{"model": {}}', work),
            ("not json", b"\xff not json", work),
            ("unknown dir", payload(), {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "elsewhere")}),
            ("empty windows", b'{"rate_limits": {"five_hour": {"used_percentage": "high"}}}', work),
        ]
        for label, data, env in cases:
            with self.subTest(label=label):
                proc = self.claude_runs(self.wrapped(original), data, env=env, sync=True)
                direct = self.claude_runs(original, data, env=env)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr),
                                 (direct.returncode, direct.stdout, direct.stderr))
                self.assertFalse(os.path.exists(os.path.join(self.state, "usage")))

    def test_t4_broken_config_and_unwritable_dir(self):
        self.ok("add", "work")
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.root, "work")}
        usage_dir = os.path.join(self.state, "usage")
        os.makedirs(usage_dir)
        os.chmod(usage_dir, 0o500)
        proc = self.claude_runs(self.wrapped("echo ok"), payload(), env=env, sync=True)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"ok\n", b""))
        self.assertEqual(os.listdir(usage_dir), [])
        os.chmod(usage_dir, 0o700)
        config = os.path.join(self.state, "config.json")
        self.write(config, "{broken")
        proc = self.claude_runs(self.wrapped("echo ok"), payload(), env=env, sync=True)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"ok\n", b""))
        self.assertEqual(os.listdir(usage_dir), [])

    def test_t5_throttle(self):
        self.ok("add", "work")
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.root, "work")}
        data = payload(five_reset=1790000000, seven_reset=1790500000)
        self.claude_runs(self.wrapped("true"), data, env=env)
        first = self.wait_for(self.snapshot_file("work"))["captured_at"]
        # 后两次同步采集：判定“没有改写”需要确定采集已经结束。
        self.claude_runs(self.wrapped("true"), data, env=env, sync=True)
        self.assertEqual(self.read_snapshot("work")["captured_at"], first)
        self.claude_runs(self.wrapped("true"), payload(five=13, five_reset=1790000000, seven_reset=1790500000),
                         env=env, sync=True)
        snap = self.read_snapshot("work")
        self.assertNotEqual(snap["captured_at"], first)
        self.assertEqual(snap["rate_limits"]["five_hour"]["used_percentage"], 13)

    def test_capture_does_not_hold_output(self):
        """后台采集卡住时，钩子仍立即结束、stdout 立即到 EOF；采集放行后快照照常写出。"""
        self.ok("add", "work")
        config = os.path.join(self.state, "config.json")
        with open(config) as handle:
            content = handle.read()
        os.unlink(config)
        # 读 FIFO 会一直阻塞到有人写入：后台采集因此停在 load_config。
        os.mkfifo(config)
        self.addCleanup(self._release_fifo, config)
        env = {"CLAUDE_CONFIG_DIR": os.path.join(self.root, "work")}
        started = time.time()
        proc = self.claude_runs(self.wrapped("echo shown"), payload(), env=env)
        self.assertLess(time.time() - started, 5)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"shown\n", b""))
        self.assertFalse(os.path.exists(self.snapshot_file("work")))
        # 非阻塞打开写端：后台进程还没开始读时会得到 ENXIO，重试到超时，而不是把整套用例挂住。
        deadline = time.time() + 10
        while True:
            try:
                fd = os.open(config, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:
                if time.time() > deadline:
                    self.fail("the background capture never opened config.json")
                time.sleep(0.05)
        os.set_blocking(fd, True)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        self.wait_for(self.snapshot_file("work"))

    @staticmethod
    def _release_fifo(path):
        """用例中途失败时，让仍卡在读 FIFO 的后台进程读到 EOF 退出，不留残留进程。"""
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        os.close(fd)

    def test_hook_entry_stays_light(self):
        """钩子在 exec 前不加载 cli、accounts 等模块；入口字面量与 HOOK_COMMAND 一致。"""
        with open(os.path.join(SRC, "multi_claude", "__main__.py")) as handle:
            self.assertIn('["{}"]'.format(hook.HOOK_COMMAND), handle.read())
        proc = subprocess.run([sys.executable, "-X", "importtime", "-m", "multi_claude", hook.HOOK_COMMAND, "true"],
                              input=b"{}", env=self._merged({}), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd=self.tmp, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        imported = proc.stderr.decode()
        self.assertIn("multi_claude.hook", imported)
        for heavy in ("multi_claude.cli", "multi_claude.accounts", "multi_claude.config", "multi_claude.platform"):
            self.assertNotIn(heavy, imported)

    def test_bad_hook_arguments(self):
        result = self.run_cli("statusline-hook")
        self.assertEqual(result.code, 2)
        self.assertNotIn("statusline-hook", self.ok("--help").out)


class InstallTest(StatuslineTestCase):
    def test_t6_install_idempotent_and_rewrap(self):
        self.write_settings("echo hi", padding=2)
        result = self.ok("statusline", "install", self.settings)
        self.assertIn("statusline: wrapped " + self.settings, result.out)
        self.assertEqual(statusline.unwrap(self.settings_command()), (self.exe, "echo hi"))
        with open(self.settings) as handle:
            data = json.load(handle)
        self.assertEqual(data["statusLine"]["padding"], 2)
        self.assertEqual(data["model"], "opus")
        backups = [name for name in os.listdir(self.tmp) if ".multi-claude-bak." in name]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.tmp, backups[0])) as handle:
            self.assertEqual(json.load(handle)["statusLine"]["command"], "echo hi")

        before = self.snapshot(self.tmp)
        self.assertIn("statusline: unchanged", self.ok("statusline", "install", self.settings).out)
        self.assertEqual(self.snapshot(self.tmp), before)

        other = self.write(os.path.join(self.tmp, "other", "multi-claude"),
                           '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        os.unlink(self.exe)
        result = self.ok("statusline", "install", self.settings,
                         env={"PATH": os.pathsep.join([os.path.dirname(other), self.fakebin, "/usr/bin", "/bin"])})
        self.assertIn("statusline: rewrapped", result.out)
        self.assertEqual(statusline.unwrap(self.settings_command()), (other, "echo hi"))

    def test_t7_refusals(self):
        cases = {
            "missing": None,
            "not json": "{broken",
            "array": "[1]",
            "no statusLine": json.dumps({"model": "opus"}),
            "not command": json.dumps({"statusLine": {"type": "static", "command": "x"}}),
            "empty command": json.dumps({"statusLine": {"type": "command", "command": "  "}}),
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                if os.path.exists(self.settings):
                    os.unlink(self.settings)
                if content is not None:
                    self.write(self.settings, content)
                before = self.snapshot(self.tmp)
                for action in ("install", "uninstall"):
                    result = self.run_cli("statusline", action, self.settings)
                    self.assertEqual(result.code, 1, result)
                    self.assertIn("phase=statusline", result.err)
                self.assertEqual(self.snapshot(self.tmp), before)

    def test_t7_not_on_path(self):
        self.write_settings("echo hi")
        os.unlink(self.exe)
        before = self.snapshot(self.tmp)
        result = self.run_cli("statusline", "install", self.settings)
        self.assertEqual(result.code, 1, result)
        self.assertIn("not on PATH", result.err)
        self.assertEqual(self.snapshot(self.tmp), before)

    def test_t8_symlink_and_mode(self):
        real = self.write(os.path.join(self.tmp, "shared", "s.json"),
                          json.dumps({"statusLine": {"type": "command", "command": "echo hi"}}), 0o640)
        link = os.path.join(self.tmp, "link.json")
        os.symlink(real, link)
        result = self.ok("statusline", "install", link)
        self.assertIn(real, result.out)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), real)
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o640)
        self.assertIsNotNone(statusline.unwrap(self.settings_command(real)))
        self.assertTrue(any(".multi-claude-bak." in name for name in os.listdir(os.path.dirname(real))))

    def test_t9_uninstall(self):
        self.write_settings(HUD_COMMAND)
        self.ok("statusline", "install", self.settings)
        result = self.ok("statusline", "uninstall", self.settings)
        self.assertIn("statusline: restored", result.out)
        self.assertEqual(self.settings_command(), HUD_COMMAND)
        before = self.snapshot(self.tmp)
        self.assertIn("statusline: not installed", self.ok("statusline", "uninstall", self.settings).out)
        self.assertEqual(self.snapshot(self.tmp), before)

    def test_t9b_dry_run(self):
        self.write_settings("echo hi")
        before = self.snapshot(self.tmp)
        result = self.ok("statusline", "install", self.settings, "--dry-run")
        self.assertIn("would be wrapped", result.out)
        self.assertEqual(self.snapshot(self.tmp), before)
        self.ok("statusline", "install", self.settings)
        before = self.snapshot(self.tmp)
        self.assertIn("would be restored", self.ok("statusline", "uninstall", self.settings, "--dry-run").out)
        self.assertEqual(self.snapshot(self.tmp), before)

    def test_t10_quoting_runs_like_original(self):
        newline_command = "printf '%s|' \"$0\"; printf 'a\\nb'\necho \"end $HOME\" 'x'\"'\"'y'"
        for original in (HUD_COMMAND, newline_command):
            with self.subTest(original=original[:20]):
                self.write_settings(original)
                self.ok("statusline", "install", self.settings)
                wrapped = self.settings_command()
                direct = self.claude_runs(original, payload())
                through = self.claude_runs(wrapped, payload())
                self.assertEqual((through.returncode, through.stdout), (direct.returncode, direct.stdout))
                self.ok("statusline", "uninstall", self.settings)
                self.assertEqual(self.settings_command(), original)


class MergeTest(StatuslineTestCase):
    def state_doc(self, fetched_at, five=23, account_uuid=UUID):
        return {"cachedUsageUtilization": {"fetchedAtMs": int(fetched_at.timestamp() * 1000), "accountUuid": UUID,
                                           "utilization": {"five_hour": {"utilization": five}}},
                "oauthAccount": {"accountUuid": account_uuid}}

    def setUp(self):
        super().setUp()
        self.ok("add", "work")
        self.account_dir = os.path.join(self.root, "work")
        self.now = datetime.now(timezone.utc)

    def write_cache(self, fetched_at, **kwargs):
        self.write(os.path.join(self.account_dir, ".claude.json"), json.dumps(self.state_doc(fetched_at, **kwargs)))

    def write_snap(self, captured_at, five=77, resets_at=None, config_dir=None, account_uuid=UUID):
        doc = {"schema_version": 1, "captured_at": captured_at.isoformat(),
               "config_dir": config_dir or os.path.realpath(self.account_dir), "account_uuid": account_uuid,
               "rate_limits": {"five_hour": {"used_percentage": five,
                                             "resets_at": resets_at or (self.now + timedelta(hours=1)).timestamp()}}}
        self.write(self.snapshot_file("work"), json.dumps(doc))

    def usage_of_work(self):
        return json.loads(self.ok("usage", "--json").out)["accounts"][0]["usage"]

    def test_t11_merge_rules(self):
        self.write_cache(self.now - timedelta(minutes=30))
        cases = [
            ("snapshot newer", dict(captured_at=self.now - timedelta(minutes=1)), "statusline", 77),
            ("snapshot older", dict(captured_at=self.now - timedelta(hours=2)), "cache", 23),
            ("dir mismatch", dict(captured_at=self.now, config_dir="/elsewhere"), "cache", 23),
            ("uuid mismatch", dict(captured_at=self.now, account_uuid="someone-else"), "cache", 23),
        ]
        for label, kwargs, source, percent in cases:
            with self.subTest(label=label):
                self.write_snap(**kwargs)
                data = self.usage_of_work()
                self.assertEqual((data["source"], data["five_hour"]["percent"]), (source, percent))
        self.write(self.snapshot_file("work"), "{broken")
        self.assertEqual(self.usage_of_work()["source"], "cache")

    def test_t11_reset_and_no_cache(self):
        self.write_snap(self.now - timedelta(hours=3), resets_at=(self.now - timedelta(minutes=5)).timestamp())
        data = self.usage_of_work()
        self.assertEqual((data["source"], data["status"]), ("statusline", "stale"))
        self.assertTrue(data["five_hour"]["reset"])
        self.assertRegex(self.ok("usage").out, r"work\s+reset\s+-\s+3h ago \(stale\) \(statusline\)")

    def test_t11_out_of_range_reset(self):
        self.write_snap(self.now, resets_at=1e300)
        data = self.usage_of_work()
        self.assertEqual((data["five_hour"]["percent"], data["five_hour"]["resets_at"]), (77, None))

    def test_t12_outputs(self):
        self.write_cache(self.now - timedelta(minutes=30))
        listed = json.loads(self.ok("list", "--json").out)["accounts"][0]["usage"]
        self.assertEqual(listed["source"], "cache")
        self.write_snap(self.now - timedelta(minutes=2))
        listed = json.loads(self.ok("list", "--json").out)["accounts"][0]["usage"]
        self.assertEqual((listed["source"], listed["five_hour"]["percent"]), ("statusline", 77))
        out = self.ok("usage").out
        self.assertRegex(out, r"work\s+77% \d\d:\d\d\s+-\s+2m ago \(statusline\)")
        self.assertIn("statusline hook", out)

    def test_captured_snapshot_is_read_back(self):
        """钩子写出的快照能被 usage 采用：两端格式一致。"""
        self.write_cache(self.now - timedelta(hours=5))
        self.claude_runs(statusline.wrap(self.exe, "true"), payload(five=55),
                         env={"CLAUDE_CONFIG_DIR": self.account_dir})
        self.wait_for(self.snapshot_file("work"))
        data = self.usage_of_work()
        self.assertEqual((data["source"], data["status"], data["five_hour"]["percent"]), ("statusline", "ok", 55))
        self.assertEqual(data["seven_day"]["percent"], 40)
        report = usage.read_snapshot(self.snapshot_file("work"), self.account_dir, UUID, self.now)
        self.assertEqual(report.source, usage.SOURCE_STATUSLINE)


if __name__ == "__main__":
    unittest.main()
