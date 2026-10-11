"""向服务端实时查询各账号的套餐与付款渠道，供 `list` 显示（方案 feature-live-plan）。

本模块是整个工具里唯一联网、唯一使用访问令牌的地方，只有 `list` 调用它：

- 每个账号用自己的访问令牌请求 `GET /api/oauth/profile`，与 Claude Code 自己查询套餐的接口相同。
  只读：不刷新令牌，不写回凭据，过期的令牌不会被发出去。
- 出站路径与启动命令 `claude-<名称>` 一致：代理取自 `env.account_env()` 构造的账号环境。
  配置了代理时，任何失败都不会改走直连。
- 不用 urllib 的 ProxyHandler：没有代理环境变量时它会读 macOS 系统代理的例外清单，
  可能把请求悄悄改成直连。这里直接用 http.client 建 CONNECT 隧道，路径完全由本模块决定。
- 令牌、响应体原文、异常文本都不进入返回值与输出；失败只归类成固定的 reason。
  服务端返回的字段先过格式校验，再进入终端输出。

线程模型：`query_all()` 为每个账号起一个守护线程，各线程只写自己那一项结果，互不共享其它可变状态。
"""

import base64
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Dict, Mapping, NamedTuple, Optional

from . import __version__, accounts, identity, platform
from .config import Account, Config
from .env import account_env

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# 测试钩子：把查询目标换成本地假服务；只有 MULTI_CLAUDE_TEST_MODE=1 时生效。
TEST_URL_HOOK = "MULTI_CLAUDE_TEST_PROFILE_URL"
# 测试钩子：同时替换套接字超时与总时限（秒），供超时用例使用。
TEST_TIMEOUT_HOOK = "MULTI_CLAUDE_TEST_PLAN_TIMEOUT"

# 单次套接字操作（连接、握手、读响应）的超时，与 Claude Code 自己查询该接口的 10 秒一致。
SOCKET_TIMEOUT_S = 10.0
# 所有账号并发查询的总时限；到时仍未返回的账号记为 timeout，list 不再等。
TOTAL_TIMEOUT_S = 15.0
# 连接类失败（代理或对端中途断开）重试的次数与间隔。查询是只读的 GET，重发没有副作用；
# 本机实测 5 个账号同时经代理建连时偶有连接在握手阶段被代理掐断，隔一会儿再连即成功。
# 超时不重试：再等一轮会超过总时限。
CONNECT_RETRIES = 1
RETRY_DELAY_S = 0.5
# 实测响应体不到 1 KB；超过这个长度的不是预期的响应，不解析。
MAX_BODY_BYTES = 65536

STATUS_OK = "ok"
STATUS_NO_LOGIN = "no_login"
STATUS_EXPIRED = "expired"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

REASON_CREDENTIALS = "credentials_unreadable"
REASON_PROXY = "proxy_unsupported"
REASON_TEST_NO_ENDPOINT = "test_no_endpoint"
REASON_TIMEOUT = "timeout"
REASON_TLS = "tls_failed"
REASON_CONNECT = "connect_failed"
REASON_MALFORMED = "malformed_response"
# 本模块自己的意外错误（不是对端的问题）；出现即说明有没覆盖到的输入，值得报告
REASON_INTERNAL = "internal_error"

ROUTE_DIRECT = "direct"

# Claude Code 按这个顺序取代理变量（方案 feature-account-manager §3 依据 5）。
_PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")
_NO_PROXY_VARS = ("no_proxy", "NO_PROXY")

# 服务端字段的格式：与 Claude Code 校验 rate_limit_tier 的规则相同。不匹配的值一律丢弃，
# 避免把任意字符串（含终端控制序列）打印到用户终端。
_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MULTIPLIER_PATTERN = re.compile(r"_(\d+)x$")
# organization_type 到套餐名的映射，取自 Claude Code。
_PLAN_NAMES = {"claude_max": "max", "claude_pro": "pro", "claude_team": "team", "claude_enterprise": "enterprise"}


