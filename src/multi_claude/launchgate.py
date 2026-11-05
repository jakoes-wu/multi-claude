"""启动前网络检查（方案 docs/feature/feature-launch-gate.md）。

职责：按账号设置检查 Claude 实际会用的代理端口、出口国家与 Clash 状态，决定是否放行这一次 Claude 启动。
只读网络状态，不改任何配置；不缓存结果，每次启动重新检查。

“实际代理”与 `doctor --network` 同一算法（netcheck.account_route_env / parse_proxy / route）：启动命令的
代理设置，再按 Claude Code 的规则用账号 settings.json 的 env 覆盖。不模拟 --settings、托管设置与项目级设置。

局限（方案 §2）：只在启动那一刻检查一次；Claude 桌面版、VS Code 插件与直接运行的 `claude` 不经过本检查。
本模块不能、也不声称能防止运行中改走直连。

只用标准库：直连请求用 http.client（它不读环境变量代理，直连因此真的不走代理）；经代理时自己建 CONNECT 隧道。
本模块只在账号开启检查时才被导入（cli._launchgate），未开启的账号启动时不加载本模块。
"""

import http.client
import io
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from typing import List, NamedTuple, Optional, Tuple

from . import __version__, netcheck, platform
from .config import NETCHECK_SKIP_ENV, PIPE_PREFIX, Account, Config

# 查出口国家的地址（用户 2026-11-05 拍板）：Claude 的接口域名上的 Cloudflare 诊断页，响应是 key=value 行。
TRACE_HOST = "api.anthropic.com"
TRACE_PORT = 443
TRACE_PATH = "/cdn-cgi/trace"
# 单位：秒。端口与 Clash 都在本机，3 s 足够；trace 要出国际网络，单次连接 / 读写给 8 s。
PORT_TIMEOUT_SEC = 3
TRACE_TIMEOUT_SEC = 8
CLASH_TIMEOUT_SEC = 3
# trace 响应正常不到 1 KB；读满上限仍找不到 loc 就判失败，防止对方返回大文件。
MAX_RESPONSE_BYTES = 64 * 1024

# 跳过变量：值（去掉首尾空白后）为 "1" 时本次跳过并提示一行；其它值视为未设置。
SKIP_ENV = NETCHECK_SKIP_ENV
SKIP_PRINT = "1"
# 测试钩子（只在 MULTI_CLAUDE_TEST_MODE=1 时生效）：trace 目标改为本地假服务器 HOST:PORT；TLS 校验改用指定的 CA 文件。
TEST_TRACE_ENV = "MULTI_CLAUDE_TEST_TRACE"
TEST_CA_ENV = "MULTI_CLAUDE_TEST_CA"

ITEM_CLASH = "clash"
ITEM_PORT = "proxy-port"
ITEM_DIRECT = "direct-country"
ITEM_PROXY = "proxy-country"

# 3.8/3.9 上 socket.timeout 与 TimeoutError 是两个类，3.10 起才是别名，两个都要接。
_TIMEOUTS = (socket.timeout, TimeoutError)


class CheckResult(NamedTuple):
    """一项检查的结果；detail 是给人看的说明（失败时含错误分类），不含代理密码与 Clash secret。"""

    item: str
    ok: bool
    detail: str
    elapsed_ms: int


class CheckFailure(Exception):
    """检查项未通过；消息即对外的失败说明。

    kind 是失败类别，只有 Clash 接口读取设置（read_clash_state）：unreachable 连不上 / 超时，auth 被拒（401），
    http 其它非 200，not-clash 回包不像 mihomo，other 其它。启动检查只用消息文字；自动查找（clashfind）
    按 kind 写每个候选的结果说明，不靠匹配文字。
    """

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


class _Line(NamedTuple):
    """Claude 访问 api.anthropic.com 实际走的线路。"""

    proxy: "netcheck.Proxy"
    env: dict
    # 请求不发往 api.anthropic.com 时的去向说明（ANTHROPIC_BASE_URL 主机名或云厂商名），否则 None
    endpoint: Optional[str]


def skip_mode() -> bool:
    """跳过变量是否为 1。先 strip()：cmd 的 `set X=1 && …` 会把 && 前的空格算进值里。"""
    return os.environ.get(SKIP_ENV, "").strip() == SKIP_PRINT


