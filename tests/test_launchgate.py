"""启动前网络检查的回归（方案 docs/feature/feature-launch-gate.md §8.1，用例 R-A1…R-C5）。

本地起假服务代替真实网络：TLS 假 trace（/cdn-cgi/trace，测试证书的 SAN 含 127.0.0.1）、假 HTTP CONNECT 代理、
假 mihomo 外部控制接口（TCP / unix 套接字）。子进程经 MULTI_CLAUDE_TEST_TRACE（HOST:PORT）/
MULTI_CLAUDE_TEST_CA 连到它们；这两个钩子只在 MULTI_CLAUDE_TEST_MODE=1 时生效（platform.test_hook_value）。
Clash 自动查找只看 MULTI_CLAUDE_TEST_CLASH_PROCESSES 指向的假进程列表，不枚举开发机上的真实进程。

“实际代理”判定检查的是 api.anthropic.com 是否被 NO_PROXY 绕过（launchgate._effective_line），trace 钩子只替换
请求真正发往的主机端口；所以 launcher 模式在 NO_PROXY 里追加的 127.0.0.1 不影响经代理的检查。
"""

import http.server
import json
import os
import shlex
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from helpers import LAUNCHER_SUFFIX, ROOT, SRC, TEXT_KWARGS, WINDOWS, CliTestCase, posix_only, windows_only

sys.path.insert(0, SRC)

from multi_claude import launcher, launchgate, platform  # noqa: E402
from multi_claude.config import (ConfigError, normalize_clash_address, parse_config,  # noqa: E402
                                 validate_env_key)

DATA = os.path.join(ROOT, "tests", "data")
CERT = os.path.join(DATA, "launchgate-cert.pem")
KEY = os.path.join(DATA, "launchgate-key.pem")
SKIP = launchgate.SKIP_ENV
# 测试证书的 SAN 是 IP:127.0.0.1 / ::1 / DNS:localhost；trace 钩子用 IP，避免 localhost 先解析到 ::1。
TRACE_HOST = "127.0.0.1"
SECRET = "SECRET-p4ss"


# ---------------------------------------------------------------- 假服务

class _Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # 不往测试输出里写访问日志；unix 套接字下 address_string 也会出错
        pass

    def reply(self, status, body):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _TraceHandler(_Quiet):
    def do_GET(self):
        server = self.server
        with server.lock:
            server.count += 1
        if self.path != "/cdn-cgi/trace":
            self.reply(404, "not found")
        elif server.status != 200:
            self.reply(server.status, "error")
        else:
            lines = ["fl=1", "h=api.anthropic.com", "ip=203.0.113.9"]
            if server.loc is not None:
                lines.append("loc=" + server.loc)
            self.reply(200, "\n".join(lines) + "\n")


