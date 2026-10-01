"""写操作互斥锁。

用 fcntl.flock 实现：进程退出（包括被 SIGKILL）时内核自动释放锁，
不会出现“持锁进程已死但锁还在”的情况，也不受 PID 复用影响。
锁文件里的 PID 只用于报错时告诉用户是谁持有锁，不参与判断。
"""

import fcntl
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
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
