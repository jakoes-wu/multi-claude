"""文件系统工具：原子写入、路径判定、文件清单与校验、跳过特殊文件的复制。

本模块只做与业务无关的文件操作，不读取配置，也不输出日志；
调用方负责决定何时调用以及如何报告结果。
"""

import hashlib
import os
import shutil
import stat
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

# entry_kind() 的返回值
KIND_MISSING = "missing"
KIND_LINK = "link"
KIND_DIR = "dir"
KIND_FILE = "file"
# socket、FIFO、设备文件等运行时产物
KIND_SPECIAL = "special"


def expand(path: str) -> str:
    """展开 `~` 并转成绝对路径；不解析软链，调用方需要真实路径时自行 realpath。"""
    return os.path.abspath(os.path.expanduser(path))


def is_under(path: str, root: str) -> bool:
    """path 等于 root 或位于 root 之下。

    必须按路径段比较：直接用 startswith(root) 会把 `~/.claude-shared`
    误判为位于 `~/.claude` 之下。
    """
    root = root.rstrip(os.sep) or os.sep
    if path == root:
        return True
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path.startswith(prefix)


def entry_kind(path: str) -> str:
    """按 lstat 判断目录项类型，不跟随软链；断开的软链返回 KIND_LINK。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return KIND_MISSING
    if stat.S_ISLNK(st.st_mode):
        return KIND_LINK
    if stat.S_ISDIR(st.st_mode):
        return KIND_DIR
    if stat.S_ISREG(st.st_mode):
        return KIND_FILE
    return KIND_SPECIAL


def same_target(link_path: str, target: str) -> bool:
    """link_path 解析后的真实路径是否与 target 的真实路径相同。

    两边都用 realpath，这样 `~` 写法、多级软链或尾部斜杠都不影响判断。
    """
    return os.path.realpath(link_path) == os.path.realpath(target)


def atomic_write(path: str, content: str, mode: int = 0o644) -> None:
    """原子替换 path 的内容：写临时文件、fsync，再用 os.replace 覆盖。

    进程在任何时刻被杀，path 要么是旧内容，要么是完整的新内容，
    不会出现写了一半的文件。临时文件与目标在同一目录，保证 replace 不跨设备。
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".multi-claude-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(directory)


def _fsync_dir(directory: str) -> None:
    # 让 rename 本身落盘；部分文件系统不支持对目录 fsync，失败可以忽略。
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def read_text(path: str) -> Optional[str]:
    """读取 UTF-8 文本；文件不存在、不可读或不是文本时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def remove_path(path: str) -> None:
    """删除一个目录项：软链只删链接本身，目录整体删除，普通文件直接删除。

    路径不存在时什么也不做，便于中断后重跑时重复调用。
    """
    kind = entry_kind(path)
    if kind == KIND_MISSING:
        return
    if kind == KIND_DIR:
        _grant_owner_access(path)
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _grant_owner_access(root: str) -> None:
    """删除前给目录树里的每个目录补上本人的读、写、执行权限。

    没有读或执行权限的目录，rmtree 既列不出内容也删不掉里面的文件；
    而用 rmtree 的错误回调补权限再重试并不可靠（新版 rmtree 按文件描述符遍历，
    回调拿到的函数可能是 os.open，直接重调会抛 TypeError）。
    所以先自上而下补齐权限：父目录先改，os.walk 才能继续列出子目录。软链不跟随、不修改。
    权限已齐全的目录不动：中途遇到无权修改的目录会抛错，此时已处理过的目录应尽量保持原样。
    """
    _add_owner_rwx(root)
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if entry_kind(full) == KIND_DIR:
                _add_owner_rwx(full)


def _add_owner_rwx(path: str) -> None:
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    if mode & stat.S_IRWXU != stat.S_IRWXU:
        os.chmod(path, mode | stat.S_IRWXU)


# 清单条目：(类型, 大小, SHA-256 或软链目标)。目录的大小与哈希固定为 0 和空串。
ManifestEntry = Tuple[str, int, str]


def build_manifest(root: str) -> Tuple[Dict[str, ManifestEntry], List[str]]:
    """递归生成 root 的文件清单，返回 (清单, 跳过的特殊文件相对路径)。

    软链不跟随，只记录链接目标；socket、FIFO、设备文件属于运行时产物，
    不进清单，单独返回给调用方写日志。
    """
    manifest: Dict[str, ManifestEntry] = {}
    skipped: List[str] = []
    # onerror 必须抛出：os.walk 默认静默跳过读不了的目录，两侧清单会同样缺一整棵子树，
    # 校验因此误判“一致”，随后 park 并删除备份，数据就丢了。
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        entries = [(name, True) for name in dirnames] + [(name, False) for name in filenames]
        for name, _is_dir_hint in entries:
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            kind = entry_kind(full)
            if kind == KIND_LINK:
                manifest[rel] = (KIND_LINK, 0, os.readlink(full))
            elif kind == KIND_DIR:
                manifest[rel] = (KIND_DIR, 0, "")
            elif kind == KIND_FILE:
                manifest[rel] = (KIND_FILE, os.lstat(full).st_size, _sha256(full))
            elif kind == KIND_SPECIAL:
                skipped.append(rel)
    return manifest, skipped


def _raise(exc: OSError) -> None:
    raise exc


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(src: str, dst: str, on_progress: Optional[Callable[[str], None]] = None) -> List[str]:
    """把 src 复制到尚不存在的 dst，返回跳过的特殊文件相对路径。

    与 shutil.copytree 的区别：遇到 socket、FIFO、设备文件时跳过而不是报错
    （copytree 会抛 `Operation not supported on socket` 并留下半份目标）。
    软链按软链本身复制；普通文件用 copy2 保留权限位和时间戳。
    目录权限在内容复制完后再设置，避免只读目录挡住后续写入。
    on_progress 在每个普通文件复制后调用一次，供测试在复制中途注入中断。
    """
    skipped: List[str] = []
    dir_modes: List[Tuple[str, str]] = []
    os.makedirs(dst)
    dir_modes.append((src, dst))
    for dirpath, dirnames, filenames in os.walk(src, onerror=_raise, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src)
        target_dir = dst if rel_dir == "." else os.path.join(dst, rel_dir)
        for name in list(dirnames) + list(filenames):
            full = os.path.join(dirpath, name)
            target = os.path.join(target_dir, name)
            kind = entry_kind(full)
            if kind == KIND_LINK:
                os.symlink(os.readlink(full), target)
            elif kind == KIND_DIR:
                os.mkdir(target)
                dir_modes.append((full, target))
            elif kind == KIND_FILE:
                shutil.copy2(full, target, follow_symlinks=False)
                if on_progress is not None:
                    on_progress(target)
            elif kind == KIND_SPECIAL:
                skipped.append(os.path.relpath(full, src))
    for source_dir, target_dir in reversed(dir_modes):
        shutil.copystat(source_dir, target_dir, follow_symlinks=False)
    return skipped


def diff_manifests(expected: Dict[str, ManifestEntry], actual: Dict[str, ManifestEntry]) -> List[str]:
    """返回两份清单不一致的相对路径（排序后），空列表表示完全一致。"""
    mismatched = [rel for rel in expected if actual.get(rel) != expected[rel]]
    mismatched.extend(rel for rel in actual if rel not in expected)
    return sorted(set(mismatched))
