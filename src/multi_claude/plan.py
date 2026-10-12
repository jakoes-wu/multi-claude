"""向服务端实时查询各账号的套餐与付款渠道，供 `list` 显示（方案 feature-live-plan）。

本模块是整个工具里唯一联网、唯一使用访问令牌的地方，只有 `list` 调用它：

- 每个账号用自己的访问令牌请求 `GET /api/oauth/profile`，与 Claude Code 自己查询套餐的接口相同。
  过期的访问令牌不会被发出去：先交给 refresh.py 用刷新令牌换新的并写回凭据（`--no-refresh` 时不做），
  换不到就报告“令牌过期”。
- 出站路径与启动命令 `claude-<名称>` 一致：代理取自 `env.account_env()` 构造的账号环境。
  配置了代理时，任何失败都不会改走直连。
- 不用 urllib 的 ProxyHandler：没有代理环境变量时它会读 macOS 系统代理的例外清单，
  可能把请求悄悄改成直连。这里直接用 http.client 建 CONNECT 隧道，路径完全由本模块决定。
- 令牌、响应体原文、异常文本都不进入返回值与输出；失败只归类成固定的 reason。
  服务端返回的字段先过格式校验，再进入终端输出。

线程模型：`query_all()` 为每个账号起一个守护线程，各线程只写自己那一项结果。唯一共享的可变状态是
`_CommitGate`：它保证“主线程放弃等待”与“线程开始刷新令牌”二者只能发生一个，见该类的说明。
主线程用事件等线程结束，不用 Thread.join（原因见 query_all）。
"""

import base64
import contextlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Set, Tuple

from . import __version__, accounts, identity, platform, refresh
from .config import IDENTITY_DEFAULT, Account, Config
from .env import account_env

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# 测试钩子：把查询目标换成本地假服务；只有 MULTI_CLAUDE_TEST_MODE=1 时生效。
TEST_URL_HOOK = "MULTI_CLAUDE_TEST_PROFILE_URL"
# 测试钩子：同时替换套接字超时与总时限（秒），供超时用例使用。
TEST_TIMEOUT_HOOK = "MULTI_CLAUDE_TEST_PLAN_TIMEOUT"
# 测试钩子：只替换总时限，用来验证“刷新途中总时限到期也要等它写完”。
TEST_TOTAL_TIMEOUT_HOOK = "MULTI_CLAUDE_TEST_PLAN_TOTAL_TIMEOUT"

# 刷新令牌的接口，与 Claude Code 相同；测试钩子的规则同 TEST_URL_HOOK。
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
TEST_TOKEN_URL_HOOK = "MULTI_CLAUDE_TEST_TOKEN_URL"
# 刷新请求的套接字超时，与 Claude Code 相同。比套餐查询宽：请求可能已被服务端受理，
# 超时放弃等于丢掉新换的刷新令牌，这个账号就得重新登录。
REFRESH_SOCKET_TIMEOUT_S = 30.0

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
    # 访问令牌过期后刷新的结果：None 表示没有尝试；否则是 refresh.py 的 status
    refresh: Optional[str] = None


class _Proxy(NamedTuple):
    host: str
    port: int
    # Proxy-Authorization 的值；代理地址不带用户名密码时为 None
    authorization: Optional[str]


