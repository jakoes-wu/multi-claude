"""方案 §8 第 4、5 条：钥匙串服务名计算与只读登录探测。"""

import os
import subprocess
import sys
import types
import unicodedata
import unittest
from unittest import mock

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import identity  # noqa: E402


class ServiceNameTest(unittest.TestCase):
    def test_matches_shasum(self):
        path = "/tmp/mcl-example/.cc/work"
        digest = subprocess.run(["sh", "-c", 'printf %s "$1" | (shasum -a 256 2>/dev/null || sha256sum)', "sh",
                                 path], stdout=subprocess.PIPE, universal_newlines=True).stdout.split()[0]
        self.assertEqual(identity.service_name("dir", path), "Claude Code-credentials-" + digest[:8])

    def test_nfc_normalization(self):
        nfd = unicodedata.normalize("NFD", "/tmp/café/work")
        nfc = unicodedata.normalize("NFC", nfd)
        self.assertNotEqual(nfd, nfc)
        self.assertEqual(identity.service_name("dir", nfd), identity.service_name("dir", nfc))

    def test_default_identity_has_no_suffix(self):
        self.assertEqual(identity.service_name("default", "/anything"), "Claude Code-credentials")

    def test_keychain_account(self):
        with mock.patch.dict(os.environ, {"USER": "a b"}):
            self.assertEqual(identity.keychain_account(), "claude-code-user")
        with mock.patch.dict(os.environ, {"USER": "alice.b-1"}):
            self.assertEqual(identity.keychain_account(), "alice.b-1")


class ProbeTest(CliTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"PATH": self.env["PATH"], "USER": "tester",
                                               "FAKE_SECURITY_LOG": self.security_log})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account = os.path.join(self.tmp, "acct")
        os.makedirs(self.account)

    def probe(self, platform_name, rc):
        with mock.patch.dict(os.environ, {"FAKE_SECURITY_RC": str(rc)}), \
                mock.patch.object(identity, "sys", types.SimpleNamespace(platform=platform_name)):
            return identity.probe("dir", self.account)

    def test_keychain_results_and_arguments(self):
        self.assertEqual(self.probe("darwin", 0), "keychain")
        self.assertEqual(self.probe("darwin", 44), "none")
        self.assertEqual(self.probe("darwin", 1), "unknown")
        expected = ["find-generic-password", "-a", "tester", "-s", identity.service_name("dir", self.account)]
        calls = self.security_calls()
        self.assertEqual(calls, [expected] * 3)
        for call in calls:
            self.assertNotIn("-w", call)
            self.assertNotIn("-g", call)

    def test_credentials_file(self):
        self.write(identity.credentials_file(self.account), "{}", 0o600)
        self.assertEqual(self.probe("darwin", 44), "file")
        self.assertEqual(self.probe("linux", 0), "file")

    def test_linux_does_not_call_security(self):
        self.assertEqual(self.probe("linux", 0), "none")
        self.assertEqual(self.security_calls(), [])


if __name__ == "__main__":
    unittest.main()
