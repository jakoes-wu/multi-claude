"""feature-directory-routing 方案 §8 第 1–7、9 条：路由配置、route / which 命令、claude-auto 行为与生命周期。

claude-auto 落到哪个账号，通过假 claude 收到的 CLAUDE_CONFIG_DIR 判断。
"""

import json
import os
import shutil
import subprocess
import unittest

from helpers import ROOT, CliTestCase


class RouteBase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.tmp, "w o'rk")
        self.client = os.path.join(self.work, "client")
        os.makedirs(self.client)
        self.ok("add", "main")
        self.ok("add", "work")
        self.ok("add", "client")

    def auto(self, cwd, *args, env=None):
        """在 cwd 下运行 claude-auto，返回 (退出码, 假 claude 收到的参数, CLAUDE_CONFIG_DIR 或 None)。"""
        for suffix in (".args", ".env"):
            if os.path.exists(self.fake_out + suffix):
                os.unlink(self.fake_out + suffix)
        proc = subprocess.run([os.path.join(self.bin, "claude-auto")] + list(args), cwd=cwd,
                              env=self._merged(env), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        if not os.path.exists(self.fake_out + ".args"):
            return proc.returncode, None, None
        with open(self.fake_out + ".args") as handle:
            received = handle.read().split("\n")[:-1]
        config_dir = None
        with open(self.fake_out + ".env") as handle:
            for line in handle.read().splitlines():
                if line.startswith("CLAUDE_CONFIG_DIR="):
                    config_dir = line.split("=", 1)[1]
        return proc.returncode, received, config_dir

    def account_of(self, cwd):
        code, _, config_dir = self.auto(cwd)
        self.assertEqual(code, 0)
        return os.path.basename(config_dir) if config_dir else None

    def which(self, cwd):
        return self.ok("which", cwd).out.split(" ")[0].strip()


class ConfigTest(RouteBase):
    def apply_routes(self, routes):
        data = self.config_data()
        data["routes"] = routes
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(data))
        return self.run_cli("apply", "-f", path)

    def test_shape_errors_return_1(self):
        bad = [[], {"rules": {}}, {"rules": [{"path": "/x"}]}, {"rules": [{"path": "rel", "account": "work"}]},
               {"rules": [{"path": "/", "account": "work"}]}, {"default": "bad name"},
               {"rules": [{"path": "/x", "account": "work"}, {"path": "/x/", "account": "main"}]},
               {"rules": [{"path": "/x", "account": "work", "extra": 1}]},
               {"rules": [{"path": "/x\ny", "account": "work"}]}]
        before = self.snapshot()
        for routes in bad:
            with self.subTest(routes=routes):
                self.assertEqual(self.apply_routes(routes).code, 1)
        self.assertEqual(before, self.snapshot())

    def test_old_config_loads_and_writes_empty_routes(self):
        self.assertEqual(self.config_data()["routes"],
                         {"default": None, "rules": []})


class ExampleConfigTest(CliTestCase):
    def test_example_config_applies_on_a_new_machine(self):
        """示例配置含 default 身份账号与引用它的默认路由：新机器上应跳过二者而不是整条失败。"""
        result = self.ok("apply", "-f", os.path.join(ROOT, "examples", "config.example.json"))
        self.assertIn("skip account main", result.out)
        self.assertIn("skip default route", result.out)
        self.assertTrue(os.path.exists(os.path.join(self.bin, "claude-auto")))


class RouteCommandTest(RouteBase):
    def config_routes(self):
        return self.config_data()["routes"]

    def test_idempotent_commands(self):
        for command in (("route", self.work, "work"), ("route", self.client, "client"),
                        ("route", "--default", "main"), ("route", self.work, "main"),
                        ("route", self.client, "--remove"), ("route", "--no-default")):
            with self.subTest(command=command):
                self.ok(*command)
                before = self.snapshot()
                second = self.ok(*command)
                for word in ("create", "update", "delete"):
                    self.assertNotIn("] " + word, second.out, second)
                self.assertEqual(before, self.snapshot())

    def test_path_spelling(self):
        self.ok("route", "~/proj/", "work")
        self.ok("route", "rel/dir", "main", cwd=self.tmp)
        rules = self.config_routes()["rules"]
        self.assertEqual(rules[0]["path"], "~/proj")
        self.assertEqual(rules[1]["path"], os.path.join(self.tmp, "rel", "dir"))
        self.assertIn("does not exist", self.ok("route", "~/missing", "work").err)

    def test_retarget_keeps_position(self):
        self.ok("route", self.work, "work")
        self.ok("route", self.client, "client")
        self.ok("route", self.work + "/", "main")
        self.assertEqual([rule["account"] for rule in self.config_routes()["rules"]], ["main", "client"])

    def test_errors(self):
        before = self.snapshot()
        self.assertEqual(self.run_cli("route", self.work, "nobody").code, 1)
        self.assertEqual(self.run_cli("route", "--default", "nobody").code, 1)
        for command in (("route",), ("route", self.work), ("route", self.work, "work", "--remove"),
                        ("route", self.work, "--default", "main"), ("route", "--remove"), ("route", "/", "work")):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(*command).code, 2)
        self.assertEqual(before, self.snapshot())
        result = self.ok("route", self.work, "--remove")
        self.assertIn("has no route", result.out)

    def test_dry_run(self):
        before = self.snapshot()
        self.assertIn("(dry-run)", self.ok("route", self.work, "work", "--dry-run").out)
        self.assertEqual(before, self.snapshot())