class _ThreadingHTTP(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class TraceServer(object):
    """本地 TLS 假 trace 服务；loc / status 可随时改，count 记录收到的请求数。"""

    def __init__(self, loc="US"):
        self.httpd = _ThreadingHTTP((TRACE_HOST, 0), _TraceHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.httpd.lock = threading.Lock()
        self.httpd.count = 0
        self.httpd.loc = loc
        self.httpd.status = 200
        self.port = self.httpd.server_address[1]
        self.hook = "{}:{}".format(TRACE_HOST, self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def count(self):
        return self.httpd.count

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _relay(left, right):
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    thread = threading.Thread(target=pump, args=(right, left), daemon=True)
    thread.start()
    pump(left, right)
    thread.join(10)
    left.close()
    right.close()


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return  # 端口检查：连上就关
            head += chunk
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        self.server.targets.append(target)
        self.server.headers.append(headers)
        if method != "CONNECT":
            sock.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        if self.server.status is not None:
            sock.sendall("HTTP/1.1 {} Nope\r\nContent-Length: 0\r\n\r\n".format(self.server.status).encode())
            return
        host, _, port = target.rpartition(":")
        # redirect 不为空时把隧道接到另一个端口：模拟“经代理出口与直连出口不同”。
        upstream = socket.create_connection((host, self.server.redirect or int(port)), 5)
        sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        _relay(sock, upstream)


class _ThreadingTCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ProxyServer(object):
    """假 http 代理（CONNECT）。status 为数字时回该状态码；redirect 把隧道转到另一个端口；targets 记录请求目标。"""

    def __init__(self):
        self.server = _ThreadingTCP(("127.0.0.1", 0), _ConnectHandler)
        self.server.targets = []
        self.server.headers = []
        self.server.status = None
        self.server.redirect = None
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:{}".format(self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class _MihomoHandler(_Quiet):
    def do_GET(self):
        server = self.server
        if server.secret and self.headers.get("Authorization") != "Bearer " + server.secret:
            self.reply(401, '{"message":"Unauthorized"}')
            return
        if self.path != "/configs":
            self.reply(404, "{}")
            return
        self.reply(200, json.dumps(server.payload))


class MihomoServer(object):
    """假 mihomo 外部控制接口（GET /configs）；unix_path 不为空时监听 unix 套接字。"""

    def __init__(self, unix_path=None):
        if unix_path:
            class _UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                daemon_threads = True
            self.server = _UnixHTTP(unix_path, _MihomoHandler)
            self.address = "unix:" + unix_path
        else:
            self.server = _ThreadingHTTP(("127.0.0.1", 0), _MihomoHandler)
            self.address = "127.0.0.1:{}".format(self.server.server_address[1])
        self.server.secret = ""
        self.server.payload = {"mode": "global", "tun": {"enable": True, "stack": "mixed"}, "port": 7890}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _network_variable(key):
    """会影响“实际代理”判定的环境变量：进程内用例开始前全部清掉，开发机终端里的代理不能混进来。"""
    upper = key.upper()
    return (upper.endswith("_PROXY") or upper.startswith("ANTHROPIC_") or upper.startswith("CLAUDE")
            or upper == "NODE_EXTRA_CA_CERTS" or upper.startswith("MULTI_CLAUDE_"))


# ---------------------------------------------------------------- 进程内：检查项本身

class ChecksTest(unittest.TestCase):
    """直接调用 launchgate.run_checks；环境变量在本进程里设置，结束时还原。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="mlg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.trace = TraceServer()
        self.addCleanup(self.trace.stop)
        clean = {key: value for key, value in os.environ.items() if not _network_variable(key)}
        clean.update({platform.TEST_MODE_ENV: "1", launchgate.TEST_TRACE_ENV: self.trace.hook,
                      launchgate.TEST_CA_ENV: CERT})
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, proxy="inherit", level="off", country="US", clash=None, clash_api=None, clash_tun=None,
               clash_global=None, settings=None):
        """clash 写旧字段（两项一起），clash_tun / clash_global 写新字段；settings 写账号目录的 settings.json。"""
        net_check = {"level": level, "country": country}
        if clash is not None:
            net_check["clash"] = clash
        if clash_tun is not None:
            net_check["clash_tun"] = clash_tun
        if clash_global is not None:
            net_check["clash_global"] = clash_global
        data = {"root": os.path.join(self.tmp, "root"), "bin_dir": os.path.join(self.tmp, "bin"),
                "accounts": {"work": {"proxy": proxy, "net_check": net_check}}}
        if clash_api is not None:
            data["clash_api"] = clash_api
        config = parse_config(json.dumps(data), "test")
        directory = os.path.join(self.tmp, "root", "work")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "settings.json")
        if settings is not None:
            with open(path, "wb") as handle:
                handle.write(settings if isinstance(settings, bytes) else json.dumps(settings).encode("utf-8"))
        elif os.path.exists(path):
            os.unlink(path)
        return config, config.find("work")

    def results(self, *args, **kwargs):
        config, account = self.config(*args, **kwargs)
        return launchgate.run_checks(config, account, stop_on_failure=False)

    def by_item(self, results):
        return {result.item: result for result in results}

    def proxy(self):
        proxy = ProxyServer()
        self.addCleanup(proxy.stop)
        return proxy

    # R-B1 basic：端口开 / 关、NO_PROXY 绕过

    def test_port_open_and_refused(self):
        """R-B1：实际代理端口开着 → proxy-port 通过；关着 → 失败并写明 connection refused。"""
        proxy = self.proxy()
        (result,) = self.results(proxy.url, "basic")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.item, "proxy-port")
        self.assertIn("http://127.0.0.1:{} from".format(proxy.port), result.detail)
        self.assertIn("(launcher)", result.detail)
        (result,) = self.results("http://127.0.0.1:{}".format(closed_port()), "basic")
        self.assertFalse(result.ok)
        self.assertIn("connection refused", result.detail)

    def test_no_proxy_bypass(self):
        """R-B1：NO_PROXY 含 api.anthropic.com → basic 与 strict 都在 proxy-port 失败；代理端口开着也一样。"""
        proxy = self.proxy()
        for value in ("api.anthropic.com", "example.org .anthropic.com"):
            with self.subTest(no_proxy=value):
                os.environ["NO_PROXY"] = os.environ["no_proxy"] = value
                for level in ("basic", "strict"):
                    found = self.by_item(self.results(proxy.url, level))
                    self.assertFalse(found["proxy-port"].ok, found)
                    self.assertIn("api.anthropic.com is bypassed by NO_PROXY", found["proxy-port"].detail)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "example.org"
        (result,) = self.results(proxy.url, "basic")
        self.assertTrue(result.ok, result)

    # R-B2 实际代理

    def test_unreadable_settings(self):
        """R-B2：settings.json 读不出（JSON 损坏 / env 不是对象 / 顶层不是对象）→ proxy-port 失败并写出路径；不存在不算读不出。"""
        proxy = self.proxy()
        path = os.path.join(self.tmp, "root", "work", "settings.json")
        for content in (b"{not json", b'{"env": []}', b"[1]"):
            with self.subTest(content=content):
                (result,) = self.results(proxy.url, "basic", settings=content)
                self.assertFalse(result.ok, result)
                self.assertIn("cannot read " + path, result.detail)
        (result,) = self.results(proxy.url, "basic", settings={"permissions": {}})
        self.assertTrue(result.ok, result)
        (result,) = self.results(proxy.url, "basic")
        self.assertTrue(result.ok, result)

    def test_inherit_uses_shell_proxy(self):
        """R-B2：账号 inherit + 终端有代理 → 查终端代理，来源标 shell；终端也没有 → no proxy in effect。"""
        proxy = self.proxy()
        (result,) = self.results("inherit", "basic")
        self.assertFalse(result.ok)
        self.assertIn("no proxy in effect", result.detail)
        self.assertIn("proxy setting of work is inherit", result.detail)
        os.environ["HTTPS_PROXY"] = proxy.url
        (result,) = self.results("inherit", "basic")
        self.assertTrue(result.ok, result)
        self.assertIn("from HTTPS_PROXY (shell)", result.detail)

    def test_settings_override_launcher_proxy(self):
        """R-B2：settings.json 的 env 覆盖启动命令代理 → 查 settings 的端口，文案标 settings.json。"""
        proxy = self.proxy()
        dead = "http://127.0.0.1:{}".format(closed_port())
        settings = {"env": {"https_proxy": proxy.url, "HTTPS_PROXY": proxy.url}}
        (result,) = self.results(dead, "basic", settings=settings)
        self.assertTrue(result.ok, result)
        self.assertIn("(settings.json)", result.detail)
        self.assertIn(str(proxy.port), result.detail)
        # 反向：启动命令代理能连、settings 指向死端口 → 失败，按 settings 的端口判定
        settings = {"env": {"https_proxy": dead, "HTTPS_PROXY": dead}}
        (result,) = self.results(proxy.url, "basic", settings=settings)
        self.assertFalse(result.ok, result)
        self.assertIn("(settings.json)", result.detail)

    def test_settings_empty_proxy(self):
        """R-B2：settings.json 把所有代理变量设成空 → no proxy in effect（即使启动命令配了代理）。"""
        proxy = self.proxy()
        empty = {name: "" for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")}
        (result,) = self.results(proxy.url, "basic", settings={"env": empty})
        self.assertFalse(result.ok, result)
        self.assertIn("no proxy in effect", result.detail)
        (result,) = self.results("off", "basic")
        self.assertIn("no proxy in effect", result.detail)

    def test_socks_and_invalid_proxy(self):
        """R-B2：实际代理是 SOCKS → 失败并说明 Claude Code 不支持；写法不对 → 说明不是合法 URL；都不带原值。"""
        cases = (("socks5://alice:{}@127.0.0.1:1080".format(SECRET), "is a SOCKS proxy"),
                 ("alice:{}@h:1".format(SECRET), "is not a valid proxy URL"))
        for value, fragment in cases:
            with self.subTest(value=value):
                os.environ["HTTPS_PROXY"] = value
                (result,) = self.results("inherit", "basic")
                self.assertFalse(result.ok)
                self.assertIn(fragment, result.detail)
                self.assertNotIn(SECRET, result.detail)
                self.assertNotIn("alice", result.detail)

    def test_custom_endpoint_note(self):
        """R-B2：设了 ANTHROPIC_BASE_URL → 照常检查 api.anthropic.com，附注写明去向；此时不判 NO_PROXY 绕过。"""
        proxy = self.proxy()
        config, account = self.config(proxy.url, "basic",
                                      settings={"env": {"ANTHROPIC_BASE_URL": "https://gw.example.com/v1"}})
        self.assertEqual(launchgate.endpoint_note(config, account),
                         "; requests go to gw.example.com; checked api.anthropic.com only")
        os.environ["NO_PROXY"] = "api.anthropic.com"
        (result,) = launchgate.run_checks(config, account, stop_on_failure=False)
        self.assertTrue(result.ok, result)
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        self.assertIn("Bedrock", launchgate.endpoint_note(config, account))
        # 只开 Clash 时不附：没有按 api.anthropic.com 检查任何东西
        config, account = self.config(proxy.url, "off", clash_tun=True,
                                      settings={"env": {"ANTHROPIC_BASE_URL": "https://gw.example.com"}})
        self.assertEqual(launchgate.endpoint_note(config, account), "")

    def test_no_secret_in_detail(self):
        """R-B2 / §5.1.2：detail 只写 scheme://host:port 与变量名、来源，不写用户名密码。"""
        proxy = self.proxy()
        os.environ["HTTPS_PROXY"] = "http://alice:{}@127.0.0.1:{}".format(SECRET, proxy.port)
        for result in self.results("inherit", "strict"):
            self.assertNotIn(SECRET, result.detail, result)
            self.assertNotIn("alice", result.detail, result)
        self.assertIn("Proxy-Authorization", proxy.server.headers[-1])

    # R-B3 strict

    def test_strict_pass(self):
        """R-B3：直连与经代理的 loc 相同且等于设定国家 → 三项都过；国家比较不分大小写。"""
        proxy = self.proxy()
        found = self.by_item(self.results(proxy.url, "strict"))
        self.assertEqual(sorted(found), ["direct-country", "proxy-country", "proxy-port"])
        self.assertTrue(all(result.ok for result in found.values()), found)
        self.assertEqual(found["direct-country"].detail, "loc=US (direct)")
        self.assertIn("loc=US (via http://127.0.0.1:{}".format(proxy.port), found["proxy-country"].detail)
        self.assertEqual(proxy.server.targets, [self.trace.hook])
        self.trace.httpd.loc = "jp"
        found = self.by_item(self.results(proxy.url, "strict", country="jp"))
        self.assertTrue(found["proxy-country"].ok, found)

    def test_strict_proxy_country_differs(self):
        """R-B3：经代理的 loc 与直连不同 → proxy-country 失败，direct-country 仍过。"""
        proxy = self.proxy()
        other = TraceServer("JP")
        self.addCleanup(other.stop)
        proxy.server.redirect = other.port
        found = self.by_item(self.results(proxy.url, "strict"))
        self.assertTrue(found["direct-country"].ok, found)
        self.assertFalse(found["proxy-country"].ok)
        self.assertIn("loc is JP, expected US", found["proxy-country"].detail)
        self.assertEqual(other.count, 1)

    def test_strict_direct_country_and_trace_errors(self):
        """R-B3：直连 loc 不符、trace 无 loc、trace 非 200、证书不受信任 → direct-country 失败并分类说明。"""
        proxy = self.proxy()
        self.trace.httpd.loc = "SG"
        found = self.by_item(self.results(proxy.url, "strict"))
        self.assertIn("loc is SG, expected US (direct)", found["direct-country"].detail)
        self.trace.httpd.loc = None
        found = self.by_item(self.results(proxy.url, "strict"))
        self.assertIn("has no loc= line", found["direct-country"].detail)
        self.trace.httpd.status = 500
        found = self.by_item(self.results(proxy.url, "strict"))
        self.assertIn("returned HTTP 500", found["direct-country"].detail)
        self.trace.httpd.status = 200
        self.trace.httpd.loc = "US"
        del os.environ[launchgate.TEST_CA_ENV]
        found = self.by_item(self.results(proxy.url, "strict"))
        self.assertFalse(found["direct-country"].ok)
        self.assertIn("TLS certificate verification failed", found["direct-country"].detail)

    def test_strict_proxy_replies(self):
        """R-B3：代理 407 → 说明要用户名密码；502 → 说明 CONNECT 状态。"""
        proxy = self.proxy()
        for status, fragment in ((407, "asks for a user name and password (407)"), (502, "(CONNECT 502)")):
            with self.subTest(status=status):
                proxy.server.status = status
                found = self.by_item(self.results(proxy.url, "strict"))
                self.assertTrue(found["proxy-port"].ok, found)
                self.assertFalse(found["proxy-country"].ok)
                self.assertIn(fragment, found["proxy-country"].detail)

    def test_strict_https_proxy(self):
        """R-B3：实际代理是 https:// → proxy-country 失败并说明 strict 不支持；setting_warning 也提醒。"""
        proxy = self.proxy()
        url = "https://127.0.0.1:{}".format(proxy.port)
        found = self.by_item(self.results(url, "strict"))
        self.assertFalse(found["proxy-country"].ok)
        self.assertIn("https proxies are not supported by the strict check", found["proxy-country"].detail)
        config, account = self.config(url, "strict")
        self.assertIn("needs an http:// proxy", launchgate.setting_warning(config, account))
        config, account = self.config(url, "basic")
        self.assertIsNone(launchgate.setting_warning(config, account))

    def test_stop_on_failure_and_order(self):
        """R-B3 / §5.1.2：顺序 clash → proxy-port → direct-country → proxy-country；放行判断在第一个失败处停。"""
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        mihomo.server.payload = {"mode": "rule", "tun": {"enable": True}}
        config, account = self.config("http://127.0.0.1:{}".format(closed_port()), "strict", clash=True,
                                      clash_api={"address": mihomo.address})
        all_items = [result.item for result in launchgate.run_checks(config, account, stop_on_failure=False)]
        self.assertEqual(all_items, ["clash", "proxy-port", "direct-country", "proxy-country"])
        stopped = launchgate.run_checks(config, account, stop_on_failure=True)
        self.assertEqual([result.item for result in stopped], ["clash"])

    def test_proxy_country_shares_line_failure(self):
        """R-B3：线路求不出（无代理）时 proxy-country 记同一原因，不去连代理。"""
        found = self.by_item(self.results("off", "strict"))
        self.assertIn("no proxy in effect", found["proxy-country"].detail)
        self.assertTrue(found["direct-country"].ok, found)

    # R-B4 Clash

    def clash_result(self, mihomo, secret="", **flags):
        api = {"address": mihomo.address}
        if secret:
            api["secret"] = secret
        if not flags:
            flags = {"clash_tun": True, "clash_global": True}
        (result,) = self.results(clash_api=api, **flags)
        self.assertEqual(result.item, "clash")
        return result

    def test_clash_split_states(self):
        """R-B4：global+TUN 过；rule 下 --clash-global 失败、--clash-tun 过；TUN 关失败；direct 下 --clash-tun 失败。"""
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        cases = [  # (mode, tun 开, 要 TUN, 要全局) → 是否通过
            ("global", True, True, True, True), ("rule", True, False, True, False),
            ("rule", True, True, False, True), ("rule", False, True, False, False),
            ("direct", True, True, False, False), ("global", False, False, True, True),
            ("global", False, True, True, False), ("Global", True, True, True, True)]
        for mode, tun, need_tun, need_global, ok in cases:
            with self.subTest(mode=mode, tun=tun, need_tun=need_tun, need_global=need_global):
                mihomo.server.payload = {"mode": mode, "tun": {"enable": tun}}
                result = self.clash_result(mihomo, clash_tun=need_tun, clash_global=need_global)
                self.assertEqual(result.ok, ok, result.detail)
        mihomo.server.payload = {"mode": "rule", "tun": {"enable": True}}
        self.assertEqual(self.clash_result(mihomo, clash_tun=True, clash_global=False).detail, "mode=rule, tun on")
        self.assertIn("mode is rule, expected global", self.clash_result(mihomo).detail)
        mihomo.server.payload = {"mode": "global", "tun": {"enable": False}}
        self.assertIn("TUN is off", self.clash_result(mihomo).detail)
        mihomo.server.payload = {"mode": "direct", "tun": {"enable": True}}
        self.assertIn("mode is direct", self.clash_result(mihomo, clash_tun=True, clash_global=False).detail)
        mihomo.server.payload = {"hello": 1}
        self.assertIn("unexpected response", self.clash_result(mihomo).detail)

    def test_clash_secret_401(self):
        """R-B4：secret 对 → 过；错或缺 → 401 说明，不回显 secret。"""
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        mihomo.server.secret = "s3cret"
        self.assertTrue(self.clash_result(mihomo, "s3cret").ok)
        result = self.clash_result(mihomo, "badvalue")
        self.assertIn("rejected the request (401)", result.detail)
        self.assertNotIn("badvalue", result.detail)
        self.assertIn("--secret-stdin", self.clash_result(mihomo).detail)

    def test_clash_not_set_or_down(self):
        """R-B4：未设接口 → 提示 clash-api auto；接口连不上 → connection refused。"""
        (result,) = self.results(clash_tun=True)
        self.assertFalse(result.ok)
        self.assertIn("multi-claude clash-api auto", result.detail)
        result = self.clash_result(type("Down", (), {"address": "127.0.0.1:{}".format(closed_port())})())
        self.assertIn("connection refused", result.detail)

    def test_clash_legacy_field(self):
        """R-B4：旧字段 "clash": true 读作两项都开，rule 模式下按全局要求失败。"""
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        config, account = self.config(clash=True, clash_api={"address": mihomo.address})
        self.assertTrue(account.net_check.clash_tun and account.net_check.clash_global)
        self.assertEqual(launchgate.scope_text(account), "clash")
        mihomo.server.payload = {"mode": "rule", "tun": {"enable": True}}
        (result,) = launchgate.run_checks(config, account, stop_on_failure=False)
        self.assertFalse(result.ok)

    @posix_only
    def test_clash_unix_socket(self):
        """R-B4：unix 套接字接口可读；mihomo 的 unix 套接字不鉴权，配置里的 secret 不影响结果。"""
        mihomo = MihomoServer(os.path.join(self.tmp, "m.sock"))
        self.addCleanup(mihomo.stop)
        self.assertTrue(self.clash_result(mihomo, "ignored").ok)

    def test_scope_and_failure_text(self):
        """R-B1 / R-B4：提示行的检查范围写法；失败文案的 skip once / turn off（开了 Clash 时带 --clash off）。"""
        cases = [(("basic", False, False), "basic"), (("strict", False, False), "strict, US"),
                 (("strict", True, True), "strict, US, clash"), (("off", True, True), "clash"),
                 (("off", True, False), "clash tun"), (("off", False, True), "clash global"),
                 (("basic", False, True), "basic, clash global")]
        for (level, tun, global_), expected in cases:
            _, account = self.config("http://127.0.0.1:1", level, clash_tun=tun, clash_global=global_)
            self.assertEqual(launchgate.scope_text(account), expected)
        _, account = self.config("http://127.0.0.1:1", "basic")
        results = [launchgate.CheckResult("proxy-port", False, "x not reachable", 3)]
        text = launchgate.failure_text(account, results, "claude-work", "; note")
        lines = text.split("\n")
        self.assertEqual(lines[0], "multi-claude: network check failed for account work (basic): proxy-port: "
                                   "x not reachable (3 ms); note")
        if WINDOWS:
            self.assertIn("$env:{}=1; claude-work".format(SKIP), text)
            self.assertIn('cmd /c "set {}=1&& claude-work"'.format(SKIP), text)
        else:
            self.assertIn("  skip once: {}=1 claude-work".format(SKIP), lines)
        self.assertEqual(lines[-1], "  turn off:  multi-claude netcheck work off")
        _, account = self.config("http://127.0.0.1:1", "strict", clash_tun=True)
        results = [launchgate.CheckResult("clash", True, "ok", 1),
                   launchgate.CheckResult("proxy-port", False, "down", 1)]
        text = launchgate.failure_text(account, results, "claude-work")
        self.assertIn("  passed: clash", text.split("\n"))
        self.assertTrue(text.endswith("multi-claude netcheck work off --clash off"), text)

    def test_parse_loc_and_trace_hook(self):
        """§5.1.2：trace 响应取 loc=；测试钩子只在 MULTI_CLAUDE_TEST_MODE=1 时生效。"""
        self.assertEqual(launchgate.parse_loc(b"ip=1\nloc=SG\nwarp=off\n"), "SG")
        self.assertIsNone(launchgate.parse_loc(b"ip=1\nlocation=SG\n"))
        self.assertEqual(launchgate._trace_target(), (TRACE_HOST, self.trace.port))
        os.environ[platform.TEST_MODE_ENV] = "0"
        self.assertEqual(launchgate._trace_target(), ("api.anthropic.com", 443))

    def test_skip_mode_values(self):
        """R-B5：跳过变量只认 strip() 后为 1 的值。"""
        for value, expected in (("1", True), (" 1 ", True), ("1\t", True), ("quiet", False), ("0", False),
                                ("", False), ("true", False)):
            with self.subTest(value=value):
                os.environ[SKIP] = value
                self.assertEqual(launchgate.skip_mode(), expected)


class ConfigTest(unittest.TestCase):
    """R-C4 配置解析与 R-A1 启动命令基线。"""

    def test_round_trip_and_inactive_not_written(self):
        """R-C4 / R-A1：国家转大写；只写新字段；未开启的账号不写 net_check；copy 是深拷贝。"""
        raw = {"accounts": {"a": {"proxy": "http://127.0.0.1:7901",
                                  "net_check": {"level": "strict", "country": "jp", "clash": True}},
                            "b": {"proxy": "http://127.0.0.1:7902"},
                            "c": {"net_check": {"level": "off", "country": "SG"}}},
               "clash_api": {"address": "127.0.0.1:9097", "secret": "x"}}
        config = parse_config(json.dumps(raw), "t")
        data = config.to_dict()
        self.assertEqual(data["accounts"]["a"]["net_check"],
                         {"level": "strict", "country": "JP", "clash_tun": True, "clash_global": True})
        self.assertNotIn("net_check", data["accounts"]["b"])
        self.assertNotIn("net_check", data["accounts"]["c"])
        self.assertEqual(data["clash_api"], {"address": "http://127.0.0.1:9097", "secret": "x"})
        self.assertEqual(parse_config(json.dumps(data), "t").to_dict(), data)
        copied = config.find("a").copy()
        copied.net_check.level = "off"
        self.assertEqual(config.find("a").net_check.level, "strict")
        self.assertNotIn("clash_api", parse_config(json.dumps({"accounts": {}}), "t").to_dict())

    def test_invalid_values(self):
        """R-C4：非法 level / country / 布尔、非法 clash 地址 → ConfigError。"""
        for net_check in ({"level": "max"}, {"country": "USA"}, {"country": 1}, {"clash": "yes"},
                          {"clash_tun": "yes"}, {"clash_global": 1}, "strict"):
            with self.subTest(net_check=net_check):
                with self.assertRaises(ConfigError):
                    parse_config(json.dumps({"accounts": {"a": {"net_check": net_check}}}), "t")
        foreign = "unix:/tmp/m.sock" if WINDOWS else "\\\\.\\pipe\\mihomo"
        for api in ({"address": "https://127.0.0.1:9097"}, {"address": ""}, {"address": "127.0.0.1"},
                    {"address": "127.0.0.1:70000"}, {"address": "unix:rel.sock"}, {"address": foreign},
                    {"address": "http://u:p@127.0.0.1:9097"}, {"address": "127.0.0.1:9097", "secret": "a\nb"},
                    "127.0.0.1:9097"):
            with self.subTest(api=api):
                with self.assertRaises(ConfigError):
                    parse_config(json.dumps({"clash_api": api}), "t")

    def test_normalize_clash_address(self):
        """R-C4：HOST:PORT / http://HOST:PORT → http://HOST:PORT；本平台的套接字 / 管道写法原样保留。"""
        self.assertEqual(normalize_clash_address("127.0.0.1:9097"), "http://127.0.0.1:9097")
        self.assertEqual(normalize_clash_address("http://localhost:9097/"), "http://localhost:9097")
        self.assertEqual(normalize_clash_address("[::1]:9097"), "http://[::1]:9097")
        if WINDOWS:
            self.assertEqual(normalize_clash_address("pipe:\\\\.\\pipe\\mihomo"), "\\\\.\\pipe\\mihomo")
        else:
            self.assertEqual(normalize_clash_address("unix:/run/m.sock"), "unix:/run/m.sock")

    def test_split_fields(self):
        """R-B4 / R-C4：新旧字段同时出现以新字段为准；只给一个新字段时另一项为关；写出不含旧字段。"""
        def parsed(net_check):
            config = parse_config(json.dumps({"accounts": {"a": {"net_check": net_check}}}), "t")
            check = config.find("a").net_check
            return check.clash_tun, check.clash_global
        self.assertEqual(parsed({"clash": True}), (True, True))
        self.assertEqual(parsed({"clash": True, "clash_tun": True}), (True, False))
        self.assertEqual(parsed({"clash": False, "clash_global": True}), (False, True))
        config = parse_config(json.dumps({"accounts": {"a": {"net_check": {"clash": True}}}}), "t")
        self.assertNotIn("clash", config.to_dict()["accounts"]["a"]["net_check"])

    def test_skip_env_reserved(self):
        """R-C4：账号 env 不能写跳过变量（不分大小写）。"""
        for key in (SKIP, SKIP.lower()):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    validate_env_key(key)
                self.assertIn("is reserved; set it on the command line for one run", str(ctx.exception))

    def test_render_baseline(self):
        """R-A1：net_check=False 的 render 与不传参数逐字节相同；为真时插入检查段，位于设置代理之前。"""
        cases = (("work", "dir", "/x/work", "/x/.claude", "http://127.0.0.1:7901", {"K": "v"}, ["--a"]),
                 ("o'b", "default", "/x/ob", "/x/.claude", "inherit", {}, []))
        for args in cases:
            with self.subTest(name=args[0]):
                base = launcher.render(*args)
                self.assertEqual(launcher.render(*args, net_check=False), base)
                self.assertNotIn("netgate", base)
                checked = launcher.render(*args, net_check=True)
                snippet = "(cd / && exec multi-claude netgate {}) || exit $?\nunset {}\n".format(
                    shlex.quote(args[0]), SKIP)
                self.assertIn(snippet, checked)
                self.assertIn("command -v multi-claude", checked)
                if "proxy" in base:
                    self.assertLess(checked.index("netgate"), checked.index("127.0.0.1:7901"))

    def test_parse_raw_response(self):
        """§5.1.2 命名管道：读全后经 http.client 解析，Content-Length 与分块编码两种回包。"""
        body = b'{"mode":"global","tun":{"enable":true}}'
        plain = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                 + str(len(body)).encode() + b"\r\n\r\n" + body)
        chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + "{:x}".format(10).encode() + b"\r\n"
                   + body[:10] + b"\r\n" + "{:x}".format(len(body) - 10).encode() + b"\r\n" + body[10:]
                   + b"\r\n0\r\n\r\n")
        for raw in (plain, chunked):
            status, data = launchgate._parse_raw_response(raw)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data.decode()), {"mode": "global", "tun": {"enable": True}})
        status, _ = launchgate._parse_raw_response(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        self.assertEqual(status, 401)

    def test_yaml_scalar(self):
        """R-B6：clashfind 读 YAML 标量（引号、注释）。"""
        from multi_claude.clashfind import _yaml_scalar
        self.assertEqual(_yaml_scalar(" 'it''s' # c"), "it's")
        self.assertEqual(_yaml_scalar(' "a\\"b"'), 'a"b')
        self.assertEqual(_yaml_scalar(" 127.0.0.1:9097  # c"), "127.0.0.1:9097")

    def test_split_ps_command(self):
        """R-B6：ps 输出（不带引号）拆成参数，含空格的路径并回前一个值。"""
        line = ("/opt/Clash Verge/cores/verge-mihomo -d /opt/u/Application Support/verge -f "
                "/opt/u/Application Support/verge/clash-verge.yaml -ext-ctl-unix /var/run/verge/501/m.sock")
        self.assertEqual(platform.split_ps_command("/a b/mihomo -d  /x  y -f /z"),
                         ["/a b/mihomo", "-d", "/x  y", "-f", "/z"])
        self.assertEqual(platform.split_ps_command(line), [
            "/opt/Clash Verge/cores/verge-mihomo", "-d", "/opt/u/Application Support/verge",
            "-f", "/opt/u/Application Support/verge/clash-verge.yaml",
            "-ext-ctl-unix", "/var/run/verge/501/m.sock"])


# ---------------------------------------------------------------- CLI 子进程

# 记录 `sys.modules` 里有没有 launchgate：run 在 POSIX 上 exec 目标命令（atexit 不会执行），所以先替换 os.execvpe。
_MODULE_PROBE = """import os, sys
from multi_claude import cli
def _report():
    sys.stdout.write("LAUNCHGATE=%s\\n" % ("multi_claude.launchgate" in sys.modules))
    sys.stdout.flush()
def _fake_exec(file, args, env):
    _report()
    os._exit(0)
os.execvpe = _fake_exec
code = cli.main(sys.argv[1:])
_report()
sys.exit(code)
"""


class LaunchGateCliBase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.trace = TraceServer()
        self.addCleanup(self.trace.stop)
        self.proxy = ProxyServer()
        self.addCleanup(self.proxy.stop)
        self.env[launchgate.TEST_TRACE_ENV] = self.trace.hook
        self.env[launchgate.TEST_CA_ENV] = CERT
        if not WINDOWS:
            # 检查版启动命令调用 PATH 上的 multi-claude：放一个转调本仓库源码的包装（PYTHONPATH 已指向 src）。
            self.wrapper = self.write(os.path.join(self.fakebin, "multi-claude"),
                                      "#!/bin/sh\nexec {} -m multi_claude \"$@\"\n".format(
                                          shlex.quote(sys.executable)), 0o755)
            # code 子命令要找 PATH 上的 code：复用假 claude 的脚本（同样记录参数与环境）。
            with open(os.path.join(self.fakebin, "claude")) as handle:
                self.write(os.path.join(self.fakebin, "code"), handle.read(), 0o755)
        self.ok("add", "work", "--proxy", self.proxy.url)
        self.launcher_path = os.path.join(self.bin, "claude-work" + LAUNCHER_SUFFIX)

    def read(self, path):
        with open(path, **TEXT_KWARGS) as handle:
            return handle.read()

    def dead_proxy(self):
        self.ok("proxy", "work", "http://127.0.0.1:{}".format(closed_port()))

    def clear_fake_out(self):
        for suffix in (".args", ".env"):
            if os.path.exists(self.fake_out + suffix):
                os.unlink(self.fake_out + suffix)

    def started(self):
        return os.path.exists(self.fake_out + ".args")

    def fake_env(self):
        result = {}
        for line in self.read(self.fake_out + ".env").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key] = value
        return result

    def assert_refused(self, code, err, started, level="basic"):
        self.assertEqual(code, 5, err)
        self.assertIn("multi-claude: network check failed for account work ({}): proxy-port:".format(level), err)
        self.assertIn("skip once", err)
        self.assertIn("turn off:  multi-claude netcheck work off", err)
        self.assertFalse(started, "claude / code must not run")

    def saved(self):
        return self.config_data()

    def probe_modules(self, *args):
        """以子进程运行 cli.main(args)，返回 (退出码, launchgate 是否被导入, stderr)。"""
        proc = subprocess.run([sys.executable, "-c", _MODULE_PROBE] + list(args), env=self._merged(None),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              cwd=self.tmp, timeout=120, **TEXT_KWARGS)
        flags = [line for line in proc.stdout.splitlines() if line.startswith("LAUNCHGATE=")]
        self.assertTrue(flags, proc.stdout + proc.stderr)
        return proc.returncode, flags[0] == "LAUNCHGATE=True", proc.stderr

    def processes(self, *argvs):
        with open(self.env[platform.TEST_CLASH_PROCESSES_ENV], "w", encoding="utf-8") as handle:
            json.dump([{"ProcessId": 100 + index, "Name": os.path.basename(argv[0]),
                        "CommandLine": command_line(argv)} for index, argv in enumerate(argvs)], handle)


