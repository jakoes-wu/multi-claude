"""list 的 PLAN / BILLING 两列：向服务端实时查询套餐（方案 feature-live-plan §8）。

目标服务与代理都是用例内启动的本地假服务，测试永远连不到真实服务：
CLI 在测试模式下只认 MULTI_CLAUDE_TEST_PROFILE_URL，没有设置时拒绝发请求（T14）。
凭据一律写进账号目录的 `.credentials.json`：Linux 只读这个文件；macOS 上假 security 默认报告
“钥匙串里没有”，随后同样读到这个文件。钥匙串分支单独用假 security 覆盖（T13）。
"""

import base64
import json
import os
import re
import socket
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from unittest import mock

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import identity, plan  # noqa: E402

TOKEN = "tok-SECRET-abc123"
# 代理场景下的目标主机名：不能用 127.0.0.1，启动命令语义会把本地地址加进 NO_PROXY 而绕过代理。
# 假代理不解析这个名字，一律转到假服务。
PROXIED_HOST = "plan.test"


class _ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class FakeProfileServer(object):
    """假的 /api/oauth/profile：记录收到的请求，按设定返回。"""

    def __init__(self) -> None:
        self.requests = []
        self.status = 200
        self.body = b"{}"
        # 置位前收到的请求一直不应答，用来模拟服务挂起
        self.hang = False
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                if owner.hang:
                    owner.release.wait(30)
                    return
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(owner.body)))
                self.end_headers()
                self.wfile.write(owner.body)

            def log_message(self, *args):
                pass

        self.server = _ThreadingServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, organization=None, status=200, raw=None) -> None:
        self.status = status
        self.body = raw if raw is not None else json.dumps({"organization": organization}).encode()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class FakeConnectProxy(object):
    """只支持 CONNECT 的假代理：记录每次 CONNECT 的目标与请求头，然后把隧道接到假服务。"""

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.connects = []
        # 前 N 次 CONNECT 收到后直接断开，模拟代理偶发掐断连接
        self.drop_first = 0
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
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
        if len(self.connects) <= self.drop_first:
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
        self.listener.close()


