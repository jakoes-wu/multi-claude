"""方案 feature-shared-exclude §8：共享项按账号退出与禁止共享清单。"""

import json
import os

from helpers import CliTestCase


class SharedExcludeTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.shared = os.path.join(self.home, ".claude-shared")
        for item in ("skills", "agents", "commands"):
            os.makedirs(os.path.join(self.shared, item))
        self.write(os.path.join(self.shared, "CLAUDE.md"), "# shared\n")
        self.ok("init", "--shared-dir", self.shared)
        self.account = os.path.join(self.root, "w")

    def link(self, item):
        return os.path.join(self.account, item)

    def account_data(self, name="w"):
        return self.config_data()["accounts"][name]


class ExcludeTest(SharedExcludeTestCase):
    def test_t1_exclude_removes_managed_link(self):
        self.ok("add", "w", "--shared")
        self.assertTrue(os.path.islink(self.link("skills")))
        shared_before = self.snapshot(self.shared)
        result = self.ok("add", "w", "--shared-exclude", "skills")
        self.assertIn("sharing disabled for this item", result.out)
        self.assertFalse(os.path.lexists(self.link("skills")))
        for item in ("agents", "commands", "CLAUDE.md"):
            self.assertTrue(os.path.islink(self.link(item)), item)
        data = self.account_data()
        self.assertEqual(data["shared_exclude"], ["skills"])
        self.assertNotIn("skills", data["managed_links"])
        self.assertEqual(self.snapshot(self.shared), shared_before)

    def test_t2_user_items_left_alone(self):
        self.ok("add", "w", "--shared")
        os.unlink(self.link("skills"))
        os.makedirs(os.path.join(self.link("skills"), "mine"))
        os.unlink(self.link("agents"))
        os.symlink(self.tmp, self.link("agents"))
        result = self.ok("add", "w", "--shared-exclude", "skills", "--shared-exclude", "agents")
        self.assertNotIn("conflict", result.out + result.err)
        self.assertTrue(os.path.isdir(os.path.join(self.link("skills"), "mine")))
        self.assertEqual(os.readlink(self.link("agents")), self.tmp)

    def test_t3_include_restores(self):
        self.ok("add", "w", "--shared", "--shared-exclude", "skills")
        self.assertFalse(os.path.lexists(self.link("skills")))
        self.ok("add", "w", "--shared-include", "skills")
        self.assertTrue(os.path.islink(self.link("skills")))
        data = self.account_data()
        self.assertNotIn("shared_exclude", data)
        self.assertIn("skills", data["managed_links"])

    def test_t4_exclusion_while_sharing_is_off(self):
        self.ok("add", "w")
        result = self.ok("add", "w", "--shared-exclude", "skills")
        self.assertIn("sharing is off for w", result.out)
        self.ok("add", "w", "--shared")
        self.assertFalse(os.path.lexists(self.link("skills")))
        self.assertTrue(os.path.islink(self.link("agents")))

    def test_t5_usage_errors(self):
        self.ok("add", "w", "--shared")
        before = self.snapshot(self.home)
        result = self.run_cli("add", "w", "--shared-exclude", "skills", "--shared-include", "skills")
        self.assertEqual(result.code, 2, result)
        self.assertEqual(self.run_cli("add", "w", "--shared-exclude", "a/b").code, 2)
        self.assertEqual(self.run_cli("add", "w", "--shared-exclude", "..").code, 2)
        self.assertEqual(self.snapshot(self.home), before)

    def test_t6_item_not_in_shared_items(self):
        self.ok("add", "w", "--shared")
        result = self.ok("add", "w", "--shared-exclude", "rules")
        self.assertIn("rules is not in shared.items", result.out)
        self.assertEqual(self.account_data()["shared_exclude"], ["rules"])

    def test_t9_list_outputs(self):
        self.ok("add", "w", "--shared", "--shared-exclude", "skills", "--shared-exclude", "agents")
        self.ok("add", "v", "--shared")
        out = self.ok("list").out
        self.assertRegex(out, r"\bw\s+dir\s+\S+\s+inherit\s+yes \(not: skills,agents\)")
        self.assertRegex(out, r"\bv\s+dir\s+\S+\s+inherit\s+yes\s")
        entries = {entry["name"]: entry for entry in json.loads(self.ok("list", "--json").out)["accounts"]}
        self.assertEqual(entries["w"]["shared_exclude"], ["skills", "agents"])
        self.assertEqual(entries["v"]["shared_exclude"], [])

    def test_t10_apply_file(self):
        self.ok("add", "w", "--shared")
        data = self.config_data()
        data["accounts"]["w"]["shared_exclude"] = ["commands", "commands"]
        path = self.write(os.path.join(self.tmp, "apply.json"), json.dumps(data))
        self.ok("apply", "-f", path)
        self.assertFalse(os.path.lexists(self.link("commands")))
        self.assertEqual(self.account_data()["shared_exclude"], ["commands"])
        del data["accounts"]["w"]["shared_exclude"]
        self.write(path, json.dumps(data))
        self.ok("apply", "-f", path)
        self.assertTrue(os.path.islink(self.link("commands")))
        self.assertNotIn("shared_exclude", self.account_data())

    def test_t11_dry_run(self):
        self.ok("add", "w", "--shared")
        before = self.snapshot(self.home)
        result = self.ok("add", "w", "--shared-exclude", "skills", "--dry-run")
        self.assertIn("sharing disabled for this item", result.out)
        self.assertIn("(dry-run)", result.out)
        self.assertEqual(self.snapshot(self.home), before)


class UnshareableTest(SharedExcludeTestCase):
    def test_t7_config_with_unshareable_item(self):
        self.ok("add", "w")
        for item in (".credentials.json", "Projects", "history.jsonl"):
            with self.subTest(item=item):
                data = self.config_data()
                data["shared"]["items"] = ["skills", item]
                self.write(os.path.join(self.state, "config.json"), json.dumps(data))
                result = self.run_cli("list")
                self.assertEqual(result.code, 1, result)
                self.assertIn("must not include {}".format(item), result.err)
                doctor = self.run_cli("doctor")
                self.assertNotEqual(doctor.code, 0, doctor)
                self.assertIn("must not include {}".format(item), doctor.out + doctor.err)
                data["shared"]["items"] = ["skills"]
                self.write(os.path.join(self.state, "config.json"), json.dumps(data))

    def test_t8_init_rejects_unshareable(self):
        before = self.snapshot(self.home)
        result = self.run_cli("init", "--shared-items", "skills,history.jsonl")
        self.assertEqual(result.code, 2, result)
        self.assertIn("history.jsonl", result.err)
        self.assertEqual(self.snapshot(self.home), before)

    def test_exclude_may_name_unshareable(self):
        self.ok("add", "w", "--shared-exclude", "projects")
        self.assertEqual(self.account_data()["shared_exclude"], ["projects"])
