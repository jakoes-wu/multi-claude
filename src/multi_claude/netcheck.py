"""`multi-claude doctor --network`：按每个账号的实际线路检查能否连上 Anthropic（方案 feature-network-check）。

只在用户显式加 `--network` 时运行，是除 `list`（plan.py）以外唯一联网的地方：

- 只连 Claude Code 首次启动自检用的两个地址 `api.anthropic.com`、`platform.claude.com`，
  每个地址发一次不带任何凭据的 `HEAD /`。不读钥匙串、不读 `.credentials.json`、不连第三方服务。
- 线路与启动命令 `claude-<名称>` 一致：先取 `env.account_env()`，再按 Claude Code 的规则用账号
  `settings.json` 的 `env` 覆盖同名变量（官方 env-vars 页 Precedence：设置文件的值胜过终端的值）。
  `settings.json` 只取 `_SETTINGS_KEYS` 列出的键，其它键（可能含令牌）不读出、不输出。
- 不复用 plan.send：http.client 建隧道时“代理没开”和“代理连不出去”都抛 OSError，分不清。
  这里自己分步建连接（解析 → TCP → CONNECT → TLS → 应答），失败时记下卡在哪一步。
- 输出只含主机、端口、变量名与来源；代理原值可能带用户名密码，一律不显示。异常文本也不外传。

线程模型：同一线路（代理 + 认证 + 目标 + 证书）只探一次，每条线路一个守护线程，各线程只写自己的
`_Job`。主线程最多等总时限，到时仍未完成的线路按线程最后记录的阶段出结论，不再等它。
"""

import base64
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

from . import __version__, accounts, platform
from .config import IDENTITY_DEFAULT, PROXY_INHERIT, Account, Config
# doctor 只在 --network 时才 import 本模块（见 doctor.run_checks），所以这里反向 import 不会循环。
from .doctor import LEVEL_ERROR, LEVEL_OK, LEVEL_WARN, Check
from .env import account_env
from .plan import _NO_PROXY_VARS, _PROXY_VARS, RETRY_DELAY_S, SOCKET_TIMEOUT_S

CHECK_ID = "network"
# 官方 network-config 页列出的、Claude Code 首次启动连通性检查所用的两个地址。
TARGETS = ("https://api.anthropic.com/", "https://platform.claude.com/")
# 测试钩子：逗号分隔的本地目标（http:// 或 https://），替换 TARGETS；只有 MULTI_CLAUDE_TEST_MODE=1 时生效。
TEST_TARGETS_HOOK = "MULTI_CLAUDE_TEST_NETCHECK_TARGETS"
# 测试钩子：同时替换套接字超时与总时限（秒）。
TEST_TIMEOUT_HOOK = "MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT"
# 全部线路并发探测的总时限（秒）；到时仍未完成的按阶段记为超时类结论。
TOTAL_TIMEOUT_S = 20.0
# CONNECT 应答头与 HEAD 状态行的读取上限；超过即不是预期的应答。
MAX_HEAD_BYTES = 65536

# 从 settings.json 的 env 里只取这些键：代理、绕过清单、额外根证书、改变请求去向的变量。
_PROVIDER_VARS = (("CLAUDE_CODE_USE_BEDROCK", "Bedrock"), ("CLAUDE_CODE_USE_VERTEX", "Vertex"),
                  ("CLAUDE_CODE_USE_FOUNDRY", "Foundry"))
_SETTINGS_KEYS = (_PROXY_VARS + _NO_PROXY_VARS + ("NODE_EXTRA_CA_CERTS", "ANTHROPIC_BASE_URL")
                  + tuple(name for name, _ in _PROVIDER_VARS))
_SOCKS_SCHEMES = frozenset(["socks", "socks4", "socks4a", "socks5", "socks5h"])
# 3.8/3.9 上 socket.timeout 与 TimeoutError 是两个类，3.10 起才是别名，两个都要接。
_TIMEOUTS = (socket.timeout, TimeoutError)
_STATUS_LINE = re.compile(rb"^HTTP/1\.[01] (\d{3})")

