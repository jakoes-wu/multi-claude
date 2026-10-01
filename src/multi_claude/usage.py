"""账号用量：读 Claude Code 缓存在 `.claude.json` 里的 `cachedUsageUtilization`（方案 v0.2 §5.1.1），
以及 statusLine 钩子存下的快照（方案 feature-statusline-usage §5.1.5），取两者中较新的一份。

本模块不读凭据、不联网：用量数据是 Claude Code 运行时顺带写下的缓存，可能已经很旧
（实测正在使用的账号也可能几十个小时没刷新），所以每份结果都带获取时间，由调用方标注年龄。
该字段未写入官方文档，任何字段缺失或类型不符都降级为“无数据”，绝不抛异常。
`.claude.json` 里还有邮箱、组织名等信息，这里只取用量与 `oauthAccount.accountUuid` 两处。
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from . import accounts, platform
from .config import IDENTITY_DEFAULT, Account, Config

# 超过这个时长的缓存视为过期：仍然展示数值，但标注 stale。
STALE_AFTER = timedelta(hours=1)

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_NO_DATA = "no-data"
# 缓存属于以前登录的另一个账号（同一配置目录换过登录），数值与当前账号无关，不展示。
STATUS_OTHER_ACCOUNT = "other-account"
STATUS_UNREADABLE = "unreadable"

# 数据来源：Claude Code 自己的缓存，或 statusLine 钩子的快照。
SOURCE_CACHE = "cache"
SOURCE_STATUSLINE = "statusline"
SNAPSHOT_SCHEMA_VERSION = 1


class UsageWindow(NamedTuple):
    # 0-100；缓存里实测为整数，也接受小数
    percent: float
    # 该窗口的重置时间（带时区）；缺失或解析失败时为 None
    resets_at: Optional[datetime]


class UsageReport(NamedTuple):
    status: str
    fetched_at: Optional[datetime]
    five_hour: Optional[UsageWindow]
    seven_day: Optional[UsageWindow]
    source: str = SOURCE_CACHE


EMPTY = UsageReport(STATUS_NO_DATA, None, None, None)


def global_state_path(identity: str, account_dir: str) -> str:
    """Claude 存放该账号全局状态（含用量缓存）的文件。

    与 accounts.settings_proxy_overrides 的取法一致：default 身份不设 CLAUDE_CONFIG_DIR，
    Claude 用 ~/.claude.json；dir 身份用 <账号目录>/.claude.json。
    """
    if identity == IDENTITY_DEFAULT:
        return os.path.join(os.path.expanduser("~"), ".claude.json")
    return os.path.join(account_dir, ".claude.json")


def read_usage(path: str, now: datetime) -> UsageReport:
    """读取并解析用量缓存。now 必须带时区（用于判断过期与窗口是否已重置）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return EMPTY
    except (OSError, ValueError):
        # Claude 正在写这个文件时可能读到半截内容，按“读不出”处理，不影响其它账号。
        return UsageReport(STATUS_UNREADABLE, None, None, None)
    if not isinstance(data, dict):
        return UsageReport(STATUS_UNREADABLE, None, None, None)
    cache = data.get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        return EMPTY
    fetched_ms = cache.get("fetchedAtMs")
    if not isinstance(fetched_ms, int) or isinstance(fetched_ms, bool):
        return EMPTY
    try:
        fetched_at = datetime.fromtimestamp(fetched_ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        # 超出平台时间范围的整数（文件被写坏）也只降级为无数据，不能让 usage / list --json 崩溃。
        return EMPTY

    cached_account = cache.get("accountUuid")
    oauth = data.get("oauthAccount")
    current_account = oauth.get("accountUuid") if isinstance(oauth, dict) else None
    if isinstance(cached_account, str) and isinstance(current_account, str) and cached_account != current_account:
        return UsageReport(STATUS_OTHER_ACCOUNT, fetched_at, None, None)

    utilization = cache.get("utilization")
    if not isinstance(utilization, dict):
        return EMPTY
    five_hour = _window(utilization.get("five_hour"))
    seven_day = _window(utilization.get("seven_day"))
    if five_hour is None and seven_day is None:
        return EMPTY
    status = STATUS_STALE if now - fetched_at > STALE_AFTER else STATUS_OK
    return UsageReport(status, fetched_at, five_hour, seven_day)


def _window(value: object) -> Optional[UsageWindow]:
    if not isinstance(value, dict):
        return None
    percent = value.get("utilization")
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
        return None
    return UsageWindow(percent, _parse_time(value.get("resets_at")))


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # 没有时区的时间无法与 now 比较，视为解析失败。
    return parsed if parsed.tzinfo is not None else None


def window_is_reset(window: UsageWindow, now: datetime) -> bool:
    """窗口已过重置时间：缓存里的百分比不再有意义（与官方 statusline 丢弃过期窗口的语义一致）。"""
    return window.resets_at is not None and window.resets_at <= now


def report_to_dict(report: UsageReport, now: datetime) -> dict:
    """JSON 输出用的结构；时间用带时区的 ISO 8601。"""
    def window_dict(window: Optional[UsageWindow]) -> Optional[dict]:
        if window is None:
            return None
        return {"percent": window.percent,
                "resets_at": window.resets_at.isoformat() if window.resets_at else None,
                "reset": window_is_reset(window, now)}
    return {
        "status": report.status,
        "fetched_at": report.fetched_at.isoformat() if report.fetched_at else None,
        "age_seconds": int((now - report.fetched_at).total_seconds()) if report.fetched_at else None,
        "five_hour": window_dict(report.five_hour),
        "seven_day": window_dict(report.seven_day),
        "source": report.source,
    }


def snapshot_path(account: Account) -> str:
    """statusLine 快照的位置。按账号目录名而不是账号名存放：改名后目录不变，快照仍对得上。"""
    return os.path.join(platform.state_dir(), "usage", account.dir + ".json")


def current_account_uuid(state_path: str) -> Optional[str]:
    """该账号全局状态文件里当前登录的 accountUuid；读不到时为 None。用来识别“同一目录换了登录”。"""
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    value = oauth.get("accountUuid") if isinstance(oauth, dict) else None
    return value if isinstance(value, str) else None


def statusline_windows(rate_limits: object) -> Optional[dict]:
    """从 statusLine stdin 的 `rate_limits`（或快照里存的同名字段）中取出合法的窗口。

    只保留 `used_percentage`（0-100）与 `resets_at`（Unix 秒）：stdin 里的其它内容不落盘。
    两个窗口都不合法时返回 None，调用方据此不写快照、不采用快照。
    """
    if not isinstance(rate_limits, dict):
        return None
    windows = {}
    for key in ("five_hour", "seven_day"):
        value = rate_limits.get(key)
        if not isinstance(value, dict) or not _is_number(value.get("used_percentage")):
            continue
        resets_at = value.get("resets_at")
        windows[key] = {"used_percentage": value["used_percentage"],
                        "resets_at": resets_at if _is_number(resets_at) else None}
    return windows or None


def read_snapshot(path: str, account_dir: str, current_uuid: Optional[str], now: datetime) -> Optional[UsageReport]:
    """读取 statusLine 快照；不能采用时返回 None（调用方退回缓存）。

    以下情况都不能采用：文件缺失或损坏；快照记录的配置目录与账号当前目录不同（目录被换掉）；
    快照与当前登录的 accountUuid 不同（同一目录换了登录，数值属于另一个账号）。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    captured_at = _parse_time(data.get("captured_at"))
    if captured_at is None or data.get("config_dir") != os.path.realpath(account_dir):
        return None
    snapshot_uuid = data.get("account_uuid")
    if isinstance(snapshot_uuid, str) and isinstance(current_uuid, str) and snapshot_uuid != current_uuid:
        return None
    windows = statusline_windows(data.get("rate_limits"))
    if windows is None:
        return None
    status = STATUS_STALE if now - captured_at > STALE_AFTER else STATUS_OK
    return UsageReport(status, captured_at, _snapshot_window(windows.get("five_hour")),
                       _snapshot_window(windows.get("seven_day")), SOURCE_STATUSLINE)


def read_account_usage(config: Config, account: Account, now: datetime) -> UsageReport:
    """usage 与 list --json 用的入口：缓存与快照都可用时取时间较新的一份。

    快照不可用时原样返回缓存的结果，保持 no-data / unreadable / other-account 的原有语义；
    缓存没有可用数值时，快照即使已过期也比“无数据”有用，直接采用。
    """
    directory = accounts.account_dir(config, account.name)
    state_path = global_state_path(account.identity, directory)
    cache = read_usage(state_path, now)
    snapshot = read_snapshot(snapshot_path(account), directory, current_account_uuid(state_path), now)
    if snapshot is None:
        return cache
    if cache.status not in (STATUS_OK, STATUS_STALE) or cache.fetched_at is None:
        return snapshot
    return snapshot if snapshot.fetched_at > cache.fetched_at else cache


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _snapshot_window(value: Optional[dict]) -> Optional[UsageWindow]:
    if value is None:
        return None
    resets_at = None
    if value["resets_at"] is not None:
        try:
            resets_at = datetime.fromtimestamp(value["resets_at"], tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            # 越界的秒数只丢掉重置时间，百分比仍可展示。
            resets_at = None
    return UsageWindow(value["used_percentage"], resets_at)
