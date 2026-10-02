"""不含凭据的账号配置包：`export NAME FILE` / `import FILE NAME`（方案 feature-config-bundle）。

只按白名单导出配置条目（settings.json、CLAUDE.md、agents、commands、skills、output-styles），
再从 `.claude.json` 里只抽出 `mcpServers` 一项；凭据、`.claude.json` 的其它内容、会话与历史、
plugins/ 下的缓存一律不进包。白名单而不是排除名单：Claude Code 新增的状态文件不会被误带走。

导入时先把整个包解到账号目录下的临时目录并完成全部比较，有冲突（且没有 --force）就什么都不改；
检查通过后才逐条 os.replace（同一文件系统内原子）。中途被打断时，已替换的条目重跑后显示 unchanged，
其余条目继续处理。
"""

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import __version__, accounts, usage
from .actions import error, info, warn
from .config import Account, Config
from .fsutil import KIND_DIR, KIND_FILE, KIND_LINK, KIND_MISSING, atomic_write, entry_kind

ITEMS = ("settings.json", "CLAUDE.md", "agents", "commands", "skills", "output-styles")
# 这两项必须是文件，其余必须是目录：包里把 settings.json 做成目录时，--force 会把文件换成目录
FILE_ITEMS = ("settings.json", "CLAUDE.md")
# 解包上限：防止压缩炸弹写满磁盘
MAX_MEMBERS = 20000
MAX_TOTAL_BYTES = 100 * 1024 * 1024
FORMAT = "multi-claude-config"
VERSION = 1
MANIFEST = "manifest.json"
MCP_FILE = "mcp-servers.json"
ITEMS_DIR = "items"
# 键名像密钥时只提示键名、不显示值（用户 2026-10-03 决定：原样导出并警告）
_SECRET_KEY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", re.IGNORECASE)


class BundleError(Exception):
    """配置包读不了或不合法：退出 1，不改任何东西。"""


# ---- 导出 ----

def export_account(config: Config, account: Account, out_path: str) -> int:
    """把账号配置写成 out_path（.tar.gz，0600）。out_path 已存在（含断开的软链）时退出 3，不覆盖。"""
    # 按原始写法检查目录项是否存在：先 resolve 会让断开的软链“消失”，进而写穿到链接目标。
    if os.path.lexists(out_path):
        error("{} already exists; nothing was written".format(out_path), phase="export", path=out_path)
        return accounts.EXIT_CONFLICT
    parent = os.path.dirname(out_path) or "."
    if not os.path.isdir(parent):
        error("directory {} does not exist".format(parent), phase="export", path=out_path)
        return accounts.EXIT_ERROR
    directory = accounts.account_dir(config, account.name)
    if not os.path.isdir(directory):
        error("account directory does not exist: {}".format(directory), phase="export")
        return accounts.EXIT_ERROR

    exported: List[str] = []
    skipped_links = {}
    servers = _read_mcp_servers(usage.global_state_path(account.identity, directory))
    # 临时文件必须用原始父路径拼出：tempfile 会对 dir 做 abspath，把 link/.. 按字面折叠，
    # 临时文件就落到与 out_path（由内核逐段解析）不同的目录，最后的 link/replace 会失败或跨目录。
    tmp_path, fd = _create_temp(parent)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            for item in ITEMS:
                source = os.path.join(directory, item)
                kind = entry_kind(source)
                if kind == KIND_MISSING:
                    continue
                if kind == KIND_LINK:
                    # 共享条目是指向共享目录的软链：两个账号本来就读同一份，不属于这个账号自己的配置。
                    info("skip {} (link)".format(item))
                    continue
                if kind not in (KIND_FILE, KIND_DIR):
                    continue
                skipped_links[item] = _add_tree(tar, source, "{}/{}".format(ITEMS_DIR, item))
                exported.append(item)
                info("export {}".format(item))
            if servers is not None:
                _add_bytes(tar, MCP_FILE, _json_bytes(servers))
                info("export mcpServers ({})".format(", ".join(sorted(servers)) or "none"))
            manifest = {"format": FORMAT, "version": VERSION, "multi_claude": __version__,
                        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source_account": account.name, "items": exported,
                        "mcp_servers": sorted(servers) if servers is not None else []}
            _add_bytes(tar, MANIFEST, _json_bytes(manifest))
        _publish(tmp_path, out_path)
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.lexists(tmp_path):
            os.unlink(tmp_path)
    for item, count in skipped_links.items():
        if count:
            info("skipped {} link(s) or special file(s) inside {}".format(count, item))
    info("wrote {}".format(out_path))
    secrets = _secret_keys(os.path.join(directory, "settings.json") if "settings.json" in exported else None,
                           servers)
    if secrets:
        warn("{} contains values under keys that look like secrets: {}; keep it private".format(
            out_path, ", ".join(secrets)))
    return accounts.EXIT_OK