def command_line(argv):
    """把参数列表拼成一行命令，格式与本平台的拆分方式（platform._split_any）对应。"""
    if WINDOWS:
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(arg) for arg in argv)


class InactiveTest(LaunchGateCliBase):
    """R-A1、R-A2：未开启账号不受影响；开启 / 关闭会重写 / 还原启动命令。"""

    def test_inactive_unchanged(self):
        """R-A1：未开启账号的配置无 net_check / clash_api，启动命令无检查段，run / launch 不导入 launchgate。"""
        data = self.saved()
        self.assertNotIn("net_check", data["accounts"]["work"])
        self.assertNotIn("clash_api", data)
        self.assertNotIn("netgate", self.read(self.launcher_path))
        self.dead_proxy()  # 未开启时代理死了也照常启动
        command = ("launch", "work", "--", "x") if WINDOWS else ("run", "work")
        code, imported, err = self.probe_modules(*command)
        self.assertEqual(code, 0, err)
        self.assertFalse(imported, "launchgate must not be imported for an account without the check")
        self.assertNotIn("network check", err)
        # 对照：开启后同一入口会导入并检查（说明上面的断言确实能区分两种情况）
        self.ok("netcheck", "work", "basic")
        code, imported, err = self.probe_modules(*command)
        self.assertEqual(code, 5, err)
        self.assertTrue(imported)

    def test_enable_rewrites_launcher_and_disable_restores(self):
        """R-A2：开启后启动命令被重写（POSIX 含检查段，Windows 不变）、list 不报 stale；关闭后逐字节还原。"""
        base = self.read(self.launcher_path)
        result = self.ok("netcheck", "work", "basic")
        self.assertIn("network check of work: basic", result.out + result.err)
        checked = self.read(self.launcher_path)
        if WINDOWS:
            self.assertEqual(checked, base)
        else:
            self.assertNotEqual(checked, base)
            self.assertIn("multi-claude netgate work) || exit $?", checked)
            self.assertEqual(os.stat(self.launcher_path).st_mode & 0o777, launcher.LAUNCHER_MODE)
        data = json.loads(self.ok("list", "--json").out)
        self.assertEqual(data["accounts"][0]["launcher"], "ok", data)
        self.ok("netcheck", "work", "strict", "--country", "jp")
        self.assertEqual(self.read(self.launcher_path), checked)
        self.assertEqual(self.saved()["accounts"]["work"]["net_check"],
                         {"level": "strict", "country": "JP", "clash_tun": False, "clash_global": False})
        self.ok("netcheck", "work", "off")
        self.assertEqual(self.read(self.launcher_path), base)
        self.assertNotIn("net_check", self.saved()["accounts"]["work"])
        self.assertEqual(json.loads(self.ok("list", "--json").out)["accounts"][0]["launcher"], "ok")

    @posix_only
    def test_hand_edited_config_is_stale(self):
        """R-A2：手改配置关掉检查后，旧启动命令显示 stale，apply 可修。"""
        self.ok("netcheck", "work", "basic")
        path = os.path.join(self.state, "config.json")
        data = self.saved()
        del data["accounts"]["work"]["net_check"]
        with open(path, "w") as handle:
            json.dump(data, handle)
        self.assertEqual(json.loads(self.ok("list", "--json").out)["accounts"][0]["launcher"], "stale")
        self.ok("apply")
        self.assertNotIn("netgate", self.read(self.launcher_path))
        self.assertEqual(json.loads(self.ok("list", "--json").out)["accounts"][0]["launcher"], "ok")

    def test_rename_keeps_setting(self):
        """R-A2：rename 带上检查设置，新启动命令里的 netgate 用新名字。"""
        self.ok("netcheck", "work", "basic")
        self.ok("rename", "work", "client")
        self.assertFalse(os.path.exists(self.launcher_path))
        new_path = os.path.join(self.bin, "claude-client" + LAUNCHER_SUFFIX)
        if not WINDOWS:
            self.assertIn("netgate client", self.read(new_path))
        self.assertIn("network check of client: basic", self.ok("netcheck", "client").out)


