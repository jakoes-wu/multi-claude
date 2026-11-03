"""statusLine 钩子入口（方案 feature-hook-latency）。

状态栏每次刷新都会启动一次钩子，它的耗时直接加在显示上，所以这里只导入 os、sys、tempfile：
读完 stdin 后，把用量采集交给一个与 Claude 断开的后台进程，自己立刻 exec 原命令。
采集逻辑本身在 statusline.capture，只在后台进程里导入。

不要在本模块顶层导入 multi_claude 的其它模块：那会把 config、platform（含 ctypes）等
重新带回显示路径。
"""

import os
import sys
import tempfile

HOOK_COMMAND = "statusline-hook"
# Windows 上后台采集进程运行的隐藏子命令（方案 feature-windows-support F1）
CAPTURE_COMMAND = "statusline-capture"
SHELL = "/bin/sh"
# Windows 包装命令前缀记下的 MSYS2_ARG_CONV_EXCL 原值；原来没设时记为这个值
MSYS_ORIG_ENV = "MULTI_CLAUDE_MSYS_ORIG"
MSYS_EXCL_ENV = "MSYS2_ARG_CONV_EXCL"
MSYS_UNSET = "__unset__"
# 设为 1 时在本进程同步采集（v0.2.0 的行为）。只供测试判定“不写快照”的场景，不写进 README。
SYNC_ENV = "MULTI_CLAUDE_HOOK_SYNC"


def main(args: list) -> int:
    """钩子：后台采集用量，再把进程交给 `/bin/sh -c <原命令>`。

    成功时不返回（exec）。只有准备 stdin 或 exec 本身失败时返回 127，这时状态栏为空，
    原因写到 stderr（Claude 只把它记进调试日志）。采集无论成败都不影响显示。
    """
    if len(args) != 1:
        sys.stderr.write("usage: multi-claude {} COMMAND\n".format(HOOK_COMMAND))
        return 2
    original = args[0]
    data = sys.stdin.buffer.read()
    if sys.platform == "win32":
        return _main_windows(original, data)
    if os.environ.get(SYNC_ENV) == "1":
        _capture(data)
    else:
        _capture_in_background(data)
    try:
        # stdin 已被读完，原命令需要同样的输入：放进一个已 unlink 的临时文件再接到 fd 0。
        # 用 exec 而不是子进程，原命令的输出、退出码与收到的信号才与 Claude 直接执行时一致。
        replay = tempfile.TemporaryFile()
        replay.write(data)
        replay.flush()
        replay.seek(0)
        os.dup2(replay.fileno(), 0)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(SHELL, [SHELL, "-c", original])
    except OSError as exc:
        sys.stderr.write("multi-claude {}: cannot run {}: {}\n".format(HOOK_COMMAND, SHELL, exc))
    return 127


def _capture_in_background(data: bytes) -> None:
    """两次 fork + setsid，让采集在一个与 Claude 断开的孙进程里运行。

    中间进程立即退出并在这里被回收，不留僵尸；孙进程归 init 所有，不在 Claude 的进程组里。
    孙进程先把 stdio 指向 /dev/null：继承来的 stdout 只要还开着，Claude 就可能一直等它关闭，
    状态栏又会被采集拖慢。fork 失败只是少采一次，不影响显示。
    """
    try:
        pid = os.fork()
    except OSError:
        return
    if pid > 0:
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return
    # 中间进程与孙进程：无论发生什么都必须 os._exit，绝不能回到上面的 exec 路径。
    try:
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        _capture(data)
    except BaseException:  # noqa: BLE001 — 后台进程的任何异常都只能吞掉
        pass
    os._exit(0)


def _capture(data: bytes) -> None:
    try:
        from datetime import datetime, timezone

        from . import statusline
        statusline.capture(data, os.environ, datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 — 采集是旁路，任何异常都不能影响状态栏
        pass


def _main_windows(original: str, data: bytes) -> int:
    """Windows 分支：没有 fork 和 exec，后台采集与原命令都用子进程。

    原命令交给 Git Bash 执行，与 Claude 在 Windows 上执行 statusLine 用的 shell 相同（方案 §3 W2）；
    它的输出直接写到本进程继承来的 stdout，退出码原样返回。
    """
    import subprocess

    if os.environ.get(SYNC_ENV) == "1":
        _capture(data)
    else:
        _start_capture_process(data, subprocess)
    from . import platform
    bash = platform.find_git_bash()
    if bash is None:
        sys.stderr.write("multi-claude {}: cannot find Git Bash; install Git for Windows or set "
                         "CLAUDE_CODE_GIT_BASH_PATH\n".format(HOOK_COMMAND))
        return 127
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return subprocess.call([bash, "-c", original], env=original_command_env(os.environ),
                               stdin=_replay_file(data))
    except OSError as exc:
        sys.stderr.write("multi-claude {}: cannot run {}: {}\n".format(HOOK_COMMAND, bash, exc))
        return 127


def _replay_file(data: bytes):
    replay = tempfile.TemporaryFile()
    replay.write(data)
    replay.flush()
    replay.seek(0)
    return replay


def original_command_env(environ) -> dict:
    """原命令看到的环境：还原包装前缀改掉的 MSYS2_ARG_CONV_EXCL，去掉记录原值的变量。

    包装前缀为关掉 Git Bash 的路径改写设了 MSYS2_ARG_CONV_EXCL='*'，它会随环境传给原命令调用的程序，
    改变它们处理 /c/... 这类参数的方式；还原后原命令的环境与不包装时相同。
    """
    env = dict(environ)
    if MSYS_ORIG_ENV not in env:
        return env
    original = env.pop(MSYS_ORIG_ENV)
    if original == MSYS_UNSET:
        env.pop(MSYS_EXCL_ENV, None)
    else:
        env[MSYS_EXCL_ENV] = original
    return env


def _start_capture_process(data: bytes, subprocess) -> None:
    """起一个与 Claude 控制台断开的 Python 进程跑 statusline-capture，把 stdin 内容交给它。

    用 CREATE_NO_WINDOW（给新进程一个看不见的控制台）而不是 DETACHED_PROCESS（没有控制台）：pipx 环境里的
    python.exe 只是转接程序，会再起一个真正的 python.exe；父进程没有控制台时，Windows 给这个子进程新开一个
    可见的控制台窗口，Claude 每回复一次就闪一下（2026-11-03 实机）。隐藏的控制台会被子进程沿用，不再弹窗。

    stdout / stderr 必须显式给 DEVNULL：Windows 上只要设了一个标准句柄，没设的会继承本进程的，
    后台进程就会一直握着 Claude 读状态栏输出的管道，状态栏要等采集结束才显示。起不来只是少采一次。
    """
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_entry.py")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen([sys.executable, "-I", entry, CAPTURE_COMMAND], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        return
    try:
        proc.stdin.write(data)
    except OSError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def capture_main() -> int:
    """statusline-capture：从 stdin 读状态栏数据并采集用量。只由 Windows 的钩子在后台进程里调用。"""
    _capture(sys.stdin.buffer.read())
    return 0
