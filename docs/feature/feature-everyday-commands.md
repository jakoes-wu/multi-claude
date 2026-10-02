# multi-claude：常用命令 run、path、restore 与两处便利（v0.5.0）

> 2026-10-02 注记：代码已按方案实现（`cli.py` run/path/restore 分派、`_default_migration_name`、`_plan_config_copy`/`_copy_settings`；新模块 `restore.py`；`migrate._busy_check` 增加 command 参数；补全；README 双语、CHANGELOG、roadmap §2.5；`tests/test_everyday.py` 23 例），全量 328 例通过，待独立代码评审与编译机验证。基线：main `050c7fa`（v0.4.0 发布后）。来源：用户 2026-10-02 同意的迭代计划 v0.5.0（`run`、`path`、`migrate-default` 名称可省、`add --config-from`、`restore`、`use`），参考同作者项目 multi-codex 的同名命令。`use` 经源码核对后不在本版实现，原因见 §6。

## 1. 背景

- 以账号身份运行任意命令，目前只能直接用启动命令 `claude-NAME`（只能运行 `claude`），或者 `mcp`、`login` 这两个固定子命令（`cli.py:195-201`）。没有命令能直接打印账号目录。
- `migrate-default` 必须给名称（`cli.py:119`）。
- 新账号的 `settings.json` 只能手工复制。
- `migrate-default` 没有对应的撤销命令；README 只给了手工步骤（`README.md` “Undoing a migration by hand”）。

## 2. 目标 / 非目标

**目标**

1. `run [NAME] [-- COMMAND ...]`：用与 `claude-NAME` 相同的环境运行命令，不给命令时运行 `claude`（带账号的固定参数）；省略 NAME 时按 `claude-auto` 的路由选账号。`path NAME` 打印账号目录。
2. `migrate-default` 名称可省：取 `~/.claude.json` 里登录账号的邮箱；`add NAME --config-from OTHER`：把 OTHER 的 `settings.json` 复制给 NAME 一次。
3. `restore NAME`：撤销 `migrate-default`，把账号目录搬回 `~/.claude`，登录不变；中断后重跑同一命令即可继续。

**非目标**

- 不做 `use`（切换 `~/.claude` 指向的账号），见 §6。
- 不改启动命令的内容，不改 `migrate-default` 的迁移流程。
- `--config-from` 只复制 `settings.json`，不复制 `settings.local.json`、`.claude.json`、凭据或会话。

## 3. 假设与约束

- `default` 身份的账号不设 `CLAUDE_CONFIG_DIR`，登录在不带后缀的钥匙串条目（macOS）或账号目录里的 `.credentials.json`（Linux），全局状态在 `~/.claude.json`（`identity.py:36-47`、`usage.py:57-61`、`migrate.py:3-6`）。`restore` 只搬目录，这三处都不碰，所以登录保持。
- `restore` 要求账号目录与 `~/.claude` 在同一文件系统（用 `os.rename`，原子）；不在时退出 3、不做任何修改，提示手工步骤。
- 邮箱读取只看 `~/.claude.json` 顶层 `oauthAccount.emailAddress`（字符串）；读不到、不是合法账号名时要求用户给名称。仓库源码只读过 `oauthAccount.accountUuid`（`usage.py:7`），`emailAddress` 字段名只有测试样例（`tests/test_usage.py:31`）佐证，属外部事实；2026-10-02 已在本机真实 `~/.claude.json` 上核对键名存在（只列键名，未输出值）。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `050c7fa`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `cli.py` `COMMAND_GROUPS` | :40-64 | 修改 | “Account settings” 的 `migrate-default` 行改为 `migrate-default, restore`；“Everyday” 增加 `run, path` |
| `cli.py` `build_parser` | :117-119 | 修改 | `migrate-default` 的 `name` 改为 `nargs="?"` |
| `cli.py` `build_parser` | :129-130 之后、:226 之前 | 新增 | `add` 增加 `--config-from OTHER`（只在 `add` 上，`set` 不加）；新增 `run`、`path`、`restore` 三个子命令 |
| `cli.py` `_split_args_command` | :264-298 | 修改 | `run` 与 `args` 一样从第一个 `--` 切出命令 |
| `cli.py` `main` | :346-375 | 新增 | `path`、`run` 只读分派（不加写锁；迁移未完成时拒绝，同 `mcp`） |
| `cli.py` `dispatch` | :420-424、:453-466、:534-551 | 修改 / 新增 | `migrate-default` 省略名称；`add --config-from` 预检与复制；`restore` 分派 |
| `cli.py` `_account_process_env` | :635-655 | 修改 | 拆出不检查 `claude` 是否在 PATH 的部分，供 `run` 复用 |
| `cli.py` `_VALUE_OPTIONS` | :812 | 修改 | 加入 `--config-from`，`add --shared work --config-from x` 仍给出“账号名放前面”的提示 |
| `migrate.py` `_busy_check` | :301-356 | 修改 | 增加 `command` 参数（默认 `migrate-default`），报错与提示里的命令名随调用方变化 |
| `src/multi_claude/restore.py` | 新文件 | 新增 | §5.1.4 |
| `completion.py` `POSITIONAL_KINDS`、`NAME_OPTIONS` | :18-21、:26 | 修改 | `run`、`path`、`restore` 第 1 个位置参数补全账号名；`--config-from` 的值补全账号名 |
| `README.md`、`README.zh-CN.md` | 命令表、“Undoing a migration by hand”、Everyday tasks | 修改 | 新命令说明；手工撤销一节改为先介绍 `restore` |
| `CHANGELOG.md` | Unreleased | 修改 | Added |
| `docs/analysis/roadmap.md` | §2.4 之后 | 新增 | §2.5 记录本版与 `use` 的结论 |
| `tests/test_everyday.py` | 新文件 | 新增 | §8 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 `run [NAME] [-- COMMAND ...]`

