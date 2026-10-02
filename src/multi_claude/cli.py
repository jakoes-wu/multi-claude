"""命令行入口：解析参数、加锁、组装新配置，再交给收敛引擎或迁移模块。

退出码契约（方案 §5.2，沿用 multi-codex）：0 成功或已是目标状态；1 运行错误；2 参数不合法；
3 存在冲突且未做任何修改；4 迁移源目录正被占用。
"""

import argparse
import difflib
import json
import os
import platform as py_platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from . import (__version__, accounts, completion, doctor, hook, identity, launcher, migrate, platform, restore,
               routes, sessions, shellpath, statusline, usage)
from .actions import error, info, warn
from .config import (DEFAULT_SHARED_DIR, DEFAULT_SHARED_ITEMS, IDENTITY_DEFAULT, IDENTITY_DIR, Account, Config, ConfigError, RouteRule,
                     is_unshareable, load_config, normalize_proxy, parse_config, validate_env_key, validate_name,
                     validate_route_path, validate_value, config_path)
from .env import account_env
from .fsutil import KIND_FILE, KIND_LINK, KIND_MISSING, atomic_write, entry_kind, expand, read_text
from .lock import LockBusyError, WriteLock

# 源码中出现、官方文档未列出的代理变量（方案 §10）：`off` 一期不清除它们，list 时只提示。
_UNMANAGED_PROXY_VARS = ("CLAUDE_CODE_HTTP_PROXY", "CLAUDE_CODE_HTTPS_PROXY")
_ONE_DAY = timedelta(days=1)


class UsageError(Exception):
    """命令行参数不合法，对应退出码 2。"""


# (分组标题, [(命令显示名, 说明)])。命令显示名里用逗号并列的每个名字都必须是子命令。
COMMAND_GROUPS = [
    ("Getting started", [
        ("add NAME", "create an account and its launcher claude-NAME"),
        ("login NAME", "sign in to an account"),
        ("list", "show accounts, logins and usage"),
    ]),
    ("Account settings", [
        ("set NAME", "change an account: proxy and sharing"),
        ("proxy, env, args", "set the proxy, extra variables or fixed arguments of a launcher"),
        ("rename, remove", "rename or unregister an account"),
        ("migrate-default, restore", "turn the existing ~/.claude into an account, or back"),
    ]),
    ("Everyday", [
        ("usage", "5-hour and 7-day usage of each account"),
        ("route, which", "choose an account by directory (launcher claude-auto)"),
        ("handoff", "copy a session to another account"),
        ("mcp", "run `claude mcp` as an account"),
        ("run, path", "run any command as an account; print its directory"),
        ("code", "open VS Code for an account (experimental)"),
    ]),
    ("Setup and checks", [
        ("init, apply", "global settings; converge everything to config.json"),
        ("doctor", "check the setup and suggest fixes"),
        ("statusline", "record usage from the status line"),
        ("completion", "print a shell completion script"),
    ]),
]

_EXAMPLES = [
    ("multi-claude add work --proxy 7901", ""),
    ("multi-claude login work", ""),
    ("multi-claude route ~/work work", ""),
    ("multi-claude add --help", "details of one command"),
]


def grouped_command_names() -> List[str]:
    """COMMAND_GROUPS 里出现的全部子命令名（去掉 NAME 之类的参数占位）。"""
    names = []
    for _title, items in COMMAND_GROUPS:
        for shown, _text in items:
            for part in shown.split(","):
                names.append(part.split()[0])
    return names


def _help_epilog() -> str:
    lines = []
    for title, items in COMMAND_GROUPS:
        lines.append("{}:".format(title))
        for shown, text in items:
            lines.append("  {:<18}{}".format(shown, text))
        lines.append("")
    lines.append("Examples:")
    for command, text in _EXAMPLES:
        lines.append("  {:<36}{}".format(command, text).rstrip())
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    # 子命令不传 help=，argparse 就不会把它们平铺成一列；改由 epilog 按用途分组列出（方案 feature-clearer-help §5.1.1）。
    # 新增子命令时必须同时写进 COMMAND_GROUPS，测试会核对两者一致。
    parser = argparse.ArgumentParser(
        prog="multi-claude",
        description="Manage multiple Claude Code accounts: separate CLAUDE_CONFIG_DIR directories,\n"
                    "per-account launchers, proxies, environment variables and arguments.",
        epilog=_help_epilog(), formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", help="one of the commands listed below")
    sub.required = True

    p_init = sub.add_parser("init", description="create or update the global settings")
    p_init.add_argument("--root", help="directory that holds account directories (default ~/.cc)")
    p_init.add_argument("--bin-dir", help="directory for claude-<name> launchers (default ~/.local/bin)")
    p_init.add_argument("--shared-dir", help="directory whose items can be linked into accounts")
    p_init.add_argument("--shared-items", help="comma-separated items to share "
                        "(default {})".format(",".join(DEFAULT_SHARED_ITEMS)))
    _add_write_options(p_init)

    p_mig = sub.add_parser("migrate-default", description="turn the default ~/.claude into a named account "
                                                   "without losing its login")
    # 省略时依次取：未完成迁移记录里的名称、已有 default 身份账号的名称、~/.claude.json 里登录账号的邮箱。
    p_mig.add_argument("name", nargs="?")
    p_mig.add_argument("--copy", action="store_true",
                       help="copy and verify instead of renaming (used automatically across file systems)")
    p_mig.add_argument("--keep-backup", action="store_true",
                       help="in copy mode, keep the original directory as a backup")
    p_mig.add_argument("--proxy", help="proxy for the new account: port, URL, off or inherit")
    p_mig.add_argument("--skip-process-check", action="store_true",
                       help="do not check whether Claude Code is running (at your own risk)")
    _add_dry_run(p_mig)

    p_add = sub.add_parser("add", description="add an account, adopt an existing directory, or change its options")
    _add_account_options(p_add, adopt=True)
    p_add.add_argument("--config-from", metavar="OTHER",
                       help="copy settings.json from account OTHER into this account once")

    p_set = sub.add_parser("set", description="change an existing account: proxy and sharing "
                                              "(use env, args, rename for the rest)")
    _add_account_options(p_set, adopt=False)

    p_proxy = sub.add_parser("proxy", description="set the proxy of an account")
    p_proxy.add_argument("name")
    p_proxy.add_argument("value", help="port (e.g. 7901), http(s) URL, off or inherit")
    _add_write_options(p_proxy)

    p_env = sub.add_parser("env", description="set or unset extra environment variables of a launcher")
    p_env.add_argument("items", nargs="*", metavar="NAME|K=V",
                       help="account name (omit with --defaults) followed by K=V assignments")
    p_env.add_argument("--defaults", action="store_true", help="change the defaults used by every account")
    p_env.add_argument("--unset", action="append", default=[], metavar="K", help="remove a variable")
    _add_write_options(p_env)

    # `args` 的参数列表在 argparse 之前从 argv 里切走（见 _split_args_command），这里只解析 `--` 之前的部分。
    p_args = sub.add_parser("args", description="replace the fixed arguments of a launcher: args NAME -- [ARG ...]")
    p_args.add_argument("name", nargs="?")
    p_args.add_argument("--defaults", action="store_true", help="change the defaults used by every account")
    _add_write_options(p_args)

    p_remove = sub.add_parser("remove", description="unregister an account (its directory and login are kept)")
    p_remove.add_argument("name")
    _add_write_options(p_remove)

    p_apply = sub.add_parser("apply", description="converge all accounts to the configuration")
    p_apply.add_argument("-f", "--file", help="use this file as the new configuration")
    _add_write_options(p_apply)

    p_list = sub.add_parser("list", description="show accounts and their status")
    list_mode = p_list.add_mutually_exclusive_group()
    list_mode.add_argument("--verbose", action="store_true",
                           help="show every field, the global settings and the keychain details")
    list_mode.add_argument("--json", action="store_true", help="print machine-readable JSON (includes usage)")
    list_mode.add_argument("--names", action="store_true", help="print only the account names, one per line")

    p_usage = sub.add_parser("usage", description="show the last known 5-hour and 7-day usage of each account "
                                           "(from Claude Code's own cache; never reads credentials)")
    p_usage.add_argument("name", nargs="?")
    p_usage.add_argument("--json", action="store_true", help="print machine-readable JSON")

    p_doctor = sub.add_parser("doctor", description="check the accounts, launchers and environment (read-only)")
    p_doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_doctor.add_argument("--verbose", action="store_true", help="also list the checks that passed")

    p_route = sub.add_parser("route", description="choose an account by directory for claude-auto: route DIR NAME, "
                                           "route DIR --remove, route --default NAME, route --no-default")
    p_route.add_argument("path", nargs="?", metavar="DIR")
    p_route.add_argument("name", nargs="?", metavar="NAME")
    route_mode = p_route.add_mutually_exclusive_group()
    route_mode.add_argument("--remove", action="store_true", help="remove the route of DIR")
    route_mode.add_argument("--default", metavar="NAME", help="account used when no route matches")
    route_mode.add_argument("--no-default", action="store_true",
                            help="run plain claude when no route matches (the initial behaviour)")
    _add_write_options(p_route)

    p_rename = sub.add_parser("rename", description="rename an account and its launcher; the directory and login stay")
    p_rename.add_argument("old")
    p_rename.add_argument("new")
    _add_write_options(p_rename)

    # mcp 的 claude 参数在 argparse 之前从 argv 里切走（见 _split_args_command）。
    p_mcp = sub.add_parser("mcp", description="run `claude mcp ...` as an account: mcp NAME [ARG ...]")
    p_mcp.add_argument("name")

    # login 的 claude 参数与 mcp 一样，在 argparse 之前从 argv 里切走（见 _split_args_command）。
    p_login = sub.add_parser("login", description="sign in to an account: runs `claude auth login` as that account: "
                                           "login NAME [ARG ...]")
    p_login.add_argument("name")

    p_which = sub.add_parser("which", description="show which account claude-auto would use in DIR (default: here)")
    p_which.add_argument("path", nargs="?", metavar="DIR")

    # run 的命令在 argparse 之前从 argv 里切走（见 _split_args_command）。
    p_run = sub.add_parser("run", description="run a command with the environment of an account's launcher: "
                                              "run [NAME] [-- COMMAND ...]; without COMMAND it runs claude, "
                                              "without NAME it uses the account claude-auto would choose here")
    p_run.add_argument("name", nargs="?")

    p_path = sub.add_parser("path", description="print the directory of an account")
    p_path.add_argument("name")

    # code 的 VS Code 参数与 run 一样，在 argparse 之前从第一个 `--` 处切走。
    p_code = sub.add_parser("code", description="open a separate VS Code instance with the environment of an "
                                                "account (experimental): code NAME [PATH] [-- CODE_ARGS ...]")
    p_code.add_argument("name")
    p_code.add_argument("path", nargs="?", metavar="PATH")

    p_restore = sub.add_parser("restore", description="undo migrate-default: move the account directory back to "
                                                      "~/.claude and unregister the account; the login is kept")
    p_restore.add_argument("name")
    p_restore.add_argument("--skip-process-check", action="store_true",
                           help="do not check whether Claude Code is running (at your own risk)")
    _add_dry_run(p_restore)

    p_completion = sub.add_parser("completion", description="print a shell completion script")
    p_completion.add_argument("shell", choices=completion.SHELLS)

    # 钩子 statusline-hook 不是子命令：它在 main 里先于 argparse 被拦下，不出现在帮助与补全中。
    p_statusline = sub.add_parser("statusline", description="record usage from Claude's status line: wrap (install) or "
                                                     "restore (uninstall) the statusLine command in a settings FILE")
    p_statusline.add_argument("action", choices=("install", "uninstall"))
    p_statusline.add_argument("file", metavar="FILE")
    _add_dry_run(p_statusline)

    p_handoff = sub.add_parser("handoff", description="copy a Claude session to another account so that it can be resumed "
                                               "there: handoff TARGET [--from NAME] [--session ID]")
    p_handoff.add_argument("target", metavar="TARGET")
    p_handoff.add_argument("--from", dest="source", metavar="NAME",
                           help="account that has the session (default: the account of the Claude session "
                                "this runs in)")
    p_handoff.add_argument("--session", metavar="ID",
                           help="session to copy (default: the current session, else the latest one here)")
    p_handoff.add_argument("--force", action="store_true",
                           help="back up and replace a different copy of the session in TARGET")
    _add_dry_run(p_handoff)
    return parser


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="show the planned actions without changing anything")