class PlanResult(NamedTuple):
    status: str
    # 以下三项仅 status == STATUS_OK 时可能非空，值已通过 _FIELD_PATTERN 校验
    organization_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    billing_type: Optional[str] = None
    # 仅 status == STATUS_ERROR 时非空，取值是上面的 REASON_* 或 http_<状态码>
    reason: Optional[str] = None
    # 发过请求时为 ROUTE_DIRECT 或代理的 "主机:端口"（不含用户名密码），否则为 None
    route: Optional[str] = None
    # 发过请求时的耗时（秒），含重试
    elapsed_s: Optional[float] = None
    # 实际发起的连接次数；重试过时为 2
    attempts: int = 0


class _Proxy(NamedTuple):
    host: str
    port: int
    # Proxy-Authorization 的值；代理地址不带用户名密码时为 None
    authorization: Optional[str]


def query_all(config: Config, base_env: Mapping[str, str], now: datetime) -> Dict[str, PlanResult]:
    """并发查询全部账号，返回 {账号名: 结果}。总耗时不超过总时限，任何失败都体现在结果里，不抛异常。"""
    timeout = _timeout_override()
    socket_timeout = SOCKET_TIMEOUT_S if timeout is None else timeout
    total_timeout = TOTAL_TIMEOUT_S if timeout is None else timeout
    now_ms = int(now.timestamp() * 1000)
    results: Dict[str, PlanResult] = {}
    threads = []
    for account in config.accounts.values():
        # 守护线程：总时限到了以后进程可以直接退出，不会被卡住的连接拖住。
        thread = threading.Thread(target=_query_into, name="plan-" + account.name, daemon=True,
                                  args=(results, config, account, base_env, now_ms, socket_timeout))
        thread.start()
        threads.append((account.name, thread))
    deadline = time.monotonic() + total_timeout
    final: Dict[str, PlanResult] = {}
    for name, thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
        # 超时的线程之后即使写入 results 也不再采用：这里取到什么就是什么。
        final[name] = results.get(name) or PlanResult(STATUS_ERROR, reason=REASON_TIMEOUT)
    return final


def _query_into(results: Dict[str, PlanResult], config: Config, account: Account, base_env: Mapping[str, str],
                now_ms: int, socket_timeout: float) -> None:
    try:
        results[account.name] = query(config, account, base_env, now_ms, socket_timeout)
    except Exception:  # 线程里的意外错误不能让 list 整体失败，也不能把异常文本打到终端
        results[account.name] = PlanResult(STATUS_ERROR, reason=REASON_INTERNAL)


def query(config: Config, account: Account, base_env: Mapping[str, str], now_ms: int,
          socket_timeout: float = SOCKET_TIMEOUT_S) -> PlanResult:
    """查询一个账号。先在本地排除不需要、不应该发请求的情况，再发请求；只有连接类失败会重试一次。"""
    token = identity.read_oauth_token(account.identity, accounts.account_dir(config, account.name))
    if token.state == identity.TOKEN_NONE:
        return PlanResult(STATUS_NO_LOGIN)
    if token.state != identity.TOKEN_OK:
        return PlanResult(STATUS_ERROR, reason=REASON_CREDENTIALS)
    if token.expires_at_ms is not None and token.expires_at_ms <= now_ms:
        # 过期的令牌服务端必然拒绝（实测 401），没有必要把它发出去；续期交给 Claude Code 自己。
        return PlanResult(STATUS_EXPIRED)
    url = _profile_url()
    if url is None:
        return PlanResult(STATUS_ERROR, reason=REASON_TEST_NO_ENDPOINT)
    target = urllib.parse.urlsplit(url)
    target_port = target.port or (443 if target.scheme == "https" else 80)
    try:
        proxy = _resolve_proxy(account_env(config, account, base_env), target.hostname or "")
    except ValueError:
        # 代理配置了但用不了：按失败处理，绝不退回直连。
        return PlanResult(STATUS_ERROR, reason=REASON_PROXY)
    route = ROUTE_DIRECT if proxy is None else "{}:{}".format(proxy.host, proxy.port)
    started = time.monotonic()
    outcome = _fetch(target, target_port, proxy, token.access_token, socket_timeout)
    attempts = 1
    while outcome.reason == REASON_CONNECT and attempts <= CONNECT_RETRIES:
        time.sleep(RETRY_DELAY_S)
        # 重试走同一条路径（同一个代理或同样直连），不因为失败而换路。
        outcome = _fetch(target, target_port, proxy, token.access_token, socket_timeout)
        attempts += 1
    elapsed = round(time.monotonic() - started, 2)
    return outcome._replace(route=route, elapsed_s=elapsed, attempts=attempts)


