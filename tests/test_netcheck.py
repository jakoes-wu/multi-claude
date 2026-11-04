"""doctor --network：逐账号网络自检（方案 feature-network-check §8）。

目标与代理都是用例内启动的本地假服务，测试永远连不到真实服务：CLI 在测试模式下只认
MULTI_CLAUDE_TEST_NETCHECK_TARGETS，没有设置时不发任何请求（T21）。
直连用例的目标用 127.0.0.1；走代理的用例目标用 netcheck.test：launcher 模式会把 127.0.0.1 追加进
NO_PROXY，用本地地址就测不到代理。假代理不解析目标名，一律转到假服务。
"""

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from unittest import mock

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import netcheck  # noqa: E402

PROXIED_HOST = "netcheck.test"
SECRET = "SECRET-p4ss"


class _ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class FakeServer(object):
    """假的目标服务：记录 HEAD 请求的路径与请求头；hang 时接受连接但不应答。"""

    def __init__(self, hang: bool = False) -> None:
        self.requests = []
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                owner.requests.append((self.command, self.path, dict(self.headers)))
                if hang:
                    owner.release.wait(30)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_HEAD

            def log_message(self, *args):
                pass

        self.server = _ThreadingServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class FakeProxy(object):
    """假 http 代理：记录每次 CONNECT，按 mode 应答。

    mode：tunnel（回 200 并接到假服务）、数字状态码、hang（不回应）、garbage（回非 HTTP 字节）、
    close（不回任何字节就断开）。drop_first 表示前 N 次 CONNECT 收到后直接断开。
    """

    def __init__(self, upstream_port: int = 0, mode: str = "tunnel") -> None:
        self.upstream_port = upstream_port
        self.mode = mode
        self.drop_first = 0
        self.connects = []
        self.release = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            head += chunk
        lines = head.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        self.connects.append((lines[0], headers))
        if len(self.connects) <= self.drop_first or self.mode == "close":
            client.close()
            return
        if self.mode == "hang":
            self.release.wait(30)
            client.close()
            return
        if self.mode == "garbage":
            client.sendall(b"\x05\x00")
            self.release.wait(30)
            client.close()
            return
        if self.mode != "tunnel":
            client.sendall("HTTP/1.1 {} Nope\r\nContent-Length: 0\r\n\r\n".format(self.mode).encode())
            client.close()
            return
        upstream = socket.create_connection(("127.0.0.1", self.upstream_port))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()
        self._pipe(client, upstream)

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                data = source.recv(4096)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                try:
                    sock.close()
                except OSError:
                    pass

    def close(self) -> None:
        self.release.set()
        self.listener.close()


