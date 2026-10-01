"""会话交接：把一条 Claude 会话从一个账号复制到另一个账号（方案 feature-session-handoff）。

Claude 把会话存成 `<配置目录>/projects/<编码后的工作目录>/<会话ID>.jsonl`，旁边的同名目录
放子代理记录与存成文件的大段工具输出。目录名编码是“非字母数字字符换成 -”，超过 200 字符时
截断再接一段 Claude 内部的哈希；本模块不复刻那段哈希，目标账号直接沿用来源账号里已有的目录名。

只复制，不移动、不删除来源，也不改写会话内容；目标里已有不同内容的同 ID 会话时默认拒绝。
"""

import os
import re
import shutil
import tempfile
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import statusline
from .fsutil import build_manifest, copy_tree, diff_manifests

# Claude 2.1.286 的 `tx`：编码后的目录名超过这个长度就截断并接哈希。
ENCODED_LIMIT = 200
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

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
