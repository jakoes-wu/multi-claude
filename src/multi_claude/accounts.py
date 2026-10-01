"""账号收敛引擎：对比新旧配置与实际文件状态，生成并执行动作列表（方案 §5.1.1、§5.1.2）。

所有写命令（init、add、proxy、env、args、remove、apply，以及迁移最后的登记步骤）都走这里：
1. 调用方算出新配置；
2. plan() 只读地生成完整动作列表，并就地更新新配置里的 managed_links；
3. 有冲突就直接返回 3，什么都不写；
4. 否则先原子写入 config.json，再逐个执行文件动作。

先写配置再执行文件动作，是为了让“文件动作中途失败”可以靠重跑收敛：
配置已经是新的，下次 plan() 会补齐缺的文件。反过来先改文件再写配置，
失败时会留下配置里没有记录的链接，之后再也不会被清理。

与 multi-codex 相比多了两条冲突规则（方案 §5.1.8）：dir 身份账号的启动命令里那串路径
一经生成就不得被改变（macOS 登录绑定在它上面）；大小写不敏感的文件系统上，账号名必须与
磁盘上的目录名逐字相同。
"""

import json
import os
from typing import FrozenSet, Iterable, List, Optional, Set, Tuple, Union

from . import launcher, platform, routes, shared
from .actions import (CONFLICT, CREATE, DELETE, SKIP, UNCHANGED, UPDATE, Action, error,
                      has_conflict, info, print_action, warn)
from .config import (IDENTITY_DEFAULT, PROXY_INHERIT, PROXY_VARS, Account, Config, config_path,
                     dump_config, save_config)
from .fsutil import KIND_DIR, KIND_MISSING, atomic_write, entry_kind, expand, read_text

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_BUSY = 4

# orphan_scope 取这个值时，清理启动命令目录里所有不在配置中的受管启动命令。
ALL_ORPHANS = "all"
NO_ORPHANS: FrozenSet[str] = frozenset()


def account_dir(config: Config, name: str) -> str:
    """账号目录路径字符串：只展开 `~` 并转绝对路径，不做 realpath。

    dir 身份账号的 macOS 钥匙串服务名由这串字符决定，与 Claude 的 we() 一样不能解析软链。
    """
    return os.path.join(expand(config.root), name)


def default_dir() -> str:
    """不设 CLAUDE_CONFIG_DIR 时 Claude 使用的配置目录（$HOME/.claude 展开后）。"""
    return expand(platform.default_source())


def render_launcher(config: Config, account: Account) -> str:
    return launcher.render(account.name, account.identity, account_dir(config, account.name), default_dir(),
                           account.proxy, config.effective_env(account), config.effective_args(account))


def plan(old: Config, new: Config, *, config_exists: bool = True,
         orphan_scope: Union[str, FrozenSet[str]] = NO_ORPHANS,
         assume_dirs: Iterable[str] = (), adopt_accounts: FrozenSet[str] = frozenset()) -> List[Action]:
    """生成从 old 收敛到 new 所需的全部动作。只读文件系统，不做任何修改。

    orphan_scope：要清理的孤儿启动命令的账号名集合（casefold 后比较）；ALL_ORPHANS 表示全部清理，
    NO_ORPHANS 表示不清理。只有 apply 和 remove 需要清理孤儿。
    assume_dirs：视为已存在的账号目录。迁移在移动数据前做预检时，目标目录还不存在，
    但迁移完成后它一定存在，不应计划“创建目录”。
    adopt_accounts：要接管已有共享软链的账号名集合（casefold 后比较），只有 `add --adopt` 会传入。
    """
    actions: List[Action] = []
    assumed = {expand(path) for path in assume_dirs}
    old_root, new_root = expand(old.root), expand(new.root)
    old_bin, new_bin = expand(old.bin_dir), expand(new.bin_dir)

    # 工具不会搬迁账号目录（搬了 macOS 上就会失去登录），所以已有账号时改根目录只能判冲突。
    if old.accounts and old_root != new_root:
        actions.append(Action(CONFLICT, "config", config_path(),
                              "cannot change root from {} to {} while accounts are registered".format(
                                  old_root, new_root)))

    planned_deletes: Set[str] = set()
    for account in new.accounts.values():
        old_account = old.find(account.name)
        if old_account is not None and old_account.name != account.name:
            # 只改了大小写：在区分大小写的文件系统上会新建另一份目录和启动命令，
            # 而旧启动命令因为按大小写不敏感匹配，既不算孤儿也不会被清理。
            actions.append(Action(CONFLICT, "config", config_path(),
                                  "account {!r} is registered as {!r}; renaming is not supported".format(
                                      account.name, old_account.name)))
            continue
        directory = os.path.join(new_root, account.name)
        actions.extend(_plan_account_dir(directory, account, assumed))
        actions.extend(_plan_launcher(new_bin, new, account))
        if old_account is not None and old_bin != new_bin:
            actions.extend(_plan_launcher_delete(old_bin, old_account.name, planned_deletes,
                                                 "bin_dir changed"))
        actions.extend(shared.plan_shared(new, account, directory, old.shared_dir,
                                          adopt=account.name.casefold() in adopt_accounts))

    for old_account in old.accounts.values():
        if new.find(old_account.name) is None:
            actions.extend(_plan_launcher_delete(old_bin, old_account.name, planned_deletes,
                                                 "account removed"))
            actions.extend(shared.plan_remove_links(old_account, os.path.join(old_root, old_account.name),
                                                    old.shared_dir))

    if orphan_scope:
        for name, path in launcher.scan_managed(new_bin).items():
            if new.find(name) is not None:
                continue
            if orphan_scope == ALL_ORPHANS or name.casefold() in orphan_scope:
                actions.extend(_plan_launcher_delete(new_bin, name, planned_deletes,
                                                     "orphan launcher", path=path))

    actions.extend(_plan_router(old, new))

    config_changed = not config_exists or dump_config(old) != dump_config(new)
    config_action = Action(CREATE if not config_exists else (UPDATE if config_changed else UNCHANGED),
                           "config", config_path())
    return [config_action] + actions


