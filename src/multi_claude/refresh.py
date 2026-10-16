"""用刷新令牌换新的访问令牌并写回凭据，供 `list` 在访问令牌过期时使用（方案 feature-token-refresh）。

这是整个工具里唯一会改动登录凭据的流程，做法与 Claude Code 自己的刷新保持兼容：

- 同一把锁：先取 `<配置目录>/.oauth_refresh.lock`，再取 `<配置目录真实路径>.lock`，都是用 mkdir 建的目录
  （Claude Code 用的 proper-lockfile 就是这样加锁）。锁被占用时直接放弃，不等待、不删除别人的锁。
- 锁内重读：别的进程刚刷新过就直接用它的令牌，不再发请求。
- 写回前比对：保存着的刷新令牌还是发出去的那个（或已被清空）才写，否则说明别人写过，放弃写入。
- 整份保留：只更新令牌相关的几个字段，凭据里的其它内容原样带回。

刷新请求一旦发出，服务端就可能已经换掉了刷新令牌；新令牌没写回，这个账号就得重新登录。
所以调用方必须保证：从 on_commit() 被调用到 renew() 返回之间，进程不提前退出。

本模块不联网：请求由调用方传入的 post() 发出（经账号的代理）。令牌不进入日志、异常或返回的原因里。
"""

import json
import math
import os
import threading
import time
from typing import Callable, List, NamedTuple, Optional, Tuple

from . import identity

# Claude Code 的 OAuth 客户端标识；凭据里存有 clientId 时以凭据为准。
DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
LOCK_NAME = ".oauth_refresh.lock"
# 持锁期间更新锁目录修改时间的间隔，与 proper-lockfile 的 update 相同；
# 修改时间超过 60 秒没更新，别的进程会把锁当作过期删掉。
LOCK_TOUCH_INTERVAL_S = 5.0
# 修改时间超过这么久没更新的锁视为没有主的残留，可以清掉（活着的持有者每 5 秒更新一次）。
DEAD_LOCK_AGE_S = 300.0
# 写回后读不到新令牌时重写的次数，与 Claude Code 相同。
SAVE_ATTEMPTS = 3

REFRESHED = "refreshed"
ADOPTED = "adopted"
SAVE_FAILED = "save_failed"
LOCK_BUSY = "lock_busy"
LOCK_ERROR = "lock_error"
NO_REFRESH_TOKEN = "no_refresh_token"
CREDENTIALS_UNREADABLE = "credentials_unreadable"
CREDENTIALS_CHANGED = "credentials_changed"
REFRESH_REJECTED = "refresh_rejected"
MALFORMED = "malformed_response"
ABANDONED = "abandoned"

# 拿到了可用令牌的结果；其余都表示访问令牌仍然是过期的。
USABLE = (REFRESHED, ADOPTED, SAVE_FAILED)
# 只有重新登录才能解决的结果。
NEEDS_LOGIN = (REFRESH_REJECTED, NO_REFRESH_TOKEN)

# post(请求体) -> (传输失败原因或 None, HTTP 状态码, 响应体)
Post = Callable[[dict], Tuple[Optional[str], int, bytes]]


class Outcome(NamedTuple):
    status: str
    # 仅 status 属于 USABLE 时非空
    access_token: str = ""
    expires_at_ms: Optional[int] = None


def renew(account_identity: str, account_dir: str, config_dir: str, now_ms: int, post: Post,
          on_commit: Callable[[], bool]) -> Outcome:
    """刷新一个账号的访问令牌。任何失败都体现在返回值里，不抛异常、不清空凭据。

    config_dir 是 Claude Code 眼里的配置目录（锁建在这里）：dir 身份是账号目录，default 身份是 ~/.claude。
    on_commit() 在发出刷新请求之前调用一次：返回 False 表示调用方已经不等了，本函数不发请求；
    返回 True 后，调用方必须等本函数返回。
    """
    if not os.path.isdir(config_dir):
        return Outcome(NO_REFRESH_TOKEN)
    locks, problem = _acquire(config_dir)
    if locks is None:
        return Outcome(problem)
    stop_touching = threading.Event()
    toucher = threading.Thread(target=_keep_alive, args=(locks, stop_touching), daemon=True)
    toucher.start()
    try:
        return _renew_locked(account_identity, account_dir, now_ms, post, on_commit)
    finally:
        stop_touching.set()
        # 先等保活线程停下再放锁：否则它可能在放锁之后再碰一次这个路径，而那时锁可能已经是别人的了。
        toucher.join()
        _release(locks)