class NetCheckCommandTest(LaunchGateCliBase):
    """R-B6、R-C3、R-C4：netcheck / clash-api 命令与 apply -f。"""

    def test_show(self):
        """R-B3 / §5.1.5：netcheck NAME 逐项列出；全部通过退出 0，有失败退出 5；未开启退出 0。"""
        self.assertIn("network check is off for work", self.ok("netcheck", "work").out)
        self.ok("netcheck", "work", "strict")
        result = self.ok("netcheck", "work")
        self.assertIn("network check of work: strict, US", result.out)
        self.assertIn("ok   proxy-port", result.out)
        self.assertIn("ok   direct-country loc=US (direct)", result.out)
        self.assertIn("ok   proxy-country loc=US (via", result.out)
        self.trace.httpd.loc = "JP"
        result = self.run_cli("netcheck", "work")
        self.assertEqual(result.code, 5, result)
        self.assertIn("fail direct-country loc is JP", result.out)
        self.assertIn("fail proxy-country loc is JP", result.out)

    def test_validation_and_warnings(self):
        """R-C4 / §5.1.5：--country 非法退出 2；未登记账号退出 1；--clash 与单项开关同给退出 2；各类只 warn 的情况。"""
        self.assertEqual(self.run_cli("netcheck", "work", "--country", "USA").code, 2)
        self.assertEqual(self.run_cli("netcheck", "ghost", "basic").code, 1)
        self.assertEqual(self.run_cli("netcheck", "work", "--clash", "on", "--clash-tun", "off").code, 2)
        result = self.ok("netcheck", "work", "--country", "JP")
        self.assertIn("--country is not saved", result.err)
        self.assertNotIn("net_check", self.saved()["accounts"]["work"])
        # 与 multi-codex 不同：代理是 off 也允许设置，只 warn
        self.ok("proxy", "work", "off")
        result = self.ok("netcheck", "work", "basic")
        self.assertIn("launches of work will be refused unless this changes", result.err)
        self.assertEqual(self.saved()["accounts"]["work"]["net_check"]["level"], "basic")
        self.ok("proxy", "work", "https://127.0.0.1:8443")
        result = self.ok("netcheck", "work", "strict")
        self.assertIn("the strict check needs an http:// proxy", result.err)
        result = self.ok("netcheck", "work", "--clash-tun", "off", "--clash-global", "on")
        self.assertIn("global mode without TUN", result.err)
        self.assertIn("the Clash controller is not set", result.err)
        self.assertIn("clash-api auto", result.err)

    def test_flags_and_dry_run(self):
        """R-B4：--clash 两项一起开 / 关，单项开关分别保存；--dry-run 不改配置。"""
        self.ok("netcheck", "work", "--clash", "on")
        check = self.saved()["accounts"]["work"]["net_check"]
        self.assertEqual((check["level"], check["clash_tun"], check["clash_global"]), ("off", True, True))
        self.ok("netcheck", "work", "--clash", "off")
        self.assertNotIn("net_check", self.saved()["accounts"]["work"])
        self.ok("netcheck", "work", "--clash-tun", "on")
        check = self.saved()["accounts"]["work"]["net_check"]
        self.assertEqual((check["clash_tun"], check["clash_global"]), (True, False))
        before = self.read(os.path.join(self.state, "config.json"))
        self.ok("netcheck", "work", "strict", "--dry-run")
        self.assertEqual(self.read(os.path.join(self.state, "config.json")), before)

    def test_legacy_field_in_config(self):
        """R-B4：配置文件里的旧字段 "clash": true 兼容读取，netcheck 显示为两项都开。"""
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        data = self.saved()
        data["accounts"]["work"]["net_check"] = {"level": "off", "clash": True}
        data["clash_api"] = {"address": mihomo.address}
        with open(os.path.join(self.state, "config.json"), "w") as handle:
            json.dump(data, handle)
        result = self.ok("netcheck", "work")
        self.assertIn("network check of work: clash", result.out)
        self.assertIn("ok   clash mode=global, tun on", result.out)
        mihomo.server.payload = {"mode": "rule", "tun": {"enable": True}}
        self.assertEqual(self.run_cli("netcheck", "work").code, 5)

    def test_clash_api_commands(self):
        """R-B6：clash-api ADDRESS --secret-stdin 写入，显示 secret set 且不打印 secret；off 清除；https 拒绝。"""
        self.assertIn("Clash controller is not set", self.ok("clash-api").out)
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        mihomo.server.secret = "topsecret"
        result = self.ok("clash-api", mihomo.address, "--secret-stdin", input="topsecret\n")
        self.assertNotIn("topsecret", result.out + result.err)
        self.assertEqual(self.saved()["clash_api"], {"address": "http://" + mihomo.address, "secret": "topsecret"})
        result = self.ok("clash-api")
        self.assertIn("Clash controller: http://{} (secret set)".format(mihomo.address), result.out)
        self.assertNotIn("topsecret", result.out)
        if not WINDOWS:
            self.assertEqual(os.stat(os.path.join(self.state, "config.json")).st_mode & 0o777, 0o600)
        self.ok("netcheck", "work", "off", "--clash", "on")
        self.assertIn("ok   clash mode=global, tun on", self.ok("netcheck", "work").out)
        self.ok("clash-api", mihomo.address)
        self.assertIn("secret not set", self.ok("clash-api").out)
        result = self.run_cli("netcheck", "work")
        self.assertEqual(result.code, 5, result)
        self.assertIn("(401)", result.out)
        self.ok("clash-api", "off")
        self.assertNotIn("clash_api", self.saved())
        result = self.run_cli("netcheck", "work")
        self.assertEqual(result.code, 5)
        self.assertIn("clash-api auto", result.out)
        self.assertEqual(self.run_cli("clash-api", "https://x:1").code, 2)
        self.assertEqual(self.run_cli("clash-api", mihomo.address, "--secret-stdin").code, 2)
        self.assertEqual(self.run_cli("clash-api", "--secret-stdin").code, 2)
        self.assertEqual(self.run_cli("clash-api", "off", "--secret-stdin").code, 2)

    def test_apply_file(self):
        """R-C3：apply -f 文件含 clash_api（即使是别的平台的写法）→ warn 并沿用当前值；含 net_check → 生效。"""
        self.ok("clash-api", "127.0.0.1:9097")
        foreign = "unix:/tmp/mihomo.sock" if WINDOWS else "\\\\.\\pipe\\mihomo"
        for api in ({"address": foreign}, {"address": "nonsense"}):
            with self.subTest(api=api):
                path = os.path.join(self.tmp, "apply.json")
                with open(path, "w") as handle:
                    json.dump({"accounts": {"work": {"proxy": self.proxy.url,
                                                     "net_check": {"level": "basic"}}}, "clash_api": api}, handle)
                result = self.run_cli("apply", "-f", path)
                self.assertEqual(result.code, 0, result)
                self.assertIn("'clash_api' in", result.err)
                saved = self.saved()
                self.assertEqual(saved["clash_api"]["address"], "http://127.0.0.1:9097")
                self.assertEqual(saved["accounts"]["work"]["net_check"]["level"], "basic")
                if not WINDOWS:
                    self.assertIn("netgate work", self.read(self.launcher_path))

    def test_env_rejects_skip_variable(self):
        """R-C4：env work MULTI_CLAUDE_SKIP_NETCHECK=1 被拒，配置不变。"""
        before = self.read(os.path.join(self.state, "config.json"))
        result = self.run_cli("env", "work", SKIP + "=1")
        self.assertNotEqual(result.code, 0, result)
        self.assertIn("is reserved; set it on the command line for one run", result.err)
        self.assertEqual(self.read(os.path.join(self.state, "config.json")), before)

    def test_completion_has_new_commands(self):
        """R-C5：补全脚本含 netcheck / clash-api，不含隐藏的 netgate。"""
        for shell in ("bash", "zsh", "fish"):
            with self.subTest(shell=shell):
                text = self.ok("completion", shell).out
                self.assertIn("netcheck", text)
                self.assertIn("clash-api", text)
                self.assertNotIn("netgate", text)