def _add_account_options(parser: argparse.ArgumentParser, adopt: bool) -> None:
    """add 与 set 共用的选项；只有 add 有 --adopt（接管只在登记已有目录时有意义）。"""
    parser.add_argument("name")
    parser.add_argument("--proxy", help="port, URL, off or inherit (new accounts default to inherit)")
    shared_group = parser.add_mutually_exclusive_group()
    # --shared 可带一个目录：不带时用已设置的共享目录（未设置则为 ~/.claude-shared），
    # 带 DIR 时把全局共享目录改为 DIR，所有开启共享的账号的链接都改指向它（方案 feature-set-command §5.1）。
    shared_group.add_argument("--shared", dest="shared_to", nargs="?", const=True, default=None, metavar="DIR",
                              help="link the shared items into this account; with DIR, also make DIR the shared "
                                   "directory for every account (default {})".format(DEFAULT_SHARED_DIR))
    shared_group.add_argument("--no-shared", dest="no_shared", action="store_true",
                              help="do not link shared items (default for new accounts)")
    if adopt:
        parser.add_argument("--adopt", action="store_true",
                            help="take over existing links that already point to the shared items, "
                                 "so that turning sharing off later removes them too")
    parser.add_argument("--shared-exclude", action="append", default=[], metavar="ITEM",
                        help="do not link this shared item into the account (repeatable); "
                             "the link multi-claude made is removed")
    parser.add_argument("--shared-include", action="append", default=[], metavar="ITEM",
                        help="undo --shared-exclude for this item (repeatable)")
    _add_write_options(parser)


def _add_write_options(parser: argparse.ArgumentParser) -> None:
    """收敛类写命令的公共选项：默认只打印有变化的动作，--verbose 列出全部。"""
    _add_dry_run(parser)
    parser.add_argument("--verbose", action="store_true", help="also show items that are already up to date")


