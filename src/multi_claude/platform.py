"""平台适配：默认路径、创建与识别链接、占用检查。

支持 macOS、Linux 与原生 Windows（方案 feature-windows-support）。平台差异集中在本模块：
默认路径、链接（Windows 上目录用 junction）、文件锁、进程判活与枚举、找可执行文件；
其它模块通过 IS_WINDOWS 和这里的函数分支，POSIX 分支保持原有行为。

占用检查分两部分（方案 §5.1.7）：
- find_busy_processes：沿用 multi-codex，按“打开的文件 / 工作目录落在源目录下”判断；
- find_claude_users：Claude 进程几乎不在账号目录里常开文件，必须读进程的 argv 与环境变量，
  再加上 IDE 锁文件、LaunchAgent 服务和 bwrap PID 命名空间三项检查。
本模块只收集事实，不决定退出码，也不输出日志；由 migrate 负责汇报。
"""

import ctypes
import ctypes.util
import errno
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple, TypeVar

# fsutil 也要用本模块的链接函数；两边都用“导入模块、调用时取属性”，循环导入不出错。
from . import fsutil

IS_WINDOWS = sys.platform == "win32"

# 在全局 shell 里导出就会让所有账号共用同一份凭据或同一个后端，破坏账号隔离（方案 §4）。
ISOLATION_BREAKING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                          "ANTHROPIC_PROFILE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                          "CLAUDE_CODE_USE_FOUNDRY")

# Claude Code 只在它自己拉起的子进程（Bash 工具、hook、status line 等）里设置这个变量；
# 本工具环境里有它，说明正运行在某个 Claude 会话里（方案 §5.1.7 第 0 条）。
CHILD_SESSION_ENV = "CLAUDE_CODE_CHILD_SESSION"
# 安装成系统服务的后台 supervisor（方案 §3 依据 14）：只带 PATH、不设 CLAUDE_CONFIG_DIR，总是服务默认账号。
# macOS 是 LaunchAgent；Linux 是 systemd user unit，位置随 XDG_CONFIG_HOME（2.1.286 Linux 构建实测）。
DAEMON_SERVICE_NAME = "com.anthropic.claude-daemon"
DAEMON_PLIST = os.path.join("Library", "LaunchAgents", DAEMON_SERVICE_NAME + ".plist")
# Claude Code 的 npm 包安装目录；当前版本在其中放原生程序 bin/claude.exe，旧版本放 JS 入口。
NPM_PACKAGE_MARKER = "/@anthropic-ai/claude-code/"

TEST_MODE_ENV = "MULTI_CLAUDE_TEST_MODE"


def test_hook_value(variable: str) -> Optional[str]:
    """读取测试钩子变量；只有同时设置 MULTI_CLAUDE_TEST_MODE=1 时才生效。

    钩子能关掉检查或注入故障，生产环境里误设了某个钩子变量也不能让它起作用。
    """
    if os.environ.get(TEST_MODE_ENV) != "1":
        return None
    return os.environ.get(variable)


def is_supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux") or IS_WINDOWS


def default_source() -> str:
    """不设 CLAUDE_CONFIG_DIR 时 Claude 使用的配置目录，也是 migrate-default 唯一的迁移源。"""
    return "~/.claude"


def default_root() -> str:
    return "~/.cc"


def default_bin_dir() -> str:
    return "~/.local/bin"


def state_dir() -> str:
    """工具状态目录：config.json、lock、migrate-journal.json 所在位置。

    Windows 放 %APPDATA%\\multi-claude（漫游配置目录的惯例位置）；APPDATA 没设时按它的默认位置推算。
    """
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        base = appdata if appdata and os.path.isabs(appdata) else os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "multi-claude")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.expanduser("~/.config")
    return os.path.join(base, "multi-claude")


def launcher_suffix() -> str:
    """启动命令与目录入口的文件扩展名：Windows 用 `.cmd`（在 PATHEXT 里，cmd 与 PowerShell 都能直接运行）。"""
    return ".cmd" if IS_WINDOWS else ""


# ---- 链接（方案 feature-windows-support §5.1 C） ----

# junction 的 reparse tag；Python 3.12 前 os.path 没有 isjunction，靠它识别
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
# 没有“创建符号链接”特权：普通 Windows 未开开发者模式时建文件符号链接会得到这个错误
_ERROR_PRIVILEGE_NOT_HELD = 1314
LINK_PERMISSION_HINT = ("creating a file link needs Developer Mode or an administrator shell: "
                        "Settings > System > For developers > Developer Mode")


