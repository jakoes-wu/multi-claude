"""写操作互斥锁。

POSIX 用 fcntl.flock，Windows 用 msvcrt 的字节锁（见 platform.lock_fd）：进程退出（包括被强杀）时
系统自动释放锁，不会出现“持锁进程已死但锁还在”的情况，也不受 PID 复用影响。
锁文件里的 PID 只用于报错时告诉用户是谁持有锁，不参与判断。
"""

import os
from typing import Optional

from . import platform


class LockBusyError(Exception):
    def __init__(self, holder_pid: Optional[str]) -> None:
        super().__init__(holder_pid)
        self.holder_pid = holder_pid


class WriteLock(object):
    def __init__(self) -> None:
        self.path = os.path.join(platform.state_dir(), "lock")
        self._fd: Optional[int] = None

    def __enter__(self) -> "WriteLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # 不能带 O_TRUNC 打开：此时还没拿到锁，截断会清掉持锁进程写的 PID。
        # 锁文件也不能用 atomic_write 写或被删除：换了 inode，两个进程会各自锁住不同的文件。
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            platform.lock_fd(fd, blocking=False)
        except (BlockingIOError, PermissionError):
            holder = os.read(fd, 64).decode("ascii", "replace").strip() or None
            os.close(fd)
            raise LockBusyError(holder)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd
        return self

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            platform.unlock_fd(self._fd)
            os.close(self._fd)
            self._fd = None
