"""agent-lock 薄集成：默认关闭，启动命令在打开之前与没有这个键时逐字节相同。"""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from helpers import SRC, CliTestCase, posix_only, windows_only

sys.path.insert(0, SRC)

from multi_claude import accounts, config, launcher, routes  # noqa: E402
from multi_claude.config import PROXY_VARS  # noqa: E402


SWITCHER = "/usr/local/sbin/agent-lock"
CLAUDE_WRAPPER = "/usr/local/sbin/agent-lock-claude"
PROCESS_WRAPPER = "CLAUDE_CODE_PROCESS_WRAPPER"


class AgentLockModelTest(unittest.TestCase):
    def test_off_render_is_byte_identical_to_absent_key(self):
        """关闭（缺键，或显式传入 None）时，启动命令与集成前的渲染逐字节相同。"""
        args = ("work", "dir", "/accounts/work", "/home/me/.claude", "inherit", {"A": "b"}, ["--ide"])
        absent = launcher.render(*args)
        explicit = launcher.render(*args, agent_lock=None)
        self.assertEqual(absent, explicit)
        self.assertNotIn("agentnet", absent)
        self.assertNotIn("agent-lock", absent)
        self.assertEqual(launcher.render(*args, net_check=True),
                         launcher.render(*args, net_check=True, agent_lock=None))

        enabled = launcher.render(*args, agent_lock=SWITCHER)
        prefix = [
            'if [ "$(/usr/bin/id -un)" != "agentnet" ]; then',
            "  exec {} \"$0\" \"$@\"".format(shlex.quote(SWITCHER)),
            "fi",
        ]
        absent_lines = absent.splitlines()
        enabled_lines = enabled.splitlines()
        # 受管标记仍在第 2 行；前缀紧跟其后，只切账户，不跑 netgate。
        self.assertEqual(enabled_lines[:2], absent_lines[:2])
        self.assertEqual(enabled_lines[1], launcher.MARKER_PREFIX + "work")
        self.assertEqual(enabled_lines[2:5], prefix)
        self.assertEqual(enabled_lines[5:], absent_lines[2:])
        self.assertNotIn("netgate", "\n".join(prefix))
        self.assertEqual(launcher.LAUNCHER_MODE, 0o755)

    def test_closed_config_omits_the_key_and_keeps_account_env(self):
        absent = config.parse_config(json.dumps({"accounts": {"work": {}}}), "t")
        self.assertIsNone(absent.agent_lock)
        self.assertNotIn("agent_lock", absent.to_dict())
        account = absent.accounts["work"].to_dict()
        self.assertEqual(account["env"], {})
        self.assertEqual(account["args"], [])

        closed = config.parse_config(json.dumps({
            "agent_lock": {"enabled": False, "wrapper": SWITCHER},
            "accounts": {"work": {}},
        }), "t")
        self.assertIsNone(closed.agent_lock)
        self.assertNotIn("agent_lock", closed.to_dict())
        self.assertNotIn("agent_lock", config.dump_config(closed))

        opened = config.parse_config(json.dumps({
            "agent_lock": {"enabled": True, "wrapper": SWITCHER},
        }), "t")
        self.assertEqual(opened.agent_lock, SWITCHER)
        self.assertEqual(opened.to_dict()["agent_lock"], {"enabled": True, "wrapper": SWITCHER})
        again = config.parse_config(config.dump_config(opened), "round-trip")
        self.assertEqual(again.agent_lock, SWITCHER)
        self.assertEqual(again.copy().agent_lock, SWITCHER)

        for value in (True, {"enabled": "yes"}, {"enabled": True, "wrapper": "relative"},
                      {"enabled": True}):
            with self.assertRaises(config.ConfigError):
                config.parse_config(json.dumps({"agent_lock": value}), "t")

    def test_proxy_overrides_ignore_process_wrapper(self):
        self.assertEqual(PROXY_VARS, (
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
            "https_proxy", "http_proxy", "all_proxy", "no_proxy"))
        # 绝对路径，不用 ~：测试进程的 HOME 是真实家目录。
        root = tempfile.mkdtemp(prefix="mcl-lock-")
        self.addCleanup(shutil.rmtree, root)
        account = config.Account("work")
        cfg = config.Config(root, os.path.join(root, "bin"), None, [], {"work": account})
        directory = os.path.join(root, "work")
        os.makedirs(directory)
        with open(os.path.join(directory, "settings.json"), "w") as handle:
            json.dump({"env": {PROCESS_WRAPPER: CLAUDE_WRAPPER, "HTTPS_PROXY": "http://127.0.0.1:9"}}, handle)
        found = accounts.settings_proxy_overrides(cfg, account)
        self.assertEqual([item[1] for item in found], ["HTTPS_PROXY"])

    @posix_only
    def test_router_miss_path_only(self):
        cfg = config.Config("/tmp/cc", "/tmp/bin", None, [], {})
        off = routes.render_router(cfg)
        cfg.agent_lock = SWITCHER
        on = routes.render_router(cfg)
        self.assertIn('exec claude "$@"', off)
        self.assertNotIn('exec claude "$@"', on)
        self.assertIn("exec {} claude \"$@\"".format(shlex.quote(SWITCHER)), on)
        self.assertIn('exec "$target" "$@"', off)
        self.assertIn('exec "$target" "$@"', on)
        off_lines = off.splitlines()
        on_lines = on.splitlines()
        diffs = [index for index, (left, right) in enumerate(zip(off_lines, on_lines)) if left != right]
        self.assertEqual(diffs, [off_lines.index('exec claude "$@"')])