def free_port() -> int:
    """一个当前没有监听的端口（绑定后立刻释放）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def org(organization_type="claude_max", rate_limit_tier="default_claude_max_5x", billing_type="apple_subscription"):
    return {"organization_type": organization_type, "rate_limit_tier": rate_limit_tier, "billing_type": billing_type}


class PlanTestCase(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = FakeProfileServer()
        self.addCleanup(self.server.close)
        self.server.reply(org())
        self.url = "http://127.0.0.1:{}/api/oauth/profile".format(self.server.port)
        self.live = {"MULTI_CLAUDE_TEST_PROFILE_URL": self.url}

    def credentials(self, name: str, token=TOKEN, expires_in_s=3600, raw=None) -> None:
        if raw is None:
            oauth = {"accessToken": token, "refreshToken": "refresh-SECRET"}
            if expires_in_s is not None:
                oauth["expiresAt"] = int((time.time() + expires_in_s) * 1000)
            raw = json.dumps({"claudeAiOauth": oauth})
        self.write(os.path.join(self.root, name, ".credentials.json"), raw, 0o600)

    def row(self, out: str, name: str) -> list:
        """简表里该账号一行的前四格：NAME LOGIN PLAN BILLING（PLAN 可能含空格，按两个以上空格切）。"""
        for line in out.splitlines():
            if line.startswith(name + " "):
                return re.split(r"\s{2,}", line.strip())[:4]
        self.fail("no row for {} in:\n{}".format(name, out))

    def assert_no_secret(self, result) -> None:
        for secret in (TOKEN, "refresh-SECRET"):
            self.assertNotIn(secret, result.out)
            self.assertNotIn(secret, result.err)


class LookupTest(PlanTestCase):
    def test_t1_plan_and_billing_columns(self):
        self.ok("add", "work")
        self.credentials("work")
        result = self.ok("list", env=self.live)
        self.assertRegex(result.out.splitlines()[0], r"^NAME\s+LOGIN\s+PLAN\s+BILLING\s+PROXY\s")
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        self.assertEqual(len(self.server.requests), 1)
        path, headers = self.server.requests[0]
        self.assertEqual(path, "/api/oauth/profile")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertTrue(headers["User-Agent"].startswith("multi-claude/"))
        self.assertNotIn("plan lookup failed", result.err)
        self.assert_no_secret(result)

    def test_t2_display_mapping(self):
        cases = [
            (org("claude_pro", "default_claude_ai", "google_play_subscription"), "pro", "google-play"),
            (org("claude_max", "default_claude_max_20x", "stripe_subscription"), "max (20x)", "stripe"),
            (org("claude_team", "default_claude_max_5x", "stripe_subscription_contracted"),
             "team (5x)", "stripe-contracted"),
            (org("claude_enterprise", None, "aws_marketplace"), "enterprise", "aws-marketplace"),
            (org("claude_future_plan", "default_x", "c4e_consumption_trial"),
             "claude_future_plan", "c4e-consumption-trial"),
            (org(None, None, None), "unknown", "-"),
        ]
        for organization, plan_text, billing_text in cases:
            result = plan.PlanResult(plan.STATUS_OK, organization["organization_type"],
                                     organization["rate_limit_tier"], organization["billing_type"])
            self.assertEqual((plan.plan_cell(result), plan.billing_cell(result)), (plan_text, billing_text))

    def test_t3_no_login_sends_nothing(self):
        self.ok("add", "none")
        self.ok("add", "empty")
        self.credentials("empty", raw="{}")
        self.ok("add", "broken")
        self.credentials("broken", raw="not json")
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "none")[2:], ["-", "-"])
        self.assertEqual(self.row(result.out, "empty")[2:], ["-", "-"])
        self.assertEqual(self.row(result.out, "broken")[2:], ["error", "-"])
        self.assertIn("plan lookup failed for broken: credentials_unreadable", result.err)
        self.assertEqual(self.server.requests, [])

    def test_t4_expired_token_is_not_sent(self):
        self.ok("add", "work")
        self.credentials("work", expires_in_s=-60)
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["expired", "-"])
        self.assertIn("expired: start claude-work once to renew the login", result.err)
        self.assertEqual(self.server.requests, [])

    def test_t5_http_errors(self):
        self.ok("add", "work")
        self.credentials("work")
        for status, cell, message in ((401, "expired", "expired: start claude-work"), (403, "rejected", ""),
                                      (500, "error", "plan lookup failed for work: http_500 (via direct, ")):
            self.server.reply(status=status, raw=b'{"error": {"message": "boom ' + TOKEN.encode() + b'"}}')
            result = self.ok("list", env=self.live)
            self.assertEqual(self.row(result.out, "work")[2:], [cell, "-"], status)
            self.assertIn(message, result.err)
            self.assert_no_secret(result)

    def test_t6_malformed_responses(self):
        self.ok("add", "work")
        self.credentials("work")
        for raw in (b"<html>", b'{"account": {}}', b"[]", b"x" * 70000):
            self.server.reply(raw=raw)
            result = self.ok("list", env=self.live)
            self.assertEqual(self.row(result.out, "work")[2:], ["error", "-"], raw[:20])
            self.assertIn("plan lookup failed for work: malformed_response", result.err)
        # 字段里有终端控制序列或大写、空格：不能原样打到终端
        self.server.reply(org("\x1b[31mclaude_max", "default claude_max_5x", "Apple"))
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["unknown", "-"])
        self.assertNotIn("\x1b", result.out)
        # 尾部换行：`^…$` 配 match 会放过它，把表格的一行打断
        self.server.reply(org("claude_pro\n", "default_claude_ai\n", "apple_subscription\n"))
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["unknown", "-"])
        self.assertEqual(len(result.out.splitlines()), 2)

    def test_t10_timeout(self):
        self.ok("add", "work")
        self.credentials("work")
        self.server.hang = True
        started = time.time()
        result = self.ok("list", env=dict(self.live, MULTI_CLAUDE_TEST_PLAN_TIMEOUT="1"))
        self.assertLess(time.time() - started, 10)
        self.assertEqual(self.row(result.out, "work")[2:], ["error", "-"])
        self.assertIn("plan lookup failed for work: timeout", result.err)

    def test_t12_json(self):
        self.ok("add", "work")
        self.credentials("work")
        self.ok("add", "idle")
        self.credentials("idle", expires_in_s=-1)
        data = json.loads(self.ok("list", "--json", env=self.live).out)
        plans = {entry["name"]: entry["plan"] for entry in data["accounts"]}
        self.assertEqual(plans["work"], {
            "status": "ok", "plan": "max (5x)", "billing": "apple", "organization_type": "claude_max",
            "rate_limit_tier": "default_claude_max_5x", "billing_type": "apple_subscription", "reason": None})
        self.assertEqual(plans["idle"], {
            "status": "expired", "plan": None, "billing": None, "organization_type": None,
            "rate_limit_tier": None, "billing_type": None, "reason": None})
        self.assertEqual(data["schema_version"], 1)

    def test_t14_test_mode_without_endpoint_never_connects(self):
        self.ok("add", "work")
        self.credentials("work")
        result = self.ok("list")
        self.assertEqual(self.row(result.out, "work")[2:], ["error", "-"])
        self.assertIn("plan lookup failed for work: test_no_endpoint", result.err)
        self.assertNotIn("(via", result.err)
        self.assertEqual(self.server.requests, [])


class RouteTest(PlanTestCase):
    """查询走账号的代理设置，配置了代理时失败不改走直连。"""

    def setUp(self) -> None:
        super().setUp()
        self.proxy = FakeConnectProxy(self.server.port)
        self.addCleanup(self.proxy.close)
        self.proxied = {"MULTI_CLAUDE_TEST_PROFILE_URL":
                        "http://{}:{}/api/oauth/profile".format(PROXIED_HOST, self.server.port)}

    def test_t7_account_proxy(self):
        self.ok("add", "work", "--proxy", str(self.proxy.port))
        self.credentials("work")
        result = self.ok("list", env=self.proxied)
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        # http.client 在 3.12 起用 HTTP/1.1 发 CONNECT，之前是 HTTP/1.0
        self.assertEqual(len(self.proxy.connects), 1)
        self.assertRegex(self.proxy.connects[0][0],
                         r"^CONNECT {}:{} HTTP/1\.[01]$".format(re.escape(PROXIED_HOST), self.server.port))
        self.assertNotIn("Proxy-Authorization", self.proxy.connects[0][1])
        self.assertEqual(len(self.server.requests), 1)

    def test_t7_inherited_proxy_with_credentials(self):
        self.ok("add", "work")
        self.credentials("work")
        env = dict(self.proxied, HTTPS_PROXY="http://alice:p%40ss-SECRET@127.0.0.1:{}".format(self.proxy.port))
        self.server.reply(status=500, raw=b"{}")
        result = self.ok("list", env=env)
        expected = "Basic " + base64.b64encode(b"alice:p@ss-SECRET").decode()
        self.assertEqual(self.proxy.connects[0][1]["Proxy-Authorization"], expected)
        self.assertIn("http_500 (via 127.0.0.1:{}, ".format(self.proxy.port), result.err)
        for text in (result.out, result.err):
            self.assertNotIn("SECRET", text)
            self.assertNotIn("alice", text)

    def test_t8_dead_proxy_does_not_fall_back_to_direct(self):
        port = free_port()
        self.ok("add", "work", "--proxy", str(port))
        self.credentials("work")
        # 目标地址本身可以直连：如果实现退回直连，假服务就会收到请求
        # （写成 `localhost.` 是为了不被启动命令语义追加的 NO_PROXY=localhost,127.0.0.1,::1 命中）
        try:
            socket.getaddrinfo("localhost.", self.server.port)
        except OSError:
            self.skipTest("`localhost.` does not resolve here, so a fallback to direct could not be observed")
        target = "http://localhost.:{}/api/oauth/profile".format(self.server.port)
        result = self.ok("list", env={"MULTI_CLAUDE_TEST_PROFILE_URL": target})
        self.assertEqual(self.row(result.out, "work")[2:], ["error", "-"])
        self.assertIn("plan lookup failed for work: connect_failed (via 127.0.0.1:{}, ".format(port), result.err)
        self.assertEqual(self.server.requests, [])
        self.assert_no_secret(result)

    def test_t16_dropped_connection_is_retried_once_through_the_same_proxy(self):
        self.ok("add", "work", "--proxy", str(self.proxy.port))
        self.credentials("work")
        self.proxy.drop_first = 1
        result = self.ok("list", env=self.proxied)
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        self.assertEqual(len(self.proxy.connects), 2)
        self.assertNotIn("plan lookup failed", result.err)
        # 一直断开：重试一次后放弃，仍然不改走直连
        self.proxy.drop_first = 99
        self.server.requests.clear()
        result = self.ok("list", env=self.proxied)
        self.assertRegex(result.err, r"plan lookup failed for work: connect_failed \(via 127\.0\.0\.1:{}, "
                                     r"[\d.]+s, 2 attempts\)".format(self.proxy.port))
        self.assertEqual(len(self.proxy.connects), 4)
        self.assertEqual(self.server.requests, [])

    def test_t8_unsupported_proxy_scheme(self):
        self.ok("add", "work", "--proxy", "https://127.0.0.1:{}".format(self.proxy.port))
        self.credentials("work")
        result = self.ok("list", env=self.proxied)
        self.assertIn("plan lookup failed for work: proxy_unsupported", result.err)
        self.assertEqual(self.proxy.connects, [])
        self.assertEqual(self.server.requests, [])

    def test_bad_proxy_host_is_a_connect_failure(self):
        self.ok("add", "work")
        self.credentials("work")
        result = self.ok("list", env=dict(self.proxied, HTTPS_PROXY="http://a..b:1"))
        self.assertIn("plan lookup failed for work: connect_failed (via a..b:1, ", result.err)
        self.assertEqual(self.server.requests, [])

    def test_t9_proxy_off_ignores_inherited_proxy(self):
        self.ok("add", "work", "--proxy", "off")
        self.credentials("work")
        env = dict(self.live, HTTPS_PROXY="http://127.0.0.1:{}".format(self.proxy.port))
        result = self.ok("list", env=env)
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        self.assertEqual(self.proxy.connects, [])
        self.assertEqual(len(self.server.requests), 1)


class OfflineTest(PlanTestCase):
    def test_t11_offline_reads_nothing_and_keeps_the_old_output(self):
        self.ok("add", "work")
        self.credentials("work")
        env = dict(self.live, FAKE_SECURITY_RC="0")
        brief = self.ok("list", "--offline", env=env).out
        self.assertRegex(brief.splitlines()[0], r"^NAME\s+LOGIN\s+PROXY\s+SHARED\s+5H\s+7D\s+LAST USED\s+STATUS$")
        self.assertRegex(brief.splitlines()[1], r"^work\s+(file|keychain)\s+inherit\s+no\s+-\s+-\s+-\s+ok$")
        verbose = self.ok("list", "--verbose", "--offline", env=env).out
        self.assertRegex(verbose, r"NAME\s+IDENTITY\s+DIR\s+PROXY\s+SHARED\s+LAUNCHER\s+LOGIN\s+LINK")
        data = json.loads(self.ok("list", "--json", "--offline", env=env).out)
        self.assertNotIn("plan", data["accounts"][0])
        self.assertEqual(self.ok("list", "--names", "--offline", env=env).out, "work\n")
        self.assertEqual(self.server.requests, [])
        self.assert_no_token_read()

    def test_r2_other_commands_never_read_the_token(self):
        self.ok("add", "work")
        self.credentials("work")
        env = dict(self.live, FAKE_SECURITY_RC="0")
        self.ok("list", "--names", env=env)
        self.ok("usage", env=env)
        self.ok("usage", "--history", env=env)
        self.run_cli("doctor", env=env)
        self.assertEqual(self.server.requests, [])
        self.assert_no_token_read()

    def assert_no_token_read(self) -> None:
        for call in self.security_calls():
            self.assertNotIn("-w", call)

    def test_r3_completion_offers_offline(self):
        self.assertIn("--offline", self.ok("completion", "bash").out)


class KeychainTokenTest(CliTestCase):
    """macOS 分支：令牌从钥匙串读出；用假 security，任何平台都能跑。"""

    def setUp(self) -> None:
        super().setUp()
        self.secret = os.path.join(self.tmp, "secret.json")
        self.account = os.path.join(self.tmp, "acct")
        os.makedirs(self.account)
        patcher = mock.patch.dict(os.environ, {"PATH": self.env["PATH"], "USER": "tester",
                                               "FAKE_SECURITY_LOG": self.security_log,
                                               "FAKE_SECURITY_SECRET": self.secret})
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, rc: int, platform_name: str = "darwin") -> identity.OAuthToken:
        with mock.patch.dict(os.environ, {"FAKE_SECURITY_RC": str(rc)}), \
                mock.patch.object(identity, "sys", types.SimpleNamespace(platform=platform_name)):
            return identity.read_oauth_token("dir", self.account)

    def test_t13_keychain_token_and_arguments(self):
        self.write(self.secret, json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 1234.0}}))
        self.assertEqual(self.read(0), identity.OAuthToken(identity.TOKEN_OK, TOKEN, 1234))
        self.assertEqual(self.security_calls(), [["find-generic-password", "-a", "tester", "-w", "-s",
                                                  identity.service_name("dir", self.account)]])

    def test_keychain_states_and_file_fallback(self):
        self.write(self.secret, "{}")
        self.assertEqual(self.read(0).state, identity.TOKEN_NONE)
        self.assertEqual(self.read(44).state, identity.TOKEN_NONE)
        # 钥匙串读不出来（被锁等）且文件里也没有：不能报成“没登录”
        self.assertEqual(self.read(1).state, identity.TOKEN_UNREADABLE)
        # 钥匙串读不出来时退到凭据文件，与 Claude 的回退顺序一致
        self.write(identity.credentials_file(self.account), json.dumps({"claudeAiOauth": {"accessToken": "from-file"}}))
        self.assertEqual(self.read(1), identity.OAuthToken(identity.TOKEN_OK, "from-file", None))
        # 过期时刻不是有限数值（JSON 的 1e999 解析成 inf）：当作没有，不抛异常
        self.write(identity.credentials_file(self.account), '{"claudeAiOauth": {"accessToken": "from-file", "expiresAt": 1e999}}')
        self.assertEqual(self.read(1), identity.OAuthToken(identity.TOKEN_OK, "from-file", None))
        # 超出浮点范围的整数不能让读取抛异常
        huge = "1" + "0" * 400
        self.write(identity.credentials_file(self.account),
                   '{"claudeAiOauth": {"accessToken": "from-file", "expiresAt": %s}}' % huge)
        self.assertEqual(self.read(1), identity.OAuthToken(identity.TOKEN_OK, "from-file", int(huge)))
        # 其它平台不调用 security
        before = len(self.security_calls())
        self.assertEqual(self.read(0, "linux").access_token, "from-file")
        self.assertEqual(len(self.security_calls()), before)

    def test_token_must_be_header_safe(self):
        for bad in ("", "has space", "line\nbreak", "tok\n", 12345, None):
            self.write(self.secret, json.dumps({"claudeAiOauth": {"accessToken": bad}}))
            self.assertEqual(self.read(0).state, identity.TOKEN_NONE, repr(bad))
