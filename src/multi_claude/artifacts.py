"""已发布 artifact 的跨账号查询与 handoff 交接记录（方案 feature-artifact-list）。

数据来源是 Claude Code 写在账号目录里的会话记录：Artifact 工具每次调用写一行带 `tool_use` 块的
assistant 记录，结果写一行带 `tool_result` 块的 user 记录，发布成功时后者顶层的 `toolUseResult`
是带 `url`、`title`、`path` 等键的对象。记录格式未写入官方文档，任何一行解析失败都跳过，不报错。
本模块只读会话记录，不联网，不读凭据。

会话记录里没有账号字段，而 handoff 会把整份会话复制进另一个账号，交接前的发布记录在两个账号里
逐字节相同。所以 handoff 每次复制后在工具状态目录追加一条交接记录（复制了哪些文件、各多少字节、
这段字节的 SHA-256），查询时据此把副本里交接前的记录归回来源账号（§5.1.3）。
"""

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

from . import history, platform, sessions

HANDOFF_LOG = "handoffs.jsonl"
HANDOFF_LOG_VERSION = 1
# fullmatch 用：match 加 $ 会放过结尾带换行的 URL
URL_PATTERN = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9_-]+")
# 只解析含这两种字节的行：tool_use 行带 "Artifact"，发布结果行带 URL。其余行（绝大多数）不做 JSON 解析
_CANDIDATE_MARKS = (b'"Artifact"', b"claude.ai/")
_HASH_CHUNK = 1024 * 1024

OWNERSHIP_SINGLE = "single"
OWNERSHIP_RECORDED = "recorded"
OWNERSHIP_UNCERTAIN = "uncertain"


class Publish(NamedTuple):
    """会话记录里的一次发布成功。来自会话内容的文字字段输出前必须过 sessions.clean_title。"""
    url: str
    title: str
    # 发布时的本地源文件；多在 Claude 的临时 scratchpad 里，事后常已不存在
    path: Optional[str]
    audience: Optional[str]
    description: str
    timestamp: Optional[datetime]  # UTC
    cwd: Optional[str]
    project: str
    # 相对项目目录：主会话 <SID>.jsonl，子代理 <SID>/subagents/<名>.jsonl
    relpath: str
    # 续聊用的会话 ID：子代理里发布的也是父会话
    session_id: str
    # 结果行在文件里的起始字节偏移；handoff 逐字节复制，同一行在来源与副本里偏移相同，追链靠它
    offset: int
    # 同一条记录在多个账号的副本共用这个 ID，用来合并副本
    record_id: str


class Handoff(NamedTuple):
    # 在 handoffs.jsonl 里的行号（从 0 起）。交接先后只认它，不比 time：同一秒内两次交接、时钟回拨都会排错
    seq: int
    # 写记录时的 UTC 时间；只用来求开始记录时刻 T0，不用来排先后
    time: Optional[datetime]
    session_id: str
    project: str
    # 来源 / 目标账号目录的真实路径
    source_dir: str
    target_dir: str
    # relpath -> (复制进目标的字节数, 这段字节的 SHA-256)
    files: Dict[str, Tuple[int, str]]


class Copy(NamedTuple):
    """某个账号目录里的一条发布记录。"""
    account: str
    publish: Publish


class Artifact(NamedTuple):
    url: str
    title: str
    description: str
    # 所有者；ownership 为 uncertain 时是所有有副本的账号
    accounts: List[str]
    ownership: str
    first_published: Optional[datetime]
    last_published: Optional[datetime]
    # 去掉副本后的发布次数
    publishes: int
    # 代表副本：目录、会话、续聊命令都取它（所有者账号里最晚的一条；所有者不确定时取全局最晚）
    representative: Copy


def valid_session_id(session_id: str) -> bool:
    # 以 - 开头的 ID 拼进续聊命令会被 claude 当成选项（与 sessions.list_sessions 同一道校验）
    return bool(sessions.SESSION_ID_PATTERN.fullmatch(session_id)) and not session_id.startswith("-")


def session_files(account_dir: str) -> Iterator[Tuple[str, str, str]]:
    """产出账号目录下要扫的会话文件 (项目目录名, relpath, 完整路径)。

    只认 <SID>.jsonl 与 <SID>/subagents/<名>.jsonl：handoff --force 的备份（<SID>.jsonl.multi-claude-bak.*、
    <SID>.multi-claude-bak.*/）与中途被杀留下的 .multi-claude-<SID>.tmp/ 都过不了 SID 校验，不是能续聊的会话。
    读不了的目录直接跳过。
    """
    projects = os.path.join(account_dir, "projects")
    try:
        project_names = sorted(os.listdir(projects))
    except OSError:
        return
    for project in project_names:
        directory = os.path.join(projects, project)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            if name.endswith(".jsonl"):
                if valid_session_id(name[:-len(".jsonl")]) and os.path.isfile(path):
                    yield project, name, path
                continue
            if not valid_session_id(name):
                continue
            for relpath, sub_path in _subagent_files(directory, name):
                yield project, relpath, sub_path