def _split_args_command(argv: List[str]) -> Tuple[List[str], Optional[List[str]]]:
    """子命令是 `args` 时，把第一个 `--` 之后的全部内容取出来作为参数列表；
    子命令是 `mcp` 或 `login` 时，账号名之后的全部内容原样作为 `claude mcp` / `claude auth login` 的参数（不经 argparse）；
    子命令是 `run` / `code` 时，第一个 `--` 之后的内容是要运行的命令 / 传给 VS Code 的参数。

    不能用 argparse.REMAINDER：可选位置参数加 --defaults 加 REMAINDER 时，
    `args --defaults -- --settings s` 会被解析成账号名 `--settings`（方案 §5.1.2，实测 3.10/3.11/3.13）。
    返回 (交给 argparse 的部分, 参数列表)；不是这两个子命令时参数列表为 None。
    """
    command_index = None
    for index, token in enumerate(argv):
        if token == "--":
            break
        if not token.startswith("-"):
            command_index = index
            break
    if command_index is not None and argv[command_index] in ("mcp", "login"):
        command = argv[command_index]
        rest = argv[command_index + 1:]
        if rest and rest[0] in ("-h", "--help"):
            return argv, []
        if not rest or rest[0].startswith("-"):
            example = "multi-claude mcp NAME list" if command == "mcp" else "multi-claude login NAME"
            raise UsageError("{} needs an account name first, e.g. `{}`".format(command, example))
        return argv[:command_index + 2], rest[1:]
    if command_index is not None and argv[command_index] in ("run", "code"):
        # run 的命令（code 的 VS Code 参数）从第一个 `--` 之后原样取出；没有 `--` 与 `--` 后为空都表示“没有”。
        if "--" not in argv[command_index + 1:]:
            return argv, None
        separator = argv.index("--", command_index + 1)
        return argv[:separator], argv[separator + 1:]
    if command_index is None or argv[command_index] != "args":
        return argv, None
    try:
        separator = argv.index("--", command_index + 1)
    except ValueError:
        # 要求显式写 `--`：否则 `args work --resume` 这种写法里的参数会被误当成账号名或选项。
        if any(token in ("-h", "--help") for token in argv[command_index + 1:]):
            return argv, []
        raise UsageError("args needs `--` before the argument list, e.g. `multi-claude args NAME -- --ide` "
                         "(use `args NAME --` to clear)")
    return argv[:separator], argv[separator + 1:]


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [hook.HOOK_COMMAND]:
        # 状态栏每次刷新都会执行钩子：跳过参数解析、平台检查与迁移记录读取，尽快把进程交给原命令。
        if len(argv) != 2:
            error("usage: multi-claude {} COMMAND".format(hook.HOOK_COMMAND))
            return accounts.EXIT_USAGE
        return hook.main(argv[1:])
    if not argv:
        # 不带参数：新用户先看到怎么上手，已有配置的用户看到账号表（方案 feature-easier-onboarding §5.1.1）。
        return cmd_overview()
    parser = build_parser()
    unknown = _unknown_command(parser, argv) or _misplaced_account_name(argv)
    if unknown is not None:
        error(unknown)
        return accounts.EXIT_USAGE
    try:
        argv, launch_args = _split_args_command(argv)
    except UsageError as exc:
        error(str(exc))
        return accounts.EXIT_USAGE
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse 在参数错误时以 2 退出、--help/--version 以 0 退出，与本工具的退出码契约一致。
        return int(exc.code or 0)
    args.launch_args = launch_args

    if not platform.is_supported():
        error("this platform is not supported yet (macOS and Linux only)")
        return accounts.EXIT_ERROR

    # completion 与 doctor 在读取迁移记录之前分派：前者不依赖任何状态，后者要把损坏的配置或
    # 迁移记录报告成检查项，而不是被下面的统一异常处理打断（方案 v0.2 §5.1.6）。
    if args.command == "completion":
        print(completion.render(args.shell, build_parser()), end="")
        return accounts.EXIT_OK
    if args.command == "doctor":
        return cmd_doctor(args.json, args.verbose)

    try:
        notice = migrate.pending_journal_notice()
        if notice:
            warn(notice)
        # list 与 usage 只读：不加写锁，迁移未完成时也放行。
        if args.command == "list":
            return cmd_list(verbose=args.verbose, as_json=args.json, names_only=args.names)
        if args.command == "usage":
            return cmd_usage(args.name, args.json)
        if args.command == "which":
            return cmd_which(args.path)
        if args.command == "statusline":
            # 只改用户指定的设置文件，不碰 config.json 与账号目录，所以不加写锁、迁移未完成时也放行。
            return cmd_statusline(args.action, args.file, args.dry_run)
        if args.command == "handoff":
            if notice:
                # 迁移进行到一半时账号目录可能正在搬动，复制进去的会话可能落在半迁移的目录里。
                error("finish the unfinished migration before running `handoff`", phase="handoff")
                return accounts.EXIT_ERROR
            # 不加写锁：只读来源账号、只写目标账号的 projects/，不碰 config.json。
            return cmd_handoff(args.target, args.source, args.session, args.force, args.dry_run)
        if args.command == "login":
            if notice:
                # 与 mcp 相同：迁移进行到一半时账号目录可能正在搬动。
                error("finish the unfinished migration before running `login`")
                return accounts.EXIT_ERROR
            # 不加写锁：凭据由 Claude 自己写入，登录过程需要浏览器交互，可能持续很久。
            return cmd_login(args.name, args.launch_args or [])
        if args.command in ("run", "path", "code"):
            if notice:
                # 与 mcp 相同：迁移进行到一半时账号目录可能正在搬动。
                error("finish the unfinished migration before running `{}`".format(args.command))
                return accounts.EXIT_ERROR
            # 只读配置、不加写锁：run 会把进程换成目标命令，可能运行很久。
            if args.command == "path":
                return cmd_path(args.name)
            if args.command == "code":
                return cmd_code(args.name, args.path, args.launch_args or [])
            return cmd_run(args.name, args.launch_args or [])
        if args.command == "mcp":
            if notice:
                # 迁移进行到一半时账号目录可能正在搬动，claude 写进去的内容可能落在半迁移的目录里。
                error("finish the unfinished migration before running `mcp`")
                return accounts.EXIT_ERROR
            # 不加写锁：claude mcp 只改 Claude 自己的文件，可能需要交互（如远程服务器登录）。
            return cmd_mcp(args.name, args.launch_args or [])
        if _blocked_by_migration(args, notice):
            return accounts.EXIT_ERROR
        with WriteLock():
            # 加锁前的检查与加锁之间，另一条命令可能刚开始一次迁移，拿到锁后再确认一次。
            if _blocked_by_migration(args, migrate.pending_journal_notice()):
                return accounts.EXIT_ERROR
            return dispatch(args)
    except migrate.JournalError as exc:
        error("{}. Check where the complete data of the unfinished migration is (the source, "
              "the target account directory, or a *.multi-claude-bak.* backup next to the source), "
              "make sure it is back at the source path, then delete the journal file".format(exc),
              phase="resume")
        return accounts.EXIT_ERROR
    except LockBusyError as exc:
        error("another multi-claude command is running (pid {})".format(exc.holder_pid or "unknown"))
        return accounts.EXIT_ERROR
    except UsageError as exc:
        error(str(exc))
        return accounts.EXIT_USAGE
    except _RenameConflict as exc:
        error("account {!r} already exists".format(exc.args[0]))
        return accounts.EXIT_CONFLICT
    except _NotRegistered as exc:
        # 与 proxy 一致：对未登记的账号名返回 1。
        error("account {!r} is not registered{}".format(
            exc.args[0], "; " + exc.args[1] if len(exc.args) > 1 else ""))
        return accounts.EXIT_ERROR
    except ConfigError as exc:
        error(str(exc), phase="config")
        return accounts.EXIT_ERROR
    except OSError as exc:
        error("{}: {}".format(type(exc).__name__, exc))
        return accounts.EXIT_ERROR


def _blocked_by_migration(args: argparse.Namespace, notice: Optional[str]) -> bool:
    # 迁移中途禁止其它写命令，防止根目录或账号在迁移过程中被改动。
    if notice and args.command != "migrate-default" and not args.dry_run:
        error("finish the unfinished migration before running `{}`".format(args.command))
        return True
    return False


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "restore":
        return restore.restore(_checked_name(args.name), args.skip_process_check, args.dry_run)
    if args.command == "migrate-default":
        name = _checked_name(args.name) if args.name is not None else _default_migration_name()
        proxy = _checked_proxy(args.proxy) if args.proxy is not None else None
        return migrate.migrate_default(name, None, args.copy, args.keep_backup, proxy,
                                       args.skip_process_check, args.dry_run)

    old, exists = load_config()
    new = old.copy()
    orphan_scope = accounts.NO_ORPHANS
    adopt_accounts = frozenset()
    # 写完后要检查“可变参数吞参”与“settings 覆盖代理”的账号；None 表示全部账号。
    warn_names: Optional[List[str]] = []
    adopted_dir: Optional[str] = None
    renamed: Optional[Tuple[str, str]] = None
    # --shared DIR 修改全局共享目录时记下 (旧值, 新值)，收敛成功后告诉用户所有共享账号都跟着改了。
    changed_shared_dir: Optional[Tuple[Optional[str], str]] = None
    # add --config-from：预检通过后要写入的 (目标路径, 文本, 权限位, 来源账号名)；None 表示不复制。
    config_copy: Optional[Tuple[str, str, int, str]] = None

    if args.command == "init":
        if args.root:
            new.root = args.root
        if args.bin_dir:
            new.bin_dir = args.bin_dir
        if args.shared_dir:
            new.shared_dir = args.shared_dir
        if args.shared_items is not None:
            items = [item.strip() for item in args.shared_items.split(",") if item.strip()]
            if any(item in (".", "..") or "/" in item for item in items):
                raise UsageError("--shared-items must be plain names without '/'")
            unshareable = [item for item in items if is_unshareable(item)]
            if unshareable:
                raise UsageError("--shared-items must not include {}: it holds account-specific state".format(
                    ", ".join(unshareable)))
            new.shared_items = items
    elif args.command in ("add", "set"):
        name = _checked_name(args.name)
        account = new.find(name)
        if account is None and args.command == "set":
            raise _NotRegistered(name, "use multi-claude add {} to create it".format(name))
        if args.command == "set" and not _has_account_option(args):
            raise UsageError("nothing to set; give at least one option, e.g. `multi-claude set {} --proxy 7901`"
                             .format(name))
        if account is None:
            account = Account(name)
            new.accounts[name] = account
            directory = accounts.account_dir(new, name)
            if os.path.isdir(directory):
                adopted_dir = directory
        if args.proxy is not None:
            account.proxy = _checked_proxy(args.proxy)
        if args.shared_to is not None:
            if args.shared_to is not True:
                changed_shared_dir = (new.shared_dir, _checked_shared_dir(args.shared_to))
                new.shared_dir = changed_shared_dir[1]
            elif not new.shared_dir:
                new.shared_dir = DEFAULT_SHARED_DIR
            account.shared = True
        elif args.no_shared:
            account.shared = False
        _apply_shared_exclude(new, account, args.shared_exclude, args.shared_include)
        if getattr(args, "adopt", False):
            # 接管只对开启了共享的账号有意义；关闭状态下工具本来就不管这些软链。
            if not account.shared:
                raise UsageError("--adopt requires sharing to be on for {!r}; add --shared".format(account.name))
            adopt_accounts = frozenset([account.name.casefold()])
        if getattr(args, "config_from", None) is not None:
            config_copy = _plan_config_copy(new, account, args.config_from, args.verbose)
            if isinstance(config_copy, int):
                return config_copy
        warn_names = [account.name]
    elif args.command == "proxy":
        account = new.find(_checked_name(args.name))
        if account is None:
            error("account {!r} is not registered".format(args.name))
            return accounts.EXIT_ERROR
        account.proxy = _checked_proxy(args.value)
        warn_names = [account.name]
    elif args.command == "env":
        target, assignments = _env_target(args)
        env = new.defaults_env if target is None else _registered(new, target).env
        for key in args.unset:
            # 删除不存在的键不算错误，重复执行结果相同（幂等）。
            env.pop(key, None)
        for key, value in assignments:
            env[key] = value
        warn_names = None if target is None else [target]
    elif args.command == "args":
        launch_args = _checked_args(args.launch_args or [])
        if args.defaults:
            if args.name is not None:
                raise UsageError("give either an account name or --defaults, not both")
            new.defaults_args = launch_args
            warn_names = None
        else:
            if args.name is None:
                raise UsageError("args needs an account name or --defaults")
            account = _registered(new, _checked_name(args.name))
            account.args = launch_args
            warn_names = [account.name]
    elif args.command == "route":
        _apply_route_args(args, new)
    elif args.command == "rename":
        old_name = _rename(new, args.old, args.new)
        orphan_scope = frozenset([old_name.casefold()])
        renamed = (old_name, args.new)
    elif args.command == "remove":
        name = _checked_name(args.name)
        account = new.find(name)
        if account is None:
            info("{} is not registered".format(name))
        else:
            del new.accounts[account.name]
        orphan_scope = frozenset([name.casefold()])
    elif args.command == "apply":
        orphan_scope = accounts.ALL_ORPHANS
        if args.file:
            new = _load_apply_file(args.file, old)
        warn_names = None

    code = accounts.converge(old, new, config_exists=exists, dry_run=args.dry_run, verbose=args.verbose,
                             orphan_scope=orphan_scope, adopt_accounts=adopt_accounts)
    if code == accounts.EXIT_OK and renamed is not None and not args.dry_run:
        info("renamed {} to {} (directory {} unchanged)".format(
            renamed[0], renamed[1], accounts.account_dir(new, renamed[1])))
    if code == accounts.EXIT_OK and config_copy is not None:
        code = _copy_settings(config_copy, args.dry_run)
    if code == accounts.EXIT_OK:
        accounts.warn_launch_settings(new, warn_names)
        if adopted_dir is not None:
            _warn_if_not_logged_in(adopted_dir)
        if args.command == "add" and not args.dry_run:
            _hint_next_step(new, args.name)
            _hint_bin_on_path(new, args.name)
        if args.command in ("add", "set") and not args.dry_run:
            if changed_shared_dir is not None and changed_shared_dir[0] != changed_shared_dir[1]:
                info("shared directory is now {} (was {}); links of every shared account point there".format(
                    changed_shared_dir[1], changed_shared_dir[0] or "not set"))
            _hint_empty_shared_dir(new, args.name)
    return code