def _plan_router(old: Config, new: Config) -> List[Action]:
    """路由入口 claude-auto 的动作与冲突（方案 feature-directory-routing §5.1.3）。

    只认路由标记：账号启动命令、用户自己的同名文件一律不覆盖、不删除。
    """
    actions: List[Action] = []
    new_path = routes.router_path(new.bin_dir)
    old_path = routes.router_path(old.bin_dir)
    if old_path != new_path and routes.is_router(old_path):
        actions.append(Action(DELETE, "router", old_path, "bin_dir changed", lambda: os.unlink(old_path)))
    if not new.routes_enabled:
        if routes.is_router(new_path):
            actions.append(Action(DELETE, "router", new_path, "no routes configured", lambda: os.unlink(new_path)))
        return actions

    referenced = [(rule.account, "route {} uses account {!r}, which is not registered; run "
                                 "`multi-claude route {} --remove` first".format(rule.path, rule.account, rule.path))
                  for rule in new.route_rules]
    if new.route_default is not None:
        referenced.append((new.route_default, "the default route uses account {!r}, which is not registered; "
                                              "run `multi-claude route --no-default` first".format(new.route_default)))
    for name, reason in referenced:
        if new.find(name) is None:
            actions.append(Action(CONFLICT, "config", config_path(), reason))
    if new.find(routes.RESERVED_ACCOUNT) is not None:
        # 账号 auto 的启动命令也叫 claude-auto，两者必然互相覆盖。
        actions.append(Action(CONFLICT, "router", new_path, "claude-auto is the directory router; an account "
                                                             "named 'auto' cannot coexist with routes"))

    content = routes.render_router(new)
    kind = entry_kind(new_path)
    if kind == KIND_MISSING:
        actions.append(Action(CREATE, "router", new_path, run=_write_launcher(new_path, content)))
    elif not routes.is_router(new_path):
        actions.append(Action(CONFLICT, "router", new_path, "already exists and is not the multi-claude router"))
    elif read_text(new_path) == content and os.access(new_path, os.X_OK):
        actions.append(Action(UNCHANGED, "router", new_path))
    else:
        actions.append(Action(UPDATE, "router", new_path, run=_write_launcher(new_path, content)))
    return actions


def _plan_account_dir(directory: str, account: Account, assumed: Set[str]) -> List[Action]:
    if directory in assumed:
        return []
    kind = entry_kind(directory)
    if kind == KIND_MISSING:
        if account.identity == IDENTITY_DEFAULT:
            # default 身份的目录是迁移搬过来的原 ~/.claude，新建一个空目录只会让启动命令落到空账号。
            return [Action(CONFLICT, "account-dir", directory,
                           "the directory of the default-identity account is missing")]
        return [Action(CREATE, "account-dir", directory, run=lambda: _make_private_dir(directory))]
    # 账号目录本身是软链（例如用户手工迁移后留的链接）也可以接受，只要最终指向目录。
    if kind == KIND_DIR or os.path.isdir(directory):
        actual = _actual_entry_name(directory)
        if actual is not None and actual != account.name:
            # 大小写不敏感的文件系统上，`add work` 会命中磁盘上的 `Work/`；启动命令却会写成 `work`，
            # 路径字符串变了，macOS 上的登录（按字符串哈希）随之失效。
            return [Action(CONFLICT, "account-dir", directory,
                           "directory exists as {!r} (differs only in case); the launcher would use {!r}, "
                           "which is not the path you logged in with; register it as {!r}".format(
                               actual, account.name, actual))]
        return [Action(UNCHANGED, "account-dir", directory)]
    return [Action(CONFLICT, "account-dir", directory, "exists but is not a directory")]


