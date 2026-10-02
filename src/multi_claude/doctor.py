"""`multi-claude doctor`：只读检查多账号环境是否健康（方案 v0.2 §5.1.2）。

所有检查都不修改任何东西、不读凭据：登录检查沿用 identity.probe 的存在性探测。
每条非 ok 结果的 message 里给出修复命令，由用户自己执行。
配置文件或迁移记录损坏时不抛异常，而是记成对应检查项的 error，其余不依赖配置的检查照常执行，
这样 `doctor --json` 在任何状态下都输出合法 JSON。
"""

import os
import platform as py_platform
import shutil
import stat
from typing import List, NamedTuple, Optional, Tuple

from . import accounts, identity, launcher, migrate, routes, shellpath
from .config import IDENTITY_DEFAULT, Account, Config, ConfigError, load_config
from .fsutil import expand

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"


class Check(NamedTuple):
    id: str
    level: str
    # 检查对象：账号名、变量名、路径等；全局检查用 "-"
    subject: str
    message: str


def run_checks() -> List[Check]:
    checks: List[Check] = []
    checks.append(_check_journal())
    config, config_check = _load()
    checks.append(config_check)
    checks.append(_check_claude_on_path())
    checks.extend(_check_auth_env())
    if config is None:
        return checks
    checks.append(_check_bin_on_path(config))
    for account in config.accounts.values():
        checks.extend(_check_account(config, account))
    checks.extend(_check_routes(config))
    return checks


def _check_routes(config: Config) -> List[Check]:
    """路由启用时检查 claude-auto 与每条规则（方案 feature-directory-routing §5.1.6）。"""
    if not config.routes_enabled:
        return []
    checks: List[Check] = []
    status = routes.router_status(config)
    path = routes.router_path(config.bin_dir)
    if status == "ok":
        checks.append(Check("router", LEVEL_OK, routes.ROUTER_NAME, path))
    else:
        hint = {"missing": "is missing", "stale": "is out of date",
                "conflict": "is taken by a file multi-claude does not manage"}[status]
        checks.append(Check("router", LEVEL_ERROR, routes.ROUTER_NAME,
                            "{} {}; run `multi-claude apply`".format(path, hint)))
    for rule in config.route_rules:
        if config.find(rule.account) is None:
            checks.append(Check("route-account", LEVEL_ERROR, rule.path,
                                "uses account {!r}, which is not registered; run `multi-claude route {} --remove` "
                                "or register the account".format(rule.account, rule.path)))
        if not os.path.isdir(expand(rule.path)):
            checks.append(Check("route-path", LEVEL_WARN, rule.path,
                                "the directory does not exist, so this route is ignored"))
    if config.route_default is not None and config.find(config.route_default) is None:
        checks.append(Check("route-account", LEVEL_ERROR, "(default)",
                            "uses account {!r}, which is not registered; run `multi-claude route --no-default` "
                            "or register the account".format(config.route_default)))
    return checks


def _check_journal() -> Check:
    try:
        journal = migrate.load_journal()
    except migrate.JournalError as exc:
        return Check("journal", LEVEL_ERROR, "-", "{}; see the README on abandoning a migration".format(exc))
    if journal is not None:
        name = journal.get("name")
        return Check("journal", LEVEL_ERROR, str(name),
                     "an unfinished migration exists; run `multi-claude migrate-default {}`".format(name))
    return Check("journal", LEVEL_OK, "-", "no unfinished migration")


def _load() -> Tuple[Optional[Config], Check]:
    try:
        config, exists = load_config()
    except ConfigError as exc:
        return None, Check("config", LEVEL_ERROR, "-", str(exc))
    if not exists:
        # 没有配置也照常检查全局项；默认配置里没有账号，按账号的检查自然为空。
        return config, Check("config", LEVEL_WARN, "-", "no configuration yet; run `multi-claude add NAME`")
    return config, Check("config", LEVEL_OK, "-", "configuration is valid")


def _check_claude_on_path() -> Check:
    found = shutil.which("claude")
    if found is None:
        return Check("claude-on-path", LEVEL_ERROR, "claude", "`claude` is not on PATH; install Claude Code")
    return Check("claude-on-path", LEVEL_OK, "claude", found)


def _check_auth_env() -> List[Check]:
    checks = []
    for variable in migrate.isolation_env_variables():
        if not os.environ.get(variable):
            continue
        # 只报变量名，绝不输出变量的值（多为凭据）。
        if variable in launcher.AUTH_OVERRIDE_ENV:
            message = "set in this shell; launchers clear it, but running `claude` directly uses it"
        else:
            message = "set in this shell; every account launched from it will share it"
        checks.append(Check("auth-env", LEVEL_WARN, variable, message))
    if not checks:
        checks.append(Check("auth-env", LEVEL_OK, "-", "no variable overrides the accounts' logins"))
    return checks