def query_all(config: Config, base_env: Mapping[str, str], now: datetime,
              allow_refresh: bool = True) -> Dict[str, PlanResult]:
    """并发查询全部账号，返回 {账号名: 结果}。任何失败都体现在结果里，不抛异常。

    总耗时一般不超过总时限。例外：已经发出刷新请求的账号必须等它把新令牌写回凭据
    （否则这个账号要重新登录），这时会等到它结束：SIGTERM、SIGHUP 不会打断这段等待，Ctrl-C 要按第二次才会。
    收到过中断时，等完后抛 KeyboardInterrupt。
    """
    timeout = _timeout_override(TEST_TIMEOUT_HOOK)
    socket_timeout = SOCKET_TIMEOUT_S if timeout is None else timeout
    total_timeout = _timeout_override(TEST_TOTAL_TIMEOUT_HOOK) or (TOTAL_TIMEOUT_S if timeout is None else timeout)
    now_ms = int(now.timestamp() * 1000)
    results: Dict[str, PlanResult] = {}
    gate = _CommitGate()
    names = [account.name for account in config.accounts.values()]
    # 每个线程结束时置位。不用 Thread.join / is_alive 判断结束：在 Python 3.9 到 3.11 上，
    # join 被 KeyboardInterrupt 打断后会把仍在运行的线程标成已结束，等待就此落空。
    finished = {name: threading.Event() for name in names}
    interrupts = _Interrupts()
    with _signals_as_interrupt(interrupts):
        try:
            for account in config.accounts.values():
                # 守护线程：总时限到了以后进程可以直接退出，不会被卡住的连接拖住。
                threading.Thread(target=_query_into, name="plan-" + account.name, daemon=True,
                                 args=(results, gate, finished[account.name], config, account, base_env, now_ms,
                                       socket_timeout, allow_refresh)).start()
            deadline = time.monotonic() + total_timeout
            for name in names:
                # 分片等待：每片结束看一眼有没有收到终止信号（信号处理函数只置标志，不抛异常）
                while not interrupts.terminated and time.monotonic() < deadline:
                    if finished[name].wait(min(0.2, max(0.0, deadline - time.monotonic()))):
                        break
        except KeyboardInterrupt as exc:
            interrupts.note(exc)
        # 无论是到时限还是被打断，收尾都一样：还没开始刷新的账号一律不许再开始，已经开始的全部等完。
        # 收尾本身被打断就重来一遍（它可以重复执行），直到做完，或用户第二次按下 Ctrl-C。
        while True:
            try:
                _settle(names, gate, interrupts)
                break
            except KeyboardInterrupt as exc:
                interrupts.note(exc)
                if interrupts.ctrl_c >= 2:
                    raise
    if interrupts.any:
        raise KeyboardInterrupt
    # 超时的线程之后即使写入 results 也不再采用：这里取到什么就是什么。
    return {name: results.get(name) or PlanResult(STATUS_ERROR, reason=REASON_TIMEOUT) for name in names}


class _Interrupts(object):
    """查询期间收到的中断。

    Ctrl-C 以 KeyboardInterrupt 的形式到达，按第二次才放弃等待。SIGTERM、SIGHUP 只置 terminated 标志：
    它们不算“用户坚持要退出”，来多少次都先把刷新写完；处理函数里也不抛异常——两个不同的信号几乎同时到达时，
    第二个异常会打断第一个的展开，把等待用的锁弄坏。
    """

    def __init__(self) -> None:
        self.terminated = False
        # 用户按 Ctrl-C 的次数；到 2 才放弃等待
        self.ctrl_c = 0
        self.warned = False

    @property
    def any(self) -> bool:
        return self.terminated or self.ctrl_c > 0

    def note(self, _exc: KeyboardInterrupt) -> None:
        self.ctrl_c += 1


def _settle(names: List[str], gate: "_CommitGate", interrupts: _Interrupts) -> None:
    """放弃所有还没开始刷新的账号，等所有正在刷新的账号把新令牌写完。可以重复执行。

    等待没有时间上限：刷新的每一步都有自己的超时，一定会结束；提前走掉的代价是那个账号要重新登录。
    """
    committing = [name for name in names if not gate.abandon(name)]
    if interrupts.any and committing and not interrupts.warned:
        interrupts.warned = True
        try:
            print("[multi-claude] saving the renewed login of {}; press Ctrl-C again to quit anyway "
                  "(that login may then be lost)".format(", ".join(committing)), file=sys.stderr)
        except (OSError, ValueError):
            # 终端已经关掉（SIGHUP）或管道对端已退出：提示打不出去，但等待必须照常进行。
            pass
    for name in committing:
        while not gate.wait_settled(name, 0.2):
            pass


