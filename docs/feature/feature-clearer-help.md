# multi-claude：帮助、列表与安装提示更易读（第二批）

> 2026-10-02 注记：plan-review 2 轮收敛；代码已按方案实现（`cli.py` `COMMAND_GROUPS` / `_help_epilog` / `_print_brief_list` / `_status_cell` / `_hint_bin_on_path`，新增 `shellpath.py`，`doctor.py` `bin-on-path`，`install.sh` `path_command`，README 重排），待独立代码评审与编译机验证，未提交。基线：main `01287b2`（第一批已合并）。用户 2026-10-02 同意分三批实施，本方案是第二批：C1 帮助分组、C2 `list` 简表、A3 PATH 提示、README 重排。三批做完后一起发 v0.3.0。

## 1. 背景

2026-10-02 新用户模拟中发现的问题：

| # | 现状 |
| ---- | ---- |
| C1 | `multi-claude --help` 把 19 个子命令平铺成一列（`cli.py:44` `add_subparsers(metavar="COMMAND")`，每个 `add_parser(help=…)`），没有分组和示例；`multi-claude add --help` 只有参数列表，没有用途说明 |
| C2 | `list` 先打印 `root:`、`bin_dir:`、`shared.dir:` 三行，表头 `NAME IDENTITY DIR PROXY SHARED LAUNCHER LOGIN LINK` 中多数是内部状态；看用量还要另跑 `usage`（`cli.py:778` `cmd_list`） |
| A3 | 安装脚本只提示 `add it in your shell profile, e.g. export PATH=…`（`install.sh:222-225`），没说是哪个文件；`add` 之后启动命令所在目录不在 PATH 上时，`claude-<名称>` 会报 command not found，只有 `doctor` 才检查（`doctor.py:126-132` `bin-on-path`） |
| R | README 中英各 353 行：命令参考表在前，常见操作分散在“Commands”的子节和后面的各个 `##` 里 |

## 2. 目标 / 非目标

**目标**

1. `--help` 按用途分组列出子命令，并附 3–4 条示例；每个子命令的 `--help` 带一句用途说明。
2. `list` 默认显示简表：账号、登录、代理、共享、5 小时与 7 天用量、状态；完整信息放进 `list --verbose`。
3. PATH 提示给出具体文件和可直接粘贴的命令，`add` 时也提示。
4. README 改为“安装 → 快速开始 → 常见操作 → 迁移（可选）→ 参考”的结构，内容不删减。

**非目标**

- 不改 `list --json`、`list --names` 的输出。
- 不替用户改 shell 配置文件。
- 第三批的默认共享目录与 `set` 命令。

## 3. 假设与约束

