"""feature-rename-mcp 方案 §8 第 1、2、5 条：账号目录字段与 rename。"""

import json
import os
import re

from helpers import CliTestCase


class DirFieldTest(CliTestCase):
    def apply_accounts(self, accounts):
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps({"version": 1, "accounts": accounts}))
        return self.run_cli("apply", "-f", path)

    def test_validation(self):
        for accounts in ({"a": {"dir": "x/y"}}, {"a": {"dir": 3}}, {"a": {"dir": "x"}, "b": {"dir": "X"}},
                         {"a": {"dir": "b"}, "b": {}}):
            with self.subTest(accounts=accounts):
                self.assertEqual(self.apply_accounts(accounts).code, 1)

    def test_unchanged_config_text_without_rename(self):
        self.ok("add", "work", "--proxy", "7901")
        with open(os.path.join(self.state, "config.json")) as handle:
            text = handle.read()
        self.assertNotIn("dir", self.config_data()["accounts"]["work"])
        self.ok("apply")
        with open(os.path.join(self.state, "config.json")) as handle:
            self.assertEqual(text, handle.read())


class RenameTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(self.shared, "skills"))
        self.ok("init", "--shared-dir", self.shared, "--shared-items", "skills")
        self.ok("add", "work", "--shared", "--proxy", "7901")
        self.ok("add", "main")
        self.work_dir = os.path.join(self.root, "work")
        self.write(os.path.join(self.work_dir, "history.jsonl"), "keep")

    def test_rename_keeps_directory_login_and_links(self):
        before_dir = self.snapshot(self.work_dir)
        before_service = re.search(r"work: keychain service '([^']+)'", self.ok("list", "--verbose").out).group(1)
        result = self.ok("rename", "work", "job")
        self.assertIn("renamed work to job (directory {} unchanged)".format(self.work_dir), result.out)
        self.assertFalse(os.path.exists(os.path.join(self.bin, "claude-work")))
        self.assertTrue(os.path.exists(os.path.join(self.bin, "claude-job")))
        self.assertEqual(before_dir, self.snapshot(self.work_dir))
        self.assertTrue(os.path.islink(os.path.join(self.work_dir, "skills")))
        data = self.config_data()["accounts"]
        self.assertEqual(data["job"]["dir"], "work")
        self.assertEqual(data["job"]["managed_links"], ["skills"])
        self.assertNotIn("work", data)
        code, _, env, _ = self.run_launcher("job")
        self.assertEqual(code, 0)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.work_dir)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7901")
        # 改名前后的钥匙串服务名完全相同：登录绑定的路径没有变（由路径计算，各平台都显示）。
        after_service = re.search(r"job: keychain service '([^']+)'", self.ok("list", "--verbose").out).group(1)
        self.assertEqual(before_service, after_service)

    def test_routes_follow(self):
        self.ok("route", self.work_dir, "work")
        self.ok("route", "--default", "work")
        self.ok("rename", "work", "job")
        routes = self.config_data()["routes"]
        self.assertEqual(routes, {"default": "job", "rules": [{"path": self.work_dir, "account": "job"}]})
        with open(os.path.join(self.bin, "claude-auto")) as handle:
            self.assertIn("claude-job", handle.read())

    def test_rename_back_drops_dir_field(self):
        self.ok("rename", "work", "job")
        self.ok("rename", "job", "work")
        self.assertNotIn("dir", self.config_data()["accounts"]["work"])
        self.assertTrue(os.path.islink(os.path.join(self.work_dir, "skills")))

    def test_errors(self):
        before = self.snapshot()
        self.assertEqual(self.run_cli("rename", "work", "Work").code, 2)
        self.assertEqual(self.run_cli("rename", "work", "-x").code, 2)
        self.assertEqual(self.run_cli("rename", "work", "main").code, 3)
        self.assertEqual(self.run_cli("rename", "nobody", "x").code, 1)
        self.assertIn("(dry-run)", self.ok("rename", "work", "job", "--dry-run").out)
        self.assertEqual(before, self.snapshot())
        self.ok("rename", "work", "job")
        self.assertEqual(self.run_cli("rename", "work", "x").code, 1)

    def test_names_and_directories_cannot_collide(self):
        self.ok("rename", "work", "job")
        before = self.snapshot()
        self.assertIn("would use the directory of account 'job'", self.run_cli("add", "work").err)
        self.assertEqual(self.run_cli("rename", "main", "work").code, 3)
        self.assertEqual(before, self.snapshot())
        # 配置仍能正常加载
        self.ok("list")

    def test_apply_file_keeps_dir(self):
        self.ok("rename", "work", "job")
        data = self.config_data()
        data["accounts"]["job"]["dir"] = "elsewhere"
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(data))
        self.ok("apply", "-f", path)
        self.assertEqual(self.config_data()["accounts"]["job"]["dir"], "work")

    def test_default_identity(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "home")
        self.ok("rename", "home", "personal")
        code, _, env, _ = self.run_launcher("personal", env={"CLAUDE_CONFIG_DIR": "/elsewhere"})
        self.assertEqual(code, 0)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(os.path.realpath(os.path.join(self.home, ".claude")),
                         os.path.realpath(os.path.join(self.root, "home")))
        self.assertRegex(self.ok("list").out, r"personal\s+default\s+ok")
