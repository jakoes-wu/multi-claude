"""list 在访问令牌过期时自动刷新并写回凭据（方案 feature-token-refresh §8）。

刷新接口与套餐接口都是用例内的本地假服务。凭据用账号目录里的 `.credentials.json`
（Linux 只读它；macOS 上假 security 默认报告“钥匙串里没有”，随后同样读到它）；
钥匙串的写入分支单独用假 security 覆盖（F12）。
"""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from helpers import SRC, CliTestCase
from test_plan import TOKEN, PlanTestCase, _ThreadingServer

sys.path.insert(0, SRC)

from multi_claude import identity, refresh  # noqa: E402

OLD_REFRESH = "refresh-SECRET"
NEW_ACCESS = "tok-NEW-access-SECRET"
NEW_REFRESH = "refresh-NEW-SECRET"


def default_signals() -> None:
    """让被测命令以默认方式对待这三个信号。

    测试套件本身可能是在“忽略 SIGINT / SIGHUP”的环境里启动的（`nohup … &`、非交互 shell 的后台任务），
    忽略状态会被子进程继承，被测命令就收不到中断，信号用例也就什么都没测到。
    """
    for number in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
        signal.signal(number, signal.SIG_DFL)


class FakeTokenServer(object):
    """假的刷新接口：记录收到的 JSON 请求体，按设定应答。"""

    def __init__(self) -> None:
        self.bodies = []
        self.status = 200
        self.reply = {"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 28800,
                      "refresh_token_expires_in": 864000, "scope": "user:inference user:profile"}
        self.raw = None
        # 应答前等待的秒数，以及应答前要执行的动作（用来模拟“发请求期间别的进程改了凭据”）
        self.delay_s = 0.0
        self.before_reply = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.bodies.append((dict(self.headers), json.loads(self.rfile.read(length).decode())))
                if owner.delay_s:
                    time.sleep(owner.delay_s)
                if owner.before_reply:
                    owner.before_reply()
                body = owner.raw if owner.raw is not None else json.dumps(owner.reply).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = _ThreadingServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/v1/oauth/token".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RefreshTestCase(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tokens = FakeTokenServer()
        self.addCleanup(self.tokens.close)
        self.live = dict(self.live, MULTI_CLAUDE_TEST_TOKEN_URL=self.tokens.url)
        self.ok("add", "work")
        self.account = os.path.join(self.root, "work")
        self.path = os.path.join(self.account, ".credentials.json")

    def expired_credentials(self, **oauth_overrides) -> dict:
        """写一份访问令牌已过期的凭据，带上刷新时必须原样保留的其它内容。"""
        oauth = {"accessToken": TOKEN, "refreshToken": OLD_REFRESH,
                 "expiresAt": int((time.time() - 3600) * 1000), "refreshTokenExpiresAt": 1,
                 "scopes": ["user:inference", "user:profile"], "subscriptionType": "max",
                 "rateLimitTier": "default_claude_max_5x"}
        oauth.update(oauth_overrides)
        document = {"claudeAiOauth": oauth, "mcpOAuth": {"server": {"token": "mcp-SECRET"}}}
        self.write(self.path, json.dumps(document), 0o600)
        return document

    def stored(self) -> dict:
        with open(self.path) as handle:
            return json.load(handle)

    def raw(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()

    def locks(self) -> list:
        return refresh.lock_paths(self.account)

    def assert_no_secret(self, result) -> None:
        for secret in (TOKEN, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH, "mcp-SECRET"):
            self.assertNotIn(secret, result.out)
            self.assertNotIn(secret, result.err)


class RenewTest(RefreshTestCase):
    def test_f1_expired_token_is_renewed_and_saved(self):
        before = self.expired_credentials()
        started_ms = int(time.time() * 1000)
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        self.assertNotIn("token", result.err)
        # 刷新请求：与 Claude Code 相同的 JSON 请求体，范围取凭据里保存的
        self.assertEqual(len(self.tokens.bodies), 1)
        headers, body = self.tokens.bodies[0]
        self.assertEqual(body, {"grant_type": "refresh_token", "refresh_token": OLD_REFRESH,
                                "client_id": refresh.DEFAULT_CLIENT_ID, "scope": "user:inference user:profile"})
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", headers)
        # 套餐查询用的是新令牌，过期的那个没有发出去
        self.assertEqual([headers["Authorization"] for _, headers in self.server.requests], ["Bearer " + NEW_ACCESS])
        # 凭据：令牌相关字段更新，其余原样保留，权限不变
        after = self.stored()
        oauth = after["claudeAiOauth"]
        self.assertEqual((oauth["accessToken"], oauth["refreshToken"]), (NEW_ACCESS, NEW_REFRESH))
        self.assertAlmostEqual(oauth["expiresAt"], started_ms + 28800 * 1000, delta=60000)
        self.assertAlmostEqual(oauth["refreshTokenExpiresAt"], started_ms + 864000 * 1000, delta=60000)
        self.assertEqual(oauth["scopes"], ["user:inference", "user:profile"])
        self.assertEqual((oauth["subscriptionType"], oauth["rateLimitTier"]), ("max", "default_claude_max_5x"))
        self.assertEqual(after["mcpOAuth"], before["mcpOAuth"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # F8：两把锁都已放掉
        self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [])
        self.assert_no_secret(result)
        # 刷新过一次后令牌有效，再运行不会再刷新
        self.ok("list", env=self.live)
        self.assertEqual(len(self.tokens.bodies), 1)

    def test_f2_response_without_refresh_token_or_scope(self):
        self.expired_credentials(clientId="custom-client")
        self.tokens.reply = {"access_token": NEW_ACCESS, "expires_in": 600}
        self.ok("list", env=self.live)
        self.assertEqual(self.tokens.bodies[0][1]["client_id"], "custom-client")
        oauth = self.stored()["claudeAiOauth"]
        self.assertEqual((oauth["accessToken"], oauth["refreshToken"]), (NEW_ACCESS, OLD_REFRESH))
        self.assertEqual(oauth["scopes"], ["user:inference", "user:profile"])
        self.assertEqual(oauth["refreshTokenExpiresAt"], 1)

    def test_f3_f4_no_refresh_offline_and_names_write_nothing(self):
        self.expired_credentials()
        before = self.raw()
        result = self.ok("list", "--no-refresh", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["token expired", "-"])
        self.assertNotIn("token refresh failed", result.err)
        self.assertIn("the subscription is not affected", result.err)
        self.ok("list", "--offline", env=self.live)
        self.ok("list", "--names", env=self.live)
        self.assertEqual(self.tokens.bodies, [])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.raw(), before)

    def test_f5_rejected_refresh_token_needs_a_new_login(self):
        self.expired_credentials()
        before = self.raw()
        self.tokens.status = 400
        self.tokens.reply = {"error": "invalid_grant", "error_description": "gone " + OLD_REFRESH}
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["token expired", "-"])
        self.assertIn("token refresh failed for work: refresh_rejected; run multi-claude login work", result.err)
        # 不清空、不改动凭据（Claude Code 自己会清空，这里留给它或用户处理）
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.server.requests, [])
        self.assert_no_secret(result)

    def test_f6_failed_refresh_changes_nothing(self):
        self.expired_credentials()
        before = self.raw()
        cases = [(500, {"error": "boom"}, None, "http_500"),
                 (200, None, b"<html>", "malformed_response"),
                 (200, {"access_token": "bad token\n", "expires_in": 600}, None, "malformed_response"),
                 (200, {"refresh_token": NEW_REFRESH, "expires_in": 600}, None, "malformed_response"),
                 (401, {"error": {"type": "invalid_grant"}}, None, "refresh_rejected")]
        for status, reply, raw, reason in cases:
            self.tokens.status, self.tokens.reply, self.tokens.raw = status, reply, raw
            result = self.ok("list", env=self.live)
            self.assertIn("token refresh failed for work: {}".format(reason), result.err, reason)
            self.assertEqual(self.row(result.out, "work")[2:], ["token expired", "-"])
            self.assertEqual(self.raw(), before)
            self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [])

    def test_f6_usable_token_is_saved_whatever_else_the_response_says(self):
        """换到了可用的访问令牌就必须存下来：旧的刷新令牌多半已经作废，丢掉响应等于让账号掉线。"""
        odd = [{"expires_in": "28800"}, {"expires_in": 0}, {"expires_in": True}, {"expires_in": 1e12}, {},
               {"expires_in": 600, "refresh_token": None}, {"expires_in": 600, "refresh_token": ""},
               {"expires_in": 600, "scope": 7, "refresh_token_expires_in": "soon"}]
        for index, extra in enumerate(odd):
            self.expired_credentials()
            access = "{}-{}".format(NEW_ACCESS, index)
            self.tokens.reply = dict({"access_token": access}, **extra)
            started_ms = int(time.time() * 1000)
            result = self.ok("list", env=self.live)
            self.assertNotIn("could not be saved", result.err, extra)
            oauth = self.stored()["claudeAiOauth"]
            self.assertEqual(oauth["accessToken"], access, extra)
            # 响应里没有可用的刷新令牌时沿用发出去的那个
            self.assertEqual(oauth["refreshToken"], OLD_REFRESH, extra)
            self.assertEqual(oauth["scopes"], ["user:inference", "user:profile"])
            if extra.get("expires_in") in ("28800", 600):
                self.assertGreater(oauth["expiresAt"], started_ms + 500 * 1000, extra)
            else:
                # 读不出有效期：按“已经过期”保存，下次由 Claude Code 自己再刷
                self.assertAlmostEqual(oauth["expiresAt"], started_ms, delta=60000, msg=extra)

    def test_f7_busy_lock_is_respected(self):
        self.expired_credentials()
        before = self.raw()
        for index, held in enumerate(self.locks()):
            os.mkdir(held)
            result = self.ok("list", env=self.live)
            self.assertIn("token refresh failed for work: lock_busy", result.err)
            self.assertEqual(self.tokens.bodies, [])
            self.assertEqual(self.raw(), before)
            # 别人的锁还在；自己先取到的那一把（如果有）已经放掉
            self.assertTrue(os.path.isdir(held))
            self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [held], index)
            os.rmdir(held)

    def test_f7_dead_lock_is_cleared_and_a_live_one_is_not(self):
        self.expired_credentials()
        dead = self.locks()[0]
        os.mkdir(dead)
        # 活着的持有者每 5 秒更新一次修改时间；一分钟没动还不算死，五分钟以上才清
        minute_ago = time.time() - 90
        os.utime(dead, (minute_ago, minute_ago))
        self.assertIn("lock_busy", self.ok("list", env=self.live).err)
        hour_ago = time.time() - 3600
        os.utime(dead, (hour_ago, hour_ago))
        result = self.ok("list", env=self.live)
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [])

    def test_f7_lock_taken_over_by_someone_else_is_left_alone(self):
        """持锁期间锁被别人删掉重建（本进程被挂起太久时会这样）：放锁时不能删别人的。"""
        self.expired_credentials()

        def takeover():
            for path in self.locks():
                # 原地删掉重建，文件系统可能复用同一个 inode 编号：归属判断不能只靠它
                os.rmdir(path)
                os.mkdir(path)

        self.tokens.before_reply = takeover
        self.ok("list", env=self.live)
        self.assertEqual([os.path.isdir(path) for path in self.locks()], [True, True])

    def test_lock_cannot_be_created(self):
        if self.running_as_root():
            self.skipTest("root ignores directory permissions")
        self.expired_credentials()
        os.chmod(self.account, 0o500)
        self.addCleanup(os.chmod, self.account, 0o700)
        result = self.ok("list", env=self.live)
        self.assertIn("token refresh failed for work: lock_error", result.err)
        self.assertEqual(self.tokens.bodies, [])

    def test_symlinked_credentials_file_stays_a_link(self):
        self.expired_credentials()
        real = os.path.join(self.tmp, "real-credentials.json")
        os.rename(self.path, real)
        os.symlink(real, self.path)
        self.ok("list", env=self.live)
        self.assertTrue(os.path.islink(self.path))
        with open(real) as handle:
            self.assertEqual(json.load(handle)["claudeAiOauth"]["accessToken"], NEW_ACCESS)

    def test_f9_no_refresh_token(self):
        self.expired_credentials(refreshToken="")
        result = self.ok("list", env=self.live)
        self.assertIn("token refresh failed for work: no_refresh_token; run multi-claude login work", result.err)
        self.assertEqual(self.tokens.bodies, [])

    def test_f10_another_process_renewed_meanwhile(self):
        document = self.expired_credentials()
        theirs = dict(document["claudeAiOauth"], accessToken="tok-THEIRS", refreshToken="refresh-THEIRS",
                      expiresAt=int((time.time() + 3600) * 1000))

        def someone_else_saves():
            self.write(self.path, json.dumps(dict(document, claudeAiOauth=theirs)), 0o600)

        self.tokens.before_reply = someone_else_saves
        data = json.loads(self.ok("list", "--json", env=self.live).out)
        self.assertEqual(data["accounts"][0]["plan"]["refresh"], "adopted")
        self.assertEqual(data["accounts"][0]["plan"]["plan"], "max (5x)")
        # 没有覆盖别人写的凭据，查询用的也是别人的令牌
        self.assertEqual(self.stored()["claudeAiOauth"], theirs)
        self.assertEqual(self.server.requests[0][1]["Authorization"], "Bearer tok-THEIRS")

    def test_f10_changed_to_something_unusable(self):
        document = self.expired_credentials()
        theirs = dict(document["claudeAiOauth"], refreshToken="refresh-THEIRS")
        self.tokens.before_reply = lambda: self.write(
            self.path, json.dumps(dict(document, claudeAiOauth=theirs)), 0o600)
        result = self.ok("list", env=self.live)
        self.assertIn("token refresh failed for work: credentials_changed", result.err)
        self.assertEqual(self.stored()["claudeAiOauth"], theirs)

    def test_f10_cleared_credentials_are_restored(self):
        """Claude Code 在刷新令牌被拒时会把三个令牌字段清空；保存着的刷新令牌是空串时允许写回。"""
        document = self.expired_credentials()
        cleared = dict(document["claudeAiOauth"], accessToken="", refreshToken="", expiresAt=0)
        self.tokens.before_reply = lambda: self.write(
            self.path, json.dumps(dict(document, claudeAiOauth=cleared)), 0o600)
        self.ok("list", env=self.live)
        self.assertEqual(self.stored()["claudeAiOauth"]["accessToken"], NEW_ACCESS)

    @staticmethod
    def running_as_root() -> bool:
        return os.geteuid() == 0

    def test_f11_save_failure_is_reported(self):
        if self.running_as_root():
            self.skipTest("root ignores directory permissions")
        self.expired_credentials()
        before = self.raw()
        # 目录只读：临时文件建不出来，原子替换无从做起；发请求前要取的锁建在目录里，所以在应答前才改权限
        self.tokens.before_reply = lambda: os.chmod(self.account, 0o500)
        self.addCleanup(os.chmod, self.account, 0o700)
        result = self.ok("list", env=self.live)
        os.chmod(self.account, 0o700)
        self.assertEqual(self.row(result.out, "work")[2:], ["max (5x)", "apple"])
        self.assertIn("the renewed login of work could not be saved", result.err)
        self.assertIn("multi-claude login work", result.err)
        self.assertEqual(self.raw(), before)
        self.assert_no_secret(result)

    def test_f13_total_timeout_does_not_abandon_a_refresh_in_flight(self):
        self.expired_credentials()
        self.tokens.delay_s = 3.0
        started = time.time()
        result = self.ok("list", env=dict(self.live, MULTI_CLAUDE_TEST_PLAN_TOTAL_TIMEOUT="1"))
        self.assertGreaterEqual(time.time() - started, 3.0)
        # 总时限早就到了，但命令等刷新写完才退出：新令牌已经在凭据里
        self.assertEqual(self.stored()["claudeAiOauth"]["accessToken"], NEW_ACCESS)
        # 不断言这一行显示的套餐：总时限早已过去，刷新写完后的套餐查询赶不赶得上都算对（要么套餐，要么超时）
        self.assertIn(self.row(result.out, "work")[2], ("max (5x)", "error"))
        self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [])

    def interrupt(self, env, signal_number, after_s=1.0):
        """启动 list，等刷新请求到达假服务后发一个信号；返回 (子进程, 发信号的时刻)。"""
        proc = subprocess.Popen([sys.executable, "-m", "multi_claude", "list"], env=self._merged(env),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                cwd=self.tmp, preexec_fn=default_signals)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.time() + 20
        while not self.tokens.bodies and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.tokens.bodies), 1, "the refresh request never arrived")
        time.sleep(after_s)
        proc.send_signal(signal_number)
        return proc, time.time()

    def test_f17_ctrl_c_during_a_refresh_waits_for_the_save(self):
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self.expired_credentials()
            self.tokens.bodies.clear()
            self.tokens.delay_s = 3.0
            proc, sent_at = self.interrupt(self.live, signal_number)
            out, err = proc.communicate(timeout=60)
            # 没有立刻退出，而是等到新令牌写完
            self.assertGreater(time.time() - sent_at, 1.0, signal_number)
            self.assertIn("saving the renewed login of work", err)
            self.assertEqual(self.stored()["claudeAiOauth"]["accessToken"], NEW_ACCESS, signal_number)
            self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [])
            self.assertNotEqual(proc.returncode, 0)

    def test_f17_hangup_twice_with_a_dead_terminal_still_saves(self):
        """关终端：进程会连着收到两个 SIGHUP，stderr 也已经不可写；两样都不能打断保存。"""
        self.expired_credentials()
        self.tokens.delay_s = 3.0
        reader, writer = os.pipe()
        os.close(reader)
        proc = subprocess.Popen([sys.executable, "-m", "multi_claude", "list"], env=self._merged(self.live),
                                stdout=subprocess.DEVNULL, stderr=writer, cwd=self.tmp,
                                preexec_fn=default_signals)
        os.close(writer)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.time() + 20
        while not self.tokens.bodies and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.7)
        sent_at = time.time()
        proc.send_signal(signal.SIGHUP)
        time.sleep(0.05)
        proc.send_signal(signal.SIGHUP)
        proc.wait(timeout=60)
        self.assertGreater(time.time() - sent_at, 1.0)
        self.assertEqual(self.stored()["claudeAiOauth"]["accessToken"], NEW_ACCESS)
        self.assertEqual([path for path in self.locks() if os.path.lexists(path)], [])

    def test_f17_two_different_signals_at_once_still_save(self):
        self.expired_credentials()
        self.tokens.delay_s = 3.0
        proc, sent_at = self.interrupt(self.live, signal.SIGHUP)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)
        self.assertGreater(time.time() - sent_at, 1.0)
        self.assertEqual(self.stored()["claudeAiOauth"]["accessToken"], NEW_ACCESS)

    def test_f17_second_ctrl_c_quits(self):
        self.expired_credentials()
        self.tokens.delay_s = 8.0
        proc, _ = self.interrupt(self.live, signal.SIGINT)
        time.sleep(1.0)
        proc.send_signal(signal.SIGINT)
        proc.communicate(timeout=6)
        self.assertNotEqual(proc.returncode, 0)

    def test_f18_ctrl_c_while_waiting_for_another_account(self):
        """主线程按配置顺序等账号：正在等的是别的账号时收到 Ctrl-C，后面那个正在刷新的账号也必须等完。"""
        # work（先注册）令牌有效，但套餐接口挂起；late 令牌过期，刷新接口 3 秒后应答
        self.credentials("work")
        self.ok("add", "late")
        late_path = os.path.join(self.root, "late", ".credentials.json")
        self.path = late_path
        self.expired_credentials()
        self.server.hang = True
        self.tokens.delay_s = 3.0
        proc, sent_at = self.interrupt(self.live, signal.SIGINT)
        proc.communicate(timeout=60)
        self.assertGreater(time.time() - sent_at, 1.0)
        self.assertEqual(self.stored()["claudeAiOauth"]["accessToken"], NEW_ACCESS)
        self.assertEqual([path for path in refresh.lock_paths(os.path.join(self.root, "late"))
                          if os.path.lexists(path)], [])

    def test_f15_json_refresh_field(self):
        self.expired_credentials()
        plan_of = lambda *args: json.loads(self.ok("list", "--json", *args, env=self.live).out)["accounts"][0]["plan"]
        skipped = plan_of("--no-refresh")
        self.assertEqual((skipped["status"], skipped["refresh"]), ("expired", None))
        self.tokens.status = 503
        failed = plan_of()
        self.assertEqual((failed["status"], failed["refresh"]), ("expired", "http_503"))
        self.tokens.status = 200
        renewed = plan_of()
        self.assertEqual((renewed["status"], renewed["refresh"]), ("ok", "refreshed"))
        again = plan_of()
        self.assertEqual((again["status"], again["refresh"]), ("ok", None))

    def test_test_mode_without_token_endpoint_never_connects(self):
        self.expired_credentials()
        env = dict(self.live)
        del env["MULTI_CLAUDE_TEST_TOKEN_URL"]
        result = self.ok("list", env=env)
        self.assertIn("token refresh failed for work: test_no_endpoint", result.err)
        self.assertEqual(self.tokens.bodies, [])

    def test_r2_other_commands_never_write_credentials(self):
        self.expired_credentials()
        before = self.raw()
        env = dict(self.live, FAKE_SECURITY_RC="0")
        for args in (["list", "--names"], ["list", "--offline"], ["list", "--no-refresh"], ["usage"], ["doctor"]):
            self.run_cli(*args, env=env)
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.tokens.bodies, [])
        for call in self.security_calls():
            self.assertNotIn("-i", call)
            self.assertNotIn("add-generic-password", call)