def scope_text(account: Account) -> str:
    """提示行括号里的检查范围：basic / `strict, 国家`，开了 Clash 再加一段；只开 Clash 时只有这一段。

    Clash 段：两项都开写 `clash`，只开一项写 `clash tun` / `clash global`。
    """
    check = account.net_check
    parts = []
    if check.level == "basic":
        parts.append("basic")
    elif check.level == "strict":
        parts.extend(["strict", check.country])
    if check.clash_tun and check.clash_global:
        parts.append("clash")
    elif check.clash_tun:
        parts.append("clash tun")
    elif check.clash_global:
        parts.append("clash global")
    return ", ".join(parts)


def endpoint_note(config: Config, account: Account) -> str:
    """请求不发往 api.anthropic.com 的账号，在通过行 / 失败行末尾附的一句；其它情况返回空串。

    只在检查代理（basic / strict）时附：只开 Clash 时没有按 api.anthropic.com 检查任何东西。
    """
    if account.net_check.level not in ("basic", "strict"):
        return ""
    path = netcheck.settings_path(config, account)
    env, _, _ = netcheck.account_route_env(config, account, os.environ, path)
    place = netcheck.custom_endpoint(env)
    if not place:
        return ""
    return "; requests go to {}; checked {} only".format(place, TRACE_HOST)


def run_checks(config: Config, account: Account, stop_on_failure: bool) -> List[CheckResult]:
    """按 clash → proxy-port → direct-country → proxy-country 的顺序检查账号开启的项。

    stop_on_failure=True 用于放行判断：第一项失败就停，失败时尽快拒绝启动；
    `netcheck NAME` 诊断用 False，逐项列出全部结果。不重试：这是一次性的放行判断，重试只会拖慢启动。
    经代理查国家依赖 proxy-port 求出的线路；线路求不出（proxy-port 失败在第 0–3 条）时，proxy-country 记同一原因。
    """
    check = account.net_check
    state = {}  # type: dict

    def line() -> _Line:
        # 只算一次：proxy-port 与 proxy-country 用同一条线路，避免两次读 settings.json 之间文件被改导致不一致。
        if "line" not in state:
            try:
                state["line"] = _effective_line(config, account)
            except CheckFailure as exc:
                state["line"] = exc
        value = state["line"]
        if isinstance(value, CheckFailure):
            raise CheckFailure(str(value))
        return value

    steps = []
    if check.clash:
        steps.append((ITEM_CLASH, lambda: _check_clash(config, check.clash_tun, check.clash_global)))
    if check.level in ("basic", "strict"):
        steps.append((ITEM_PORT, lambda: _check_port(line())))
    if check.level == "strict":
        steps.append((ITEM_DIRECT, lambda: _check_country(None, None, check.country)))
        steps.append((ITEM_PROXY, lambda: _check_country(line().proxy, line().env, check.country)))
    results = []
    for item, step in steps:
        start = time.monotonic()
        try:
            detail = step()
            ok = True
        except CheckFailure as exc:
            detail = str(exc)
            ok = False
        results.append(CheckResult(item, ok, detail, int((time.monotonic() - start) * 1000)))
        if not ok and stop_on_failure:
            break
    return results


def gate(config: Config, account: Account, hint: str) -> Optional[str]:
    """启动前的放行判断：返回 None 表示放行，否则返回拒绝说明（多行，调用方打印到 stderr 并退出 5）。

    未开启检查 → 直接放行，不读跳过变量。跳过变量为 1 → 放行并提示一行，同时从 os.environ 删掉它：
    在 Python 里启动 Claude 的路径不让 Claude 继承“跳过”（调用方另从已算好的子进程环境里删）。
    通过时向 stderr 打一行“passed”。hint 是用户本次运行的命令，用于拼出“本次跳过”的写法。
    KeyboardInterrupt 不在这里处理，由调用方转成退出码 130。
    """
    if not account.net_check.active:
        return None
    if skip_mode():
        os.environ.pop(SKIP_ENV, None)
        _print_stderr("multi-claude: network check skipped for this run ({}=1)".format(SKIP_ENV))
        return None
    start = time.monotonic()
    results = run_checks(config, account, stop_on_failure=True)
    note = _safe_endpoint_note(config, account)
    failed = [result for result in results if not result.ok]
    if not failed:
        _print_stderr("multi-claude: network check passed for account {} ({}; {} ms){}".format(
            account.name, scope_text(account), int((time.monotonic() - start) * 1000), note))
        return None
    return failure_text(account, results, hint, note)


