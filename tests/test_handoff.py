"""方案 feature-session-handoff §8：会话交接。

“在 Claude 会话里运行”用 Claude 传给子进程的环境变量模拟：CLAUDECODE、CLAUDE_CODE_SESSION_ID、
CLAUDE_CONFIG_DIR（方案 §3，2.1.286 实测）。
"""

import json
import os
import stat
import sys
import time

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)

from multi_claude import sessions  # noqa: E402

SID = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER = "22222222-aaaa-bbbb-cccc-000000000002"


def lines(*texts):
    return "".join(json.dumps({"type": "user", "sessionId": SID, "message": text}) + "\n" for text in texts).encode()


class HandoffTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ok("add", "a")
        self.ok("add", "b")
        self.dir_a = os.path.join(self.root, "a")
        self.dir_b = os.path.join(self.root, "b")
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)
        self.encoded = sessions.encode(os.path.realpath(self.project))

    def make_session(self, account_dir, sid=SID, content=None, extra=True, encoded=None, mtime=None):
        directory = os.path.join(account_dir, "projects", encoded or self.encoded)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, sid + ".jsonl")
        with open(path, "wb") as handle:
            handle.write(lines("hello", "world") if content is None else content)
        os.chmod(path, 0o600)
        if extra:
            self.write(os.path.join(directory, sid, "subagents", "agent-1.jsonl"), "sub\n")
            self.write(os.path.join(directory, sid, "tool-results", "r1.txt"), "big output")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return directory

    def in_session(self, account_dir=None, sid=SID, **extra):
        env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": sid, "PWD": self.project}
        env["CLAUDE_CONFIG_DIR"] = account_dir if account_dir is not None else self.dir_a
        env.update(extra)
        return env

    def handoff(self, *args, env=None, cwd=None):
        return self.run_cli("handoff", *args, env=env, cwd=cwd or self.project)

    def target_dir(self, encoded=None):
        return os.path.join(self.dir_b, "projects", encoded or self.encoded)


class CopyTest(HandoffTestCase):
    def test_t1_current_session(self):
        source = self.make_session(self.dir_a)
        self.make_session(self.dir_a, sid=OTHER, extra=False, mtime=time.time() + 100)
        before = self.snapshot(self.dir_a)
        result = self.handoff("b", env=self.in_session())
        self.assertEqual(result.code, 0, result)
        target = self.target_dir()
        with open(os.path.join(target, SID + ".jsonl"), "rb") as handle:
            self.assertEqual(handle.read(), lines("hello", "world"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(target, SID + ".jsonl")).st_mode), 0o600)
        self.assertEqual(sessions.build_manifest(os.path.join(source, SID))[0],
                         sessions.build_manifest(os.path.join(target, SID))[0])
        # 当前会话优先于修改时间更新的另一条会话。
        self.assertFalse(os.path.exists(os.path.join(target, OTHER + ".jsonl")))
        self.assertIn("continue with: cd {} && claude-b --resume {}".format(
            os.path.realpath(self.project), SID), result.out)
        self.assertEqual(self.snapshot(self.dir_a), before)
        self.assertEqual([name for name in os.listdir(target) if name.startswith(".")], [])

    def test_t2_default_identity_session(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        self.ok("migrate-default", "main")
        main_dir = os.path.join(self.root, "main")
        self.make_session(main_dir)
        env = self.in_session()
        del env["CLAUDE_CONFIG_DIR"]
        result = self.handoff("b", env=env)
        self.assertEqual(result.code, 0, result)
        self.assertIn("from main to b", result.out)

    def test_t3_plain_terminal(self):
        self.make_session(self.dir_a, sid=OTHER, extra=False, mtime=time.time() - 100)
        self.make_session(self.dir_a, extra=False)
        result = self.handoff("b")
        self.assertEqual(result.code, 2, result)
        self.assertIn("--from", result.err)
        result = self.handoff("b", "--from", "a")
        self.assertEqual(result.code, 0, result)
        self.assertTrue(os.path.exists(os.path.join(self.target_dir(), SID + ".jsonl")))
        self.assertFalse(os.path.exists(os.path.join(self.target_dir(), OTHER + ".jsonl")))

    def test_t4_explicit_session(self):
        self.make_session(self.dir_a, extra=False)
        self.make_session(self.dir_a, sid=OTHER, extra=False, mtime=time.time() - 100)
        self.assertEqual(self.handoff("b", "--from", "a", "--session", OTHER).code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.target_dir(), OTHER + ".jsonl")))
        before = self.snapshot(self.dir_b)
        self.assertEqual(self.handoff("b", "--from", "a", "--session", "../x").code, 2)
        self.assertEqual(self.handoff("b", "--from", "a", "--session", "nope").code, 1)
        self.assertEqual(self.snapshot(self.dir_b), before)

    def test_t5_partial_last_line(self):
        self.make_session(self.dir_a, content=lines("done") + b'{"type": "user", "mess', extra=False)
        self.assertEqual(self.handoff("b", env=self.in_session()).code, 0)
        with open(os.path.join(self.target_dir(), SID + ".jsonl"), "rb") as handle:
            self.assertEqual(handle.read(), lines("done"))
        self.make_session(self.dir_a, sid=OTHER, content=b'{"half', extra=False)
        result = self.handoff("b", env=self.in_session(sid=OTHER), cwd=self.project)
        self.assertEqual(result.code, 1, result)
        self.assertIn("no complete lines", result.err)