- 解析：`_split_args_command` 遇到 `run` 时，把第一个 `--` 之后的全部内容作为命令（不经 argparse），之前的部分交给 argparse（`run` 只有可选位置参数 `name`）。没有 `--`、或 `--` 后为空时都视为“没有命令”（运行 claude）。不带 `--` 却给了多个位置参数（如 `run work env`）由 argparse 报错退出 2。
- 选账号：给了 NAME 用 NAME（未登记退出 1）；没给时用 `routes.resolve(config, os.getcwd())`（`routes.py`，与 `claude-auto` / `which` 同一规则）：有账号用该账号；规则或默认账号引用了未登记账号退出 1；没有任何路由时退出 2，提示给出 NAME。选中后在 stderr 打一行 `[multi-claude] run: using account X (route DIR)`（给了 NAME 时不打）。这行用 `print(..., file=sys.stderr)`，不用 `info()`（`actions.py:51-52` 写 stdout，会污染 `run -- cmd | …` 的输出）。
- 环境：复用 `_account_process_env` 的检查（账号目录存在、`default` 身份时 `~/.claude` 仍指向账号目录），再用 `account_env` 构造环境（`env.py:15-29`）。
- 命令：给了命令就运行它；没给就运行 `["claude"] + [launcher.expand_value(a) for a in config.effective_args(account)]`（`config.py:159`、`launcher.py:75`，与启动命令逐项一致，`~/x` 形式的值同样展开）。在新环境的 PATH 里找不到可执行文件时退出 127。
- 执行：`os.execvpe`，进程直接换成目标命令，退出码与信号都由它决定。exec 之前 flush stdout/stderr。exec 本身失败（如 ENOEXEC、权限不足）抛 `OSError`，由 `main` 现有的 `except OSError` 退出 1。

#### 5.1.2 `path NAME`

打印 `accounts.account_dir(config, account.name)`（`accounts.py:40-48`），一行，不做存在性检查以外的处理：目录不存在时照样打印，并退出 1、在 stderr 说明。未登记退出 1。

#### 5.1.3 `migrate-default` 名称可省、`add --config-from`