def _safe_endpoint_note(config: Config, account: Account) -> str:
    # 附注只是提示，求它出错（如 settings.json 读不出，proxy-port 已报）不能影响放行判断本身。
    try:
        return endpoint_note(config, account)
    except Exception:
        return ""


def failure_text(account: Account, results: List[CheckResult], hint: str, note: str = "") -> str:
    """拼出检查不通过时给用户看的多行说明：第一行写账号、档位和第一个失败项及原因，
    之后依次是已通过的项、本次跳过的写法（Windows 给 PowerShell 与 cmd 两种）和关闭检查的命令。
    results 里至少要有一个失败项。"""
    failed = [result for result in results if not result.ok][0]
    lines = ["multi-claude: network check failed for account {} ({}): {}: {} ({} ms){}".format(
        account.name, scope_text(account), failed.item, failed.detail, failed.elapsed_ms, note)]
    passed = [result.item for result in results if result.ok]
    if passed:
        lines.append("  passed: " + ", ".join(passed))
    if platform.IS_WINDOWS:
        lines.append("  skip once (PowerShell): try {{ $env:{0}=1; {1} }} finally {{ Remove-Item Env:{0} }}".format(
            SKIP_ENV, hint))
        lines.append('  skip once (cmd):        cmd /c "set {}=1&& {}"'.format(SKIP_ENV, hint))
    else:
        lines.append("  skip once: {}=1 {}".format(SKIP_ENV, hint))
    turn_off = "multi-claude netcheck {} off".format(account.name)
    if account.net_check.clash:
        turn_off += " --clash off"
    lines.append("  turn off:  " + turn_off)
    return "\n".join(lines)


def _print_stderr(message: str) -> None:
    """提示写 stderr：stdout 可能被调用方当作 Claude 的输出读取（如 `claude-work -p … > out`）。"""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- 实际线路


def proxy_label(proxy: "netcheck.Proxy") -> str:
    """代理的对外写法：只含 scheme、主机端口、变量名与来源，不含用户名密码。"""
    return "{}://{} from {} ({})".format(proxy.scheme, netcheck.authority(proxy.host, proxy.port),
                                         proxy.variable, proxy.source)


def setting_warning(config: Config, account: Account) -> Optional[str]:
    """设置检查时的提醒：此刻算出的实际代理会让启动被拒时，返回一句说明；否则返回 None。

    只提醒、不拒绝设置（方案 §5.1.5）：实际代理可能来自终端或 settings.json，要到启动时才能定。
    """
    if account.net_check.level not in ("basic", "strict"):
        return None
    try:
        line = _effective_line(config, account)
    except CheckFailure as exc:
        return "launches of {} will be refused unless this changes: {}".format(account.name, exc)
    if account.net_check.level == "strict" and line.proxy.scheme != "http":
        return ("the strict check needs an http:// proxy, but {} uses {}; launches of {} will be refused".format(
            account.name, proxy_label(line.proxy), account.name))
    return None


def _effective_line(config: Config, account: Account) -> _Line:
    """按方案 §3 判定规则第 0–3 条求 Claude 访问 api.anthropic.com 实际走的代理；不满足时抛 CheckFailure。"""
    path = netcheck.settings_path(config, account)
    env, sources, unreadable = netcheck.account_route_env(config, account, os.environ, path)
    if unreadable:
        # 放行判断不能忽略这份设置：它可能把代理改到别处，忽略就可能放过 Claude 实际会走的线路。
        raise CheckFailure("cannot read {} (not valid JSON, or env is not an object); fix it so the check "
                           "knows which proxy Claude uses".format(path))
    parsed = netcheck.parse_proxy(env, sources)
    if parsed is None:
        raise CheckFailure("no proxy in effect: the proxy setting of {} is {}, and neither the terminal nor {} "
                           "sets HTTPS_PROXY / HTTP_PROXY; set one with `multi-claude proxy {} <port>`".format(
                               account.name, account.proxy, path, account.name))
    if not isinstance(parsed, netcheck.Proxy):
        code, label = parsed
        if code == "unsupported_socks":
            raise CheckFailure("{} is a SOCKS proxy, which Claude Code does not support; use an http proxy".format(
                label))
        raise CheckFailure("{} is not a valid proxy URL and Claude Code will fail to start; "
                           "use http://host:port".format(label))
    endpoint = netcheck.custom_endpoint(env)
    if endpoint is None and netcheck.route(parsed, env, TRACE_HOST).proxy is None:
        # Claude 对这个目标实际直连：代理端口通也没用。请求发往别处的账号不判这条：绕过一个它不访问的地址与它无关。
        raise CheckFailure("{} is bypassed by NO_PROXY, so Claude connects directly instead of through {}; "
                           "remove it from NO_PROXY".format(TRACE_HOST, proxy_label(parsed)))
    return _Line(parsed, env, endpoint)


