"""方案 feature-artifact-list §8：跨账号列出已发布的 artifact，以及 handoff 写交接记录。

会话记录按 Claude Code 的实际形态构造：assistant 行带 Artifact 的 tool_use，user 行带 tool_result，
发布成功时顶层有 toolUseResult（url、title、path、audience）。
"""

import contextlib
import io
import json
import os
import sys
from unittest import mock

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import artifacts, cli, sessions  # noqa: E402

SID = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER = "22222222-aaaa-bbbb-cccc-000000000002"
URL1 = "https://claude.ai/artifact/AAAAAAAAAAAAAAAAAAAAA1"
URL2 = "https://claude.ai/artifact/BBBBBBBBBBBBBBBBBBBBB2"
URL3 = "https://claude.ai/code/artifact/0b8f4c2e-1111-2222-3333-444455556666"


class ArtifactTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "a")
        self.ok("add", "b")
        self.dir_a = os.path.join(self.root, "a")
        self.dir_b = os.path.join(self.root, "b")
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)
        self.encoded = sessions.encode(os.path.realpath(self.project))
        self.counter = 0

    # ---- 构造会话记录 ----

    def publish(self, url, title="Title", when="2026-10-01T00:00:00.000Z", description="desc", path=None,
                error=False, result=True, name="Artifact", cwd=None):
        """一次发布的两行：tool_use 与 tool_result。result=False 时 toolUseResult 不是带 url 的对象。"""
        self.counter += 1
        tool_id = "toolu_{:04d}".format(self.counter)
        cwd = self.project if cwd is None else cwd
        use = {"type": "assistant", "uuid": "use-{}".format(self.counter), "timestamp": when, "cwd": cwd,
               "message": {"role": "assistant", "content": [
                   {"type": "tool_use", "id": tool_id, "name": name,
                    "input": {"file_path": "/tmp/x.html", "description": description}}]}}
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": "published " + url}
        if error:
            block["is_error"] = True
        done = {"type": "user", "uuid": "res-{}".format(self.counter), "timestamp": when, "cwd": cwd,
                "message": {"role": "user", "content": [block]}}
        if result:
            done["toolUseResult"] = {"url": url, "title": title, "path": path, "audience": "owner"}
        else:
            done["toolUseResult"] = "published " + url
        return json.dumps(use) + "\n" + json.dumps(done) + "\n"

    def project_dir(self, account_dir):
        return os.path.join(account_dir, "projects", self.encoded)

    def session(self, account_dir, *chunks, sid=SID):
        path = os.path.join(self.project_dir(account_dir), sid + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("".join(chunks) or "{}\n")
        return path

    def append(self, path, *chunks):
        with open(path, "a") as handle:
            handle.write("".join(chunks))

    def seed_log(self):
        """写一条更早、与被测会话无关的有效交接记录，让开始记录时刻 T0 早于被测发布（方案 §8 前置说明）。"""
        entry = {"v": 1, "time": "2000-01-01T00:00:00.000000Z", "session_id": OTHER, "project": "-unrelated",
                 "source_dir": os.path.realpath(self.dir_a), "target_dir": os.path.realpath(self.dir_b),
                 "files": {OTHER + ".jsonl": [1, "0" * 64]}}
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "handoffs.jsonl"), "a") as handle:
            handle.write(json.dumps(entry) + "\n")

    def log_lines(self):
        path = os.path.join(self.state, "handoffs.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, "rb") as handle:
            return handle.read().split(b"\n")

    def handoff(self, target, source, *extra):
        return self.run_cli("handoff", target, "--from", source, "--session", SID, *extra, cwd=self.project)

    def listing(self, *args):
        result = self.ok("artifacts", "--json", *args)
        return {item["url"]: item for item in json.loads(result.out)["artifacts"]}


class ParseTest(ArtifactTestCase):
    def scan(self, text, relpath=SID + ".jsonl"):
        path = os.path.join(self.tmp, "s.jsonl")
        with open(path, "w") as handle:
            handle.write(text)
        return artifacts.scan_file(path, "-p", relpath)[0]

    def test_r1_publish_fields(self):
        found = self.scan(self.publish(URL1, title="T1", path="/tmp/a.html") + self.publish(URL3, title="T3"))
        self.assertEqual([item.url for item in found], [URL1, URL3])
        first = found[0]
        self.assertEqual((first.title, first.path, first.audience, first.description, first.session_id),
                         ("T1", "/tmp/a.html", "owner", "desc", SID))
        self.assertEqual(first.cwd, self.project)
        self.assertEqual(first.record_id, "res-1")
        self.assertEqual(first.timestamp.year, 2026)
        # 偏移指向结果行的开头
        self.assertEqual(first.offset, len(self.publish(URL1).split("\n")[0]) + 1)

    def test_r2_not_publishes(self):
        text = (self.publish(URL1, error=True) + self.publish(URL1, result=False)
                + self.publish(URL1, name="Bash") + self.publish(URL1 + "\n")
                + self.publish("https://claude.ai/artifact/bad id") + "{not json claude.ai/\n" + '"Artifact"\n')
        self.assertEqual(self.scan(text), [])

    def test_r3_subagent(self):
        found = self.scan(self.publish(URL1), relpath=SID + "/subagents/agent-1.jsonl")
        self.assertEqual((found[0].session_id, found[0].relpath), (SID, SID + "/subagents/agent-1.jsonl"))

    def test_r9c_only_session_files(self):
        project = self.project_dir(self.dir_a)
        self.session(self.dir_a, self.publish(URL1))
        self.write(os.path.join(project, SID, "subagents", "agent-1.jsonl"), self.publish(URL2))
        # 不扫：备份文件与备份目录、半途留下的 tmp 目录、- 开头的 ID
        self.write(os.path.join(project, SID + ".jsonl.multi-claude-bak.20261015"), self.publish(URL3))
        self.write(os.path.join(project, SID + ".multi-claude-bak.20261015", "subagents", "x.jsonl"),
                   self.publish(URL3))
        self.write(os.path.join(project, ".multi-claude-" + SID + ".tmp", "subagents", "x.jsonl"),
                   self.publish(URL3))
        self.write(os.path.join(project, "-" + SID[1:] + ".jsonl"), self.publish(URL3))
        found = sorted((relpath for _project, relpath, _path in artifacts.session_files(self.dir_a)))
        self.assertEqual(found, [SID + ".jsonl", SID + "/subagents/agent-1.jsonl"])
        self.assertEqual(sorted(self.listing()), [URL1, URL2])

    def test_r12_control_characters_cleaned(self):
        self.session(self.dir_a, self.publish(URL1, title="bad\x1b[31mred\x07"))
        out = self.ok("artifacts").out
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("\x1b", self.listing()[URL1]["title"])


class OwnershipTest(ArtifactTestCase):
    def test_r4_single(self):
        self.session(self.dir_a, self.publish(URL1))
        item = self.listing()[URL1]
        self.assertEqual((item["accounts"], item["ownership"], item["publishes"]), (["a"], "single", 1))

    def test_r5_copies_without_record(self):
        text = self.publish(URL1)
        self.session(self.dir_a, text)
        self.session(self.dir_b, text)
        item = self.listing()[URL1]
        self.assertEqual((item["accounts"], item["ownership"], item["publishes"]), (["a", "b"], "uncertain", 1))
        out = self.ok("artifacts").out
        self.assertRegex(out, r"a,b \?")
        self.assertIn("? = owner unknown", out)

    def test_r6_handoff_records_owner(self):
        self.seed_log()
        self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        self.append(os.path.join(self.project_dir(self.dir_b), SID + ".jsonl"),
                    self.publish(URL2, when="2026-10-02T00:00:00.000Z"))
        found = self.listing()
        self.assertEqual((found[URL1]["accounts"], found[URL1]["ownership"]), (["a"], "recorded"))
        self.assertEqual((found[URL2]["accounts"], found[URL2]["ownership"]), (["b"], "single"))
        self.assertEqual(self.listing("b").keys(), {URL2})

    def test_r7_round_trip_force(self):
        self.seed_log()
        path_a = self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        self.append(os.path.join(self.project_dir(self.dir_b), SID + ".jsonl"),
                    self.publish(URL2, when="2026-10-02T00:00:00.000Z"))
        # A 也继续写，两边分叉，回传需要 --force
        self.append(path_a, self.publish(URL3, when="2026-10-03T00:00:00.000Z"))
        self.ok("handoff", "a", "--from", "b", "--session", SID, "--force", cwd=self.project)
        found = self.listing()
        self.assertEqual(found[URL1]["accounts"], ["a"])
        self.assertEqual(found[URL1]["ownership"], "recorded")
        self.assertEqual(found[URL2]["accounts"], ["b"])
        # URL3 只在 A 的备份里，备份不扫
        self.assertNotIn(URL3, found)

    def round_trip_with_times(self, times):
        """跑完 R7 后把两条真实交接记录的时间改掉：先后只认行序，结果不变。"""
        self.test_r7_round_trip_force()
        path = os.path.join(self.state, "handoffs.jsonl")
        entries = [json.loads(line) for line in self.log_lines() if line]
        for entry, moment in zip(entries[1:], times):
            entry["time"] = moment
        with open(path, "w") as handle:
            handle.write("".join(json.dumps(entry) + "\n" for entry in entries))
        found = self.listing()
        self.assertEqual((found[URL1]["accounts"], found[URL1]["ownership"]), (["a"], "recorded"))
        self.assertEqual(found[URL2]["accounts"], ["b"])

    def test_r9b_same_second_round_trip(self):
        self.round_trip_with_times(("2026-10-05T00:00:00.000000Z",) * 2)

    def test_r9b_clock_went_back(self):
        self.round_trip_with_times(("2026-10-05T00:00:01.000000Z", "2026-10-05T00:00:00.000000Z"))

    def test_owner_without_copy_falls_back(self):
        self.seed_log()
        path_a = self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        os.unlink(path_a)
        item = self.listing()[URL1]
        self.assertEqual((item["accounts"], item["ownership"]), (["a"], "recorded"))
        self.assertIn("claude-b --resume " + SID, item["resume"])
        self.assertIn(URL1, self.ok("artifacts").out)

    def test_r8_rewritten_prefix_is_uncertain(self):
        self.seed_log()
        path_a = self.session(self.dir_a, self.publish(URL1, title="Abc"))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        os.unlink(path_a)
        path_b = os.path.join(self.project_dir(self.dir_b), SID + ".jsonl")
        with open(path_b, "r+b") as handle:
            data = handle.read()
            handle.seek(0)
            handle.write(data.replace(b"/tmp/x.html", b"/tmp/y.html", 1))
        self.assertEqual(self.listing()[URL1]["ownership"], "uncertain")

    def test_r11b_merge_takes_certain_copy(self):
        self.seed_log()
        self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        path_b = os.path.join(self.project_dir(self.dir_b), SID + ".jsonl")
        with open(path_b, "r+b") as handle:
            data = handle.read()
            handle.seek(0)
            handle.write(data.replace(b"/tmp/x.html", b"/tmp/y.html", 1))
        item = self.listing()[URL1]
        self.assertEqual((item["accounts"], item["ownership"]), (["a"], "recorded"))

    def test_r9e_unrecorded_history_is_uncertain(self):
        # 上线前 A→B：手工复制，不写记录
        text = self.publish(URL1)
        self.session(self.dir_a, text)
        self.session(self.dir_b, text)
        self.append(os.path.join(self.project_dir(self.dir_b), SID + ".jsonl"),
                    self.publish(URL2, when="2026-10-02T00:00:00.000Z"))
        self.ok("handoff", "a", "--from", "b", "--session", SID, "--force", cwd=self.project)
        item = self.listing()[URL1]
        self.assertEqual(item["ownership"], "uncertain")
        self.assertEqual(item["accounts"], ["a", "b"])

    def test_r9f_after_t0(self):
        self.seed_log()
        self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        self.assertEqual(self.listing()[URL1]["ownership"], "recorded")

    def test_r9g_first_handoff_is_uncertain(self):
        self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        self.assertEqual(self.listing()[URL1]["ownership"], "uncertain")


class HandoffLogTest(ArtifactTestCase):
    def test_record_format(self):
        self.session(self.dir_a, self.publish(URL1))
        self.write(os.path.join(self.project_dir(self.dir_a), SID, "subagents", "agent-1.jsonl"), "sub\n")
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        path = os.path.join(self.state, "handoffs.jsonl")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        entry = json.loads(self.log_lines()[0])
        self.assertEqual(entry["session_id"], SID)
        self.assertEqual(entry["project"], self.encoded)
        self.assertEqual(entry["source_dir"], os.path.realpath(self.dir_a))
        self.assertEqual(entry["target_dir"], os.path.realpath(self.dir_b))
        self.assertEqual(sorted(entry["files"]), [SID + ".jsonl", SID + "/subagents/agent-1.jsonl"])
        self.assertEqual(entry["files"][SID + "/subagents/agent-1.jsonl"][0], 4)

    def test_r9_no_record_when_nothing_copied(self):
        self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, "--dry-run", cwd=self.project)
        self.assertEqual(self.log_lines(), [])
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)  # UNCHANGED
        self.assertEqual(len([line for line in self.log_lines() if line]), 1)

    def test_r9_unwritable_state_only_warns(self):
        self.session(self.dir_a, self.publish(URL1))
        os.makedirs(self.state, exist_ok=True)
        os.mkdir(os.path.join(self.state, "handoffs.jsonl"))  # 打不开：是个目录
        result = self.handoff("b", "a")
        self.assertEqual(result.code, 0, result)
        self.assertIn("continue with:", result.out)
        self.assertIn("warning: could not record the handoff", result.err)
        self.assertIn("wrong owner", result.err)

    def test_r9_record_error_in_process(self):
        # run_cli 走子进程打不了桩，这条在进程内调用 cli.main
        self.session(self.dir_a, self.publish(URL1))
        out, err = io.StringIO(), io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.project)
        try:
            with mock.patch.dict(os.environ, self._merged({"PWD": self.project}), clear=True), \
                    mock.patch("multi_claude.artifacts.record_handoff", side_effect=OSError("boom")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["handoff", "b", "--from", "a", "--session", SID])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("continue with:", out.getvalue())
        self.assertIn("could not record the handoff", err.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.project_dir(self.dir_b), SID + ".jsonl")))

    def test_r9d_half_line_then_append(self):
        self.seed_log()
        path = os.path.join(self.state, "handoffs.jsonl")
        with open(path, "a") as handle:
            handle.write('{"v": 1, "session_id": "half')
        self.session(self.dir_a, self.publish(URL1))
        self.ok("handoff", "b", "--from", "a", "--session", SID, cwd=self.project)
        result = self.ok("artifacts", "--json")
        self.assertIn("ignored 1 unreadable line", result.err)
        item = {entry["url"]: entry for entry in json.loads(result.out)["artifacts"]}[URL1]
        self.assertEqual((item["accounts"], item["ownership"]), (["a"], "recorded"))

    def test_r10_bad_lines_skipped(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "handoffs.jsonl"), "w") as handle:
            handle.write("garbage\n" + json.dumps({"v": 2, "session_id": SID}) + "\n[]\n")
        self.session(self.dir_a, self.publish(URL1))
        result = self.ok("artifacts")
        self.assertIn("ignored 3 unreadable line", result.err)
        self.assertIn(URL1, result.out)


