"""方案 feature-session-list §8：跨账号、跨目录的会话列表 `sessions`（别名 `conversations`）。

会话文件全部是合成的，放在测试沙箱的家目录里；行的形状取自方案 §1.1 核实过的键名。
"""

import json
import os
import shlex
import shutil
import sys
import unittest

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import sessions  # noqa: E402

SID1 = "11111111-aaaa-bbbb-cccc-000000000001"
SID2 = "22222222-aaaa-bbbb-cccc-000000000002"
SID3 = "33333333-aaaa-bbbb-cccc-000000000003"
ENV_DEFAULT = "env -u CLAUDE_CONFIG_DIR -u CLAUDE_SECURESTORAGE_CONFIG_DIR claude --resume "


def user(text, cwd="/w/proj", ts="2026-10-01T02:00:00.000Z", **extra):
    record = {"type": "user", "cwd": cwd, "timestamp": ts, "message": {"role": "user", "content": text}}
    record.update(extra)
    return record


def assistant(ts, cwd="/w/proj"):
    return {"type": "assistant", "cwd": cwd, "timestamp": ts, "message": {"role": "assistant", "content": []}}


def custom(title):
    return {"type": "custom-title", "customTitle": title}


def ai(title):
    return {"type": "ai-title", "aiTitle": title}


def padding(size):
    """没有 cwd、时间戳、标题的一行，用来把别的行推出尾部窗口或撑满头部额度。"""
    return {"type": "attachment", "blob": "x" * size}


class SessionsTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "a")
        self.ok("add", "b")
        self.dir_a = os.path.join(self.root, "a")
        self.dir_b = os.path.join(self.root, "b")
        self.default = os.path.join(self.home, ".claude")

    def make(self, account_dir, sid, records, project="-w-proj", raw_tail=b"", mtime=None):
        directory = os.path.join(account_dir, "projects", project)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, sid + ".jsonl")
        with open(path, "wb") as handle:
            for record in records:
                handle.write(record if isinstance(record, bytes) else (json.dumps(record) + "\n").encode())
            handle.write(raw_tail)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def listing(self, *args, **kwargs):
        return json.loads(self.ok("sessions", "--json", *args, **kwargs).out)

    def one(self, *args, **kwargs):
        data = self.listing(*args, **kwargs)
        self.assertEqual(len(data["sessions"]), 1, data)
        return data["sessions"][0]


