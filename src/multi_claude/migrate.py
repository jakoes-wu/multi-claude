"""默认目录迁移 `migrate-default`（方案 §5.1.6，事务机制沿用 multi-codex §5.1.5）。

把 S（固定为 ~/.claude）变成账号目录 T（<root>/<名称>），并在 S 位置留兼容软链。
迁移后的账号登记为 default 身份：启动命令不设 CLAUDE_CONFIG_DIR，Claude 仍按默认规则使用
~/.claude（即软链）、~/.claude.json 和不带后缀的钥匙串条目，所以登录不会失效。
~/.claude.json、钥匙串与 .credentials.json 一律不碰。
这是本工具唯一可能丢数据的操作，所以按“事务记录 + 按实际状态续跑”的方式实现：

- 事务记录 migrate-journal.json 只提供路径、模式和参数；
- 真实进度一律从文件系统推断（observe + classify），阶段字段只在状态有歧义时区分；
- 进程在“动作已完成、记录还没更新”之间被杀，重跑也能判断正确。

记号：S 源位置，T 目标目录，B copy 模式下的备份目录（与 S 同目录，改名是原子操作）。
核心不变量：只要 B 存在，原始数据就在 B 里，任何分支都不得删除 T 或丢掉事务记录。
"""

import errno
import json
import os
import sys
import time
from typing import Optional

from . import accounts, platform
from .actions import error, info, warn
from .config import IDENTITY_DEFAULT, PROXY_INHERIT, Account, Config, load_config, normalize_proxy
from .fsutil import (KIND_DIR, KIND_LINK, KIND_MISSING, atomic_write, build_manifest, copy_tree,
                     diff_manifests, entry_kind, expand, is_under, remove_path, same_target)

JOURNAL_VERSION = 1

PHASE_PLANNED = "planned"
PHASE_COPYING = "copying"
PHASE_MOVED = "moved"
PHASE_PARKED = "parked"
PHASE_LINKED = "linked"
PHASE_REGISTERED = "registered"
PHASE_ROLLED_BACK = "rolled-back"

MODE_RENAME = "rename"
MODE_COPY = "copy"


class InjectedFailure(Exception):
    """测试钩子抛出的可捕获异常，用来触发回滚路径。"""


def _test_hook(point: str) -> None:
    """测试专用注入点；只有同时设置 MULTI_CLAUDE_TEST_MODE=1 时才生效，正常使用时什么也不做。

    MULTI_CLAUDE_TEST_FAIL_AT：在该点抛出可捕获异常，走回滚。
    MULTI_CLAUDE_TEST_CRASH_AT：在该点直接 os._exit(137)，模拟进程被杀，不做任何清理。
    """
    if platform.test_hook_value("MULTI_CLAUDE_TEST_FAIL_AT") == point:
        raise InjectedFailure(point)
    if platform.test_hook_value("MULTI_CLAUDE_TEST_CRASH_AT") == point:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(137)


def journal_path() -> str:
    return os.path.join(platform.state_dir(), "migrate-journal.json")


class JournalError(Exception):
    """事务记录存在但无法读取或内容不完整；此时不能猜测迁移进度，只能交给用户处理。"""


_JOURNAL_KEYS = ("name", "source", "target", "backup", "mode", "phase")


def load_journal() -> Optional[dict]:
    path = journal_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            journal = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise JournalError("cannot read the migration journal {}: {}".format(path, exc))
    if not isinstance(journal, dict) or any(not isinstance(journal.get(key), str) for key in _JOURNAL_KEYS):
        raise JournalError("the migration journal {} is incomplete".format(path))
    return journal


def _save_journal(journal: dict) -> None:
    atomic_write(journal_path(), json.dumps(journal, indent=2, ensure_ascii=False) + "\n", mode=0o600)


def _set_phase(journal: dict, phase: str) -> None:
    journal["phase"] = phase
    _save_journal(journal)