def free_port() -> int:
    """一个当前无人监听的端口。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class NetcheckCliTest(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.server2 = FakeServer()
        self.addCleanup(self.server2.close)
        # 两个直连目标，端口不同：顺带验证去重键含端口（D18）
        self.direct = "http://127.0.0.1:{},http://127.0.0.1:{}".format(self.server.port, self.server2.port)

    def proxy(self, mode: str = "tunnel") -> FakeProxy:
        proxy = FakeProxy(self.server.port, mode)
        self.addCleanup(proxy.close)
        return proxy

    def proxied_targets(self) -> str:
        return "http://{}:{}".format(PROXIED_HOST, self.server.port)

    def doctor(self, targets=None, env=None, *extra):
        merged = {"MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT": "5"}
        if targets is not None:
            merged["MULTI_CLAUDE_TEST_NETCHECK_TARGETS"] = targets
        merged.update(env or {})
        result = self.run_cli("doctor", "--json", "--network", *extra, env=merged)
        data = json.loads(result.out)
        return result, [c for c in data["checks"] if c["id"] == "network"], data

    def settings(self, name: str, env) -> None:
        self.write(os.path.join(self.root, name, "settings.json"), json.dumps({"env": env}))

    def assert_one(self, checks, level, *fragments, subject="work"):
        mine = [c for c in checks if c["subject"] == subject]
        self.assertTrue(mine, checks)
        for check in mine:
            self.assertEqual(check["level"], level, check)
            for fragment in fragments:
                self.assertIn(fragment, check["message"], check)
        return mine

    # T1
    def test_without_network_flag_nothing_is_sent(self):
        self.ok("add", "work", "--proxy", "off")
        result = self.run_cli("doctor", "--json", env={"MULTI_CLAUDE_TEST_NETCHECK_TARGETS": self.direct})
        data = json.loads(result.out)
        self.assertEqual([c for c in data["checks"] if c["id"] == "network"], [])
        self.assertEqual(self.server.requests, [])
        self.assertNotIn("checking the network", result.err)

    # T2 + T21（开始提示、schema、退出码）
    def test_direct_ok(self):
        self.ok("add", "work", "--proxy", "off")
        result, checks, data = self.doctor(self.direct)
        self.assertEqual(len(checks), 2, checks)
        self.assert_one(checks, "ok", "via direct: reachable (")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.server2.requests), 1)
        command, path, headers = self.server.requests[0]
        self.assertEqual((command, path), ("HEAD", "/"))
        lowered = {key.lower() for key in headers}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("checking the network for 1 account(s)", result.err)
        self.assertNotIn("checking the network", result.out)

    def test_exit_code(self):
        self.ok("add", "work", "--proxy", "off")
        self.ok("doctor", "--network", env={"MULTI_CLAUDE_TEST_NETCHECK_TARGETS": self.direct})
        result = self.run_cli("doctor", "--network",
                              env={"MULTI_CLAUDE_TEST_NETCHECK_TARGETS": "http://127.0.0.1:{}".format(free_port())})
        self.assertEqual(result.code, 1, result)
        self.assertIn("[error] network work: 127.0.0.1:", result.out)

    # T3
    def test_launcher_proxy_ok(self):
        proxy = self.proxy()
        self.ok("add", "work", "--proxy", str(proxy.port))
        _, checks, _ = self.doctor(self.proxied_targets())
        self.assert_one(checks, "ok", "via 127.0.0.1:{} (launcher): reachable".format(proxy.port))
        self.assertEqual(proxy.connects[0][0], "CONNECT {}:{} HTTP/1.1".format(PROXIED_HOST, self.server.port))

    # T4
    def test_proxy_not_listening(self):
        self.ok("add", "work", "--proxy", str(free_port()))
        _, checks, _ = self.doctor(self.proxied_targets())
        self.assert_one(checks, "error", "cannot reach the proxy", "multi-claude proxy work <port>")

    # T5 / T6 / T13 / T14 / 无字节断开
    def test_proxy_replies(self):
        cases = (("502", "could not reach {}:{} (502)"), ("407", "user name and password (407)"),
                 ("hang", "did not connect to"), ("garbage", "did not answer like an http proxy"),
                 ("close", "closed the connection instead of reaching"))
        self.ok("add", "work", "--proxy", "off")
        for mode, fragment in cases:
            with self.subTest(mode=mode):
                proxy = self.proxy(mode)
                self.ok("proxy", "work", str(proxy.port))
                env = {"MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT": "2"} if mode == "hang" else None
                fragment = fragment.format(PROXIED_HOST, self.server.port) if "{}" in fragment else fragment
                _, checks, _ = self.doctor(self.proxied_targets(), env)
                self.assert_one(checks, "error", fragment)
                self.assertNotIn("cannot reach the proxy", checks[0]["message"])

    # T7
    def test_settings_json_overrides(self):
        proxy = self.proxy()
        good = "http://127.0.0.1:{}".format(proxy.port)
        dead = "http://127.0.0.1:{}".format(free_port())
        cases = (
            ("off", {"HTTPS_PROXY": good}, None, "ok", "(settings.json)"),
            (dead, {"https_proxy": good}, None, "ok", "(settings.json)"),
            ("inherit", {"https_proxy": good}, {"https_proxy": dead}, "ok", "(settings.json)"),
        )
        if not sys.platform == "win32":
            # 只设大写：launcher 写的小写 https_proxy 仍在、先被取到（env.py 两套都写）
            cases += ((dead, {"HTTPS_PROXY": good}, None, "error", "(launcher)"),)
        for index, (mode, settings, shell, level, source) in enumerate(cases):
            with self.subTest(index=index):
                name = "a{}".format(index)
                self.ok("add", name, "--proxy", mode)
                self.settings(name, settings)
                _, checks, _ = self.doctor(self.proxied_targets(), shell)
                mine = self.assert_one(checks, level, source, subject=name)
                if level == "error":
                    for check in mine:
                        self.assertIn("cannot reach the proxy", check["message"], check)
                self.ok("proxy", name, "off")
                os.remove(os.path.join(self.root, name, "settings.json"))

    # T8
    def test_no_proxy_bypass(self):
        proxy = self.proxy()
        self.ok("add", "work", "--proxy", "inherit")
        for separator in (",", " "):
            with self.subTest(separator=separator):
                env = {"HTTPS_PROXY": "http://127.0.0.1:{}".format(proxy.port),
                       "NO_PROXY": "example.org{}127.0.0.1".format(separator)}
                _, checks, _ = self.doctor("http://127.0.0.1:{}".format(self.server.port), env)
                self.assert_one(checks, "warn", "via direct: reachable", "NO_PROXY covers this host")
                _, checks, _ = self.doctor("http://127.0.0.1:{}".format(free_port()), env)
                mine = self.assert_one(checks, "error", "cannot connect", "NO_PROXY covers this host")
                for check in mine:
                    # 账号已配代理、问题出在 NO_PROXY，不该再建议“设个代理”
                    self.assertNotIn("set a proxy", check["message"], check)
        self.assertEqual(proxy.connects, [])

    # T9 / T10
    def test_no_secrets_in_output(self):
        proxy = self.proxy("407")
        self.ok("add", "work", "--proxy", "inherit")
        self.write(os.path.join(self.root, "work", ".credentials.json"), json.dumps({"token": SECRET}))
        self.settings("work", {"ANTHROPIC_API_KEY": SECRET, "OTHER": SECRET})
        cases = (("socks5://alice:{}@h:1".format(SECRET), "error", "is a SOCKS proxy"),
                 ("alice:{}@h:1".format(SECRET), "error", "not a valid proxy URL"),
                 ("http://alice:{}@127.0.0.1:{}".format(SECRET, proxy.port), "error", "(407)"))
        for value, level, fragment in cases:
            with self.subTest(value=value):
                result, checks, _ = self.doctor(self.proxied_targets(), {"HTTPS_PROXY": value})
                self.assert_one(checks, level, fragment)
                self.assertNotIn(SECRET, result.out + result.err)
                self.assertNotIn("alice", result.out + result.err)
                result = self.run_cli("doctor", "--network", "--verbose", env={
                    "HTTPS_PROXY": value, "MULTI_CLAUDE_TEST_NETCHECK_TARGETS": self.proxied_targets()})
                self.assertNotIn(SECRET, result.out + result.err)
        self.assertIn("Proxy-Authorization", proxy.connects[0][1])

    # T9：有没有凭据文件，网络检查结果都一样（不读凭据）
    def test_credentials_do_not_change_result(self):
        self.ok("add", "work", "--proxy", "off")

        def network_checks():
            _, checks, _ = self.doctor(self.direct)
            # 耗时每次不同，只比级别、对象和去掉耗时的消息
            return sorted((c["level"], c["subject"], re.sub(r"\([0-9.]+s\)", "", c["message"]))
                          for c in checks if c["id"] == "network")

        before = network_checks()
        self.write(os.path.join(self.root, "work", ".credentials.json"), json.dumps({"token": SECRET}))
        self.assertEqual(network_checks(), before)
        self.assertTrue(before)

    # T11
    def test_unreadable_settings(self):
        self.ok("add", "work", "--proxy", "off")
        path = os.path.join(self.root, "work", "settings.json")
        for content in (b"{not json", "{\"env\": {\"A\": \"中\"}}".encode("gbk"), b"{\"env\": []}"):
            with self.subTest(content=content):
                with open(path, "wb") as handle:
                    handle.write(content)
                _, checks, _ = self.doctor(self.direct)
                warns = [c for c in checks if "could not read" in c["message"]]
                self.assertEqual(len(warns), 1, checks)
                self.assertEqual(warns[0]["level"], "warn")

    # T12
    def test_timeout(self):
        hung = FakeServer(hang=True)
        self.addCleanup(hung.close)
        self.ok("add", "work", "--proxy", "off")
        start = time.monotonic()
        _, checks, _ = self.doctor("http://127.0.0.1:{}".format(hung.port),
                                   {"MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT": "1"})
        self.assertLess(time.monotonic() - start, 4)
        self.assert_one(checks, "error", "timed out")

    # T15
    def test_retry(self):
        proxy = self.proxy()
        proxy.drop_first = 1
        self.ok("add", "work", "--proxy", str(proxy.port))
        _, checks, _ = self.doctor(self.proxied_targets())
        self.assert_one(checks, "warn", "second try")

    # T16
    def test_dedup(self):
        proxy = self.proxy()
        other = FakeServer()
        self.addCleanup(other.close)
        self.ok("add", "a", "--proxy", str(proxy.port))
        self.ok("add", "b", "--proxy", str(proxy.port))
        targets = "{},http://{}:{}".format(self.proxied_targets(), PROXIED_HOST, other.port)
        _, checks, _ = self.doctor(targets)
        self.assertEqual(len(checks), 4, checks)
        self.assertEqual(len(proxy.connects), 2, proxy.connects)
        # 认证不同的账号各自探测
        proxy.connects.clear()
        self.ok("add", "c", "--proxy", "inherit")
        env = {"HTTPS_PROXY": "http://u:p@127.0.0.1:{}".format(proxy.port)}
        self.ok("proxy", "a", "inherit")
        self.ok("proxy", "b", "off")
        self.settings("b", {"HTTPS_PROXY": "http://v:q@127.0.0.1:{}".format(proxy.port)})
        _, checks, _ = self.doctor(self.proxied_targets(), env)
        self.assertEqual(len(proxy.connects), 2, proxy.connects)

    def test_completion_has_flag(self):
        self.assertIn("--network", self.ok("completion", "bash").out)

    # T18
    @unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a self-signed certificate")
    def test_self_signed_certificate(self):
        cert = os.path.join(self.tmp, "cert.pem")
        key = os.path.join(self.tmp, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-keyout", key, "-out", cert],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                try:
                    context.wrap_socket(client, server_side=True).close()
                except (OSError, ssl.SSLError):
                    client.close()

        threading.Thread(target=serve, daemon=True).start()
        self.ok("add", "work", "--proxy", "off")
        _, checks, _ = self.doctor("https://127.0.0.1:{}".format(listener.getsockname()[1]))
        self.assert_one(checks, "warn", "could not verify the certificate")

    # T21
    def test_other_outcomes(self):
        self.ok("add", "work", "--proxy", "off")
        self.settings("work", {"ANTHROPIC_BASE_URL": "https://user:{}@gw.example.com/v1".format(SECRET)})
        result, checks, _ = self.doctor(self.direct)
        warn = [c for c in checks if "sends requests to" in c["message"]]
        self.assertEqual(len(warn), 1)
        self.assertIn("gw.example.com", warn[0]["message"])
        self.assertNotIn(SECRET, result.out + result.err)

        self.ok("proxy", "work", "inherit")
        os.remove(os.path.join(self.root, "work", "settings.json"))
        _, checks, _ = self.doctor(self.proxied_targets(), {"HTTPS_PROXY": "https://127.0.0.1:1"})
        self.assert_one(checks, "warn", "not checked", "claude --debug")
        _, checks, _ = self.doctor("http://127.0.0.1:{}".format(self.server.port),
                                   {"HTTPS_PROXY": "https://127.0.0.1:1", "NO_PROXY": "127.0.0.1"})
        self.assert_one(checks, "warn", "via direct: reachable", "NO_PROXY covers this host")
        _, checks, _ = self.doctor(self.proxied_targets(), {"HTTPS_PROXY": "ftp://h:1"})
        self.assert_one(checks, "error", "not a valid proxy URL")

    def test_no_endpoint_in_test_mode(self):
        self.ok("add", "work", "--proxy", "off")
        result, checks, _ = self.doctor(None, {"MULTI_CLAUDE_TEST_NETCHECK_TARGETS": None})
        self.assertEqual(result.code, 1)
        self.assert_one(checks, "error", "nothing was sent")
        self.assertEqual(self.server.requests, [])


class NetcheckUnitTest(unittest.TestCase):
    # T19
    def test_classify_tls(self):
        cases = ((ssl.SSLCertVerificationError(), "cert_unverified"), (ssl.SSLEOFError(), "tls_closed"),
                 (ssl.SSLError(), "tls_failed"), (socket.timeout(), "timeout"), (TimeoutError(), "timeout"),
                 (ConnectionResetError(), "tls_closed"))
        for exc, code in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(netcheck._classify_tls(exc), code)

    # T21：探测线程里的意外异常立即记为 internal_error，不让主线程空等
    def test_internal_error_returns_at_once(self):
        job = netcheck._Job(urllib.parse.urlsplit("http://127.0.0.1:1"), None, None, 5.0)
        with mock.patch.object(netcheck, "_attempt", side_effect=RuntimeError("boom")):
            start = time.monotonic()
            netcheck._run_jobs({"k": job}, 20.0)
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(netcheck._job_result(job).code, "internal_error")

    def test_deadline_uses_stage(self):
        job = netcheck._Job(urllib.parse.urlsplit("http://127.0.0.1:1"), None, None, 5.0)
        job.stage = netcheck.STAGE_TUNNEL
        self.assertEqual(netcheck._job_result(job).code, "proxy_upstream_timeout")
        job.stage = netcheck.STAGE_RESOLVE
        self.assertEqual(netcheck._job_result(job).code, "dns_timeout")


if __name__ == "__main__":
    unittest.main()