class LinkPermissionError(OSError):
    """Windows 上没有权限建文件符号链接。调用方据此提示开启开发者模式或排除该共享文件。"""


def create_link(target: str, link_path: str) -> None:
    """在 link_path 创建指向 target 的软链；target 使用绝对路径，避免随工作目录变化。

    Windows 上目录建 junction：不需要管理员权限，也不需要开发者模式；文件只能建符号链接，
    没有权限时抛 LinkPermissionError。不退而复制文件：复制后两个账号各改各的，共享就失效了。
    """
    if not IS_WINDOWS:
        os.symlink(target, link_path)
        return
    if os.path.isdir(target):
        _create_junction(target, link_path)
        return
    _create_file_symlink(target, link_path)


def _create_file_symlink(target: str, link_path: str, directory: bool = False) -> None:
    try:
        os.symlink(target, link_path, target_is_directory=directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == _ERROR_PRIVILEGE_NOT_HELD:
            raise LinkPermissionError(errno.EPERM, LINK_PERMISSION_HINT, link_path)
        raise


def _create_junction(target: str, link_path: str) -> None:
    # junction 只能指向本机磁盘上的绝对路径；网络路径要用符号链接，而那需要开发者模式。
    if target.startswith("\\\\") or target.startswith("//"):
        raise OSError(errno.EINVAL, "a directory link cannot point to a network path on Windows", target)
    target = os.path.abspath(target)
    try:
        import _winapi
        create = getattr(_winapi, "CreateJunction", None)
    except ImportError:
        create = None
    if create is not None:
        create(target, link_path)
        return
    proc = subprocess.run([system_program("cmd.exe"), "/c", "mklink", "/J", link_path, target], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True)
    if proc.returncode != 0:
        raise OSError(errno.EIO, "mklink /J failed (exit {}): {}".format(proc.returncode, proc.stderr.strip()),
                      link_path)


def copy_link(source: str, link_path: str) -> None:
    """把链接 source 原样复制到 link_path（复制目录树时用）。

    POSIX 与原实现相同：按原样复制链接目标。Windows 上源是 junction 就建 junction；
    目录符号链接仍建目录符号链接。目标不存在时两种链接都照样建，与 POSIX 的悬空软链一致。
    """
    if not IS_WINDOWS:
        os.symlink(os.readlink(source), link_path)
        return
    target = read_link(source)
    if is_junction(source):
        _create_junction(target, link_path)
    else:
        _create_file_symlink(target, link_path, directory=_is_directory_entry(source))


def is_junction(path: str) -> bool:
    """path 本身是不是 junction（不跟随）；非 Windows 恒为 False。"""
    if not IS_WINDOWS:
        return False
    check = getattr(os.path, "isjunction", None)
    if check is not None:
        return check(path)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def is_link(path: str) -> bool:
    """path 本身是软链或 junction。os.path.islink 对 junction 返回 False，凡要“认出链接”的地方都用本函数。"""
    return os.path.islink(path) or is_junction(path)


def remove_link(path: str) -> None:
    """只删除链接本身，不碰它指向的内容。

    Windows 上 junction 和目录符号链接都是“目录项”，os.unlink 删不掉，要用 os.rmdir；
    rmdir 对链接只删链接，不会删目标目录里的东西。
    """
    if IS_WINDOWS and (is_junction(path) or (os.path.islink(path) and _is_directory_entry(path))):
        os.rmdir(path)
        return
    os.unlink(path)


def _is_directory_entry(path: str) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10))


def read_link(path: str) -> str:
    """读链接目标。Windows 上 junction 的目标可能带 `\\\\?\\` 前缀，去掉后与写入时的路径一致。"""
    target = os.readlink(path)
    if IS_WINDOWS and target.startswith("\\\\?\\") and not target.upper().startswith("\\\\?\\UNC\\"):
        target = target[4:]
    return target


# ---- 文件锁与按偏移读取 ----

# Windows 的字节锁是强制锁：锁住的区域别的进程读不了。锁在远超文件长度的位置，
# 文件内容（锁文件里的进程号、交接日志）照常可读；Windows 允许锁定文件末尾之后的区域。
LOCK_OFFSET = 0x7FFFFFFF
_LOCK_RETRY_INTERVAL = 0.05


