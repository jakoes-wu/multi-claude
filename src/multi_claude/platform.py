"""平台适配：默认路径、创建与识别链接、占用检查。

一期只支持 macOS 与 Linux。二期加 Windows 时，平台差异都在本模块和启动命令模板里扩展，
其它模块不直接判断操作系统。

占用检查分两部分（方案 §5.1.7）：
- find_busy_processes：沿用 multi-codex，按“打开的文件 / 工作目录落在源目录下”判断；
- find_claude_users：Claude 进程几乎不在账号目录里常开文件，必须读进程的 argv 与环境变量，
  再加上 IDE 锁文件、LaunchAgent 服务和 bwrap PID 命名空间三项检查。
本模块只收集事实，不决定退出码，也不输出日志；由 migrate 负责汇报。
"""

import ctypes
import ctypes.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from .fsutil import is_under

# 在全局 shell 里导出就会让所有账号共用同一份凭据或同一个后端，破坏账号隔离（方案 §4）。
ISOLATION_BREAKING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                          "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                          "CLAUDE_CODE_USE_FOUNDRY")

# Claude Code 只在它自己拉起的子进程（Bash 工具、hook、status line 等）里设置这个变量；
# 本工具环境里有它，说明正运行在某个 Claude 会话里（方案 §5.1.7 第 0 条）。
CHILD_SESSION_ENV = "CLAUDE_CODE_CHILD_SESSION"
# 安装成系统服务的后台 supervisor（方案 §3 依据 14），只带 PATH，总是服务默认账号。
DAEMON_PLIST = os.path.join("Library", "LaunchAgents", "com.anthropic.claude-daemon.plist")

TEST_MODE_ENV = "MULTI_CLAUDE_TEST_MODE"


def test_hook_value(variable: str) -> Optional[str]:
    """读取测试钩子变量；只有同时设置 MULTI_CLAUDE_TEST_MODE=1 时才生效。

    钩子能关掉检查或注入故障，生产环境里误设了某个钩子变量也不能让它起作用。
    """
    if os.environ.get(TEST_MODE_ENV) != "1":
        return None
    return os.environ.get(variable)


def is_supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def default_source() -> str:
    """不设 CLAUDE_CONFIG_DIR 时 Claude 使用的配置目录，也是 migrate-default 唯一的迁移源。"""
    return "~/.claude"


def default_root() -> str:
    return "~/.cc"


def default_bin_dir() -> str:
    return "~/.local/bin"


