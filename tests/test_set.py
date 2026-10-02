"""方案 feature-set-command §8：默认共享目录、--shared DIR、空共享目录提示、set 命令。"""

import os

from helpers import CliTestCase


class SharedDirTest(CliTestCase):
    def shared(self, *parts):
        return os.path.join(self.home, ".claude-shared", *parts)

    def test_t1_default_shared_dir(self):
        os.makedirs(self.shared("skills"))
        result = self.ok("add", "work", "--shared")
        self.assertEqual(self.config_data()["shared"]["dir"], "~/.claude-shared")
        self.assertEqual(os.readlink(os.path.join(self.root, "work", "skills")), self.shared("skills"))
        self.assertNotIn("has none of", result.out)

    def test_t2_existing_shared_dir_is_kept(self):
        other = os.path.join(self.tmp, "mine")
        os.makedirs(os.path.join(other, "skills"))
        self.ok("init", "--shared-dir", other)
        self.ok("add", "work", "--shared")
        self.assertEqual(self.config_data()["shared"]["dir"], other)

    def test_t3_shared_dir_argument_moves_every_account(self):
        os.makedirs(self.shared("skills"))
        self.ok("add", "work", "--shared")
        self.ok("add", "home", "--shared")
        new_dir = os.path.join(self.tmp, "s2")
        os.makedirs(os.path.join(new_dir, "skills"))
        result = self.ok("set", "home", "--shared", new_dir)
        self.assertIn("shared directory is now {} (was ~/.claude-shared)".format(new_dir), result.out)
        self.assertEqual(self.config_data()["shared"]["dir"], new_dir)
        for name in ("work", "home"):
            self.assertEqual(os.readlink(os.path.join(self.root, name, "skills")), os.path.join(new_dir, "skills"))

    def test_t4_usage_errors(self):
        self.ok("add", "work")
        before = self.snapshot(self.home)
        result = self.run_cli("set", "work", "--shared", "work-dir")
        self.assertEqual(result.code, 2, result)
        self.assertIn("expects a directory path", result.err)
        self.assertEqual(self.run_cli("set", "work", "--shared", "--no-shared").code, 2)
        self.assertEqual(self.snapshot(self.home), before)

    def test_t5_empty_shared_dir_hint(self):
        result = self.ok("add", "work", "--shared")
        self.assertIn("~/.claude-shared has none of agents, commands, skills, CLAUDE.md", result.out)
        os.makedirs(self.shared("agents"))
        self.assertNotIn("has none of", self.ok("set", "work", "--shared").out)
        self.assertNotIn("has none of", self.ok("add", "solo").out)

    def test_t10_missing_items_are_quiet_by_default(self):
        os.makedirs(self.shared("skills"))
        result = self.ok("add", "work", "--shared")
        self.assertNotIn("not present in shared dir", result.out)
        self.assertIn("not present in shared dir", self.ok("set", "work", "--shared", "--verbose").out)


class SetTest(CliTestCase):
    def test_t6_same_result_as_add(self):
        os.makedirs(os.path.join(self.home, ".claude-shared", "skills"))
        self.ok("add", "a")
        self.ok("add", "b")
        for options in (["--proxy", "7901"], ["--shared"], ["--shared-exclude", "skills"], ["--no-shared"]):
            with self.subTest(options=options):
                self.ok("add", "a", *options)
                self.ok("set", "b", *options)
                data = self.config_data()["accounts"]
                for key in ("proxy", "shared", "managed_links"):
                    self.assertEqual(data["a"].get(key), data["b"].get(key), key)
                self.assertEqual(data["a"].get("shared_exclude"), data["b"].get("shared_exclude"))

    def test_t7_refusals(self):
        self.ok("add", "work")
        before = self.snapshot(self.home)
        result = self.run_cli("set", "nobody", "--proxy", "7901")
        self.assertEqual(result.code, 1)
        self.assertIn("use multi-claude add nobody to create it", result.err)
        result = self.run_cli("set", "work")
        self.assertEqual(result.code, 2)
        self.assertIn("nothing to set", result.err)
        self.assertEqual(self.run_cli("set", "work", "--adopt").code, 2)
        self.assertEqual(self.snapshot(self.home), before)

    def test_t8_help_and_completion(self):
        self.assertRegex(self.ok("--help").out, r"Account settings:\n  set NAME\s+change an account")
        self.assertIn("change an existing account", self.ok("set", "--help").out)