def _create_temp(parent: str):
    for _attempt in range(100):
        path = os.path.join(parent, ".multi-claude-export.{}.tar.gz".format(os.urandom(6).hex()))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.fchmod(fd, 0o600)  # 不受 umask 影响：包里可能有密钥，只给本人读
        return path, fd
    raise OSError("cannot create a temporary file in {}".format(parent))


def _publish(tmp_path: str, out_path: str) -> None:
    """把写好的临时文件放到 out_path，且不覆盖检查之后才出现的同名文件：硬链接遇到已存在的目标会失败。"""
    try:
        os.link(tmp_path, out_path)
    except FileExistsError:
        raise OSError("{} appeared while exporting; nothing was written".format(out_path))
    except OSError:
        # 不支持硬链接的文件系统（如 exFAT）：再检查一次后改名
        if os.path.lexists(out_path):
            raise OSError("{} appeared while exporting; nothing was written".format(out_path))
        os.replace(tmp_path, out_path)
        return
    os.unlink(tmp_path)


def _add_tree(tar: tarfile.TarFile, source: str, arcname: str) -> int:
    """加入文件或目录（不跟随软链）；返回跳过的软链与特殊文件数。"""
    if entry_kind(source) == KIND_FILE:
        _add_entry(tar, source, arcname)
        return 0
    skipped = 0
    _add_entry(tar, source, arcname)
    for root, dirs, files in os.walk(source):
        rel_root = os.path.relpath(root, source)
        for name in sorted(dirs):
            path = os.path.join(root, name)
            if entry_kind(path) != KIND_DIR:
                skipped += 1  # 指向目录的软链：os.walk 不会进入，但出现在 dirs 里
                continue
            _add_entry(tar, path, _join(arcname, rel_root, name))
        dirs[:] = [name for name in dirs if entry_kind(os.path.join(root, name)) == KIND_DIR]
        for name in sorted(files):
            path = os.path.join(root, name)
            if entry_kind(path) != KIND_FILE:
                skipped += 1
                continue
            _add_entry(tar, path, _join(arcname, rel_root, name))
    return skipped


def _join(arcname: str, rel_root: str, name: str) -> str:
    return "/".join(part for part in (arcname, "" if rel_root == "." else rel_root.replace(os.sep, "/"), name) if part)


