"""会话交接与会话列表。

交接：把一条 Claude 会话从一个账号复制到另一个账号（方案 feature-session-handoff）。
列表：只读列出一个账号目录下全部项目的会话（方案 feature-session-list），见文件末尾。

Claude 把会话存成 `<配置目录>/projects/<编码后的工作目录>/<会话ID>.jsonl`，旁边的同名目录
放子代理记录与存成文件的大段工具输出。目录名编码是“非字母数字字符换成 -”，超过 200 字符时
截断再接一段 Claude 内部的哈希；本模块不复刻那段哈希，目标账号直接沿用来源账号里已有的目录名。

交接只复制，不移动、不删除来源，也不改写会话内容；目标里已有不同内容的同 ID 会话时默认拒绝。
"""

import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import history, statusline
from .fsutil import build_manifest, copy_tree, diff_manifests

# Claude 2.1.286 的 `tx`：编码后的目录名超过这个长度就截断并接哈希。
ENCODED_LIMIT = 200
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
# 会话列表每个文件的读取量：尾部一块（字节），头部逐行累计的上限（字节）。
# 标题行会随会话进行反复写入，所以尾部一块就能取到；头部要逐行读，因为单行（大段粘贴）可能比任何固定块都长。
TAIL_BYTES = 64 * 1024
HEAD_BUDGET = 1024 * 1024

COPIED = "copied"
UNCHANGED = "unchanged"
CONFLICT = "conflict"


class HandoffError(Exception):
    """找不到目录或会话、复制失败等，对应退出码 1。"""


class Plan(NamedTuple):
    session_id: str
    # 找到会话目录时用的工作目录写法；续聊命令必须 cd 到这个写法，目标账号才会按同一目录名找会话
    cwd: str
    # 来源账号里的项目目录；目标账号沿用它的目录名
    source_dir: str
    target_dir: str
    # 截到最后一个换行后的会话内容
    content: bytes
    # copied（将要 / 已经复制）、unchanged 或 conflict
    state: str