def _path_entries() -> List[str]:
    return [os.path.abspath(os.path.expanduser(entry))
            for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]


def _check_bin_on_path(config: Config) -> Check:
    bin_dir = expand(config.bin_dir)
    if bin_dir not in _path_entries():
        return Check("bin-on-path", LEVEL_WARN, bin_dir,
                     "the launcher directory is not on PATH; " + shellpath.path_hint(
                         bin_dir, os.environ.get("SHELL"), py_platform.system()))
    return Check("bin-on-path", LEVEL_OK, bin_dir, "on PATH")


def _check_account(config: Config, account: Account) -> List[Check]:
    name = account.name
    directory = accounts.account_dir(config, name)
    checks: List[Check] = []

    status = accounts.launcher_status(config, name)
    if status == "ok":
        checks.append(Check("launcher", LEVEL_OK, name, "up to date"))
    else:
        hint = {"missing": "the launcher is missing",
                "stale": "the launcher is out of date",
                "conflict": "the launcher path is taken by a file multi-claude does not manage"}[status]
        checks.append(Check("launcher", LEVEL_ERROR, name, "{}; run `multi-claude apply`".format(hint)))

    expected = launcher.launcher_path(expand(config.bin_dir), name)
    found = shutil.which(launcher.LAUNCHER_PREFIX + name)
    # 找不到时由 bin-on-path 或 launcher 检查报告，这里只管“PATH 上先找到了别的同名文件”。
    if found is not None and os.path.realpath(found) != os.path.realpath(expected):
        checks.append(Check("launcher-shadowed", LEVEL_WARN, name,
                            "{} comes first on PATH and hides {}".format(found, expected)))

    checks.extend(_check_account_dir(name, directory))
    if account.identity == IDENTITY_DEFAULT:
        link = accounts.default_link_status(config, account)
        if link != "ok":
            checks.append(Check("default-link", LEVEL_ERROR, name,
                                "{} is {}; restore the link to {} or run `multi-claude migrate-default {}`".format(
                                    accounts.default_dir(), link, directory, name)))
        else:
            checks.append(Check("default-link", LEVEL_OK, name, "{} -> {}".format(accounts.default_dir(), directory)))

    for item in config.shared_items:
        path = os.path.join(directory, item)
        if os.path.islink(path) and not os.path.exists(path):
            checks.append(Check("shared-link", LEVEL_WARN, name,
                                "{} is a broken link to {}".format(path, os.readlink(path))))

    login = identity.probe(account.identity, directory)
    if login in (identity.LOGIN_NONE, identity.LOGIN_UNKNOWN):
        message = ("no login found; start `claude-{}` and log in".format(name) if login == identity.LOGIN_NONE
                   else "could not check the login (the keychain query failed)")
        checks.append(Check("login", LEVEL_WARN, name, message))
    else:
        checks.append(Check("login", LEVEL_OK, name, login))
    return checks


def _check_account_dir(name: str, directory: str) -> List[Check]:
    try:
        mode = stat.S_IMODE(os.stat(directory).st_mode)
    except OSError:
        return [Check("account-dir", LEVEL_ERROR, name, "{} does not exist".format(directory))]
    if not os.path.isdir(directory):
        return [Check("account-dir", LEVEL_ERROR, name, "{} is not a directory".format(directory))]
    if mode & 0o077:
        # 目录里有会话记录，Linux 上还有 .credentials.json，组或其他用户不应能读写。
        return [Check("account-dir", LEVEL_WARN, name,
                      "{} has mode {:o}; run `chmod 700 '{}'`".format(directory, mode, directory))]
    return [Check("account-dir", LEVEL_OK, name, directory)]


def summary(checks: List[Check]) -> dict:
    counts = {LEVEL_OK: 0, LEVEL_WARN: 0, LEVEL_ERROR: 0}
    for check in checks:
        counts[check.level] += 1
    return counts


def format_text(checks: List[Check], verbose: bool) -> List[str]:
    lines = ["[{}] {} {}: {}".format(check.level, check.id, check.subject, check.message)
             for check in checks if verbose or check.level != LEVEL_OK]
    counts = summary(checks)
    lines.append("{} ok, {} warning(s), {} error(s)".format(counts[LEVEL_OK], counts[LEVEL_WARN],
                                                             counts[LEVEL_ERROR]))
    return lines


def exit_code(checks: List[Check]) -> int:
    return 1 if any(check.level == LEVEL_ERROR for check in checks) else 0


def to_dict(checks: List[Check]) -> dict:
    return {"schema_version": 1,
            "checks": [check._asdict() for check in checks],
            "summary": summary(checks)}
