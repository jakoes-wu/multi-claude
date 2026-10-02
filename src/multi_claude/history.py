"""本地 token 用量历史与最近使用时间（方案 feature-usage-history §5.1.1、§5.1.3）。

数据来源是 Claude Code 自己写在账号目录里的会话记录 `projects/**/*.jsonl`：每条 assistant 行带
`message.id`、`message.model` 与 `message.usage`。本模块只读这几个计数字段，不输出、不保存
任何对话内容，不联网，不读凭据。记录格式未写入官方文档，任何一行解析失败都跳过，不报错。

同一条回复会被拆成多行写入（每个内容块一行，`usage` 随之重复或逐步增大），所以按 `message.id`
合并，每项计数取最大值；直接累加会把同一条回复重复计算好几遍。
"""

import json
import os
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

SYNTHETIC_MODEL = "<synthetic>"
# 缺少模型名的回复仍计入，按这个名字归组，计数不丢
UNKNOWN_MODEL = "unknown"
# mtime 过滤的余量：只有 mtime 被人为回拨时才会漏数据，多读一天的文件换更稳妥的结果
_MTIME_MARGIN = timedelta(days=1)
_COUNTERS = (("input", "input_tokens"), ("output", "output_tokens"),
             ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens"))


class Reply(NamedTuple):
    timestamp: datetime  # UTC
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int


class Row(NamedTuple):
    key: str  # 本地日期（YYYY-MM-DD）或模型名
    input: int
    output: int
    cache_read: int
    cache_write: int
    replies: int


def _session_files(account_dir: str) -> Iterable[str]:
    root = os.path.join(account_dir, "projects")
    # os.walk 默认忽略读不了的目录（onerror=None）；projects/ 不存在时什么也不产出，视为没有数据
    for directory, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".jsonl"):
                yield os.path.join(directory, name)


def _parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        # Python 3.8 的 fromisoformat 不认末尾的 Z，先换成 +00:00
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _count(usage: dict, key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def scan(account_dir: str, since: datetime) -> Tuple[Dict[str, Reply], int]:
    """返回 (since 之后的回复按 message.id 合并的结果, 读不了而跳过的文件数)；since 必须带时区。

    先按 ID 合并（时间取最早、计数取最大），再按最早时间判断是否在窗口内：一条回复的多行可能跨过起点，
    按行过滤会把它算进窗口。mtime 早于 since 减一天的文件整个跳过：文件 mtime 不早于其中任何一行的时间。
    文件在扫描中被删除或没有读权限时跳过并计数，命令照常给出其余结果。
    """
    cutoff = (since - _MTIME_MARGIN).timestamp()
    merged: Dict[str, Reply] = {}
    skipped = 0
    for path in _session_files(account_dir):
        try:
            if os.stat(path).st_mtime < cutoff:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"usage"' not in line:
                        continue
                    reply_id, reply = _parse_line(line)
                    if reply_id is None:
                        continue
                    previous = merged.get(reply_id)
                    merged[reply_id] = reply if previous is None else merge(previous, reply)
        except OSError:
            skipped += 1
    return {reply_id: reply for reply_id, reply in merged.items() if reply.timestamp >= since}, skipped


def _parse_line(line: str):
    try:
        data = json.loads(line)
    except ValueError:
        return None, None
    if not isinstance(data, dict) or data.get("type") != "assistant":
        return None, None
    message = data.get("message")
    if not isinstance(message, dict):
        return None, None
    reply_id = message.get("id")
    usage = message.get("usage")
    if not isinstance(reply_id, str) or not reply_id or not isinstance(usage, dict):
        return None, None
    model = message.get("model")
    if model == SYNTHETIC_MODEL:
        # Claude Code 自己插入的占位回复，计数为 0，不算一次回复
        return None, None
    if not isinstance(model, str) or not model:
        model = UNKNOWN_MODEL
    timestamp = _parse_time(data.get("timestamp"))
    if timestamp is None:
        return None, None
    # usage.iterations 等其它字段与顶层计数的关系未核实，不使用
    return reply_id, Reply(timestamp, model, *(_count(usage, key) for _name, key in _COUNTERS))


def merge(a: Reply, b: Reply) -> Reply:
    return Reply(min(a.timestamp, b.timestamp), a.model, max(a.input, b.input), max(a.output, b.output),
                 max(a.cache_read, b.cache_read), max(a.cache_write, b.cache_write))


def summarize(replies: Iterable[Reply], by: str, tz: Optional[tzinfo] = None) -> List[Row]:
    """按本地日期（by="day"）或模型（by="model"）汇总；日期升序，模型按输出 token 降序。"""
    totals: Dict[str, List[int]] = {}
    for reply in replies:
        key = _local_date(reply.timestamp, tz).isoformat() if by == "day" else reply.model
        bucket = totals.setdefault(key, [0, 0, 0, 0, 0])
        bucket[0] += reply.input
        bucket[1] += reply.output
        bucket[2] += reply.cache_read
        bucket[3] += reply.cache_write
        bucket[4] += 1
    rows = [Row(key, *values) for key, values in totals.items()]
    if by == "day":
        return sorted(rows, key=lambda row: row.key)
    return sorted(rows, key=lambda row: (-row.output, row.key))


def _local_date(moment: datetime, tz: Optional[tzinfo]) -> date:
    # 逐条换算：tz 为 None 时 astimezone() 用该时刻本机的偏移，跨夏令时也分对天
    return moment.astimezone(tz).date()


def last_used(account_dir: str) -> Optional[datetime]:
    """账号最近一次使用的时间：history.jsonl 与第一层会话文件的最大 mtime（UTC）；都没有时为 None。

    只 stat 不读内容；不进 subagents/（子代理文件只会和主会话一起更新）。目录 mtime 只在增删条目时变化，不可靠。
    """
    candidates = [os.path.join(account_dir, "history.jsonl")]
    projects = os.path.join(account_dir, "projects")
    try:
        project_dirs = os.listdir(projects)
    except OSError:
        project_dirs = []
    for project in project_dirs:
        try:
            names = os.listdir(os.path.join(projects, project))
        except OSError:
            continue
        candidates.extend(os.path.join(projects, project, name) for name in names if name.endswith(".jsonl"))
    latest = None
    for path in candidates:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        latest = mtime if latest is None else max(latest, mtime)
    return None if latest is None else datetime.fromtimestamp(latest, timezone.utc)
