# 每个子命令的帮助都附示例

> 2026-10-17 落地注记：代码已落地。`_attach_examples`（`src/multi_claude/cli.py:114`）、`_SUBCOMMAND_EXAMPLES`（`cli.py:165`）、`build_parser` 末尾挂示例的循环（`cli.py:517-519`）；测试 `tests/test_help_examples.py`（E1–E4、E6 与 23 个子命令各一个实跑测试）。代码评审 1 轮可合并（0 高 0 中 4 低，低 1–3 已修，低 4 不改）；本机与编译机（Python 3.8.10）全量 540 项通过。
>
> 2026-10-17 注记（评审过程）：第 1 轮 0 高 4 中 4 低，已修订（D1–D7；D8 未采纳，理由见 §8.1 第 5 条）；第 2 轮 0 高 2 中 2 低，已修订（D9–D12），并用原型实跑核过（§8.1 第 7 条）；第 3 轮 0 高 0 中 5 低，收敛，低级 D13–D17 已修订。用户 2026-10-17 指示“评审通过后，直接开始编码”。基线：分支 `feature/help-examples`，基于 `origin/main` `9524d23`（v0.13.0）；下文行号都是这个提交的锚点。

## 1. 背景

主帮助 `multi-claude --help` 末尾有 `Examples:` 段（`src/multi_claude/cli.py:73` `_EXAMPLES`，由 `_help_epilog` `:92` 拼出）。v0.13.0 起 `add`、`set`、`list` 三个子命令的帮助也有示例（`_ADD_EXAMPLES` `:127`、`_SET_EXAMPLES` `:114`、`_LIST_EXAMPLES` `:139`，挂在 `:190`、`:198`、`:230` 的 `epilog` 上），写法见 `docs/feature/feature-renewal-date.md` §5.1.4。

其余 25 个子命令（`init`、`migrate-default`、`proxy`、`env`、`args`、`remove`、`apply`、`usage`、`doctor`、`route`、`rename`、`mcp`、`login`、`which`、`run`、`path`、`code`、`restore`、`export`、`import`、`completion`、`statusline`、`handoff`、`sessions`、`artifacts`）的 `add_parser` 都没有 `epilog`（`:167-372`），`<子命令> --help` 只有一句说明和选项列表。

2026-10-16 用户要求“在 `--help` 中显示所有的示例用法”，并决定单列为一个任务。

## 2. 目标 / 非目标

目标：

1. 上面 25 个子命令的 `--help` 末尾都有 `Examples:` 段，格式与 `add` / `set` / `list` 相同（`_examples_lines` `:103`）。
2. 每个子命令的示例合起来用到该命令的每个选项、每个固定取值（`choices`），照抄即可运行，不会因参数组合不合法被拒（退出码 2）。
3. 测试遍历全部 28 个子命令，核对第 1、2 条，以后新增子命令或选项而忘了补示例时测试失败。

非目标：

- 不改 `add`、`set`、`list` 已有的示例内容，不改主帮助的分组与示例。
- 不改任何选项的名字、默认值、`help=` 文字和命令行为。
- 不限制示例行的总宽度（`add` / `set` 已有行超过 80 列，续费日代码评审低 2 已决定不改）。
- 不给 shell 补全加示例（补全脚本不读 `description` / `epilog`，见 §7）。

## 3. 假设与约束

- argparse 在打印帮助时才读解析器的 `description`、`epilog` 与 `formatter_class`：`format_help` 调 `self._get_formatter()`，后者返回 `self.formatter_class(prog=self.prog)`，再依次 `add_text(self.description)`、`add_text(self.epilog)`（本机 Python 3.11.9 `argparse.py:2574-2598`）。所以子解析器建好之后再设置这三个属性同样生效。项目支持 Python 3.8+，3.8 / 3.10 / 3.12 由 CI 矩阵（`.github/workflows` 的 test 任务）覆盖，编译机 Python 3.8.10 再跑一次。
- `RawDescriptionHelpFormatter` 对 `description` 与 `epilog` 都原样输出、不再自动折行（同文件 `:679-687` 的 `_fill_text`）。所以加了示例的子命令，说明文字要由我们自己折行。
- 示例里的账号名统一用 `work`、`home`，目录用 `~/work`，与 README 已有示例一致；会话号用 README 里出现过的 `3c588340-1219-48df-a231-509866789487`。
- 示例里的 `claude` / VS Code 参数只用 README 已出现过的写法（`claude --version`、`mcp ... list`、`mcp ... add --scope user github -- npx -y @modelcontextprotocol/server-github`、`--ide`、`--disable-gpu`），不新造第三方参数。