def lock_fd(fd: int, blocking: bool) -> None:
    """给 fd 加排它锁。非阻塞时拿不到锁抛 BlockingIOError（与 flock 的行为一致）。

    不论成败，返回时文件位置都在 0：调用方加锁失败后会从头读持锁进程号。
    """
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    import msvcrt
    try:
        while True:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                # 只有“被别人锁着”才重试；其它错误（坏描述符等）直接抛，免得阻塞模式下死循环
                if exc.errno not in (errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)):
                    raise
                if not blocking:
                    raise BlockingIOError(errno.EAGAIN, "file is locked by another process")
            # 不用 LK_LOCK：它只重试 10 次（约 10 秒）就抛错，阻塞语义要求一直等
            time.sleep(_LOCK_RETRY_INTERVAL)
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def unlock_fd(fd: int) -> None:
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt
    try:
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, 0, os.SEEK_SET)


def read_at(fd: int, size: int, offset: int) -> bytes:
    """从 offset 处读 size 字节。Windows 没有 os.pread，用 lseek + read（调用方已持锁）。"""
    if not IS_WINDOWS:
        return os.pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


# ---- 找可执行文件与共享冲突重试 ----

# Windows 自带程序所在的子目录（相对 SystemRoot\System32）
_SYSTEM_PROGRAMS = {"cmd.exe": "", "powershell.exe": os.path.join("WindowsPowerShell", "v1.0")}


