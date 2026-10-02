"""v0.7.0：usage --history、list 的 LAST USED、找不到 claude 时的安装提示
（方案 docs/feature/feature-usage-history.md §8）。全部使用合成的会话记录。"""

import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone

from helpers import CliTestCase


def _line(reply_id, model, when, input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
          kind="assistant"):
    return json.dumps({"type": kind, "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "message": {"id": reply_id, "model": model,
                                   "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                             "cache_read_input_tokens": cache_read,
                                             "cache_creation_input_tokens": cache_write}}}) + "\n"


class HistoryBase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "work")
        self.ok("add", "home")
        # 本地今天中午：任何时区下都落在“今天”
        self.today = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        self.yesterday = self.today - timedelta(days=1)

    def session(self, account, name, lines, sub=None):
        parts = [self.root, account, "projects", "-tmp-proj"] + ([name, "subagents"] if sub else [])
        path = os.path.join(*parts, (sub or name) + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as handle:
            handle.writelines(lines)
        return path

    def history_json(self, *args):
        return json.loads(self.ok("usage", "--history", "--json", *args).out)


class UsageHistoryTest(HistoryBase):
    def setUp(self):
        super().setUp()
        t, y = self.today.astimezone(timezone.utc), self.yesterday.astimezone(timezone.utc)
        self.session("work", "s1", [
            _line("m1", "opus", y, 10, 100, 1000, 50),
            _line("m1", "opus", y, 10, 120, 1000, 50),   # 同一回复的后续内容块：取最大值，不累加
            _line("m2", "sonnet", t, 1, 20, 300, 0),
            "not json\n",
            _line("u1", "opus", t, 99, 99, 99, 99, kind="user"),
            _line("syn", "<synthetic>", t, 5, 5, 5, 5),
        ])
        self.session("work", "s1", [_line("a1", "haiku", t, 2, 3, 4, 5)], sub="agent-x")
        self.session("home", "s9", [_line("m2", "sonnet", t, 1, 20, 300, 0), _line("h1", "opus", t, 7, 70, 0, 0)])

    def test_t1_t4_t5_by_day_and_total(self):
        data = self.history_json()
        work = {row["key"]: row for row in data["accounts"][0]["rows"]}
        self.assertEqual(data["accounts"][0]["name"], "work")
        day_y, day_t = self.yesterday.date().isoformat(), self.today.date().isoformat()
        self.assertEqual((work[day_y]["output"], work[day_y]["replies"]), (120, 1))
        self.assertEqual((work[day_t]["input"], work[day_t]["output"], work[day_t]["replies"]), (3, 23, 2))
        total = {row["key"]: row for row in data["total"]}
        # m2 在两个账号里都有（模拟 handoff），TOTAL 只算一次
        self.assertEqual((total[day_t]["output"], total[day_t]["replies"]), (93, 3))
        self.assertEqual(data["days"], 7)

    def test_t2_by_model(self):
        data = self.history_json("--by", "model")
        keys = [row["key"] for row in data["accounts"][0]["rows"]]
        self.assertEqual(keys, ["opus", "sonnet", "haiku"])
        self.assertNotIn("<synthetic>", keys)

    def test_t3_days_window_and_old_files(self):
        old = self.session("work", "old", [_line("o1", "opus", self.today.astimezone(timezone.utc), 1, 1, 1, 1)])
        long_ago = time.time() - 30 * 86400
        os.utime(old, (long_ago, long_ago))
        data = self.history_json("--days", "1", "work")
        rows = data["accounts"][0]["rows"]
        self.assertEqual([row["key"] for row in rows], [self.today.date().isoformat()])
        self.assertEqual(rows[0]["replies"], 2)  # o1 所在文件 mtime 太旧被跳过；昨天的 m1 不在窗口内

    def test_text_output(self):
        out = self.ok("usage", "--history").out
        self.assertIn("work:", out)
        self.assertIn("TOTAL (each reply counted once):", out)
        self.assertIn("never reads credentials", out)


class HistoryErrorsTest(HistoryBase):
    def test_t6_bad_arguments(self):
        for args in (("--history", "--days", "0"), ("--history", "--days", "400"), ("--history", "--by", "x"),
                     ("--days", "3"), ("--by", "model")):
            with self.subTest(args=args):
                self.assertEqual(self.run_cli("usage", *args).code, 2)
        self.assertEqual(self.run_cli("usage", "--history", "nosuch").code, 1)

    def test_t7_no_replies(self):
        out = self.ok("usage", "--history", "home").out
        self.assertIn("no replies in the last 7 days", out)


class HistoryEdgeTest(HistoryBase):
    def test_unreadable_file_is_skipped_with_warning(self):
        t = self.today.astimezone(timezone.utc)
        self.session("work", "ok", [_line("r1", "opus", t, 1, 2)])
        bad = self.session("work", "bad", [_line("r2", "opus", t, 1, 2)])
        os.chmod(bad, 0)
        self.addCleanup(os.chmod, bad, 0o644)
        if os.access(bad, os.R_OK):
            self.skipTest("running as a user who can read any file")
        result = self.ok("usage", "--history", "work", "--json")
        self.assertIn("skipped 1 session file", result.err)
        self.assertEqual(json.loads(result.out)["accounts"][0]["rows"][0]["replies"], 1)

    def test_reply_straddling_the_start_is_left_out(self):
        start = self.today.replace(hour=0)
        before, after = (start - timedelta(minutes=1)).astimezone(timezone.utc), (start + timedelta(minutes=1)).astimezone(timezone.utc)
        self.session("work", "s", [_line("x", "opus", before, 1, 5), _line("x", "opus", after, 1, 9)])
        rows = self.history_json("--days", "1", "work")["accounts"][0]["rows"]
        self.assertEqual(rows, [])

    def test_missing_model_counts_as_unknown(self):
        line = json.loads(_line("q", "opus", self.today.astimezone(timezone.utc), 1, 4))
        del line["message"]["model"]
        self.session("work", "s", [json.dumps(line) + "\n"])
        rows = self.history_json("--by", "model", "work")["accounts"][0]["rows"]
        self.assertEqual([(row["key"], row["output"]) for row in rows], [("unknown", 4)])

    def test_local_time_zone_decides_the_day(self):
        # 上海（UTC+8）：UTC 15:30 是当天 23:30，UTC 16:30 已是第二天 00:30
        day = datetime(2026, 9, 30, tzinfo=timezone.utc)
        self.session("work", "s", [_line("a", "opus", day.replace(hour=15, minute=30), 1, 1),
                                   _line("b", "opus", day.replace(hour=16, minute=30), 1, 1)])
        for path in [os.path.join(self.root, "work", "projects", "-tmp-proj", "s.jsonl")]:
            os.utime(path, None)
        data = json.loads(self.ok("usage", "--history", "work", "--json", "--days", "366",
                                  env={"TZ": "Asia/Shanghai"}).out)
        keys = [row["key"] for row in data["accounts"][0]["rows"]]
        self.assertEqual(keys, ["2026-09-30", "2026-10-01"])

    def test_no_configuration_and_bad_days(self):
        import shutil
        shutil.rmtree(self.state)
        result = self.ok("usage", "--history")
        self.assertIn("no configuration yet", result.out)
        self.assertEqual(json.loads(self.ok("usage", "--history", "--json").out)["accounts"], [])
        self.assertEqual(self.run_cli("usage", "--history", "--days", "abc").code, 2)


class LastUsedTest(HistoryBase):
    def test_verbose_list_has_no_last_used(self):
        self.assertNotIn("LAST USED", self.ok("list", "--verbose").out)


    def test_t8_last_used(self):
        self.session("work", "s1", [_line("m1", "opus", self.today.astimezone(timezone.utc), 1, 1)])
        two_hours_ago = time.time() - 2 * 3600
        for path in (os.path.join(self.root, "work", "projects", "-tmp-proj", "s1.jsonl"),):
            os.utime(path, (two_hours_ago, two_hours_ago))
        lines = self.ok("list").out.splitlines()
        self.assertIn("LAST USED", lines[0])
        row = {line.split()[0]: line for line in lines[1:3]}
        self.assertIn("2h ago", row["work"])
        self.assertIn(" - ", row["home"])
        data = json.loads(self.ok("list", "--json").out)
        entries = {entry["name"]: entry for entry in data["accounts"]}
        self.assertRegex(entries["work"]["last_used"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNone(entries["home"]["last_used"])


class InstallHintTest(CliTestCase):
    def test_t9_claude_not_found(self):
        self.ok("add", "work")
        env = {"PATH": self.fakebin}
        os.unlink(os.path.join(self.fakebin, "claude"))
        for args, code in ((("login", "work"), 127), (("mcp", "work", "list"), 127), (("run", "work"), 127),
                           (("doctor",), 1)):
            with self.subTest(args=args):
                result = self.run_cli(*args, env=env)
                self.assertEqual(result.code, code, result)
                self.assertIn("curl -fsSL https://claude.ai/install.sh", result.out + result.err)
        # 其它命令找不到时不带 claude 的安装提示
        result = self.run_cli("run", "work", "--", "no-such-cmd-xyz", env=env)
        self.assertNotIn("claude.ai/install.sh", result.err)
        result = self.run_cli("code", "work", env=env)
        self.assertEqual(result.code, 127, result)
        self.assertNotIn("claude.ai/install.sh", result.err)


if __name__ == "__main__":
    unittest.main()
