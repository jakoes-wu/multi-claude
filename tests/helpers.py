"""测试公共设施：临时 HOME、假 claude、假 security、以子进程方式运行 CLI。

每个用例都在独立的临时 HOME 下运行，绝不触碰真实的 ~/.claude、~/.cc、钥匙串或 ~/.local/bin。
setUp 里的安全闸任一不满足就让用例失败（而不是跳过）：
- 子进程 PATH 固定，第一个 `claude` 与第一个 `security` 必须是本用例的假脚本；
- HOME、XDG_CONFIG_HOME 指向临时目录；Windows 上 expanduser 读的是 USERPROFILE，状态目录在 APPDATA 下，
  这几个变量同样指向临时目录，并核对子进程里 `~` 确实展开到临时目录；
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
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from multi_claude import platform  # noqa: E402

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CLAUDE_OUT.args"
env > "$FAKE_CLAUDE_OUT.env"
exit "${FAKE_CLAUDE_RC:-0}"
"""

# 每次调用把参数逐行追加到记录文件，以一行 `--` 分隔；退出码由 FAKE_SECURITY_RC 决定（默认 44 = 不存在）。
# 带 -w（读出条目内容）且退出码为 0 时输出 FAKE_SECURITY_SECRET 指向的文件，未设置时输出 `{}`：
# 真实的 security 在条目存在时一定有输出，`{}` 表示“条目在，但里面没有 OAuth 登录”。
# `-i`（从 stdin 读一行命令，Claude Code 用它写钥匙串）：把这一行记进日志（十六进制内容换成 <hex>），
# 并把 -X 后面的十六进制解码写入 FAKE_SECURITY_SECRET；退出码由 FAKE_SECURITY_WRITE_RC 决定（默认 0）。
# 参数形式的 `add-generic-password … -X <hex>` 同样写入（日志里记的是完整参数）。
FAKE_SECURITY = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_SECURITY_LOG"
if [ "${1:-}" = "-i" ]; then
  IFS= read -r line
  printf 'stdin: %s\\n' "$(printf '%s' "$line" | sed -E 's/-X "[0-9a-f]*"/-X <hex>/')" >> "$FAKE_SECURITY_LOG"
  echo -- >> "$FAKE_SECURITY_LOG"
  wrc="${FAKE_SECURITY_WRITE_RC:-0}"
  if [ "$wrc" = 0 ] && [ -n "${FAKE_SECURITY_SECRET:-}" ]; then
    hex="$(printf '%s' "$line" | sed -E 's/.*-X "([0-9a-f]*)".*/\\1/')"
    perl -e 'print pack("H*", $ARGV[0])' "$hex" > "$FAKE_SECURITY_SECRET"
  fi
  exit "$wrc"
fi
echo -- >> "$FAKE_SECURITY_LOG"
if [ "${1:-}" = "add-generic-password" ]; then
  # 参数形式的写入（内容超过 -i 的长度上限时用）：-X 后面一个参数是十六进制内容
  wrc="${FAKE_SECURITY_WRITE_RC:-0}"
  prev=""
  for arg in "$@"; do
    if [ "$prev" = "-X" ] && [ "$wrc" = 0 ] && [ -n "${FAKE_SECURITY_SECRET:-}" ]; then
      perl -e 'print pack("H*", $ARGV[0])' "$arg" > "$FAKE_SECURITY_SECRET"
    fi
    prev="$arg"
  done
  exit "$wrc"
fi
rc="${FAKE_SECURITY_RC:-44}"
if [ "$rc" = 0 ]; then
  for arg in "$@"; do
    if [ "$arg" = "-w" ]; then
      if [ -n "${FAKE_SECURITY_SECRET:-}" ]; then cat "$FAKE_SECURITY_SECRET"; else echo '{}'; fi
    fi
  done
fi
exit "$rc"
"""

# Windows 上 .cmd 不能当 shebang 脚本直接运行：假程序改用 Python 写，同名 .cmd 转调。
# 输出格式与上面的 sh 版逐字节相同（参数逐行；环境变量 k=v 逐行；没有参数时也写一个空行，同 printf）。
FAKE_CLAUDE_PY = """import os, sys
out = os.environ["FAKE_CLAUDE_OUT"]
with open(out + ".args", "w", encoding="utf-8", newline="\\n") as handle:
    handle.write("".join(arg + "\\n" for arg in sys.argv[1:]) or "\\n")
with open(out + ".env", "w", encoding="utf-8", newline="\\n") as handle:
    handle.write("".join("{}={}\\n".format(key, value) for key, value in os.environ.items()))
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC", "0")))
"""

FAKE_SECURITY_PY = """import os, re, sys
args = sys.argv[1:]
log = os.environ["FAKE_SECURITY_LOG"]
secret = os.environ.get("FAKE_SECURITY_SECRET", "")
def append(text):
    with open(log, "a", encoding="utf-8", newline="\\n") as handle:
        handle.write(text)
def store(digits):
    with open(secret, "wb") as handle:
        handle.write(bytes.fromhex(digits))
append("".join(arg + "\\n" for arg in args) or "\\n")
if args[:1] == ["-i"]:
    line = sys.stdin.readline().rstrip("\\n")
    append("stdin: " + re.sub(r'-X "[0-9a-f]*"', "-X <hex>", line) + "\\n--\\n")
    wrc = os.environ.get("FAKE_SECURITY_WRITE_RC", "0")
    found = re.findall(r'-X "([0-9a-f]*)"', line)
    if wrc == "0" and secret and found:
        store(found[-1])
    sys.exit(int(wrc))