def _delete_journal() -> None:
    try:
        os.unlink(journal_path())
    except FileNotFoundError:
        pass


# 当前正在执行的步骤名，只用于错误输出里的 phase=，取值与方案 §9 一致。
_current_step = {"name": "precheck"}


def _enter_step(name: str) -> None:
    _current_step["name"] = name


class MigrationStop(Exception):
    """迁移按设计停下（冲突、占用、S 被重建等），带退出码，不触发回滚。"""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code


def migrate_default(name: str, source: Optional[str], force_copy: bool, keep_backup: bool,
                    proxy: Optional[str], skip_process_check: bool, dry_run: bool) -> int:
    """迁移入口。调用方已持有写锁。

    source 只供内部续跑使用（回滚完成后按记录里的源重新开始）；命令行没有 --source，
    因为 default 身份依赖“Claude 不设变量时用 $HOME/.claude”，迁移其它目录没有意义。
    有事务记录时一律按记录续跑，参数以记录为准；名称与记录不同返回 2。
    """
    _enter_step("precheck")
    journal = load_journal()
    source_path = journal["source"] if journal is not None else expand(source or platform.default_source())
    code = _check_environment(source_path)
    if code:
        return code
    if journal is not None:
        return _resume(journal, name, source, force_copy, keep_backup, proxy, skip_process_check, dry_run)

    config, config_exists = load_config()
    target_path = os.path.join(expand(config.root), name)
    normalized_proxy = normalize_proxy(proxy) if proxy is not None else None

    try:
        done = _precheck_without_journal(config, config_exists, name, source_path, target_path,
                                         normalized_proxy, dry_run)
    except MigrationStop as stop:
        if stop.args[0]:
            error(stop.args[0], phase="precheck", path=source_path)
        return stop.code
    if done is not None:
        return done

    if not skip_process_check:
        code = _busy_check(source_path)
        if code:
            return code

    root = expand(config.root)
    mode = MODE_COPY if force_copy or not _same_device(source_path, root) else MODE_RENAME
    backup = "{}.multi-claude-bak.{}".format(source_path, time.strftime("%Y%m%d_%H%M%S"))
    if dry_run:
        info("(dry-run) would move {} to {} using {} mode and leave a link at {}".format(
            source_path, target_path, mode, source_path))
        return accounts.EXIT_OK

    if not os.path.isdir(root):
        os.makedirs(root, mode=0o700)
    journal = {
        "version": JOURNAL_VERSION,
        "name": name,
        "source": source_path,
        "target": target_path,
        "backup": backup,
        "mode": mode,
        "phase": PHASE_PLANNED,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "copy": force_copy,
        "keep_backup": keep_backup,
        "proxy": normalized_proxy,
        "skip_process_check": skip_process_check,
    }
    _save_journal(journal)
    _test_hook("journal-planned")
    info("migrating {} to {} ({} mode)".format(source_path, target_path, mode))
    return _run(journal)