SOURCE_LAUNCHER = "launcher"
SOURCE_SHELL = "shell"
SOURCE_SETTINGS = "settings.json"

# 探测阶段；总时限到期时按线程停在哪个阶段出结论。
STAGE_RESOLVE = "resolve"
STAGE_CONNECT = "connect"
STAGE_TUNNEL = "tunnel"
STAGE_TLS = "tls"
STAGE_RESPONSE = "response"

# 这些失败多半是代理或对端偶发掐断，隔 0.5 秒再试一次（理由同 plan.CONNECT_RETRIES）；超时不重试。
_RETRYABLE = frozenset(["connect_failed", "proxy_unreachable", "proxy_upstream_failed", "tls_closed", "no_response"])


class _Proxy(NamedTuple):
    scheme: str
    host: str
    port: int
    # Basic 认证头原文；只用于发给代理和在内存里区分线路，绝不进入输出
    authorization: Optional[str]
    # 实际生效的变量名（如 https_proxy）与来源，用于修法文案
    variable: str
    source: str


class _Route(NamedTuple):
    # None 表示直连
    proxy: Optional[_Proxy]
    # 有代理但目标被 NO_PROXY 覆盖、改走直连时，记下被绕过的代理
    bypassed: Optional[_Proxy]


class _Result(NamedTuple):
    code: str
    # CONNECT 失败时代理回的状态码；没有时为 None
    status: Optional[int]
    elapsed: float


class _Job(object):
    """一条线路的探测。由一个线程写 stage / result，主线程只读；result 写一次后不再改。"""

    def __init__(self, target: urllib.parse.SplitResult, proxy: Optional[_Proxy], ca_path: Optional[str],
                 socket_timeout: float) -> None:
        self.target = target
        self.proxy = proxy
        self.ca_path = ca_path
        self.socket_timeout = socket_timeout
        self.stage = STAGE_RESOLVE
        self.result: Optional[_Result] = None
        self.done = threading.Event()


class _Pending(NamedTuple):
    account: Account
    target: urllib.parse.SplitResult
    route: _Route
    key: tuple
    settings_path: str


def run(config: Config, base_env: Optional[Mapping[str, str]] = None) -> List[Check]:
    """对每个账号探测两个官方地址，返回 id 为 network 的检查项。不向外抛异常（保证 --json 合法）。"""
    try:
        return _run(config, os.environ if base_env is None else base_env)
    except Exception:  # 意外错误只记一条，不打印异常文本
        return [Check(CHECK_ID, LEVEL_ERROR, "-", "unexpected error while checking")]


def _run(config: Config, base_env: Mapping[str, str]) -> List[Check]:
    targets, has_endpoint = _targets()
    socket_timeout, total_timeout = _timeouts()
    account_list = list(config.accounts.values())
    sys.stderr.write("checking the network for {} account(s) ({})...\n".format(
        len(account_list), ", ".join(_label(target) for target in targets)))
    sys.stderr.flush()
    jobs: Dict[tuple, _Job] = {}
    items: List[Union[Check, _Pending]] = []
    for account in account_list:
        try:
            items.extend(_plan_account(config, account, base_env, targets, has_endpoint, jobs, socket_timeout))
        except Exception:  # 单个账号的意外错误不影响其它账号
            items.append(Check(CHECK_ID, LEVEL_ERROR, account.name, "unexpected error while checking"))
    _run_jobs(jobs, total_timeout)
    checks: List[Check] = []
    for item in items:
        if isinstance(item, Check):
            checks.append(item)
        else:
            checks.append(_format(item, _job_result(jobs[item.key])))
    return checks


def _targets() -> Tuple[List[urllib.parse.SplitResult], bool]:
    """返回 (目标列表, 是否可以发请求)。测试模式下未给目标时不发任何请求：测试套件永远不能连到真实服务。"""
    if platform.test_hook_value(platform.TEST_MODE_ENV) is None:
        return [urllib.parse.urlsplit(url) for url in TARGETS], True
    hook = platform.test_hook_value(TEST_TARGETS_HOOK)
    if not hook:
        return [urllib.parse.urlsplit(url) for url in TARGETS], False
    return [urllib.parse.urlsplit(url.strip()) for url in hook.split(",") if url.strip()], True


