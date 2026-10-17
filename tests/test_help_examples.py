"""方案 feature-help-examples §8：每个子命令的帮助都有示例，示例能解析、能运行、覆盖全部选项。"""

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import sys
import unittest

from helpers import CliTestCase, SRC

sys.path.insert(0, SRC)

from multi_claude import cli  # noqa: E402

# 示例里出现这些 shell 运算符时，之后的词交给 shell，不是 multi-claude 的参数。
SHELL_OPERATORS = (">", ">>", "|")

# E3：这两个命令的 --verbose 是自己的功能；其余命令的 --verbose 来自 _add_write_options，是公共写选项，不要求示例覆盖。
OWN_VERBOSE = ("list", "doctor")

# E5 跳过的命令及理由（方案 §8.1 第 5 条）；它们的示例仍由 E2 解析核对。
RUN_SKIPPED = {
    "list": "looks up plans over the network; its examples are parsed by test_renewal T8",
    "handoff": "needs a Claude session, or a real session in the source account",
    "migrate-default": "needs a real ~/.claude and the login e-mail in ~/.claude.json",
    "restore": "needs an account migrated from ~/.claude",
    "code": "would start the real VS Code found in PATH; covered by test_vscode with a fake code",
}

# E5 允许的非零退出码：示例 → (退出码, 原因)。只收沙箱环境造成的失败，退出码 2（参数被拒）一律不允许。
ALLOWED_EXIT = {}


def subparsers():
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("no subparsers")


def help_text(name):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            cli.build_parser().parse_args([name, "--help"])
        except SystemExit as exc:
            if exc.code != 0:
                raise AssertionError("{} --help exited {}".format(name, exc.code))
    return out.getvalue()


def example_commands(text):
    """帮助文本 Examples: 段里每行的命令列（与说明之间至少两个空格）。"""
    if "Examples:" not in text:
        return []
    commands = []
    for line in text.split("Examples:", 1)[1].splitlines()[1:]:
        if not line.startswith("  "):
            break
        commands.append(re.split(r"\s{2,}", line.strip())[0])
    return commands


def example_words(command):
    """示例命令切成 multi-claude 之后的参数：去掉会话里运行用的 `! `，截掉 shell 运算符之后的部分。"""
    words = shlex.split(command)
    if words[:1] == ["!"]:
        words = words[1:]
    if words[:1] != ["multi-claude"]:
        raise AssertionError("example does not start with multi-claude: {}".format(command))
    words = words[1:]
    for index, word in enumerate(words):
        if word in SHELL_OPERATORS:
            return words[:index]
    return words


def parse_like_main(words):
    """走 main 在 argparse 之前的同一串步骤（方案 §5.1.3），返回解析结果；--help 示例返回 None。"""
    parser = cli.build_parser()
    argv = cli._resolve_command_alias(list(words))
    problem = cli._unknown_command(parser, argv) or cli._misplaced_account_name(argv)
    if problem is not None:
        raise AssertionError(problem)
    argv, _launch_args = cli._split_args_command(argv)
    if "--help" in argv or "-h" in argv:
        return None
    return parser.parse_args(argv)