## 4. 涉及模块

| 区域 | 行号锚点（9524d23） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/cli.py` 导入 | `:17` `import sys` 与 `:18` `import unicodedata` 之间 | 新增 | `import textwrap` |
| `src/multi_claude/cli.py` 示例常量 | `:153` `_RENEWS_LEGEND` 之后 | 新增 | `_SUBCOMMAND_EXAMPLES`：子命令名 → 示例列表（§5.1.2） |
| `src/multi_claude/cli.py` `build_parser` | `:374` `return parser` 之前 | 新增 | 循环给 25 个子解析器挂示例、换格式器、折行说明（§5.1.1） |
| `src/multi_claude/cli.py` 新函数 | `_examples_lines` `:103-111` 之后 | 新增 | `_attach_examples(parser, examples)` |
| `tests/test_help_examples.py` | 新文件 | 新增 | §8 的 E1–E6 |
| `README.md` | `:509` `### Commands` 表格之前 | 新增 | 一句：每个命令的 `--help` 末尾都有示例 |
| `README.zh-CN.md` | `:509` `### 命令` 表格之前 | 新增 | 同上，中文 |
| `CHANGELOG.md` | `:7` `## [Unreleased]` 下 | 新增 | `### Added` 一条 |
| `docs/analysis/roadmap.md` | §2.4 表格 `:60` 之后 | 新增 | 一行“子命令帮助示例” |

`add_parser` 的 25 处调用（`:167-372`）一行不改：说明文字的折行由 `_attach_examples` 统一做。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 挂示例的方式

新增 `_attach_examples`（插入位置：`cli.py:111` `_examples_lines` 之后）：

```python
def _attach_examples(parser: argparse.ArgumentParser, examples: List[Tuple[str, str]]) -> None:
    """给子命令帮助末尾加 Examples: 段。RawDescriptionHelpFormatter 才能保留示例的列对齐，
    但它也不再折行说明文字，所以这里先把说明按 79 列折好（已手动加过 \\n 的说明原样保留）。"""
    if "\n" not in (parser.description or ""):
        parser.description = "\n".join(textwrap.wrap(parser.description or "", 79))
    parser.epilog = "\n".join(_examples_lines(examples))
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
```

在 `build_parser` 的 `return parser`（`:374`）之前：

```python
    for name, examples in _SUBCOMMAND_EXAMPLES.items():
        _attach_examples(sub.choices[name], examples)
```

- `sub` 是 `:164` 的 `add_subparsers` 返回值，`sub.choices` 是子命令名 → 子解析器。
- `textwrap.wrap` 按空格断行，单词不拆开；说明里最长的不可断片段是 `sessions` 的 `[--grep TEXT]` 之类，远小于 79。
- `add`、`set`、`list` 不在 `_SUBCOMMAND_EXAMPLES` 里，保持原样（它们的说明已手动加 `\n`，`epilog` 在 `add_parser` 时就传了）。

#### 5.1.2 示例清单

`_SUBCOMMAND_EXAMPLES` 的键按 `build_parser` 里的定义顺序排列。每条是 `(命令, 说明)`，说明可为空。下表“覆盖”一列写出该条用到的选项或取值，最后一列是写这条时核过的运行期约束。