def state_dir() -> str:
    """工具状态目录：config.json、lock、migrate-journal.json 所在位置。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.expanduser("~/.config")
    return os.path.join(base, "multi-claude")


def create_link(target: str, link_path: str) -> None:
    """在 link_path 创建指向 target 的软链；target 使用绝对路径，避免随工作目录变化。"""
    os.symlink(target, link_path)


class BusyProcess(NamedTuple):
    pid: int
    command: str
    # 占用方式（给人看的）：cwd（工作目录）、executable（可执行文件）、mapped（内存映射）或 fd N（打开的文件）
    usage: str
    # 落在源目录下的那个路径，让用户一眼看出是哪个文件被占用
    path: str


def describe_usage(fd: str) -> str:
    """把 lsof 的 fd 字段或 /proc 的条目名翻译成好懂的占用方式；未知值原样返回。"""
    if fd == "txt":
        return "executable"
    if fd == "mem":
        return "mapped"
    digits = fd.rstrip("rwuRWU")
    if digits.isdigit():
        return "fd " + digits
    return fd


class BusyCheckError(Exception):
    """占用检查本身无法完成（找不到检查工具，或 lsof 没有正常输出）。"""


def find_busy_processes(path: str, exclude_pids: Optional[List[int]] = None) -> List[BusyProcess]:
    """列出工作目录、可执行文件或打开的文件落在 path 之下的进程。

    只检查顶层数据库文件会漏掉浏览器扩展宿主这类只把工作目录设在其中的进程，
    所以这里对全部进程逐个判断。结果按“进程名含 claude 的在前”排序。
    """
    root = os.path.realpath(path)
    excluded = set(exclude_pids or [])
    excluded.add(os.getpid())
    if shutil.which("lsof"):
        found = _scan_with_lsof(root, excluded)
    elif sys.platform.startswith("linux") and os.path.isdir("/proc"):
        found = _scan_proc(root, excluded)
    else:
        raise BusyCheckError("neither lsof nor /proc is available")
    found.sort(key=lambda item: ("claude" not in item.command.lower(), item.pid))
    return found


def _scan_with_lsof(root: str, excluded: set) -> List[BusyProcess]:
    # -F pcfn：按字段输出 pid、命令、fd、文件名；-n -P 关闭 DNS 与端口名解析，避免卡顿。
    proc = subprocess.Popen(
        ["lsof", "-n", "-P", "-F", "pcfn"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        errors="replace",
    )
    stdout, _stderr = proc.communicate()
    excluded = excluded | {proc.pid}
    has_process_record = False
    found = {}
    pid = None
    command = ""
    fd = ""
    for line in stdout.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            has_process_record = True
            pid = int(value) if value.isdigit() else None
            command = ""
            fd = ""
        elif field == "c":
            command = value
        elif field == "f":
            fd = value
        elif field == "n" and pid is not None and pid not in excluded:
            if value.startswith("/") and is_under(value, root) and pid not in found:
                found[pid] = BusyProcess(pid, command, describe_usage(fd), value)
    # Linux 普通用户运行 lsof 时，常因无权读取其它用户的进程而返回非 0 并输出告警；
    # 只有一个进程记录都没有时才视为检查失败，否则会把正常情况误判为失败。
    if proc.returncode != 0 and not has_process_record:
        raise BusyCheckError("lsof exited with code {} and produced no output".format(proc.returncode))
    return list(found.values())


def _scan_proc(root: str, excluded: set) -> List[BusyProcess]:
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in excluded:
            continue
        base = os.path.join("/proc", entry)
        hit = _proc_usage(base, root)
        if hit:
            found.append(BusyProcess(pid, _proc_command(base), describe_usage(hit[0]), hit[1]))
    return found


def _proc_usage(base: str, root: str) -> Optional[Tuple[str, str]]:
    """返回 (占用方式, 路径)；与 lsof 的 fd 字段取值保持一致，交给 describe_usage 统一翻译。"""
    for name, usage in (("cwd", "cwd"), ("exe", "txt")):
        target = _readlink_quiet(os.path.join(base, name))
        if target and is_under(target, root):
            return usage, target
    fd_dir = os.path.join(base, "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    for fd in fds:
        target = _readlink_quiet(os.path.join(fd_dir, fd))
        if target and target.startswith("/") and is_under(target, root):
            return fd, target
    return None


def _proc_command(base: str) -> str:
    try:
        with open(os.path.join(base, "comm"), "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return "?"


def _readlink_quiet(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


# ---- Claude 进程与相关占用（方案 §5.1.7 第 2–5 条） ----

class ProcessInfo(NamedTuple):
    pid: int
    argv: List[str]
    # 读不出环境时为 None：进程已退出、权限不足，或 macOS 上 Apple 自带程序（/bin/zsh 等）——
    # 系统对它们只返回 argv、隐去环境（macOS 27 实测，`ps -E` 同样读不到）。
    env: Optional[Dict[str, str]]
    # 可执行文件路径：macOS 取 KERN_PROCARGS2 开头的 exec_path，Linux 取 /proc/<pid>/exe；取不到为空串
    exe: str


class ClaudeUsers(NamedTuple):
    busy: List[BusyProcess]
    # 每个命中的 Claude 进程对应的 `claude daemon stop --any` 命令（去重、保持出现顺序）
    stop_commands: List[str]
    # 同 uid 进程中读不出环境的个数，汇总提示用
    unreadable: int
    # 只提示、不判占用的问题，例如 IDE 锁文件不是合法 JSON
    warnings: List[str]


def is_claude_process(argv: List[str], exe: str) -> bool:
    """按 argv 与可执行文件路径判断是否 Claude Code 进程（方案 §5.1.7 第 2 条，依据 11）。

    终端启动的 claude 的 argv[0] 是 `claude`，但 Claude 自己拉起的子进程不一定：
    pinToCurrentBinary 时直接用版本文件路径，后台会话宿主的 argv[0] 是 `claude bg-pty-host`，
    npm 安装形态是 `node .../bin/claude`。不能只看 argv[1]，否则 `ssh claude` 之类会被误判进来。
    """
    argv0 = argv[0] if argv else ""
    base = os.path.basename(argv0)
    if base == "claude" or base.startswith("claude "):
        return True
    if "/claude/versions/" in argv0 or "/claude/versions/" in exe:
        return True
    if "--bg-pty-host" in argv:
        return True
    if base in ("node", "bun") and len(argv) > 1:
        script = argv[1]
        if os.path.basename(script) == "claude" or "@anthropic-ai/claude-code" in script:
            return True
    return False


def config_dir_of(env: Dict[str, str]) -> str:
    """该进程的 Claude 配置目录：与 Claude 的 we() 相同，CLAUDE_CONFIG_DIR 为空时用 <HOME>/.claude。"""
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        return configured
    return os.path.join(env.get("HOME", "~"), ".claude")


def daemon_stop_command(env: Dict[str, str]) -> str:
    """停掉该进程所属配置目录的后台 supervisor 的命令。

    supervisor 按配置目录各起一个实例（依据 14）；不指定变量就会停掉当前 shell 所指账号的实例，
    所以未设变量的进程要显式 `env -u CLAUDE_CONFIG_DIR`。按需启动的实例需要 `--any`。
    """
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        return "CLAUDE_CONFIG_DIR={} claude daemon stop --any".format(shlex.quote(configured))
    return "env -u CLAUDE_CONFIG_DIR claude daemon stop --any"


def find_claude_users(source: str, exclude_pids: Optional[List[int]] = None) -> ClaudeUsers:
    """检查第 2–5 条：Claude 进程、继承了 CLAUDE_CONFIG_DIR 的进程、IDE 锁、后台服务。

    source 是迁移源 S（`~/.claude` 展开后）。任何账号的 Claude 进程都会写 $HOME/.claude 下的
    bridge-spawn、state/、ide/（依据 12），所以第 2 条按 HOME 比较，而不是按配置目录比较。
    检查本身无法完成时抛出 BusyCheckError。
    """
    _check_pid_namespace()
    source_real = os.path.realpath(source)
    home_real = os.path.realpath(os.path.expanduser("~"))
    excluded = set(exclude_pids or [])
    excluded.add(os.getpid())

    processes, helper_pids = _list_processes(excluded)
    excluded |= helper_pids
    busy: List[BusyProcess] = []
    stop_commands: List[str] = []
    readable = 0
    unreadable = 0
    for process in processes:
        if process.pid in excluded:
            continue
        env = process.env
        command = process.argv[0] if process.argv else "?"
        if env is None:
            unreadable += 1
            # 环境读不出、但 argv 能认出是 Claude：无法排除它写的是本机的 ~/.claude，按占用处理。
            if is_claude_process(process.argv, process.exe):
                busy.append(BusyProcess(process.pid, command, "claude-process", "(environment not readable)"))
            continue
        readable += 1
        if is_claude_process(process.argv, process.exe):
            home = env.get("HOME")
            # 读不到 HOME 的 Claude 进程同样无法排除，按占用处理。
            if home is None or os.path.realpath(home) == home_real:
                busy.append(BusyProcess(process.pid, command, "claude-process", config_dir_of(env)))
                stop = daemon_stop_command(env)
                if stop not in stop_commands:
                    stop_commands.append(stop)
                continue
        configured = env.get("CLAUDE_CONFIG_DIR")
        # 相对路径要按那个进程的工作目录解析，这里无从得知，只比较绝对路径。
        if configured and os.path.isabs(configured) and os.path.realpath(configured) == source_real:
            busy.append(BusyProcess(process.pid, command, "config-dir", configured))
    if processes and readable == 0:
        raise BusyCheckError("could not read the environment of any process")

    warnings: List[str] = []
    busy.extend(_ide_lock_users(source, warnings))
    busy.extend(_daemon_service())
    return ClaudeUsers(busy, stop_commands, unreadable, warnings)


def _check_pid_namespace() -> None:
    """Linux：PID 1 是 bwrap 说明本工具在 bwrap 建的 PID 命名空间里，看不到宿主上的进程。

    开启 CLAUDE_CODE_SUBPROCESS_ENV_SCRUB 的 Claude 会话用 `bwrap --unshare-pid --proc /proc`
    隔离子进程；这种情况通常已被第 0 条拦下，这里是第二道防线。NSpid 与 /proc/1 属主在 bwrap 中
    都看不出嵌套（方案 §5.1.7 实测），所以只看 /proc/1/comm。
    """
    if not sys.platform.startswith("linux"):
        return
    comm = test_hook_value("MULTI_CLAUDE_TEST_PROC1_COMM")
    if comm is None:
        try:
            with open("/proc/1/comm", "r", encoding="utf-8", errors="replace") as handle:
                comm = handle.read()
        except OSError:
            return
    if comm.strip() == "bwrap":
        raise BusyCheckError("running inside a bwrap PID namespace (PID 1 is bwrap), "
                             "so processes on the host are not visible")


def _list_processes(excluded: Set[int]) -> Tuple[List[ProcessInfo], Set[int]]:
    """枚举与本工具同 uid 的进程，返回 (进程列表, 本工具为枚举而启动的子进程 pid)。"""
    if sys.platform == "darwin":
        return _list_processes_darwin(excluded)
    if os.path.isdir("/proc"):
        return _list_processes_proc(excluded), set()
    raise BusyCheckError("cannot list processes on this platform")


def _list_processes_darwin(excluded: Set[int]) -> Tuple[List[ProcessInfo], Set[int]]:
    try:
        proc = subprocess.Popen(["ps", "-axo", "pid=,uid="], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, universal_newlines=True)
    except OSError as exc:
        raise BusyCheckError("cannot run ps: {}".format(exc))
    stdout, _ = proc.communicate()
    if proc.returncode != 0:
        raise BusyCheckError("ps exited with code {}".format(proc.returncode))
    uid = os.getuid()
    processes = []
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pid = int(fields[0])
        if int(fields[1]) != uid or pid in excluded or pid == proc.pid:
            continue
        raw = _procargs2(pid)
        if raw is None:
            processes.append(ProcessInfo(pid, [], None, ""))
        else:
            processes.append(_parse_procargs2(pid, raw))
    return processes, {proc.pid}


_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROCARGS2 = 49
_libc = None


def _sysctl(mib: List[int], size: int) -> Optional[bytes]:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib_array = (ctypes.c_int * len(mib))(*mib)
    buffer = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    if _libc.sysctl(mib_array, ctypes.c_uint(len(mib)), buffer, ctypes.byref(length), None,
                    ctypes.c_size_t(0)) != 0:
        return None
    return buffer.raw[:length.value]


def _procargs2(pid: int) -> Optional[bytes]:
    """macOS：用 sysctl(KERN_PROCARGS2) 读进程的 exec_path、argv 与环境，原始数据按 NUL 分隔。

    不用 `ps -E`：它把环境变量用空格拼在命令行后面，路径含空格时无法可靠切分（方案 §6）。
    """
    argmax_raw = _sysctl([_CTL_KERN, _KERN_ARGMAX], ctypes.sizeof(ctypes.c_int))
    if argmax_raw is None or len(argmax_raw) < ctypes.sizeof(ctypes.c_int):
        return None
    argmax = int.from_bytes(argmax_raw[:ctypes.sizeof(ctypes.c_int)], sys.byteorder)
    return _sysctl([_CTL_KERN, _KERN_PROCARGS2, pid], argmax)


def _parse_procargs2(pid: int, raw: bytes) -> ProcessInfo:
    """KERN_PROCARGS2 的格式：int argc，exec_path\\0，若干填充 \\0，argv 各项\\0，环境各项\\0，空串结束。"""
    int_size = ctypes.sizeof(ctypes.c_int)
    if len(raw) < int_size:
        return ProcessInfo(pid, [], None, "")
    argc = int.from_bytes(raw[:int_size], sys.byteorder)
    rest = raw[int_size:]
    end = rest.find(b"\0")
    if end < 0:
        return ProcessInfo(pid, [], None, "")
    exe = os.fsdecode(rest[:end])
    position = end
    while position < len(rest) and rest[position:position + 1] == b"\0":
        position += 1
    tokens = rest[position:].split(b"\0")
    argv = [os.fsdecode(token) for token in tokens[:argc]]
    env: Dict[str, str] = {}
    for token in tokens[argc:]:
        if not token:
            break
        key, sep, value = os.fsdecode(token).partition("=")
        if sep:
            env[key] = value
    # 一个环境变量都没有：多半是系统隐去了环境（见 ProcessInfo.env），按读不出处理，不能当成“检查过、没问题”。
    return ProcessInfo(pid, argv, env or None, exe)


def _list_processes_proc(excluded: Set[int]) -> List[ProcessInfo]:
    uid = os.getuid()
    processes = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        base = os.path.join("/proc", entry)
        try:
            if pid in excluded or os.stat(base).st_uid != uid:
                continue
        except OSError:
            continue
        argv = [os.fsdecode(item) for item in _read_nul_list(os.path.join(base, "cmdline")) or []]
        environ = _read_nul_list(os.path.join(base, "environ"))
        env = None
        if environ is not None:
            env = {}
            for token in environ:
                key, sep, value = os.fsdecode(token).partition("=")
                if sep:
                    env[key] = value
        processes.append(ProcessInfo(pid, argv, env, _readlink_quiet(os.path.join(base, "exe"))))
    return processes


def _read_nul_list(path: str) -> Optional[List[bytes]]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return [token for token in data.split(b"\0") if token]


def _ide_lock_users(source: str, warnings: List[str]) -> List[BusyProcess]:
    """第 4 条：IDE 扩展在 ~/.claude/ide/<端口>.lock 里登记自己的 pid，pid 存活即判占用。

    锁文件里还有 authToken，这里只取 pid 与 ideName 两个字段，绝不输出其它内容。
    """
    ide_dir = os.path.join(source, "ide")
    try:
        names = sorted(os.listdir(ide_dir))
    except OSError:
        return []
    found = []
    for name in names:
        if not name.endswith(".lock"):
            continue
        path = os.path.join(ide_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            warnings.append("cannot parse IDE lock file {}; skipped".format(path))
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            warnings.append("IDE lock file {} has no valid pid; skipped".format(path))
            continue
        if _pid_alive(pid):
            ide_name = data.get("ideName")
            found.append(BusyProcess(pid, ide_name if isinstance(ide_name, str) else "IDE", "ide-lock", path))
    return found


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _daemon_service() -> List[BusyProcess]:
    """第 5 条（macOS）：装了 LaunchAgent 形态的 supervisor 时，它随时可能被 launchd 拉起并写 S。

    不论进程是否在运行都判占用；pid 记为 0 表示这不是某个具体进程。
    """
    if sys.platform != "darwin":
        return []
    plist = os.path.join(os.path.expanduser("~"), DAEMON_PLIST)
    if os.path.lexists(plist):
        return [BusyProcess(0, "launchd", "daemon-service", plist)]
    return []