append("--\\n")
if args[:1] == ["add-generic-password"]:
    wrc = os.environ.get("FAKE_SECURITY_WRITE_RC", "0")
    for prev, arg in zip([""] + args, args):
        if prev == "-X" and wrc == "0" and secret:
            store(arg)
    sys.exit(int(wrc))
rc = os.environ.get("FAKE_SECURITY_RC", "44")
if rc == "0":
    for arg in args:
        if arg == "-w":
            if secret:
                with open(secret, "rb") as handle:
                    sys.stdout.buffer.write(handle.read())
            else:
                sys.stdout.write("{}\\n")
sys.exit(int(rc))
"""

WINDOWS = sys.platform == "win32"
if WINDOWS:
    _system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    SYSTEM_PATH = [os.path.join(_system_root, "System32"), _system_root]
else:
    SYSTEM_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
# 生成的启动命令在 Windows 上带 .cmd 扩展名
LAUNCHER_SUFFIX = ".cmd" if WINDOWS else ""
# Windows 上子进程输出按 UTF-8 解码（子进程环境里设了 PYTHONIOENCODING）；POSIX 维持原来的默认解码
TEXT_KWARGS = {"encoding": "utf-8", "errors": "replace"} if WINDOWS else {}

posix_only = unittest.skipIf(WINDOWS, "POSIX-only behaviour")
windows_only = unittest.skipUnless(WINDOWS, "Windows-only behaviour")


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
        if WINDOWS:
            for name, content in (("claude", FAKE_CLAUDE_PY), ("security", FAKE_SECURITY_PY)):
                script = os.path.join(self.fakebin, name + "-fake.py")
                with open(script, "w", encoding="utf-8") as handle:
                    handle.write(content)
                with open(os.path.join(self.fakebin, name + ".cmd"), "w") as handle:
                    handle.write('@"{}" "{}" %*\r\n@exit /b %errorlevel%\r\n'.format(sys.executable, script))
        else:
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
        if WINDOWS:
            appdata = os.path.join(self.home, "AppData", "Roaming")
            self.state = os.path.join(appdata, "multi-claude")
            self.env.pop("LC_ALL")
            self.env.update({
                "USERPROFILE": self.home,
                "APPDATA": appdata,
                "LOCALAPPDATA": os.path.join(self.home, "AppData", "Local"),
                "TEMP": self.tmp,
                "TMP": self.tmp,
                "USERNAME": "tester",
                # 没有 SYSTEMROOT 时 Windows 上的 Python 子进程初始化随机数会失败
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
                "COMSPEC": os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
                "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                "PYTHONIOENCODING": "utf-8",
            })
        self.root = os.path.join(self.home, ".cc")
        self.bin = os.path.join(self.home, ".local", "bin")
        self.assert_safe_env(self.env)

    def assert_safe_env(self, env: Dict[str, str]) -> None:
        """安全闸：子进程只能找到假 claude 与假 security，HOME 在临时目录里，没有 CLAUDE_* 变量。"""
        for name in ("claude", "security"):
            found = shutil.which(name, path=env["PATH"])
            expected = os.path.join(self.fakebin, name + (".cmd" if WINDOWS else ""))
            self.assertEqual(os.path.normcase(found or ""), os.path.normcase(expected),
                             "the first {} on PATH must be the fake one".format(name))
        for key in ("HOME", "XDG_CONFIG_HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            if key in env:
                self.assertTrue(env[key].startswith(self.tmp + os.sep), "{} must be inside the temp dir".format(key))
        if WINDOWS:
            # HOMEDRIVE / HOMEPATH 会被某些程序当作家目录；子进程里 ~ 必须展开到临时目录
            self.assertNotIn("HOMEDRIVE", env)
            self.assertNotIn("HOMEPATH", env)
            home = subprocess.run([sys.executable, "-I", "-c", "import os; print(os.path.expanduser('~'))"],
                                  env=env, stdout=subprocess.PIPE, universal_newlines=True, **TEXT_KWARGS)
            self.assertEqual(os.path.normcase(home.stdout.strip()), os.path.normcase(self.home),
                             "~ must expand to the temp home")
        leaked = [key for key in env if key.startswith("CLAUDE") or key.startswith("ANTHROPIC")]
        self.assertEqual(leaked, [], "Claude variables must not leak into the test environment")

    def _cleanup(self) -> None:
        def onerror(func, path, _exc):
            os.chmod(os.path.dirname(path), 0o755)
            if WINDOWS:
                # Windows 的 chmod 只管只读属性：删不掉多半是文件本身只读
                os.chmod(path, stat.S_IWRITE)
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
                              universal_newlines=True, cwd=cwd or self.tmp, timeout=120, **TEXT_KWARGS)
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
        proc = subprocess.run([os.path.join(self.bin, "claude-" + name + LAUNCHER_SUFFIX)] + list(args),
                              env=self._merged(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, **TEXT_KWARGS)
        received_args: List[str] = []
        received_env: Dict[str, str] = {}
        if os.path.exists(self.fake_out + ".args"):
            with open(self.fake_out + ".args", **TEXT_KWARGS) as handle:
                received_args = handle.read().split("\n")[:-1]
            with open(self.fake_out + ".env", **TEXT_KWARGS) as handle:
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
                    if platform.is_junction(path):
                        # os.walk 会进入 junction：记成链接，并且不往里走
                        result[path] = ("link", platform.read_link(path))
                        if name in dirnames:
                            dirnames.remove(name)
                    elif stat.S_ISLNK(st.st_mode):
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
        # Windows 默认按 ANSI 代码页写，内容含非 ASCII 会抛 UnicodeEncodeError；POSIX 下 TEXT_KWARGS 为空，行为不变
        with open(path, "w", **TEXT_KWARGS) as handle:
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
