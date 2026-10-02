"""动作列表：计划阶段产出、执行阶段逐个执行的最小单元，以及统一的输出格式。

所有写命令都先生成完整的动作列表，只要其中有一个 conflict 就什么都不写（方案 §5.1.1），
所以“计划”与“执行”必须分开：计划阶段只读文件系统，副作用全部放在 run 回调里。
"""

import sys
from typing import Callable, List, Optional

CREATE = "create"
UPDATE = "update"
DELETE = "delete"
UNCHANGED = "unchanged"
CONFLICT = "conflict"
# 不需要改动、但值得告诉用户的情况，例如共享目录里没有某个条目。
SKIP = "skip"

LOG_PREFIX = "[multi-claude]"


class Action(object):
    def __init__(self, status: str, kind: str, path: str, reason: str = "",
                 run: Optional[Callable[[], None]] = None, quiet: bool = False) -> None:
        self.status = status
        # 对象类型：config、account-dir、launcher、shared-link、backup 等
        self.kind = kind
        self.path = path
        self.reason = reason
        self.run = run
        # 不改变任何东西、又会在每条命令里重复出现的提示（如共享目录里缺某一项）：默认不打印，--verbose 时打印。
        self.quiet = quiet

    @property
    def changes(self) -> bool:
        return self.status in (CREATE, UPDATE, DELETE)


def has_conflict(actions: List[Action]) -> bool:
    return any(action.status == CONFLICT for action in actions)


def print_action(action: Action, dry_run: bool = False) -> None:
    status = action.status + (" (dry-run)" if dry_run and action.changes else "")
    line = "{} {} {} {}".format(LOG_PREFIX, status, action.kind, action.path)
    if action.reason:
        line += " ({})".format(action.reason)
    stream = sys.stderr if action.status == CONFLICT else sys.stdout
    print(line, file=stream)


def info(message: str) -> None:
    print("{} {}".format(LOG_PREFIX, message))


def warn(message: str) -> None:
    print("{} warning: {}".format(LOG_PREFIX, message), file=sys.stderr)


def error(message: str, phase: Optional[str] = None, path: Optional[str] = None) -> None:
    """错误输出：带阶段和路径，让用户不必翻代码就能知道失败在哪一步、哪个文件。"""
    parts = [LOG_PREFIX, "error"]
    if phase:
        parts.append("phase={}".format(phase))
    if path:
        parts.append("path={}".format(path))
    print("{}: {}".format(" ".join(parts), message), file=sys.stderr)