def _renew_locked(account_identity: str, account_dir: str, now_ms: int, post: Post,
                  on_commit: Callable[[], bool]) -> Outcome:
    credentials = identity.read_credentials(account_identity, account_dir)
    if credentials.state == identity.TOKEN_UNREADABLE:
        return Outcome(CREDENTIALS_UNREADABLE)
    if credentials.state != identity.TOKEN_OK:
        return Outcome(NO_REFRESH_TOKEN)
    current = _usable_token(credentials.document, now_ms)
    if current is not None:
        # 等锁、读凭据的这段时间里别的进程已经刷新过：直接用，不要再把刷新令牌用一次。
        return Outcome(ADOPTED, current.access_token, current.expires_at_ms)
    oauth = credentials.document["claudeAiOauth"]
    posted = oauth.get("refreshToken")
    if not identity.is_token(posted):
        return Outcome(NO_REFRESH_TOKEN)
    body = {"grant_type": "refresh_token", "refresh_token": posted,
            "client_id": oauth.get("clientId") if identity.is_token(oauth.get("clientId")) else DEFAULT_CLIENT_ID}
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and scopes and all(isinstance(scope, str) for scope in scopes):
        # 用凭据里保存的范围，即 Claude Code 在默认范围被拒绝时的回退做法：不会缩小、也不会扩大授权。
        body["scope"] = " ".join(scopes)
    if not on_commit():
        # 调用方已经不等这个账号了：现在发请求，新令牌可能来不及写回，所以不发。
        return Outcome(ABANDONED)
    reason, status, raw = post(body)
    if reason is not None:
        return Outcome(reason)
    data = _json_object(raw)
    if status in (400, 401) and _error_code(data) == "invalid_grant":
        # 刷新令牌已失效。Claude Code 此时会清空凭据；这里不动凭据，只让用户重新登录。
        return Outcome(REFRESH_REJECTED)
    if status != 200:
        return Outcome("http_{}".format(status))
    access_token = data.get("access_token")
    if not identity.is_token(access_token):
        return Outcome(MALFORMED)
    # 到这里服务端已经发了新令牌，旧的刷新令牌多半已作废：无论其余字段长什么样，都必须把新令牌存下来。
    # 所以下面只做降级，不再因为次要字段不合预期而放弃。
    refresh_token = data.get("refresh_token")
    if not identity.is_token(refresh_token):
        refresh_token = posted
    received_ms = int(time.time() * 1000)
    expires_in_s = _seconds(data.get("expires_in"))
    # 读不出有效期时按“已经过期”保存：Claude Code 下次启动会用新的刷新令牌自己再刷一次。
    expires_at_ms = received_ms + int((expires_in_s or 0) * 1000)
    refresh_expires_in_s = _seconds(data.get("refresh_token_expires_in"))
    scope = data.get("scope")
    scopes = [part for part in scope.split(" ") if part] if isinstance(scope, str) else None
    for attempt in range(SAVE_ATTEMPTS):
        if attempt:
            time.sleep(0.1 * attempt)
        try:
            saved = _save(account_identity, account_dir, now_ms, posted, access_token, refresh_token,
                          expires_at_ms, None if refresh_expires_in_s is None
                          else received_ms + int(refresh_expires_in_s * 1000), scopes)
        except Exception:  # 一次尝试里的意外不能让剩下的重试落空
            saved = None
        if saved is not None:
            return saved
    # 新令牌本次还能用；写不回去的后果由调用方告诉用户。
    return Outcome(SAVE_FAILED, access_token, expires_at_ms)


def _save(account_identity: str, account_dir: str, now_ms: int, posted: str, access_token: str,
          refresh_token: str, expires_at_ms: int, refresh_expires_at_ms: Optional[int],
          scopes: Optional[List[str]]) -> Optional[Outcome]:
    """比对后写回一次，并读回确认；返回最终结果，或 None 表示这一次没写成、可以重试。"""
    latest = identity.read_credentials(account_identity, account_dir)
    if latest.state != identity.TOKEN_OK:
        return None
    stored = latest.document["claudeAiOauth"]
    if stored.get("accessToken") == access_token:
        # 上一次写入其实成功了，只是当时没读回来
        return Outcome(REFRESHED, access_token, expires_at_ms)
    if stored.get("refreshToken") not in (posted, ""):
        # 别的进程在我们发请求期间写过凭据：以它为准，绝不覆盖。
        theirs = _usable_token(latest.document, now_ms)
        if theirs is not None:
            return Outcome(ADOPTED, theirs.access_token, theirs.expires_at_ms)
        return Outcome(CREDENTIALS_CHANGED)
    updated = dict(stored, accessToken=access_token, refreshToken=refresh_token, expiresAt=expires_at_ms)
    if refresh_expires_at_ms is not None:
        updated["refreshTokenExpiresAt"] = refresh_expires_at_ms
    if scopes:
        updated["scopes"] = scopes
    document = dict(latest.document, claudeAiOauth=updated)
    if not identity.write_credentials(account_identity, account_dir, latest.source, document):
        return None
    check = identity.read_credentials(account_identity, account_dir)
    if check.state == identity.TOKEN_OK and check.document["claudeAiOauth"].get("accessToken") == access_token:
        return Outcome(REFRESHED, access_token, expires_at_ms)
    return None


def _usable_token(document: dict, now_ms: int) -> Optional[identity.OAuthToken]:
    token = identity.token_of(document)
    if token is None or (token.expires_at_ms is not None and token.expires_at_ms <= now_ms):
        return None
    return token