class ConflictTest(RouteBase):
    def assert_conflict(self, *command):
        before = self.snapshot()
        result = self.run_cli(*command)
        self.assertEqual(result.code, 3, result)
        self.assertEqual(before, self.snapshot())
        return result

    def test_removing_referenced_account(self):
        self.ok("route", self.work, "work")
        self.ok("route", "--default", "main")
        self.assertIn("--remove` first", self.assert_conflict("remove", "work").err)
        self.assertIn("--no-default", self.assert_conflict("remove", "main").err)
        data = self.config_data()
        del data["accounts"]["work"]
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(data))
        self.assert_conflict("apply", "-f", path)

    def test_apply_file_skips_routes_of_skipped_default_account(self):
        data = self.config_data()
        data["accounts"]["home"] = {"identity": "default"}
        data["routes"] = {"default": "home", "rules": [{"path": self.work, "account": "work"},
                                                       {"path": self.client, "account": "home"}]}
        path = self.write(os.path.join(self.tmp, "c.json"), json.dumps(data))
        result = self.ok("apply", "-f", path)
        self.assertIn("skip account home", result.out)
        self.assertIn("skip route {} (account home is skipped".format(self.client), result.out)
        self.assertIn("skip default route", result.out)
        self.assertEqual(self.config_data()["routes"],
                         {"default": None, "rules": [{"path": self.work, "account": "work"}]})

    def test_account_named_auto(self):
        self.ok("add", "auto")
        self.assertIn("named 'auto'", self.assert_conflict("route", self.work, "work").err)
        self.ok("remove", "auto")
        self.ok("route", self.work, "work")
        self.assert_conflict("add", "Auto")

    def test_router_path_taken(self):
        self.write(os.path.join(self.bin, "claude-auto"), "#!/bin/sh\necho mine\n", 0o755)
        self.assertIn("not the multi-claude router", self.assert_conflict("route", self.work, "work").err)


