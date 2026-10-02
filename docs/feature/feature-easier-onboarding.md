# multi-claude：降低上手门槛（第一批）

> 2026-10-02 注记：plan-review 2 轮收敛，用户同意分批实施；代码已按方案实现（`cli.py` `cmd_overview` / `_unknown_command` / `cmd_login` / `_hint_next_step` / `_add_write_options`，`accounts.execute` 的 `verbose`，`config.normalize_proxy`），本机 267 用例通过，待独立代码评审与编译机验证，未提交。基线：main `7b9353c`（v0.2.2）。用户 2026-10-02 同意分三批实施，本方案是第一批：A1、A2、B1、D1、D2、D3。第二批（帮助分组、`list` 简表、PATH 提示、README 重排）与第三批（默认共享目录、`set` 命令）另写方案。

## 1. 背景

在临时 HOME 里从零模拟一个新用户（假 `claude`，2026-10-02）。从安装到第一个账号能用共 5 步，下列 6 处没有给出下一步提示，或报错让人看不懂：

| # | 实测现象 |
| ---- | ---- |
| A1 | 不带参数运行 `multi-claude`，输出 `error: the following arguments are required: COMMAND`，退出 2（`cli.py:43` `sub.required = True`） |
| A2 | README 的快速开始第一步就是 `migrate-default main`（`README.md:57`）：这是风险最高的命令，要求先关掉所有 Claude 会话、在普通终端里运行 |
| B1 | `add me@example.com` 只打印三行 `create …`，没有提示要登录；没有登录命令，只能先启动 `claude-<名称>` 再在会话里输入 `/login` |
| D1 | 把 `list` 拼成 `lsit`，argparse 报 `invalid choice` 并列出全部 18 个子命令，不给建议 |
| D2 | `proxy NAME 7891x` 报 `unsupported proxy scheme ''; use one of http, https`（`config.py:458-460`） |
| D3 | 每条写命令都把计划中的每一项都打印出来，包括大量 `unchanged …`（`accounts.py:282-283`）；例如 `add Work` 也会打印其它账号的 `unchanged launcher` |

Claude Code 2.1.286 自带 `claude auth login`（实测 `claude auth --help`：`login [options]  Sign in to your Anthropic account`，选项 `--claudeai`（订阅，默认）、`--console`、`--email <email>`、`--sso`）。

## 2. 目标 / 非目标

**目标**

1. 新用户的路径变成“安装 → `add` → `login` → 启动”，每一步结束时都告诉用户下一步。
2. 常见输错（拼错子命令、代理写法不对）的报错能直接说明正确写法。
3. 写命令默认只输出有变化的内容。

**非目标**

- 第二批、第三批的内容（帮助分组、`list` 简表、PATH 提示、默认共享目录、`set` 命令）。
- 不改任何已有命令的退出码契约与 `--json` 输出。
- 不替用户运行 `claude` 会话本身。

## 3. 假设与约束

- `login` 照搬 `mcp` 的做法：用账号的身份环境（`env.account_env`）运行 `claude auth login …`，不带账号的固定参数。原因与 `mcp` 相同：固定参数里取多个值的选项会吞掉子命令（`feature-rename-mcp.md` §1）。
- 登录探测复用 `identity.probe`（`identity.py:68`）：macOS 查钥匙串，再查凭据文件；Linux 查凭据文件。探测结果只用于决定要不要打印提示，不改变退出码。
- 子命令名的候选列表从 argparse 解析器取（与 `completion.collect` 同源），不另写一份清单。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `7b9353c`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/cli.py` | `main` :211、:224-226 之前 | 新增 | 无参数时输出上手说明或账号表（A1）；未知子命令时给建议（D1） |
| `src/multi_claude/cli.py` | `build_parser` :142-145 之间 | 新增 | `login` 子命令；`_add_dry_run` :172 旁新增 `_add_write_options`（`--dry-run` + `--verbose`） |
| `src/multi_claude/cli.py` | `_split_args_command` :191 | 修改 | `login NAME` 之后的内容原样交给 `claude auth login`，与 `mcp` 相同 |
| `src/multi_claude/cli.py` | `main` 中 `mcp` 分派（`if args.command == "mcp"` 一段） | 新增 | `login` 分派：迁移未完成时拒绝，不加写锁 |
| `src/multi_claude/cli.py` | `cmd_mcp` :508-528 | 修改 | 抽出账号检查与环境构造的公共部分，供 `cmd_login` 复用 |
| `src/multi_claude/cli.py` | `dispatch` 的 `add` :346-365、收尾 :415-423 | 修改 | 把 `verbose` 传给 `converge`；`add` 成功后提示下一步（B1） |
| `src/multi_claude/accounts.py` | `execute` :280-283、`converge` :309-314 | 修改 | 新增 `verbose` 参数；默认不打印 `unchanged`，全部不变时打印一行汇总（D3） |
| `src/multi_claude/config.py` | `normalize_proxy` :444-460 | 修改 | 没有 `://` 的非数字输入给出可读报错（D2） |
| `src/multi_claude/completion.py` | `POSITIONAL_KINDS` | 修改 | `login` 的第 1 个位置参数补账号名 |
| `tests/test_onboarding.py` | 新文件 | 新增 | 见 §8 |
| `tests/test_accounts.py` | :481 | 修改 | 断言 `unchanged shared-link` 的用例改用 `--verbose` |
| `tests/test_completion.py` | `SUBCOMMANDS` | 修改 | 加 `login` |
| `README.md`、`README.zh-CN.md` | 快速开始（:57）、命令表 | 修改 | 快速开始改为 `add → login → 启动`，`migrate-default` 移到其后的可选说明（A2）；命令表加 `login`，说明 `--verbose` |
| `CHANGELOG.md` | Unreleased | 修改 | Added / Changed |