# ---------------------------------------------------------------- 各检查项


def _check_port(line: _Line) -> str:
    proxy = line.proxy
    try:
        sock = socket.create_connection((proxy.host, proxy.port), PORT_TIMEOUT_SEC)
    except OSError as exc:
        raise CheckFailure("{} not reachable: {}".format(proxy_label(proxy), _classify(exc)))
    sock.close()
    return "{} reachable".format(proxy_label(proxy))


def _trace_target() -> Tuple[str, int]:
    override = platform.test_hook_value(TEST_TRACE_ENV)
    if override:
        host, _, port = override.rpartition(":")
        return host, int(port)
    return TRACE_HOST, TRACE_PORT


def _tls_context(env: Optional[dict]) -> ssl.SSLContext:
    """默认证书；测试时改用测试 CA。经代理时再加载 NODE_EXTRA_CA_CERTS（公司代理换证书的场景，
    Claude Code 也信任它）；文件读不出就不加载，与 doctor --network 的处理一致。"""
    context = ssl.create_default_context(cafile=platform.test_hook_value(TEST_CA_ENV) or None)
    extra = netcheck.env_value(env, "NODE_EXTRA_CA_CERTS") if env is not None else ""
    if extra and netcheck.usable_ca(extra):
        context.load_verify_locations(extra)
    return context


class _TunnelHTTPSConnection(http.client.HTTPSConnection):
    """在已建好的 TLS 连接上发请求：CONNECT 隧道与 TLS 由 _open_via_proxy 完成，这里只负责 HTTP。"""

    def __init__(self, host: str, port: int, sock: socket.socket) -> None:
        super().__init__(host, port, timeout=TRACE_TIMEOUT_SEC)
        self._ready = sock

    def connect(self) -> None:
        self.sock = self._ready


def _open_via_proxy(proxy: "netcheck.Proxy", host: str, port: int, context: ssl.SSLContext) -> socket.socket:
    """TCP 连代理 → CONNECT（带 Proxy-Authorization）→ 按目标主机名 TLS 握手；失败抛 CheckFailure。"""
    try:
        sock = socket.create_connection((proxy.host, proxy.port), TRACE_TIMEOUT_SEC)
    except OSError as exc:
        raise CheckFailure("{} not reachable: {}".format(proxy_label(proxy), _classify(exc)))
    try:
        failure = netcheck.tunnel(sock, host, port, proxy.authorization)
        if failure is not None:
            raise CheckFailure(_tunnel_text(failure, proxy))
        try:
            return context.wrap_socket(sock, server_hostname=host)
        except OSError as exc:
            raise CheckFailure("{} (via {}): {}".format(TRACE_PATH, proxy_label(proxy), _classify(exc)))
    except BaseException:
        sock.close()
        raise


def _tunnel_text(failure: Tuple[str, Optional[int]], proxy: "netcheck.Proxy") -> str:
    code, status = failure
    via = proxy_label(proxy)
    if code == "proxy_auth":
        return "the proxy {} asks for a user name and password (407)".format(via)
    if code == "proxy_upstream_timeout":
        return "the proxy {} did not connect to {} in time".format(via, TRACE_HOST)
    if code == "proxy_bad_reply":
        return "the proxy {} did not answer like an http proxy; check the port".format(via)
    if status is not None:
        return "the proxy {} could not reach {} (CONNECT {})".format(via, TRACE_HOST, status)
    return "the proxy {} closed the connection instead of reaching {}".format(via, TRACE_HOST)