@posix_only
class AgentLockCliTest(CliTestCase):
    def launcher_path(self):
        return os.path.join(self.bin, "claude-work")

    def router_path(self):
        return os.path.join(self.bin, "claude-auto")

    def read(self, path):
        with open(path) as handle:
            return handle.read()

    def write_settings(self, wrapper):
        return self.write(os.path.join(self.home, ".claude", "settings.json"),
                          json.dumps({"env": {PROCESS_WRAPPER: wrapper}}) + "\n")

    def write_switcher(self, name="switcher"):
        return self.write(os.path.join(self.tmp, name), "#!/bin/sh\nexit 0\n", 0o755)

    def write_id(self, rc=0):
        self.write(os.path.join(self.fakebin, "id"), "#!/bin/sh\nexit {}\n".format(rc), 0o755)

    def test_status_does_not_create_config(self):
        result = self.ok("agent-lock")
        self.assertEqual(result.out, "enabled: no\n")
        self.assertFalse(os.path.exists(os.path.join(self.state, "config.json")))
        refused = self.run_cli("agent-lock", "--wrapper", SWITCHER)
        self.assertEqual(refused.code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "config.json")))

    def test_on_exits_when_settings_lack_wrapper_and_does_not_rewrite(self):
        self.ok("add", "work")
        launcher_path = self.launcher_path()
        before = self.read(launcher_path)
        config_path = os.path.join(self.state, "config.json")
        before_config = self.read(config_path)
        mode = os.stat(launcher_path).st_mode
        switcher = self.write_switcher()
        self.write_id(0)
        result = self.run_cli("agent-lock", "on", "--wrapper", switcher)
        self.assertEqual(result.code, 1, result)
        self.assertIn(PROCESS_WRAPPER, result.err)
        self.assertIn("`agent-lock install`", result.err)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))
        self.assertEqual(self.read(launcher_path), before)
        self.assertEqual(self.read(config_path), before_config)
        self.assertNotIn("agent_lock", self.config_data())
        self.assertEqual(os.stat(launcher_path).st_mode, mode)

    def test_on_rewrites_and_off_restores(self):
        self.ok("add", "work")
        self.ok("route", "--default", "work")
        launcher_path = self.launcher_path()
        router_path = self.router_path()
        before_launcher = self.read(launcher_path)
        before_router = self.read(router_path)
        account_dir = os.path.join(self.root, "work")
        dir_mode = stat.S_IMODE(os.stat(account_dir).st_mode)
        bin_mode = stat.S_IMODE(os.stat(self.bin).st_mode)
        self.assertEqual(dir_mode, 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(launcher_path).st_mode), 0o755)

        switcher = self.write_switcher()
        settings_path = self.write_settings(CLAUDE_WRAPPER)
        settings_bytes = self.read(settings_path)
        self.write_id(0)
        self.ok("agent-lock", "on", "--wrapper", switcher)
        saved = self.config_data()
        self.assertEqual(saved["agent_lock"], {"enabled": True, "wrapper": switcher})
        self.assertNotIn("enabled", json.dumps({key: value for key, value in saved.items() if key != "agent_lock"}))
        self.assertEqual(saved["accounts"]["work"]["env"], {})
        self.assertEqual(saved["accounts"]["work"]["args"], [])
        self.assertNotIn(PROCESS_WRAPPER, json.dumps(saved["accounts"]))
        rewritten = self.read(launcher_path)
        self.assertIn('/usr/bin/id -un', rewritten)
        self.assertNotIn("netgate", rewritten)
        self.assertEqual(rewritten.splitlines()[1], launcher.MARKER_PREFIX + "work")
        self.assertIn(shlex.quote(switcher), rewritten)
        self.assertNotEqual(rewritten, before_launcher)
        router = self.read(router_path)
        self.assertNotIn('exec claude "$@"', router)
        self.assertIn("exec {} claude \"$@\"".format(shlex.quote(switcher)), router)
        self.assertIn('exec "$target" "$@"', router)
        self.assertEqual(stat.S_IMODE(os.stat(launcher_path).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(router_path).st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(account_dir).st_mode), dir_mode)
        self.assertEqual(stat.S_IMODE(os.stat(self.bin).st_mode), bin_mode)
        self.assertEqual(self.read(settings_path), settings_bytes)
        self.assertEqual(self.ok("agent-lock").out, "enabled: yes\nwrapper: {}\n".format(switcher))

        # 文件里的 agent_lock 被丢掉，沿用本机的值；没有这个键不会把开关关掉。
        current = self.config_data()
        without = dict(current)
        del without["agent_lock"]
        plain = self.write(os.path.join(self.tmp, "plain.json"), json.dumps(without))
        plain_result = self.ok("apply", "-f", plain)
        self.assertNotIn("agent_lock", plain_result.err)
        self.assertEqual(self.config_data()["agent_lock"]["wrapper"], switcher)
        self.assertEqual(self.read(launcher_path), rewritten)

        other = dict(current)
        other["agent_lock"] = {"enabled": True, "wrapper": "/elsewhere/agent-lock"}
        other_path = self.write(os.path.join(self.tmp, "other.json"), json.dumps(other))
        other_result = self.ok("apply", "-f", other_path)
        self.assertIn("'agent_lock' in {}".format(other_path), other_result.err)
        self.assertEqual(self.config_data()["agent_lock"]["wrapper"], switcher)
        self.assertEqual(self.read(launcher_path), rewritten)

        invalid = dict(current)
        invalid["agent_lock"] = {"enabled": True, "wrapper": "relative"}
        invalid_path = self.write(os.path.join(self.tmp, "invalid.json"), json.dumps(invalid))
        invalid_result = self.ok("apply", "-f", invalid_path)
        self.assertIn("'agent_lock'", invalid_result.err)
        self.assertEqual(self.config_data()["agent_lock"]["wrapper"], switcher)

        refused = self.run_cli("agent-lock", "off", "--wrapper", switcher)
        self.assertEqual(refused.code, 2, refused)
        self.assertEqual(self.config_data()["agent_lock"]["wrapper"], switcher)

        self.ok("agent-lock", "off")
        self.assertNotIn("agent_lock", self.config_data())
        self.assertNotIn("agent_lock", self.read(os.path.join(self.state, "config.json")))
        self.assertEqual(self.read(launcher_path), before_launcher)
        self.assertEqual(self.read(router_path), before_router)
        self.assertEqual(self.ok("agent-lock").out, "enabled: no\n")
        self.assertEqual(stat.S_IMODE(os.stat(account_dir).st_mode), dir_mode)
        self.assertEqual(self.read(settings_path), settings_bytes)

    def test_preconditions_block_rewrite(self):
        self.ok("add", "work")
        before = self.read(self.launcher_path())
        before_config = self.read(os.path.join(self.state, "config.json"))
        switcher = self.write_switcher()
        self.write_settings(CLAUDE_WRAPPER)
        self.write_id(0)

        missing = self.run_cli("agent-lock", "on")
        self.assertEqual(missing.code, 1, missing)
        self.assertIn("switcher", missing.err)
        self.assertEqual(self.read(self.launcher_path()), before)

        relative = self.run_cli("agent-lock", "on", "--wrapper", "relative")
        self.assertEqual(relative.code, 2, relative)
        self.assertEqual(self.read(self.launcher_path()), before)

        self.write_id(1)
        no_user = self.run_cli("agent-lock", "on", "--wrapper", switcher)
        self.assertEqual(no_user.code, 1, no_user)
        self.assertIn("agentnet", no_user.err)
        self.assertEqual(self.read(self.launcher_path()), before)
        self.assertEqual(self.read(os.path.join(self.state, "config.json")), before_config)

    def test_state_file_selects_the_claude_wrapper(self):
        self.ok("add", "work")
        before = self.read(self.launcher_path())
        custom = "/opt/agent-lock/claude-wrap"
        # 沙箱的 XDG_CONFIG_HOME 已是 ~/.config。state.json 与 multi-claude 的配置在同一配置根下。
        self.write(os.path.join(self.home, ".config", "agent-lock", "state.json"),
                   json.dumps({"claude_wrapper": custom}))
        self.write_settings(CLAUDE_WRAPPER)
        switcher = self.write_switcher()
        self.write_id(0)
        mismatch = self.run_cli("agent-lock", "on", "--wrapper", switcher)
        self.assertEqual(mismatch.code, 1, mismatch)
        self.assertIn(custom, mismatch.err)
        self.assertIn("`agent-lock install`", mismatch.err)
        self.assertEqual(self.read(self.launcher_path()), before)
        self.assertNotIn("agent_lock", self.config_data())

        # 相对的 XDG_CONFIG_HOME 不当目录用，仍读 ~/.config 里的 state.json。
        self.write_settings(custom)
        self.ok("agent-lock", "on", "--wrapper", switcher, env={"XDG_CONFIG_HOME": "relative"})
        self.assertEqual(self.config_data()["agent_lock"]["wrapper"], switcher)

    def test_broken_state_file_uses_the_default_wrapper(self):
        self.ok("add", "work")
        self.write(os.path.join(self.home, ".config", "agent-lock", "state.json"), "{")
        self.write_settings(CLAUDE_WRAPPER)
        switcher = self.write_switcher()
        self.write_id(0)
        self.ok("agent-lock", "on", "--wrapper", switcher)
        self.assertEqual(self.config_data()["agent_lock"]["wrapper"], switcher)

    def test_prefix_does_not_nest_when_already_agentnet(self):
        account_dir = os.path.join(self.tmp, "acct")
        os.makedirs(account_dir)
        wrapper = os.path.join(self.tmp, "switcher")
        log = os.path.join(self.tmp, "switcher.log")
        fake_id = self.write(os.path.join(self.tmp, "fake-id"),
                             "#!/bin/sh\n"
                             "if [ \"${1:-}\" = \"-un\" ]; then\n"
                             "  printf '%s\\n' \"${AGENT_LOCK_USER:-tester}\"\n"
                             "  exit 0\n"
                             "fi\n"
                             "exit 1\n", 0o755)
        self.write(wrapper,
                   "#!/bin/sh\n"
                   "printf '%s\\n' \"$*\" >> {log}\n"
                   "if [ \"${{AGENT_LOCK_USER:-}}\" = \"agentnet\" ]; then\n"
                   "  exit 99\n"
                   "fi\n"
                   "target=$1\n"
                   "shift\n"
                   "AGENT_LOCK_USER=agentnet\n"
                   "export AGENT_LOCK_USER\n"
                   "exec \"$target\" \"$@\"\n".format(log=shlex.quote(log)), 0o755)
        text = launcher.render("work", "dir", account_dir, os.path.join(self.home, ".claude"),
                               "inherit", {}, [], agent_lock=wrapper)
        self.assertEqual(text.splitlines()[1], launcher.MARKER_PREFIX + "work")
        self.assertIn("/usr/bin/id -un", text)
        self.assertNotIn("netgate", text)
        self.assertEqual(text.count("exec {}".format(shlex.quote(wrapper))), 1)
        patched = text.replace("/usr/bin/id", fake_id)
        self.assertNotIn("/usr/bin/id", patched)
        script = self.write(os.path.join(self.tmp, "claude-work-copy"), patched, 0o755)

        def run(user, arg):
            if os.path.exists(log):
                os.unlink(log)
            for suffix in (".args", ".env"):
                if os.path.exists(self.fake_out + suffix):
                    os.unlink(self.fake_out + suffix)
            env = self._merged({"AGENT_LOCK_USER": user} if user else None)
            proc = subprocess.run([script, arg], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True, timeout=10)
            calls = []
            if os.path.exists(log):
                with open(log) as handle:
                    calls = [line for line in handle.read().splitlines() if line]
            received = []
            if os.path.exists(self.fake_out + ".args"):
                with open(self.fake_out + ".args") as handle:
                    received = handle.read().split("\n")[:-1]
            return proc.returncode, calls, received, proc.stderr

        code, calls, received, err = run(None, "from-tester")
        self.assertEqual((code, calls, received), (0, [script + " from-tester"], ["from-tester"]), err)
        code, calls, received, err = run("agentnet", "from-agentnet")
        self.assertEqual((code, calls, received), (0, [], ["from-agentnet"]), err)


@windows_only
class AgentLockWindowsTest(CliTestCase):
    def test_on_is_refused_without_rewriting(self):
        self.ok("add", "work")
        path = os.path.join(self.bin, "claude-work.cmd")
        before = self.read_bytes(path)
        result = self.run_cli("agent-lock", "on", "--wrapper", SWITCHER)
        self.assertEqual(result.code, 1, result)
        self.assertIn("macOS and Linux", result.err)
        self.assertNotIn(PROCESS_WRAPPER, result.err)
        self.assertEqual(self.read_bytes(path), before)
        self.assertNotIn("agent_lock", self.config_data())

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()


if __name__ == "__main__":
    unittest.main()