- 子命令分组用 argparse 的 `epilog` 加 `RawDescriptionHelpFormatter` 实现：子命令的 `add_parser` 不再传 `help=`，argparse 就不会在顶层列出它们；用途说明改用 `description=`，在子命令自己的 `--help` 里显示。补全从 `_SubParsersAction.choices` 取名字（`completion.collect`），不依赖 `help`，不受影响。
- 简表的 5H / 7D 复用 `usage.read_account_usage` 与 `_window_cell`（`cli.py:932`），与 `usage` 命令同一口径；读用量只是读 `.claude.json` 和快照文件，不联网、不读凭据。
- shell 判定取 `$SHELL` 的 basename：`zsh` → `~/.zshrc`；`bash` → macOS 上 `~/.bash_profile`、其它系统 `~/.bashrc`；`fish` → `fish_add_path <目录>`；其它 → 保留现有通用提示。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `01287b2`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/cli.py` | `build_parser` :38-175 | 修改 | 顶层 `formatter_class` 与分组 `epilog`；各子命令 `help=` 改为 `description=`（文字不变） |
| `src/multi_claude/cli.py` | `cmd_list` :778-833 | 修改 | 默认简表；`--verbose` 输出现有完整内容 |
| `src/multi_claude/cli.py` | `build_parser` 中 `list --verbose` 的帮助 | 修改 | 改为 `show every field, the global settings and the keychain details` |
| `src/multi_claude/cli.py` | `_hint_next_step` :697 | 修改 | 启动命令目录不在 PATH 上时追加一条提示 |
| 新增 `src/multi_claude/shellpath.py` | — | 新增 | 按 `$SHELL` 生成“把目录加进 PATH”的一行命令，供 `cli` 与 `doctor` 共用 |
| `src/multi_claude/doctor.py` | `bin-on-path` :126-132 | 修改 | 修复建议改用同一条具体命令 |
| `install.sh` | :222-225 | 修改 | 按 `$SHELL` 给出具体文件与命令（shell 中实现同一规则） |
| `tests/test_accounts.py`、`tests/test_migrate.py`、`tests/test_routes.py`、`tests/test_rename.py`、`tests/test_shared_exclude.py` | 依赖完整表格的断言（全集见下） | 修改 | 改用 `list --verbose` |
| `tests/test_install.py` | `InstallTest` | 新增用例 | §8 T8 |
| `tests/test_help.py` | 新文件 | 新增 | 见 §8 |
| `README.md`、`README.zh-CN.md` | 全文结构 | 修改 | 按 §5.1.4 重排 |
| `CHANGELOG.md` | Unreleased / Changed | 修改 | 条目 |

依赖 `list` 完整表格的断言全集（`grep -rn 'ok("list"\|run_cli("list"' tests/*.py | grep -v -- '--json\|--names'`，不截断）共 12 处：`test_accounts.py:123`、`:190`、`:194`、`:370`、`:375-376`、`:592`，`test_migrate.py:182-184`、`:193`、`:200`，`test_routes.py:279`、`:283-284`，`test_rename.py:115`，`test_shared_exclude.py:87-88`。`test_onboarding.py:40-41` 比较无参数与 `list` 两者输出相同，不受影响。其余调用（`test_accounts.py:198`、`:335`、`:354`、`:681`、`:711`、`:717`，`test_rename.py:96`，`test_shared_exclude.py:125`）只看 stderr 或退出码，`test_rename.py:44`、`:60` 本来就用 `--verbose`。初稿写成 10 处，是枚举命令接了截断导致的漏数，编码时由全量回归发现后更正。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 C1 帮助分组

顶层 `--help` 的 `epilog`：

```text
Getting started:
  add NAME          create an account and its launcher claude-NAME
  login NAME        sign in to an account
  list              show accounts, logins and usage

Account settings:
  proxy, env, args  set the proxy, extra variables or fixed arguments of a launcher
  rename, remove    rename or unregister an account
  migrate-default   turn the existing ~/.claude into an account

Everyday:
  usage             5-hour and 7-day usage of each account
  route, which      choose an account by directory (launcher claude-auto)
  handoff           copy a session to another account
  mcp               run `claude mcp` as an account

Setup and checks:
  init, apply       global settings; converge everything to config.json
  doctor            check the setup and suggest fixes
  statusline        record usage from the status line
  completion        print a shell completion script

Examples:
  multi-claude add work --proxy 7901
  multi-claude login work
  multi-claude route ~/work work
  multi-claude add --help        details of one command
```

分组中的命令必须与解析器里的子命令全集一致，由测试断言（新增子命令时如果忘了写进分组，测试会失败）。

子命令：`sub.add_parser(name, description=<原 help 文字>)`。顶层 `usage` 行保持 `multi-claude [-h] [--version] COMMAND ...`。

#### 5.1.2 C2 `list` 简表

默认输出：

```text
NAME  LOGIN     PROXY                  SHARED  5H         7D       STATUS
work  keychain  http://127.0.0.1:7901  yes     23% 14:00  41% Fri  ok
home  none      inherit                no      -          -        not logged in
```

- **LOGIN**：`identity.probe` 的结果。
- **SHARED**：`_shared_cell`。
- **5H / 7D**：`_window_cell(read_account_usage(...).five_hour / seven_day)`。
- **STATUS**：按以下顺序收集问题，用 `, ` 连接，没有问题时为 `ok`：
  - `directory missing`（`dir_exists` 为假）；
  - `launcher <状态>`（`launcher_status` 不是 `ok`：`missing` / `stale` / `conflict`）；
  - `link <状态>`（default 身份且链接不是 `ok`：`missing` / `broken`）；
  - `not logged in`（LOGIN 为 `none`）。

简表之后照旧输出 `routes:` 一节；STATUS 不全是 `ok` 时，再加一行 `run multi-claude doctor for details`。`warn_launch_settings` 与各项环境告警照旧。

`list --verbose`：输出与现在的默认输出完全相同，包括 `root:` 等三行、完整表格、default 账号的 `.claude.json` 说明和钥匙串详情。

#### 5.1.3 A3 PATH 提示

`shellpath.add_to_path_command(directory, shell, system)` 返回一行命令：

| `$SHELL` | 命令 |
| ---- | ---- |
| zsh | `echo 'export PATH="<目录>:$PATH"' >> ~/.zshrc` |
| bash（macOS） | `echo 'export PATH="<目录>:$PATH"' >> ~/.bash_profile` |
| bash（其它） | `echo 'export PATH="<目录>:$PATH"' >> ~/.bashrc` |
| fish | `fish_add_path <目录>` |
| 其它或未设置 | `None`（调用方退回到通用提示） |

目录里含单引号时返回 `None`（避免生成错误的命令）。

- **`add` 成功后**：`expand(config.bin_dir)` 不在 `PATH` 的各项中时，`info("note: {bin} is not on PATH, so claude-NAME will not be found; run: {命令}, then open a new terminal")`。命令为 `None` 时改为“add it to PATH in your shell profile”。
- **`doctor`**：`bin-on-path` 的建议改用同一条命令。
- **`install.sh`**：用 `case "$(basename "${SHELL:-}")"` 与 `uname` 实现同一张表，输出 `note: … is not on PATH; run: <命令>, then open a new terminal`。

#### 5.1.4 README 重排

中英两版结构一致，各节内容原样移动，只调整标题层级，并在“常见操作”的开头加一句导引：

1. Features / Requirements / Installation / Quick start（不变）
2. **Everyday tasks**（新的 `##`）：
   - Proxy values
   - Environment variables and arguments
   - Shared resources（由 `##` 降为 `###`）
   - Choosing an account by directory
   - Usage
   - Handing a session over to another account
   - Renaming accounts and managing MCP servers
   - Diagnostics and completion
3. Migrating `~/.claude`（标题文字不变，锚点 `#migrating-claude` 保持有效）
4. **Reference**（新的 `##`）：
   - Commands（原命令表，由 `##` 降为 `###`）
   - Default locations
   - Declarative setup with `apply`
   - Logins and account paths（含 Moving an account directory by hand，降为 `####`）
   - Exit codes
5. Uninstalling / Contributing / License（不变）

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `list` 文本输出 | 默认简表；原输出移到 `--verbose` | 人读输出；脚本应使用 `--json`（不变） |
| `--help` 文本 | 分组与示例 | 人读 |
| `install.sh`、`doctor` 的 PATH 提示文字 | 修改 | 人读 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **为每个分组创建 argparse 子解析器组**：argparse 不支持给子命令分组，只能改格式化器内部逻辑，依赖未公开的接口。`epilog` 是公开接口，因此采用 `epilog`。
- **`list` 加 `--brief` 而保持默认不变**：新用户看不到简表，与目标相反，因此不采用。

## 7. 影响分析

**正向**

- 只改人读输出与安装提示；配置、收敛、退出码都不变。
- `list` 默认多读一次每个账号的用量（读本地文件，与 `usage` 相同），没有网络请求。

**反向**

- **测试**：§4 列出的 12 处断言改为 `--verbose`。
- **补全**：取子命令名不依赖 `help`；`test_completion` 全量回归。
- **不在分组里的命令**：测试强制分组覆盖全集；第三批新增 `set` 时要同步写进分组。
- **无参数入口**：`cmd_overview` 调用 `list`，自动变成简表，`test_onboarding` T2 不变（比较的是两者相同）。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 顶层 `--help` | 含四个分组标题与 `Examples:`；分组里出现的命令名集合等于解析器子命令全集；不再出现每行一个子命令的平铺列表 |
| T2 | `add --help`、`login --help` | 含原来的用途说明文字 |
| T3 | `list` 简表 | 表头为 `NAME LOGIN PROXY SHARED 5H 7D STATUS`；没有 `root:` 行；有用量缓存时显示百分比；无登录时 STATUS 为 `not logged in` 并提示 `doctor` |
| T4 | STATUS 各种问题 | 目录缺失、启动命令 stale、default 链接 broken 分别出现在 STATUS 里 |
| T5 | `list --verbose` | 与改动前的默认输出一致（原 10 处断言改用 `--verbose` 后通过） |
| T6 | `shellpath` 单元 | zsh / bash（Darwin、Linux）/ fish / 其它 / 含单引号的目录，逐一核对 |
| T7 | `add` 时 bin 目录不在 PATH | 输出含具体命令（以 zsh 为例）；在 PATH 上时不输出 |
| T8 | `install.sh` 在 PATH 外安装（`SHELL=/bin/zsh`、`/bin/bash`、未设置） | 输出相应的命令；未设置时为通用提示 |
| T9 | `doctor` 的 `bin-on-path` 建议 | 含同一条命令 |
| T10 | README | 两版各级标题顺序一致；`#migrating-claude`、`#迁移-claude` 对应的标题仍存在；两版行数与重排前相差不超过新增的导引行 |
| T11 | 回归 | 现有全部用例通过（基线 267 个） |

## 9. 日志 / 观测点

- **`list`**：STATUS 列；有问题时的 `run multi-claude doctor for details`。
- **PATH 提示**：`add` 与 `install.sh` 的 `note: … is not on PATH …; run: <命令>`。
