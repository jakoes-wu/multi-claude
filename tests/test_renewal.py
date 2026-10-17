"""方案 feature-renewal-date §8：手填续费日的写入、校验、list 显示与帮助示例。"""

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta

from helpers import CliTestCase, SRC

sys.path.insert(0, SRC)

from multi_claude import cli  # noqa: E402
from multi_claude.config import dump_config, parse_config  # noqa: E402


def _today():
    return datetime.now().astimezone().date()


class RenewsSettingTest(CliTestCase):
    def account_data(self, name="work"):
        return self.config_data()["accounts"][name]

    def test_t1_set_records_date(self):
        self.ok("add", "work")
        self.ok("set", "work", "--renews", "2026-11-10")
        self.assertEqual(self.account_data()["renews"], "2026-11-10")

    def test_t2_off_removes_key(self):
        self.ok("add", "work", "--renews", "2026-11-10")
        self.assertEqual(self.account_data()["renews"], "2026-11-10")
        self.ok("set", "work", "--renews", "off")
        self.assertNotIn("renews", self.account_data())

    def test_t3_bad_dates_rejected(self):
        self.ok("add", "work", "--renews", "2026-11-10")
        before = self.config_data()
        for value in ("2026-02-30", "11-10", "tomorrow", "２０２６-11-10"):
            with self.subTest(value=value):
                result = self.run_cli("set", "work", "--renews", value)
                self.assertEqual(result.code, 2, result)
                self.assertIn("YYYY-MM-DD", result.err)
                self.assertEqual(self.config_data(), before)

    def test_t4_apply_file_validates(self):
        self.ok("add", "work")
        data = self.config_data()
        path = os.path.join(self.tmp, "apply.json")
        for bad in ("x", 5):
            with self.subTest(value=bad):
                data["accounts"]["work"]["renews"] = bad
                self.write(path, json.dumps(data))
                result = self.run_cli("apply", "-f", path)
                self.assertEqual(result.code, 1, result)
                self.assertIn("'renews'", result.err)
        data["accounts"]["work"]["renews"] = "2026-11-10"
        self.write(path, json.dumps(data))
        self.ok("apply", "-f", path)
        self.assertEqual(self.account_data()["renews"], "2026-11-10")

    def test_t5_kept_by_other_changes(self):
        self.ok("add", "work", "--renews", "2026-11-10")
        self.ok("set", "work", "--proxy", "7901")
        self.assertEqual(self.account_data()["renews"], "2026-11-10")
        self.ok("rename", "work", "home")
        self.assertEqual(self.account_data("home")["renews"], "2026-11-10")
        # migrate-default 与 restore 都靠 config.copy() 复制配置。
        with open(os.path.join(self.state, "config.json")) as handle:
            config = parse_config(handle.read(), "config.json")
        self.assertEqual(config.copy().accounts["home"].renews, "2026-11-10")
        self.assertEqual(dump_config(config.copy()), dump_config(config))

    def test_t10_dry_run_writes_nothing(self):
        self.ok("add", "work")
        before = self.config_data()
        self.ok("set", "work", "--renews", "2026-11-10", "--dry-run")
        self.assertEqual(self.config_data(), before)

    def test_t13_apply_file_without_renews_clears_it(self):
        self.ok("add", "work", "--renews", "2026-11-10")
        data = self.config_data()
        del data["accounts"]["work"]["renews"]
        path = self.write(os.path.join(self.tmp, "apply.json"), json.dumps(data))
        self.ok("apply", "-f", path)
        self.assertNotIn("renews", self.account_data())

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_t11_bash_completion(self):
        script = self.write(os.path.join(self.tmp, "c.bash"), self.ok("completion", "bash").out)
        runner = self.write(os.path.join(self.tmp, "run.bash"), (
            'source "$1"\n'
            'COMP_LINE="$2"; COMP_POINT=${#COMP_LINE}\n'
            'COMP_WORDS=($COMP_LINE); COMP_CWORD=${#COMP_WORDS[@]}\n'
            '_multi_claude\n'
            'printf "%s\\n" "${COMPREPLY[@]}"\n'))
        self.write(os.path.join(self.fakebin, "multi-claude"),
                   '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        env = self._merged({"PATH": os.pathsep.join([os.path.dirname(sys.executable), self.fakebin,
                                                    "/usr/bin", "/bin"])})
        proc = subprocess.run(["bash", runner, script, "multi-claude set x --ren"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--renews", proc.stdout.split())


class RenewsListTest(CliTestCase):
    def test_t6_no_renewal_dates_no_change(self):
        self.ok("add", "work")
        brief = self.ok("list")
        self.assertRegex(brief.out.splitlines()[0],
                         r"^NAME\s+LOGIN\s+PLAN\s+BILLING\s+PROXY\s+SHARED\s+5H\s+7D\s+LAST USED\s+STATUS$")
        self.assertNotIn("renewal date", brief.err)
        verbose = self.ok("list", "--verbose")
        header = [line for line in verbose.out.splitlines() if line.startswith("NAME")][0]
        self.assertNotIn("RENEWS", header)
        self.assertNotIn("renewal date", verbose.err)
        as_json = self.ok("list", "--json")
        self.assertNotIn("renewal date", as_json.err)
        data = json.loads(as_json.out)
        self.assertEqual([entry["renews"] for entry in data["accounts"]], [None])

    def _list_with_dates(self):
        """一个账号 +25 天、另一个 -6 天；前后两次取到的今天不同（跨午夜）就重跑一次。"""
        for _attempt in range(2):
            today = _today()
            self.ok("set", "soon", "--renews", (today + timedelta(days=25)).isoformat())
            self.ok("set", "late", "--renews", (today - timedelta(days=6)).isoformat())
            brief = self.ok("list")
            offline = self.ok("list", "--offline")
            verbose = self.ok("list", "--verbose")
            as_json = self.ok("list", "--json")
            if _today() == today:
                return brief, offline, verbose, as_json
        self.fail("the date changed twice while the test was running")

    def test_t7b_and_t9_columns_and_json(self):
        self.ok("add", "soon")
        self.ok("add", "late")
        brief, offline, verbose, as_json = self._list_with_dates()
        for result in (brief, offline):
            header = result.out.splitlines()[0]
            self.assertIn("RENEWS", header)
            self.assertLess(header.index("RENEWS"), header.index("PROXY"))
            self.assertRegex(result.out, r"\nsoon\s.*\(25d\)\s")
            self.assertRegex(result.out, r"\nlate\s.*\(6d ago\)\s")
        self.assertIn("LOGIN  RENEWS", offline.out.splitlines()[0])
        self.assertIn("BILLING  RENEWS", brief.out.splitlines()[0])
        verbose_header = [line for line in verbose.out.splitlines() if line.startswith("NAME")][0]
        self.assertLess(verbose_header.index("RENEWS"), verbose_header.index("LINK"))
        for result in (brief, offline, verbose, as_json):
            self.assertIn("renewal date has passed for late", result.err)
            self.assertNotIn("soon", result.err.split("renewal date has passed for")[1].splitlines()[0])
        data = json.loads(as_json.out)
        days = {entry["name"]: entry["renews"]["days_left"] for entry in data["accounts"]}
        self.assertEqual(days, {"soon": 25, "late": -6})


class RenewsCellTest(unittest.TestCase):
    def test_t7_cells(self):
        today = date(2026, 10, 16)
        cases = [("2026-11-10", "11-10 (25d)"), ("2026-10-19", "10-19 (3d!)"), ("2026-10-16", "10-16 (today!)"),
                 ("2026-10-10", "10-10 (6d ago)"), ("2027-01-05", "2027-01-05 (81d)"), (None, "-")]
        for value, shown in cases:
            with self.subTest(value=value):
                self.assertEqual(cli._renews_cell(value, today), shown)
        self.assertEqual(cli._renews_dict("2026-10-10", today), {"date": "2026-10-10", "days_left": -6})
        self.assertIsNone(cli._renews_dict(None, today))


class RenewsHelpTest(unittest.TestCase):
    def help_text(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.build_parser().parse_args(list(argv) + ["--help"])
        self.assertEqual(raised.exception.code, 0)
        return out.getvalue()

    def example_commands(self, text):
        self.assertIn("Examples:", text)
        lines = text.split("Examples:", 1)[1].splitlines()
        commands = []
        for line in lines[1:]:
            if not line.startswith("  "):
                break
            commands.append(re.split(r"\s{2,}", line.strip())[0])
        return commands

    def parse_example(self, command):
        words = shlex.split(command)
        self.assertEqual(words[0], "multi-claude")
        if "--help" in words:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                cli.build_parser().parse_args(words[1:])
            self.assertEqual(raised.exception.code, 0)
            return None
        return cli.build_parser().parse_args(words[1:])

    def subparser(self, name):
        parser = cli.build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices[name]
        raise AssertionError("no subparsers")

    def test_t8_examples_parse_and_cover_options(self):
        for argv in ((), ("set",), ("add",), ("list",)):
            with self.subTest(command=argv):
                commands = self.example_commands(self.help_text(*argv))
                self.assertTrue(commands)
                for command in commands:
                    self.parse_example(command)
                if not argv:
                    continue
                options = {option for action in self.subparser(argv[0])._actions
                           for option in action.option_strings} - {"-h", "--help"}
                if argv[0] in ("set", "add"):
                    options.discard("--verbose")
                used = {word for command in commands for word in shlex.split(command)}
                self.assertEqual(sorted(options - used), [])

    def test_t14_main_examples_keep_descriptions(self):
        lines = self.help_text().split("Examples:", 1)[1].splitlines()
        for command, text in cli._EXAMPLES:
            if not text:
                continue
            with self.subTest(command=command):
                self.assertTrue(any(command + "  " in line and line.endswith(text) for line in lines))

    def test_descriptions_fit_80_columns(self):
        for name in ("set", "add", "list"):
            with self.subTest(command=name):
                for line in (self.subparser(name).description or "").splitlines():
                    self.assertLessEqual(len(line), 80)


if __name__ == "__main__":
    unittest.main()