class ListingTest(SessionsTestCase):
    def test_t1_text_output_across_accounts_and_directories(self):
        self.make(self.dir_a, SID1, [user("first question", ts="2026-10-01T02:00:00.000Z"),
                                     assistant("2026-10-01T03:30:00.000Z"), ai("Older session")])
        self.make(self.dir_a, SID2, [user("q", cwd="/w/other", ts="2026-10-02T16:30:00.000Z"),
                                     assistant("2026-10-03T01:00:00.000Z", cwd="/w/other"), custom("Newest session")],
                  project="-w-other")
        self.make(self.dir_b, SID3, [user("q", cwd="/w/third", ts="2026-10-02T00:00:00.000Z"), ai("Middle session")],
                  project="-w-third")
        out = self.ok("sessions", env={"TZ": "Asia/Shanghai"}).out.splitlines()
        # 按最后更新时间从新到旧；时间按本地时区（UTC+8）显示。不带 --cmd 时每条会话一行，不打印续聊命令
        self.assertEqual(out, [
            "TITLE           ACCOUNT  CREATED           UPDATED           SESSION ID",
            "Newest session  a        2026-10-03 00:30  2026-10-03 09:00  " + SID2,
            "Middle session  b        2026-10-02 08:00  2026-10-02 08:00  " + SID3,
            "Older session   a        2026-10-01 10:00  2026-10-01 11:30  " + SID1])

    def test_cmd_column_when_the_terminal_is_wide_enough(self):
        self.make(self.dir_a, SID1, [user("q"), ai("One")])
        self.make(self.dir_b, SID2, [user("q", cwd="/w/other", ts="2026-10-01T01:00:00.000Z"), ai("Two")],
                  project="-w-other")
        env = {"TZ": "Asia/Shanghai", "COLUMNS": "300"}
        wide = self.ok("sessions", "--cmd", env=env).out.splitlines()
        self.assertEqual(wide, [
            "TITLE  ACCOUNT  CREATED           UPDATED           CMD",
            "One    a        2026-10-01 10:00  2026-10-01 10:00  cd /w/proj && claude-a --resume " + SID1,
            "Two    b        2026-10-01 09:00  2026-10-01 09:00  cd /w/other && claude-b --resume " + SID2])
        # 会话 ID 已在命令末尾，带 --cmd 时不再单列一次
        # 放不下时整张表改成命令单独一行；恰好放得下仍算放得下。标题与目录都是 ASCII，len 就是显示宽度
        widest = max(len(line) for line in wide)
        self.assertEqual(self.ok("sessions", "--cmd", env=dict(env, COLUMNS=str(widest))).out.splitlines(), wide)
        narrow = [
            "TITLE  ACCOUNT  CREATED           UPDATED",
            "One    a        2026-10-01 10:00  2026-10-01 10:00",
            "  cd /w/proj && claude-a --resume " + SID1,
            "Two    b        2026-10-01 09:00  2026-10-01 09:00",
            "  cd /w/other && claude-b --resume " + SID2]
        self.assertEqual(self.ok("sessions", "--cmd", env=dict(env, COLUMNS=str(widest - 1))).out.splitlines(), narrow)
        self.assertEqual(self.ok("sessions", "--cmd", env=dict(env, COLUMNS="40")).out.splitlines(), narrow)

    def test_wide_titles_keep_the_columns_aligned(self):
        # 中文一个字占两格：按字符数补空格的话，中文标题后面的列会往右错
        self.make(self.dir_a, SID1, [user("q"), custom("修复解析")])
        self.make(self.dir_a, SID2, [user("q", ts="2026-10-01T01:00:00.000Z"), custom("abcdefgh")])
        tail = "  a        2026-10-01 {}:00  2026-10-01 {}:00  {}"
        env = {"TZ": "Asia/Shanghai"}
        self.assertEqual(self.ok("sessions", env=env).out.splitlines(),
                         ["TITLE     ACCOUNT  CREATED           UPDATED           SESSION ID",
                          "修复解析" + tail.format("10", "10", SID1), "abcdefgh" + tail.format("09", "09", SID2)])
        self.make(self.dir_a, SID3, [user("q", ts="2026-10-01T00:00:00.000Z"), custom("修复解析器")])
        self.assertEqual(self.ok("sessions", env=env).out.splitlines(),
                         ["TITLE       ACCOUNT  CREATED           UPDATED           SESSION ID",
                          "修复解析  " + tail.format("10", "10", SID1), "abcdefgh  " + tail.format("09", "09", SID2),
                          "修复解析器" + tail.format("08", "08", SID3)])
        # 零宽连接符与组合字符不占格：e 加组合重音符是 1 格，这个标题共 8 格，和 abcdefgh 一样宽
        self.make(self.dir_a, SID3, [user("q", ts="2026-10-01T00:00:00.000Z"), custom("abcde\u0301f\u200dgh")])
        self.assertEqual(self.ok("sessions", env=env).out.splitlines()[1:],
                         ["修复解析" + tail.format("10", "10", SID1), "abcdefgh" + tail.format("09", "09", SID2),
                          "abcde\u0301f\u200dgh" + tail.format("08", "08", SID3)])

    def test_cmd_writes_the_home_directory_as_a_tilde(self):
        cases = ((os.path.join(self.home, "sub"), "cd ~/sub"), (self.home, "cd ~"), (self.home + "/", "cd ~"),
                 (os.path.join(self.home, "my proj"), "cd ~/'my proj'"), ("/w/proj", "cd /w/proj"),
                 (self.home + "2", "cd " + shlex.quote(self.home + "2")))
        for cwd, expected in cases:
            self.make(self.dir_a, SID1, [user("q", cwd=cwd), ai("T")], project=sessions.encode(cwd))
            line = "  {} && claude-a --resume {}".format(expected, SID1)
            self.assertEqual(self.ok("sessions", "--cmd").out.splitlines()[2], line, cwd)
            # JSON 给脚本用，保持绝对路径；--cmd 对它没有影响
            data = self.listing()
            self.assertTrue(data["sessions"][0]["resume"].startswith("cd " + shlex.quote(cwd) + " && "), cwd)
            self.assertEqual(self.listing("--cmd"), data)
            shutil.rmtree(os.path.join(self.dir_a, "projects"))

    def test_t16_json_structure(self):
        path = self.make(self.dir_a, SID1, [user("q"), assistant("2026-10-01T03:30:00.000Z"), custom("Named")])
        data = self.listing()
        self.assertEqual(data, {"schema_version": 1, "total": 1, "skipped": 0, "sessions": [{
            "account": "a", "registered": True, "directory": "/w/proj", "project": "-w-proj", "session_id": SID1,
            "title": "Named", "title_source": "custom", "created": "2026-10-01T02:00:00Z",
            "updated": "2026-10-01T03:30:00Z", "file": path,
            "resume": "cd /w/proj && claude-a --resume " + SID1}]})

    def test_same_update_time_orders_by_account_then_id(self):
        for account_dir, sid in ((self.dir_b, SID1), (self.dir_a, SID2), (self.dir_a, SID1)):
            self.make(account_dir, sid, [user("q"), ai("T")])
        self.assertEqual([(row["account"], row["session_id"]) for row in self.listing()["sessions"]],
                         [("a", SID1), ("a", SID2), ("b", SID1)])

    def test_t9_same_session_in_two_accounts(self):
        for account_dir in (self.dir_a, self.dir_b):
            self.make(account_dir, SID1, [user("q"), ai("Shared")])
        rows = self.listing()["sessions"]
        self.assertEqual(sorted(row["resume"] for row in rows),
                         ["cd /w/proj && claude-a --resume " + SID1, "cd /w/proj && claude-b --resume " + SID1])

    def test_t19_directory_with_a_space_is_quoted(self):
        self.make(self.dir_a, SID1, [user("q", cwd="/w/my proj")], project="-w-my-proj")
        self.assertEqual(self.one()["resume"], "cd '/w/my proj' && claude-a --resume " + SID1)

    def test_t22_read_only(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        self.make(self.default, SID2, [user("q")])
        before = self.snapshot()
        self.ok("sessions")
        self.ok("sessions", "--json", "--grep", "q")
        self.assertEqual(self.snapshot(), before)

    def test_t20_runs_during_an_unfinished_migration(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        self.write(os.path.join(self.state, "migrate-journal.json"), json.dumps(
            {"name": "main", "source": "s", "target": "t", "backup": "b", "mode": "rename", "phase": "planned"}))
        result = self.ok("sessions")
        self.assertIn(SID1, result.out)
        self.assertIn("unfinished migration", result.err)


class TitleTest(SessionsTestCase):
    def test_t2_title_priority(self):
        self.make(self.dir_a, SID1, [user("the prompt"), ai("AI title"), custom("Custom title"), ai("AI again")])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("Custom title", "custom"))
        self.make(self.dir_a, SID1, [user("the prompt"), ai("AI title"), ai("AI again")])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("AI again", "ai"))
        self.make(self.dir_a, SID1, [user("the prompt")])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("the prompt", "prompt"))
        self.make(self.dir_a, SID1, [{"type": "mode", "mode": "x"}, {"type": "system", "cwd": "/w/proj",
                                                                    "timestamp": "2026-10-01T02:00:00.000Z"}])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("", "none"))
        self.assertTrue(self.ok("sessions").out.splitlines()[1].startswith("-      a  "))

    def test_t3_first_plain_prompt(self):
        self.make(self.dir_a, SID1, [
            user("meta line", isMeta=True),
            user("sidechain line", isSidechain=True),
            user("<command-name>/clear</command-name>"),
            user([{"type": "tool_result", "content": "x"}]),
            # 普通提问行带着值为 false 的标记：按值判断，不能因为键存在就排除
            user([{"type": "text", "text": "real question"}], isSidechain=False, isMeta=False),
            user("later question"),
        ])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("real question", "prompt"))

    def test_t4_title_is_cleaned_and_cut(self):
        self.make(self.dir_a, SID1, [user("q"), custom("line one\nline\ttwo \x1b[31mred\x07 " + "x" * 300)])
        title = self.one()["title"]
        self.assertTrue(title.startswith("line one line two [31mred x"), title)
        self.assertEqual(len(title), 201)
        self.assertTrue(title.endswith("…"))
        out = self.ok("sessions").out
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertEqual(len(out.splitlines()), 2)
        # 标题在第一列；清洗后的标题里没有连续空格，按列间的两个空格切开即可
        shown = out.splitlines()[1].split("  a  ", 1)[0]
        self.assertEqual(len(shown), 61)
        self.assertTrue(shown.endswith("…"))

    def test_t7b_title_that_cleans_to_nothing_does_not_hide_the_next_one(self):
        self.make(self.dir_a, SID1, [user("q"), ai("Good"), custom("\x1b\x07")])
        row = self.one()
        self.assertEqual((row["title"], row["title_source"]), ("Good", "ai"))

    def test_t7b_format_characters_are_removed(self):
        # U+202E 能让终端把后面的文字倒着显示：标题里删掉，目录里出现就不用这个目录
        self.make(self.dir_a, SID1, [user("q", cwd="/w/\u202eproj"), ai("Ti\u202etle\u2028x")])
        row = self.one()
        self.assertEqual((row["title"], row["directory"]), ("Title x", None))
        self.assertNotIn("\u202e", self.ok("sessions").out)

    def test_t7b_ordinary_text_is_kept(self):
        # 过滤规则不能误伤正常文字：中文、空格、用零宽连接符（U+200D）拼成的组合表情
        title, cwd = "修复 解析器 \U0001f468\u200d\U0001f469", "/w/我的 项目\u200c"
        self.make(self.dir_a, SID1, [user("q", cwd=cwd), custom(title)], project=sessions.encode(cwd))
        row = self.one()
        self.assertEqual((row["title"], row["directory"]), (title, cwd))
        self.assertIn("cd '{}' && claude-a --resume {}".format(cwd, SID1), self.ok("sessions", "--cmd").out)

    def test_t6_title_only_near_the_start(self):
        # 标题行在首条提问之前、又被挤出尾部窗口：靠头部扫描取到
        self.make(self.dir_a, SID1, [custom("Early title"), user("the prompt"),
                                     padding(sessions.TAIL_BYTES), assistant("2026-10-01T05:00:00.000Z")])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("Early title", "custom"))

    def test_t6b_title_between_prompt_and_tail_is_not_seen(self):
        # 方案 §5.1.2 的已知限制：头部取到提问就停，尾部窗口里又没有标题
        self.make(self.dir_a, SID1, [user("the prompt"), ai("Middle title"),
                                     padding(sessions.TAIL_BYTES), assistant("2026-10-01T05:00:00.000Z")])
        self.assertEqual((self.one()["title"], self.one()["title_source"]), ("the prompt", "prompt"))


