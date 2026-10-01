"""占用方式的文字说明、Claude 进程判定规则与 KERN_PROCARGS2 解析（方案 §5.1.7 第 2、5 条）。"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from multi_claude import platform  # noqa: E402
from multi_claude.platform import describe_usage, is_claude_process  # noqa: E402


class DescribeUsageTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(describe_usage("cwd"), "cwd")
        self.assertEqual(describe_usage("txt"), "executable")
        self.assertEqual(describe_usage("mem"), "mapped")
        self.assertEqual(describe_usage("3"), "fd 3")
        self.assertEqual(describe_usage("12u"), "fd 12")
        self.assertEqual(describe_usage("rtd"), "rtd")


class ClaudeProcessRuleTest(unittest.TestCase):
    def test_claude_processes(self):
        cases = [
            (["claude"], ""),
            (["/opt/bin/claude", "--resume"], ""),
            (["claude bg-pty-host"], ""),
            (["/x/.local/share/claude/versions/2.1.286", "-p"], ""),
            (["something"], "/x/.local/share/claude/versions/2.1.286"),
            (["whatever", "--bg-pty-host", "1"], ""),
            (["node", "/usr/lib/node_modules/.bin/claude"], ""),
            (["/usr/bin/node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"], ""),
            (["bun", "/x/claude"], ""),
        ]
        for argv, exe in cases:
            with self.subTest(argv=argv, exe=exe):
                self.assertTrue(is_claude_process(argv, exe))

    def test_other_processes(self):
        cases = [
            (["ssh", "claude"], ""),
            (["python3", "/x/claude"], ""),
            (["claude-work"], ""),
            (["vim", "claude.md"], ""),
            ([], ""),
        ]
        for argv, exe in cases:
            with self.subTest(argv=argv):
                self.assertFalse(is_claude_process(argv, exe))

    def test_daemon_stop_command(self):
        self.assertEqual(platform.daemon_stop_command({"HOME": "/h"}),
                         "env -u CLAUDE_CONFIG_DIR claude daemon stop --any")
        self.assertEqual(platform.daemon_stop_command({"CLAUDE_CONFIG_DIR": "/a b"}),
                         "CLAUDE_CONFIG_DIR='/a b' claude daemon stop --any")
        self.assertEqual(platform.config_dir_of({"HOME": "/h"}), "/h/.claude")


class ProcArgsParsingTest(unittest.TestCase):
    def test_parse(self):
        raw = (struct.pack("=i", 2) + b"/usr/bin/claude\0\0\0\0" + b"claude\0a b\0"
               + b"HOME=/h\0CLAUDE_CONFIG_DIR=/c d\0X=1=2\0\0\0ptr_munge=abc\0")
        info = platform._parse_procargs2(42, raw)
        self.assertEqual(info.pid, 42)
        self.assertEqual(info.exe, "/usr/bin/claude")
        self.assertEqual(info.argv, ["claude", "a b"])
        self.assertEqual(info.env, {"HOME": "/h", "CLAUDE_CONFIG_DIR": "/c d", "X": "1=2"})

    def test_hidden_environment(self):
        # macOS 对 Apple 自带程序只返回 argv，环境部分为空：按读不出处理。
        raw = struct.pack("=i", 1) + b"/bin/zsh\0\0\0" + b"/bin/zsh\0"
        info = platform._parse_procargs2(7, raw)
        self.assertEqual(info.argv, ["/bin/zsh"])
        self.assertIsNone(info.env)

    def test_truncated(self):
        self.assertIsNone(platform._parse_procargs2(1, b"\x01").env)


class TestHookTest(unittest.TestCase):
    def test_hooks_need_test_mode(self):
        saved = {key: os.environ.get(key) for key in ("MULTI_CLAUDE_TEST_MODE", "MULTI_CLAUDE_TEST_X")}
        try:
            os.environ["MULTI_CLAUDE_TEST_X"] = "1"
            os.environ.pop("MULTI_CLAUDE_TEST_MODE", None)
            self.assertIsNone(platform.test_hook_value("MULTI_CLAUDE_TEST_X"))
            os.environ["MULTI_CLAUDE_TEST_MODE"] = "1"
            self.assertEqual(platform.test_hook_value("MULTI_CLAUDE_TEST_X"), "1")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