| 子命令 | 示例命令 | 说明 | 覆盖 / 约束依据 |
| ---- | ---- | ---- | ---- |
| init | `multi-claude init --root ~/.cc --bin-dir ~/.local/bin` | where account directories and launchers go | `--root` `--bin-dir` |
| init | `multi-claude init --shared-dir ~/.claude-shared` | directory whose items can be linked into accounts | `--shared-dir` |
| init | `multi-claude init --shared-items agents,commands,skills,CLAUDE.md` | items to share (this is the default) | `--shared-items`；值与 `config.py:18` 默认一致，不含不可共享项（`cli.py:639-643`） |
| init | `multi-claude init --root ~/.cc --dry-run --verbose` | list every planned action without writing | `--dry-run` `--verbose` |
| migrate-default | `multi-claude migrate-default --dry-run` | preview; without NAME, usually the login e-mail | `--dry-run`；省略名称时依次取三个来源，见 `:177` 注释 |
| migrate-default | `multi-claude migrate-default home` | turn ~/.claude into account home, login kept | |
| migrate-default | `multi-claude migrate-default home --proxy 7901` | and give it a proxy | `--proxy` |
| migrate-default | `multi-claude migrate-default home --copy --keep-backup` | copy instead of rename; keep the original | `--copy` `--keep-backup` |
| migrate-default | `multi-claude migrate-default home --skip-process-check` | do not check for running Claude Code (risky) | `--skip-process-check` |
| proxy | `multi-claude proxy work 7901` | same as http://127.0.0.1:7901 | 取值见 README “Proxy values” |
| proxy | `multi-claude proxy work https://proxy.example.com:8443` | any http(s) URL without user or password | |
| proxy | `multi-claude proxy work off` | unset the proxy variables in claude-work | |
| proxy | `multi-claude proxy work inherit --dry-run` | keep the shell's proxy (default); preview only | `--dry-run` |
| env | `multi-claude env work MAX_THINKING_TOKENS=8000` | add or change a variable | 需要账号名或 `--defaults`、至少一个 K=V 或 `--unset`（`:1204-1220`） |
| env | `multi-claude env work --unset MAX_THINKING_TOKENS` | remove it again | `--unset` |
| env | `multi-claude env --defaults MAX_THINKING_TOKENS=8000` | for every account | `--defaults` |
| env | `multi-claude env work DISABLE_TELEMETRY=1 --dry-run` | preview only | `--dry-run` |
| args | `multi-claude args work -- --ide` | claude-work always starts with --ide | 必须有 `--`（`:446-454`） |
| args | `multi-claude args --defaults -- --ide` | the same for every account | `--defaults`；账号名与 `--defaults` 二选一（`:703-708`） |
| args | `multi-claude args work --` | clear the fixed arguments of work | |
| args | `multi-claude args work --dry-run -- --ide` | preview only | `--dry-run`（选项写在 `--` 之前） |
| remove | `multi-claude remove work` | unregister work; directory and login stay | |
| remove | `multi-claude remove work --dry-run` | preview only | `--dry-run` |
| apply | `multi-claude apply` | rewrite launchers and links to match config.json | |
| apply | `multi-claude apply --dry-run --verbose` | list every planned action without writing | `--dry-run` `--verbose` |
| apply | `multi-claude apply -f accounts.json` | adopt accounts.json as the new configuration | `-f/--file`；README `:574` 原样 |
| usage | `multi-claude usage` | 5-hour and 7-day usage of every account | |
| usage | `multi-claude usage work --json` | one account, for scripts | `--json` |
| usage | `multi-claude usage --history` | tokens per day, last 7 days | `--history` |
| usage | `multi-claude usage --history work --days 30 --by model` | one account, 30 days, per model | `--days` `--by model`；需 `--history`（`:511`），天数 1–366（`:1779`）；README `:276` 原样 |
| usage | `multi-claude usage --history --by day` | per day (the default grouping) | `--by day` |
| doctor | `multi-claude doctor` | check the setup and suggest fixes | |
| doctor | `multi-claude doctor --verbose` | also list the checks that passed | `--verbose`（本命令自己的选项） |
| doctor | `multi-claude doctor --json` | for scripts | `--json` |
| route | `multi-claude route ~/work work` | claude-auto uses work under ~/work | 四种写法见 `:870-896` |
| route | `multi-claude route ~/work --remove` | remove that route | `--remove` |
| route | `multi-claude route --default home` | use home where no route matches | `--default` |
| route | `multi-claude route --no-default` | run plain claude where no route matches | `--no-default` |
| route | `multi-claude route ~/work work --dry-run` | preview only | `--dry-run` |
| rename | `multi-claude rename work job` | claude-work becomes claude-job | |
| rename | `multi-claude rename work job --dry-run` | preview only | `--dry-run` |
| mcp | `multi-claude mcp work list` | the MCP servers of work | README `:387`，账号名由 `client-a` 改为 `work` |
| mcp | `multi-claude mcp work add --scope user github -- npx -y @modelcontextprotocol/server-github` | everything after the name goes to `claude mcp` | README `:386`，账号名由 `client-a` 改为 `work`；账号名后原样传给 claude（`:430-438`） |
| login | `multi-claude login work` | runs `claude auth login` as work | README `:118` |
| which | `multi-claude which` | the account claude-auto would use here | |
| which | `multi-claude which ~/work` | ... in ~/work | |
| run | `multi-claude run work` | same as claude-work | README `:398` |
| run | `multi-claude run work -- claude --version` | any command, with the environment of claude-work | README `:397`；`--` 之后原样（`:439-444`） |
| run | `multi-claude run -- env` | without a name: the account claude-auto would use here | README `:399`；不给账号名时需要有路由或默认账号，否则退出码 2（`cli.py:977-982`） |
| path | `multi-claude path work` | print the directory of work | |
| code | `multi-claude code work ~/work` | a separate VS Code window for work | README `:411` 改用 `~/work` |
| code | `multi-claude code work . -- --disable-gpu` | arguments after -- go to VS Code | README `:412`，账号名由 `personal` 改为 `work` |
| restore | `multi-claude restore home --dry-run` | preview moving home back to ~/.claude | `--dry-run` |
| restore | `multi-claude restore home` | undo migrate-default, login kept | |
| restore | `multi-claude restore home --skip-process-check` | do not check for running Claude Code (risky) | `--skip-process-check` |
| export | `multi-claude export work ~/work-settings.tar.gz` | settings, CLAUDE.md, agents, skills, MCP; no logins | README `:206` |
| import | `multi-claude import ~/work-settings.tar.gz home` | into an existing account | README `:207` 账号名改为 `home` |
| import | `multi-claude import ~/work-settings.tar.gz home --dry-run` | preview only | `--dry-run` |
| import | `multi-claude import ~/work-settings.tar.gz home --force` | back up and replace items that differ | `--force` |
| completion | `multi-claude completion bash > ~/.local/share/bash-completion/completions/multi-claude` | bash completion for your user | `bash`；README `:429` |
| completion | `multi-claude completion zsh > "${fpath[1]}/_multi-claude"` | zsh: any directory on your fpath | `zsh`；README `:430` |
| completion | `multi-claude completion fish > ~/.config/fish/completions/multi-claude.fish` | fish completion | `fish`；README `:431` |
| statusline | `multi-claude statusline install ~/.claude/settings.json` | record fresh usage from the status line | `install`；README `:262` |
| statusline | `multi-claude statusline uninstall ~/.claude/settings.json --dry-run` | preview restoring the original command | `uninstall` `--dry-run` |
| handoff | `! multi-claude handoff home` | inside a Claude session: hand it to home | 会话外需要 `--from`（`:1866`），故写成会话内 `!` 的用法（README `:372`） |
| handoff | `multi-claude handoff home --from work` | the latest session of work in this directory | `--from` |
| handoff | `multi-claude handoff home --from work --session 3c588340-1219-48df-a231-509866789487` | a given session | `--session`；会话号格式见 `:1869-1870` |
| handoff | `multi-claude handoff home --from work --force --dry-run` | preview replacing a different copy | `--force` `--dry-run` |
| sessions | `multi-claude sessions` | the 30 newest sessions of every account | |
| sessions | `multi-claude sessions work --limit 10` | one account, 10 sessions | `--limit` |
| sessions | `multi-claude sessions --grep importer --cmd` | with the command that resumes each | `--grep` `--cmd` |
| sessions | `multi-claude sessions --limit 0 --json` | every session, for scripts | `--json`；`--limit` ≥ 0（`:1956`） |
| sessions | `multi-claude conversations --grep importer` | another name for sessions | 别名（`:1137`） |
| artifacts | `multi-claude artifacts` | the 30 newest artifacts of every account | |
| artifacts | `multi-claude artifacts work --grep report` | one account, matching text | `--grep` |
| artifacts | `multi-claude artifacts --path --cmd` | with the source file and the resume command | `--path` `--cmd` |
| artifacts | `multi-claude artifacts --limit 0 --json` | every artifact, for scripts | `--limit` `--json`；`--limit` ≥ 0（`:2054`） |