class GateTest(LaunchGateCliBase):
    """R-B1、R-B2、R-B5、R-C1、R-C2：各入口的放行与拒绝。"""

    def setUp(self):
        super().setUp()
        self.ok("netcheck", "work", "basic")

    def launch(self, *args, env=None):
        return self.run_launcher("work", *args, env=env)

    def auto(self, project, env=None):
        self.clear_fake_out()
        proc = subprocess.run([os.path.join(self.bin, "claude-auto" + LAUNCHER_SUFFIX), "x"], env=self._merged(env),
                              cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=120, **TEXT_KWARGS)
        return proc.returncode, proc.stderr

    def project(self):
        project = os.path.join(self.tmp, "proj")
        os.makedirs(project, exist_ok=True)
        self.ok("route", project, "work")
        return project

    def test_passed_line_and_same_environment(self):
        """R-B1：端口开 → 一行通过提示、参数原样；开启与关闭时假 claude 看到的环境相同。"""
        code, received, seen, err = self.launch("a", "b c")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("multi-claude: network check passed for account work (basic;"), 1, err)
        self.assertEqual(received, ["a", "b c"])
        self.ok("netcheck", "work", "off")
        code, received_off, seen_off, err_off = self.launch("a", "b c")
        self.assertEqual(code, 0, err_off)
        self.assertNotIn("network check", err_off)
        ignore = {"PWD", "OLDPWD", "SHLVL", "_"}
        self.assertEqual({k: v for k, v in seen.items() if k not in ignore},
                         {k: v for k, v in seen_off.items() if k not in ignore})
        self.assertEqual(received, received_off)

    def test_refused_basic_and_no_proxy(self):
        """R-B1：端口关 → 退出 5、文案含 skip once 与 turn off、未启动 claude；NO_PROXY 含 api.anthropic.com 同样失败。"""
        self.dead_proxy()
        code, received, _, err = self.launch("--version")
        self.assert_refused(code, err, bool(received))
        if not WINDOWS:
            self.assertIn("skip once: {}=1 claude-work".format(SKIP), err)
        self.ok("proxy", "work", self.proxy.url)
        code, received, _, err = self.launch(env={"NO_PROXY": "api.anthropic.com", "no_proxy": "api.anthropic.com"})
        self.assert_refused(code, err, bool(received))
        self.assertIn("api.anthropic.com is bypassed by NO_PROXY", err)

    def test_effective_proxy_sources(self):
        """R-B2：inherit + 终端代理 → 查终端代理（shell）；settings.json 覆盖 → 标 settings.json；设成空 → no proxy in effect。"""
        self.ok("proxy", "work", "inherit")
        code, _, _, err = self.launch(env={"HTTPS_PROXY": self.proxy.url, "https_proxy": self.proxy.url})
        self.assertEqual(code, 0, err)
        result = self.run_cli("netcheck", "work", env={"HTTPS_PROXY": self.proxy.url})
        self.assertIn("from HTTPS_PROXY (shell)", result.out)
        self.dead_proxy()
        settings = os.path.join(self.root, "work", "settings.json")
        self.write(settings, json.dumps({"env": {"https_proxy": self.proxy.url, "HTTPS_PROXY": self.proxy.url}}))
        code, _, _, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertIn("passed for account work", err)
        self.assertIn("(settings.json)", self.ok("netcheck", "work").out)
        empty = {name: "" for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")}
        self.write(settings, json.dumps({"env": empty}))
        code, received, _, err = self.launch()
        self.assert_refused(code, err, bool(received))
        self.assertIn("no proxy in effect", err)

    def test_unreadable_settings_and_socks(self):
        """R-B2：settings.json 损坏 → 拒绝并写出路径；终端代理是 SOCKS → 拒绝并说明。"""
        settings = self.write(os.path.join(self.root, "work", "settings.json"), "{not json")
        code, received, _, err = self.launch()
        self.assert_refused(code, err, bool(received))
        self.assertIn("cannot read " + settings, err)
        os.unlink(settings)
        self.ok("proxy", "work", "inherit")
        code, received, _, err = self.launch(env={"HTTPS_PROXY": "socks5://127.0.0.1:1080",
                                                  "https_proxy": "socks5://127.0.0.1:1080"})
        self.assert_refused(code, err, bool(received))
        self.assertIn("is a SOCKS proxy", err)

    def test_custom_endpoint_note(self):
        """R-B2：设了 ANTHROPIC_BASE_URL → 通过行带 checked api.anthropic.com only。"""
        self.write(os.path.join(self.root, "work", "settings.json"),
                   json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://gw.example.com"}}))
        code, _, _, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertIn("; requests go to gw.example.com; checked api.anthropic.com only", err)
        self.dead_proxy()
        code, _, _, err = self.launch()
        self.assertEqual(code, 5, err)
        self.assertIn("checked api.anthropic.com only", err.split("\n")[0])

    def test_strict_via_launcher(self):
        """R-B3：strict 经启动命令：loc 匹配放行（trace 收到直连与经代理两次请求）；经代理 loc 不同 → 拒绝。"""
        self.ok("netcheck", "work", "strict")
        code, received, _, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertIn("network check passed for account work (strict, US;", err)
        self.assertEqual(self.trace.count, 2)
        other = TraceServer("JP")
        self.addCleanup(other.stop)
        self.proxy.server.redirect = other.port
        code, received, _, err = self.launch()
        self.assertEqual(code, 5, err)
        self.assertIn("(strict, US): proxy-country: loc is JP, expected US", err)
        self.assertIn("  passed: proxy-port, direct-country", err)
        self.assertFalse(received)

    def test_clash_via_launcher(self):
        """R-B4：只开 Clash TUN 的账号经启动命令检查 Clash，rule+TUN 放行、TUN 关拒绝并提示 --clash off。"""
        mihomo = MihomoServer()
        self.addCleanup(mihomo.stop)
        self.ok("clash-api", mihomo.address)
        self.ok("netcheck", "work", "off", "--clash-tun", "on")
        mihomo.server.payload = {"mode": "rule", "tun": {"enable": True}}
        code, _, _, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertIn("(clash tun;", err)
        mihomo.server.payload = {"mode": "rule", "tun": {"enable": False}}
        code, received, _, err = self.launch()
        self.assertEqual(code, 5, err)
        self.assertIn("(clash tun): clash: Clash TUN is off", err)
        self.assertIn("multi-claude netcheck work off --clash off", err)
        self.assertFalse(received)

    def test_skip_once(self):
        """R-B5：跳过变量为 1 / 带空格的 1 → 打印一行后跳过；其它值不跳过；启动的 claude 环境里没有该变量。"""
        self.dead_proxy()
        for value in ("1", " 1 "):
            with self.subTest(value=value):
                code, received, seen, err = self.launch("--x", env={SKIP: value})
                self.assertEqual(code, 0, err)
                self.assertEqual(received, ["--x"])
                self.assertNotIn(SKIP, seen)
                self.assertIn("multi-claude: network check skipped for this run ({}=1)".format(SKIP), err)
        for value in ("quiet", "0", "yes"):
            with self.subTest(value=value):
                code, received, _, err = self.launch(env={SKIP: value})
                self.assert_refused(code, err, bool(received))

    def test_skip_not_inherited_python_entries(self):
        """R-B5：run / login 跳过后，假 claude 的环境里没有跳过变量。"""
        self.dead_proxy()
        for args in (("run", "work"), ("login", "work")):
            with self.subTest(args=args):
                self.clear_fake_out()
                result = self.run_cli(*args, env={SKIP: "1 "})
                self.assertEqual(result.code, 0, result)
                self.assertIn("network check skipped for this run", result.err)
                self.assertNotIn(SKIP, self.fake_env())

    @posix_only
    def test_skip_not_inherited_code_and_auto(self):
        """R-B5：code 与 claude-auto 跳过后，假 code / claude 的环境里没有跳过变量。"""
        project = self.project()
        self.dead_proxy()
        self.clear_fake_out()
        result = self.run_cli("code", "work", env={SKIP: "1"})
        self.assertEqual(result.code, 0, result)
        self.assertNotIn(SKIP, self.fake_env())
        code, err = self.auto(project, env={SKIP: "1"})
        self.assertEqual(code, 0, err)
        self.assertIn("network check skipped for this run", err)
        self.assertNotIn(SKIP, self.fake_env())

    @windows_only
    def test_skip_not_inherited_windows_launch(self):
        """R-B5：Windows launch 入口跳过后，假 claude 的环境里没有跳过变量。"""
        self.dead_proxy()
        self.clear_fake_out()
        result = self.run_cli("launch", "work", "--", "x", env={SKIP: "1"})
        self.assertEqual(result.code, 0, result)
        self.assertNotIn(SKIP.upper(), {key.upper() for key in self.fake_env()})

    def test_refused_python_entries(self):
        """R-C1：run / login / netgate 在失败时退出 5 且未启动假 claude；文案里的 skip once 用各自的命令。"""
        self.dead_proxy()
        for args, hint in ((("run", "work"), "multi-claude run work"), (("login", "work"), "multi-claude login work"),
                           (("netgate", "work"), "claude-work")):
            with self.subTest(args=args):
                self.clear_fake_out()
                result = self.run_cli(*args)
                self.assert_refused(result.code, result.err, self.started())
                self.assertIn(hint, result.err)

    def test_python_entries_pass(self):
        """R-C1：run / login / netgate 通过时各打一行通过提示（netgate 退出 0 且不启动 claude）。"""
        for args in (("run", "work"), ("login", "work")):
            with self.subTest(args=args):
                self.clear_fake_out()
                result = self.run_cli(*args)
                self.assertEqual(result.code, 0, result)
                self.assertEqual(result.err.count("network check passed for account work"), 1, result)
                self.assertTrue(self.started())
        self.assertEqual(self.read(self.fake_out + ".args").split("\n")[:2], ["auth", "login"])
        self.clear_fake_out()
        result = self.run_cli("netgate", "work")
        self.assertEqual(result.code, 0, result)
        self.assertFalse(self.started())

    def test_netgate_errors(self):
        """R-C1 / §3：netgate 未登记账号退出 1 并带配置路径；参数个数不对退出 2。"""
        result = self.run_cli("netgate", "ghost")
        self.assertEqual(result.code, 1, result)
        self.assertIn("account 'ghost' is not registered in " + os.path.join(self.state, "config.json"), result.err)
        self.assertEqual(self.run_cli("netgate").code, 2)
        self.assertEqual(self.run_cli("netgate", "a", "b").code, 2)

    def test_mcp_not_checked(self):
        """R-C1：mcp 不做网络检查，代理死了照常运行 claude mcp。"""
        self.dead_proxy()
        self.clear_fake_out()
        result = self.run_cli("mcp", "work", "list")
        self.assertEqual(result.code, 0, result)
        self.assertNotIn("network check", result.err)
        self.assertEqual(self.read(self.fake_out + ".args").split("\n")[:2], ["mcp", "list"])

    @posix_only
    def test_code_refused_and_passed(self):
        """R-C1：code 通过时启动假 code；失败退出 5、不启动、也不建用户数据目录。"""
        result = self.run_cli("code", "work")
        self.assertEqual(result.code, 0, result)
        self.assertIn("network check passed for account work", result.err)
        self.assertIn("--user-data-dir", self.read(self.fake_out + ".args"))
        data_dir = os.path.join(self.root, ".apps")
        shutil.rmtree(data_dir)
        self.dead_proxy()
        self.clear_fake_out()
        result = self.run_cli("code", "work")
        self.assert_refused(result.code, result.err, self.started())
        self.assertIn("multi-claude code work", result.err)
        self.assertFalse(os.path.exists(data_dir))

    def test_claude_auto_checked(self):
        """R-C1：claude-auto 执行的是账号启动命令，通过放行、失败退出 5。"""
        project = self.project()
        code, err = self.auto(project)
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("network check passed for account work"), 1, err)
        self.dead_proxy()
        code, err = self.auto(project)
        self.assert_refused(code, err, self.started())

    @windows_only
    def test_windows_launch(self):
        """R-C1：Windows launch 入口失败退出 5、不启动；通过时启动。"""
        result = self.run_cli("launch", "work", "--", "x")
        self.assertEqual(result.code, 0, result)
        self.dead_proxy()
        self.clear_fake_out()
        result = self.run_cli("launch", "work", "--", "x")
        self.assert_refused(result.code, result.err, self.started())

    def test_missing_dir_reports_dir_error(self):
        """R-C2：账号目录不存在 → 先报目录错误、退出 1，不做网络检查（启动命令与 run 都是）。"""
        # 代理可用 + strict：检查一旦运行就会请求 trace，trace 计数为 0 才能证明检查没跑。
        self.ok("netcheck", "work", "strict")
        shutil.rmtree(os.path.join(self.root, "work"))
        code, _, _, err = self.launch()
        self.assertEqual(code, 1, err)
        self.assertIn("account directory does not exist", err)
        self.assertNotIn("network check", err)
        result = self.run_cli("run", "work")
        self.assertEqual(result.code, 1, result)
        self.assertIn("account directory does not exist", result.err)
        self.assertNotIn("network check", result.err)
        self.assertEqual(self.trace.count, 0)

    @posix_only
    def test_missing_multi_claude_or_claude(self):
        """R-C1 / §5.1.3：PATH 上没有 multi-claude → 127 并说明；有 multi-claude 没 claude → 与未开启时同样的报错。"""
        os.unlink(self.wrapper)
        code, received, _, err = self.launch()
        self.assertEqual(code, 127, err)
        self.assertIn("multi-claude not found in PATH", err)
        self.assertFalse(received)
        only = os.path.join(self.tmp, "only")
        self.write(os.path.join(only, "multi-claude"),
                   "#!/bin/sh\nexec {} -m multi_claude \"$@\"\n".format(shlex.quote(sys.executable)), 0o755)
        code, _, _, err = self.launch(env={"PATH": os.pathsep.join([only, "/usr/bin", "/bin"])})
        self.assertEqual(code, 127, err)
        self.assertIn("network check passed", err)
        self.assertTrue(err.endswith("multi-claude: claude not found in PATH\n"), err)

    @posix_only
    def test_malicious_module_in_cwd_not_loaded(self):
        """R-C1 / §5.1.3：在含 json.py 等同名文件的目录启动，netgate 不会导入它们（cd /）。"""
        project = os.path.join(self.tmp, "evil")
        os.makedirs(project)
        marker = os.path.join(self.tmp, "pwned")
        for module in ("json", "ssl", "socket"):
            self.write(os.path.join(project, module + ".py"),
                       "open({!r}, 'w').close()\nraise SystemExit(99)\n".format(marker))
        proc = subprocess.run([self.launcher_path, "--v"], env=self._merged(None), cwd=project,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertIn("PWD=" + project, self.read(self.fake_out + ".env"))


class ClashFindTest(LaunchGateCliBase):
    """R-B6：`clash-api auto` 与 netcheck 开启 Clash 时的自动查找（只看假进程列表与临时 HOME 下的配置文件）。"""

    def setUp(self):
        super().setUp()
        self.mihomo = MihomoServer()
        self.addCleanup(self.mihomo.stop)

    def saved_api(self):
        return self.saved().get("clash_api")

    def temp_dir(self):
        directory = tempfile.mkdtemp(prefix="mcf-", dir=self.tmp)
        return directory

    def write_file(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 显式 UTF-8：Windows 默认按 cp1252 写，含中文注释的配置会写不进去；Clash 配置本身是 UTF-8。
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def http(self, address):
        return "http://" + address

    def test_tcp_flag_forms(self):
        """R-B6：-ext-ctl 的三种写法都能找到 TCP 接口。"""
        for argv in (["/opt/mihomo", "-ext-ctl", self.mihomo.address],
                     ["/opt/mihomo", "-ext-ctl=" + self.mihomo.address],
                     ["/opt/mihomo", "--ext-ctl", self.mihomo.address]):
            with self.subTest(argv=argv):
                self.ok("clash-api", "off")
                self.processes(argv)
                result = self.ok("clash-api", "auto")
                self.assertIn("found Clash controller", result.err)
                self.assertEqual(self.saved_api()["address"], self.http(self.mihomo.address))

    @posix_only
    def test_unix_socket(self):
        """R-B6：-ext-ctl-unix 绝对路径，或配合 -d 的相对路径。"""
        directory = self.temp_dir()
        server = MihomoServer(os.path.join(directory, "m.sock"))
        self.addCleanup(server.stop)
        self.processes(["/opt/verge-mihomo", "-ext-ctl-unix", os.path.join(directory, "m.sock")])
        self.ok("clash-api", "auto")
        self.assertEqual(self.saved_api(), {"address": "unix:" + os.path.join(directory, "m.sock")})
        self.ok("clash-api", "off")
        self.processes(["/opt/verge-mihomo", "-d", directory, "-ext-ctl-unix", "m.sock"])
        self.ok("clash-api", "auto")
        self.assertEqual(self.saved_api()["address"], "unix:" + os.path.join(directory, "m.sock"))

    def test_config_file_and_secret_flag(self):
        """R-B6：-f 指向的配置文件；-secret 覆盖文件里的 secret，-v 输出不含 secret 原文。"""
        directory = self.temp_dir()
        config = os.path.join(directory, "my config.yaml")
        self.write_file(config, "mixed-port: 7890\nexternal-controller: {}\nsecret: other\n".format(
            self.mihomo.address))
        self.processes(["/opt/clash-meta", "-f", config])
        self.ok("clash-api", "auto")
        self.assertEqual(self.saved_api()["address"], self.http(self.mihomo.address))
        self.ok("clash-api", "off")
        self.mihomo.server.secret = "s3cretflag"
        self.processes(["/opt/clash-meta", "-f", config, "-secret", "s3cretflag"])
        result = self.ok("clash-api", "auto", "--verbose")
        self.assertNotIn("s3cretflag", result.out + result.err)
        self.assertEqual(self.saved_api()["secret"], "s3cretflag")
        self.assertIn("secret set", self.ok("clash-api").out)

    def test_first_dead_second_ok(self):
        """R-B6：第一个候选连不上，继续试下一个。"""
        dead = "127.0.0.1:{}".format(closed_port())
        self.processes(["/opt/mihomo", "-ext-ctl", dead], ["/opt/mihomo2", "-ext-ctl", self.mihomo.address])
        result = self.ok("clash-api", "auto", "--verbose")
        self.assertIn(dead, result.err)
        self.assertEqual(self.saved_api()["address"], self.http(self.mihomo.address))

    def test_not_found_and_dry_run(self):
        """R-B6：找不到 → 退出 1 不写；--dry-run 找到也不写；auto 与 --secret-stdin 同给 → 退出 2。"""
        result = self.run_cli("clash-api", "auto")
        self.assertEqual(result.code, 1, result)
        self.assertIn("no working Clash", result.err)
        self.assertIsNone(self.saved_api())
        self.processes(["/opt/mihomo", "-ext-ctl", self.mihomo.address])
        result = self.ok("clash-api", "auto", "--dry-run")
        self.assertIn("found Clash controller", result.err)
        self.assertIsNone(self.saved_api())
        self.assertEqual(self.run_cli("clash-api", "auto", "--secret-stdin").code, 2)

    def test_ignored_candidates(self):
        """R-B6：非 Clash 进程、非本机地址、相对路径的候选被跳过。"""
        directory = self.temp_dir()
        config = os.path.join(directory, "c.yaml")
        self.write_file(config, "external-controller: {}\n".format(self.mihomo.address))
        self.processes(["/usr/bin/tail", "-f", config],
                       ["/opt/mihomo", "-ext-ctl", "10.0.0.1:9090"],
                       ["/opt/mihomo", "-f", "rel.yaml", "-ext-ctl-unix", "rel.sock"])
        result = self.run_cli("clash-api", "auto", "--verbose")
        self.assertEqual(result.code, 1, result)
        self.assertIn("10.0.0.1:9090 (process mihomo): not a local address, skipped", result.err)
        self.assertNotIn("rel.yaml", result.err)
        if not WINDOWS:
            self.assertIn("relative path and the home directory is unknown", result.err)
        self.assertNotIn(self.mihomo.address, result.err)

    @unittest.skipUnless(WINDOWS or sys.platform == "darwin", "no known Clash Verge config path here")
    def test_known_config_file(self):
        """R-B6：Clash Verge 的已知配置文件（在临时 HOME / APPDATA 下）。"""
        if WINDOWS:
            base = os.path.join(self.env["APPDATA"], "io.github.clash-verge-rev.clash-verge-rev")
        else:
            base = os.path.join(self.home, "Library", "Application Support",
                                "io.github.clash-verge-rev.clash-verge-rev")
        self.mihomo.server.secret = "filesecret"
        lines = ['external-controller: "{}"  # 注释'.format(self.mihomo.address),
                 "profile:", "  external-controller: 127.0.0.1:{}".format(closed_port()),
                 "secret: filesecret"]
        if not WINDOWS:
            lines.append("external-controller-unix: rel.sock")
        self.write_file(os.path.join(base, "config.yaml"), "\n".join(lines) + "\n")
        result = self.ok("clash-api", "auto", "--verbose")
        self.assertEqual(self.saved_api(), {"address": self.http(self.mihomo.address), "secret": "filesecret"})
        self.assertNotIn("filesecret", result.out + result.err)
        if not WINDOWS:
            self.assertIn("unix:" + os.path.join(base, "rel.sock"), result.err)

    def test_secret_not_sendable(self):
        """R-B6：secret 含中文时编不进请求头：该候选记为失败，继续试下一个，不出 traceback。"""
        self.processes(["/opt/mihomo", "-ext-ctl", "127.0.0.1:{}".format(closed_port()), "-secret", "密码"],
                       ["/opt/mihomo2", "-ext-ctl", self.mihomo.address])
        result = self.ok("clash-api", "auto", "--verbose")
        self.assertIn("cannot be sent in an HTTP header", result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertNotIn("密码", result.out + result.err)
        self.assertEqual(self.saved_api()["address"], self.http(self.mihomo.address))

    def test_netcheck_finds_when_unset(self):
        """R-B6：netcheck 开启 Clash 而接口未设置时自动查找一次并保存；已设置则不覆盖。"""
        self.processes(["/opt/mihomo", "-ext-ctl", self.mihomo.address])
        self.ok("netcheck", "work", "basic")
        self.assertIsNone(self.saved_api())
        result = self.ok("netcheck", "work", "--clash-tun", "on")
        self.assertIn("found Clash controller", result.err)
        self.assertEqual(self.saved_api()["address"], self.http(self.mihomo.address))
        other = MihomoServer()
        self.addCleanup(other.stop)
        self.processes(["/opt/mihomo", "-ext-ctl", other.address])
        result = self.ok("netcheck", "work", "--clash-global", "on")
        self.assertNotIn("found Clash controller", result.err)
        self.assertEqual(self.saved_api()["address"], self.http(self.mihomo.address))


if __name__ == "__main__":
    unittest.main()