def encode(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def cwd_variants(environ: Dict[str, str]) -> List[str]:
    """Claude 编码时用的是物理路径还是 $PWD 没有核实，两种都试；$PWD 必须与当前目录是同一个目录。"""
    variants = [os.getcwd()]
    pwd = environ.get("PWD")
    if pwd and os.path.isabs(pwd) and pwd not in variants:
        try:
            if os.path.samefile(pwd, variants[0]):
                variants.append(pwd)
        except OSError:
            pass
    return variants


def project_dir(account_dir: str, variants: List[str]) -> Tuple[str, str]:
    """在账号的 projects/ 下找当前工作目录对应的目录，返回 (目录, 命中的工作目录写法)；没有或有多个时抛 HandoffError。"""
    projects = os.path.join(account_dir, "projects")
    try:
        names = os.listdir(projects)
    except OSError:
        names = []
    found: List[Tuple[str, str]] = []
    for variant in variants:
        encoded = encode(variant)
        if len(encoded) <= ENCODED_LIMIT:
            matches = [encoded] if encoded in names else []
        else:
            # 超长路径的目录名后缀是 Claude 内部哈希，只能按截断后的前缀找。
            prefix = encoded[:ENCODED_LIMIT] + "-"
            matches = [name for name in names if name.startswith(prefix)]
        for name in matches:
            path = os.path.join(projects, name)
            if os.path.isdir(path) and path not in [item[0] for item in found]:
                found.append((path, variant))
    if not found:
        raise HandoffError("no sessions for {} in {}".format(variants[0], projects))
    if len(found) > 1:
        raise HandoffError("several session directories match {}: {}".format(
            variants[0], ", ".join(item[0] for item in found)))
    return found[0]


def pick_session(directory: str, requested: Optional[str], current: Optional[str]) -> str:
    """选会话：--session 优先；其次当前会话（仅当它在这个目录里）；最后取修改时间最新的一条。"""
    if requested is not None:
        if not os.path.isfile(os.path.join(directory, requested + ".jsonl")):
            raise HandoffError("session {} not found in {}".format(requested, directory))
        return requested
    if current and SESSION_ID_PATTERN.match(current) and os.path.isfile(os.path.join(directory, current + ".jsonl")):
        return current
    candidates = [name[:-len(".jsonl")] for name in os.listdir(directory)
                  if name.endswith(".jsonl") and os.path.isfile(os.path.join(directory, name))]
    if not candidates:
        raise HandoffError("no sessions in {}".format(directory))
    return max(candidates, key=lambda sid: os.stat(os.path.join(directory, sid + ".jsonl")).st_mtime_ns)


def plan(source_account_dir: str, target_account_dir: str, variants: List[str],
         requested: Optional[str], current: Optional[str]) -> Plan:
    source_dir, cwd = project_dir(source_account_dir, variants)
    session_id = pick_session(source_dir, requested, current)
    with open(os.path.join(source_dir, session_id + ".jsonl"), "rb") as handle:
        raw = handle.read()
    # 来源会话可能正在追加，只取到最后一个完整行，避免目标里出现半行 JSON。
    end = raw.rfind(b"\n")
    if end < 0:
        raise HandoffError("session {} has no complete lines yet".format(session_id))
    content = raw[:end + 1]
    target_dir = os.path.join(target_account_dir, "projects", os.path.basename(source_dir))
    return Plan(session_id, cwd, source_dir, target_dir, content,
                _state(source_dir, target_dir, session_id, content))


def _state(source_dir: str, target_dir: str, session_id: str, content: bytes) -> str:
    target_file = os.path.join(target_dir, session_id + ".jsonl")
    target_extra = os.path.join(target_dir, session_id)
    if not os.path.lexists(target_file) and not os.path.lexists(target_extra):
        return COPIED
    try:
        with open(target_file, "rb") as handle:
            same_file = handle.read() == content
    except OSError:
        same_file = False
    return UNCHANGED if same_file and _same_extra(os.path.join(source_dir, session_id), target_extra) else CONFLICT


def _same_extra(source: str, target: str) -> bool:
    has_source, has_target = os.path.isdir(source), os.path.lexists(target)
    if not has_source or not has_target:
        return has_source == has_target
    if not os.path.isdir(target) or os.path.islink(target):
        return False
    return not diff_manifests(build_manifest(source)[0], build_manifest(target)[0])


def execute(handoff: Plan, force: bool) -> List[str]:
    """按计划复制，返回 --force 时生成的备份路径。

    同名目录先落位、会话文件最后落位：会话文件出现就说明这次复制是完整的。
    任一步失败时删除本次的临时项；已落位的目录保留，再次运行时会参与冲突判定，不会被静默覆盖。
    """
    session_id = handoff.session_id
    target_file = os.path.join(handoff.target_dir, session_id + ".jsonl")
    target_extra = os.path.join(handoff.target_dir, session_id)
    backups = []
    if handoff.state == CONFLICT:
        if not force:
            raise RuntimeError("conflict must be handled by the caller")
        # 备份名不以 .jsonl 结尾，Claude 不会把它当成会话列出来。
        for path in (target_file, target_extra):
            if os.path.lexists(path):
                backup = statusline._backup_path(path)
                os.rename(path, backup)
                backups.append(backup)
    os.makedirs(handoff.target_dir, exist_ok=True)
    source_extra = os.path.join(handoff.source_dir, session_id)
    if os.path.isdir(source_extra):
        staging = os.path.join(handoff.target_dir, ".multi-claude-{}.tmp".format(session_id))
        # 上次复制中途被杀会留下临时目录，它从未落位、不属于任何会话，直接清掉再复制。
        _remove_tree(staging)
        try:
            copy_tree(source_extra, staging)
            os.rename(staging, target_extra)
        except BaseException:
            _remove_tree(staging)
            raise
    fd, tmp_path = tempfile.mkstemp(prefix=".multi-claude-{}.".format(session_id), suffix=".jsonl.tmp",
                                    dir=handoff.target_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(handoff.content)
            handle.flush()
            os.fsync(handle.fileno())
        # 会话里可能有提示词与代码，与 Claude 自己写的会话文件一样只给属主读写。
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, target_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return backups


def _remove_tree(path: str) -> None:
    if os.path.lexists(path):
        shutil.rmtree(path, ignore_errors=True)


# ---- 会话列表（方案 feature-session-list） ----

TITLE_CUSTOM = "custom"
TITLE_AI = "ai"
TITLE_PROMPT = "prompt"
TITLE_NONE = "none"


class SessionInfo(NamedTuple):
    session_id: str
    # 会话启动时的工作目录（续聊要先 cd 到这里）；会话文件里取不到可信的值时为 None
    directory: Optional[str]
    # 磁盘上的项目目录名（编码后的工作目录，不可逆），directory 为 None 时用来提示用户
    project: str
    # 未清洗、未截断的原文；来自会话内容，输出前必须过 clean_title
    title: str
    # custom、ai、prompt 或 none
    title_source: str
    # 第一行带时间戳的记录的时间（UTC）；取不到时为 None。不用文件创建时间：handoff 复制的副本是复制时间
    created: Optional[datetime]
    # 最后一行带时间戳的记录的时间（UTC）；取不到时退到文件修改时间
    updated: datetime
    path: str


def list_sessions(account_dir: str) -> Tuple[List[SessionInfo], int]:
    """只读列出账号目录下全部项目的会话，返回 (会话列表, 读不了而跳过的文件数)。

    只看 projects/<项目目录>/ 第一层、文件名是 <会话ID>.jsonl 的常规文件：子代理记录在更深一层，
    handoff 的临时文件与备份都不以 .jsonl 结尾。projects/ 不存在视为没有会话。
    """
    projects = os.path.join(account_dir, "projects")
    try:
        project_names = sorted(os.listdir(projects))
    except OSError:
        return [], 0
    found: List[SessionInfo] = []
    skipped = 0
    for project in project_names:
        directory = os.path.join(projects, project)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            # 不是目录，或读不了：里面有没有会话无从得知，不计入 skipped（那是“看得见但读不了的会话文件”）
            continue
        for name in names:
            session_id = name[:-len(".jsonl")]
            # fullmatch：match 加 $ 会放过结尾带换行的文件名，换行进了输出就把一条会话拆成两行。
            # 以 - 开头的名字拼进续聊命令会被 claude 当成选项，不是会话 ID
            if not name.endswith(".jsonl") or not SESSION_ID_PATTERN.fullmatch(session_id) \
                    or session_id.startswith("-"):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                found.append(read_session(path))
            except OSError:
                skipped += 1
    return found, skipped


def read_session(path: str) -> SessionInfo:
    """从一个会话文件取列表要用的字段；只读尾部一块和头部若干行，不通读文件。读不了时抛 OSError。

    记录格式未写入官方文档：解析失败、不是对象、字段类型不符的行一律跳过，取不到的字段按 SessionInfo 的说明兜底。
    """
    project = os.path.basename(os.path.dirname(path))
    with open(path, "rb") as handle:
        stat = os.fstat(handle.fileno())
        handle.seek(max(0, stat.st_size - TAIL_BYTES))
        tail = [record for record in map(_record, handle.read(TAIL_BYTES).split(b"\n")) if record is not None]
        handle.seek(0)
        head = _scan_head(handle, has_tail_title=any(_title_of(record)[0] for record in tail))
    tail_titles = _last_titles(tail)
    title, source = "", TITLE_NONE
    # 尾部窗口先于头部窗口；同一窗口内 custom 先于 ai（方案 §3、§5.1.2）
    for candidate, candidate_source in ((tail_titles[TITLE_CUSTOM], TITLE_CUSTOM), (tail_titles[TITLE_AI], TITLE_AI),
                                        (head.titles[TITLE_CUSTOM], TITLE_CUSTOM), (head.titles[TITLE_AI], TITLE_AI),
                                        (head.prompt, TITLE_PROMPT)):
        if candidate:
            title, source = candidate, candidate_source
            break
    directory = head.cwd
    if directory is None and not head.cwd_rejected:
        # 头部没读到 cwd（第一个带 cwd 的行比头部额度还长）：会话中途可能换过目录，
        # 只认编码后正好等于项目目录名的那个，它才是启动目录。
        for record in tail:
            cwd = record.get("cwd")
            if _usable_cwd(cwd) and encode(cwd) == project:
                directory = cwd
    updated = None
    for record in tail:
        updated = history._parse_time(record.get("timestamp")) or updated
    if updated is None:
        updated = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    return SessionInfo(os.path.basename(path)[:-len(".jsonl")], directory, project, title, source,
                       head.created, updated, path)


class _Head(NamedTuple):
    cwd: Optional[str]
    # 头部第一个 cwd 含控制字符而被拒绝：此时不能再用后面或尾部的 cwd 顶替
    cwd_rejected: bool
    created: Optional[datetime]
    titles: Dict[str, str]
    prompt: str


def _scan_head(handle, has_tail_title: bool) -> _Head:
    """从文件开头逐行读，取启动目录、创建时间、标题与首条提问；凑齐就停，最多读 HEAD_BUDGET 字节。

    停止条件是“见过 cwd、有了创建时间、并且标题有着落（尾部已有标题，或读到了首条提问）”。
    所以标题行夹在首条提问之后、又不在尾部窗口里的会话，读不到那个标题，显示首条提问
    （方案 §5.1.2 的已知限制）。这是有意的取舍：为它继续往后读，就等于通读每个长会话。
    """
    cwd: Optional[str] = None
    cwd_seen = False
    created: Optional[datetime] = None
    titles = {TITLE_CUSTOM: "", TITLE_AI: ""}
    prompt = ""
    used = 0
    while used < HEAD_BUDGET:
        # 带上限读：一行比剩余额度还长时只读到额度为止，不把整行（可能几十 MB 的粘贴内容）读进内存
        raw = handle.readline(HEAD_BUDGET - used + 1)
        if not raw:
            break
        used += len(raw)
        if not raw.endswith(b"\n") and used > HEAD_BUDGET:
            break
        record = _record(raw)
        if record is None:
            continue
        if not cwd_seen and isinstance(record.get("cwd"), str) and record["cwd"]:
            # 只认第一个 cwd：它是启动目录。它不合法时宁可报“目录未知”，也不拿后面的目录顶替
            cwd_seen = True
            cwd = record["cwd"] if _usable_cwd(record["cwd"]) else None
        if created is None:
            created = history._parse_time(record.get("timestamp"))
        text, kind = _title_of(record)
        if text:
            titles[kind] = text
        if not prompt:
            prompt = _plain_prompt(record)
        if cwd_seen and created is not None and (has_tail_title or prompt):
            break
    return _Head(cwd, cwd_seen and cwd is None, created, titles, prompt)


def _record(raw: bytes) -> Optional[dict]:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        # RecursionError：嵌套上千层的 JSON 行。它不是 ValueError，漏掉它一个坏文件就让整条命令带着调用栈退出
        return None
    return data if isinstance(data, dict) else None


def _title_of(record: dict) -> Tuple[str, str]:
    """标题行返回 (标题, custom 或 ai)，其它行返回 ("", "")。"""
    for kind, type_name, key in ((TITLE_CUSTOM, "custom-title", "customTitle"), (TITLE_AI, "ai-title", "aiTitle")):
        if record.get("type") == type_name:
            value = record.get(key)
            # 按清洗后的结果判断：只有控制字符的标题打印出来是空的，不能让它挡住后面的候选
            if isinstance(value, str) and clean_title(value, len(value)):
                return value, kind
    return "", ""


def _last_titles(records: List[dict]) -> Dict[str, str]:
    titles = {TITLE_CUSTOM: "", TITLE_AI: ""}
    for record in records:
        text, kind = _title_of(record)
        if text:
            titles[kind] = text
    return titles


def _plain_prompt(record: dict) -> str:
    """用户自己敲的提问返回原文，其它行返回空串。

    isMeta / isSidechain 为真的行、以 < 开头的内容（斜杠命令的包装、系统提示）都不是用户敲的提问。
    普通提问行可能带着 "isSidechain": false，所以按值判断，不按键是否存在判断。
    """
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return ""
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = next((block.get("text") for block in content
                        if isinstance(block, dict) and block.get("type") == "text"), None)
    if not isinstance(content, str):
        return ""
    stripped = content.strip()
    return "" if not stripped or stripped.startswith("<") else content


# 不能原样打印的字符：控制字符（Cc，含换行与终端转义的 ESC）、格式字符（Cf，如能把后面的文字
# 倒过来显示的 U+202E）、行分隔符与段分隔符（Zl、Zp）
_UNPRINTABLE = frozenset(("Cc", "Cf", "Zl", "Zp"))


# 零宽非连接符与零宽连接符也属于 Cf，但它们是正常文字的一部分（波斯语等文字的拼写、组合表情），
# 也不会改变别的字符的显示：删掉会拆开表情，目录名带着它的会话还会丢掉 cd
_KEPT_FORMAT_CHARS = frozenset(("\u200c", "\u200d"))


def _unprintable(char: str) -> bool:
    return unicodedata.category(char) in _UNPRINTABLE and char not in _KEPT_FORMAT_CHARS


def _has_control(text: str) -> bool:
    return any(_unprintable(char) for char in text)


def _usable_cwd(value) -> bool:
    # 目录要原样拼进续聊命令；shlex.quote 不去换行与控制字符，含这些字符的目录会破坏输出，也可能改写终端显示。
    # 必须是绝对路径：相对路径换个地方 cd 就不是原来的目录，`-` 还会被 cd 当成“上一个目录”
    return isinstance(value, str) and os.path.isabs(value) and not _has_control(value)


def clean_title(text: str, limit: int) -> str:
    """把来自会话内容的文字变成能安全打印的一行：空白压成一个空格，删控制与格式字符，超长截断加 …。

    标题与提问是会话里的任意文字，可能含换行和终端转义序列，原样打印会破坏表格，转义序列还能改写终端显示。
    """
    flat = " ".join(text.split())
    flat = "".join(char for char in flat if not _unprintable(char)).strip()
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"
