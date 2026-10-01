"""statusLine 包装与用量采集（方案 feature-statusline-usage）。

`statusline install FILE` 把设置文件里已有的 statusLine 命令包成
`<multi-claude 路径> statusline-hook <原命令>`。Claude Code 刷新状态栏时用 `/bin/sh -c` 执行它，
并把一段 JSON 写进 stdin；钩子从中取出 `rate_limits` 存成该账号的用量快照，
然后用 exec 把进程交给 `/bin/sh -c <原命令>`，状态栏的输出、退出码与不包装时完全相同。

钩子靠环境里的 `CLAUDE_CONFIG_DIR` 认账号（default 身份不设它），不读凭据、不联网。
钩子的 stdout 就是状态栏内容，所以采集过程中不得向 stdout 输出任何东西，采集失败也只能静默。
"""

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Mapping, NamedTuple, Optional, Tuple

from . import accounts, usage
from .config import Account, Config, load_config
from .fsutil import atomic_write

HOOK_COMMAND = "statusline-hook"
EXECUTABLE = "multi-claude"
SHELL = "/bin/sh"
# 同一账号的状态栏约 300 ms 刷新一次；数值没变时一分钟内只落盘一次。
THROTTLE = timedelta(seconds=60)

# install / uninstall 的结果
WRAPPED = "wrapped"
REWRAPPED = "rewrapped"
UNCHANGED = "unchanged"
RESTORED = "restored"
NOT_INSTALLED = "not installed"


class StatuslineError(Exception):
    """设置文件或其中的 statusLine 不合法，对应退出码 1；文件未被修改。"""


class Result(NamedTuple):
    state: str
    # 实际改写的文件（FILE 是软链时为它指向的文件）
    target: str
    # 改写前的备份；没有写入（无变化或 --dry-run）时为 None
    backup: Optional[str]
    # 改写后的 statusLine 命令
    command: str


def wrap(executable: str, original: str) -> str:
    """包装后的命令。两段都用 shlex.quote：/bin/sh 对单引号串不做任何展开，钩子收到的参数与原命令逐字节相同。"""
    return "{} {} {}".format(shlex.quote(executable), HOOK_COMMAND, shlex.quote(original))


def unwrap(command: str) -> Optional[Tuple[str, str]]:
    """若 command 是本工具包装过的命令，返回 (multi-claude 路径, 原命令)；否则返回 None。"""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) == 3 and parts[1] == HOOK_COMMAND and os.path.basename(parts[0]) == EXECUTABLE:
        return parts[0], parts[2]
    return None


def install(path: str, executable: str, dry_run: bool = False) -> Result:
    """包装 path 中的 statusLine 命令；已包装且路径相同时不改动，路径变了则改用新路径。"""
    target, data, command = _load(path)
    wrapped = unwrap(command)
    if wrapped is not None and wrapped[0] == executable:
        return Result(UNCHANGED, target, None, command)
    original = wrapped[1] if wrapped is not None else command
    new_command = wrap(executable, original)
    state = REWRAPPED if wrapped is not None else WRAPPED
    return Result(state, target, _save(target, data, new_command, dry_run), new_command)


def uninstall(path: str, dry_run: bool = False) -> Result:
    """把 path 中包装过的 statusLine 命令还原成原命令；没有包装时不改动。"""
    target, data, command = _load(path)
    wrapped = unwrap(command)
    if wrapped is None:
        return Result(NOT_INSTALLED, target, None, command)
    return Result(RESTORED, target, _save(target, data, wrapped[1], dry_run), wrapped[1])


def _load(path: str) -> Tuple[str, dict, str]:
    # 改软链指向的文件而不是替换软链本身：~/.claude-shared 下的设置文件常被各账号软链引用。
    target = os.path.realpath(path)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise StatuslineError("{} does not exist".format(target))
    except (OSError, UnicodeDecodeError) as exc:
        raise StatuslineError("cannot read {}: {}".format(target, exc))
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StatuslineError("{} is not valid JSON: {}".format(target, exc))
    if not isinstance(data, dict):
        raise StatuslineError("{} is not a JSON object".format(target))
    status_line = data.get("statusLine")
    if (not isinstance(status_line, dict) or status_line.get("type") != "command"
            or not isinstance(status_line.get("command"), str) or not status_line["command"].strip()):
        raise StatuslineError("no command statusLine in {}".format(target))
    return target, data, status_line["command"]