说明列的最终措辞在编码时可微调，但每条命令、覆盖面与约束不变。

两处特殊写法：

- `handoff` 第一条以 `! ` 开头（在 Claude 会话里用 `!` 运行），测试取命令时去掉开头的 `! `。
- `completion` 三条带 `>` 重定向，测试解析前截掉第一个 shell 运算符（`>`、`>>`、`|`）及之后的词。

#### 5.1.3 测试怎么取示例、怎么解析

沿用 `tests/test_renewal.py:178-227` `RenewsHelpTest` 的取法：帮助文本 `Examples:` 之后、以两个空格开头的行，按“两个以上空格”切出第一列。

解析必须走 `main`（`cli.py:458-484`）在 argparse 之前的同一串步骤，否则 `args`、`run`、`code`、`mcp`、`login`、别名 `conversations` 的示例无法正确解析：

1. `shlex.split`，去掉开头的 `!`，确认第一个词是 `multi-claude` 并去掉；截掉 shell 运算符及之后的词。
2. `cli._resolve_command_alias`（`:1140`）。
3. `cli._unknown_command(parser, argv)`（`:1153`）与 `cli._misplaced_account_name(argv)`（`:1255`）都返回 `None`。
4. `cli._split_args_command(argv)`（`:414`）不抛 `UsageError`。
5. `parser.parse_args(argv)` 不抛 `SystemExit`；解析出的 `command` 等于该帮助所属的子命令（防止把别的命令的示例贴错地方）。