class LockStampTest(unittest.TestCase):
    """锁指纹里的修改时间必须是本工具写入的精细时间，不能交给内核取。

    ext4 上几毫秒内删掉重建的锁目录，inode 和内核记的修改时间都与原来相同；只有自己写入的时间能分辨出来。
    这里不依赖 inode 复用，在任何文件系统上确定性地检查“建锁和保活都写入了 time_ns 的值”。
    """

    # 低位非零：内核按时钟节拍记的时间不会恰好是这个值
    STAMP_NS = 1_700_000_000_123_456_789

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcl-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, refresh.LOCK_NAME)
        os.mkdir(self.path)

    def test_creating_and_touching_write_the_fine_clock(self):
        # refresh.time 就是标准库 time：桩只包住这两次调用，不影响进程里的其它代码
        with mock.patch.object(refresh.time, "time_ns", return_value=self.STAMP_NS):
            lock = refresh._Lock(self.path)
        self.assertEqual(os.lstat(self.path).st_mtime_ns, self.STAMP_NS)
        self.assertTrue(lock.is_mine())

        later = self.STAMP_NS + 5_000_000_000
        with mock.patch.object(refresh.time, "time_ns", return_value=later):
            lock.touch()
        self.assertEqual(os.lstat(self.path).st_mtime_ns, later)
        self.assertTrue(lock.is_mine())