def _timeouts() -> Tuple[float, float]:
    value = platform.test_hook_value(TEST_TIMEOUT_HOOK)
    try:
        override = float(value) if value else None
    except ValueError:
        override = None
    if override is not None:
        return override, override
    return SOCKET_TIMEOUT_S, TOTAL_TIMEOUT_S


def _label(target: urllib.parse.SplitResult) -> str:
    # 真实目标是默认端口，只显示主机名；测试目标带端口。
    return target.netloc


def _settings_path(config: Config, account: Account) -> str:
    # Claude Code 眼里的配置目录：default 身份不设 CLAUDE_CONFIG_DIR，用的是 ~/.claude（同 plan._renew）。
    if account.identity == IDENTITY_DEFAULT:
        return os.path.join(os.path.expanduser("~/.claude"), "settings.json")
    return os.path.join(accounts.account_dir(config, account.name), "settings.json")


def _norm(name: str) -> str:
    # Windows 的环境变量名不分大小写，按大写比较；POSIX 区分大小写。
    return name.upper() if platform.IS_WINDOWS else name


def _get(env: Mapping[str, str], name: str) -> str:
    wanted = _norm(name)
    for key, value in env.items():
        if _norm(key) == wanted:
            return value
    return ""


def _read_settings_env(path: str) -> Tuple[Dict[str, object], bool]:
    """读 settings.json 的 env；返回 (env, 是否读不出)。文件不存在不算读不出。

    任何读取或解析异常都吞掉：这里读不出不能让整个 doctor 失败，只提示这份设置没被考虑。
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, False
    except Exception:
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    if "env" not in data:
        return {}, False
    env = data["env"]
    if not isinstance(env, dict):
        return {}, True
    return env, False


def _account_env(config: Config, account: Account, base_env: Mapping[str, str],
                 settings_path: str) -> Tuple[Dict[str, str], Dict[str, str], bool]:
    """返回 (合并后的环境, 代理变量的来源, settings.json 是否读不出)。来源按 _norm 后的变量名记录。"""
    env = account_env(config, account, base_env)
    sources: Dict[str, str] = {}
    # 账号 env 不允许写代理变量（config.py 校验），所以代理变量要么来自启动命令的代理设置，要么来自终端。
    default_source = SOURCE_SHELL if account.proxy == PROXY_INHERIT else SOURCE_LAUNCHER
    for name in _PROXY_VARS:
        if _get(env, name):
            sources[_norm(name)] = default_source
    settings_env, unreadable = _read_settings_env(settings_path)
    wanted = {_norm(name) for name in _SETTINGS_KEYS}
    for key, value in settings_env.items():
        if not isinstance(value, str) or _norm(key) not in wanted:
            continue
        # 设置文件的值覆盖终端的同名值，空字符串也照样覆盖（之后“取第一个非空值”自然视为未设置）。
        for existing in [k for k in env if _norm(k) == _norm(key)]:
            del env[existing]
        env[key] = value
        sources[_norm(key)] = SOURCE_SETTINGS
    return env, sources, unreadable


def _custom_endpoint(env: Mapping[str, str]) -> Optional[str]:
    """请求不发往 api.anthropic.com 时返回去向说明（只含主机名或云厂商名），否则 None。"""
    places = []
    base_url = _get(env, "ANTHROPIC_BASE_URL")
    if base_url:
        try:
            host = urllib.parse.urlsplit(base_url).hostname
        except ValueError:
            host = None
        places.append(host or "a custom ANTHROPIC_BASE_URL")
    for name, label in _PROVIDER_VARS:
        value = _get(env, name).strip().lower()
        if value and value not in ("0", "false"):
            places.append(label)
    return " / ".join(places) if places else None


def _parse_proxy(env: Mapping[str, str], sources: Mapping[str, str]) -> Union[None, _Proxy, Tuple[str, str]]:
    """按 Claude Code 的顺序取代理并解析。返回 None（直连）、_Proxy，或 (失败代号, 变量名与来源)。

    先于 NO_PROXY 解析：代理值写错时 Claude Code 启动即失败，与目标是否被绕过无关。
    失败时只返回变量名与来源，不返回原值：原值可能带用户名密码。
    """
    variable = next((name for name in _PROXY_VARS if _get(env, name)), None)
    if variable is None:
        return None
    value = _get(env, variable)
    label = "{} ({})".format(variable, sources.get(_norm(variable), SOURCE_SHELL))
    parts = urllib.parse.urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme in _SOCKS_SCHEMES:
        return "unsupported_socks", label
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:  # 端口不是合法整数
        return "invalid_proxy", label
    if scheme not in ("http", "https") or not host:
        return "invalid_proxy", label
    if port is None:
        port = 443 if scheme == "https" else 80
    authorization = None
    if parts.username is not None:
        userinfo = "{}:{}".format(urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))
        authorization = "Basic " + base64.b64encode(userinfo.encode("utf-8")).decode("ascii")
    return _Proxy(scheme, host, port, authorization, variable, sources.get(_norm(variable), SOURCE_SHELL))


def _route(proxy: Optional[_Proxy], env: Mapping[str, str], host: str) -> _Route:
    if proxy is None:
        return _Route(None, None)
    no_proxy = next((_get(env, name) for name in _NO_PROXY_VARS if _get(env, name)), "")
    # 官方允许逗号或空格分隔；标准库只认逗号。只看这份清单，不读 macOS 系统代理设置（同 plan.py）。
    no_proxy = ",".join(item for item in re.split(r"[,\s]+", no_proxy) if item)
    if no_proxy and urllib.request.proxy_bypass_environment(host, {"no": no_proxy}):
        return _Route(None, proxy)
    return _Route(proxy, None)


def _usable_ca(path: str) -> bool:
    try:
        ssl.create_default_context().load_verify_locations(path)
    except Exception:
        return False
    return True


def _plan_account(config: Config, account: Account, base_env: Mapping[str, str],
                  targets: List[urllib.parse.SplitResult], has_endpoint: bool, jobs: Dict[tuple, _Job],
                  socket_timeout: float) -> List[Union[Check, _Pending]]:
    """解析一个账号的线路，返回账号级提示与待探测项；同一线路的 _Job 登记在 jobs 里共享。"""
    name = account.name
    settings_path = _settings_path(config, account)
    env, sources, unreadable = _account_env(config, account, base_env, settings_path)
    items: List[Union[Check, _Pending]] = []
    if unreadable:
        items.append(Check(CHECK_ID, LEVEL_WARN, name,
                           "could not read {}; proxy settings in it were not considered".format(settings_path)))
    place = _custom_endpoint(env)
    if place:
        items.append(Check(CHECK_ID, LEVEL_WARN, name,
                           "this account sends requests to {}; the results below only cover "
                           "api.anthropic.com and platform.claude.com".format(place)))
    parsed = _parse_proxy(env, sources)
    if parsed is not None and not isinstance(parsed, _Proxy):
        code, label = parsed
        if code == "unsupported_socks":
            text = "{} is a SOCKS proxy, which Claude Code does not support; use an http proxy".format(label)
        else:
            text = ("{} is not a valid proxy URL and Claude Code will fail to start; "
                    "use http://host:port".format(label))
        items.append(Check(CHECK_ID, LEVEL_ERROR, name, text))
        return items
    proxy = parsed
    ca_path = _get(env, "NODE_EXTRA_CA_CERTS") or None
    if ca_path and not _usable_ca(ca_path):
        items.append(Check(CHECK_ID, LEVEL_WARN, name,
                           "NODE_EXTRA_CA_CERTS points to a file that cannot be read as certificates; "
                           "checked with the default certificates only"))
        ca_path = None
    for target in targets:
        if not has_endpoint:
            items.append(Check(CHECK_ID, LEVEL_ERROR, name,
                               "{}: test mode without {}; nothing was sent".format(_label(target),
                                                                                   TEST_TARGETS_HOOK)))
            continue
        route = _route(proxy, env, target.hostname or "")
        if route.proxy is not None and route.proxy.scheme == "https":
            items.append(Check(CHECK_ID, LEVEL_WARN, name,
                               "{} via {}: https proxies are not checked by multi-claude; run claude --debug "
                               "or /status to see whether it connects".format(_label(target),
                                                                              _route_label(route.proxy))))
            continue
        via = route.proxy
        key = (via.host if via else None, via.port if via else None, via.authorization if via else None,
               target.scheme, target.hostname, _target_port(target), ca_path)
        if key not in jobs:
            jobs[key] = _Job(target, via, ca_path, socket_timeout)
        items.append(_Pending(account, target, route, key, settings_path))
    return items


def _target_port(target: urllib.parse.SplitResult) -> int:
    return target.port or (443 if target.scheme == "https" else 80)


def _route_label(proxy: Optional[_Proxy]) -> str:
    if proxy is None:
        return "direct"
    return "{} ({})".format(_authority(proxy.host, proxy.port), proxy.source)


# ---- 探测 ----

def _run_jobs(jobs: Dict[tuple, _Job], total_timeout: float) -> None:
    deadline = time.monotonic() + total_timeout
    for job in jobs.values():
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    # 用事件等，不用 Thread.join：到时限就放弃，线程只做只读探测，丢下不管没有副作用。
    for job in jobs.values():
        job.done.wait(max(0.0, deadline - time.monotonic()))


def _job_result(job: _Job) -> _Result:
    result = job.result
    if result is not None:
        return result
    via_proxy = job.proxy is not None
    stage = job.stage
    # 总时限到期：按线程最后停在的阶段出结论。getaddrinfo 没有超时参数，DNS 卡住只能落在这里。
    if stage == STAGE_RESOLVE:
        code = "proxy_dns_timeout" if via_proxy else "dns_timeout"
    elif stage == STAGE_CONNECT:
        code = "proxy_timeout" if via_proxy else "timeout"
    elif stage == STAGE_TUNNEL:
        code = "proxy_upstream_timeout"
    else:
        code = "timeout"
    return _Result(code, None, 0.0)


def _run_job(job: _Job) -> None:
    try:
        start = time.monotonic()
        code, status = _attempt(job)
        retried = False
        if code in _RETRYABLE:
            time.sleep(RETRY_DELAY_S)
            job.stage = STAGE_RESOLVE
            code, status = _attempt(job)
            retried = True
        if retried and code == "ok":
            code = "ok_after_retry"
        job.result = _Result(code, status, time.monotonic() - start)
    except Exception:  # 线程里的意外错误立即记为 internal_error，不让主线程空等到总时限
        job.result = _Result("internal_error", None, 0.0)
    finally:
        job.done.set()


def _attempt(job: _Job) -> Tuple[str, Optional[int]]:
    """按阶段建一次连接并发 HEAD /。返回 (结果代号, CONNECT 失败时的状态码)。"""
    target = job.target
    host = target.hostname or ""
    port = _target_port(target)
    proxy = job.proxy
    via_proxy = proxy is not None
    connect_host, connect_port = (proxy.host, proxy.port) if proxy is not None else (host, port)
    job.stage = STAGE_RESOLVE
    try:
        socket.getaddrinfo(connect_host, connect_port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return ("proxy_dns_failed" if via_proxy else "dns_failed"), None
    job.stage = STAGE_CONNECT
    try:
        sock = socket.create_connection((connect_host, connect_port), timeout=job.socket_timeout)
    except _TIMEOUTS:
        return ("proxy_timeout" if via_proxy else "timeout"), None
    except (OSError, UnicodeError):
        return ("proxy_unreachable" if via_proxy else "connect_failed"), None
    try:
        if proxy is not None:
            job.stage = STAGE_TUNNEL
            failure = _tunnel(sock, host, port, proxy.authorization)
            if failure is not None:
                return failure
        if target.scheme == "https":
            job.stage = STAGE_TLS
            failure_code, sock = _handshake(sock, host, job.ca_path)
            if failure_code is not None:
                return failure_code, None
        job.stage = STAGE_RESPONSE
        return _head(sock, host, port, target.scheme), None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _authority(host: str, port: int) -> str:
    return "[{}]:{}".format(host, port) if ":" in host else "{}:{}".format(host, port)


def _tunnel(sock: socket.socket, host: str, port: int,
            authorization: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
    """向 http 代理发 CONNECT；成功返回 None，失败返回 (代号, 状态码)。"""
    authority = _authority(host, port)
    request = "CONNECT {0} HTTP/1.1\r\nHost: {0}\r\n".format(authority)
    if authorization:
        request += "Proxy-Authorization: {}\r\n".format(authorization)
    request += "\r\n"
    data = b""
    try:
        sock.sendall(request.encode("latin-1"))
        while b"\r\n\r\n" not in data and len(data) <= MAX_HEAD_BYTES:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            # 开头就不像 HTTP（如端口填成了 SOCKS 端口）：不必等到超时，立即判定。
            prefix = b"HTTP/1."
            if data[:len(prefix)] != prefix[:len(data)]:
                return "proxy_bad_reply", None
    except _TIMEOUTS:
        return "proxy_upstream_timeout", None
    except OSError:
        return "proxy_upstream_failed", None
    if not data:
        return "proxy_upstream_failed", None
    match = _STATUS_LINE.match(data)
    if match is None:
        return "proxy_bad_reply", None
    status = int(match.group(1))
    if status == 200:
        return None
    if status == 407:
        return "proxy_auth", status
    return "proxy_upstream_failed", status


def _handshake(sock: socket.socket, host: str, ca_path: Optional[str]) -> Tuple[Optional[str], socket.socket]:
    context = ssl.create_default_context()
    if ca_path:
        context.load_verify_locations(ca_path)
    try:
        return None, context.wrap_socket(sock, server_hostname=host)
    except ssl.SSLError as exc:
        return _classify_tls(exc), sock
    except (_TIMEOUTS + (OSError,)) as exc:
        return _classify_tls(exc), sock


def _classify_tls(exc: BaseException) -> str:
    """TLS 握手异常归类。顺序不能换：证书错误与 EOF 都是 SSLError 的子类，超时与 SSLError 都是 OSError 的子类。"""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "cert_unverified"
    if isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError, ConnectionResetError)):
        # 握手途中被掐断：是线路问题，不是证书问题
        return "tls_closed"
    if isinstance(exc, ssl.SSLError):
        return "tls_failed"
    if isinstance(exc, _TIMEOUTS):
        return "timeout"
    return "tls_closed"


def _head(sock: socket.socket, host: str, port: int, scheme: str) -> str:
    """发 HEAD / 并读状态行；读到合法 HTTP 状态行（任何状态码）即算连通。"""
    default_port = 443 if scheme == "https" else 80
    if port != default_port:
        host_header = _authority(host, port)
    else:
        host_header = "[{}]".format(host) if ":" in host else host
    request = ("HEAD / HTTP/1.1\r\nHost: {}\r\nUser-Agent: multi-claude/{}\r\n"
               "Connection: close\r\n\r\n").format(host_header, __version__)
    data = b""
    try:
        sock.sendall(request.encode("ascii"))
        while b"\r\n" not in data and len(data) <= MAX_HEAD_BYTES:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except _TIMEOUTS:
        return "timeout"
    except OSError:
        return "no_response"
    return "ok" if _STATUS_LINE.match(data) else "no_response"


# ---- 文案 ----

def _proxy_fix(account: Account, proxy: _Proxy, settings_path: str) -> str:
    """修代理的建议，按代理是在哪设的给出。"""
    if proxy.source == SOURCE_SETTINGS:
        return "start the proxy app, or fix {} in {}".format(proxy.variable, settings_path)
    if proxy.source == SOURCE_SHELL:
        return "start the proxy app, or fix {} in your terminal".format(proxy.variable)
    return "start the proxy app, or change the port with: multi-claude proxy {} <port>".format(account.name)


_SWITCH_LINE = "switch to another proxy line or node"


def _format(item: _Pending, result: _Result) -> Check:
    target = _label(item.target)
    via = item.route.proxy
    # 被 NO_PROXY 绕过的直连：账号其实配了代理，问题在 NO_PROXY（消息末尾另有说明），
    # 这里再建议“设个代理”会把人往错的方向引。
    suggest_proxy = item.route.bypassed is None
    code = result.code
    if code in ("ok", "ok_after_retry"):
        level = LEVEL_OK if code == "ok" else LEVEL_WARN
        text = "reachable ({:.2f}s)".format(result.elapsed)
        if code == "ok_after_retry":
            text = "reachable on the second try ({:.2f}s); the connection is unstable".format(result.elapsed)
    elif code in ("proxy_dns_failed", "proxy_dns_timeout", "proxy_unreachable", "proxy_timeout"):
        level = LEVEL_ERROR
        text = "cannot reach the proxy at {}; {}".format(_authority(via.host, via.port),
                                                         _proxy_fix(item.account, via, item.settings_path))
    elif code == "proxy_auth":
        level = LEVEL_ERROR
        text = "the proxy asks for a user name and password (407)"
    elif code == "proxy_upstream_failed":
        level = LEVEL_ERROR
        if result.status is not None:
            text = "the proxy is running but could not reach {} ({}); {}".format(target, result.status,
                                                                                 _SWITCH_LINE)
        else:
            text = "the proxy closed the connection instead of reaching {}; {}".format(target, _SWITCH_LINE)
    elif code == "proxy_upstream_timeout":
        level = LEVEL_ERROR
        text = "the proxy accepted the request but did not connect to {} in time; {}".format(target, _SWITCH_LINE)
    elif code == "proxy_bad_reply":
        level = LEVEL_ERROR
        text = ("the proxy did not answer like an http proxy; check the port, "
                "or switch the proxy app to http mode")
    elif code in ("dns_failed", "dns_timeout"):
        level = LEVEL_ERROR
        text = "cannot resolve {}; check the network or DNS".format(target)
        if suggest_proxy:
            text += ", or set a proxy"
    elif code in ("connect_failed", "timeout", "no_response"):
        level = LEVEL_ERROR
        text = {"connect_failed": "cannot connect to {}".format(target),
                "timeout": "timed out",
                "no_response": "no answer from {}".format(target)}[code]
        if via is not None:
            text += "; " + _SWITCH_LINE
        elif suggest_proxy:
            text += "; check the network, or set a proxy with: multi-claude proxy {} <port>".format(item.account.name)
        else:
            text += "; check the network"
    elif code == "tls_closed":
        level = LEVEL_ERROR
        text = ("the connection was cut during the secure handshake; the network or proxy "
                "may be blocking {}".format(target))
    elif code == "tls_failed":
        level = LEVEL_ERROR
        text = "secure connection failed"
    elif code == "cert_unverified":
        level = LEVEL_WARN
        text = ("Python could not verify the certificate; a network or app may be intercepting HTTPS. "
                "Claude Code also trusts the system certificate store and NODE_EXTRA_CA_CERTS, "
                "so it may still work")
    else:
        level = LEVEL_ERROR
        text = "unexpected error while checking"
    message = "{} via {}: {}".format(target, _route_label(via), text)
    bypassed = item.route.bypassed
    if bypassed is not None:
        # 绕过代理本身不算错：级别跟直连结果走，只是直连成功时也提醒一下，避免用户以为走了代理。
        if level == LEVEL_OK:
            level = LEVEL_WARN
        message += (" (NO_PROXY covers this host, so Claude Code connects directly instead of through {}; "
                    "remove it from NO_PROXY if that is not intended)".format(_authority(bypassed.host, bypassed.port)))
    return Check(CHECK_ID, level, item.account.name, message)


# ---- 供启动前网络检查复用（方案 feature-launch-gate §3）----
# launchgate 要按与 doctor --network 相同的算法求“Claude 实际会用的代理”，这里只给这些函数起公开名字，
# 原名与 doctor 的调用点不动、逻辑不变；改它们的行为会同时影响 doctor --network 与启动前检查。
Proxy = _Proxy
account_route_env = _account_env
custom_endpoint = _custom_endpoint
env_value = _get
parse_proxy = _parse_proxy
route = _route
settings_path = _settings_path
tunnel = _tunnel
usable_ca = _usable_ca
authority = _authority