### 5.2 接口变更

- 新增：25 个子命令的 `--help` 输出末尾多出 `Examples:` 段；这些子命令的说明文字改为固定按 79 列折行（原来随终端宽度折行）。
- 无选项、位置参数、退出码、配置文件、JSON 输出的变更；补全脚本不变。
- 本仓库没有 `docs/reference/`，无 reference sibling 回补项。

## 6. 备选方案与决策

| 方案 | 结论 | 理由 |
| ---- | ---- | ---- |
| A：每个 `add_parser` 调用里传 `epilog=`、`formatter_class=`，说明手动加 `\n`（`add` / `set` / `list` 的写法） | 否决 | 要改 25 处调用和 15 段超过 80 列的说明，手动折行容易出错，以后改说明还得重新折 |
| B：自定义格式器，覆盖 `_fill_text`，只对含换行的文字原样输出 | 否决 | `_fill_text` 是 argparse 的内部方法（`argparse.py:682-683` 写明只有类名是公开接口），跨版本有风险 |
| C：建完子解析器后统一设置 `description` / `epilog` / `formatter_class`（本方案） | 采用 | 只用构造参数对应的公开属性；改动集中在一处，说明折行自动完成 |

## 7. 影响分析

- **帮助输出**（正向）：只有 `<子命令> --help` 与 `<子命令> -h` 的输出变化。`-h` 在 `mcp` / `login`（`:433-434`）、`args`（`:451-452`）的预切分里另有分支，但都是把参数交回 argparse，帮助内容同样来自子解析器，会带上示例。`conversations --help` 经别名换成 `sessions --help`，同样带示例。
- **说明折行**（正向）：原来 `HelpFormatter` 按终端宽度减 2 折行，改后固定 79 列。终端窄于 80 列时长行会被终端自动折回，与 `set` / `list` 现状相同。现有测试 `tests/test_help.py:30-35` 断言的三段文字：`add`、`login` 的说明在第一行内；`artifacts` 的 “list the artifacts published from the Claude sessions of every account” 共 70 个字符，位于说明开头，79 列折行不会拆开。由 §8 R1 覆盖。
- **其它读 `description` / `epilog` 的代码**（反向）：`grep -rn "description\|epilog" src/` 只有 `cli.py` 自身建解析器的位置与 artifact 的 `description` 字段（`:2084`、`:2154`，与解析器无关）；`completion.py` 生成补全脚本不读这两个属性。由 §8 R2 覆盖。
- **运行时**：`build_parser` 每次调用多一个 25 次的循环和 25 次 `textwrap.wrap`，微秒级；`main` 每次运行都会调用 `build_parser`（`:470`），状态栏钩子在 `build_parser` 之前就返回（`:460-465`），不受影响。
- **测试时间**：E5 共 23 个沙箱、82 条示例，连同准备步骤约 140 次子进程；按现有 `CliTestCase` 单次子进程约 0.2 秒估计，本机增加约 30 秒，编译机更慢。编码后实测并写进落地注记。
- **部署形态**：纯 Python 标准库，无新依赖；实际运行的版本：CI 3.8 / 3.10 / 3.12（`.github/workflows/ci.yml:13-21`）、本机 3.11、编译机 3.8.10；3.9 与 3.13 不在其中。