def _subagent_files(project_dir: str, session_id: str) -> Iterator[Tuple[str, str]]:
    subagents = os.path.join(project_dir, session_id, "subagents")
    try:
        names = sorted(os.listdir(subagents))
    except OSError:
        return
    for name in names:
        path = os.path.join(subagents, name)
        if name.endswith(".jsonl") and os.path.isfile(path):
            yield "{}/subagents/{}".format(session_id, name), path


def scan_file(path: str, project: str, relpath: str,
              expected_prefix: Optional[Tuple[int, str]] = None) -> Tuple[List[Publish], Optional[bool]]:
    """读一个会话文件，返回 (发布记录, 前缀是否与交接记录一致)。读不了时抛 OSError。

    expected_prefix 是该文件最新一条交接记录的 (字节数, SHA-256)；给了就边读边算这段前缀的哈希，
    不另外读一遍。文件比记录的字节数短，或哈希不同，都算不一致；没给时第二项为 None。
    """
    session_id = relpath.split("/", 1)[0]
    if session_id.endswith(".jsonl"):
        session_id = session_id[:-len(".jsonl")]
    uses: Dict[str, dict] = {}
    found: List[Publish] = []
    digest = hashlib.sha256() if expected_prefix is not None else None
    limit = expected_prefix[0] if expected_prefix is not None else 0
    hashed = 0
    offset = 0
    with open(path, "rb") as handle:
        for line in handle:
            if digest is not None and hashed < limit:
                part = line[:limit - hashed]
                digest.update(part)
                hashed += len(part)
            if any(mark in line for mark in _CANDIDATE_MARKS):
                publish = _parse_line(line, uses, project, relpath, session_id, offset)
                if publish is not None:
                    found.append(publish)
            offset += len(line)
    if expected_prefix is None:
        return found, None
    return found, hashed == limit and digest.hexdigest() == expected_prefix[1]


def _parse_line(line: bytes, uses: Dict[str, dict], project: str, relpath: str, session_id: str,
                offset: int) -> Optional[Publish]:
    """登记本行的 Artifact tool_use；本行是它的成功结果时返回 Publish。"""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "Artifact" and isinstance(block.get("id"), str):
            tool_input = block.get("input")
            uses[block["id"]] = tool_input if isinstance(tool_input, dict) else {}
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
            # 失败的发布（is_error）没有 URL；别的工具的结果里出现 claude.ai 链接也不算，必须对得上 Artifact 调用
            if block.get("is_error") is True:
                return None
            return _publish(record, uses[block["tool_use_id"]], project, relpath, session_id, offset)
    return None


def _publish(record: dict, tool_input: dict, project: str, relpath: str, session_id: str,
             offset: int) -> Optional[Publish]:
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    url = result.get("url")
    # quickstart / read / list 的结果没有单个 url；只认完整匹配的 artifact 链接
    if not isinstance(url, str) or not URL_PATTERN.fullmatch(url):
        return None
    timestamp = history._parse_time(record.get("timestamp"))
    cwd = record.get("cwd")
    uuid = record.get("uuid")
    return Publish(url, _text(result.get("title")), _text(result.get("path")) or None,
                   _text(result.get("audience")) or None, _text(tool_input.get("description")),
                   timestamp, cwd if sessions._usable_cwd(cwd) else None, project, relpath, session_id, offset,
                   uuid if isinstance(uuid, str) and uuid else "{}|{}".format(url, record.get("timestamp")))


def _text(value) -> str:
    return value if isinstance(value, str) else ""


# ---- 交接记录 ----

def log_path() -> str:
    return os.path.join(platform.state_dir(), HANDOFF_LOG)


def record_handoff(handoff: "sessions.Plan", source_account_dir: str, target_account_dir: str) -> None:
    """handoff 复制完成后追加一条交接记录。失败时抛 OSError，由调用方只警告：会话已经复制成功。

    主会话文件的大小与哈希取 handoff.content（就是写进目标的字节）；子代理文件读刚落位的目标副本，
    来源可能仍在被 Claude 追加，取来源会与目标对不上。
    单行可能有几十 KB（一个会话可有上百个子代理文件），一次 write 不保证原子：
    加本文件的排他锁后循环写满整行。被杀留下的半行在读取时按坏行跳过；
    下一次写入先补一个换行，新记录不会接在半行后面一起变成坏行。
    """
    session_id = handoff.session_id
    files = {session_id + ".jsonl": [len(handoff.content), hashlib.sha256(handoff.content).hexdigest()]}
    for relpath, path in _subagent_files(handoff.target_dir, session_id):
        files[relpath] = list(_file_digest(path))
    entry = {"v": HANDOFF_LOG_VERSION,
             "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
             "session_id": session_id, "project": os.path.basename(handoff.target_dir),
             "source_dir": os.path.realpath(source_account_dir), "target_dir": os.path.realpath(target_account_dir),
             "files": files}
    data = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # 阻塞等待：两个 handoff 同时追加时排队，不交错。锁的是本文件，不是工具的写锁（handoff 不拿写锁）
        fcntl.flock(fd, fcntl.LOCK_EX)
        # 只写描述符读不了末字节，所以上面用 O_RDWR 打开
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        # 关闭即释放 flock
        os.close(fd)