@contextlib.contextmanager
def _signals_as_interrupt(interrupts: _Interrupts):
    """查询期间接管 SIGTERM、SIGHUP：只记一笔，让关终端、被 kill 时同样先把刷新写完，再按中断退出。

    信号处理函数只能在主线程里装；不在主线程时什么也不做。原本被忽略的信号（如 nohup 下的 SIGHUP）保持忽略。
    """
    def handler(_signum, _frame):
        interrupts.terminated = True

    previous = {}
    for number in (signal.SIGTERM, signal.SIGHUP):
        try:
            old = signal.getsignal(number)
            # None 表示原处理方式不是 Python 设置的（被别的程序嵌入时），之后没法原样恢复，所以不接管
            if old is None or old == signal.SIG_IGN:
                continue
            signal.signal(number, handler)
            previous[number] = old
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for number, old in previous.items():
            try:
                signal.signal(number, old)
            except (ValueError, OSError, TypeError):
                pass


class _CommitGate(object):
    """协调“主线程不再等某个账号”与“该账号的线程开始刷新令牌”。

    刷新请求发出后必须等到新令牌写回凭据，进程才能退出。如果主线程已经决定不等了，线程随后才开始刷新，
    进程退出时刷新可能正做到一半，新令牌就丢了。所以两件事在同一把锁下互斥：
    线程开始刷新前调用 enter()，主线程放弃前调用 abandon()；先到的生效，后到的得到 False。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._committing: Set[str] = set()
        self._abandoned: Set[str] = set()

    def enter(self, name: str) -> bool:
        """线程在发出刷新请求之前调用；返回 False 表示主线程已经不等了，不得刷新。"""
        with self._lock:
            if name in self._abandoned:
                return False
            self._committing.add(name)
            return True

    def leave(self, name: str) -> None:
        """刷新结束（成功、失败或没做）后调用；之后主线程不必再等这个账号。"""
        with self._lock:
            self._committing.discard(name)
            self._abandoned.add(name)

    def abandon(self, name: str) -> bool:
        """主线程在放弃等待前调用；返回 False 表示该线程正在刷新，必须继续等。"""
        with self._lock:
            if name in self._committing:
                return False
            self._abandoned.add(name)
            return True

    def wait_settled(self, name: str, timeout_s: float) -> bool:
        """等到该账号不在刷新为止，最多等 timeout_s；返回是否已经结束。"""
        end = time.monotonic() + timeout_s
        while True:
            with self._lock:
                if name not in self._committing:
                    return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)


def _query_into(results: Dict[str, PlanResult], gate: _CommitGate, finished: threading.Event, config: Config,
                account: Account, base_env: Mapping[str, str], now_ms: int, socket_timeout: float,
                allow_refresh: bool) -> None:
    try:
        results[account.name] = query(config, account, base_env, now_ms, socket_timeout, allow_refresh,
                                      on_commit=lambda: gate.enter(account.name),
                                      on_settled=lambda: gate.leave(account.name))
    except Exception:  # 线程里的意外错误不能让 list 整体失败，也不能把异常文本打到终端
        results[account.name] = PlanResult(STATUS_ERROR, reason=REASON_INTERNAL)
    finally:
        gate.leave(account.name)
        finished.set()


def query(config: Config, account: Account, base_env: Mapping[str, str], now_ms: int,
          socket_timeout: float = SOCKET_TIMEOUT_S, allow_refresh: bool = True,
          on_commit: Callable[[], bool] = lambda: True,
          on_settled: Callable[[], None] = lambda: None) -> PlanResult:
    """查询一个账号。先在本地排除不需要、不应该发请求的情况，再发请求；只有连接类失败会重试一次。

    访问令牌过期时先刷新（allow_refresh 为真才做），刷新不成则结果为 STATUS_EXPIRED。
    """
    account_dir = accounts.account_dir(config, account.name)
    token = identity.read_oauth_token(account.identity, account_dir)
    if token.state == identity.TOKEN_NONE:
        return PlanResult(STATUS_NO_LOGIN)
    if token.state != identity.TOKEN_OK:
        return PlanResult(STATUS_ERROR, reason=REASON_CREDENTIALS)
    env = account_env(config, account, base_env)
    access_token = token.access_token
    refreshed = None
    if token.expires_at_ms is not None and token.expires_at_ms <= now_ms:
        # 过期的访问令牌服务端必然拒绝（实测 401），不把它发出去。
        if not allow_refresh:
            return PlanResult(STATUS_EXPIRED)
        try:
            outcome = _renew(account, account_dir, env, now_ms, on_commit)
        finally:
            # 凭据已经写完（或没写成）：后面的套餐查询不值得让主线程无限期等
            on_settled()
        if outcome.status not in refresh.USABLE:
            return PlanResult(STATUS_EXPIRED, refresh=outcome.status)
        access_token, refreshed = outcome.access_token, outcome.status
    url = _endpoint(PROFILE_URL, TEST_URL_HOOK)
    if url is None:
        return PlanResult(STATUS_ERROR, reason=REASON_TEST_NO_ENDPOINT, refresh=refreshed)
    target = urllib.parse.urlsplit(url)
    try:
        proxy = _resolve_proxy(env, target.hostname or "")
    except ValueError:
        # 代理配置了但用不了：按失败处理，绝不退回直连。
        return PlanResult(STATUS_ERROR, reason=REASON_PROXY, refresh=refreshed)
    route = ROUTE_DIRECT if proxy is None else "{}:{}".format(proxy.host, proxy.port)
    started = time.monotonic()
    outcome = _fetch(target, proxy, access_token, socket_timeout)
    attempts = 1
    while outcome.reason == REASON_CONNECT and attempts <= CONNECT_RETRIES:
        time.sleep(RETRY_DELAY_S)
        # 重试走同一条路径（同一个代理或同样直连），不因为失败而换路。
        outcome = _fetch(target, proxy, access_token, socket_timeout)
        attempts += 1
    elapsed = round(time.monotonic() - started, 2)
    return outcome._replace(route=route, elapsed_s=elapsed, attempts=attempts, refresh=refreshed)


def _renew(account: Account, account_dir: str, env: Mapping[str, str], now_ms: int,
           on_commit: Callable[[], bool]) -> refresh.Outcome:
    """用该账号的出站路径刷新令牌。刷新请求不重试：重发会把同一个刷新令牌用两次。"""
    url = _endpoint(TOKEN_URL, TEST_TOKEN_URL_HOOK)
    if url is None:
        return refresh.Outcome(REASON_TEST_NO_ENDPOINT)
    target = urllib.parse.urlsplit(url)
    try:
        proxy = _resolve_proxy(env, target.hostname or "")
    except ValueError:
        return refresh.Outcome(REASON_PROXY)

    def post(body: dict):
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "multi-claude/" + __version__}
        return send(target, proxy, "POST", headers, payload, REFRESH_SOCKET_TIMEOUT_S)

    # Claude Code 眼里的配置目录：default 身份不设 CLAUDE_CONFIG_DIR，用的是 ~/.claude。
    config_dir = os.path.expanduser("~/.claude") if account.identity == IDENTITY_DEFAULT else account_dir
    return refresh.renew(account.identity, account_dir, config_dir, now_ms, post, on_commit)


def _endpoint(real_url: str, hook: str) -> Optional[str]:
    """请求地址。测试模式下必须显式给出本地地址，否则不发请求：测试套件永远不能连到真实服务。"""
    if platform.test_hook_value(platform.TEST_MODE_ENV) is None:
        return real_url
    return platform.test_hook_value(hook) or None


def _timeout_override(hook: str) -> Optional[float]:
    value = platform.test_hook_value(hook)
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


def send(target: urllib.parse.SplitResult, proxy: Optional[_Proxy], method: str, headers: Mapping[str, str],
         body: Optional[bytes], socket_timeout: float) -> Tuple[Optional[str], int, bytes]:
    """发一次请求，返回 (传输失败原因或 None, 状态码, 响应体)。不重试、不跟随重定向。

    异常只按类型归类成固定的原因；异常文本可能带地址或响应片段，不外传。
    """
    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    target_port = target.port or (443 if target.scheme == "https" else 80)
    options = {"timeout": socket_timeout}
    if target.scheme == "https":
        options["context"] = ssl.create_default_context()
    if proxy is None:
        connection = connection_class(target.hostname, target_port, **options)
    else:
        connection = connection_class(proxy.host, proxy.port, **options)
        tunnel_headers = {"Proxy-Authorization": proxy.authorization} if proxy.authorization else {}
        connection.set_tunnel(target.hostname, target_port, headers=tunnel_headers)
    try:
        # 主机名不合法（如 `a..b`）时编码成 IDNA 会抛 UnicodeError：地址用不了，归为连接失败。
        connection.request(method, target.path or "/", body=body, headers=dict(headers))
        response = connection.getresponse()
        return None, response.status, response.read(MAX_BODY_BYTES + 1)
    # 顺序不能换：socket.timeout 与 ssl.SSLError 都是 OSError 的子类，EOF 两类又是 SSLError 的子类。
    except socket.timeout:
        return REASON_TIMEOUT, 0, b""
    except UnicodeError:
        return REASON_CONNECT, 0, b""
    except (ssl.SSLEOFError, ssl.SSLZeroReturnError):
        # 握手或读取途中连接被掐断：是连接问题，不是证书问题，不能让用户去查根证书。
        return REASON_CONNECT, 0, b""
    except ssl.SSLError:
        return REASON_TLS, 0, b""
    except OSError:
        return REASON_CONNECT, 0, b""
    except http.client.HTTPException:
        return REASON_MALFORMED, 0, b""
    finally:
        connection.close()


def _fetch(target: urllib.parse.SplitResult, proxy: Optional[_Proxy], access_token: str,
           socket_timeout: float) -> PlanResult:
    """用访问令牌查一次套餐，并把结果归类。"""
    headers = {"Authorization": "Bearer " + access_token, "User-Agent": "multi-claude/" + __version__,
               "Accept": "application/json"}
    reason, status, body = send(target, proxy, "GET", headers, None, socket_timeout)
    if reason is not None:
        return PlanResult(STATUS_ERROR, reason=reason)
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
    # 没登录不是故障，与其它列一样用 "-"。
    if result.status == STATUS_NO_LOGIN:
        return "-"
    # 写成 token expired：只写 expired 会被读成“订阅过期”，而过期的只是几小时有效的访问令牌。
    return "token expired" if result.status == STATUS_EXPIRED else result.status


def billing_cell(result: PlanResult) -> str:
    return billing_name(result) or "-"


def to_dict(result: PlanResult) -> dict:
    """`list --json` 里每个账号的 plan 对象（方案 §5.2）。"""
    return {"status": result.status, "plan": plan_name(result), "billing": billing_name(result),
            "organization_type": result.organization_type, "rate_limit_tier": result.rate_limit_tier,
            "billing_type": result.billing_type, "reason": result.reason, "refresh": result.refresh}


def failure_text(name: str, result: PlanResult) -> str:
    """查询失败时给用户的一行说明：固定的 reason，发过请求的再带出站路径与耗时。"""
    text = "plan lookup failed for {}: {}".format(name, result.reason)
    if result.route is not None:
        text += " (via {}, {}s{})".format(result.route, result.elapsed_s,
                                         ", {} attempts".format(result.attempts) if result.attempts > 1 else "")
    return text