## 8. 回归测试

新文件 `tests/test_help_examples.py`：

| 编号 | 内容 | 对应影响 |
| ---- | ---- | ---- |
| E1 | 遍历 `build_parser()` 的全部子命令（28 个），每个 `--help` 退出码 0，且含 `Examples:` 段、至少一条示例 | 目标 1 |
| E2 | 每条示例按 §5.1.3 的五步解析成功，解析出的子命令等于所属子命令 | 目标 2 |
| E3 | 覆盖：子解析器的每个可选参数（`-h/--help` 除外；用 `_add_write_options` 加的公共 `--verbose` 除外，即 `list`、`doctor` 以外的命令）至少一个选项名出现在示例里（沿用 `tests/test_renewal.py:222-226`：`shlex.split` 切词后按整词集合比较，避免 `--shared` 被 `--shared-exclude` 误判为已覆盖）；每个带 `choices` 的参数（`completion` 的 shell、`statusline` 的 action、`usage --by`）每个取值都出现 | 目标 2、3 |
| E4 | 28 个子命令的说明每行不超过 80 列；`Examples:` 段每行命令与说明之间至少两个空格 | §7 说明折行 |
| E5 | 运行期，规则见 §8.1 | 目标 2 |
| E6 | 主帮助与 `add`、`set`、`list` 不受循环影响：断言这四个解析器的 `description`、`epilog`、`formatter_class` 与 `build_parser` 里传入的原值相同（不做整段帮助逐字比对：选项说明仍随终端宽度折行，`argparse.py:512`） | 非目标第 1 条 |
| R1 | 现有断言子命令帮助的测试全部通过：`tests/test_help.py`、`tests/test_renewal.py`、`tests/test_sessions.py`、`tests/test_set.py`、`tests/test_mcp.py`、`tests/test_onboarding.py` | §7 说明折行 |
| R2 | 现有 `tests/test_completion.py` 全部通过，补全脚本输出不变 | §7 其它读取方 |
| R3 | 全量测试：本机 macOS 与编译机 Ubuntu 20 / Python 3.8.10 都通过；CI 3.8 / 3.10 / 3.12 通过 | §3 版本假设 |

### 8.1 E5 运行期测试的规则

E5 在 `tests/helpers.py` 的 `CliTestCase` 沙箱里以子进程真正运行示例。规则：