def _actual_entry_name(directory: str) -> Optional[str]:
    """isdir 为真但父目录里没有逐字相同的名字时，返回磁盘上的实际名称；否则返回 None。

    大小写敏感的文件系统上，`Work/` 与 `work/` 可以并存，`work` 能在 listdir 中逐字找到，不受影响。
    """
    parent, name = os.path.split(directory)
    try:
        entries = os.listdir(parent)
    except OSError:
        return None
    if name in entries:
        return None
    for entry in entries:
        if entry.casefold() == name.casefold():
            return entry
    return None


def _make_private_dir(directory: str) -> None:
    # 目录里会保存 .credentials.json（Linux）和会话记录，所以账号目录和根目录都只允许本人访问。
    parent = os.path.dirname(directory)
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700)
    os.mkdir(directory, 0o700)
    os.chmod(directory, 0o700)


def _plan_launcher(bin_dir: str, config: Config, account: Account) -> List[Action]:
    name = account.name
    path = launcher.launcher_path(bin_dir, name)
    content = render_launcher(config, account)
    kind = entry_kind(path)
    if kind == KIND_MISSING:
        return [Action(CREATE, "launcher", path, run=_write_launcher(path, content))]
    owner = launcher.managed_account(path)
    if owner is None:
        return [Action(CONFLICT, "launcher", path, "already exists and is not managed by multi-claude")]
    if owner.casefold() != name.casefold():
        return [Action(CONFLICT, "launcher", path, "managed by multi-claude for account {!r}".format(owner))]
    current = read_text(path)
    if current == content and os.access(path, os.X_OK):
        return [Action(UNCHANGED, "launcher", path)]
    if account.identity != IDENTITY_DEFAULT and current is not None:
        old_dir = launcher.rendered_account_dir(current)
        new_dir = account_dir(config, name)
        if old_dir is not None and old_dir != new_dir:
            # 典型原因：HOME 变了而状态目录与 bin_dir 写的是绝对路径。改写这一行会让 macOS 上的
            # 钥匙串服务名变掉，账号随之“掉登录”，所以只能交给用户决定（方案 §5.1.8）。
            return [Action(CONFLICT, "launcher", path,
                           "would change the login-bound account path ({} -> {})".format(old_dir, new_dir))]
    return [Action(UPDATE, "launcher", path, run=_write_launcher(path, content))]


def _write_launcher(path: str, content: str):
    def run() -> None:
        atomic_write(path, content, mode=launcher.LAUNCHER_MODE)
    return run


def _plan_launcher_delete(bin_dir: str, name: str, planned: Set[str], reason: str,
                          path: Optional[str] = None) -> List[Action]:
    path = path or launcher.launcher_path(bin_dir, name)
    if path in planned or entry_kind(path) == KIND_MISSING:
        return []
    owner = launcher.managed_account(path)
    if owner is None or owner.casefold() != name.casefold():
        return [Action(SKIP, "launcher", path, "not managed by multi-claude for this account; left untouched")]
    planned.add(path)
    return [Action(DELETE, "launcher", path, reason, lambda: os.unlink(path))]


def execute(old: Config, new: Config, actions: List[Action], *, dry_run: bool) -> int:
    """按 §5.1.1 的顺序执行：有冲突则全部不写；否则先写配置，再逐个执行文件动作。"""
    for action in actions:
        print_action(action, dry_run=dry_run)
    if has_conflict(actions):
        info("nothing was changed because of the conflicts above")
        return EXIT_CONFLICT
    if dry_run:
        return EXIT_OK
    config_action = actions[0]
    if config_action.changes:
        try:
            save_config(new)
        except OSError as exc:
            error(str(exc), phase="config", path=config_action.path)
            return EXIT_ERROR
    for action in actions[1:]:
        if not action.changes or action.run is None:
            continue
        try:
            action.run()
        except OSError as exc:
            # 配置已经写入，重跑同一命令或 `apply` 会从这里继续收敛。
            error("{}; rerun the command or `multi-claude apply` to finish".format(exc),
                  phase=action.kind, path=action.path)
            return EXIT_ERROR
    return EXIT_OK