def _file_digest(path: str) -> Tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def load_handoffs() -> Tuple[List[Handoff], int]:
    """按行序读交接记录，返回 (记录, 跳过的坏行数)。文件不存在视为没有记录；读不了时抛 OSError。

    读取时以字节切行：补进来的换行会多出一个空行，跳过即可，行号仍单调递增。
    """
    try:
        with open(log_path(), "rb") as handle:
            raw_lines = handle.read().split(b"\n")
    except FileNotFoundError:
        return [], 0
    found: List[Handoff] = []
    bad = 0
    for seq, raw in enumerate(raw_lines):
        if not raw.strip():
            continue
        handoff = _handoff(seq, raw)
        if handoff is None:
            bad += 1
        else:
            found.append(handoff)
    return found, bad


def _handoff(seq: int, raw: bytes) -> Optional[Handoff]:
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("v") != HANDOFF_LOG_VERSION:
        return None
    strings = [entry.get(key) for key in ("session_id", "project", "source_dir", "target_dir")]
    files = entry.get("files")
    if not all(isinstance(value, str) and value for value in strings) or not isinstance(files, dict):
        return None
    parsed: Dict[str, Tuple[int, str]] = {}
    for relpath, value in files.items():
        if not (isinstance(value, list) and len(value) == 2 and isinstance(value[0], int)
                and not isinstance(value[0], bool) and value[0] >= 0 and isinstance(value[1], str)):
            return None
        parsed[relpath] = (value[0], value[1])
    return Handoff(seq, history._parse_time(entry.get("time")), strings[0], strings[1], strings[2], strings[3],
                   parsed)


def recording_started(handoffs: List[Handoff]) -> Optional[datetime]:
    """开始记录时刻 T0：有效记录里最早的写入时间；没有记录时为 None（视为还没开始记录）。

    第一条记录就是本功能上线后的第一次 handoff，所以不需要另写启动标记。
    """
    times = [handoff.time for handoff in handoffs if handoff.time is not None]
    return min(times) if times else None


# ---- 归属判定与汇总 ----

class _Index(object):
    """按 (目标账号目录, 项目, relpath) 索引交接记录，供追链查“某次交接之前最近的一次”。"""

    def __init__(self, handoffs: List[Handoff]) -> None:
        self._by_key: Dict[Tuple[str, str, str], List[Handoff]] = {}
        for handoff in handoffs:
            for relpath in handoff.files:
                self._by_key.setdefault((handoff.target_dir, handoff.project, relpath), []).append(handoff)

    def latest(self, target_dir: str, project: str, relpath: str, before: Optional[int] = None) -> Optional[Handoff]:
        best = None
        for handoff in self._by_key.get((target_dir, project, relpath), ()):
            if (before is None or handoff.seq < before) and (best is None or handoff.seq > best.seq):
                best = handoff
        return best


def _owner_dir(index: _Index, account_dir: str, publish: Publish, prefix_ok: Optional[bool]) -> Optional[str]:
    """这条副本最早是在哪个账号目录里写下的（真实路径）；判不出时为 None。

    副本前 N 字节是第 N 字节交接复制来的：偏移落在里面就到来源账号、在那次交接之前继续追；
    偏移不在里面，说明是交接后在本账号实时写下的。只校验当前文件的前缀，链上更早的环节按记录可信。
    """
    current = account_dir
    before: Optional[int] = None
    while True:
        handoff = index.latest(current, publish.project, publish.relpath, before)
        if handoff is None or publish.offset >= handoff.files[publish.relpath][0]:
            return current
        if before is None and prefix_ok is not True:
            # 当前文件开头已不是当时复制进来的字节，偏移对应关系不成立
            return None
        current, before = handoff.source_dir, handoff.seq