def _profile_url() -> Optional[str]:
    """查询地址。测试模式下必须显式给出本地地址，否则不发请求：测试套件永远不能连到真实服务。"""
    if platform.test_hook_value(platform.TEST_MODE_ENV) is None:
        return PROFILE_URL
    return platform.test_hook_value(TEST_URL_HOOK) or None


def _timeout_override() -> Optional[float]:
    value = platform.test_hook_value(TEST_TIMEOUT_HOOK)
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _resolve_proxy(env: Mapping[str, str], host: str) -> Optional[_Proxy]:
    """按 Claude Code 的规则从账号环境里取代理；返回 None 表示直连。

    代理地址用不了（不是 http 协议、缺主机或端口）时抛 ValueError，由调用方判为失败。
    只支持 http 代理：http.client 的隧道向代理发明文 CONNECT，不会先与代理做 TLS 握手。
    """
    value = next((env[name] for name in _PROXY_VARS if env.get(name)), "")
    if not value:
        return None
    no_proxy = next((env[name] for name in _NO_PROXY_VARS if env.get(name)), "")
    # 只看传入的清单，不读 macOS 系统代理设置。
    if no_proxy and urllib.request.proxy_bypass_environment(host, {"no": no_proxy}):
        return None
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "http" or not parts.hostname or parts.port is None:
        raise ValueError("unsupported proxy")
    authorization = None
    if parts.username is not None:
        userinfo = "{}:{}".format(urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))
        authorization = "Basic " + base64.b64encode(userinfo.encode("utf-8")).decode("ascii")
    return _Proxy(parts.hostname, parts.port, authorization)


def _fetch(target: urllib.parse.SplitResult, target_port: int, proxy: Optional[_Proxy], access_token: str,
           socket_timeout: float) -> PlanResult:
    """发一次 GET 并把结果归类。异常只按类型归类，异常文本可能带地址或响应片段，不外传。"""
    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    options = {"timeout": socket_timeout}
    if target.scheme == "https":
        options["context"] = ssl.create_default_context()
    if proxy is None:
        connection = connection_class(target.hostname, target_port, **options)
    else:
        connection = connection_class(proxy.host, proxy.port, **options)
        tunnel_headers = {"Proxy-Authorization": proxy.authorization} if proxy.authorization else {}
        connection.set_tunnel(target.hostname, target_port, headers=tunnel_headers)
    headers = {"Authorization": "Bearer " + access_token, "User-Agent": "multi-claude/" + __version__,
               "Accept": "application/json"}
    try:
        # 主机名不合法（如 `a..b`）时编码成 IDNA 会抛 UnicodeError：地址用不了，归为连接失败。
        connection.request("GET", target.path or "/", headers=headers)
        response = connection.getresponse()
        status = response.status
        body = response.read(MAX_BODY_BYTES + 1)
    # 顺序不能换：socket.timeout 与 ssl.SSLError 都是 OSError 的子类，EOF 两类又是 SSLError 的子类。
    except socket.timeout:
        return PlanResult(STATUS_ERROR, reason=REASON_TIMEOUT)
    except UnicodeError:
        return PlanResult(STATUS_ERROR, reason=REASON_CONNECT)
    except (ssl.SSLEOFError, ssl.SSLZeroReturnError):
        # 握手或读取途中连接被掐断：是连接问题，不是证书问题，不能让用户去查根证书。
        return PlanResult(STATUS_ERROR, reason=REASON_CONNECT)
    except ssl.SSLError:
        return PlanResult(STATUS_ERROR, reason=REASON_TLS)
    except OSError:
        return PlanResult(STATUS_ERROR, reason=REASON_CONNECT)
    except http.client.HTTPException:
        return PlanResult(STATUS_ERROR, reason=REASON_MALFORMED)
    finally:
        connection.close()
    if status == 401:
        # 本地看还没过期、服务端已不认：对用户来说处理办法相同，重新启动一次该账号。
        return PlanResult(STATUS_EXPIRED)
    if status == 403:
        return PlanResult(STATUS_REJECTED)
    if status != 200:
        return PlanResult(STATUS_ERROR, reason="http_{}".format(status))
    return _parse_profile(body)