def converge(old: Config, new: Config, *, config_exists: bool, dry_run: bool,
             orphan_scope: Union[str, FrozenSet[str]] = NO_ORPHANS, assume_dirs: Iterable[str] = (),
             adopt_accounts: FrozenSet[str] = frozenset()) -> int:
    actions = plan(old, new, config_exists=config_exists, orphan_scope=orphan_scope,
                   assume_dirs=assume_dirs, adopt_accounts=adopt_accounts)
    return execute(old, new, actions, dry_run=dry_run)


def launcher_status(config: Config, name: str) -> str:
    """list 命令用：ok、missing、conflict 或 stale（内容与配置不一致）。"""
    account = config.find(name)
    path = launcher.launcher_path(expand(config.bin_dir), name)
    if entry_kind(path) == KIND_MISSING:
        return "missing"
    owner = launcher.managed_account(path)
    if owner is None or owner.casefold() != name.casefold() or account is None:
        return "conflict"
    expected = render_launcher(config, account)
    return "ok" if read_text(path) == expected and os.access(path, os.X_OK) else "stale"


def default_link_status(config: Config, account: Account) -> str:
    """default 身份账号的 ~/.claude 软链状态：ok、broken（指向别处或断链）或 missing。"""
    source = default_dir()
    kind = entry_kind(source)
    if kind == KIND_MISSING:
        return "missing"
    target = account_dir(config, account.name)
    if os.path.isdir(source) and os.path.isdir(target) and os.path.realpath(source) == os.path.realpath(target):
        return "ok"
    return "broken"


# ---- 只提示、不改变退出码的检查（方案 §5.1.3、§5.1.5） ----

def warn_launch_settings(config: Config, names: Optional[Iterable[str]] = None, *,
                         note_invalid: bool = False) -> None:
    """对指定账号（None 表示全部）输出可变参数告警与“settings 覆盖代理”告警。

    note_invalid 为真时（list 命令），另外注明哪些 settings 文件因不是合法 JSON 而没有检查。
    """
    selected = list(config.accounts.values()) if names is None else [
        account for account in (config.find(name) for name in names) if account is not None]
    for account in selected:
        message = launcher.variadic_warning(config.effective_args(account))
        if message:
            warn("claude-{}: {}".format(account.name, message))
        if account.proxy == PROXY_INHERIT:
            continue
        invalid: List[str] = []
        for path, key in settings_proxy_overrides(config, account, invalid):
            warn("{} sets {} in env, which overrides the proxy set by claude-{}".format(path, key, account.name))
        if note_invalid:
            for path in invalid:
                info("{} is not valid JSON; not checked for proxy overrides of claude-{}".format(
                    path, account.name))


def settings_proxy_overrides(config: Config, account: Account,
                             invalid: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """列出会覆盖启动命令代理的 settings 来源：(文件或 'inline --settings', 变量名)。

    Claude 会把 .claude.json、各级 settings（含 --settings 指定的文件）的 env 写进进程环境，
    同名变量以 settings 为准（方案 §3 依据 13）。这里只读顶层 env 的键名，不读、不输出值；
    文件不存在或不是合法 JSON 时跳过，后者记入 invalid（调用方传入时）。托管设置的位置由管理员决定，不检查。
    """
    directory = account_dir(config, account.name)
    global_config = (os.path.join(os.path.expanduser("~"), ".claude.json")
                     if account.identity == IDENTITY_DEFAULT else os.path.join(directory, ".claude.json"))
    sources: List[Tuple[str, Optional[str]]] = [
        (os.path.join(directory, "settings.json"), None),
        (global_config, None),
    ]
    args = config.effective_args(account)
    for index, arg in enumerate(args):
        value = None
        if arg == "--settings" and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--settings="):
            value = arg[len("--settings="):]
        if value is None:
            continue
        if value.lstrip().startswith("{"):
            sources.append(("inline --settings", value))
        else:
            sources.append((launcher.expand_value(value), None))
    proxy_keys = {var.upper() for var in PROXY_VARS}
    found: List[Tuple[str, str]] = []
    for label, inline in sources:
        env = _settings_env_keys(label, inline)
        if env is None:
            if invalid is not None:
                invalid.append(label)
            continue
        for key in env:
            if key.upper() in proxy_keys:
                found.append((label, key))
    return found


def _settings_env_keys(path: str, inline: Optional[str]) -> Optional[List[str]]:
    """返回顶层 env 的键名；文件不存在返回空列表，存在但不是合法 JSON 返回 None。"""
    try:
        if inline is not None:
            data = json.loads(inline)
        else:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
    except OSError:
        return []
    except ValueError:
        return None
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return []
    return [key for key in env if isinstance(key, str)]