class ConflictTest(HandoffTestCase):
    def test_t6_unchanged(self):
        self.make_session(self.dir_a)
        self.assertEqual(self.handoff("b", env=self.in_session()).code, 0)
        before = self.snapshot(self.dir_b)
        result = self.handoff("b", env=self.in_session())
        self.assertEqual(result.code, 0, result)
        self.assertIn("(unchanged)", result.out)
        self.assertIn("continue with:", result.out)
        self.assertEqual(self.snapshot(self.dir_b), before)

    def test_t7_conflict_and_force(self):
        self.make_session(self.dir_a)
        self.assertEqual(self.handoff("b", env=self.in_session()).code, 0)
        self.make_session(self.dir_a, content=lines("hello", "world", "more"))
        before = self.snapshot(self.dir_b)
        result = self.handoff("b", env=self.in_session())
        self.assertEqual(result.code, 3, result)
        self.assertIn("--force", result.err)
        self.assertEqual(self.snapshot(self.dir_b), before)
        result = self.handoff("b", "--force", env=self.in_session())
        self.assertEqual(result.code, 0, result)
        target = self.target_dir()
        with open(os.path.join(target, SID + ".jsonl"), "rb") as handle:
            self.assertEqual(handle.read(), lines("hello", "world", "more"))
        names = sorted(os.listdir(target))
        backups = [name for name in names if ".multi-claude-bak." in name]
        self.assertEqual(len(backups), 2, names)
        self.assertTrue(any(name.startswith(SID + ".jsonl.multi-claude-bak.") for name in backups))
        self.assertFalse(any(name.endswith(".jsonl") and ".multi-claude-bak." in name for name in names))
        self.assertTrue(os.path.isdir(os.path.join(target, SID, "subagents")))

    def test_t7_extra_dir_differs(self):
        self.make_session(self.dir_a)
        self.assertEqual(self.handoff("b", env=self.in_session()).code, 0)
        self.write(os.path.join(self.target_dir(), SID, "tool-results", "r1.txt"), "changed")
        self.assertEqual(self.handoff("b", env=self.in_session()).code, 3)

    def test_t8_dry_run(self):
        self.make_session(self.dir_a)
        before = self.snapshot(self.dir_b)
        result = self.handoff("b", "--dry-run", env=self.in_session())
        self.assertEqual(result.code, 0, result)
        self.assertIn("would copy", result.out)
        self.assertEqual(self.snapshot(self.dir_b), before)
        self.assertEqual(self.handoff("b", env=self.in_session()).code, 0)
        self.make_session(self.dir_a, content=lines("changed"))
        before = self.snapshot(self.dir_b)
        self.assertEqual(self.handoff("b", "--dry-run", env=self.in_session()).code, 3)
        result = self.handoff("b", "--dry-run", "--force", env=self.in_session())
        self.assertEqual(result.code, 0, result)
        self.assertIn("backing up", result.out)
        self.assertEqual(self.snapshot(self.dir_b), before)


