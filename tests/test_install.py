"""方案 §8 第 15 条（移植 multi-codex §8 第 16 条）：install.sh 的本地安装、远程安装（file:// 源码包）、重复安装、中断恢复与卸载。"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import unittest

from helpers import ROOT, CliTestCase

INSTALL = os.path.join(ROOT, "install.sh")


class InstallTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.prefix = os.path.join(self.tmp, "prefix")
        self.env.pop("PYTHONPATH")
        # 安装脚本需要 python3、tar、find 等常规工具，保留系统 PATH，但仍把假 claude 与假 security 放在最前面。
        self.env["PATH"] = self.fakebin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")

    def install(self, *args, env=None, script=INSTALL):
        full_env = dict(self.env)
        full_env.update(env or {})
        return subprocess.run(["sh", script, "--prefix", self.prefix] + list(args), env=full_env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def tool(self, *args):
        return subprocess.run([os.path.join(self.prefix, "bin", "multi-claude")] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def make_tarball(self):
        """模拟 GitHub 源码包：顶层一个目录，里面是仓库内容。"""
        path = os.path.join(self.tmp, "src.tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            for name in ("src", "install.sh", "README.md"):
                full = os.path.join(ROOT, name)
                if os.path.exists(full):
                    archive.add(full, arcname=os.path.join("multi-claude-main", name),
                                filter=lambda info: None if "__pycache__" in info.name else info)
        return path

    def test_local_install_is_idempotent(self):
        first = self.install()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.count("unchanged"), 2, second.stdout)
        self.assertEqual(self.tool("--version").returncode, 0)

    def test_version_manager_shim_is_resolved(self):
        """方案 feature-hook-latency §5.1.4：python3 是 shim 时，启动命令写它背后的真实解释器。"""
        shims = os.path.join(self.tmp, "pyenv", "shims")
        self.write(os.path.join(shims, "python3"), '#!/bin/sh\nexec "{}" "$@"\n'.format(sys.executable), 0o755)
        real = subprocess.run([sys.executable, "-c", "import sys; print(sys.executable)"],
                              stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        result = self.install(env={"PATH": shims + os.pathsep + self.env["PATH"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("instead of the shim", result.stdout)
        with open(os.path.join(self.prefix, "bin", "multi-claude")) as handle:
            wrapper = handle.read()
        self.assertIn("exec '{}' -m multi_claude".format(real), wrapper)
        self.assertNotIn("/shims/", wrapper)
        self.assertEqual(self.tool("--version").returncode, 0)

    def test_remote_install_from_tarball(self):
        # 从临时目录里的副本运行脚本，确保走的是“下载”分支而不是本地源码。
        script = os.path.join(self.tmp, "install.sh")
        shutil.copy(INSTALL, script)
        env = {"MULTI_CLAUDE_TARBALL": "file://" + self.make_tarball()}
        result = self.install(env=env, script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("downloading", result.stdout)
        self.assertEqual(self.tool("list").returncode, 0)

    def test_no_release_without_ref(self):
        script = os.path.join(self.tmp, "install.sh")
        shutil.copy(INSTALL, script)
        # 用不存在的 file:// 地址代替 GitHub API，测试不依赖网络。
        env = {"MULTI_CLAUDE_API": "file://" + os.path.join(self.tmp, "no-api")}
        result = self.install(env=env, script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MULTI_CLAUDE_REF", result.stderr)

    def test_config_runs_apply(self):
        config = self.write(os.path.join(self.tmp, "accounts.json"), json.dumps(
            {"version": 1, "accounts": {"work": {"proxy": "7901"}}}))
        result = self.install("--config", config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.bin, "claude-work")))

    def test_interrupted_swap_recovers(self):
        self.assertEqual(self.install().returncode, 0)
        package = os.path.join(self.prefix, "share", "multi-claude", "multi_claude", "__init__.py")
        with open(package, "a") as handle:
            handle.write("# older\n")  # 让下次安装判定为“内容不同”，进入替换流程
        # 钩子只在 MULTI_CLAUDE_TEST_MODE=1 时生效：不设时照常完成安装。
        normal = self.install(env={"MULTI_CLAUDE_TEST_CRASH_SWAP": "1", "MULTI_CLAUDE_TEST_MODE": "0"})
        self.assertEqual(normal.returncode, 0, normal.stderr)
        with open(package, "a") as handle:
            handle.write("# older\n")
        crashed = self.install(env={"MULTI_CLAUDE_TEST_CRASH_SWAP": "1"})
        self.assertEqual(crashed.returncode, 137)
        self.assertTrue(os.path.isdir(os.path.join(self.prefix, "share", "multi-claude.old")))
        recovered = self.install()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "share", "multi-claude.old")))
        self.assertEqual(self.tool("--version").returncode, 0)

    def test_stale_old_dir_is_removed(self):
        self.assertEqual(self.install().returncode, 0)
        stale = os.path.join(self.prefix, "share", "multi-claude.old")
        os.makedirs(stale)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(stale))

    def test_piped_install_ignores_clone_in_current_directory(self):
        # `curl | sh` 时 $0 是 sh；即使当前目录恰好是仓库克隆，也必须走下载分支。
        with open(INSTALL) as handle:
            script = handle.read()
        env = dict(self.env)
        env["MULTI_CLAUDE_API"] = "file://" + os.path.join(self.tmp, "no-api")
        proc = subprocess.run(["sh", "-s", "--", "--prefix", self.prefix], input=script, env=env, cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MULTI_CLAUDE_REF", proc.stderr)

    def test_uninstall_keeps_accounts(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.tool("add", "work").returncode, 0)
        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "bin", "multi-claude")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "work")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "config.json")))
        code, _, env, _ = self.run_launcher("work")
        self.assertEqual(code, 0)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.join(self.root, "work"))
        again = self.install("--uninstall")
        self.assertIn("unchanged", again.stdout)

    def test_help_and_bad_arguments(self):
        help_result = subprocess.run(["sh", INSTALL, "--help"], stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--config", help_result.stdout)
        bad = subprocess.run(["sh", INSTALL, "--bogus"], stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotEqual(bad.returncode, 0)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        proc = subprocess.run(["shellcheck", INSTALL], stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)


if __name__ == "__main__":
    unittest.main()