- `migrate-default` 不给名称时，名称来源按顺序：① 迁移记录里的名称（`migrate.load_journal()`，续跑场景）；② 已有 default 身份账号的名称（`config.default_account()`，`config.py:147`；保证迁移后重复执行仍是“already migrated”，不会因邮箱名不同而报 `source is a link to another location`，`migrate.py:251-252`）；③ 读 `~/.claude.json`，取 `oauthAccount.emailAddress`，用 `validate_name` 校验，通过就作为名称并 `info("using the name X (the email of the login in ~/.claude.json)")`；文件缺失、不是 JSON、没有该字段或校验失败时退出 2：`migrate-default needs a NAME here: <原因>`。之后与给了名称完全相同。
- `add NAME --config-from OTHER`（只读 OTHER、只写 NAME 的 `settings.json`）：
  1. 预检（收敛之前）：读出并按 UTF-8 解码源文件（不是合法 UTF-8 时退出 1，配置与目录都不改），第 2 步直接写入这里读到的文本；OTHER 必须已登记（未登记退出 1，与仓库约定一致，`cli.py:398-402`），且不是 NAME 本身（退出 2）；新配置里 NAME 开启共享、且 `settings.json` 在 `shared_items` 里没有被 `shared_exclude` 排除时退出 3（复制会把共享软链换成普通文件，之后每次收敛都判冲突，`shared.py:72-76`）；`<OTHER 目录>/settings.json` 必须是普通文件（不存在或是软链退出 1：软链说明它是共享条目，复制没有意义）。NAME 的目标 `settings.json`：不存在 → 将复制；是普通文件且内容相同 → 不变；其它（内容不同、是软链或目录）→ 退出 3，提示先移走，**不做任何修改**（包括不登记账号）。
  2. 收敛成功且不是 `--dry-run` 后复制：复制前再用 `entry_kind` 确认目标仍不存在或是内容相同的普通文件，否则不写并告警；按 UTF-8 文本读源文件，用 `fsutil.atomic_write(path, text, mode=stat.S_IMODE(源权限))`（`fsutil.py:64-86`）写入；`--dry-run` 只打印 `would copy`。
  3. 输出 `[multi-claude] copied settings.json from OTHER`；不变时只在 `--verbose` 下输出。

#### 5.1.4 `restore NAME`（新模块 `restore.py`）

记号：S = `~/.claude`（`accounts.default_dir()`），T = 账号目录。

- 前置：
  - 迁移记录存在时一律拒绝（包括 `--dry-run`；`_blocked_by_migration` 对 dry-run 放行，`cli.py:413`，所以 restore 自己再查一次 `migrate.load_journal()`），退出 1。
  - NAME 未登记：若 S 是真实目录、没有任何 default 身份账号，视为已完成（可能是上次在注销中途被杀，也可能只是名字拼错）：用 `orphan_scope={NAME}` 再收敛一次清理残留启动命令，输出 `NAME is not registered; ~/.claude is a real directory, nothing to restore`，确实删掉了残留启动命令时再说明删了哪个文件，退出 0（与 `remove` 对未登记账号的处理一致，`cli.py:523-524`）；否则退出 1（未登记）。
  - `dir` 身份退出 2，说明 `restore` 只撤销 `migrate-default`。
  - T 本身是软链（收敛允许，`accounts.py:195-196`）时退出 3：rename 会搬走软链而不是目录。
- 预检登记冲突：构造去掉该账号的新配置，用 `accounts.plan` 检查；有冲突（例如路由或默认路由引用该账号，`accounts.py:158-163`）退出 3，不做修改。
- 按实际状态决定下一步（不需要事务记录，每一步都可重跑）：

| 状态 | 判断 | 动作 |
| ---- | ---- | ---- |
| A 已迁移 | S 是软链且指向 T，T 是真实目录 | 占用检查 → 删软链 S → `os.rename(T, S)` → 注销 |
| B 删了软链、还没搬 | S 不存在，T 是真实目录 | 占用检查 → `os.rename(T, S)` → 注销 |
| C 搬完、还没注销 | S 是目录且不是软链，T 不存在 | 注销 |
| 其它 | S 指向别处、S 与 T 都是目录、两者都不存在等 | 退出 3，打印 S 与 T 的现状，不做修改 |