class FieldFallbackTest(SessionsTestCase):
    def test_t5_long_line_carries_the_first_cwd(self):
        self.make(self.dir_a, SID1, [user("p" * (sessions.TAIL_BYTES + 1000)), ai("T"),
                                     padding(sessions.TAIL_BYTES), assistant("2026-10-01T05:00:00.000Z", cwd="/w/later")])
        row = self.one()
        self.assertEqual((row["directory"], row["created"]), ("/w/proj", "2026-10-01T02:00:00Z"))

    def test_t5b_first_cwd_line_longer_than_the_head_budget(self):
        huge = user("p" * (sessions.HEAD_BUDGET + 1000))
        # 尾部有两个 cwd：只有编码后等于项目目录名的那个是启动目录
        self.make(self.dir_a, SID1, [huge, assistant("2026-10-01T05:00:00.000Z"),
                                     assistant("2026-10-01T06:00:00.000Z", cwd="/w/later")])
        row = self.one()
        self.assertEqual((row["directory"], row["created"], row["updated"]),
                         ("/w/proj", None, "2026-10-01T06:00:00Z"))
        self.make(self.dir_a, SID1, [huge, assistant("2026-10-01T06:00:00.000Z", cwd="/w/later")])
        row = self.one()
        self.assertIsNone(row["directory"])
        self.assertEqual(row["resume"], "claude-a --resume " + SID1)

    def test_t5c_cwd_with_a_newline_is_not_used(self):
        self.make(self.dir_a, SID1, [user("q", cwd="/w/pr\noj"), assistant("2026-10-01T05:00:00.000Z"), ai("T")])
        self.assertIsNone(self.one()["directory"])
        self.assertEqual(len(self.ok("sessions").out.splitlines()), 2)
        self.assertEqual(len(self.ok("sessions", "--cmd").out.splitlines()), 3)

    def test_t7_missing_fields_bad_lines_and_half_line(self):
        self.make(self.dir_a, SID1, [b"not json\n", b"[1, 2]\n", {"type": "mode", "mode": "x"}, ai("Only a title")],
                  raw_tail=b'{"type": "user", "cwd": "/w/proj", "timestamp": "2026-10-0', mtime=1790000000)
        row = self.one()
        self.assertEqual(row["directory"], None)
        self.assertEqual(row["project"], "-w-proj")
        self.assertEqual(row["created"], None)
        self.assertEqual(row["updated"], "2026-09-21T14:13:20Z")
        self.assertEqual(row["resume"], "claude-a --resume " + SID1)
        unknown = "claude-a --resume {}  (start directory unknown; project -w-proj)".format(SID1)
        out = self.ok("sessions", "--cmd", env={"COLUMNS": "40"}).out.splitlines()
        self.assertEqual(out[2], "  " + unknown)
        self.assertIn("  -  ", out[1])
        self.assertTrue(self.ok("sessions", "--cmd", env={"COLUMNS": "300"}).out.splitlines()[1].endswith("  " + unknown))
        # 不带 --cmd 时没有命令，这段说明也不出现
        self.assertNotIn("start directory unknown", self.ok("sessions").out)

    def test_t7b_deeply_nested_line_is_skipped(self):
        # 嵌套太深的行让 json 抛 RecursionError（不是 ValueError）；它只是一条坏行，别的字段照常取
        self.make(self.dir_a, SID1, [user("q"), b"[" * 60000 + b"\n", ai("T")])
        result = self.ok("sessions")
        self.assertNotIn("Traceback", result.err)
        self.assertEqual((self.one()["title"], self.one()["directory"]), ("T", "/w/proj"))

    def test_t7b_timestamps_outside_the_local_range(self):
        # 合法的 ISO 时间，换算成本地时间却越界：0001 年在 UTC 以西、9999 年在 UTC 以东
        self.make(self.dir_a, SID1, [user("q", ts="0001-01-01T00:00:00Z"), ai("Old")])
        self.make(self.dir_a, SID2, [user("q", ts="9999-12-31T23:59:59Z"), ai("Far")])
        for zone, sid in (("America/New_York", SID1), ("Asia/Shanghai", SID2), ("UTC", None)):
            result = self.ok("sessions", env={"TZ": zone})
            self.assertNotIn("Traceback", result.err)
            self.assertEqual(len(result.out.splitlines()), 3, result)
            # 换算不了的时间显示 -（这两条会话的创建与更新时间是同一个值）；UTC 下两条都能显示
            dashed = [line.split()[4] for line in result.out.splitlines() if line.split()[2:4] == ["-", "-"]]
            self.assertEqual(dashed, [sid] if sid else [], result)
        self.assertEqual(len(self.listing()["sessions"]), 2)

    def test_t7b_relative_cwd_is_not_used(self):
        self.make(self.dir_a, SID1, [user("q", cwd="-"), ai("T")], project="-")
        self.assertEqual(self.one()["resume"], "claude-a --resume " + SID1)

    def test_empty_file(self):
        self.make(self.dir_a, SID1, [])
        row = self.one()
        self.assertEqual((row["title_source"], row["directory"], row["created"]), ("none", None, None))

    def test_t8_other_files_are_not_sessions(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        directory = os.path.join(self.dir_a, "projects", "-w-proj")
        line = json.dumps(user("q")) + "\n"
        self.write(os.path.join(directory, SID1, "subagents", "agent-x.jsonl"), line)
        self.write(os.path.join(directory, ".multi-claude-{}.abc.jsonl.tmp".format(SID1)), line)
        self.write(os.path.join(directory, SID1 + ".jsonl.multi-claude-bak.20261011_120000"), line)
        self.write(os.path.join(directory, "notes.txt"), line)
        os.makedirs(os.path.join(directory, SID2 + ".jsonl"))
        self.write(os.path.join(self.dir_a, "projects", "stray.jsonl"), line)
        # 结尾带换行的名字能通过 match 加 $，以 - 开头的名字拼进续聊命令会被当成选项
        self.write(os.path.join(directory, SID3 + "\n.jsonl"), line)
        self.write(os.path.join(directory, "--help.jsonl"), line)
        self.assertEqual([row["session_id"] for row in self.listing()["sessions"]], [SID1])

    @unittest.skipIf(os.geteuid() == 0, "root can read files without the read permission")
    def test_t17_unreadable_file_is_skipped_with_a_warning(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        os.chmod(self.make(self.dir_a, SID2, [user("q")]), 0)
        result = self.ok("sessions", "--json")
        data = json.loads(result.out)
        self.assertEqual(([row["session_id"] for row in data["sessions"]], data["skipped"]), ([SID1], 1))
        self.assertIn("skipped 1 session file(s) that could not be read", result.err)


class SourcesTest(SessionsTestCase):
    def test_t10_unregistered_default_directory(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        self.make(self.default, SID2, [user("q", cwd="/w/home"), ai("Plain claude")], project="-w-home")
        rows = {row["session_id"]: row for row in self.listing()["sessions"]}
        self.assertEqual((rows[SID2]["account"], rows[SID2]["registered"]), ("default", False))
        self.assertEqual(rows[SID2]["resume"], "cd /w/home && " + ENV_DEFAULT + SID2)
        self.assertTrue(rows[SID1]["registered"])
        for name in ("default", "DEFAULT"):
            self.assertEqual(self.one(name)["session_id"], SID2)

    def test_t10b_registered_account_named_default_wins(self):
        self.ok("add", "default")
        self.make(os.path.join(self.root, "default"), SID1, [user("q"), ai("T")])
        self.make(self.default, SID2, [user("q"), ai("T")])
        rows = {row["session_id"]: row for row in self.listing()["sessions"]}
        self.assertEqual((rows[SID1]["account"], rows[SID1]["registered"]), ("default", True))
        self.assertEqual(rows[SID1]["resume"], "cd /w/proj && claude-default --resume " + SID1)
        self.assertEqual((rows[SID2]["account"], rows[SID2]["registered"]), ("default", False))
        self.assertEqual(self.one("default")["session_id"], SID1)

    def test_t11_migrated_default_is_listed_once(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        os.symlink(self.dir_a, self.default)
        row = self.one()
        self.assertEqual((row["account"], row["registered"]), ("a", True))

    def test_t12_no_configuration(self):
        os.unlink(os.path.join(self.state, "config.json"))
        self.assertEqual(self.ok("sessions").out, "no sessions found\n")
        self.assertEqual(self.listing(), {"schema_version": 1, "sessions": [], "total": 0, "skipped": 0})
        self.make(self.default, SID1, [user("q"), ai("T")])
        self.assertEqual(self.one()["account"], "default")

    def test_t13_account_argument(self):
        self.make(self.dir_a, SID1, [user("q"), ai("T")])
        self.make(self.dir_b, SID2, [user("q"), ai("T")])
        self.assertEqual(self.one("b")["session_id"], SID2)
        self.assertEqual(self.one("B")["session_id"], SID2)
        result = self.run_cli("sessions", "nosuch")
        self.assertEqual(result.code, 1)
        self.assertIn("account 'nosuch' is not registered", result.err)
        # 默认目录没有 projects/：default 不是可选的来源
        self.assertEqual(self.run_cli("sessions", "default").code, 1)
        self.assertEqual(self.run_cli("sessions", "bad name").code, 2)
        os.unlink(os.path.join(self.state, "config.json"))
        self.assertEqual(self.run_cli("sessions", "bad name").code, 2)
        self.assertEqual(self.run_cli("sessions", "a").code, 1)

    def test_account_without_projects(self):
        self.assertEqual(self.ok("sessions").out, "no sessions found\n")


class FilterTest(SessionsTestCase):
    def setUp(self):
        super().setUp()
        self.make(self.dir_a, SID1, [user("q", ts="2026-10-01T02:00:00.000Z"), custom("Fix the Parser")])
        self.make(self.dir_a, SID2, [user("q", cwd="/w/robot", ts="2026-10-02T02:00:00.000Z"), ai("Other")],
                  project="-w-robot")
        # 没有时间戳的会话按文件修改时间排序：给一个很早的时间，让它排在最后
        self.make(self.dir_b, SID3, [{"type": "mode", "mode": "x"}], project="-w-lonely", mtime=1500000000)

    def test_t14_grep(self):
        self.assertEqual(self.one("--grep", "parser")["session_id"], SID1)
        self.assertEqual(self.one("--grep", "/W/ROBOT")["session_id"], SID2)
        self.assertEqual(self.one("--grep", "33333333")["session_id"], SID3)
        # 目录未知时按项目目录名匹配
        self.assertEqual(self.one("--grep", "lonely")["session_id"], SID3)
        self.assertEqual(self.listing("--grep", "nothing-matches")["sessions"], [])
        self.assertEqual(self.ok("sessions", "--grep", "nothing-matches").out, "no sessions found\n")

    def test_t15_limit(self):
        data = self.listing("--limit", "1")
        self.assertEqual(([row["session_id"] for row in data["sessions"]], data["total"]), ([SID2], 3))
        out = self.ok("sessions", "--limit", "1").out.splitlines()
        self.assertEqual(len(out), 3)
        self.assertEqual(out[-1], "showing 1 of 3 sessions; use --limit 0 for all, or --grep TEXT to narrow")
        self.assertEqual(len(self.ok("sessions", "--limit", "0").out.splitlines()), 4)
        self.assertEqual(len(self.ok("sessions", "--limit", "0", "--cmd").out.splitlines()), 7)
        result = self.run_cli("sessions", "--limit", "-1")
        self.assertEqual(result.code, 2)
        self.assertIn("--limit must be 0 or more", result.err)
        self.assertEqual(self.run_cli("sessions", "--limit", "x").code, 2)

    def test_t15_default_limit_is_30_for_text_only(self):
        for index in range(28):
            self.make(self.dir_b, "44444444-aaaa-bbbb-cccc-0000000000{:02d}".format(index), [user("q")])
        out = self.ok("sessions").out.splitlines()
        self.assertEqual(len(out), 1 + 30 + 1)
        self.assertEqual(out[-1], "showing 30 of 31 sessions; use --limit 0 for all, or --grep TEXT to narrow")
        self.assertEqual(len(self.listing()["sessions"]), 31)

    def test_t18_conversations_alias(self):
        self.assertEqual(self.ok("conversations").out, self.ok("sessions").out)
        self.assertEqual(self.ok("conversations", "a", "--json").out, self.ok("sessions", "a", "--json").out)
        result = self.run_cli("convrsations")
        self.assertEqual(result.code, 2)
        self.assertIn("unknown command 'convrsations'", result.err)
        # 别名只在子命令位置上生效：叫 conversations 的账号照常使用
        self.ok("add", "conversations")
        self.assertEqual(self.listing("conversations")["sessions"], [])

    def test_t21_help(self):
        self.assertIn("sessions", self.ok("--help").out.split("Everyday:")[1].split("Setup and checks:")[0])
        self.assertIn("conversations", self.ok("sessions", "--help").out)


if __name__ == "__main__":
    unittest.main()
