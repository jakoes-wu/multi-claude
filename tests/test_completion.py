"""v0.2 方案 §8 第 6、7 条：补全脚本的语法、内容与账号名补全。"""

import os
import shutil
import subprocess
import sys
import unittest

from helpers import CliTestCase

SUBCOMMANDS = ("init", "migrate-default", "add", "proxy", "env", "args", "remove", "apply", "list",
               "usage", "doctor", "route", "which", "rename", "mcp", "completion", "statusline", "handoff", "login")


class CompletionTest(CliTestCase):
    def script(self, shell):
        return self.ok("completion", shell).out

    def test_scripts_contain_subcommands(self):
        for shell in ("bash", "zsh", "fish"):
            with self.subTest(shell=shell):
                text = self.script(shell)
                for command in SUBCOMMANDS:
                    self.assertIn(command, text)
                self.assertIn("multi-claude list --names", text)
                self.assertIn("install uninstall", text)
                # 钩子是内部入口，不应出现在补全候选里。
                self.assertNotIn("statusline-hook", text)

    def test_syntax(self):
        checks = {"bash": ["bash", "-n"], "zsh": ["zsh", "-n"], "fish": ["fish", "--no-execute"]}
        for shell, command in checks.items():
            with self.subTest(shell=shell):
                if not shutil.which(command[0]):
                    self.skipTest("{} is not installed".format(command[0]))
                path = self.write(os.path.join(self.tmp, "c." + shell), self.script(shell))
                proc = subprocess.run(command + [path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def bash_complete(self, line):
        """在 bash 里加载补全脚本，模拟光标位于 line 末尾时按 Tab，返回 COMPREPLY。"""
        script = self.write(os.path.join(self.tmp, "c.bash"), self.script("bash"))
        runner = self.write(os.path.join(self.tmp, "run.bash"), (
            'source "$1"\n'
            'COMP_LINE="$2"; COMP_POINT=${#COMP_LINE}\n'
            'COMP_WORDS=($COMP_LINE); COMP_CWORD=${#COMP_WORDS[@]}\n'
            '_multi_claude\n'
            'printf "%s\\n" "${COMPREPLY[@]}"\n'))
        env = self._merged({"PATH": os.pathsep.join([os.path.dirname(sys.executable), self.fakebin,
                                                    "/usr/bin", "/bin"])})
        # 补全脚本调用 `multi-claude`；用一个转调当前源码的小脚本代替安装版。
        self.write(os.path.join(self.fakebin, "multi-claude"),
                   '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        proc = subprocess.run(["bash", runner, script, line], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [item for item in proc.stdout.split("\n") if item]

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_account_names_with_at_sign(self):
        self.ok("add", "a@example.com")
        self.ok("add", "b+x@example.com")
        self.assertEqual(sorted(self.bash_complete("multi-claude proxy ")), ["a@example.com", "b+x@example.com"])
        # 光标在 @ 之后时，bash 只替换 @ 后的片段，候选要去掉 "a@"。
        self.assertEqual(self.bash_complete("multi-claude proxy a@ex"), ["example.com"])
        self.assertEqual(self.bash_complete("multi-claude proxy a@example.com o"), ["off"])
        self.assertIn("usage", self.bash_complete("multi-claude us"))
        self.assertEqual(self.bash_complete("multi-claude completion z"), ["zsh"])
        self.assertEqual(self.bash_complete("multi-claude args a@example.com -- "), [])
        self.assertIn("--proxy", self.bash_complete("multi-claude add x --pro"))

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_route_and_which(self):
        self.ok("add", "a@example.com")
        os.makedirs(os.path.join(self.tmp, "proj-one"))
        prefix = os.path.join(self.tmp, "proj-")
        self.assertEqual(self.bash_complete("multi-claude route " + prefix), [prefix + "one"])
        self.assertEqual(self.bash_complete("multi-claude which " + prefix), [prefix + "one"])
        self.assertEqual(self.bash_complete("multi-claude route /x "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude route --default "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude remove "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude rename "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude mcp "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude handoff "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude login "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude handoff x --from "), ["a@example.com"])

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_statusline(self):
        self.write(os.path.join(self.tmp, "settings-one.json"), "{}")
        self.assertEqual(self.bash_complete("multi-claude statusline "), ["install", "uninstall"])
        self.assertEqual(self.bash_complete("multi-claude statusline u"), ["uninstall"])
        prefix = os.path.join(self.tmp, "settings-")
        self.assertEqual(self.bash_complete("multi-claude statusline install " + prefix), [prefix + "one.json"])

    def test_completion_ignores_broken_state(self):
        self.write(os.path.join(self.state, "migrate-journal.json"), "{broken")
        self.write(os.path.join(self.state, "config.json"), "{broken")
        self.ok("completion", "bash")


if __name__ == "__main__":
    unittest.main()