`converge` 的调用点全集（`grep -rn "converge(" src/`，不截断）：`cli.py:415`、`migrate.py:298`。`migrate.py` 不传 `verbose`，按默认值处理（只打印有变化的动作）。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 A1：不带参数

`main` 在拦截钩子之后、`_split_args_command` 之前判断：`argv` 为空时不再交给 argparse。

- **配置文件不存在**：打印上手说明，退出 0：

```text
multi-claude runs several Claude Code accounts side by side, each with its own launcher.

Get started:
  multi-claude add work      create the account "work" and the launcher claude-work
  multi-claude login work    sign in to that account
  claude-work                start Claude Code with it

Already have a login in ~/.claude? It keeps working as plain `claude`;
`multi-claude migrate-default --help` explains how to turn it into an account.
All commands: multi-claude --help
```

- **配置文件存在**：等同 `multi-claude list`（第二批会把它改成简表），并在最后加一行 `All commands: multi-claude --help`。
- **配置损坏**：沿用 `list` 的现有处理（`ConfigError` → 退出 1）。

#### 5.1.2 D1：拼错子命令

在 `parser.parse_args` 之前：

1. 取出第一个不以 `-` 开头的参数作为命令名。
2. 若它不是任何子命令（候选取自解析器里的 `_SubParsersAction.choices`），用 `difflib.get_close_matches(word, names, n=1, cutoff=0.6)` 找相近的。
3. 输出 `[multi-claude] error: unknown command 'lsit'; did you mean 'list'?`；没有相近的就输出 `… unknown command 'xyz'; run multi-claude --help for the list`。退出 2。

`--help`、`--version`、`statusline-hook`（已在更早处拦截）不受影响。

#### 5.1.3 D2：代理写法

`normalize_proxy`（`config.py:444`）在 `urlsplit` 之前补一条判断：输入既不是纯数字，也不含 `://` 时，报

`proxy must be a port (e.g. 7890), an http(s) URL (e.g. http://127.0.0.1:7890), off or inherit; got '7891x'`

已有的 SOCKS、凭据、路径等报错不变。

#### 5.1.4 B1：登录与下一步提示

- **`multi-claude login NAME [ARG ...]`**：
  - `NAME` 之后的全部内容原样作为 `claude auth login` 的参数（在 `_split_args_command` 里与 `mcp` 一起处理）。
  - 账号检查与环境构造与 `cmd_mcp` 相同，抽成 `_account_process_env(name) -> (env, code)` 供两者共用：账号未登记 → 1；账号目录不存在 → 1；default 身份但链接不对 → 1；PATH 中没有 `claude` → 127。
  - 账号名形如邮箱（含 `@`、且 `@` 后有 `.`），且参数里没有 `--email` 时，追加 `--email NAME`，在登录页预填邮箱。
  - 退出码等于 `claude auth login` 的退出码。
  - 迁移未完成时拒绝（同 `mcp`），不加写锁。
- **`add` 之后的提示**：`add` 成功（退出 0、不是 `--dry-run`）后，若 `identity.probe(account.identity, 账号目录)` 返回 `none`，打印：

```text
[multi-claude] next: multi-claude login <名称>   (then start claude-<名称>)
```

  已有登录（`keychain` / `file`）或探测失败（`unknown`）时不打印。原有 `_warn_if_not_logged_in`（接管已有目录时的路径写法提醒，`cli.py:423`）保持不变，两者可能同时出现。

#### 5.1.5 D3：只打印有变化的动作

- `execute(..., verbose=False)`：
  - `verbose` 为假时，跳过状态为 `unchanged` 的动作，`create` / `update` / `delete` / `skip` / `conflict` 照常打印。
  - 一行都没打印、也没有冲突时，打印 `[multi-claude] nothing to change`。
  - `verbose` 为真时与现在完全相同。