class HelpExamplesTest(unittest.TestCase):
    def test_e1_every_subcommand_has_examples(self):
        names = list(subparsers())
        self.assertEqual(len(names), 28)
        for name in names:
            with self.subTest(command=name):
                self.assertTrue(example_commands(help_text(name)))

    def test_e2_examples_parse_like_main(self):
        for name in subparsers():
            for command in example_commands(help_text(name)):
                with self.subTest(command=command):
                    args = parse_like_main(example_words(command))
                    if args is not None:
                        # 防止把别的命令的示例贴进这个命令的帮助。
                        self.assertEqual(args.command, name)

    def test_e3_examples_cover_every_option_and_choice(self):
        for name, parser in subparsers().items():
            with self.subTest(command=name):
                # 按整词比较：子串匹配会让 --shared-exclude 冒充 --shared 已覆盖。
                used = {word for command in example_commands(help_text(name)) for word in example_words(command)}
                for action in parser._actions:
                    if "-h" in action.option_strings:
                        continue
                    if action.option_strings == ["--verbose"] and name not in OWN_VERBOSE:
                        continue
                    if action.option_strings:
                        self.assertTrue(set(action.option_strings) & used,
                                        "{} {} has no example".format(name, action.option_strings))
                    for choice in action.choices or ():
                        self.assertIn(choice, used, "{} value {!r} has no example".format(name, choice))

    def test_e4_descriptions_and_example_columns(self):
        for name, parser in subparsers().items():
            with self.subTest(command=name):
                for line in (parser.description or "").splitlines():
                    self.assertLessEqual(len(line), 80)
                lines = help_text(name).split("Examples:", 1)[1].splitlines()[1:]
                lines = lines[:next((i for i, line in enumerate(lines) if not line.startswith("  ")), len(lines))]
                # 说明列对齐、且与最长的命令至少隔两个空格；只看拆分结果查不出“只隔一个空格”。
                widest = max(len(re.split(r"\s{2,}", line.strip())[0]) for line in lines)
                starts = {len(line) - len(re.split(r"\s{2,}", line.strip())[-1]) for line in lines
                          if re.search(r"\S\s{2,}\S", line)}
                self.assertEqual(len(starts), 1, lines)
                self.assertGreaterEqual(starts.pop(), 2 + widest + 2, lines)

    def test_e6_existing_help_untouched(self):
        # add、set、list 与主帮助的示例在本方案之前就有，统一挂示例的循环不得改动它们。
        self.assertFalse({"add", "set", "list"} & set(cli._SUBCOMMAND_EXAMPLES))
        parsers = subparsers()
        expected = {
            "add": "\n".join(cli._examples_lines(cli._ADD_EXAMPLES)),
            "set": "\n".join(cli._examples_lines(cli._SET_EXAMPLES)),
            "list": "\n".join(cli._examples_lines(cli._LIST_EXAMPLES) + [""] + cli._RENEWS_LEGEND),
        }
        for name, epilog in expected.items():
            with self.subTest(command=name):
                self.assertEqual(parsers[name].epilog, epilog)
                self.assertIs(parsers[name].formatter_class, argparse.RawDescriptionHelpFormatter)
        self.assertEqual(parsers["add"].description,
                         "add an account, adopt an existing directory, or change its options")
        self.assertEqual(parsers["set"].description, "change an existing account: proxy, sharing and renewal date\n"
                                                     "(use env, args, rename for the rest)")
        self.assertEqual(parsers["list"].description, "show accounts and their status; looks up each account's plan\n"
                                                      "and billing channel from Anthropic unless --offline is given")
        main = cli.build_parser()
        self.assertEqual(main.epilog, cli._help_epilog())
        self.assertEqual(main.description,
                         "Manage multiple Claude Code accounts: separate CLAUDE_CONFIG_DIR directories,\n"
                         "per-account launchers, proxies, environment variables and arguments.")
        self.assertIs(main.formatter_class, argparse.RawDescriptionHelpFormatter)


class HelpExamplesRunTest(CliTestCase):
    """E5（方案 §8.1）：在沙箱里以子进程真正运行示例，确认照抄能用，不只是能解析。"""

    def prepare(self, name):
        self.ok("add", "work")
        self.ok("add", "home")
        os.makedirs(os.path.join(self.home, "work"))
        if name == "run":
            # 不给账号名的 `run -- env` 要靠路由或默认账号选账号，否则以用法错误退出。
            self.ok("route", "--default", "home")
        elif name == "import":
            # 只在 import 沙箱预先导出：export 沙箱若也有这个文件，export 示例会因目标已存在而失败。
            self.ok("export", "work", os.path.join(self.home, "work-settings.tar.gz"))
        elif name == "apply":
            shutil.copy(os.path.join(self.state, "config.json"), os.path.join(self.home, "accounts.json"))
        elif name == "add":
            # add work --config-from home 要从 home 复制 settings.json。
            self.write(os.path.join(self.root, "home", "settings.json"), "{}")
        elif name == "statusline":
            # install 要在 PATH 里找到 multi-claude；install 与 uninstall 都要读一份带 command 类型 statusLine 的设置文件。
            fake = self.write(os.path.join(self.fakebin, "multi-claude"), "#!/bin/sh\nexit 0\n")
            os.chmod(fake, 0o755)
            self.write(os.path.join(self.home, ".claude", "settings.json"),
                       json.dumps({"statusLine": {"type": "command", "command": "echo usage"}}))

    def run_example(self, command):
        # shlex 不展开 ~，multi-claude 的部分命令也不展开，这里按 shell 的规则展开到沙箱的 home。
        words = [self.home + word[1:] if word == "~" or word.startswith("~/") else word
                 for word in example_words(command)]
        return self.run_cli(*words, cwd=self.home)

    def check_examples_run(self, name):
        commands = example_commands(help_text(name))
        if name != "statusline":
            # 先跑预览：rename / remove 真改之后，同命令的 --dry-run 示例会找不到账号。
            # statusline 例外，按顺序跑，uninstall 的预览才是 install 之后的还原。
            commands = [c for c in commands if "--dry-run" in c] + [c for c in commands if "--dry-run" not in c]
        self.prepare(name)
        for command in commands:
            with self.subTest(command=command):
                result = self.run_example(command)
                expected = ALLOWED_EXIT.get(command, (0, ""))[0]
                self.assertNotEqual(result.code, 2, result)
                self.assertEqual(result.code, expected, result)


# 每个子命令一个测试方法，各自一个新沙箱：前一个命令的示例不会改掉后一个命令依赖的账号与文件。
for _name in subparsers():
    if _name not in RUN_SKIPPED:
        setattr(HelpExamplesRunTest, "test_e5_" + _name.replace("-", "_"),
                lambda self, name=_name: self.check_examples_run(name))

if __name__ == "__main__":
    unittest.main()