def _check_country(proxy: Optional["netcheck.Proxy"], env: Optional[dict], country: str) -> str:
    """访问 trace 地址，要求 loc 等于 country。proxy 为 None 时直连（不读任何代理设置）。"""
    host, port = _trace_target()
    context = _tls_context(env)
    if proxy is None:
        via = "direct"
        conn = http.client.HTTPSConnection(host, port, timeout=TRACE_TIMEOUT_SEC, context=context)
    else:
        via = "via " + proxy_label(proxy)
        if proxy.scheme != "http":
            # https:// 代理要 TLS 套 TLS，标准库做不到。
            raise CheckFailure("{}: {} proxies are not supported by the strict check; use an http:// proxy".format(
                proxy_label(proxy), proxy.scheme))
        conn = _TunnelHTTPSConnection(host, port, _open_via_proxy(proxy, host, port, context))
    try:
        try:
            conn.request("GET", TRACE_PATH, headers={
                "User-Agent": "multi-claude/" + __version__, "Connection": "close"})
            response = conn.getresponse()
            body = response.read(MAX_RESPONSE_BYTES)
        except (OSError, http.client.HTTPException) as exc:
            raise CheckFailure("{} ({}): {}".format(TRACE_PATH, via, _classify(exc)))
    finally:
        conn.close()
    if response.status != 200:
        raise CheckFailure("{} ({}) returned HTTP {}".format(TRACE_PATH, via, response.status))
    loc = parse_loc(body)
    if loc is None:
        raise CheckFailure("{} ({}) has no loc= line".format(TRACE_PATH, via))
    if loc.upper() != country.upper():
        raise CheckFailure("loc is {}, expected {} ({})".format(loc, country, via))
    return "loc={} ({})".format(loc, via)


def parse_loc(body: bytes) -> Optional[str]:
    """从 trace 响应（逐行 key=value）里取 loc 的值。"""
    for line in body.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "loc":
            return value.strip()
    return None


def _classify(exc: BaseException) -> str:
    """把底层异常转成带分类的说明；子类在前，ssl / timeout 都是 OSError 的子类。"""
    if isinstance(exc, socket.gaierror):
        return "DNS lookup failed ({})".format(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return ("TLS certificate verification failed ({}); Python may not find the system CA "
                "certificates".format(exc.verify_message or exc))
    if isinstance(exc, ssl.SSLError):
        return "TLS error ({})".format(exc)
    if isinstance(exc, _TIMEOUTS):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, http.client.HTTPException):
        return "bad HTTP response ({})".format(exc.__class__.__name__)
    return "{} ({})".format(exc.__class__.__name__, exc)


# ---------------------------------------------------------------- Clash / mihomo


def _check_clash(config: Config, need_tun: bool, need_global: bool) -> str:
    """按账号开启的两项判定 Clash 状态。只读，不修改 Clash 的任何设置。

    need_global：mode 必须是 global。need_tun：tun.enable 必须为真；只要求 TUN 时还要拒绝 direct 模式——
    direct 下 TUN 接住的流量照样直连出去，等于没开代理。只要求全局时不看 TUN（设置时已提醒其局限）。
    """
    api = config.clash_api
    if api is None:
        raise CheckFailure("Clash controller is not set; run `multi-claude clash-api auto` or "
                           "`multi-claude clash-api ADDRESS`")
    mode, tun_on = read_clash_state(api.address, api.secret)
    if need_global and mode != "global":
        raise CheckFailure("Clash mode is {}, expected global".format(mode))
    if need_tun and not tun_on:
        raise CheckFailure("Clash TUN is off")
    if need_tun and not need_global and mode == "direct":
        raise CheckFailure("Clash mode is direct; TUN would pass traffic through without the proxy")
    return "mode={}, tun {}".format(mode, "on" if tun_on else "off")


def read_clash_state(address: str, secret: str) -> Tuple[str, bool]:
    """GET /configs，返回 (mode 小写, tun.enable 是否为真)（mihomo v1.19.32 hub/route/configs.go）。

    TUN 启动失败时 mihomo 会把 tun.enable 写回 false（listener/listener.go），所以它反映的是实际状态。
    失败抛 CheckFailure 并按类别设 kind；启动检查与 `clash-api auto` 的候选验证共用这一个读法。
    """
    try:
        status, body = _clash_get(address, secret)
    except CheckFailure as exc:
        # _pipe_get 自己抛的（管道超时等）都属于连不上。
        raise CheckFailure(str(exc), "unreachable")
    except (OSError, http.client.HTTPException) as exc:
        raise CheckFailure("Clash controller {}: {}".format(address, _classify(exc)), "unreachable")
    except UnicodeError:
        # http.client 按 latin-1 编码请求头，secret 含中文等字符时在发送前就抛 UnicodeEncodeError；
        # 自动查找会从别人写的配置文件里读到这种 secret，不兜住会带 traceback 退出、后面的候选也不再试。
        # 说明里不带 secret 原文。
        raise CheckFailure("Clash controller {}: the secret has characters that cannot be sent in an HTTP "
                           "header".format(address), "other")
    if status == 401:
        raise CheckFailure("Clash controller {} rejected the request (401): secret missing or wrong; "
                           "set it with `multi-claude clash-api ADDRESS --secret-stdin`".format(address), "auth")
    if status != 200:
        raise CheckFailure("Clash controller {} returned HTTP {}".format(address, status), "http")
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        data = None
    mode = data.get("mode") if isinstance(data, dict) else None
    tun = data.get("tun") if isinstance(data, dict) else None
    if not isinstance(mode, str) or not isinstance(tun, dict) or "enable" not in tun:
        raise CheckFailure("Clash controller {} gave an unexpected response (no mode / tun.enable); "
                           "is it a Clash / mihomo controller?".format(address), "not-clash")
    return mode.lower(), tun.get("enable") is True


