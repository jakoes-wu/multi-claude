"""共享资源链接的计划（方案 §5.1.10，沿用 multi-codex §5.1.6）。

每个账号可以把 CLAUDE.md、skills 等条目软链到同一个共享目录。
判断“是不是本工具建的链接”只看 Account.managed_links：链接本身带不了受管标记，
而用户很可能早就手工建过指向同一共享目录的链接，这类链接关闭共享时不能删。
"""

import os
from typing import List, Optional

from . import platform
from .actions import CONFLICT, CREATE, DELETE, SKIP, UNCHANGED, UPDATE, Action
from .config import Account, Config
from .fsutil import KIND_LINK, KIND_MISSING, entry_kind, expand, same_target


def plan_shared(new: Config, account: Account, account_dir: str,
                old_shared_dir: Optional[str], adopt: bool = False) -> List[Action]:
    """为一个账号生成共享链接动作，并就地更新 account.managed_links。

    adopt 为真时（`add --adopt`），已经指向对应共享条目、但不在名单里的软链会被写进名单（接管），
    链接本身不动。只接管“指向正确”的软链：指向别处的软链和真实目录照旧判冲突，
    以免把用户另有用途的东西交给工具，之后关闭共享时被删掉。

    old_shared_dir 是变更前的共享目录：修改 shared.dir 后，
    仍指向旧目录的受管链接需要改指向新目录，而不是被当成“指向别处”的冲突。
    """
    actions: List[Action] = []
    desired = {}
    if account.shared:
        if not new.shared_dir:
            return [Action(CONFLICT, "shared-link", account_dir,
                           "shared.dir is not configured; run `multi-claude init --shared-dir DIR`")]
        shared_root = expand(new.shared_dir)
        for item in new.shared_items:
            source = os.path.join(shared_root, item)
            if entry_kind(source) == KIND_MISSING:
                actions.append(Action(SKIP, "shared-link", os.path.join(account_dir, item),
                                      "not present in shared dir {}".format(shared_root)))
                continue
            desired[item] = source

    managed = set(account.managed_links)
    old_root = expand(old_shared_dir) if old_shared_dir else None
    kept: List[str] = []

    for item, source in desired.items():
        link = os.path.join(account_dir, item)
        kind = entry_kind(link)
        if kind == KIND_MISSING:
            actions.append(Action(CREATE, "shared-link", link, "-> " + source,
                                  _create_link(source, link)))
            kept.append(item)
        elif kind == KIND_LINK and same_target(link, source):
            if item in managed:
                actions.append(Action(UNCHANGED, "shared-link", link))
                kept.append(item)
            elif adopt:
                # 没有文件操作：接管只体现在 config.json 的 managed_links 里。
                actions.append(Action(UPDATE, "shared-link", link, "adopted"))
                kept.append(item)
            else:
                actions.append(Action(UNCHANGED, "shared-link", link))
        elif kind == KIND_LINK and item in managed and _points_to(link, old_root, item):
            actions.append(Action(UPDATE, "shared-link", link, "-> " + source,
                                  _replace_link(source, link)))
            kept.append(item)
        else:
            reason = "a link to another location" if kind == KIND_LINK else "a real {}".format(
                "directory" if kind == "dir" else "file")
            actions.append(Action(CONFLICT, "shared-link", link,
                                  "already exists as {}".format(reason)))

    for item in account.managed_links:
        if item in desired:
            continue
        link = os.path.join(account_dir, item)
        kind = entry_kind(link)
        if kind == KIND_MISSING:
            continue
        if kind == KIND_LINK and (_points_to(link, old_root, item) or
                                  _points_to(link, expand(new.shared_dir) if new.shared_dir else None, item)):
            actions.append(Action(DELETE, "shared-link", link, "sharing disabled for this item",
                                  _unlink(link)))
        else:
            # 用户已把它换成了别的东西，不再归本工具管理；从名单里移除，但不动它。
            actions.append(Action(SKIP, "shared-link", link,
                                  "no longer the link multi-claude created; left untouched"))

    # 保持与 shared_items 相同的顺序，便于人工阅读 config.json。
    order = {item: index for index, item in enumerate(new.shared_items)}
    account.managed_links = sorted(set(kept), key=lambda item: order.get(item, len(order)))
    return actions


def plan_remove_links(account: Account, account_dir: str, shared_dir: Optional[str]) -> List[Action]:
    """账号被注销时，删除本工具建立、且仍指向共享目录的链接。"""
    actions: List[Action] = []
    root = expand(shared_dir) if shared_dir else None
    for item in account.managed_links:
        link = os.path.join(account_dir, item)
        if entry_kind(link) == KIND_LINK and _points_to(link, root, item):
            actions.append(Action(DELETE, "shared-link", link, "account removed", _unlink(link)))
    return actions


def _points_to(link: str, shared_root: Optional[str], item: str) -> bool:
    return shared_root is not None and same_target(link, os.path.join(shared_root, item))


def _create_link(source: str, link: str):
    def run() -> None:
        platform.create_link(source, link)
    return run


def _replace_link(source: str, link: str):
    def run() -> None:
        os.unlink(link)
        platform.create_link(source, link)
    return run


def _unlink(link: str):
    def run() -> None:
        os.unlink(link)
    return run
