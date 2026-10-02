"""v0.6.0 `code NAME [PATH]`：按账号打开独立的 VS Code 实例（方案 docs/feature/feature-vscode-launch.md §8）。

用假 `code` 脚本记录参数与环境，不启动真实的 VS Code。"""

import json
import os
import stat
import sys
import unittest

from helpers import CliTestCase

FAKE_CODE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_OUT.code-args"
env > "$FAKE_CLAUDE_OUT.code-env"
"""


class CodeTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.code_path = self.write(os.path.join(self.fakebin, "code"), FAKE_CODE, 0o755)
        self.ok("add", "work", "--proxy", "7901")
        self.data_dir = os.path.join(self.root, ".apps", "work", "vscode")

    def received(self):
        with open(self.fake_out + ".code-args") as handle:
            args = handle.read().split("\n")[:-1]
        with open(self.fake_out + ".code-env") as handle:
            env = dict(line.split("=", 1) for line in handle.read().splitlines() if "=" in line)
        return args, env

    def test_t1_opens_with_account_environment(self):
        result = self.ok("code", "work")
        self.assertIn("experimental", result.err)
        args, env = self.received()
        self.assertEqual(args, ["--user-data-dir", self.data_dir])
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "work"))
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7901")
        project = os.path.join(self.tmp, "proj")
        self.ok("code", "work", project, "--", "--new-window")
        args, _ = self.received()
        self.assertEqual(args, ["--user-data-dir", self.data_dir, project, "--new-window"])

    def test_t2_private_data_dir(self):
        self.ok("code", "work")
        self.ok("code", "work")
        for path in (os.path.join(self.root, ".apps"), os.path.join(self.root, ".apps", "work"), self.data_dir):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)
        # .apps 不是账号：list 与 apply 都不受影响
        self.ok("apply")
        self.assertEqual(self.ok("list", "--names").out, "work\n")

    def test_t3_rename_keeps_data_dir(self):
        self.ok("rename", "work", "client")
        self.ok("code", "client")
        args, _ = self.received()
        self.assertEqual(args[:2], ["--user-data-dir", self.data_dir])

    def test_t4_failures(self):
        self.assertEqual(self.run_cli("code", "nosuch").code, 1)
        os.unlink(self.code_path)
        # PATH 只留假命令目录：系统目录里可能有真的 code（编译机 /usr/bin/code），不能让测试拉起真实的 VS Code
        result = self.run_cli("code", "work", env={"PATH": self.fakebin})
        self.assertEqual(result.code, 127, result)
        self.assertIn("Install 'code' command in PATH", result.err)
        self.assertFalse(os.path.lexists(os.path.join(self.root, ".apps")))
        with open(os.path.join(self.state, "migrate-journal.json"), "w") as handle:
            json.dump({"name": "main", "source": "s", "target": "t", "backup": "b", "mode": "rename",
                       "phase": "planned"}, handle)
        result = self.run_cli("code", "work")
        self.assertEqual(result.code, 1, result)
        self.assertIn("unfinished migration", result.err)

    def test_t4_more_failures_leave_no_data_dir(self):
        self.assertEqual(self.run_cli("code").code, 2)
        os.rmdir(os.path.join(self.root, "work"))
        result = self.run_cli("code", "work")
        self.assertEqual(result.code, 1, result)
        self.assertFalse(os.path.lexists(os.path.join(self.root, ".apps")))

    def test_t4_run_not_found_has_no_code_hint(self):
        result = self.run_cli("run", "work", "--", "no-such-cmd-xyz")
        self.assertEqual(result.code, 127)
        self.assertNotIn("Install 'code'", result.err)

    def test_t5_redirected_default_link(self):
        source = os.path.join(self.home, ".claude")
        self.write(os.path.join(source, ".credentials.json"), "{}", 0o600)
        self.ok("migrate-default", "main")
        os.unlink(source)
        os.makedirs(source)
        result = self.run_cli("code", "main")
        self.assertEqual(result.code, 1, result)
        self.assertFalse(os.path.lexists(os.path.join(self.root, ".apps", "main")))

    def test_t5_remove_keeps_data_dir(self):
        self.ok("code", "work")
        self.ok("remove", "work")
        self.assertTrue(os.path.isdir(self.data_dir))

    @unittest.skipUnless(sys.platform == "darwin", "macOS-only warnings")
    def test_t5_macos_warnings(self):
        result = self.ok("code", "work")
        self.assertIn("open --env", result.err)
        # 提示只看用户数据目录的长度：macOS 的临时目录本身就很长，按实际长度判断应不应出现
        if len(self.data_dir) > 80:
            self.assertIn("is long", result.err)
        else:
            self.assertNotIn("is long", result.err)

    def test_t5_default_identity_has_no_config_dir(self):
        source = os.path.join(self.home, ".claude")
        self.write(os.path.join(source, ".credentials.json"), "{}", 0o600)
        self.ok("migrate-default", "main")
        self.ok("code", "main")
        _, env = self.received()
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_t6_help_and_completion(self):
        self.assertIn("open VS Code for an account", self.ok("--help").out)
        for shell in ("bash", "zsh", "fish"):
            self.assertIn("code", self.ok("completion", shell).out)


if __name__ == "__main__":
    unittest.main()