- `converge(..., verbose=False)` 透传。
- `init`、`add`、`proxy`、`env`、`args`、`remove`、`apply`、`route`、`rename` 用 `_add_write_options`，新增 `--verbose`（帮助：`also show items that are already up to date`）。`migrate-default`、`statusline`、`handoff` 仍用 `_add_dry_run`。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `multi-claude`（无参数） | 由“用法错误、退出 2”改为上手说明或账号表、退出 0 | 依赖无参数退出 2 的脚本会受影响（不太可能存在） |
| 未知子命令 | 报错文字改为带建议，退出码仍为 2 | 兼容 |
| `multi-claude login NAME [ARG ...]` | 新增 | 新增 |
| 写命令的 `--verbose` | 新增；默认输出不再含 `unchanged` 行 | 解析人读输出、找 `unchanged` 的脚本需加 `--verbose`；`--json` 不涉及 |
| `normalize_proxy` 报错文字 | 修改 | 退出码不变 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **`login` 直接启动 `claude-<名称>` 并提示输入 `/login`**：需要用户进会话再操作。`claude auth login` 是官方的非会话入口，步骤更少，因此不采用。
- **无参数时进入交互式向导**：要处理终端交互与取消，第一批先给出静态说明。

## 7. 影响分析

**正向**

- 只改输出与入口分派；收敛计划、写入顺序、退出码契约都不变（无参数一项除外）。
- `login` 只启动一个子进程，凭据由 Claude 自己写入，本工具不读写凭据。

**反向**

- **测试**：断言 `unchanged …` 的用例（`tests/test_accounts.py:481`）需要加 `--verbose`；`tests/test_routes.py:251` 是先删掉 `unchanged router` 再断言，不受影响。全量回归兜底。
- **`list` 与无参数**：配置存在时，无参数等同 `list`，所以 `list` 的告警（隔离变量、代理变量）也会出现。
- **`add` 后探测登录**：macOS 上多一次 `security find-generic-password` 调用（测试里是假 `security`，并检查调用参数）。
- **运行时**：没有新进程（`login` 本身除外）、没有新文件。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 无配置时 `multi-claude` | 退出 0，输出含 `Get started:` 与 `multi-claude login work` |
| T2 | 有配置时 `multi-claude` | 输出与 `list` 相同，另多 `All commands` 一行；退出 0 |
| T3 | `multi-claude lsit`；`multi-claude xyz` | 退出 2；前者含 `did you mean 'list'`，后者含 `run multi-claude --help` |
| T4 | `proxy NAME 7891x`；`proxy NAME socks5://h:1` | 前者报错含 `got '7891x'`；后者仍是原 SOCKS 报错；退出码不变 |
| T5 | `login NAME`（假 claude 记录参数与环境） | 假 claude 收到 `auth login`；`CLAUDE_CONFIG_DIR` 为账号目录；鉴权覆盖变量已清除；代理与账号一致；不含账号的固定参数；退出码透传（`FAKE_CLAUDE_RC=5` → 5） |
| T6 | `login me@example.com` 与 `login me@example.com --email x@y.z`；`login work --console` | 第一个追加 `--email me@example.com`；第二个不追加；第三个原样透传 `--console` |
| T7 | `login nobody`；迁移未完成时 `login work` | 退出 1，不调用 claude |
| T8 | `add work` 无登录 / 有登录 / `--dry-run` | 第一个输出 `next: multi-claude login work`；第二、第三个不输出。“有登录”的构造按平台：macOS 让假 `security` 返回 0（`FAKE_SECURITY_RC=0`），Linux 在账号目录写入 `.credentials.json`（`identity.credentials_file`） |
| T9 | 写命令默认输出 | 重复 `add work` 只输出 `nothing to change`；`--verbose` 时输出与改动前相同（含 `unchanged`）；有变化时只列变化项 |
| T10 | 补全 | 三种 shell 含 `login`；bash 下 `login ` 补账号名 |
| T11 | 回归 | 现有全部用例通过（基线 258 个） |
| T12 | 实机（L3，需用户同意） | `multi-claude login <账号>` 打开浏览器登录页，登录后 `list` 中 LOGIN 变为 keychain |

T1–T11 在编译机上闭合软件控制流；T12 验证真实的 `claude auth login`。

## 9. 日志 / 观测点

- **上手说明**：无参数时输出 `Get started:` 一段。
- **建议**：`error: unknown command '<词>'; did you mean '<命令>'?`。
- **下一步提示**：`next: multi-claude login <名称>`。
- **汇总**：`nothing to change`。
- **`login` 失败**：沿用 `mcp` 的报错文字（`account '…' is not registered`、`account directory does not exist`、`claude not found in PATH`）。