def _clash_get(address: str, secret: str) -> Tuple[int, bytes]:
    headers = {"Connection": "close", "User-Agent": "multi-claude/" + __version__}
    if address.lower().startswith(PIPE_PREFIX):  # 规范化时按不区分大小写接受，原样保存
        return _pipe_get(address, headers)
    if address.startswith("unix:"):
        conn = _UnixHTTPConnection(address[len("unix:"):], CLASH_TIMEOUT_SEC)  # type: http.client.HTTPConnection
    else:
        parts = urllib.parse.urlsplit(address)
        conn = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=CLASH_TIMEOUT_SEC)
        # mihomo 只在 TCP 接口上校验 secret；unix 套接字与命名管道不鉴权，不发送。
        if secret:
            headers["Authorization"] = "Bearer " + secret
    try:
        conn.request("GET", "/configs", headers=headers)
        response = conn.getresponse()
        return response.status, response.read(MAX_RESPONSE_BYTES)
    finally:
        conn.close()


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _BufferSocket(object):
    """让 http.client.HTTPResponse 解析已读到内存里的响应：它只调用 makefile("rb")。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def makefile(self, *args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(self._data)


def _parse_raw_response(data: bytes) -> Tuple[int, bytes]:
    """把已读全的原始 HTTP 响应字节交给 http.client 解析，返回（状态码，正文）；
    带 Content-Length 与分块编码两种回包都能处理。命名管道没有 socket 可读，所以先读全再解析。"""
    response = http.client.HTTPResponse(_BufferSocket(data))  # type: ignore
    response.begin()
    return response.status, response.read()


def _pipe_get(path: str, headers: dict) -> Tuple[int, bytes]:
    """经 Windows 命名管道发 GET /configs，读到 EOF 后再解析。

    请求带 Connection: close，mihomo 回完就关管道；读到空字节或 BrokenPipeError 都按读完处理。
    管道句柄的读写没有超时参数，所以放在守护线程里做，主线程最多等 CLASH_TIMEOUT_SEC；
    超时后线程仍可能阻塞在读上，它是守护线程，不阻止进程退出。
    """
    outcome = {}  # type: dict

    def worker() -> None:
        try:
            with open(path, "r+b", buffering=0) as handle:
                # 用 HTTP/1.0：服务端不会回分块编码，读到 EOF 即整份响应。
                request = "GET /configs HTTP/1.0\r\nHost: localhost\r\n" + "".join(
                    "{}: {}\r\n".format(key, value) for key, value in headers.items()) + "\r\n"
                handle.write(request.encode("ascii"))
                data = b""
                while len(data) < MAX_RESPONSE_BYTES:
                    try:
                        chunk = handle.read(MAX_RESPONSE_BYTES - len(data))
                    except BrokenPipeError:
                        break
                    if not chunk:
                        break
                    data += chunk
            outcome["result"] = _parse_raw_response(data)
        except BaseException as exc:  # 交回主线程分类
            outcome["error"] = exc

    thread = threading.Thread(target=worker, name="launchgate-pipe", daemon=True)
    thread.start()
    thread.join(CLASH_TIMEOUT_SEC)
    if thread.is_alive():
        raise CheckFailure("Clash controller {}: timed out".format(path))
    if "error" in outcome:
        error = outcome["error"]
        if isinstance(error, (OSError, http.client.HTTPException)):
            raise error
        raise CheckFailure("Clash controller {}: {}".format(path, error))
    return outcome["result"]