def _parse_profile(body: bytes) -> PlanResult:
    if len(body) > MAX_BODY_BYTES:
        return PlanResult(STATUS_ERROR, reason=REASON_MALFORMED)
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return PlanResult(STATUS_ERROR, reason=REASON_MALFORMED)
    organization = data.get("organization") if isinstance(data, dict) else None
    if not isinstance(organization, dict):
        return PlanResult(STATUS_ERROR, reason=REASON_MALFORMED)
    return PlanResult(STATUS_OK, _field(organization.get("organization_type")),
                      _field(organization.get("rate_limit_tier")), _field(organization.get("billing_type")))


def _field(value: object) -> Optional[str]:
    # fullmatch 而不是 match：`$` 会匹配末尾换行之前的位置，带尾部换行的值会混过去。
    return value if isinstance(value, str) and _FIELD_PATTERN.fullmatch(value) else None


def plan_name(result: PlanResult) -> Optional[str]:
    """套餐的显示文本，如 pro、max (5x)；查询不成功时为 None。"""
    if result.status != STATUS_OK:
        return None
    if result.organization_type is None:
        return "unknown"
    name = _PLAN_NAMES.get(result.organization_type, result.organization_type)
    multiplier = _MULTIPLIER_PATTERN.search(result.rate_limit_tier or "")
    return "{} ({}x)".format(name, multiplier.group(1)) if multiplier else name


def billing_name(result: PlanResult) -> Optional[str]:
    """付款渠道的显示文本：apple_subscription → apple，google_play_subscription → google-play。"""
    if result.status != STATUS_OK or result.billing_type is None:
        return None
    parts = [part for part in result.billing_type.split("_") if part and part != "subscription"]
    return "-".join(parts) or result.billing_type


def plan_cell(result: PlanResult) -> str:
    if result.status == STATUS_OK:
        return plan_name(result) or "-"
    # 没登录不是故障，与其它列一样用 "-"；其余状态直接显示状态名。
    return "-" if result.status == STATUS_NO_LOGIN else result.status


def billing_cell(result: PlanResult) -> str:
    return billing_name(result) or "-"


def to_dict(result: PlanResult) -> dict:
    """`list --json` 里每个账号的 plan 对象（方案 §5.2）。"""
    return {"status": result.status, "plan": plan_name(result), "billing": billing_name(result),
            "organization_type": result.organization_type, "rate_limit_tier": result.rate_limit_tier,
            "billing_type": result.billing_type, "reason": result.reason}


def failure_text(name: str, result: PlanResult) -> str:
    """查询失败时给用户的一行说明：固定的 reason，发过请求的再带出站路径与耗时。"""
    text = "plan lookup failed for {}: {}".format(name, result.reason)
    if result.route is not None:
        text += " (via {}, {}s{})".format(result.route, result.elapsed_s,
                                         ", {} attempts".format(result.attempts) if result.attempts > 1 else "")
    return text
