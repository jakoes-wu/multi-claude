"""方案 feature-clearer-help §8：帮助分组、list 简表、PATH 提示、README 结构。"""

import argparse
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

from helpers import ROOT, SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import cli, shellpath  # noqa: E402


class HelpTest(CliTestCase):
    def test_t1_grouped_help_covers_every_command(self):
        out = self.ok("--help").out
        for title in ("Getting started:", "Account settings:", "Everyday:", "Setup and checks:", "Examples:"):
            self.assertIn(title, out)
        parser = cli.build_parser()
        names = {name for action in parser._actions if isinstance(action, argparse._SubParsersAction)
                 for name in action.choices}
        self.assertEqual(sorted(cli.grouped_command_names()), sorted(names))
        # 子命令不再平铺：原来每行一个子命令加说明的列表里会有 "    init  " 这样的行。
        self.assertNotRegex(out, r"\n    init\s{2,}create or update")

    def test_t2_subcommand_descriptions(self):
        self.assertIn("add an account, adopt an existing directory", self.ok("add", "--help").out)
        self.assertIn("sign in to an account", self.ok("login", "--help").out)


class BriefListTest(CliTestCase):
    def write_cache(self, name, percent):
        now = datetime.now(timezone.utc)
        doc = {"cachedUsageUtilization": {"fetchedAtMs": int(now.timestamp() * 1000), "utilization": {
            "five_hour": {"utilization": percent, "resets_at": (now + timedelta(hours=1)).isoformat()}}}}
        self.write(os.path.join(self.root, name, ".claude.json"), json.dumps(doc))

    def test_t3_brief_table(self):
        self.ok("add", "work", "--proxy", "7901")
        self.write_cache("work", 23)
        out = self.ok("list").out
        self.assertRegex(out.splitlines()[0], r"^NAME\s+LOGIN\s+PROXY\s+SHARED\s+5H\s+7D\s+STATUS$")
        self.assertNotIn("root:", out)
        self.assertRegex(out, r"work\s+none\s+http://127\.0\.0\.1:7901\s+no\s+23% \d\d:\d\d\s+-\s+not logged in")
        self.assertIn("run multi-claude doctor for details", out)

    def test_t4_status_problems(self):
        self.ok("add", "gone")
        self.ok("add", "old")
        os.rename(os.path.join(self.root, "gone"), os.path.join(self.tmp, "moved-away"))
        launcher = os.path.join(self.bin, "claude-old")
        with open(launcher, "a") as handle:
            handle.write("# edited\n")
        out = self.ok("list").out
        self.assertRegex(out, r"gone\s.*directory missing, not logged in")
        self.assertRegex(out, r"old\s.*launcher stale, not logged in")

    def test_t4_default_link_problem(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main")
        os.unlink(os.path.join(self.home, ".claude"))
        self.assertRegex(self.ok("list").out, r"main\s.*link missing")

    def test_brief_list_keeps_the_oauth_warning(self):
        self.ok("add", "work")
        err = self.ok("list", env={"CLAUDE_CODE_CUSTOM_OAUTH_URL": "https://example.invalid"}).err
        self.assertIn("CLAUDE_CODE_CUSTOM_OAUTH_URL is set", err)

    def test_t5_verbose_keeps_the_full_listing(self):
        self.ok("add", "work")
        out = self.ok("list", "--verbose").out
        self.assertIn("root: ", out)
        self.assertRegex(out, r"NAME\s+IDENTITY\s+DIR\s+PROXY\s+SHARED\s+LAUNCHER\s+LOGIN\s+LINK")

    def test_brief_list_shows_routes_and_empty_config(self):
        self.ok("init")
        self.assertIn("no accounts registered", self.ok("list").out)
        self.ok("add", "work")
        work_dir = os.path.join(self.tmp, "proj")
        os.makedirs(work_dir)
        self.ok("route", work_dir, "work")
        self.assertIn("routes:\n  {} -> work".format(work_dir), self.ok("list").out)


class PathHintTest(CliTestCase):
    def test_t6_commands_per_shell(self):
        cases = [
            ("/bin/zsh", "Darwin", "echo 'export PATH=\"/x/bin:$PATH\"' >> ~/.zshrc"),
            ("/bin/bash", "Darwin", "echo 'export PATH=\"/x/bin:$PATH\"' >> ~/.bash_profile"),
            ("/usr/bin/bash", "Linux", "echo 'export PATH=\"/x/bin:$PATH\"' >> ~/.bashrc"),
            ("/opt/homebrew/bin/fish", "Darwin", "fish_add_path '/x/bin'"),
            ("/bin/tcsh", "Linux", None),
            (None, "Linux", None),
        ]
        for shell, system, expected in cases:
            with self.subTest(shell=shell, system=system):
                self.assertEqual(shellpath.add_to_path_command("/x/bin", shell, system), expected)
        self.assertIsNone(shellpath.add_to_path_command("/it's/bin", "/bin/zsh", "Darwin"))
        self.assertEqual(shellpath.path_hint("/x/bin", None, "Linux"), "add it to PATH in your shell profile")

    def test_t7_add_hints_when_bin_dir_is_not_on_path(self):
        result = self.ok("add", "work", env={"SHELL": "/bin/zsh"})
        self.assertIn("is not on PATH, so claude-work will not be found", result.out)
        self.assertIn(">> ~/.zshrc", result.out)
        on_path = os.pathsep.join([self.bin, self.env["PATH"]])
        result = self.ok("add", "other", env={"SHELL": "/bin/zsh", "PATH": on_path})
        self.assertNotIn("is not on PATH", result.out)

    def test_t9_doctor_uses_the_same_hint(self):
        self.ok("add", "work")
        result = self.run_cli("doctor", env={"SHELL": "/bin/zsh"})
        self.assertIn(">> ~/.zshrc", result.out + result.err)


class ReadmeTest(unittest.TestCase):
    def headings(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            return [re.match(r"^(#+) ", line).group(1) for line in handle if re.match(r"^#{2,4} ", line)]

    def test_t10_both_readmes_have_the_same_structure(self):
        self.assertEqual(self.headings("README.md"), self.headings("README.zh-CN.md"))
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            english = handle.read()
        with open(os.path.join(ROOT, "README.zh-CN.md"), encoding="utf-8") as handle:
            chinese = handle.read()
        self.assertIn("\n## Migrating `~/.claude`\n", english)
        self.assertIn("\n## 迁移 `~/.claude`\n", chinese)
        self.assertIn("\n### Commands\n", english)
        self.assertIn("\n### 命令\n", chinese)


if __name__ == "__main__":
    unittest.main()
