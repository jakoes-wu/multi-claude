"""撤销 migrate-default：`restore NAME`（方案 feature-everyday-commands §5.1.4）。

记号：S = ~/.claude（default 身份账号的入口），T = 账号目录。迁移后 S 是指向 T 的软链；
restore 删掉软链、把 T 原样改名回 S，再注销账号（与 remove 相同，启动命令随之删除）。

登录不受影响：default 身份本来就不设 CLAUDE_CONFIG_DIR，用的是不带后缀的钥匙串条目（macOS）、
目录里的 .credentials.json（Linux）与 ~/.claude.json，这三样本模块都不碰，搬回后 `claude` 照常使用。

不用事务记录：每一步都是单个原子操作（unlink、同文件系统内 rename、收敛写配置），
重跑时按 S 与 T 的实际状态判断走到了哪一步（下表 A/B/C），所以中途被杀后重跑同一命令即可继续。
调用方（cli.dispatch）已持有写锁，且已确认没有未完成的迁移。
"""

import os
from typing import Optional

from . import accounts, migrate, platform
from .actions import error, info
from .config import IDENTITY_DEFAULT, Config, load_config
from .fsutil import KIND_DIR, KIND_LINK, KIND_MISSING, entry_kind

# 测试钩子：为真时把“T 与 S 的父目录在同一文件系统”判为否，用来覆盖跨文件系统的拒绝路径。
CROSS_DEVICE_HOOK = "MULTI_CLAUDE_TEST_RESTORE_CROSS_DEVICE"

STATE_LINKED = "linked"        # A：S 是指向 T 的软链，T 是真实目录
STATE_UNLINKED = "unlinked"    # B：软链已删，T 还没搬回
STATE_MOVED = "moved"          # C：T 已搬回 S，账号还登记着


def restore(name: str, skip_process_check: bool, dry_run: bool) -> int:
    """restore 入口；返回值沿用 cli 的退出码契约（0/1/2/3/4）。"""
    if migrate.load_journal() is not None:
        # _blocked_by_migration 对 --dry-run 放行；半迁移状态下的计划没有意义，dry-run 也拒绝。
        error("finish the unfinished migration before running `restore`", phase="restore")
        return accounts.EXIT_ERROR
    config, config_exists = load_config()
    source = accounts.default_dir()
    account = config.find(name)
    if account is None:
        return _not_registered(config, config_exists, name, source, dry_run)
    if account.identity != IDENTITY_DEFAULT:
        error("{} was not created by migrate-default; restore only undoes migrate-default".format(account.name),
              phase="restore")
        return accounts.EXIT_USAGE
    target = accounts.account_dir(config, account.name)

    state = _classify(source, target)
    if state is None:
        error("cannot restore automatically: {} is {} and {} is {}; nothing was changed".format(
            source, _describe(source), target, _describe(target)), phase="restore", path=source)
        return accounts.EXIT_CONFLICT

    # 先确认注销不会冲突（例如路由仍引用该账号）：搬动目录之后再发现就无法干净退出了。
    new = config.copy()
    del new.accounts[account.name]
    conflicts = [action for action in accounts.plan(config, new, config_exists=config_exists,
                                                    orphan_scope=frozenset([account.name.casefold()]))
                 if action.status == accounts.CONFLICT]
    if conflicts:
        for action in conflicts:
            error(action.reason, phase="restore", path=action.path)
        info("nothing was changed")
        return accounts.EXIT_CONFLICT

    if state in (STATE_LINKED, STATE_UNLINKED):
        if not _same_file_system(target, source):
            error("{} and {} are on different file systems; restore by hand (see README, "
                  "\"Undoing a migration by hand\"); nothing was changed".format(target, source),
                  phase="restore", path=target)
            return accounts.EXIT_CONFLICT
        if dry_run:
            if state == STATE_LINKED:
                info("(dry-run) would remove the link {}".format(source))
            info("(dry-run) would move {} to {}".format(target, source))
            info("(dry-run) would unregister {} and delete its launcher".format(account.name))
            return accounts.EXIT_OK
        if not skip_process_check:
            # 按 T 检查：B 状态下 S 已不存在，按 S 查会漏掉打开的文件、工作目录与 ide/ 锁。
            code = migrate._busy_check(target, command="restore")
            if code:
                return code
        if state == STATE_LINKED:
            os.unlink(source)
            info("restore: removed the link {}".format(source))
            migrate._test_hook("restore-unlinked")
        os.rename(target, source)
        info("restore: moved {} to {}".format(target, source))
        migrate._test_hook("restore-moved")
    elif dry_run:
        info("(dry-run) would unregister {} and delete its launcher".format(account.name))
        return accounts.EXIT_OK

    code = accounts.converge(config, new, config_exists=config_exists, dry_run=False,
                             orphan_scope=frozenset([account.name.casefold()]))
    if code == accounts.EXIT_OK:
        info("restore: unregistered {}; plain `claude` uses {} again with the same login".format(
            account.name, source))
    return code


def _classify(source: str, target: str) -> Optional[str]:
    """按 S 与 T 的实际状态判断走到了哪一步；不属于 A/B/C 的组合返回 None（交给用户处理）。"""
    source_kind = entry_kind(source)
    target_kind = entry_kind(target)
    # T 本身是软链时 rename 会搬走软链而不是目录（收敛允许这种账号目录，accounts.py 的 _plan_account_dir）。
    if target_kind == KIND_DIR:
        if source_kind == KIND_LINK and os.path.realpath(source) == os.path.realpath(target):
            return STATE_LINKED
        if source_kind == KIND_MISSING:
            return STATE_UNLINKED
        return None
    if target_kind == KIND_MISSING and source_kind == KIND_DIR:
        return STATE_MOVED
    return None


def _describe(path: str) -> str:
    kind = entry_kind(path)
    if kind == KIND_LINK:
        return "a link to {}".format(os.readlink(path))
    if kind == KIND_DIR:
        return "a directory"
    if kind == KIND_MISSING:
        return "missing"
    return "not a directory"


def _same_file_system(target: str, source: str) -> bool:
    """os.rename 只在同一文件系统内是原子的；S 此时可能不存在，所以比较 S 的父目录。"""
    if platform.test_hook_value(CROSS_DEVICE_HOOK) == "1":
        return False
    return os.stat(target).st_dev == os.stat(os.path.dirname(source)).st_dev


def _not_registered(config: Config, config_exists: bool, name: str, source: str, dry_run: bool) -> int:
    """NAME 未登记：可能已经恢复完（上次在注销中途被杀，只剩启动命令），也可能只是名字写错。

    S 是真实目录且没有任何 default 身份账号时，视为无事可做，顺带清理残留的启动命令，退出 0
    （与 remove 对未登记账号的处理一致）；其余情况按“未登记”退出 1。
    """
    if entry_kind(source) != KIND_DIR or config.default_account() is not None:
        error("account {!r} is not registered".format(name), phase="restore")
        return accounts.EXIT_ERROR
    info("{} is not registered; {} is a real directory, nothing to restore".format(name, source))
    if not config_exists:
        return accounts.EXIT_OK
    # 只清理受管标记属于该名字的启动命令；用户自己的同名文件收敛不会删除。
    return accounts.converge(config, config.copy(), config_exists=config_exists, dry_run=dry_run,
                             orphan_scope=frozenset([name.casefold()]))