def system_program(name: str) -> str:
    """Windows 自带的 cmd.exe / powershell.exe 的绝对路径。

    不能只写程序名：CreateProcess 找程序时先查当前目录，用户在放了同名程序的项目目录里运行就会执行到它。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return os.path.join(root, "System32", _SYSTEM_PROGRAMS[name], name)


def short_path(path: str) -> str:
    """Windows 上取路径的 8.3 短名（全 ASCII）；取不到（卷上关了短名、路径不存在）时原样返回。非 Windows 原样返回。

    .cmd 文件由 cmd.exe 按控制台代码页读取，不按 UTF-8：路径里有中文时，写进启动命令的 Python 路径会被读成乱码。
    """
    if not IS_WINDOWS:
        return path
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel32.GetShortPathNameW.restype = ctypes.c_uint32
    size = kernel32.GetShortPathNameW(path, None, 0)
    if not size:
        return path
    buffer = ctypes.create_unicode_buffer(size)
    if not kernel32.GetShortPathNameW(path, buffer, size):
        return path
    return buffer.value


def which(name: str, path: Optional[str] = None) -> Optional[str]:
    """按 PATH（path 为 None 时取本进程的 PATH）找可执行文件的完整路径。

    Windows 上不用 shutil.which：它会先查当前目录，在某个项目目录里运行时可能执行到目录里放的同名程序。
    这里只查 PATH 中的绝对路径目录，扩展名按 PATHEXT；名字本身带路径时原样检查。
    """
    if not IS_WINDOWS:
        return shutil.which(name, path=path)
    extensions = [ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if ext]
    if os.path.dirname(name):
        for candidate in [name] + [name + ext for ext in extensions]:
            if os.path.isfile(candidate):
                return candidate
        return None
    if os.path.splitext(name)[1].upper() in (ext.upper() for ext in extensions):
        candidates = [name]
    else:
        candidates = [name + ext for ext in extensions]
    search = os.environ.get("PATH", "") if path is None else path
    for directory in search.split(os.pathsep):
        directory = directory.strip().strip('"')
        if not directory or not os.path.isabs(directory):
            continue
        for candidate in candidates:
            full = os.path.join(directory, candidate)
            if os.path.isfile(full):
                return full
    return None


def find_git_bash() -> Optional[str]:
    """Claude Code 在 Windows 上执行 hook / statusLine 用的 Git Bash，查找顺序与它相同（方案 §3 W2）。"""
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured and os.path.isfile(configured):
        return configured
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.isfile(candidate):
            return candidate
    git = which("git")
    if git:
        candidate = os.path.normpath(os.path.join(git, "..", "..", "bin", "bash.exe"))
        if os.path.isfile(candidate):
            return candidate
    return None


_T = TypeVar("_T")
# 杀毒软件、索引服务、Claude 本身短暂打开文件时，Windows 上替换 / 改名会失败（拒绝访问 5、共享冲突 32）
_SHARING_ERRORS = (5, 32)
_SHARING_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def retry_on_sharing_violation(func: Callable[..., _T], *args: object) -> _T:
    """Windows 上遇共享冲突按 50/100/200/400/800 ms 退避重试，最后一次的错误原样抛出；POSIX 直接调用。"""
    if not IS_WINDOWS:
        return func(*args)
    for delay in _SHARING_RETRY_DELAYS:
        try:
            return func(*args)
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in _SHARING_ERRORS:
                raise
        time.sleep(delay)
    return func(*args)


def is_sharing_violation(exc: BaseException) -> bool:
    """Windows 上文件被其它程序占用导致的失败（改名、替换、删除时常见）。"""
    return IS_WINDOWS and isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in _SHARING_ERRORS


def can_create_file_link() -> bool:
    """当前用户能否建文件符号链接（方案 C4）。POSIX 恒为真；Windows 上在临时目录里试建一个再删掉。"""
    if not IS_WINDOWS:
        return True
    import tempfile
    directory = tempfile.mkdtemp(prefix="multi-claude-link-")
    try:
        target = os.path.join(directory, "target")
        with open(target, "w"):
            pass
        try:
            _create_file_symlink(target, os.path.join(directory, "link"))
        except LinkPermissionError:
            return False
        return True
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def in_use_message(path: str, exc: BaseException) -> str:
    """改名遇占用时给用户的提示（方案 feature-windows-support E2）。"""
    return ("{} is in use by another program (winerror {}); close all Claude sessions, IDEs and terminals "
            "opened in it, then retry".format(path, getattr(exc, "winerror", None)))


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
    if IS_WINDOWS:
        # Windows 没有 lsof 与 /proc。占用由 find_claude_users 的全进程表兜住（任何 Claude 进程都判占用），
        # 其余程序占着文件时，migrate 的改名会失败并按“被占用”退出（方案 §5.1 E1/E2）。
        return []
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
            if value.startswith("/") and fsutil.is_under(value, root) and pid not in found:
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
        if target and fsutil.is_under(target, root):
            return usage, target
    fd_dir = os.path.join(base, "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    for fd in fds:
        target = _readlink_quiet(os.path.join(fd_dir, fd))
        if target and target.startswith("/") and fsutil.is_under(target, root):
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

    终端启动的 claude 的 argv[0] 是 `claude`，但 Claude 自己拉起的子进程不一定：原生程序拉起子进程时
    argv[0] 就是 process.execPath——官方安装是 `.../claude/versions/<版本>`，npm 安装是
    `.../@anthropic-ai/claude-code/bin/claude.exe`；后台会话宿主的 argv[0] 是 `claude bg-pty-host`；
    旧版 npm 包是 `node .../bin/claude`。不能只看 argv[1]，否则 `ssh claude` 之类会被误判进来。
    """
    argv0 = argv[0] if argv else ""
    if IS_WINDOWS:
        # Windows 上路径分隔符是 `\`、大小写不敏感、程序名带 .exe；统一成 POSIX 的样子再按同一套规则比较
        argv = [_windows_comparable(item) for item in argv]
        argv0 = argv[0] if argv else ""
        exe = _windows_comparable(exe)
    base = os.path.basename(argv0)
    if base == "claude" or base.startswith("claude "):
        return True
    if "/claude/versions/" in argv0 or "/claude/versions/" in exe:
        return True
    if NPM_PACKAGE_MARKER in argv0 or NPM_PACKAGE_MARKER in exe:
        return True
    if "--bg-pty-host" in argv:
        return True
    if base in ("node", "bun") and len(argv) > 1:
        script = argv[1]
        if os.path.basename(script) == "claude" or "@anthropic-ai/claude-code" in script:
            return True
    return False


def _windows_comparable(value: str) -> str:
    value = value.replace("\\", "/").lower()
    return value[:-4] if value.endswith(".exe") else value


def config_dir_of(env: Dict[str, str]) -> str:
    """该进程的 Claude 配置目录：与 Claude 的 we() 相同，CLAUDE_CONFIG_DIR 为空时用 <HOME>/.claude。

    Windows 上 Claude 的家目录取自 USERPROFILE（方案 §3 W6）。
    """
    configured = env.get("CLAUDE_CONFIG_DIR")
    if configured:
        return configured
    return os.path.join(env.get("USERPROFILE" if IS_WINDOWS else "HOME", "~"), ".claude")


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
                # Windows 读不到任何进程的环境，分不清它属于哪个账号，只能一律判占用（方案 §5.1 E1）
                detail = ("(any Claude process blocks migration on Windows)" if IS_WINDOWS
                          else "(environment not readable)")
                busy.append(BusyProcess(process.pid, command, "claude-process", detail))
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
    # Windows 读不到任何其它进程的环境，这是常态而不是检查失败：上面已把所有 Claude 进程判为占用。
    if processes and readable == 0 and not IS_WINDOWS:
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
    if IS_WINDOWS:
        return _list_processes_windows(excluded)
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


