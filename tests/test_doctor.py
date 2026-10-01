"""v0.2 方案 §8 第 3、7 条：doctor 各检查项、JSON 输出与退出码。"""

import json
import os
import shutil
import unittest

from helpers import CliTestCase


class DoctorTest(CliTestCase):
    def setUp(self):
        super().setUp()
        # 启动命令目录放进 PATH，正常情况下各项都应通过。
        self.env["PATH"] = os.pathsep.join([self.fakebin, self.bin, "/usr/bin", "/bin", "/usr/sbin", "/sbin"])

    def doctor(self, *extra, env=None):
        result = self.run_cli("doctor", "--json", *extra, env=env)
        return result, json.loads(result.out)

    def find(self, data, check_id, subject=None):
        return [check for check in data["checks"]
                if check["id"] == check_id and (subject is None or check["subject"] == subject)]

    def healthy(self):
        self.ok("add", "work")
        os.chmod(os.path.join(self.root, "work"), 0o700)
        self.write(os.path.join(self.root, "work", ".credentials.json"), "{}", 0o600)

    def test_all_ok(self):
        self.healthy()
        result, data = self.doctor()
        self.assertEqual(result.code, 0, result)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["summary"]["error"], 0)
        self.assertEqual(data["summary"]["warn"], 0, data)
        text = self.ok("doctor")
        self.assertNotIn("[ok]", text.out)
        self.assertIn("[ok] launcher work", self.ok("doctor", "--verbose").out)

    def test_config_states(self):
        result, data = self.doctor()
        self.assertEqual(result.code, 0)
        self.assertEqual(self.find(data, "config")[0]["level"], "warn")
        self.write(os.path.join(self.state, "config.json"), "{broken")
        result, data = self.doctor()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.find(data, "config")[0]["level"], "error")
        # 配置读不出时，不依赖配置的检查照常执行。
        self.assertTrue(self.find(data, "claude-on-path"))

    def test_journal(self):
        self.healthy()
        self.write(os.path.join(self.state, "migrate-journal.json"), json.dumps(
            {"name": "main", "source": "/x", "target": "/y", "backup": "/z", "mode": "rename", "phase": "planned"}))
        result, data = self.doctor()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.find(data, "journal")[0]["level"], "error")
        self.write(os.path.join(self.state, "migrate-journal.json"), "{broken")
        result, data = self.doctor()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.find(data, "journal")[0]["level"], "error")

    def test_path_checks(self):
        self.healthy()
        result, data = self.doctor(env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(result.code, 1)
        self.assertEqual(self.find(data, "claude-on-path")[0]["level"], "error")
        self.assertEqual(self.find(data, "bin-on-path")[0]["level"], "warn")
        shadow = os.path.join(self.tmp, "shadow")
        self.write(os.path.join(shadow, "claude-work"), "#!/bin/sh\n", 0o755)
        _, data = self.doctor(env={"PATH": os.pathsep.join([self.fakebin, shadow, self.bin, "/usr/bin", "/bin"])})
        self.assertEqual(self.find(data, "launcher-shadowed", "work")[0]["level"], "warn")

    def test_launcher_states(self):
        self.healthy()
        path = os.path.join(self.bin, "claude-work")
        with open(path) as handle:
            content = handle.read()
        self.write(path, content.replace("unset ANTHROPIC_API_KEY", "unset NOTHING"), 0o755)
        result, data = self.doctor()
        self.assertEqual(result.code, 1)
        self.assertIn("out of date", self.find(data, "launcher", "work")[0]["message"])
        os.unlink(path)
        _, data = self.doctor()
        self.assertIn("missing", self.find(data, "launcher", "work")[0]["message"])
        self.write(path, "#!/bin/sh\necho mine\n", 0o755)
        _, data = self.doctor()
        self.assertIn("does not manage", self.find(data, "launcher", "work")[0]["message"])

    def test_account_dir_and_links(self):
        shared = os.path.join(self.tmp, "shared")
        os.makedirs(os.path.join(shared, "skills"))
        self.ok("init", "--shared-dir", shared, "--shared-items", "skills,commands")
        self.healthy()
        os.chmod(os.path.join(self.root, "work"), 0o755)
        os.symlink(os.path.join(shared, "commands"), os.path.join(self.root, "work", "commands"))
        result, data = self.doctor()
        self.assertEqual(result.code, 0)
        self.assertEqual(self.find(data, "account-dir", "work")[0]["level"], "warn")
        self.assertIn("chmod 700", self.find(data, "account-dir", "work")[0]["message"])
        self.assertEqual(len(self.find(data, "shared-link", "work")), 1)
        shutil.rmtree(os.path.join(self.root, "work"))
        result, data = self.doctor()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.find(data, "account-dir", "work")[0]["level"], "error")

    def test_default_link(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main")
        _, data = self.doctor()
        self.assertEqual(self.find(data, "default-link", "main")[0]["level"], "ok")
        os.unlink(os.path.join(self.home, ".claude"))
        result, data = self.doctor()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.find(data, "default-link", "main")[0]["level"], "error")

    def test_login_and_auth_env(self):
        self.ok("add", "work")
        os.chmod(os.path.join(self.root, "work"), 0o700)
        result, data = self.doctor(env={"ANTHROPIC_API_KEY": "sk-secret-value", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "x"})
        self.assertEqual(result.code, 0)
        self.assertEqual(self.find(data, "login", "work")[0]["level"], "warn")
        subjects = {check["subject"] for check in self.find(data, "auth-env")}
        self.assertEqual(subjects, {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN"})
        self.assertNotIn("sk-secret-value", result.out + result.err)


if __name__ == "__main__":
    unittest.main()