class ErrorTest(HandoffTestCase):
    def test_t9_errors(self):
        self.make_session(self.dir_a)
        before = self.snapshot(self.home)
        self.assertEqual(self.handoff("nobody", env=self.in_session()).code, 1)
        self.assertEqual(self.handoff("a", env=self.in_session()).code, 2)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        result = self.handoff("b", env=self.in_session(PWD=other), cwd=other)
        self.assertEqual(result.code, 1, result)
        self.assertIn("no sessions for", result.err)
        empty = self.make_session(self.dir_a, encoded=sessions.encode(os.path.realpath(other)), extra=False)
        os.unlink(os.path.join(empty, SID + ".jsonl"))
        result = self.handoff("b", env=self.in_session(PWD=other), cwd=other)
        self.assertEqual(result.code, 1, result)
        self.assertIn("no sessions in", result.err)
        os.rmdir(empty)
        self.assertEqual(self.snapshot(self.home), before)
        self.write(os.path.join(self.state, "migrate-journal.json"), "{}")
        result = self.handoff("b", env=self.in_session())
        self.assertEqual(result.code, 1, result)
        self.assertIn("unfinished migration", result.err)
        self.assertFalse(os.path.exists(self.target_dir()))

    def test_t10_symlinked_cwd(self):
        link = os.path.join(self.tmp, "link-to-proj")
        os.symlink(self.project, link)
        # 会话目录按 $PWD（软链写法）编码，与物理路径不同。
        self.make_session(self.dir_a, encoded=sessions.encode(link), extra=False)
        result = self.handoff("b", env=self.in_session(PWD=link), cwd=link)
        self.assertEqual(result.code, 0, result)
        self.assertTrue(os.path.exists(os.path.join(self.target_dir(sessions.encode(link)), SID + ".jsonl")))
        # 续聊命令要 cd 到软链写法，目标账号才会按同一目录名找到会话。
        self.assertIn("cd {} && claude-b --resume".format(link), result.out)

    def test_stale_staging_is_replaced(self):
        self.make_session(self.dir_a)
        self.write(os.path.join(self.target_dir(), ".multi-claude-{}.tmp".format(SID), "junk"), "x")
        result = self.handoff("b", env=self.in_session())
        self.assertEqual(result.code, 0, result)
        self.assertEqual([name for name in os.listdir(self.target_dir()) if name.startswith(".")], [])

    def test_t11_long_path(self):
        deep = os.path.join(self.project, *(["d" * 40] * 6))
        os.makedirs(deep)
        encoded = sessions.encode(os.path.realpath(deep))
        self.assertGreater(len(encoded), sessions.ENCODED_LIMIT)
        name = encoded[:sessions.ENCODED_LIMIT] + "-abc123"
        self.make_session(self.dir_a, encoded=name, extra=False)
        result = self.handoff("b", env=self.in_session(PWD=deep), cwd=deep)
        self.assertEqual(result.code, 0, result)
        self.assertTrue(os.path.exists(os.path.join(self.dir_b, "projects", name, SID + ".jsonl")))
        self.make_session(self.dir_a, encoded=encoded[:sessions.ENCODED_LIMIT] + "-zzz", extra=False)
        result = self.handoff("b", env=self.in_session(PWD=deep), cwd=deep)
        self.assertEqual(result.code, 1, result)
        self.assertIn("several session directories", result.err)
