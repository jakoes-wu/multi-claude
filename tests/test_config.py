"""方案 §8 第 6、8 条（校验部分）：代理解析、账号名、额外环境变量的键与值。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from multi_claude.config import normalize_proxy, validate_env_key, validate_name  # noqa: E402

from helpers import CliTestCase  # noqa: E402

REJECTED_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_CUSTOM_OAUTH_URL", "HOME",
                 "USER", "https_proxy", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "MCP_CLIENT_SECRET",
                 "MY_TOKEN", "DB_PASSWORD", "X_API_KEY", "1BAD", "A-B", "claude_config_dir")
ACCEPTED_KEYS = ("MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
                 "CLAUDE_CODE_PLUGIN_CACHE_DIR")


class ProxyParsingTest(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(normalize_proxy("7901"), "http://127.0.0.1:7901")
        self.assertEqual(normalize_proxy("https://proxy.local:8443"), "https://proxy.local:8443")
        self.assertEqual(normalize_proxy("http://proxy.local:8080/"), "http://proxy.local:8080")
        self.assertEqual(normalize_proxy("http://[::1]:3128"), "http://[::1]:3128")
        self.assertEqual(normalize_proxy("off"), "off")
        self.assertEqual(normalize_proxy("inherit"), "inherit")

    def test_invalid_values(self):
        for value in ("0", "70000", "ftp://127.0.0.1:21", "http://127.0.0.1:8080/path",
                      "http://user:pw@127.0.0.1:8080", "http://127.0.0.1", "http://:8080", "",
                      "socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_proxy(value)

    def test_socks_message_cites_documentation(self):
        with self.assertRaises(ValueError) as caught:
            normalize_proxy("socks5h://127.0.0.1:1080")
        self.assertIn("SOCKS proxies are not supported by Claude Code", str(caught.exception))


class ProxyCliExitCodeTest(CliTestCase):
    BAD = ("0", "70000", "ftp://127.0.0.1:21", "http://127.0.0.1:8080/p", "http://u:p@127.0.0.1:1",
           "socks5://127.0.0.1:1080", "socks5h://127.0.0.1:1080")

    def test_invalid_proxy_on_command_line_returns_2(self):
        for value in self.BAD:
            with self.subTest(value=value):
                self.assertEqual(self.run_cli("add", "work", "--proxy", value).code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.state, "config.json")))
        self.ok("add", "work")
        before = self.snapshot()
        result = self.run_cli("proxy", "work", "socks5://127.0.0.1:1080")
        self.assertEqual(result.code, 2, result)
        self.assertIn("SOCKS proxies are not supported", result.err)
        self.assertEqual(before, self.snapshot())

    def test_invalid_proxy_in_config_file_returns_1(self):
        for value in self.BAD:
            with self.subTest(value=value):
                path = self.write(os.path.join(self.tmp, "bad.json"), json.dumps(
                    {"version": 1, "accounts": {"work": {"proxy": value}}}))
                before = self.snapshot()
                result = self.run_cli("apply", "-f", path)
                self.assertEqual(result.code, 1, result)
                self.assertEqual(before, self.snapshot())


class NameTest(CliTestCase):
    def test_rejected_names(self):
        for name in ("-x", ".hidden", "a/b", "a" * 65, ""):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_name(name)
        self.assertEqual(self.run_cli("add", ".hidden").code, 2)
        # "-x" 会先被 argparse 当成未知选项，同样返回 2。
        self.assertEqual(self.run_cli("add", "-x").code, 2)
        self.assertEqual(self.run_cli("add", "--", "-x").code, 2)

    def test_email_names_accepted(self):
        validate_name("someone+test@example.com")

    def test_case_insensitive_duplicate(self):
        self.ok("add", "Work")
        result = self.ok("add", "work")
        self.assertNotIn("create", result.out)
        self.assertEqual(list(self.config_data()["accounts"]), ["Work"])


class EnvKeyTest(CliTestCase):
    def test_validate_env_key(self):
        for key in REJECTED_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    validate_env_key(key)
        for key in ACCEPTED_KEYS:
            with self.subTest(key=key):
                validate_env_key(key)

    def test_command_line_returns_2(self):
        self.ok("add", "work")
        before = self.snapshot()
        for key in REJECTED_KEYS:
            with self.subTest(key=key):
                self.assertEqual(self.run_cli("env", "work", key + "=x").code, 2)
        result = self.run_cli("env", "work", "A=line1\nline2")
        self.assertEqual(result.code, 2, result)
        self.assertEqual(self.run_cli("args", "work", "--", "a\nb").code, 2)
        self.assertEqual(before, self.snapshot())
        for key in ACCEPTED_KEYS:
            with self.subTest(key=key):
                self.ok("env", "work", key + "=1")

    def test_config_file_returns_1(self):
        for env in ({"ANTHROPIC_API_KEY": "x"}, {"HOME": "/x"}, {"OK": "a\nb"}, {"OK": 1}):
            with self.subTest(env=env):
                path = self.write(os.path.join(self.tmp, "bad.json"), json.dumps(
                    {"version": 1, "defaults": {"env": env}, "accounts": {"work": {}}}))
                self.assertEqual(self.run_cli("apply", "-f", path).code, 1)
        path = self.write(os.path.join(self.tmp, "bad.json"), json.dumps(
            {"version": 1, "accounts": {"work": {"env": {"MY_SECRET": "x"}}}}))
        self.assertEqual(self.run_cli("apply", "-f", path).code, 1)
        path = self.write(os.path.join(self.tmp, "bad.json"), json.dumps(
            {"version": 1, "accounts": {"work": {"args": ["a", 1]}}}))
        self.assertEqual(self.run_cli("apply", "-f", path).code, 1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "config.json")))

    def test_credential_error_does_not_echo_value(self):
        self.ok("add", "work")
        result = self.run_cli("env", "work", "ANTHROPIC_API_KEY=sk-secret-value")
        self.assertEqual(result.code, 2)
        self.assertNotIn("sk-secret-value", result.err + result.out)


if __name__ == "__main__":
    unittest.main()