1. **范围与沙箱**：全部 28 个子命令减去第 5 条跳过的 5 个，共 23 个（含 `add`、`set` 已有的示例），每个子命令一个新沙箱。公共准备：`add work`、`add home`，建目录 `<home>/work`。各命令的额外准备：
   - `run`：`route --default home`（不给账号名的 `run -- env` 需要它，`cli.py:977-982`）；
   - `import`：先 `export work <home>/work-settings.tar.gz`（`export` 沙箱不做这一步，否则目标已存在，`bundle.py:52-56` 返回退出码 3）；
   - `apply`：把沙箱的 `config.json` 复制为 `<home>/accounts.json`；
   - `add`：写入 `<root>/home/settings.json`（内容 `{}`），否则 `add work --config-from home` 因来源没有 `settings.json` 退出 1（`cli.py:810-812`）；
   - `statusline`：在假命令目录放一个 `multi-claude` 小脚本（否则 install 因 PATH 里找不到它退出 1，`cli.py:2233-2237`），并写一份带 command 类型 `statusLine` 的 `<home>/.claude/settings.json`（install 与 uninstall 都要读它，文件不存在或没有 command 类型的 statusLine 时退出 1，`statusline.py:70`、`:96`、`:104-106`，`cli.py:2240-2242`）。
2. **路径与工作目录**：示例里以 `~` 开头的词按 shell 规则展开为沙箱的 home（`shlex.split` 不展开 `~`，`export` 也不展开，`bundle.py:54-58`）；子进程工作目录设为沙箱的 home（`run_cli` 默认是 `self.tmp`，`tests/helpers.py:152-156`）。截掉 shell 运算符、去掉开头 `! ` 的做法同 §5.1.3。
3. **顺序**：同一子命令里先运行含 `--dry-run` 的示例，再按表中顺序运行其余示例，避免 `rename work job`、`remove work` 先执行后让同命令的 `--dry-run` 示例找不到账号。例外：`statusline` 按表中顺序运行，让 `uninstall --dry-run` 预览的是 install 之后的还原。
4. **判据**：每条示例退出码为 0。例外只能是沙箱环境造成的非零码，列在测试里的允许表中，每项写明退出码和源码依据；退出码 2（参数被拒）任何情况下都不允许。初始允许表为空。编码时实测发现的其它环境性非零码按同样格式补进允许表，并写进落地注记。
5. **跳过**（理由写进测试注释）：
   - `list`：会联网查套餐，示例已由 `tests/test_renewal.py` 的 T8 做解析核对；
   - `handoff`：示例要么在 Claude 会话里运行，要么需要来源账号里真有会话；
   - `migrate-default`、`restore`：需要真实的 `~/.claude` 与 `~/.claude.json` 登录信息（省略名称时从登录邮箱取名，`cli.py:773-783`；`restore` 要求账号是迁移来的 default 身份），沙箱里构造等于重写 `tests/test_migrate.py` 的准备步骤；
   - `code`：会从 PATH 拉起 VS Code（`cli.py:1043`、`:1052`），沙箱 PATH 含 `/usr/bin`，测试机若装了 VS Code 会真的打开窗口；行为由 `tests/test_vscode.py` 用假 `code` 覆盖。
   这些命令的示例仍由 E2 解析核对，参数约束见 §5.1.2 最后一列。
6. **失败信息**：断言信息带上示例命令、退出码和 stderr。
7. **原型实测**（2026-10-17，本机 macOS / Python 3.11.9，基线 9524d23 未改代码）：按第 1–3 条在 `CliTestCase` 沙箱里运行 §5.1.2 与 `_ADD_EXAMPLES`、`_SET_EXAMPLES` 的全部示例，82 条退出码均为 0。原型脚本在会话草稿目录，不入库；编码时按同样规则写成正式测试。

手动核对（L1，本机）：`multi-claude route --help`、`multi-claude args --help`、`multi-claude conversations --help`、`multi-claude mcp --help` 各看一次，确认示例列对齐、说明折行正常。

## 9. 日志 / 观测点

本改动只影响帮助文本，不新增日志。观测方式：

- `multi-claude <子命令> --help | sed -n '/^Examples:/,$p'` 看示例段；
- `python3 -m unittest tests.test_help_examples -v` 看 E1–E6 逐项结果；
- E5 失败时断言信息里带上示例命令、退出码和 stderr，便于定位是哪条示例被拒。