def collect(sources: List[Tuple[str, str]]) -> Tuple[List[Artifact], int, int]:
    """扫描各账号目录，返回 (artifact 列表, 读不了而跳过的会话文件数, 交接记录坏行数)。

    sources 是 (账号名, 账号目录)，要扫全部账号：归属判定要看到所有副本。顺序决定平手时的取舍。
    """
    handoffs, bad_lines = load_handoffs()
    index = _Index(handoffs)
    started = recording_started(handoffs)
    names_by_dir = {os.path.realpath(directory): name for name, directory in sources}
    copies: Dict[str, List[Tuple[Copy, Optional[str]]]] = {}
    skipped = 0
    for name, directory in sources:
        real = os.path.realpath(directory)
        for project, relpath, path in session_files(directory):
            latest = index.latest(real, project, relpath)
            expected = latest.files[relpath] if latest is not None else None
            try:
                found, prefix_ok = scan_file(path, project, relpath, expected)
            except OSError:
                skipped += 1
                continue
            for publish in found:
                owner_dir = _owner_dir(index, real, publish, prefix_ok)
                # 来源目录对不上任何当前账号（账号已删除，或 migrate-default 前的 ~/.claude）：判不出
                owner = names_by_dir.get(owner_dir) if owner_dir is not None else None
                copies.setdefault(publish.record_id, []).append((Copy(name, publish), owner))
    order = {name: position for position, (name, _directory) in enumerate(sources)}
    return _group(_gate(copies, started), order), skipped, bad_lines


def _gate(copies: Dict[str, List[Tuple[Copy, Optional[str]]]],
          started: Optional[datetime]) -> Dict[str, List[Tuple[Copy, Optional[str]]]]:
    """时间闸：开始记录前的发布、又出现在多个账号里的，追链结果不可信，全部改判不确定。

    T0 之后能把记录复制进别的账号的交接一定有记录；T0 之前的交接可能没有记录，追链会停在错误的账号上
    （上线前 A→B、上线后 B→A --force 时，A 的原件成了不扫的备份，两个副本都追到 B）。
    只在一个账号里出现的早期记录没有被复制过的迹象，照常判该账号。
    """
    gated = {}
    for record_id, record_copies in copies.items():
        moment = record_copies[0][0].publish.timestamp
        early = moment is None or started is None or moment < started
        if early and len({copy.account for copy, _owner in record_copies}) > 1:
            record_copies = [(copy, None) for copy, _owner in record_copies]
        gated[record_id] = record_copies
    return gated


def _group(copies: Dict[str, List[Tuple[Copy, Optional[str]]]], order: Dict[str, int]) -> List[Artifact]:
    by_url: Dict[str, List[Tuple[List[Copy], Set[str]]]] = {}
    for record_copies in copies.values():
        # 同一条记录的各副本：所有者取各副本判出的账号的并集，只有全部判不出才算不确定
        owners = {owner for _copy, owner in record_copies if owner is not None}
        url = record_copies[0][0].publish.url
        by_url.setdefault(url, []).append(([copy for copy, _owner in record_copies], owners))
    artifacts = []
    for url, records in by_url.items():
        all_copies = [copy for record_copies, _owners in records for copy in record_copies]
        owners: Set[str] = set()
        certain = True
        for _record_copies, record_owners in records:
            certain = certain and bool(record_owners)
            owners |= record_owners
        seen = sorted({copy.account for copy in all_copies}, key=lambda name: order.get(name, len(order)))
        if certain and len(owners) == 1:
            accounts = sorted(owners)
            # single：副本全在所有者账号里，没靠交接记录；否则（副本在别的账号，或所有者账号里已没有副本）是 recorded
            ownership = OWNERSHIP_SINGLE if seen == accounts else OWNERSHIP_RECORDED
            # 所有者账号里可能已没有副本（交接后删掉了原会话）：退回全部副本，续聊命令指向还在的那份
            pool = [copy for copy in all_copies if copy.account in owners] or all_copies
        else:
            ownership = OWNERSHIP_UNCERTAIN
            accounts = seen
            pool = all_copies
        times = [copy.publish.timestamp for copy in all_copies if copy.publish.timestamp is not None]
        ordered = sorted(all_copies, key=lambda copy: _time_key(copy.publish.timestamp))
        artifacts.append(Artifact(
            url, _last_text(ordered, "title"), _last_text(ordered, "description"), accounts, ownership,
            min(times) if times else None, max(times) if times else None, len(records),
            _representative(pool, order)))
    return artifacts


def _time_key(moment: Optional[datetime]) -> float:
    return moment.timestamp() if moment is not None else float("-inf")


def _last_text(ordered: List[Copy], field: str) -> str:
    for copy in reversed(ordered):
        value = getattr(copy.publish, field)
        if value:
            return value
    return ""


def _representative(pool: List[Copy], order: Dict[str, int]) -> Copy:
    # 时间最晚的优先；时间相同按账号顺序取靠前的，结果稳定
    return min(pool, key=lambda copy: (-_time_key(copy.publish.timestamp), order.get(copy.account, len(order)),
                                       copy.publish.relpath, copy.publish.offset))