class CliTest(ArtifactTestCase):
    def test_r11_name_filter(self):
        self.session(self.dir_a, self.publish(URL1))
        self.session(self.dir_b, self.publish(URL2), sid=OTHER)
        shared = self.publish(URL3)
        self.session(self.dir_a, shared, sid="33333333-aaaa-bbbb-cccc-000000000003")
        self.session(self.dir_b, shared, sid="33333333-aaaa-bbbb-cccc-000000000003")
        self.assertEqual(set(self.listing("a")), {URL1, URL3})
        self.assertEqual(set(self.listing("b")), {URL2, URL3})
        result = self.run_cli("artifacts", "nobody")
        self.assertEqual(result.code, 1, result)
        self.assertIn("not registered", result.err)
        self.assertEqual(self.run_cli("artifacts", "--limit", "-1").code, 2)

    def test_r11_grep_fields(self):
        self.session(self.dir_a, self.publish(URL1, title="Quarterly Report", description="numbers",
                                              path="/tmp/scratch/report.html"))
        self.session(self.dir_b, self.publish(URL2, title="Other", cwd=self.tmp), sid=OTHER)
        for needle in ("quarterly", "NUMBERS", "AAAAAAAA", self.project[-6:], "scratch/report", SID[:8]):
            self.assertEqual(set(self.listing("--grep", needle)), {URL1}, needle)

    def test_r11_limit_and_order(self):
        chunks = [self.publish("https://claude.ai/artifact/X{:02d}".format(index),
                               when="2026-10-01T00:{:02d}:00.000Z".format(index)) for index in range(32)]
        self.session(self.dir_a, *chunks)
        out = self.ok("artifacts").out.splitlines()
        self.assertTrue(out[0].startswith("TITLE"))
        self.assertIn("X31", out[1])
        self.assertEqual(len([line for line in out if "claude.ai/artifact/X" in line]), 30)
        self.assertIn("showing 30 of 32 artifacts", out[-1])
        payload = json.loads(self.ok("artifacts", "--json").out)
        self.assertEqual((len(payload["artifacts"]), payload["total"], payload["skipped"]), (32, 32, 0))
        self.assertEqual(len(json.loads(self.ok("artifacts", "--json", "--limit", "2").out)["artifacts"]), 2)

    def test_r11_json_fields(self):
        source = os.path.join(self.tmp, "page.html")
        self.write(source, "<p>")
        self.session(self.dir_a, self.publish(URL1, title="Old", when="2026-10-01T00:00:00.000Z", path=source),
                     self.publish(URL1, title="New", when="2026-10-02T00:00:00.000Z", path=source))
        payload = json.loads(self.ok("artifacts", "--json").out)
        self.assertEqual(payload["schema_version"], 1)
        item = payload["artifacts"][0]
        self.assertEqual(sorted(item), sorted([
            "url", "title", "description", "accounts", "ownership", "first_published", "last_published",
            "publishes", "directory", "project", "session_id", "path", "path_exists", "audience", "resume"]))
        self.assertEqual((item["title"], item["publishes"], item["path_exists"]), ("New", 2, True))
        self.assertEqual(item["first_published"][:10], "2026-10-01")
        self.assertEqual(item["last_published"][:10], "2026-10-02")
        self.assertEqual(item["directory"], self.project)
        self.assertEqual(item["resume"], "cd {} && claude-a --resume {}".format(self.project, SID))

    def test_r11_cmd(self):
        self.session(self.dir_a, self.publish(URL1))
        self.session(self.dir_b, self.publish(URL2, cwd="relative/dir"), sid=OTHER)
        out = self.ok("artifacts", "--cmd").out
        self.assertIn("claude-a --resume " + SID, out)
        self.assertIn("  claude-b --resume {}  (start directory unknown; project".format(OTHER), out)
        self.assertNotIn("--resume", self.ok("artifacts").out)

    def test_path_option(self):
        source = os.path.join(self.tmp, "page.html")
        self.write(source, "<p>")
        self.session(self.dir_a, self.publish(URL1, path=source, when="2026-10-03T00:00:00.000Z"),
                     self.publish(URL2, path="/gone/x.html", when="2026-10-02T00:00:00.000Z"),
                     self.publish(URL3, when="2026-10-01T00:00:00.000Z"))
        lines = self.ok("artifacts", "--path", "--cmd").out.splitlines()
        # 每条下面先是路径、再是续聊命令
        self.assertEqual(lines[2], "  " + source)
        self.assertTrue(lines[3].endswith("--resume " + SID))
        self.assertEqual(lines[5], "  /gone/x.html  (missing)")
        self.assertEqual(lines[8], "  (no source file)")
        self.assertNotIn(source, self.ok("artifacts").out)

    def test_r11_subagent_resumes_parent(self):
        self.write(os.path.join(self.project_dir(self.dir_a), SID, "subagents", "agent-1.jsonl"), self.publish(URL1))
        item = self.listing()[URL1]
        self.assertEqual(item["session_id"], SID)
        self.assertTrue(item["resume"].endswith("--resume " + SID))

    def test_r11_empty_and_no_config(self):
        self.assertIn("no artifacts found", self.ok("artifacts").out)
        os.unlink(os.path.join(self.state, "config.json"))
        default = os.path.join(self.home, ".claude")
        self.session(default, self.publish(URL1))
        item = self.listing()[URL1]
        self.assertEqual(item["accounts"], ["default"])
        self.assertIn("--resume " + SID, item["resume"])