def _add_entry(tar: tarfile.TarFile, path: str, arcname: str) -> None:
    info_ = tar.gettarinfo(path, arcname)
    # 不带本机的用户名与 uid：包可能被搬到另一台机器
    info_.uid = info_.gid = 0
    info_.uname = info_.gname = ""
    if info_.isfile():
        with open(path, "rb") as handle:
            tar.addfile(info_, handle)
    else:
        tar.addfile(info_)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    entry = tarfile.TarInfo(name)
    entry.size = len(data)
    entry.mode = 0o600
    entry.mtime = int(time.time())
    tar.addfile(entry, io.BytesIO(data))


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _read_mcp_servers(path: str) -> Optional[dict]:
    """只取 .claude.json 顶层的 mcpServers；文件不存在或没有这一项时为 None。登录与项目状态一律不读出。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        warn("cannot read {} ({}); MCP servers are not exported".format(path, exc))
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else None


def _secret_keys(settings_path: Optional[str], servers: Optional[dict]) -> List[str]:
    found = []
    if settings_path is not None:
        try:
            with open(settings_path, "r", encoding="utf-8") as handle:
                settings = json.load(handle)
        except (OSError, ValueError):
            settings = None
        env = settings.get("env") if isinstance(settings, dict) else None
        if isinstance(env, dict):
            found.extend("env.{}".format(key) for key in env if _SECRET_KEY.search(key))
    for name, server in sorted((servers or {}).items()):
        if not isinstance(server, dict):
            continue
        for field in ("env", "headers"):
            values = server.get(field)
            if isinstance(values, dict):
                found.extend("mcpServers.{}.{}.{}".format(name, field, key) for key in values
                             if _SECRET_KEY.search(key))
    return found


# ---- 导入 ----

class _Plan:
    def __init__(self) -> None:
        self.items: List[Tuple[str, str]] = []        # (条目, create|replace|unchanged)
        self.servers: List[Tuple[str, str]] = []      # (服务器名, add|replace|unchanged)
        self.conflicts: List[str] = []                # --force 可以处理的冲突
        self.blocked: List[str] = []                  # --force 也不能处理的冲突


def import_account(config: Config, account: Account, archive: str, force: bool, dry_run: bool) -> int:
    directory = accounts.account_dir(config, account.name)
    if not os.path.isdir(directory):
        error("account directory does not exist: {}".format(directory), phase="import")
        return accounts.EXIT_ERROR
    _remove_leftover_staging(directory)
    staging = tempfile.mkdtemp(prefix=".multi-claude-import.", dir=directory)
    try:
        try:
            manifest = _extract(archive, staging)
        except BundleError as exc:
            error(str(exc), phase="import", path=archive)
            return accounts.EXIT_ERROR
        state_path = usage.global_state_path(account.identity, directory)
        plan = _plan_import(directory, staging, state_path)
        if plan is None:
            return accounts.EXIT_ERROR
        _print_plan(plan, dry_run)
        if plan.blocked or (plan.conflicts and not force):
            for line in plan.blocked:
                error(line, phase="import")
            if not force:
                for line in plan.conflicts:
                    error(line, phase="import")
            info("nothing was changed{}".format("" if plan.blocked else " (use --force to back up and replace)"))
            return accounts.EXIT_CONFLICT
        if dry_run:
            return accounts.EXIT_OK
        _apply(plan, directory, staging, state_path)
        info("imported {} into {}".format(manifest.get("source_account", archive), account.name))
        return accounts.EXIT_OK
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _remove_leftover_staging(directory: str) -> None:
    """清掉上次被杀时留下的临时目录。调用方持有写锁，同一时刻不会有另一个导入在用它们。"""
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if name.startswith(".multi-claude-import.") and entry_kind(path) == KIND_DIR:
            shutil.rmtree(path, ignore_errors=True)


def _extract(archive: str, staging: str) -> dict:
    """校验并解包到 staging；任何不合规的成员都让整个导入失败。返回 manifest。

    Python 3.8 没有 extractall(filter="data")，所以逐个成员手工写出：权限统一为 0600 / 0700（带执行位的文件），
    不保留包里的属主、setuid 等位；限制成员数与解压总量；拒绝重复路径。
    """
    try:
        tar = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise BundleError("cannot read {}: {}".format(archive, exc))
    with tar:
        try:
            members = tar.getmembers()
        except (OSError, tarfile.TarError) as exc:
            raise BundleError("cannot read {}: {}".format(archive, exc))
        if len(members) > MAX_MEMBERS:
            raise BundleError("{} has too many entries ({})".format(archive, len(members)))
        seen = set()
        total = 0
        for member in members:
            _check_member(member)
            if member.name in seen:
                raise BundleError("duplicate entry in bundle: {!r}".format(member.name))
            seen.add(member.name)
            total += member.size if member.isfile() else 0
            if total > MAX_TOTAL_BYTES:
                raise BundleError("{} is larger than {} MB when unpacked".format(archive, MAX_TOTAL_BYTES >> 20))
        _check_item_types(members)
        if MANIFEST not in seen:
            raise BundleError("{} is not a multi-claude configuration bundle (no {})".format(archive, MANIFEST))
        for member in members:
            target = os.path.join(staging, *member.name.split("/"))
            if member.isdir():
                os.makedirs(target, mode=0o700, exist_ok=True)
                os.chmod(target, 0o700)
                continue
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise BundleError("cannot read {} from {}".format(member.name, archive))
            with source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(target, 0o700 if member.mode & 0o111 else 0o600)
    try:
        with open(os.path.join(staging, MANIFEST), "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        raise BundleError("invalid {}: {}".format(MANIFEST, exc))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BundleError("{} is not a multi-claude configuration bundle".format(archive))
    if manifest.get("version") != VERSION:
        raise BundleError("unsupported bundle version {!r}; upgrade multi-claude".format(manifest.get("version")))
    return manifest


def _check_item_types(members: List[tarfile.TarInfo]) -> None:
    """settings.json、CLAUDE.md 只能是一个文件；目录条目下的成员必须挂在目录下。"""
    for member in members:
        parts = member.name.split("/")
        if parts[0] != ITEMS_DIR:
            continue
        item = parts[1]
        if item in FILE_ITEMS and (len(parts) != 2 or not member.isfile()):
            raise BundleError("{} must be a single file in the bundle".format(item))
        if item not in FILE_ITEMS and len(parts) == 2 and not member.isdir():
            raise BundleError("{} must be a directory in the bundle".format(item))


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    parts = name.split("/")
    if name.startswith("/") or "\\" in name or "\x00" in name or any(part in ("", ".", "..") for part in parts):
        raise BundleError("unsafe path in bundle: {!r}".format(name))
    if not (member.isfile() or member.isdir()):
        raise BundleError("unsupported entry in bundle (only files and directories): {!r}".format(name))
    if parts[0] in (MANIFEST, MCP_FILE):
        if len(parts) != 1 or not member.isfile():
            raise BundleError("unexpected entry in bundle: {!r}".format(name))
        return
    if parts[0] != ITEMS_DIR or len(parts) < 2 or parts[1] not in ITEMS:
        raise BundleError("unexpected entry in bundle: {!r}".format(name))


def _plan_import(directory: str, staging: str, state_path: str) -> Optional[_Plan]:
    plan = _Plan()
    staged_items = os.path.join(staging, ITEMS_DIR)
    for item in ITEMS:
        staged = os.path.join(staged_items, item)
        if not os.path.lexists(staged):
            continue
        target = os.path.join(directory, item)
        kind = entry_kind(target)
        if kind == KIND_MISSING:
            plan.items.append((item, "create"))
        elif kind == KIND_LINK:
            plan.blocked.append("{} is a shared link in this account; turn sharing off for it first "
                                "(multi-claude set NAME --shared-exclude {})".format(item, item))
        elif _tree_digest(staged) == _tree_digest(target):
            plan.items.append((item, "unchanged"))
        else:
            plan.items.append((item, "replace"))
            plan.conflicts.append("{} already exists with different content".format(item))
    staged_mcp = os.path.join(staging, MCP_FILE)
    if os.path.exists(staged_mcp):
        try:
            with open(staged_mcp, "r", encoding="utf-8") as handle:
                servers = json.load(handle)
        except (OSError, ValueError) as exc:
            error("invalid {} in bundle: {}".format(MCP_FILE, exc), phase="import")
            return None
        if not isinstance(servers, dict):
            error("invalid {} in bundle".format(MCP_FILE), phase="import")
            return None
        current = _load_state(state_path)
        if current is None:
            return None
        existing = current.get("mcpServers", {})
        if not isinstance(existing, dict):
            plan.blocked.append("mcpServers in {} is not a JSON object; fix it by hand first".format(state_path))
            return plan
        for name in sorted(servers):
            if name not in existing:
                plan.servers.append((name, "add"))
            elif existing[name] == servers[name]:
                plan.servers.append((name, "unchanged"))
            else:
                plan.servers.append((name, "replace"))
                plan.conflicts.append("MCP server {} already exists with a different configuration".format(name))
    return plan


def _load_state(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        error("cannot read {} ({}); close Claude sessions of this account and retry".format(path, exc),
              phase="import", path=path)
        return None
    if not isinstance(data, dict):
        error("{} is not a JSON object".format(path), phase="import", path=path)
        return None
    return data


def _print_plan(plan: _Plan, dry_run: bool) -> None:
    prefix = "(dry-run) " if dry_run else ""
    for item, action in plan.items:
        info("{}import {}: {}".format(prefix, item, action))
    for name, action in plan.servers:
        info("{}mcp server {}: {}".format(prefix, name, action))


def _apply(plan: _Plan, directory: str, staging: str, state_path: str) -> None:
    """检查都通过后才执行。.claude.json 先读好再动条目：读失败时什么都没改，不会留下“条目已换、MCP 未写”的半截状态。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    changed = [(name, action) for name, action in plan.servers if action != "unchanged"]
    data = None
    if changed:
        with open(os.path.join(staging, MCP_FILE), "r", encoding="utf-8") as handle:
            servers = json.load(handle)
        # .claude.json 是软链时（例如用 dotfiles 仓库管理）写到链接目标，不把软链换成普通文件
        state_path = os.path.realpath(state_path) if entry_kind(state_path) == KIND_LINK else state_path
        data = _load_state(state_path)
        if data is None:
            raise OSError("cannot read {}".format(state_path))
    for item, action in plan.items:
        if action == "unchanged":
            continue
        target = os.path.join(directory, item)
        if action == "replace":
            backup = _backup_name(target, stamp)
            os.rename(target, backup)
            info("backup: {}".format(backup))
        os.replace(os.path.join(staging, ITEMS_DIR, item), target)
    if data is None:
        return
    warn("Claude sessions of this account that are running keep their own copy of {} and may overwrite "
         "the imported MCP servers when they save it; restart them now".format(os.path.basename(state_path)))
    if any(action == "replace" for _name, action in changed) and os.path.exists(state_path):
        backup = _backup_name(state_path, stamp)
        shutil.copy2(state_path, backup)
        info("backup: {}".format(backup))
    merged = dict(data.get("mcpServers") or {}) if isinstance(data.get("mcpServers"), dict) else {}
    for name, _action in changed:
        merged[name] = servers[name]
    data["mcpServers"] = merged
    mode = stat.S_IMODE(os.stat(state_path).st_mode) if os.path.exists(state_path) else 0o600
    atomic_write(state_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)


def _backup_name(path: str, stamp: str) -> str:
    candidate = "{}.multi-claude-bak.{}".format(path, stamp)
    counter = 1
    while os.path.lexists(candidate):
        counter += 1
        candidate = "{}.multi-claude-bak.{}-{}".format(path, stamp, counter)
    return candidate


def _tree_digest(path: str) -> Dict[str, str]:
    """文件按字节、目录按“相对路径 + 类型 + 内容”比较；不跟随软链。"""
    if entry_kind(path) == KIND_FILE:
        return {".": "file:" + _sha256(path)}
    result = {}
    for root, dirs, files in os.walk(path):
        rel_root = os.path.relpath(root, path)
        for name in dirs:
            entry = os.path.join(root, name)
            result[os.path.normpath(os.path.join(rel_root, name))] = (
                "dir" if entry_kind(entry) == KIND_DIR else "other")
        dirs[:] = [name for name in dirs if entry_kind(os.path.join(root, name)) == KIND_DIR]
        for name in files:
            entry = os.path.join(root, name)
            key = os.path.normpath(os.path.join(rel_root, name))
            result[key] = "file:" + _sha256(entry) if entry_kind(entry) == KIND_FILE else "other"
    return result


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