class MatchTest(RouteBase):
    def setUp(self):
        super().setUp()
        self.ok("route", self.work, "work")
        self.ok("route", self.client, "client")

    def test_prefix_and_longest(self):
        deep = os.path.join(self.client, "a", "b")
        os.makedirs(deep)
        sibling = self.work + "shop"
        os.makedirs(sibling)
        cases = {self.work: "work", self.client: "client", deep: "client", sibling: None, self.tmp: None}
        for cwd, expected in cases.items():
            with self.subTest(cwd=cwd):
                self.assertEqual(self.account_of(cwd), expected)
                self.assertEqual(self.which(cwd), expected or "claude")

    def test_symlinks(self):
        link = os.path.join(self.tmp, "link-to-client")
        os.symlink(self.client, link)
        # 经软链进入的目录按物理路径匹配。
        self.assertEqual(self.account_of(link), "client")
        self.assertEqual(self.which(link), "client")
        # 规则目录本身是软链：同一物理目录的两种写法，配置中靠前的生效。
        self.ok("route", link, "main")
        self.assertEqual(self.account_of(self.client), "client")
        self.assertEqual(self.which(self.client), "client")

    def test_missing_route_directory_is_skipped(self):
        # 不存在的规则目录即使是当前目录的“更长前缀”写法，也不参与匹配。
        gone = os.path.join(self.work, "gone")
        self.ok("route", gone, "main")
        self.assertEqual(self.account_of(self.work), "work")

    def test_fallbacks(self):
        self.assertEqual(self.account_of(self.tmp), None)
        code, args, config_dir = self.auto(self.tmp, env={"CLAUDE_CONFIG_DIR": "/parent"})
        self.assertEqual((code, config_dir), (0, "/parent"))
        self.ok("route", "--default", "main")
        self.assertEqual(self.account_of(self.tmp), "main")
        self.assertEqual(self.ok("which", self.tmp).out.strip(), "main (default)")
        self.ok("route", "--no-default")
        code, args, _ = self.auto(self.tmp, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((code, args), (127, None))

    def test_arguments(self):
        self.ok("args", "client", "--", "--ide")
        code, args, _ = self.auto(self.client, "a b", "-p")
        self.assertEqual((code, args), (0, ["--ide", "a b", "-p"]))

    def test_which_errors(self):
        self.assertEqual(self.run_cli("which", os.path.join(self.tmp, "nope")).code, 1)
        self.assertIn("(route ", self.ok("which", self.work).out)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        self.ok("route", "--default", "main")
        for state in ("rules+default", "default-only"):
            if state == "default-only":
                self.ok("route", self.work, "--remove")
                self.ok("route", self.client, "--remove")
            proc = subprocess.run(["shellcheck", os.path.join(self.bin, "claude-auto")], stdout=subprocess.PIPE,
                                  universal_newlines=True)
            self.assertEqual(proc.returncode, 0, proc.stdout)


class LifecycleTest(RouteBase):
    def router(self):
        return os.path.join(self.bin, "claude-auto")

    def test_create_update_delete(self):
        self.assertFalse(os.path.exists(self.router()))
        self.assertIn("create router", self.ok("route", self.work, "work").out)
        self.assertTrue(os.access(self.router(), os.X_OK))
        self.assertNotIn("router", self.ok("apply").out.replace("unchanged router", ""))
        self.assertIn("delete router", self.ok("route", self.work, "--remove").out)
        self.assertFalse(os.path.exists(self.router()))

    def test_bin_dir_change(self):
        self.ok("route", self.work, "work")
        other = os.path.join(self.tmp, "bin2")
        self.ok("init", "--bin-dir", other)
        self.assertFalse(os.path.exists(self.router()))
        self.assertTrue(os.path.exists(os.path.join(other, "claude-auto")))

    def test_stale_and_repair(self):
        self.ok("route", self.work, "work")
        with open(self.router(), "a") as handle:
            handle.write("# edited\n")
        doctor = json.loads(self.run_cli("doctor", "--json").out)
        self.assertEqual([c["level"] for c in doctor["checks"] if c["id"] == "router"], ["error"])
        self.assertIn("update router", self.ok("apply").out)

    def test_not_treated_as_orphan(self):
        self.ok("route", self.work, "work")
        self.ok("apply")
        self.ok("remove", "client")
        self.assertTrue(os.path.exists(self.router()))


class ListTest(RouteBase):
    def test_table_and_json(self):
        self.assertNotIn("routes:", self.ok("list").out)
        self.ok("route", self.work, "work")
        self.ok("route", "~/missing", "main")
        self.ok("route", "--default", "main")
        out = self.ok("list").out
        self.assertIn("routes:\n  {} -> work\n  ~/missing -> main\n  (default) -> main".format(self.work), out)
        routes = json.loads(self.ok("list", "--json").out)["routes"]
        self.assertEqual(routes["default"], "main")
        self.assertEqual([(r["account"], r["exists"]) for r in routes["rules"]], [("work", True), ("main", False)])

    def test_doctor(self):
        self.ok("route", "~/missing", "main")
        checks = json.loads(self.run_cli("doctor", "--json").out)["checks"]
        self.assertEqual([c["level"] for c in checks if c["id"] == "route-path"], ["warn"])
        # 手工改坏：规则引用未登记账号。
        data = self.config_data()
        data["routes"]["rules"][0]["account"] = "ghost"
        self.write(os.path.join(self.state, "config.json"), json.dumps(data))
        result = self.run_cli("doctor", "--json")
        self.assertEqual(result.code, 1)
        checks = json.loads(result.out)["checks"]
        self.assertEqual([c["level"] for c in checks if c["id"] == "route-account"], ["error"])
        self.assertEqual(self.run_cli("which", self.tmp).code, 0)
        os.makedirs(os.path.join(self.home, "missing"))
        result = self.run_cli("which", "~/missing")
        self.assertEqual(result.code, 1)
        self.assertIn("ghost (not registered", result.out)


if __name__ == "__main__":
    unittest.main()