def _json_object(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error_code(data: dict) -> Optional[str]:
    """响应体里的错误码：OAuth 标准写法是字符串 `error`，也兼容对象形式里的 code / type。"""
    error = data.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for key in ("code", "type"):
            if isinstance(error.get(key), str):
                return error[key]
    return None


def _seconds(value: object) -> Optional[float]:
    """把响应里的秒数读成正的有限数；数字形式的字符串也接受（Claude Code 对它直接做乘法，同样能用）。"""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # 上限一百年：挡住非有限值与离谱的大数，后面要乘 1000 取整
    return float(value) if 0 < value <= 3153600000 and math.isfinite(value) else None


def lock_paths(config_dir: str) -> List[str]:
    """新旧两把锁的路径，顺序即加锁顺序。旧锁建在配置目录真实路径的旁边，老版本的 Claude Code 只认它。"""
    return [os.path.join(config_dir, LOCK_NAME), os.path.realpath(config_dir) + ".lock"]


class _Lock(object):
    """自己建的一把锁，以及用来确认“这个路径上还是我建的那个目录”的指纹。

    指纹是设备号、inode 和自己最后一次设置的修改时间（proper-lockfile 也靠修改时间判断锁有没有被别人动过）。
    只比 inode 不够：别人删掉重建时文件系统可能复用同一个编号。修改时间也不能交给内核取：
    ext4 上几毫秒内删掉重建的目录，inode 和内核记的修改时间多半与原来相同（实测 2000 次里 1993 次），会把别人的锁认成自己的。
    所以建锁和保活时都用 _set_mtime_now 写入此刻的精细时间。
    不能改成往锁目录里放标记文件：proper-lockfile 用 rmdir 清理过期锁，目录非空就删不掉，锁会一直卡住。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        try:
            self._set_mtime_now()
        except OSError:
            # 设不了时间就用内核写的时间记指纹：锁照样能用，只是分辨不出同一时钟节拍里的删掉重建。
            pass
        self._stamp = self._read()

    def _set_mtime_now(self) -> None:
        now_ns = time.time_ns()
        os.utime(self.path, ns=(now_ns, now_ns))

    def _read(self) -> Optional[Tuple[int, int, int]]:
        try:
            status = os.lstat(self.path)
        except OSError:
            return None
        return status.st_dev, status.st_ino, status.st_mtime_ns

    def is_mine(self) -> bool:
        return self._stamp is not None and self._read() == self._stamp

    def touch(self) -> None:
        """保活：把修改时间更新为现在。锁已经不是自己的就不碰。"""
        if not self.is_mine():
            return
        try:
            # 不能用 os.utime(path, None)：那样又由内核取时间，保活一次后指纹就退回分辨不出重建的状态。
            self._set_mtime_now()
        except OSError:
            return
        fresh = self._read()
        # 只有还是同一个目录（设备号与 inode 没变）才把新的修改时间记成自己的：
        # 核对与更新之间锁如果被别人换掉，不能把别人的锁认成自己的，否则放锁时会删掉它。
        if fresh is not None and self._stamp is not None and fresh[:2] == self._stamp[:2]:
            self._stamp = fresh

    def remove(self) -> None:
        # 进程被挂起很久（合盖休眠）后，这把锁可能已被别人当作过期锁删掉并重建：那就不是自己的了，不能删。
        if not self.is_mine():
            return
        try:
            os.rmdir(self.path)
        except OSError:
            pass


def _acquire(config_dir: str) -> Tuple[Optional[List[_Lock]], str]:
    """取两把锁；返回 (锁列表, "")，取不到时返回 (None, 原因)。不等待。"""
    held: List[_Lock] = []
    for path in lock_paths(config_dir):
        lock, problem = _make_lock(path)
        if lock is None:
            # 只放掉自己刚取到的，别人的锁不碰。
            _release(held)
            return None, problem
        held.append(lock)
    return held, ""


def _make_lock(path: str) -> Tuple[Optional[_Lock], str]:
    for attempt in range(2):
        try:
            os.mkdir(path)
        except FileExistsError:
            if attempt or not _remove_if_dead(path):
                return None, LOCK_BUSY
            continue
        except OSError:
            # 目录只读、没有权限等：不是被占用，是这里根本建不了锁
            return None, LOCK_ERROR
        return _Lock(path), ""
    return None, LOCK_BUSY


def _remove_if_dead(path: str) -> bool:
    """锁目录的修改时间很久没更新时把它删掉，返回是否删掉了。

    活着的持有者每 5 秒更新一次修改时间，proper-lockfile 以 60 秒为过期线；这里用更宽的线，
    只清理确定没有主的残留（例如本工具被强杀后留下的），宁可多报几次 lock_busy。
    """
    try:
        age_s = time.time() - os.lstat(path).st_mtime
        if age_s <= DEAD_LOCK_AGE_S:
            return False
        os.rmdir(path)
    except OSError:
        return False
    return True


def _release(locks: List[_Lock]) -> None:
    for lock in reversed(locks):
        lock.remove()


def _keep_alive(locks: List[_Lock], stop: threading.Event) -> None:
    while not stop.wait(LOCK_TOUCH_INTERVAL_S):
        for lock in locks:
            lock.touch()
