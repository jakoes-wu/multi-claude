"""凭据定位与只读的登录状态探测（方案 §5.1.9）。

Claude Code 在 macOS 上把登录凭据存进钥匙串，服务名由配置目录的路径字符串决定
（方案 §3 依据 2）；Linux 上存进配置目录里的 `.credentials.json`（依据 3）。
本模块只计算“凭据应该在哪里”，并查询它是否存在：

- 绝不读取、复制或删除凭据内容；
- 钥匙串只调用 `security find-generic-password -a <账户> -s <服务名>`，不带 -w / -g，
  只看退出码。整个工具只有这一处调用 `security`，测试会断言它收到的参数。
"""

import hashlib
import os
import pwd
import re
import subprocess
import sys
import unicodedata

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