def _save(target: str, data: dict, command: str, dry_run: bool) -> Optional[str]:
    """先备份再原子写入；只改 statusLine.command，其它键（padding、refreshInterval 等）原样保留。"""
    if dry_run:
        return None
    data["statusLine"]["command"] = command
    mode = stat.S_IMODE(os.stat(target).st_mode)
    backup = _backup_path(target)
    shutil.copy2(target, backup)
    atomic_write(target, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)
    return backup


def _backup_path(target: str) -> str:
    base = "{}.multi-claude-bak.{}".format(target, time.strftime("%Y%m%d_%H%M%S"))
    candidate, index = base, 1
    # 同一秒内连续执行两次时不覆盖前一份备份。
    while os.path.lexists(candidate):
        candidate = "{}.{}".format(base, index)
        index += 1
    return candidate


def run_hook(original: str) -> int:
    """statusLine 钩子：采集用量后把进程交给 `/bin/sh -c <原命令>`。

    采集失败不影响显示。成功时本函数不返回（exec）；只有准备 stdin 或 exec 本身失败时返回 127，
    这时状态栏为空，原因写到 stderr（Claude 只把它记进调试日志）。
    """
    data = sys.stdin.buffer.read()
    try:
        capture(data, os.environ, datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 — 采集是旁路，任何异常都不能影响状态栏
        pass
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


def capture(data: bytes, environ: Mapping[str, str], now: datetime) -> None:
    """把 stdin JSON 里的 rate_limits 存成所属账号的快照；没有数据、认不出账号时什么也不做。"""
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        return
    windows = usage.statusline_windows(payload.get("rate_limits"))
    if windows is None:
        # 非 Pro/Max 订阅、或会话还没收到第一次 API 响应时没有 rate_limits。
        return
    config, exists = load_config()
    if not exists:
        return
    account = account_for(config, environ.get("CLAUDE_CONFIG_DIR"))
    if account is None:
        return
    path = usage.snapshot_path(account)
    if _recently_captured(path, windows, now):
        return
    directory = accounts.account_dir(config, account.name)
    document = {
        "schema_version": usage.SNAPSHOT_SCHEMA_VERSION,
        "captured_at": now.isoformat(),
        # 读取时与账号当前目录比较，目录被换掉后旧快照不再采用。
        "config_dir": os.path.realpath(directory),
        # 读取时与当前登录比较，同一目录换了登录后旧快照不再采用。
        "account_uuid": usage.current_account_uuid(usage.global_state_path(account.identity, directory)),
        "rate_limits": windows,
    }
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # 同一账号的多个会话会同时写：原子替换保证读到的总是某一次完整的快照。
    atomic_write(path, json.dumps(document, indent=2) + "\n", mode=0o600)


def account_for(config: Config, config_dir: Optional[str]) -> Optional[Account]:
    """按 Claude 进程的 CLAUDE_CONFIG_DIR 找账号；未设置时就是 default 身份账号（它靠不设该变量使用 ~/.claude）。

    比较真实路径：经软链或带 `~` 的写法指向同一目录也能认出。
    """
    if not config_dir:
        return config.default_account()
    real = os.path.realpath(os.path.expanduser(config_dir))
    for account in config.accounts.values():
        if os.path.realpath(accounts.account_dir(config, account.name)) == real:
            return account
    return None


def _recently_captured(path: str, windows: dict, now: datetime) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(previous, dict) or previous.get("rate_limits") != windows:
        return False
    try:
        captured_at = datetime.fromisoformat(previous.get("captured_at"))
    except (TypeError, ValueError):
        return False
    if captured_at.tzinfo is None:
        return False
    # 快照时间在未来（时钟回拨）时不算“刚写过”，重新写一次把时间纠正回来。
    return timedelta(0) <= now - captured_at < THROTTLE
