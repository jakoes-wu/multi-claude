"""方案 feature-usability-fixes §8：状态栏新会话提示、账号名写错位置的报错、文档。"""

import json
import os
import sys

from helpers import ROOT, CliTestCase

NOTE = "keep the old status line"


class StatuslineNoteTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.write(os.path.join(self.fakebin, "multi-claude"),
                   '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        self.settings = self.write(os.path.join(self.tmp, "s.json"),
                                   json.dumps({"statusLine": {"type": "command", "command": "echo hi"}}))

    def test_t1_note_only_when_wrapping(self):
        self.assertNotIn(NOTE, self.ok("statusline", "install", self.settings, "--dry-run").out)
        self.assertIn(NOTE, self.ok("statusline", "install", self.settings).out)
        self.assertNotIn(NOTE, self.ok("statusline", "install", self.settings).out)
        other = self.write(os.path.join(self.tmp, "other", "multi-claude"),
                           '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        rewrap = self.ok("statusline", "install", self.settings,
                         env={"PATH": os.pathsep.join([os.path.dirname(other), self.env["PATH"]])})
        self.assertIn("rewrapped", rewrap.out)
        self.assertIn(NOTE, rewrap.out)
        self.assertNotIn(NOTE, self.ok("statusline", "uninstall", self.settings).out)


class MisplacedNameTest(CliTestCase):
    def test_t2_name_after_shared(self):
        for args, command in ((["add", "--shared", "work"], "add"), (["set", "--shared", "work"], "set"),
                              (["add", "--proxy", "7901", "--shared", "work"], "add")):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.code, 2, result)
                self.assertIn("put the account name before the options, e.g. multi-claude {} work --shared"
                              .format(command), result.err)

    def test_help_version_and_conflicts_are_left_to_argparse(self):
        self.assertEqual(self.run_cli("--version", "add", "--shared", "work").code, 0)
        self.assertEqual(self.run_cli("add", "--help", "--shared", "work").code, 0)
        result = self.run_cli("add", "--no-shared", "--shared", "work")
        self.assertEqual(result.code, 2)
        self.assertNotIn("put the account name", result.err)

    def test_t3_valid_forms_unchanged(self):
        shared = os.path.join(self.tmp, "s")
        os.makedirs(shared)
        self.ok("add", "a", "--shared")
        self.ok("add", "--shared", shared, "b")
        self.ok("add", "c", "--shared", shared)
        result = self.run_cli("add", "d", "--shared", "work-dir")
        self.assertEqual(result.code, 2)
        self.assertIn("expects a directory path", result.err)


class DocsTest(CliTestCase):
    def read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_t5_docs(self):
        roadmap = self.read("docs", "analysis", "roadmap.md")
        self.assertIn("### 2.4 降低上手门槛", roadmap)
        self.assertIn("不是版本号", roadmap)
        self.assertIn("keep the old status line", self.read("README.md"))
        self.assertIn("仍用旧的状态栏", self.read("README.zh-CN.md"))
