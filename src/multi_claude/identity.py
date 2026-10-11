"""凭据定位、只读的登录状态探测，以及访问令牌的读取（方案 §5.1.9、feature-live-plan §5.1.1）。

Claude Code 在 macOS 上把登录凭据存进钥匙串，服务名由配置目录的路径字符串决定
（方案 §3 依据 2）；Linux 上存进配置目录里的 `.credentials.json`（依据 3）。
本模块计算“凭据应该在哪里”，并提供两种只读访问：

- `probe()`：只查询凭据是否存在。钥匙串调用 `security find-generic-password -a <账户> -s <服务名>`，
  不带 -w / -g，只看退出码。
- `read_oauth_token()`：读出访问令牌，供 `list` 向服务端查询套餐（plan.py 是唯一调用方）。
  钥匙串调用带 -w 的同一条命令。令牌只留在内存里，不写入文件、日志或任何输出。

任何情况下都不写入、复制或删除凭据。整个工具只有这两处调用 `security`，测试会断言它们收到的参数。
"""

import hashlib
import json
import math
import os
import pwd
import re
import subprocess
import sys
import unicodedata
from typing import NamedTuple, Optional, Tuple

from .config import IDENTITY_DEFAULT

SERVICE_BASE = "Claude Code-credentials"
CREDENTIALS_FILE = ".credentials.json"

# probe() 的返回值
LOGIN_KEYCHAIN = "keychain"
LOGIN_FILE = "file"
LOGIN_NONE = "none"
LOGIN_UNKNOWN = "unknown"

# security 的退出码 44 = errSecItemNotFound（条目不存在）
_SECURITY_NOT_FOUND = 44
_KEYCHAIN_ACCOUNT_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


def service_name(identity: str, account_dir: str) -> str:
    """该账号在 macOS 钥匙串里的服务名。

    default 身份不设 CLAUDE_CONFIG_DIR，Claude 用不带后缀的服务名；dir 身份的后缀是
    启动命令里那串路径（先做 NFC 规范化，与 Claude 的 we() 一致）的 SHA-256 前 8 位。
    这里假定正式环境（OAUTH_FILE_SUFFIX 为空）：设置了 CLAUDE_CODE_CUSTOM_OAUTH_URL 时
    服务名中段会多出 `-custom-oauth`，调用方负责提示探测结果可能不准。
    """
    if identity == IDENTITY_DEFAULT:
        return SERVICE_BASE
    normalized = unicodedata.normalize("NFC", account_dir)
    suffix = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return "{}-{}".format(SERVICE_BASE, suffix)


def keychain_account() -> str:
    """与 Claude 的 ok() 相同：取 USER，缺失时取当前 uid 的用户名；不合规则时用 claude-code-user。"""
    user = os.environ.get("USER")
    if not user:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            user = ""
    if not _KEYCHAIN_ACCOUNT_PATTERN.match(user):
        return "claude-code-user"
    return user


def credentials_file(account_dir: str) -> str:
    return os.path.join(account_dir, CREDENTIALS_FILE)


def probe(identity: str, account_dir: str) -> str:
    """返回登录凭据所在的位置：keychain、file、none 或 unknown（查询本身失败）。

    macOS 先查钥匙串，再查凭据文件（钥匙串写入失败时 Claude 会退回到文件）；
    其它平台只查凭据文件。只用于提示，不参与任何写操作的判定。
    """
    keychain_state = LOGIN_NONE
    if sys.platform == "darwin":
        keychain_state = _probe_keychain(service_name(identity, account_dir))
        if keychain_state == LOGIN_KEYCHAIN:
            return LOGIN_KEYCHAIN
    if os.path.isfile(credentials_file(account_dir)):
        return LOGIN_FILE
    return keychain_state