def _check_environment(source_path: str) -> int:
    """检查本工具自身的环境（方案 §5.1.6 预检增量）；返回 0 表示可以继续。

    占用检查第 3 条排除了本工具自身，看不到 `CLAUDE_CONFIG_DIR=~/.claude multi-claude …`
    这种只对单条命令生效的写法，所以在这里单独拦：此时迁移会和一个指向 S 的环境并存，
    当前 shell 里启动的 Claude 会在迁移窗口内写 S。
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        if os.path.realpath(config_dir) == os.path.realpath(source_path):
            error("CLAUDE_CONFIG_DIR in this shell points to {}; run `unset CLAUDE_CONFIG_DIR` "
                  "and rerun".format(source_path), phase="precheck", path=source_path)
            print("  pid={} command=multi-claude usage=config-dir path={}".format(os.getpid(), config_dir),
                  file=sys.stderr)
            return accounts.EXIT_BUSY
        warn("CLAUDE_CONFIG_DIR is set to {}; running `claude` directly in this shell uses that "
             "directory, not {}".format(config_dir, source_path))
    for variable in ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_CUSTOM_OAUTH_URL"):
        if variable in os.environ:
            warn("{} is set in this shell; it changes where `claude` looks for the login".format(variable))
    warn_isolation_env()
    return accounts.EXIT_OK


def warn_isolation_env() -> None:
    for variable in platform.ISOLATION_BREAKING_ENV:
        if os.environ.get(variable):
            warn("{} is set in the environment; every account launched from this shell "
                 "will share it".format(variable))


def _precheck_without_journal(config: Config, config_exists: bool, name: str, source: str,
                              target: str, proxy: Optional[str], dry_run: bool) -> Optional[int]:
    """没有事务记录时的预检（§5.1.5 第 1 步）。返回退出码表示已结束，返回 None 表示继续迁移。"""
    registered = config.find(name)
    source_kind = entry_kind(source)

    if source_kind == KIND_LINK and entry_kind(target) != KIND_MISSING and same_target(source, target):
        if registered is not None and registered.name == name:
            info("already migrated: {} -> {}".format(source, target))
            return accounts.EXIT_OK
        if registered is None:
            info("{} already links to {}; registering the account".format(source, target))
            return _register(config, config_exists, name, proxy, dry_run)

    if registered is not None:
        raise MigrationStop(accounts.EXIT_CONFLICT,
                            "account {!r} is already registered".format(registered.name))
    if source_kind == KIND_LINK:
        raise MigrationStop(accounts.EXIT_CONFLICT, "source is a link to another location")
    if source_kind == KIND_MISSING:
        raise MigrationStop(accounts.EXIT_CONFLICT, "source does not exist")
    if source_kind != KIND_DIR:
        raise MigrationStop(accounts.EXIT_CONFLICT, "source is not a directory")
    if entry_kind(target) != KIND_MISSING:
        raise MigrationStop(accounts.EXIT_CONFLICT, "target {} already exists".format(target))
    if is_under(target, os.path.realpath(source)) or is_under(os.path.realpath(source), target):
        raise MigrationStop(accounts.EXIT_CONFLICT, "source and target must not contain each other")

    _check_registration_conflicts(config, config_exists, name, target, proxy)
    return None


def _check_registration_conflicts(config: Config, config_exists: bool, name: str, target: str,
                                  proxy: Optional[str]) -> None:
    """预检登记阶段会不会冲突（R3）：必须在移动数据之前发现，移动后就无法干净退出了。"""
    _check_default_identity(config, name)
    new = _config_with_account(config, name, proxy)
    actions = accounts.plan(config, new, config_exists=config_exists, assume_dirs=[target])
    conflicts = [action for action in actions if action.status == accounts.CONFLICT]
    if conflicts:
        for action in conflicts:
            error(action.reason, phase="precheck", path=action.path)
        raise MigrationStop(accounts.EXIT_CONFLICT, "registration would conflict; nothing was moved")


def _check_default_identity(config: Config, name: str) -> None:
    """一台机器只有一个 ~/.claude，已有别的 default 身份账号时不能再迁移出第二个。"""
    existing = config.default_account()
    if existing is not None and existing.name.casefold() != name.casefold():
        raise MigrationStop(accounts.EXIT_CONFLICT,
                            "another account already has the default identity ({!r})".format(existing.name))


def _config_with_account(config: Config, name: str, proxy: Optional[str]) -> Config:
    new = config.copy()
    existing = new.find(name)
    if existing is None:
        new.accounts[name] = Account(name, proxy or PROXY_INHERIT, False, identity=IDENTITY_DEFAULT)
    elif proxy is not None:
        existing.proxy = proxy
    return new


def _register(config: Config, config_exists: bool, name: str, proxy: Optional[str], dry_run: bool) -> int:
    _check_default_identity(config, name)
    new = _config_with_account(config, name, proxy)
    return accounts.converge(config, new, config_exists=config_exists, dry_run=dry_run)


def _busy_check(source: str) -> int:
    """占用检查第 0–5 条（方案 §5.1.7）：迁移期间不能有任何 Claude 进程。

    任何账号的 Claude 进程都会写 $HOME/.claude（依据 12）：rename 窗口内写入会重建真实的 S，
    copy 模式下复制完成到 park 之间写入的内容会随备份一起被删掉。返回 0 可以继续，
    1 检查本身失败，4 有占用。
    """
    if platform.CHILD_SESSION_ENV in os.environ:
        error("multi-claude is running inside a Claude Code session; run migrate-default from a plain "
              "terminal with no Claude session", phase="busy-check", path=source)
        print("  pid={} command=multi-claude usage=inside-claude-session path={}".format(os.getpid(), source),
              file=sys.stderr)
        print("  note: {} can also be inherited by screen, tmux or programs started by Claude; if you are "
              "sure this is not a Claude session, rerun as `env -u {} multi-claude ...`".format(
                  platform.CHILD_SESSION_ENV, platform.CHILD_SESSION_ENV), file=sys.stderr)
        return accounts.EXIT_BUSY
    try:
        busy = platform.find_busy_processes(source)
        users = platform.find_claude_users(source)
    except platform.BusyCheckError as exc:
        error("cannot check whether {} is in use ({}); close every Claude session and rerun with "
              "--skip-process-check to proceed anyway".format(source, exc), phase="busy-check", path=source)
        return accounts.EXIT_ERROR
    for message in users.warnings:
        warn(message)
    if users.unreadable:
        # Linux 上通常是已退出或不可 dump 的进程；macOS 上还包括系统隐去环境的 Apple 自带程序。
        info("the environment of {} process(es) could not be read, so they were not checked for "
             "CLAUDE_CONFIG_DIR (for example, macOS hides it for Apple's own programs such as the "
             "system shells)".format(users.unreadable))
    seen = {process.pid for process in busy}
    busy.extend(process for process in users.busy if process.pid == 0 or process.pid not in seen)
    if not busy:
        return accounts.EXIT_OK
    error("{} is in use; close these processes and rerun:".format(source), phase="busy-check", path=source)
    for process in busy:
        print("  pid={} command={} usage={} path={}".format(process.pid or "-", process.command, process.usage,
                                                           process.path),
              file=sys.stderr)
    parent = os.getppid()
    parent_config = [process.path for process in busy if process.pid == parent and process.usage == "config-dir"]
    if parent_config:
        print("  note: the shell running this command exports CLAUDE_CONFIG_DIR={}; run "
              "`unset CLAUDE_CONFIG_DIR` first".format(parent_config[0]), file=sys.stderr)
    elif any(process.pid == parent for process in busy):
        print("  note: the shell running this command has its working directory inside the source; "
              "cd out of it first", file=sys.stderr)
    if users.stop_commands:
        print("  every Claude account writes to {} (bridge-spawn, state/, ide/), so close all Claude sessions; "
              "to stop background sessions run:".format(source), file=sys.stderr)
        for command in users.stop_commands:
            print("    " + command, file=sys.stderr)
    if any(process.usage == "daemon-service" for process in busy):
        print("  the background service is installed and launchd/systemd may restart it at any time; run "
              "`claude daemon uninstall` first and install it again after the migration", file=sys.stderr)
    return accounts.EXIT_BUSY


def _same_device(source: str, root: str) -> bool:
    probe = root if os.path.isdir(root) else os.path.dirname(root)
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    return os.stat(source).st_dev == os.stat(probe).st_dev


def _resume(journal: dict, name: str, source: Optional[str], force_copy: bool, keep_backup: bool,
            proxy: Optional[str], skip_process_check: bool, dry_run: bool) -> int:
    """按事务记录续跑一次未完成的迁移。

    参数一律以记录为准：续跑时用户可能忘了带上首次的 --keep-backup 等选项，
    按命令行走会删掉本想保留的备份。名称（或内部续跑传入的源）不同说明用户想做另一次迁移，
    返回 2 并提示先完成记录中的那次；其它选项不同只给出警告。
    """
    _enter_step("resume")
    if name.casefold() != journal["name"].casefold() or (
            source is not None and expand(source) != journal["source"]):
        error("an unfinished migration of {!r} from {} exists; rerun `multi-claude migrate-default {}` "
              "to finish it first".format(journal["name"], journal["source"], journal["name"]),
              phase="resume", path=journal_path())
        return accounts.EXIT_USAGE
    if keep_backup != journal.get("keep_backup") or (proxy is not None and
                                                     normalize_proxy(proxy) != journal.get("proxy")):
        warn("options differ from the unfinished migration; using the recorded options "
             "(keep_backup={}, proxy={})".format(journal.get("keep_backup"), journal.get("proxy")))
    if skip_process_check != bool(journal.get("skip_process_check")):
        warn("--skip-process-check differs from the unfinished migration; using the recorded value ({})".format(
            bool(journal.get("skip_process_check"))))
    if force_copy and journal.get("mode") != MODE_COPY:
        warn("--copy is ignored while resuming; the recorded mode is {}".format(journal.get("mode")))
    info("resuming migration of {} (recorded phase: {})".format(journal["source"], journal["phase"]))
    if dry_run:
        state = _observe(journal)
        info("(dry-run) current state: source={} target={} backup={}".format(*state))
        return accounts.EXIT_OK
    return _run(journal)


def _observe(journal: dict):
    """返回 (S 的状态, T 是否存在, B 是否存在)。S 的状态：link-target、link-other、dir、missing、other。"""
    source, target, backup = journal["source"], journal["target"], journal["backup"]
    kind = entry_kind(source)
    if kind == KIND_LINK:
        source_state = "link-target" if same_target(source, target) and os.path.isdir(target) else "link-other"
    elif kind in (KIND_DIR, KIND_MISSING):
        source_state = kind
    else:
        source_state = "other"
    return source_state, entry_kind(target) != KIND_MISSING, entry_kind(backup) != KIND_MISSING


def _run(journal: dict) -> int:
    """按实际状态逐步推进，直到完成或按设计停下；可捕获的异常走回滚。"""
    try:
        return _advance(journal)
    except MigrationStop as stop:
        if stop.args[0]:
            error(stop.args[0], phase=_current_step["name"], path=journal["source"])
        _print_state(journal)
        return stop.code
    except Exception as exc:  # noqa: BLE001 —— 任何未预期的异常都要尝试把数据还原到完整位置
        error("{}: {}".format(type(exc).__name__, exc), phase=_current_step["name"], path=journal["source"])
        return _rollback(journal)


def _advance(journal: dict) -> int:
    verified_this_run = False
    while True:
        source_state, target_exists, backup_exists = _observe(journal)
        phase = journal["phase"]
        mode = journal["mode"]

        # 第 1 行：回滚未完成，先把回滚做完，再按“没有事务记录”从头开始。
        if phase == PHASE_ROLLED_BACK:
            _finish_rollback(journal)
            info("previous rollback finished; starting the migration again")
            return migrate_default(journal["name"], journal["source"], bool(journal.get("copy")) or mode == MODE_COPY,
                                   bool(journal.get("keep_backup")), journal.get("proxy"),
                                   bool(journal.get("skip_process_check")), False)

        # 第 2 行：兼容软链已建好，只剩登记与清理。
        if source_state == "link-target":
            return _step_register_and_cleanup(journal)

        # 第 3 行：S 已改名为 B，还没建软链。T 必须仍在，否则（只会是人工删除）按第 10 行处理，
        # 避免在 S 位置建出一条断链。
        if source_state == KIND_MISSING and backup_exists and target_exists:
            _step_link(journal)
            continue

        # 第 4 行：已 park，但 S 又被重建。原始数据在 B，只能报错等用户处理。
        if source_state == KIND_DIR and backup_exists:
            raise MigrationStop(accounts.EXIT_ERROR, _rebuilt_message(journal, journal["backup"]))

        # 第 5 行：rename 已完成，还没建软链。
        if source_state == KIND_MISSING and not backup_exists and target_exists and mode == MODE_RENAME:
            _step_link(journal)
            continue

        if source_state == KIND_DIR and not backup_exists:
            # 第 6 行：还没移动任何数据，重新预检后开始移动。
            if not target_exists:
                _step_move(journal)
                verified_this_run = mode == MODE_COPY
                continue
            if mode == MODE_COPY and phase in (PHASE_PLANNED, PHASE_COPYING):
                # 第 7 行：复制被中断，T 不完整，删掉重来。
                info("removing incomplete copy {}".format(journal["target"]))
                remove_path(journal["target"])
                _set_phase(journal, PHASE_PLANNED)
                continue
            if mode == MODE_COPY and phase == PHASE_MOVED:
                # 第 8 行：已复制并校验，尚未 park。
                _step_park(journal, verified_this_run)
                verified_this_run = False
                continue
            if mode == MODE_RENAME:
                # 第 9 行：rename 后 S 被重建。
                raise MigrationStop(accounts.EXIT_ERROR, _rebuilt_message(journal, journal["target"]))

        # 第 10 行：只会在人工改动后出现，不自动处理。
        raise MigrationStop(accounts.EXIT_ERROR, "cannot determine the migration state automatically")


def _step_move(journal: dict) -> None:
    """第 1～3 步：重新预检、占用检查、移动数据。只在 S 是真实目录、T 与 B 都不存在时调用。

    预检或占用检查不通过时，数据一点都还没动，直接删除事务记录后停下，
    这样不会留下一条记录挡住其它命令。
    """
    _enter_step("precheck")
    config, config_exists = load_config()
    _check_registration_conflicts_or_abandon(config, config_exists, journal)
    _enter_step("busy-check")
    if not journal.get("skip_process_check"):
        code = _busy_check(journal["source"])
        if code:
            # 还没移动任何数据，放弃这次迁移，不留事务记录挡住其它命令。
            _delete_journal()
            raise MigrationStop(code)

    source, target = journal["source"], journal["target"]
    _enter_step("move")
    if journal["mode"] == MODE_RENAME:
        try:
            os.rename(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # 跨设备无法 rename：先把模式写进记录再复制，中断后续跑才会按 copy 规则处理。
            journal["mode"] = MODE_COPY
            _set_phase(journal, PHASE_PLANNED)
            info("source and target are on different file systems; switching to copy mode")
            return
        _test_hook("rename-target")
        _set_phase(journal, PHASE_MOVED)
        _test_hook("journal-moved")
        return

    _set_phase(journal, PHASE_COPYING)
    _test_hook("journal-copying")
    copied = [0]

    def on_file(_path: str) -> None:
        copied[0] += 1
        if copied[0] == 1:
            _test_hook("copy-midway")

    skipped = copy_tree(source, target, on_file)
    _test_hook("copy-done")
    for rel in skipped:
        info("skipped special file (socket/FIFO/device): {}".format(os.path.join(source, rel)))
    _verify_copy(journal)
    _set_phase(journal, PHASE_MOVED)
    _test_hook("journal-moved")


def _check_registration_conflicts_or_abandon(config: Config, config_exists: bool, journal: dict) -> None:
    try:
        _check_registration_conflicts(config, config_exists, journal["name"], journal["target"],
                                      journal.get("proxy"))
    except MigrationStop:
        _delete_journal()
        raise


def _verify_copy(journal: dict) -> None:
    """比对 S 与 T 的清单和 SHA-256；不一致时删除 T，S 不动，返回错误。"""
    _enter_step("verify")
    source_manifest, _ = build_manifest(journal["source"])
    target_manifest, _ = build_manifest(journal["target"])
    _test_hook("verify")
    mismatched = diff_manifests(source_manifest, target_manifest)
    if mismatched:
        remove_path(journal["target"])
        _delete_journal()
        shown = ", ".join(mismatched[:5]) + (" ..." if len(mismatched) > 5 else "")
        raise MigrationStop(accounts.EXIT_ERROR,
                            "copy verification failed for {} entries ({}); the copy was removed and "
                            "the source is untouched".format(len(mismatched), shown))


def _step_park(journal: dict, verified_this_run: bool) -> None:
    """copy 模式：再做一次占用检查，然后把 S 改名为 B（§5.1.5 第 3 步第 4 点）。

    复制和校验可能耗时几分钟，这期间写进 S 的数据不会出现在 T 里，所以 park 前必须再查一次。
    续跑时（本次运行没有校验过）还要重新校验，确认 T 仍与 S 一致。
    """
    _enter_step("park")
    if not journal.get("skip_process_check"):
        code = _busy_check(journal["source"])
        if code:
            raise MigrationStop(code)
    if not verified_this_run:
        source_manifest, _ = build_manifest(journal["source"])
        target_manifest, _ = build_manifest(journal["target"])
        if diff_manifests(source_manifest, target_manifest):
            info("the source changed since it was copied; copying again")
            remove_path(journal["target"])
            _set_phase(journal, PHASE_PLANNED)
            return
    os.rename(journal["source"], journal["backup"])
    _test_hook("rename-backup")
    _set_phase(journal, PHASE_PARKED)
    _test_hook("journal-parked")


def _step_link(journal: dict) -> None:
    """第 4 步：在 S 位置建指向 T 的兼容软链。S 被别的进程重建时报错，不回滚。"""
    _enter_step("link")
    source, target = journal["source"], journal["target"]
    kind = entry_kind(source)
    if kind != KIND_MISSING:
        if kind == KIND_LINK and same_target(source, target):
            return
        where = journal["backup"] if entry_kind(journal["backup"]) != KIND_MISSING else target
        raise MigrationStop(accounts.EXIT_ERROR, _rebuilt_message(journal, where))
    platform.create_link(target, source)
    _test_hook("link")
    _set_phase(journal, PHASE_LINKED)
    _test_hook("journal-linked")


def _step_register_and_cleanup(journal: dict) -> int:
    """第 5 步：登记账号、生成启动命令，然后按记录决定是否删除 B，最后删除事务记录。"""
    _enter_step("register")
    config, config_exists = load_config()
    code = _register(config, config_exists, journal["name"], journal.get("proxy"), False)
    if code != accounts.EXIT_OK:
        raise MigrationStop(code, "data is in place but registration failed; fix the problem above "
                                  "and rerun the same command")
    _test_hook("register")
    _set_phase(journal, PHASE_REGISTERED)
    _test_hook("journal-registered")
    _enter_step("cleanup")
    backup = journal["backup"]
    if journal["mode"] == MODE_COPY and entry_kind(backup) != KIND_MISSING:
        if journal.get("keep_backup"):
            info("backup kept at {}".format(backup))
        else:
            try:
                remove_path(backup)
                _test_hook("remove-backup")
            except Exception as exc:  # noqa: BLE001 —— 迁移已经完成，删备份失败只能提示，不能让流程卡住
                warn("could not remove backup {}: {}: {}".format(backup, type(exc).__name__, exc))
    _delete_journal()
    info("migrated {} -> {}".format(journal["source"], journal["target"]))
    return accounts.EXIT_OK


def _rollback(journal: dict) -> int:
    """回滚：只把数据恢复到仍然完整的位置，只在 S 为空（或 S 未动过）时执行。"""
    _enter_step("rollback")
    source_state, target_exists, backup_exists = _observe(journal)
    try:
        if journal["mode"] == MODE_RENAME and source_state == KIND_MISSING and target_exists \
                and not backup_exists:
            os.rename(journal["target"], journal["source"])
            _delete_journal()
            info("rolled back: {} restored".format(journal["source"]))
        elif journal["mode"] == MODE_COPY and source_state == KIND_DIR and not backup_exists:
            # park 之前失败：S 没动过，T 只是副本。
            remove_path(journal["target"])
            _delete_journal()
            info("rolled back: removed the partial copy; {} is untouched".format(journal["source"]))
        elif journal["mode"] == MODE_COPY and source_state == KIND_MISSING and backup_exists:
            # 先写记录再动文件：回滚在任何一步中断，续跑表第 1 行都能接着做完。
            _set_phase(journal, PHASE_ROLLED_BACK)
            _test_hook("journal-rolled-back")
            _finish_rollback(journal)
            info("rolled back: {} restored from backup".format(journal["source"]))
        else:
            info("no automatic rollback in this state; rerun the same command to continue")
            _print_state(journal)
    except Exception as exc:  # noqa: BLE001 —— 回滚失败必须保留事务记录并报告现场
        error("rollback failed: {}: {}".format(type(exc).__name__, exc), phase="rollback",
              path=journal["source"])
        _print_state(journal)
    return accounts.EXIT_ERROR


def _finish_rollback(journal: dict) -> None:
    """完成 copy 模式 park 之后的回滚：B 改回 S，删除 T，最后删除事务记录。

    调用前事务记录必须已处于 rolled-back：每一步都可能被中断，续跑表第 1 行会再次调用本函数，
    所以每一步都要能重复执行（B 已改回就跳过，T 已删就忽略）。
    状态不是“S 为真实目录且 B 不存在”时不删除 T，抛出 MigrationStop 交给用户处理。
    """
    source, target, backup = journal["source"], journal["target"], journal["backup"]
    if entry_kind(source) == KIND_MISSING and entry_kind(backup) != KIND_MISSING:
        os.rename(backup, source)
        _test_hook("rollback-restore")
    if entry_kind(source) != KIND_DIR or entry_kind(backup) != KIND_MISSING:
        raise MigrationStop(accounts.EXIT_ERROR, "cannot finish the rollback automatically")
    remove_path(target)
    _test_hook("rollback-remove-target")
    _delete_journal()


def _rebuilt_message(journal: dict, data_location: str) -> str:
    return ("{} was recreated by another process while migrating. The complete data is in {}. "
            "Close the process that recreated it, move the recreated {} away, and rerun "
            "`multi-claude migrate-default {}`.").format(
                journal["source"], data_location, journal["source"], journal["name"])


def _print_state(journal: dict) -> None:
    """报告现场：S、T、B 的当前状态，事务记录位置，以及放弃迁移时的手工收尾步骤。"""
    source_state, target_exists, backup_exists = _observe(journal)
    lines = [
        "  source {}: {}".format(journal["source"], source_state),
        "  target {}: {}".format(journal["target"], "exists" if target_exists else "missing"),
        "  backup {}: {}".format(journal["backup"], "exists" if backup_exists else "missing"),
        "  journal {} (phase {})".format(journal_path(), journal.get("phase")),
    ]
    if entry_kind(journal_path()) != KIND_MISSING:
        data = journal["backup"] if backup_exists else journal["target"]
        lines.append("  to abandon this migration: make sure the complete data in {} is back at {}, "
                     "then delete {}".format(data, journal["source"], journal_path()))
    print("\n".join(lines), file=sys.stderr)


def pending_journal_notice() -> Optional[str]:
    journal = load_journal()
    if journal is None:
        return None
    return "an unfinished migration exists; rerun `multi-claude migrate-default {}` to finish it".format(
        journal.get("name"))