- 同一文件系统：A、B 在动手前比较 `os.stat(T).st_dev` 与 `os.stat(dirname(S)).st_dev`，不同则退出 3，打印手工步骤（README 中的同一组命令）。
- 占用检查：复用 `migrate._busy_check(T, command="restore")`（`migrate.py:301-356`，退出 4/1），`--skip-process-check` 跳过。传 T 而不是 S：状态 B 下 S 不存在，按 S 检查会漏掉打开文件、工作目录与 `ide/` 锁（`platform.py:113`、`:487-491`）；状态 A 下 S 的 realpath 就是 T，结果相同。原因同迁移：任何 Claude 进程都会写 `$HOME/.claude`，删掉软链后写入会在 S 处建出真目录，导致 rename 失败。
- 注销：用 `accounts.converge` 删除该账号（与 `remove` 相同，`cli.py:520-527`，orphan 范围只含该账号），启动命令随之删除。账号目录里由本工具建的共享软链保留在 S 里，继续可用。
- 测试注入点：`restore-unlinked`（删软链后）、`restore-moved`（rename 后），用 `_test_hook`（`migrate.py:48-59`），只用 `MULTI_CLAUDE_TEST_CRASH_AT`（`FAIL_AT` 抛的 `InjectedFailure` 不被 `main` 捕获）。收敛本身没有注入点（`accounts.py` 中无 `_test_hook`）；注销中途（写完配置、未删启动命令）的情形在测试里手工构造：先从 `config.json` 删掉该账号、保留 `claude-NAME` 与真实的 `~/.claude`，再重跑 restore。
- `--dry-run`：只打印 restore 自己的三步（`would remove the link S`、`would move T to S`、`would unregister NAME and delete its launcher`），不调用 `accounts.plan` 预览（T 仍在时计划里会出现实际不会执行的删共享软链动作，`accounts.py:118-119`）。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `run [NAME] [-- COMMAND ...]` | 新增 | 新增 |
| `path NAME` | 新增 | 新增 |
| `restore NAME [--skip-process-check] [--dry-run]` | 新增；退出码 0/1/2/3/4 沿用现有契约 | 新增 |
| `migrate-default [NAME]` | 名称变为可省 | 兼容：给名称时行为不变 |
| `add --config-from OTHER` | 新增选项 | 兼容 |
| 补全脚本 | 新命令与选项自动出现（`completion.collect` 读 parser），账号名补全靠 `POSITIONAL_KINDS` 与 `NAME_OPTIONS` | 兼容 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **`use NAME`（本版不做，见 §10）**：multi-codex 的 `use` 改写 `~/.codex` 软链的指向。在 Claude Code 上，直接运行 `claude` 时有三样东西不跟着 `~/.claude` 走：
  1. **钥匙串**：macOS 上的登录是不带后缀的钥匙串条目（`identity.py:44-45`）。
  2. **全局状态**：default 身份用 `~/.claude.json`（`usage.py:59-60`），其中有登录账号信息与用量缓存（`usage.py:89`、`:161`）；MCP 等其它内容未在仓库内核实。
  3. **default 账号的启动命令**：它在 `~/.claude` 不指向自己时拒绝启动（`launcher.py` `_identity_lines`）。

  所以把 `~/.claude` 改指向别的账号后：
  - 直接运行 `claude` 会用 B 账号的目录，配上原账号的 `~/.claude.json`；
  - 在 macOS 上还会用原账号的钥匙串登录，若在那里重新登录，会覆盖原账号的钥匙串条目；
  - `claude-<原账号>` 无法启动。

  Linux 上第 1 点不存在，第 2、3 点仍然成立。用户此前同意的处理方式（macOS 上要求 `--accept-relogin`）只针对第 1 点，不足以避免另外两点，因此本版不做，结论写进 roadmap，等用户决定。
- **`restore` 用事务记录**：状态只有三种、每一步都是单个原子操作，按实际状态判断即可重跑，不需要 `migrate-journal.json` 那一套。
- **`run` 用 `subprocess.call`**：会多一层 Python 进程，Ctrl-C 同时打到两个进程；`exec` 更接近直接运行启动命令。

## 7. 影响分析

- **新命令**：`run`、`path` 只读配置，不加写锁；`run` 用 `exec` 替换进程，不留后台进程。`restore` 加写锁，经 `dispatch`。
- **`migrate-default`**：只在省略名称时多读一次 `~/.claude.json`（只读）；给名称的路径代码不变。
- **`add`**：不带 `--config-from` 时代码路径不变；带它时，预检失败发生在收敛之前，配置与目录都不改。
- **`restore` 对其它账号**：只删除该账号的登记与启动命令；`dir` 身份账号、它们的钥匙串条目与目录都不碰。路由引用它时拒绝（不悄悄删路由）。
- **补全**：`completion.collect` 从 parser 自动收集子命令与选项（`completion.py:45-63`）；新增 `POSITIONAL_KINDS` 条目与 `NAME_OPTIONS` 中的 `--config-from`，让账号名可补全。`test_help` 会核对 `COMMAND_GROUPS` 与子命令一致。
- **运行时**：没有新进程常驻；`run` 的 PATH 查找与 `mcp` 相同。

## 8. 回归测试