def _probe_keychain(service: str) -> str:
    try:
        proc = subprocess.run(["security", "find-generic-password", "-a", keychain_account(), "-s", service],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return LOGIN_UNKNOWN
    if proc.returncode == 0:
        return LOGIN_KEYCHAIN
    if proc.returncode == _SECURITY_NOT_FOUND:
        return LOGIN_NONE
    return LOGIN_UNKNOWN


# read_oauth_token() 的 state
TOKEN_OK = "ok"
TOKEN_NONE = "none"
TOKEN_UNREADABLE = "unreadable"

# 访问令牌要原样放进 HTTP 请求头：只接受可见 ASCII，含空白或控制字符的值会破坏请求头。
_TOKEN_PATTERN = re.compile(r"[\x21-\x7e]+")


class OAuthToken(NamedTuple):
    state: str
    # 仅 state == TOKEN_OK 时非空
    access_token: str
    # 过期时刻，毫秒时间戳（Claude Code 写入的 claudeAiOauth.expiresAt）；凭据里没有该字段时为 None
    expires_at_ms: Optional[int]


def read_oauth_token(identity: str, account_dir: str) -> OAuthToken:
    """读出该账号当前登录的访问令牌。只读，不刷新、不写回。

    来源顺序与 probe() 相同：macOS 先钥匙串、后凭据文件；其它平台只有凭据文件。
    TOKEN_NONE 表示没有可用的 OAuth 登录（没登录，或只用 API key）；TOKEN_UNREADABLE 表示
    凭据在但读不出来（钥匙串被锁、命令超时、文件打不开、内容不是 JSON）。
    调用方不得把返回的令牌写进日志、异常信息或输出。
    """
    states = []
    if sys.platform == "darwin":
        state, token = _parse_credentials(*_read_keychain_secret(service_name(identity, account_dir)))
        if token is not None:
            return token
        states.append(state)
    state, token = _parse_credentials(*_read_credentials_file(credentials_file(account_dir)))
    if token is not None:
        return token
    states.append(state)
    # 有一个来源读不出来时不能报“没登录”：令牌可能就在那个来源里。
    final = TOKEN_UNREADABLE if TOKEN_UNREADABLE in states else TOKEN_NONE
    return OAuthToken(final, "", None)


def _read_keychain_secret(service: str) -> Tuple[str, Optional[str]]:
    """返回 (状态, 条目内容)。与 Claude 自己的读取命令相同；stderr 丢弃，内容只进内存。"""
    try:
        proc = subprocess.run(["security", "find-generic-password", "-a", keychain_account(), "-w", "-s", service],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return TOKEN_UNREADABLE, None
    if proc.returncode == _SECURITY_NOT_FOUND:
        return TOKEN_NONE, None
    if proc.returncode != 0:
        return TOKEN_UNREADABLE, None
    return TOKEN_OK, proc.stdout.decode("utf-8", "replace")


def _read_credentials_file(path: str) -> Tuple[str, Optional[str]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return TOKEN_OK, handle.read()
    except FileNotFoundError:
        return TOKEN_NONE, None
    except OSError:
        return TOKEN_UNREADABLE, None


def _parse_credentials(state: str, text: Optional[str]) -> Tuple[str, Optional[OAuthToken]]:
    """从凭据 JSON 里取访问令牌；返回 (该来源的状态, 令牌或 None)。"""
    if text is None:
        return state, None
    try:
        data = json.loads(text)
    except ValueError:
        return TOKEN_UNREADABLE, None
    if not isinstance(data, dict):
        return TOKEN_UNREADABLE, None
    oauth = data.get("claudeAiOauth")
    access_token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    # fullmatch 而不是 match：`$` 会放过尾部换行
    if not isinstance(access_token, str) or not _TOKEN_PATTERN.fullmatch(access_token):
        return TOKEN_NONE, None
    expires_at = oauth.get("expiresAt")
    # bool 是 int 的子类，要先排除
    # JSON 里的 1e999 会解析成 inf，取整会抛异常：非有限值同样当作“没有过期时刻”
    # （只对浮点数判断：超出浮点范围的整数交给 isfinite 会抛 OverflowError，而整数本身总是有限的）
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) \
            or (isinstance(expires_at, float) and not math.isfinite(expires_at)):
        expires_at = None
    return TOKEN_OK, OAuthToken(TOKEN_OK, access_token, None if expires_at is None else int(expires_at))
