"""v0.2 方案 §8 第 6、7 条：补全脚本的语法、内容与账号名补全。"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import SRC, CliTestCase

sys.path.insert(0, SRC)
from multi_claude import cli, completion  # noqa: E402

SUBCOMMANDS = ("init", "migrate-default", "add", "proxy", "env", "args", "remove", "apply", "list",
               "usage", "doctor", "route", "which", "rename", "mcp", "completion", "statusline", "handoff", "login", "set",
               "sessions", "artifacts")


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
        proc = subprocess.run(["bash", runner, script, line], env=self.shell_env(), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [item for item in proc.stdout.split("\n") if item]

    def shell_env(self):
        """补全脚本调用 `multi-claude`；用一个转调当前源码的小脚本代替安装版，并只留系统目录在 PATH 里。"""
        self.write(os.path.join(self.fakebin, "multi-claude"),
                   '#!/bin/sh\nexec "{}" -m multi_claude "$@"\n'.format(sys.executable), 0o755)
        directories = [os.path.dirname(sys.executable), self.fakebin, "/usr/bin", "/bin"]
        # zsh / fish 在 Linux 上装在 /usr/bin，在 macOS 上可能在 Homebrew 目录，要能被找到。
        for shell in ("zsh", "fish"):
            found = shutil.which(shell)
            if found:
                directories.append(os.path.dirname(found))
        return self._merged({"PATH": os.pathsep.join(directories)})

    def zsh_complete(self, line):
        """在 zsh 里加载补全脚本，把 compadd 换成逐个打印候选的函数，模拟光标在 line 末尾时按 Tab。"""
        script = self.write(os.path.join(self.tmp, "c.zsh"), self.script("zsh"))
        runner = self.write(os.path.join(self.tmp, "run.zsh"), (
            'compdef() { : }\n'
            'compadd() { [[ "$1" == -- ]] && shift; print -rl -- "$@" }\n'
            '_files() { print -r -- __files__ }\n'
            'source "$1"\n'
            'words=(${(z)2})\n'
            '[[ "$2" == *" " ]] && words+=("")\n'
            'CURRENT=${#words}\n'
            '_multi_claude\n'))
        proc = subprocess.run([shutil.which("zsh"), "-f", runner, script, line], env=self.shell_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [item for item in proc.stdout.split("\n") if item]

    def fish_complete(self, line):
        """用 fish 的 `complete -C` 取补全结果，只留候选本身（去掉制表符后的说明）。"""
        script = self.write(os.path.join(self.tmp, "c.fish"), self.script("fish"))
        env = self.shell_env()
        env.update({"MC_SCRIPT": script, "MC_LINE": line})
        proc = subprocess.run([shutil.which("fish"), "--no-config", "-c", 'source "$MC_SCRIPT"; complete -C "$MC_LINE"'],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [item.split("\t")[0] for item in proc.stdout.split("\n") if item]

    def set_shared_items(self, items):
        path = os.path.join(self.state, "config.json")
        data = self.config_data()
        data.setdefault("shared", {})["items"] = items
        self.write(path, json.dumps(data))

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
        self.assertEqual(self.bash_complete("multi-claude set "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude handoff x --from "), ["a@example.com"])
        self.assertEqual(self.bash_complete("multi-claude sessions "), ["a@example.com"])

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_statusline(self):
        self.write(os.path.join(self.tmp, "settings-one.json"), "{}")
        self.assertEqual(self.bash_complete("multi-claude statusline "), ["install", "uninstall"])
        self.assertEqual(self.bash_complete("multi-claude statusline u"), ["uninstall"])
        prefix = os.path.join(self.tmp, "settings-")
        self.assertEqual(self.bash_complete("multi-claude statusline install " + prefix), [prefix + "one.json"])

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_option_values(self):
        # 方案 feature-completion-values §8 C1–C3、C5、C6。
        self.ok("add", "work")
        for option in ("--clash", "--clash-tun", "--clash-global"):
            self.assertEqual(self.bash_complete("multi-claude netcheck work strict {} ".format(option)), ["on", "off"])
        self.assertEqual(self.bash_complete("multi-claude netcheck work --clash o"), ["on", "off"])
        self.assertEqual(self.bash_complete("multi-claude usage --history --by "), ["day", "model"])
        self.assertEqual(self.bash_complete("multi-claude set work --shared-exclude "),
                         ["agents", "commands", "skills", "CLAUDE.md"])
        self.assertEqual(self.bash_complete("multi-claude add x --shared-include sk"), ["skills"])
        self.assertEqual(self.bash_complete("multi-claude netcheck work --country "), [])
        # 取值之后照常数位置参数：--by day 之后补账号名。
        self.assertEqual(self.bash_complete("multi-claude usage --by day "), ["work"])
        self.assertEqual(self.bash_complete("multi-claude netcheck work "), ["off", "basic", "strict"])
        self.assertEqual(self.bash_complete("multi-claude clash-api "), ["auto", "off"])
        self.assertEqual(self.bash_complete("multi-claude completion "), list(completion.SHELLS))

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_shared_items_are_filtered(self):
        # C4：名字里有空白、冒号、$ 或以 - 开头时，写进候选会被拆开、展开或当成选项，不输出。
        self.ok("add", "work")
        self.set_shared_items(["skills", "a b", "x:y", "$HOME", "-x", "rules"])
        self.assertEqual(self.bash_complete("multi-claude set work --shared-exclude "), ["skills", "rules"])

    def test_list_shared_items(self):
        # C7：没有配置文件时输出默认共享项；配置损坏时输出空、退出 0；与 --names 互斥。
        self.assertEqual(self.ok("list", "--shared-items").out.split(), ["agents", "commands", "skills", "CLAUDE.md"])
        self.ok("add", "work")
        self.set_shared_items(["skills", "$(touch x)", "rules"])
        result = self.ok("list", "--shared-items")
        self.assertEqual(result.out.split(), ["skills", "rules"])
        self.assertEqual(result.err, "")
        self.write(os.path.join(self.state, "config.json"), "{broken")
        result = self.ok("list", "--shared-items")
        self.assertEqual((result.out, result.err), ("", ""))
        self.assertEqual(self.run_cli("list", "--shared-items", "--names").code, 2)
        self.assertNotIn("--shared-items", self.ok("list", "--help").out)

    def test_value_spec_reads_choices(self):
        # C8：argparse 里新加带 choices 的位置参数与选项，四种脚本都自动带上取值，不用改补全表。
        parser = argparse.ArgumentParser(prog="multi-claude")
        sub = parser.add_subparsers(dest="command")
        demo = sub.add_parser("demo-cmd")
        demo.add_argument("mode", choices=("alpha", "beta"))
        demo.add_argument("--color", choices=("red", "green"))
        spec = completion.value_spec(parser)
        self.assertEqual(spec.kinds["demo-cmd"], ["fixed"])
        self.assertEqual(spec.positional[("demo-cmd", 0)], ["alpha", "beta"])
        self.assertEqual(spec.option_values[("demo-cmd", "--color")], ["red", "green"])
        for shell in completion.SHELLS:
            with self.subTest(shell=shell):
                text = completion.render(shell, parser)
                for word in ("alpha", "beta", "red", "green"):
                    self.assertIn(word, text)

    def test_value_spec_rejects_bad_values(self):
        # C9：写不进脚本的取值、可重复的带取值位置、类型表冲突，都在生成时报错。
        cases = [
            lambda p: p.add_argument("mode", choices=("-x", "y")),
            lambda p: p.add_argument("mode", choices=("a b",)),
            lambda p: p.add_argument("--color", choices=("$HOME",)),
            lambda p: p.add_argument("modes", nargs="*", choices=("a", "b")),
        ]
        for index, add in enumerate(cases):
            with self.subTest(case=index):
                parser = argparse.ArgumentParser(prog="multi-claude")
                add(parser.add_subparsers(dest="command").add_parser("demo-cmd"))
                with self.assertRaises(ValueError):
                    completion.value_spec(parser)
        # remove 的第 0 个位置在类型表里是账号名，加上 choices 就冲突。
        parser = argparse.ArgumentParser(prog="multi-claude")
        parser.add_subparsers(dest="command").add_parser("remove").add_argument("name", choices=("a",))
        with self.assertRaises(ValueError):
            completion.value_spec(parser)

    def test_value_spec_matches_cli(self):
        # C6：从 argparse 读出的取值与原手写表一致（词与顺序都不变）。
        spec = completion.value_spec(cli.build_parser())
        self.assertEqual(spec.positional[("completion", 0)], list(completion.SHELLS))
        self.assertEqual(spec.positional[("statusline", 0)], ["install", "uninstall"])
        self.assertEqual(spec.positional[("netcheck", 1)], ["off", "basic", "strict"])
        self.assertEqual(spec.positional[("proxy", 1)], ["off", "inherit"])
        self.assertEqual(spec.positional[("clash-api", 0)], ["auto", "off"])
        self.assertEqual(spec.option_values[("usage", "--by")], ["day", "model"])

    def test_hidden_option_not_offered(self):
        # C10：隐藏选项不作为选项名出现在任何脚本里，只出现在 list --shared-items 的调用里。
        # init 另有一个公开的 --shared-items（逗号列表），所以按 list 自己的选项表核。
        self.assertNotIn("--shared-items", completion.collect(cli.build_parser())["list"].options)
        lines = {"bash": "    list) opts=", "zsh": "    list) opts=", "powershell": "        'list' = @(",
                 "fish": "__fish_seen_subcommand_from list' -l shared-items"}
        for shell in completion.SHELLS:
            with self.subTest(shell=shell):
                text = self.script(shell)
                if shell == "fish":
                    self.assertNotIn(lines[shell], text)
                    # ubuntu-22.04 的旧 fish 不支持 $(...)。
                    self.assertNotIn("$(", text)
                    continue
                list_lines = [line for line in text.splitlines() if line.startswith(lines[shell])]
                self.assertEqual(len(list_lines), 1 if shell != "powershell" else 2, text)
                for line in list_lines:
                    self.assertNotIn("--shared-items", line)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_zsh_option_values(self):
        self.ok("add", "work")
        # 候选必须是两个词，不能是一个 "on off"。
        self.assertEqual(self.zsh_complete("multi-claude netcheck work strict --clash "), ["on", "off"])
        self.assertEqual(self.zsh_complete("multi-claude usage --history --by "), ["day", "model"])
        self.assertEqual(self.zsh_complete("multi-claude set work --shared-exclude "),
                         ["agents", "commands", "skills", "CLAUDE.md"])
        self.assertEqual(self.zsh_complete("multi-claude completion "), list(completion.SHELLS))
        self.assertEqual(self.zsh_complete("multi-claude proxy work "), ["off", "inherit"])
        self.assertEqual(self.zsh_complete("multi-claude proxy "), ["work"])
        self.assertEqual(self.zsh_complete("multi-claude netcheck work --country "), [])

    @unittest.skipUnless(shutil.which("fish"), "fish is not installed")
    def test_fish_option_values(self):
        self.ok("add", "work")
        self.assertEqual(sorted(self.fish_complete("multi-claude netcheck work strict --clash ")), ["off", "on"])
        self.assertEqual(sorted(self.fish_complete("multi-claude netcheck work ")), ["basic", "off", "strict"])
        # 补选项的值时不混进位置参数的账号名。
        self.assertEqual(sorted(self.fish_complete("multi-claude usage --history --by ")), ["day", "model"])
        self.assertEqual(sorted(self.fish_complete("multi-claude set work --shared-exclude ")),
                         sorted(["agents", "commands", "skills", "CLAUDE.md"]))
        self.assertEqual(self.fish_complete("multi-claude proxy "), ["work"])

    def test_completion_ignores_broken_state(self):
        self.write(os.path.join(self.state, "migrate-journal.json"), "{broken")
        self.write(os.path.join(self.state, "config.json"), "{broken")
        self.ok("completion", "bash")


if __name__ == "__main__":
    unittest.main()
