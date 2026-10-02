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
SHELL = "/bin/sh"
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