**环境**：本机（Python 3.11）与编译机 ubuntu20（Python 3.8.10）各跑 `python3 -m unittest discover -s tests`；全部用例在临时 HOME 中运行（`tests/helpers.py`）。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | `run work`（work 有代理与固定参数，其中一个值是 `~/x`） | 假 claude 收到的参数与环境与 `claude-work` 相同 |
| T2 | `run work -- env`、`run work -- sh -c 'exit 7'`、`run work --`（空命令） | 环境含 `CLAUDE_CONFIG_DIR=<work 目录>`；退出码 7；空命令运行 claude |
| T3 | `run -- env` 在路由目录内 / 路由外有默认 / 无路由；`run work env`（缺 `--`） | 分别选中路由账号、默认账号，stdout 只有命令输出；无路由退出 2；缺 `--` 退出 2 |
| T4 | `run nosuch`、`run work -- no-such-cmd`、default 身份但 `~/.claude` 被改指、迁移未完成时 `run` / `path` | 退出 1、127、1、1 |
| T5 | `path work`、`path nosuch`、目录被删 | 打印目录退出 0；退出 1；打印并退出 1 |
| T6 | `migrate-default`（省略名称）：`~/.claude.json` 有邮箱 / 没有字段 / 不是 JSON / 邮箱不合法 / 有迁移记录（崩溃后续跑）/ 已迁移为另一名称后再执行 | 用邮箱作名称迁移成功；退出 2 未改动（三种）；按记录名续跑成功；输出 already migrated 退出 0 |
| T7 | `add new --config-from work`：复制、重复执行、目标已有不同内容、OTHER 无文件、OTHER 是软链、OTHER=自己、OTHER 未登记、NAME 共享 settings.json、`--dry-run`、`add --shared work --config-from x` | 复制且权限一致；不变；退出 3 且未登记；退出 1；退出 1；退出 2；退出 1；退出 3 未登记；不复制；提示账号名放前面 |
| T8 | `restore main`：正常；重复执行 | `~/.claude` 成为原目录，内容不变，共享软链仍在，账号与启动命令已删除，`~/.claude.json` 未改，假 security 未被调用；重复执行退出 0（already restored） |
| T9 | `restore` 中断：在 `restore-unlinked`、`restore-moved` 处崩溃后重跑；配置已删而启动命令残留 | 重跑退出 0，结果同 T8，残留启动命令被删除 |
| T10 | `restore` 冲突与拒绝：dir 身份账号、路由引用、S 指向别处、S 与 T 都是目录、T 是软链、跨文件系统（测试钩子）、迁移记录存在（含 `--dry-run`） | 分别退出 2、3、3、3、3、3、1，均未改动 |
| T11 | `restore` 占用与跳过：设置 `CLAUDE_CODE_CHILD_SESSION=1`；再加 `--skip-process-check` | 退出 4 且提示里是 restore；加选项后成功 |
| T12 | `restore --dry-run` | 只输出三步计划，未改动 |
| T13 | 帮助与补全 | `--help` 分组含新命令；bash/zsh/fish 补全含 `run`、`path`、`restore`、`--config-from`，`--config-from` 的值补全账号名 |
| T14 | 回归 | 现有 305 个用例全部通过（`migrate-default` 的占用提示文字不变） |

跨文件系统用例需要一个测试钩子：`MULTI_CLAUDE_TEST_RESTORE_CROSS_DEVICE=1` 时把 st_dev 判断视为不同（只在 `MULTI_CLAUDE_TEST_MODE=1` 时生效，与现有钩子一致，`platform.py:43-51`）。

## 9. 日志 / 观测点

- `[multi-claude] run: using account X (route DIR)` / `(default)`
- `[multi-claude] using the name X (the email of the login in ~/.claude.json)`
- `[multi-claude] copied settings.json from OTHER` / `would copy …`
- `[multi-claude] restore: removed link …`、`moved T to S`、`unregistered NAME`；冲突时 `error: restore: …` 带 S 与 T 的现状

## 10. 开放问题

- **`use` 是否做、做成什么样**（2026-10-03 用户决定：选 ①，不做；README 常见问题已说明替代方式）：用户 2026-10-02 同意的计划含 `use`，并同意“macOS 上登录不跟着走时要求 `--accept-relogin`”。源码核对发现另外两处不跟着 `~/.claude` 走（§6 第 2、3 点），这一处理方式不足以避免混用状态，因此本版先不做。可选方向：① 不做，README 说明用 `route --default` 或启动命令代替；② 只做只读的 `use`（显示 `~/.claude` 指向哪个账号）；③ 用户接受上述限制后照 multi-codex 实现并加醒目提示。