def _default_migration_name() -> str:
    """migrate-default 省略名称时的名称来源（方案 feature-everyday-commands §5.1.3），按顺序：

    1. 未完成迁移记录里的名称：续跑必须用同一个名字；
    2. 已有 default 身份账号的名称：迁移后重复执行仍是 already migrated，不会因邮箱名不同而判冲突；
    3. ~/.claude.json 里登录账号的邮箱（只读这一个字段，不读凭据）。
    """
    journal = migrate.load_journal()
    if journal is not None:
        return journal["name"]
    config, _ = load_config()
    existing = config.default_account()
    if existing is not None:
        return existing.name
    path = os.path.join(os.path.expanduser("~"), ".claude.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise UsageError("migrate-default needs a NAME here: {} does not exist".format(path))
    except (OSError, ValueError) as exc:
        raise UsageError("migrate-default needs a NAME here: cannot read {} ({})".format(path, exc))
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    email = oauth.get("emailAddress") if isinstance(oauth, dict) else None
    if not isinstance(email, str) or not email:
        raise UsageError("migrate-default needs a NAME here: {} has no login email".format(path))
    try:
        validate_name(email)
    except ValueError as exc:
        raise UsageError("migrate-default needs a NAME here: the login email cannot be an account name ({})"
                         .format(exc))
    info("using the name {} (the email of the login in {})".format(email, path))
    return email


_SETTINGS_FILE = "settings.json"


def _plan_config_copy(config: Config, account: Account, other_name: str, verbose: bool):
    """add --config-from 的预检，必须在收敛之前完成：任何一项不满足都退出，账号不登记、目录不创建。

    返回 (目标路径, 文本, 权限位, 来源账号名)、None（目标已是相同内容，不用复制）或退出码（int）。
    """
    other = config.find(_checked_name(other_name))
    if other is None:
        error("account {!r} is not registered".format(other_name))
        return accounts.EXIT_ERROR
    if other.name == account.name:
        raise UsageError("--config-from must name another account")
    source = os.path.join(accounts.account_dir(config, other.name), _SETTINGS_FILE)
    source_kind = entry_kind(source)
    if source_kind == KIND_LINK:
        # 来源是软链说明它是共享条目：两个账号本来就读同一份，复制没有意义。
        error("{} of {} is a link (a shared item); nothing to copy".format(_SETTINGS_FILE, other.name),
              path=source)
        return accounts.EXIT_ERROR
    if source_kind != KIND_FILE:
        error("{} has no {} to copy".format(other.name, _SETTINGS_FILE), path=source)
        return accounts.EXIT_ERROR
    if (account.shared and _SETTINGS_FILE in config.shared_items
            and _SETTINGS_FILE not in account.shared_exclude):
        # 收敛会在目标处建共享软链，复制再把它换成普通文件：共享被悄悄断开，之后每次收敛都判冲突。
        error("{} shares {}; copying would replace the shared link. Exclude it first with "
              "--shared-exclude {}".format(account.name, _SETTINGS_FILE, _SETTINGS_FILE))
        return accounts.EXIT_CONFLICT
    try:
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
        mode = stat.S_IMODE(os.stat(source).st_mode)
    except (OSError, ValueError) as exc:
        error("cannot read {}: {}".format(source, exc), path=source)
        return accounts.EXIT_ERROR
    target = os.path.join(accounts.account_dir(config, account.name), _SETTINGS_FILE)
    target_kind = entry_kind(target)
    if target_kind == KIND_MISSING:
        return (target, text, mode, other.name)
    if target_kind == KIND_FILE and read_text(target) == text:
        if verbose:
            info("{} of {} already matches {} (unchanged)".format(_SETTINGS_FILE, account.name, other.name))
        return None
    error("{} already exists with different content; move it away first".format(target), path=target)
    return accounts.EXIT_CONFLICT


def _copy_settings(planned: Tuple[str, str, int, str], dry_run: bool) -> int:
    """收敛成功后写入预检读到的文本；写之前再确认目标仍不存在，避免覆盖收敛期间出现的软链或文件。"""
    target, text, mode, other_name = planned
    if dry_run:
        info("would copy {} from {}".format(_SETTINGS_FILE, other_name))
        return accounts.EXIT_OK
    if entry_kind(target) != KIND_MISSING:
        warn("{} appeared while adding the account; not copied".format(target))
        return accounts.EXIT_OK
    atomic_write(target, text, mode=mode)
    info("copied {} from {}".format(_SETTINGS_FILE, other_name))
    return accounts.EXIT_OK


def _apply_shared_exclude(config: Config, account: Account, exclude: List[str], include: List[str]) -> None:
    """add 的 --shared-exclude / --shared-include：先撤销再排除。只改配置，链接由随后的收敛计划处理。"""
    for item in exclude + include:
        if item in (".", "..") or "/" in item or not item:
            raise UsageError("shared items must be plain names without '/': {!r}".format(item))
    both = sorted(set(exclude) & set(include))
    if both:
        raise UsageError("{} given to both --shared-exclude and --shared-include".format(", ".join(both)))
    account.shared_exclude = [item for item in account.shared_exclude if item not in include]
    for item in exclude:
        if item not in account.shared_exclude:
            account.shared_exclude.append(item)
        if item not in config.shared_items:
            info("note: {} is not in shared.items; it takes effect only if added there".format(item))
    if exclude and not account.shared:
        info("note: sharing is off for {}; the exclusion applies once it is turned on".format(account.name))


def _apply_route_args(args: argparse.Namespace, new: Config) -> None:
    """route 的四种形式（互斥）：DIR NAME、DIR --remove、--default NAME、--no-default。"""
    if args.default is not None or args.no_default:
        if args.path is not None or args.name is not None:
            raise UsageError("--default and --no-default do not take DIR or NAME")
        if args.no_default:
            new.route_default = None
            return
        new.route_default = _registered(new, _checked_name(args.default)).name
        return
    if args.path is None:
        raise UsageError("route needs DIR NAME, DIR --remove, --default NAME or --no-default")
    path = routes.normalize_path(args.path, os.getcwd())
    try:
        expanded = validate_route_path(path)
    except ValueError as exc:
        raise UsageError(str(exc))
    existing = [index for index, rule in enumerate(new.route_rules) if expand(rule.path) == expanded]
    if args.remove:
        if args.name is not None:
            raise UsageError("route DIR --remove does not take NAME")
        if not existing:
            info("{} has no route".format(path))
        new.route_rules = [rule for rule in new.route_rules if expand(rule.path) != expanded]
        return
    if args.name is None:
        raise UsageError("route DIR needs NAME (or --remove)")
    account = _registered(new, _checked_name(args.name))
    if not os.path.isdir(expanded):
        warn("{} does not exist; the route is ignored until it does".format(path))
    rule = RouteRule(path, account.name)
    if existing:
        # 改指已有规则时保留它在列表中的位置：位置决定“物理路径相同的两条规则”谁生效。
        new.route_rules[existing[0]] = RouteRule(new.route_rules[existing[0]].path, account.name)
    else:
        new.route_rules.append(rule)


def _rename(new: Config, old: str, target: str) -> str:
    """改账号名：账号对象换键、dir 不变，路由引用跟着改。返回原登记的账号名。"""
    _checked_name(target)
    account = _registered(new, _checked_name(old))
    if account.name.casefold() == target.casefold():
        # 大小写不敏感的文件系统上新旧启动命令是同一个文件，删旧建新会互相覆盖。
        raise UsageError("only the letter case differs; renaming that way is not supported")
    if new.find(target) is not None:
        # 交给收敛计划报冲突（退出码 3）：这里只需保证不覆盖已有账号。
        raise _RenameConflict(target)
    old_name = account.name
    del new.accounts[old_name]
    account.name = target
    new.accounts[target] = account
    new.route_rules = [RouteRule(rule.path, target) if rule.account == old_name else rule
                       for rule in new.route_rules]
    if new.route_default == old_name:
        new.route_default = target
    return old_name


class _RenameConflict(Exception):
    """rename 的新名字已被另一个账号使用，对应退出码 3。"""


def _account_process_env(name: str) -> Tuple[Optional[dict], int]:
    """mcp 与 login 共用：检查账号可用，返回 (该账号的进程环境, 0)；不可用时返回 (None, 退出码)。"""
    config, _ = load_config()
    account = config.find(_checked_name(name))
    if account is None:
        error("account {!r} is not registered".format(name))
        return None, accounts.EXIT_ERROR
    process_env, code = _usable_account_env(config, account)
    if process_env is None:
        return None, code
    if shutil.which("claude", path=process_env.get("PATH")) is None:
        error("claude not found in PATH")
        return None, 127
    return process_env, accounts.EXIT_OK


def _usable_account_env(config: Config, account: Account) -> Tuple[Optional[dict], int]:
    """账号能否以启动命令的身份运行：目录存在，default 身份时 ~/.claude 仍指向它。返回 (进程环境, 0) 或 (None, 1)。"""
    directory = accounts.account_dir(config, account.name)
    if not os.path.isdir(directory):
        error("account directory does not exist: {}".format(directory))
        return None, accounts.EXIT_ERROR
    if account.identity == IDENTITY_DEFAULT and accounts.default_link_status(config, account) != "ok":
        # 与启动命令相同的保护：~/.claude 不再指向账号目录时，直接运行会落到别的目录。
        error("{} no longer points to {}; restore the link or run multi-claude migrate-default again".format(
            accounts.default_dir(), directory))
        return None, accounts.EXIT_ERROR
    return account_env(config, account, os.environ), accounts.EXIT_OK


def cmd_run(name: Optional[str], command: List[str]) -> int:
    """以账号启动命令的环境运行任意命令（方案 feature-everyday-commands §5.1.1）。

    不给命令时运行 claude 加账号的固定参数，与 claude-NAME 逐项一致；不给 NAME 时按 claude-auto 的路由选账号。
    成功时用 exec 把本进程换成目标命令，退出码与信号都由它决定，不会返回。
    """
    config, _ = load_config()
    if name is not None:
        account = config.find(_checked_name(name))
        if account is None:
            error("account {!r} is not registered".format(name))
            return accounts.EXIT_ERROR
    else:
        account, rule = routes.resolve(config, os.getcwd())
        if account is None:
            if rule is not None or config.route_default is not None:
                error("the route for this directory uses account {!r}, which is not registered; run "
                      "`multi-claude doctor`".format(rule.account if rule else config.route_default))
                return accounts.EXIT_ERROR
            raise UsageError("no route or default account applies here; give the account name, "
                             "e.g. `multi-claude run NAME`")
        # 提示写 stderr：stdout 留给目标命令，`run -- cmd | …` 才不会混进这一行。
        print("[multi-claude] run: using account {} ({})".format(
            account.name, "route " + rule.path if rule is not None else "default"), file=sys.stderr)
    if not command:
        # 固定参数按启动命令的规则展开 `~`（launcher.render），两种启动方式收到的参数才逐项相同。
        command = ["claude"] + [launcher.expand_value(arg) for arg in config.effective_args(account)]
    return _exec_as(config, account, command)


def _exec_as(config: Config, account: Account, command: List[str], not_found_hint: str = "") -> int:
    """run 与 code 共用：账号可用时以它的启动命令环境 exec 目标命令，成功则不返回。

    返回值只出现在失败时：账号不可用 1，PATH 中找不到命令 127。
    """
    process_env, code = _usable_account_env(config, account)
    if process_env is None:
        return code
    if shutil.which(command[0], path=process_env.get("PATH")) is None:
        error("{} not found in PATH{}".format(command[0], not_found_hint))
        return 127
    sys.stdout.flush()
    sys.stderr.flush()
    # exec 失败（权限、格式错误）抛 OSError，由 main 统一按退出码 1 报告。
    os.execvpe(command[0], command, process_env)
    return accounts.EXIT_ERROR  # 不会执行到这里：execvpe 成功后不返回


# code 依赖的未公开行为由 2026-10-02 实测确认的版本（方案 feature-vscode-launch §1.1）。
VSCODE_VERIFIED_WITH = "VS Code 1.139.1, extension 2.1.286"
# macOS 的 unix 套接字路径上限是 104 字节，VS Code 在 macOS 上把主套接字放在用户数据目录下（形如 1.13-main.sock，
# 约 15 字节，见 VS Code out/main.js 构造套接字路径的函数）；Linux 设了 XDG_RUNTIME_DIR 时不放在这里。
_SOCKET_DIR_WARN_LENGTH = 80
_CODE_NOT_FOUND_HINT = "; in VS Code run \"Shell Command: Install 'code' command in PATH\""


def cmd_code(name: str, path: Optional[str], code_args: List[str]) -> int:
    """以账号环境打开一个独立的 VS Code 实例（实验功能，方案 feature-vscode-launch §5.1）。

    必须给每个账号单独的 --user-data-dir：VS Code 已在运行时，同一用户数据目录的请求会转给已运行的实例，
    新窗口沿用那个实例的环境，账号就不生效了。目录按账号目录名存放，rename 后数据仍在原处。
    """
    warn("experimental: code relies on undocumented behaviour of VS Code and the Claude Code extension "
         "(verified with {})".format(VSCODE_VERIFIED_WITH))
    config, _ = load_config()
    account = config.find(_checked_name(name))
    if account is None:
        error("account {!r} is not registered".format(name))
        return accounts.EXIT_ERROR
    # 先确认账号可用、找得到 code 命令，再建目录：任何失败都不留下空的用户数据目录。
    process_env, code = _usable_account_env(config, account)
    if process_env is None:
        return code
    if shutil.which("code", path=process_env.get("PATH")) is None:
        error("code not found in PATH{}".format(_CODE_NOT_FOUND_HINT))
        return 127
    data_dir = _private_dirs(expand(config.root), [".apps", account.dir, "vscode"])
    if sys.platform == "darwin":
        if len(data_dir) > _SOCKET_DIR_WARN_LENGTH:
            warn("{} is long; VS Code's socket path inside it may exceed the 104-byte limit".format(data_dir))
        warn("on macOS, code passes the whole environment (including this account's variables) to "
             "`open --env`, so the values are briefly visible in the process list")
    command = ["code", "--user-data-dir", data_dir] + ([path] if path else []) + list(code_args)
    return _exec_as(config, account, command, not_found_hint=_CODE_NOT_FOUND_HINT)


def _private_dirs(root: str, parts: List[str]) -> str:
    """在 root 下逐级创建 parts，每一级都只允许本人访问（makedirs 的 mode 受 umask 影响，所以再 chmod）。"""
    path = root
    for part in parts:
        path = os.path.join(path, part)
        os.makedirs(path, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def cmd_path(name: str) -> int:
    """打印账号目录；目录不存在时照样打印，但以 1 退出，脚本里 `cd "$(multi-claude path X)"` 不会静默进错目录。"""
    config, _ = load_config()
    account = config.find(_checked_name(name))
    if account is None:
        error("account {!r} is not registered".format(name))
        return accounts.EXIT_ERROR
    directory = accounts.account_dir(config, account.name)
    print(directory)
    if not os.path.isdir(directory):
        error("account directory does not exist: {}".format(directory))
        return accounts.EXIT_ERROR
    return accounts.EXIT_OK


def cmd_mcp(name: str, claude_args: List[str]) -> int:
    """以账号的身份环境运行 `claude mcp …`；不带固定参数（见 env.py 说明）。"""
    process_env, code = _account_process_env(name)
    if process_env is None:
        return code
    return subprocess.call(["claude", "mcp"] + claude_args, env=process_env)


# 只用于判断“账号名看起来像邮箱”，以便给登录页预填；不做完整的邮箱校验。
_EMAIL_LIKE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def cmd_login(name: str, claude_args: List[str]) -> int:
    """以账号的身份环境运行 `claude auth login …`（方案 feature-easier-onboarding §5.1.4）。

    凭据由 Claude 自己写入该账号的钥匙串条目或凭据文件，本工具不读写凭据。
    不带账号的固定参数：取多个值的选项会把 `auth login` 当成自己的值吞掉（同 mcp）。
    """
    process_env, code = _account_process_env(name)
    if process_env is None:
        return code
    login_args = list(claude_args)
    if _EMAIL_LIKE.match(name) and not any(arg == "--email" or arg.startswith("--email=") for arg in login_args):
        login_args += ["--email", name]
    return subprocess.call(["claude", "auth", "login"] + login_args, env=process_env)


def cmd_overview() -> int:
    """不带参数时的输出：没有配置就给上手步骤，有配置就显示账号表。"""
    if not platform.is_supported():
        # 与其它命令一致：不支持的平台上先报平台错误，不给出走不通的上手步骤。
        error("this platform is not supported yet (macOS and Linux only)")
        return accounts.EXIT_ERROR
    if not os.path.exists(config_path()):
        print(_GETTING_STARTED, end="")
        return accounts.EXIT_OK
    code = main(["list"])
    if code == accounts.EXIT_OK:
        print("All commands: multi-claude --help")
    return code


_GETTING_STARTED = """multi-claude runs several Claude Code accounts side by side, each with its own launcher.

Get started:
  multi-claude add work      create the account "work" and the launcher claude-work
  multi-claude login work    sign in to that account
  claude-work                start Claude Code with it

Already have a login in ~/.claude? It keeps working as plain `claude`;
`multi-claude migrate-default --help` explains how to turn it into an account.
All commands: multi-claude --help
"""


def _unknown_command(parser: argparse.ArgumentParser, argv: List[str]) -> Optional[str]:
    """第一个非选项参数不是子命令时，返回带建议的报错文字；否则返回 None 交给 argparse。"""
    word = next((token for token in argv if not token.startswith("-")), None)
    if word is None:
        return None
    names = [name for action in parser._actions if isinstance(action, argparse._SubParsersAction)
             for name in action.choices]
    if word in names:
        return None
    close = difflib.get_close_matches(word, names, n=1, cutoff=0.6)
    if close:
        return "unknown command {!r}; did you mean {!r}?".format(word, close[0])
    return "unknown command {!r}; run multi-claude --help for the list".format(word)


def cmd_which(path: Optional[str]) -> int:
    """只读：显示 DIR 下运行 claude-auto 会用哪个账号；与 claude-auto 使用同一判定规则。"""
    directory = os.path.abspath(os.path.expanduser(path)) if path else os.getcwd()
    if not os.path.isdir(directory):
        error("{} is not a directory".format(directory))
        return accounts.EXIT_ERROR
    config, _ = load_config()
    account, rule = routes.resolve(config, directory)
    if account is not None and rule is not None:
        print("{} (route {})".format(account.name, rule.path))
    elif account is not None:
        print("{} (default)".format(account.name))
    elif rule is not None or config.route_default is not None:
        # 规则或默认账号引用了未登记的账号：只会出现在手工改过的配置里。
        print("{} (not registered; run `multi-claude doctor`)".format(rule.account if rule else config.route_default))
        return accounts.EXIT_ERROR
    else:
        print("claude (no route, no default)")
    return accounts.EXIT_OK


class _NotRegistered(Exception):
    """env / args / set 指定的账号未登记，对应退出码 1。args[1]（可选）是附加在报错后的提示。"""


def _registered(config: Config, name: str) -> Account:
    account = config.find(name)
    if account is None:
        raise _NotRegistered(name)
    return account


def _env_target(args: argparse.Namespace) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    """拆出 env 命令的作用对象（账号名，或 None 表示 --defaults）与 K=V 列表，并校验键和值。"""
    items = list(args.items)
    target = None
    if not args.defaults:
        if not items or "=" in items[0]:
            raise UsageError("env needs an account name or --defaults")
        target = _checked_name(items.pop(0))
    assignments = []
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise UsageError("expected K=V, got {!r}".format(item))
        try:
            validate_env_key(key)
            validate_value(value)
        except ValueError as exc:
            raise UsageError(str(exc))
        assignments.append((key, value))
    if not assignments and not args.unset:
        raise UsageError("env needs at least one K=V or --unset K")
    return target, assignments


def _checked_args(values: List[str]) -> List[str]:
    for value in values:
        try:
            validate_value(value)
        except ValueError as exc:
            raise UsageError(str(exc))
    return list(values)


def _hint_next_step(config: Config, name: str) -> None:
    """add 成功后，账号还没有登录时提示下一步；已登录或探测失败时不打扰。"""
    account = config.find(name)
    if account is None:
        return
    if identity.probe(account.identity, accounts.account_dir(config, account.name)) == identity.LOGIN_NONE:
        info("next: multi-claude login {0}   (then start claude-{0})".format(account.name))


def _has_account_option(args: argparse.Namespace) -> bool:
    return bool(args.proxy is not None or args.shared_to is not None or args.no_shared
                or args.shared_exclude or args.shared_include)


def _looks_like_path(value: str) -> bool:
    return "/" in value or value.startswith(("~", "."))


# add/set 里取一个值的选项：判断位置参数时要跳过它们的值。
_VALUE_OPTIONS = ("--proxy", "--shared-exclude", "--shared-include", "--config-from")


def _misplaced_account_name(argv: List[str]) -> Optional[str]:
    """`add --shared work` 这类把账号名写在选项后面的输入：argparse 会把 work 当成 --shared 的目录，
    再报“缺少 name”，看不出原因。只在“除这个词外没有任何位置参数”（argparse 本来就会报错）时接管报错。
    """
    # --version / --help 会让 argparse 直接输出后正常退出；--no-shared 与 --shared 同时出现时
    # argparse 会报互斥错误，那条更准确。这几种情况都不接管。
    if any(token in ("--version", "-h", "--help", "--no-shared") for token in argv):
        return None
    command_index = next((i for i, token in enumerate(argv) if not token.startswith("-")), None)
    if command_index is None or argv[command_index] not in ("add", "set"):
        return None
    rest = argv[command_index + 1:]
    candidate = None
    positionals = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in _VALUE_OPTIONS:
            index += 2
            continue
        if token == "--shared":
            following = rest[index + 1] if index + 1 < len(rest) else None
            if following is not None and not following.startswith("-") and not _looks_like_path(following):
                candidate = following
            index += 2 if following is not None and not following.startswith("-") else 1
            continue
        if not token.startswith("-"):
            positionals.append(token)
        index += 1
    if candidate is None or positionals:
        return None
    return "put the account name before the options, e.g. multi-claude {} {} --shared".format(
        argv[command_index], candidate)


def _checked_shared_dir(value: str) -> str:
    """--shared DIR 的值必须像路径，免得把 `add --shared work` 里的账号名之类的词当成目录。"""
    if not _looks_like_path(value):
        raise UsageError("--shared DIR expects a directory path, e.g. --shared ~/claude-shared; got {!r}".format(value))
    return value


def _hint_empty_shared_dir(config: Config, name: str) -> None:
    """开启共享后，共享目录里一个默认条目都没有时，说明该往哪里放什么（工具不替用户创建）。"""
    account = config.find(name)
    if account is None or not account.shared or not config.shared_dir:
        return
    root = expand(config.shared_dir)
    if any(os.path.lexists(os.path.join(root, item)) for item in config.shared_items):
        return
    info("note: {} has none of {} yet; put what every account should share there, then run "
         "multi-claude apply".format(config.shared_dir, ", ".join(config.shared_items)))


def _hint_bin_on_path(config: Config, name: str) -> None:
    """启动命令目录不在 PATH 上时，`claude-NAME` 会找不到；给出按当前 shell 可直接粘贴的命令。"""
    bin_dir = expand(config.bin_dir)
    entries = [os.path.abspath(os.path.expanduser(entry)) for entry in os.environ.get("PATH", "").split(os.pathsep)
               if entry]
    if bin_dir in entries:
        return
    account = config.find(name)
    launcher_name = "claude-" + (account.name if account is not None else name)
    info("note: {} is not on PATH, so {} will not be found; {}".format(
        bin_dir, launcher_name, shellpath.path_hint(bin_dir, os.environ.get("SHELL"), py_platform.system())))


def _warn_if_not_logged_in(directory: str) -> None:
    """add 登记已有目录时的只读登录探测（方案 §5.1.9）：只提示，不改变退出码。

    macOS 上登录绑定在路径字符串上：以前用 `~/x` 与 `/Users/me/x` 两种写法登录过，结果并不相同。
    """
    if sys.platform != "darwin":
        return
    if "CLAUDE_CODE_CUSTOM_OAUTH_URL" in os.environ:
        warn("CLAUDE_CODE_CUSTOM_OAUTH_URL is set; the login check assumes the production service "
             "and may be wrong")
    if identity.probe(IDENTITY_DIR, directory) == identity.LOGIN_NONE:
        warn("no login found for the path {}; the first launch will ask you to log in. If you logged in "
             "with a different spelling of this path before, keep using that spelling".format(directory))


def _load_apply_file(path: str, old: Config) -> Config:
    """读取 apply -f 的文件。

    managed_links、identity 与 dir 是工具内部状态，已登记账号一律沿用当前配置里的值；
    dir 与 default 之间的转换只能由 migrate-default 完成（方案 §5.1.6）。文件中未登记、
    却写了 identity=default 的账号不登记：直接建目录会得到一个没有登录的空账号。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ConfigError("cannot read {}: {}".format(path, exc))
    new = parse_config(raw, path)
    skipped = set()
    for name, account in list(new.accounts.items()):
        current = old.find(account.name)
        if current is not None:
            account.identity = current.identity
            account.managed_links = list(current.managed_links)
            # 目录名同样是内部状态：改了它，macOS 上按路径绑定的登录就会丢。
            account.dir = current.dir
            continue
        if account.identity == IDENTITY_DEFAULT:
            info("skip account {0} (identity is default; run multi-claude migrate-default {0} to create it, "
                 "then set its proxy/env/args/shared again)".format(account.name))
            del new.accounts[name]
            skipped.add(name.casefold())
            continue
        account.managed_links = []
    # 引用被跳过账号的路由一并跳过：否则整条 apply 会因“规则引用未登记账号”判冲突，
    # 带路由的配置文件就无法在新机器上使用。迁移完成后由用户重新设置。
    for rule in [rule for rule in new.route_rules if rule.account.casefold() in skipped]:
        info("skip route {} (account {} is skipped; set it again with `multi-claude route {} {}`)".format(
            rule.path, rule.account, rule.path, rule.account))
        new.route_rules.remove(rule)
    if new.route_default is not None and new.route_default.casefold() in skipped:
        info("skip default route (account {0} is skipped; set it again with "
             "`multi-claude route --default {0}`)".format(new.route_default))
        new.route_default = None
    return new


def _checked_name(name: str) -> str:
    try:
        validate_name(name)
    except ValueError as exc:
        raise UsageError(str(exc))
    return name


def _checked_proxy(value: str) -> str:
    try:
        return normalize_proxy(value)
    except ValueError as exc:
        raise UsageError(str(exc))


def cmd_list(verbose: bool = False, as_json: bool = False, names_only: bool = False) -> int:
    config, exists = load_config()
    if names_only:
        # 给补全脚本用：只输出名称，不输出任何告警。
        for name in config.accounts if exists else []:
            print(name)
        return accounts.EXIT_OK
    migrate.warn_isolation_env()
    for variable in _UNMANAGED_PROXY_VARS:
        if variable in os.environ:
            warn("{} is set; launchers do not change it, even with proxy off".format(variable))
    if as_json:
        _print_json(_list_data(config, exists))
        if exists:
            accounts.warn_launch_settings(config, None, note_invalid=True)
        return accounts.EXIT_OK
    if not exists:
        info("no configuration yet at {}; run `multi-claude add NAME` to start".format(
            os.path.join(platform.state_dir(), "config.json")))
        return accounts.EXIT_OK
    if not verbose:
        return _print_brief_list(config)
    print("root: {}".format(expand(config.root)))
    print("bin_dir: {}".format(expand(config.bin_dir)))
    print("shared.dir: {}".format(expand(config.shared_dir) if config.shared_dir else "(not set)"))
    if not config.accounts:
        print("no accounts registered")
        return accounts.EXIT_OK
    if "CLAUDE_CODE_CUSTOM_OAUTH_URL" in os.environ:
        warn("CLAUDE_CODE_CUSTOM_OAUTH_URL is set; LOGIN assumes the production service and may be wrong")
    rows = [("NAME", "IDENTITY", "DIR", "PROXY", "SHARED", "LAUNCHER", "LOGIN", "LINK")]
    details = []
    default_account = None
    for entry in _account_entries(config):
        if entry["identity"] == IDENTITY_DEFAULT:
            default_account = entry["name"]
        rows.append((entry["name"], entry["identity"], "ok" if entry["dir_exists"] else "missing-dir",
                     entry["proxy"], _shared_cell(entry), entry["launcher"], entry["login"],
                     entry["link"] or "-"))
        if verbose:
            details.append("  {}: keychain service {!r}, credentials file {}".format(
                entry["name"], entry["keychain_service"], entry["credentials_file"]))
    _print_table(rows)
    _print_routes(config)
    if default_account is not None:
        print("note: {} keeps its global state in {} (not moved by migrate-default)".format(
            default_account, os.path.join(os.path.expanduser("~"), ".claude.json")))
    for line in details:
        print(line)
    accounts.warn_launch_settings(config, None, note_invalid=True)
    return accounts.EXIT_OK


def _print_brief_list(config: Config) -> int:
    """list 的默认输出：只放新用户关心的列（方案 feature-clearer-help §5.1.2）；完整信息见 --verbose。"""
    if not config.accounts:
        print("no accounts registered; run `multi-claude add NAME` to start")
        return accounts.EXIT_OK
    if "CLAUDE_CODE_CUSTOM_OAUTH_URL" in os.environ:
        # 与完整输出相同：LOGIN 与由它得出的 STATUS 都按正式服务探测，这时可能不准。
        warn("CLAUDE_CODE_CUSTOM_OAUTH_URL is set; LOGIN assumes the production service and may be wrong")
    now = datetime.now(timezone.utc)
    rows = [("NAME", "LOGIN", "PROXY", "SHARED", "5H", "7D", "STATUS")]
    has_problem = False
    for entry in _account_entries(config):
        report = usage.read_account_usage(config, config.find(entry["name"]), now)
        status = _status_cell(entry)
        has_problem = has_problem or status != "ok"
        rows.append((entry["name"], entry["login"], entry["proxy"], _shared_cell(entry),
                     _window_cell(report.five_hour, now), _window_cell(report.seven_day, now), status))
    _print_table(rows)
    _print_routes(config)
    if has_problem:
        print("run multi-claude doctor for details")
    accounts.warn_launch_settings(config, None, note_invalid=True)
    return accounts.EXIT_OK


def _status_cell(entry: dict) -> str:
    """把账号的各项问题合成一格；顺序固定，便于用户和测试对照。"""
    problems = []
    if not entry["dir_exists"]:
        problems.append("directory missing")
    if entry["launcher"] != "ok":
        problems.append("launcher {}".format(entry["launcher"]))
    if entry["link"] not in (None, "ok"):
        problems.append("link {}".format(entry["link"]))
    if entry["login"] == identity.LOGIN_NONE:
        problems.append("not logged in")
    return ", ".join(problems) or "ok"


def _print_table(rows: List[tuple]) -> None:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _print_routes(config: Config) -> None:
    if not config.routes_enabled:
        return
    print("routes:")
    for rule in config.route_rules:
        print("  {} -> {}".format(rule.path, rule.account))
    if config.route_default is not None:
        print("  (default) -> {}".format(config.route_default))


def _shared_cell(entry: dict) -> str:
    if not entry["shared"]:
        return "no"
    if entry["shared_exclude"]:
        return "yes (not: {})".format(",".join(entry["shared_exclude"]))
    return "yes"


def _account_entries(config: Config, with_usage: bool = False) -> List[dict]:
    """list 的表格与 JSON 共用的每账号数据；表格的列取自这里，保证两种输出口径一致。"""
    now = datetime.now(timezone.utc)
    entries = []
    for name, account in config.accounts.items():
        directory = accounts.account_dir(config, name)
        is_default = account.identity == IDENTITY_DEFAULT
        entry = {
            "name": name,
            "identity": account.identity,
            "dir": directory,
            "dir_exists": os.path.isdir(directory),
            "proxy": account.proxy,
            "shared": account.shared,
            "shared_exclude": list(account.shared_exclude),
            "launcher": accounts.launcher_status(config, name),
            "login": identity.probe(account.identity, directory),
            "link": accounts.default_link_status(config, account) if is_default else None,
            "keychain_service": identity.service_name(account.identity, directory),
            "credentials_file": identity.credentials_file(directory),
        }
        if with_usage:
            entry["usage"] = usage.report_to_dict(usage.read_account_usage(config, account, now), now)
        entries.append(entry)
    return entries


def _list_data(config: Config, exists: bool) -> dict:
    if not exists:
        return {"schema_version": 1, "configured": False, "accounts": [],
                "routes": {"default": None, "rules": []}}
    return {
        "schema_version": 1,
        "configured": True,
        "root": expand(config.root),
        "bin_dir": expand(config.bin_dir),
        "shared_dir": expand(config.shared_dir) if config.shared_dir else None,
        "accounts": _account_entries(config, with_usage=True),
        "routes": {"default": config.route_default,
                   "rules": [{"path": rule.path, "account": rule.account, "exists": os.path.isdir(expand(rule.path))}
                             for rule in config.route_rules]},
    }


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


# 固定英文缩写，不用 %a：%a 随系统语言变化，表格对不齐，也不利于脚本解析。
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def cmd_usage(name: Optional[str], as_json: bool) -> int:
    """显示各账号最近一次已知的用量。数据来自 Claude Code 自己的缓存，不读凭据、不联网。"""
    config, exists = load_config()
    selected = list(config.accounts.values()) if exists else []
    if name is not None:
        account = config.find(_checked_name(name)) if exists else None
        if account is None:
            error("account {!r} is not registered".format(name))
            return accounts.EXIT_ERROR
        selected = [account]
    now = datetime.now(timezone.utc)
    reports = []
    for account in selected:
        reports.append((account.name, usage.read_account_usage(config, account, now)))
    if as_json:
        data = {"schema_version": 1, "configured": exists,
                "accounts": [{"name": account_name, "usage": usage.report_to_dict(report, now)}
                             for account_name, report in reports]}
        _print_json(data)
        return accounts.EXIT_OK
    if not exists:
        info("no configuration yet at {}; run `multi-claude add NAME` to start".format(
            os.path.join(platform.state_dir(), "config.json")))
        return accounts.EXIT_OK
    rows = [("NAME", "5H", "7D", "UPDATED")]
    for account_name, report in reports:
        rows.append((account_name, _window_cell(report.five_hour, now), _window_cell(report.seven_day, now),
                     _updated_cell(account_name, report, now)))
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    print("Values come from Claude Code's own cache (.claude.json) or the statusline hook, and may be out of date; "
          "multi-claude never reads credentials.")
    return accounts.EXIT_OK


def _window_cell(window: Optional[usage.UsageWindow], now: datetime) -> str:
    if window is None:
        return "-"
    if usage.window_is_reset(window, now):
        return "reset"
    text = "{}%".format(int(round(window.percent)))
    if window.resets_at is None:
        return text
    local = window.resets_at.astimezone()
    when = local.strftime("%H:%M") if window.resets_at - now <= _ONE_DAY else _WEEKDAYS[local.weekday()]
    return "{} {}".format(text, when)


def _updated_cell(name: str, report: usage.UsageReport, now: datetime) -> str:
    if report.status == usage.STATUS_NO_DATA:
        return "no data (start claude-{} once)".format(name)
    if report.status == usage.STATUS_UNREADABLE:
        return "unreadable"
    if report.status == usage.STATUS_OTHER_ACCOUNT:
        return "cache belongs to an earlier login"
    age = _format_age(now - report.fetched_at)
    if report.status == usage.STATUS_STALE:
        age += " (stale)"
    return age + (" (statusline)" if report.source == usage.SOURCE_STATUSLINE else "")


def _format_age(delta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return "{}m ago".format(minutes)
    if minutes < 48 * 60:
        return "{}h ago".format(minutes // 60)
    return "{}d ago".format(minutes // (24 * 60))


def cmd_handoff(target_name: str, source_name: Optional[str], session_id: Optional[str], force: bool,
                dry_run: bool) -> int:
    """把一条会话复制到目标账号并打印续聊命令。目标已有不同内容的同 ID 会话时退出 3、不改任何文件。"""
    config, exists = load_config()
    if not exists:
        raise _NotRegistered(target_name)
    target = _registered(config, _checked_name(target_name))
    environ = os.environ
    in_session = bool(environ.get("CLAUDECODE"))
    # 当前 Claude 会话所属账号；不在会话里时为 None。
    session_account = statusline.account_for(config, environ.get("CLAUDE_CONFIG_DIR")) if in_session else None
    if source_name is not None:
        source = _registered(config, _checked_name(source_name))
    elif in_session:
        # 在会话里用 ! 运行：Claude 把自己的 CLAUDE_CONFIG_DIR 传给子进程，default 身份账号则没有这个变量。
        source = session_account
        if source is None:
            raise UsageError("cannot tell which account this Claude session belongs to; use --from NAME")
    else:
        # 普通终端里没有 CLAUDE_CONFIG_DIR 不能说明用的是 default 账号，不猜。
        raise UsageError("--from is required outside a Claude session")
    if source.name == target.name:
        raise UsageError("the session is already in account {!r}".format(target.name))
    if session_id is not None and not sessions.SESSION_ID_PATTERN.match(session_id):
        raise UsageError("invalid session id {!r}".format(session_id))
    # 只有来源就是当前会话所属账号时，CLAUDE_CODE_SESSION_ID 才指向来源里的会话。
    current = environ.get("CLAUDE_CODE_SESSION_ID") if session_account is not None and session_account is source \
        else None
    target_dir = accounts.account_dir(config, target.name)
    if not os.path.isdir(target_dir):
        error("account directory of {} does not exist".format(target.name), phase="handoff", path=target_dir)
        return accounts.EXIT_ERROR
    try:
        handoff = sessions.plan(accounts.account_dir(config, source.name), target_dir,
                                sessions.cwd_variants(dict(environ)), session_id, current)
    except sessions.HandoffError as exc:
        error(str(exc), phase="handoff")
        return accounts.EXIT_ERROR
    target_file = os.path.join(handoff.target_dir, handoff.session_id + ".jsonl")
    if handoff.state == sessions.CONFLICT and not force:
        error("{} already has a different copy of session {}; use --force to back it up and replace it".format(
            target.name, handoff.session_id), phase="handoff", path=target_file)
        return accounts.EXIT_CONFLICT
    # cd 到找到会话时用的写法（物理路径或软链写法的 $PWD），目标账号才会按同一目录名找到会话。
    resume = "cd {} && claude-{} --resume {}".format(shlex.quote(handoff.cwd), target.name, handoff.session_id)
    if handoff.state == sessions.UNCHANGED:
        info("handoff: session {} is already in {} (unchanged)".format(handoff.session_id, target.name))
    elif dry_run:
        info("handoff: would copy session {} from {} to {} ({}){}".format(
            handoff.session_id, source.name, target.name, handoff.target_dir,
            ", backing up the existing copy" if handoff.state == sessions.CONFLICT else ""))
        return accounts.EXIT_OK
    else:
        backups = sessions.execute(handoff, force)
        info("handoff: copied session {} from {} to {} ({})".format(
            handoff.session_id, source.name, target.name, handoff.target_dir))
        for backup in backups:
            info("backup: {}".format(backup))
    info("continue with: {}".format(resume))
    return accounts.EXIT_OK


def cmd_statusline(action: str, path: str, dry_run: bool) -> int:
    """包装或还原设置文件中的 statusLine 命令。文件不合法时退出 1 且不改动；其它写入错误由 main 统一处理。"""
    try:
        if action == "install":
            # 包装命令里写的是绝对路径：Claude 执行 statusLine 时的 PATH 不一定含 multi-claude 所在目录。
            executable = shutil.which(statusline.EXECUTABLE)
            if executable is None:
                error("{} is not on PATH; the wrapped statusLine needs its full path".format(statusline.EXECUTABLE),
                      phase="statusline")
                return accounts.EXIT_ERROR
            result = statusline.install(path, executable, dry_run=dry_run)
        else:
            result = statusline.uninstall(path, dry_run=dry_run)
    except statusline.StatuslineError as exc:
        error(str(exc), phase="statusline")
        return accounts.EXIT_ERROR
    changed = result.state in (statusline.WRAPPED, statusline.REWRAPPED, statusline.RESTORED)
    if dry_run and changed:
        info("statusline: would be {} {}".format(result.state, result.target))
    else:
        info("statusline: {} {}".format(result.state, result.target))
    if result.backup:
        info("backup: {}".format(result.backup))
    if changed:
        info("command: {}".format(result.command))
    if not dry_run and result.state in (statusline.WRAPPED, statusline.REWRAPPED):
        # 实测（2026-10-01）：包装前就在运行的会话一直不产生快照，新会话收到第一次回复后才有。
        info("note: Claude sessions that are already running keep the old status line. Usage is recorded from "
             "sessions started after `install`, once they have had their first reply")
    return accounts.EXIT_OK


def cmd_doctor(as_json: bool, verbose: bool) -> int:
    try:
        checks = doctor.run_checks()
    except OSError as exc:
        # 意外的读写错误也记成一条检查结果，保证 --json 在任何情况下都输出合法 JSON。
        checks = [doctor.Check("doctor", doctor.LEVEL_ERROR, "-", "{}: {}".format(type(exc).__name__, exc))]
    if as_json:
        _print_json(doctor.to_dict(checks))
    else:
        for line in doctor.format_text(checks, verbose):
            print(line)
    return doctor.exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