class KeychainWriteTest(CliTestCase):
    """macOS 分支：凭据在钥匙串里，写回走 `security -i`；用假 security，任何平台都能跑。"""

    def setUp(self) -> None:
        super().setUp()
        self.secret = os.path.join(self.tmp, "secret.json")
        self.account = os.path.join(self.tmp, "acct")
        os.makedirs(self.account)
        for patcher in (mock.patch.dict(os.environ, {"PATH": self.env["PATH"], "USER": "tester",
                                                     "FAKE_SECURITY_LOG": self.security_log,
                                                     "FAKE_SECURITY_SECRET": self.secret,
                                                     "FAKE_SECURITY_RC": "0"}),
                        mock.patch.object(identity, "sys", types.SimpleNamespace(platform="darwin"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.document = {"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": OLD_REFRESH, "expiresAt": 1000,
                                           "scopes": ["user:inference"]},
                         "mcpOAuth": {"keep": "me"}}
        self.write(self.secret, json.dumps(self.document))
        self.posted = []

    def post(self, body):
        self.posted.append(body)
        return None, 200, json.dumps({"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH,
                                      "expires_in": 100}).encode()

    def renew(self, on_commit=lambda: True):
        return refresh.renew("dir", self.account, self.account, int(time.time() * 1000), self.post, on_commit)

    def test_f12_keychain_is_updated_through_stdin(self):
        outcome = self.renew()
        self.assertEqual((outcome.status, outcome.access_token), (refresh.REFRESHED, NEW_ACCESS))
        with open(self.secret) as handle:
            saved = json.load(handle)
        self.assertEqual((saved["claudeAiOauth"]["accessToken"], saved["claudeAiOauth"]["refreshToken"]),
                         (NEW_ACCESS, NEW_REFRESH))
        self.assertEqual(saved["mcpOAuth"], {"keep": "me"})
        service = identity.service_name("dir", self.account)
        writes = [call for call in self.security_calls() if call[0] == "-i"]
        self.assertEqual(writes, [["-i", 'stdin: add-generic-password -U -a "tester" -s "{}" -X <hex>'.format(service)]])
        # 内容只经 stdin 传递：任何一次调用的参数里都没有十六进制内容，也没有令牌
        hexed = json.dumps(saved, separators=(",", ":")).encode().hex()
        for call in self.security_calls():
            self.assertNotIn(hexed, " ".join(call))
            self.assertNotIn(NEW_ACCESS, " ".join(call))
        self.assertEqual([path for path in refresh.lock_paths(self.account) if os.path.lexists(path)], [])

    def test_oversized_payload_falls_back_to_arguments(self):
        self.document["mcpOAuth"] = {"big": "x" * 3000}
        self.write(self.secret, json.dumps(self.document))
        self.assertEqual(self.renew().status, refresh.REFRESHED)
        with open(self.secret) as handle:
            saved = json.load(handle)
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], NEW_ACCESS)
        self.assertEqual(saved["mcpOAuth"], {"big": "x" * 3000})
        # `security -i` 的一行有长度上限，超过时与 Claude Code 一样改用参数
        self.assertEqual([call for call in self.security_calls() if call[0] == "-i"], [])
        calls = [call for call in self.security_calls() if call[0] == "add-generic-password"]
        self.assertEqual(calls[0][:7], ["add-generic-password", "-U", "-a", "tester", "-s",
                                        identity.service_name("dir", self.account), "-X"])

    def test_missing_config_dir(self):
        outcome = refresh.renew("dir", self.account, os.path.join(self.tmp, "nowhere"),
                                int(time.time() * 1000), self.post, lambda: True)
        self.assertEqual(outcome.status, refresh.NO_REFRESH_TOKEN)
        self.assertEqual(self.posted, [])

    def test_keychain_account_rejects_a_trailing_newline(self):
        with mock.patch.dict(os.environ, {"USER": "bob\n"}):
            self.assertEqual(identity.keychain_account(), "claude-code-user")

    def test_keychain_write_failure_is_a_save_failure(self):
        with mock.patch.dict(os.environ, {"FAKE_SECURITY_WRITE_RC": "1"}):
            outcome = self.renew()
        self.assertEqual((outcome.status, outcome.access_token), (refresh.SAVE_FAILED, NEW_ACCESS))
        with open(self.secret) as handle:
            self.assertEqual(json.load(handle), self.document)

    def test_abandoned_before_posting(self):
        outcome = self.renew(on_commit=lambda: False)
        self.assertEqual(outcome.status, refresh.ABANDONED)
        self.assertEqual(self.posted, [])
        self.assertEqual([path for path in refresh.lock_paths(self.account) if os.path.lexists(path)], [])

    def test_valid_token_is_adopted_without_posting(self):
        self.document["claudeAiOauth"]["expiresAt"] = int((time.time() + 600) * 1000)
        self.write(self.secret, json.dumps(self.document))
        outcome = self.renew()
        self.assertEqual((outcome.status, outcome.access_token), (refresh.ADOPTED, TOKEN))
        self.assertEqual(self.posted, [])
