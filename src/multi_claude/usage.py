"""账号用量：只读 Claude Code 自己缓存在 `.claude.json` 里的 `cachedUsageUtilization`（方案 v0.2 §5.1.1）。

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

from .config import IDENTITY_DEFAULT

# 超过这个时长的缓存视为过期：仍然展示数值，但标注 stale。
STALE_AFTER = timedelta(hours=1)

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_NO_DATA = "no-data"
# 缓存属于以前登录的另一个账号（同一配置目录换过登录），数值与当前账号无关，不展示。
STATUS_OTHER_ACCOUNT = "other-account"
STATUS_UNREADABLE = "unreadable"


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
    }