# PowerShell 默认按控制台代码页输出，路径里有中文会乱码；先把输出编码切成 UTF-8。
# 没有控制台时设置会失败，用 try 吞掉：编码退回代码页只会让非 ASCII 路径变乱码，程序名 claude.exe 仍认得出
_WIN32_PROCESS_QUERY = ("try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}; "
                        "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine "
                        "| ConvertTo-Json -Compress")
# WMI 冷启动可能要二三十秒（CI 上实测第一次查询超过 30 秒），留足余量；迁移是低频操作，多等不影响日常
_WIN32_PROCESS_TIMEOUT = 90


def _list_processes_windows(excluded: Set[int]) -> Tuple[List[ProcessInfo], Set[int]]:
    """Windows：用 PowerShell 的 Win32_Process 取所有进程的 pid、程序路径与命令行。

    Windows 读不到其它进程的环境变量，env 一律记为 None；find_claude_users 因此把任何 Claude 进程都判为占用。
    """
    try:
        proc = subprocess.Popen([system_program("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command",
                                 _WIN32_PROCESS_QUERY],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise BusyCheckError("cannot run powershell: {}".format(exc))
    try:
        stdout, _ = proc.communicate(timeout=_WIN32_PROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise BusyCheckError("powershell did not list processes within {} seconds".format(_WIN32_PROCESS_TIMEOUT))
    if proc.returncode != 0:
        raise BusyCheckError("powershell exited with code {}".format(proc.returncode))
    return parse_win32_processes(stdout.decode("utf-8", "replace"), excluded | {proc.pid}), {proc.pid}


def parse_win32_processes(text: str, excluded: Set[int]) -> List[ProcessInfo]:
    """解析 ConvertTo-Json 的输出：多个进程是数组，只有一个时是单个对象。输出不是合法 JSON 时抛 BusyCheckError。"""
    text = text.strip().lstrip("\ufeff")
    try:
        data = json.loads(text) if text else []
    except ValueError:
        raise BusyCheckError("cannot parse the process list from powershell")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise BusyCheckError("unexpected process list from powershell")
    processes = []
    for item in data:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid in excluded:
            continue
        exe = item.get("ExecutablePath")
        command = item.get("CommandLine")
        argv = split_command_line(command) if isinstance(command, str) else []
        processes.append(ProcessInfo(pid, argv, None, exe if isinstance(exe, str) else ""))
    return processes


def split_command_line(command: str) -> List[str]:
    """按 Windows 自己的规则（CommandLineToArgvW）把命令行拆成 argv；非 Windows 上（只在测试里用到）按空白拆。"""
    if not command:
        return []
    if not IS_WINDOWS:
        return command.split()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        return command.split()
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


def _pid_alive_windows(pid: int) -> bool:
    """只查询、不碰目标进程：OpenProcess 打开查询句柄，看退出码是否仍是 STILL_ACTIVE。

    打不开且错误是“参数无效”说明没有这个 pid；其它错误（如拒绝访问）说明进程在但属于别人，按存活处理，
    与 POSIX 上 os.kill 得到 PermissionError 时一致。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER  # type: ignore[attr-defined]
    try:
        code = ctypes.c_uint32(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


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
    # 绝不能在 Windows 上用 os.kill(pid, 0)：那里它会调用 TerminateProcess，直接结束目标进程（比如 IDE）。
    if IS_WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def daemon_service_path() -> Optional[str]:
    """`claude daemon install` 写下的服务文件路径；不支持的平台返回 None。

    Linux 上 Claude 按它自己进程的 XDG_CONFIG_HOME 取位置，这里用本工具的环境推算，二者通常一致。
    """
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), DAEMON_PLIST)
    if sys.platform.startswith("linux"):
        config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        return os.path.join(config_home, "systemd", "user", DAEMON_SERVICE_NAME + ".service")
    return None


def _daemon_service() -> List[BusyProcess]:
    """第 5 条：装了系统服务形态的 supervisor 时，它随时可能被 launchd / systemd 拉起并写 S。

    该服务设了自动重启（Linux 为 Restart=always），所以不论进程此刻是否在运行都判占用；
    pid 记为 0 表示这不是某个具体进程。
    """
    path = daemon_service_path()
    if path is not None and os.path.lexists(path):
        manager = "launchd" if sys.platform == "darwin" else "systemd"
        return [BusyProcess(0, manager, "daemon-service", path)]
    return []
