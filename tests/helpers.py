"""测试公共设施：临时 HOME、假 claude、假 security、以子进程方式运行 CLI。

每个用例都在独立的临时 HOME 下运行，绝不触碰真实的 ~/.claude、~/.cc、钥匙串或 ~/.local/bin。
setUp 里的安全闸任一不满足就让用例失败（而不是跳过）：
- 子进程 PATH 固定，第一个 `claude` 与第一个 `security` 必须是本用例的假脚本；
- HOME、XDG_CONFIG_HOME 指向临时目录；
- 子进程环境从空字典构造，不继承任何 CLAUDE_* 变量（在 Claude 会话里跑测试时，
  继承的 CLAUDE_CODE_CHILD_SESSION 会让每个迁移用例都被占用检查第 0 条拦下）。
CLI 一律以子进程运行：迁移的中断测试需要真正的进程退出（os._exit），
也能顺带覆盖“真实 import / 启动”这一层。
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_OUT.args"
env > "$FAKE_CLAUDE_OUT.env"
exit "${FAKE_CLAUDE_RC:-0}"
"""

# 每次调用把参数逐行追加到记录文件，以一行 `--` 分隔；退出码由 FAKE_SECURITY_RC 决定（默认 44 = 不存在）。
FAKE_SECURITY = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_SECURITY_LOG"
echo -- >> "$FAKE_SECURITY_LOG"
exit "${FAKE_SECURITY_RC:-44}"
"""

SYSTEM_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


class Result(object):
    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    def __repr__(self) -> str:
        return "rc={}\n--- stdout\n{}--- stderr\n{}".format(self.code, self.out, self.err)


class CliTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        # macOS 的 unix socket 路径上限只有 104 字节，所以临时目录要尽量短。
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mcl-"))
        self.addCleanup(self._cleanup)
        self.home = os.path.join(self.tmp, "h")
        self.fakebin = os.path.join(self.tmp, "fb")
        os.makedirs(self.home)
        os.makedirs(self.fakebin)
        for name, content in (("claude", FAKE_CLAUDE), ("security", FAKE_SECURITY)):
            path = os.path.join(self.fakebin, name)
            with open(path, "w") as handle:
                handle.write(content)
            os.chmod(path, 0o755)
        self.fake_out = os.path.join(self.tmp, "claude-out")
        self.security_log = os.path.join(self.tmp, "security.log")
        self.state = os.path.join(self.home, ".config", "multi-claude")
        self.env = {
            "HOME": self.home,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "USER": "tester",
            "PATH": os.pathsep.join([self.fakebin] + SYSTEM_PATH),
            "PYTHONPATH": SRC,
            "FAKE_CLAUDE_OUT": self.fake_out,
            "FAKE_SECURITY_LOG": self.security_log,
            "MULTI_CLAUDE_TEST_MODE": "1",
            "LC_ALL": "C.UTF-8" if sys.platform.startswith("linux") else "en_US.UTF-8",
        }
        self.root = os.path.join(self.home, ".cc")
        self.bin = os.path.join(self.home, ".local", "bin")
        self.assert_safe_env(self.env)

    def assert_safe_env(self, env: Dict[str, str]) -> None:
        """安全闸：子进程只能找到假 claude 与假 security，HOME 在临时目录里，没有 CLAUDE_* 变量。"""
        for name in ("claude", "security"):
            found = shutil.which(name, path=env["PATH"])
            self.assertEqual(found, os.path.join(self.fakebin, name),
                             "the first {} on PATH must be the fake one".format(name))
        for key in ("HOME", "XDG_CONFIG_HOME"):
            if key in env:
                self.assertTrue(env[key].startswith(self.tmp + os.sep), "{} must be inside the temp dir".format(key))
        leaked = [key for key in env if key.startswith("CLAUDE") or key.startswith("ANTHROPIC")]
        self.assertEqual(leaked, [], "Claude variables must not leak into the test environment")

    def _cleanup(self) -> None:
        def onerror(func, path, _exc):
            os.chmod(os.path.dirname(path), 0o755)
            func(path)
        shutil.rmtree(self.tmp, onerror=onerror)

    # ---- 运行 ----

    def _merged(self, env: Optional[Dict[str, Optional[str]]]) -> Dict[str, str]:
        """在基础环境上叠加 env；值为 None 表示删除该变量。"""
        full_env = dict(self.env)
        for key, value in (env or {}).items():
            if value is None:
                full_env.pop(key, None)
            else:
                full_env[key] = value
        return full_env

    def run_cli(self, *args: str, env: Optional[Dict[str, Optional[str]]] = None,
                cwd: Optional[str] = None) -> Result:
        proc = subprocess.run([sys.executable, "-m", "multi_claude"] + list(args), env=self._merged(env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, cwd=cwd or self.tmp, timeout=120)
        return Result(proc)

    def ok(self, *args: str, **kwargs) -> Result:
        result = self.run_cli(*args, **kwargs)
        self.assertEqual(result.code, 0, result)
        return result

    def run_launcher(self, name: str, *args: str, env: Optional[Dict[str, Optional[str]]] = None):
        """执行生成的启动命令，返回 (退出码, 假 claude 收到的参数, 假 claude 看到的环境变量, stderr)。"""
        for suffix in (".args", ".env"):
            if os.path.exists(self.fake_out + suffix):
                os.unlink(self.fake_out + suffix)
        proc = subprocess.run([os.path.join(self.bin, "claude-" + name)] + list(args), env=self._merged(env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        received_args: List[str] = []
        received_env: Dict[str, str] = {}
        if os.path.exists(self.fake_out + ".args"):
            with open(self.fake_out + ".args") as handle:
                received_args = handle.read().split("\n")[:-1]
            with open(self.fake_out + ".env") as handle:
                for line in handle.read().splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        received_env[key] = value
        return proc.returncode, received_args, received_env, proc.stderr

    def security_calls(self) -> List[List[str]]:
        """假 security 每次收到的参数列表。"""
        if not os.path.exists(self.security_log):
            return []
        with open(self.security_log) as handle:
            lines = handle.read().split("\n")
        calls, current = [], []
        for line in lines:
            if line == "--":
                calls.append(current)
                current = []
            elif line:
                current.append(line)
        return calls

    # ---- 文件系统快照 ----

    def snapshot(self, *roots: str) -> Dict[str, tuple]:
        """记录目录树中每一项的类型、内容摘要、链接目标和 mtime，用来断言“什么都没改”。"""
        result: Dict[str, tuple] = {}
        for root in roots or (self.home,):
            if not os.path.lexists(root):
                result[root] = ("missing",)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                for name in dirnames + filenames + [""]:
                    path = os.path.join(dirpath, name) if name else dirpath
                    # 锁文件与状态目录本身是加锁时建出的工具内部记录，不算“用户可见的改动”；
                    # 状态目录里的 config.json 与迁移事务记录仍然参与比较。
                    if path in (os.path.join(self.state, "lock"), self.state, os.path.dirname(self.state)):
                        continue
                    st = os.lstat(path)
                    if stat.S_ISLNK(st.st_mode):
                        result[path] = ("link", os.readlink(path))
                    elif stat.S_ISREG(st.st_mode):
                        with open(path, "rb") as handle:
                            result[path] = ("file", handle.read(), st.st_mtime_ns, stat.S_IMODE(st.st_mode))
                    elif stat.S_ISDIR(st.st_mode):
                        result[path] = ("dir", stat.S_IMODE(st.st_mode))
                    else:
                        result[path] = ("special",)
        return result

    def write(self, path: str, content: str = "x", mode: Optional[int] = None) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def journal(self) -> Optional[str]:
        path = os.path.join(self.state, "migrate-journal.json")
        if not os.path.exists(path):
            return None
        with open(path) as handle:
            return handle.read()

    def config_data(self) -> dict:
        with open(os.path.join(self.state, "config.json")) as handle:
            return json.load(handle)
