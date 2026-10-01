# multi-claude：会话交接 handoff

> 2026-10-01 注记：plan-review 2 轮收敛，用户同意编码；代码已按方案实现（`src/multi_claude/sessions.py`、`cli.py` `cmd_handoff`），待独立代码评审与编译机验证，未提交。来源：`docs/analysis/competitor-analysis.md` §6.2 第 6 条、§7 表 `handoff` 行。基线：main `a397796`。用户决定（2026-10-01）：
>
> - 复制范围：会话文件和同名目录；
> - 来源账号：自动识别，也可用 `--from` 指定；
> - 复制之后：只打印续聊命令；
> - 目标账号已有同 ID 会话：内容相同时跳过，不同时拒绝，`--force` 先备份再覆盖。

## 1. 背景

Claude Code 把每条会话存成 `<配置目录>/projects/<编码后的工作目录>/<会话ID>.jsonl`。旁边还有一个同名目录，放子代理记录（`subagents/`）和存成外部文件的大段工具输出（`tool-results/`）。各账号的配置目录彼此独立，所以在账号 A 开的会话，用账号 B 的 `claude --resume` 找不到。本机核实的形态见 §3。

## 2. 目标 / 非目标

**目标**

1. `multi-claude handoff TARGET`：把一条会话（会话文件 + 同名目录）从来源账号复制到目标账号的同一项目目录下，再打印 `cd <目录> && claude-<TARGET> --resume <ID>`。
2. 来源账号与会话的默认值：
   - 在 Claude 会话里（用 `!` 运行）时，来源账号取当前账号，会话取当前这条会话；
   - 在普通终端里，必须给 `--from`，会话取当前目录里最新的一条；
   - `--session ID` 可以指定其它会话。
3. 同 ID 冲突：内容相同时跳过（退出 0）；不同时拒绝（退出 3，不改任何文件）；加 `--force` 时先把目标里的旧版本改名备份，再复制。

**非目标**

- 不移动、不删除、不软链来源会话。
- 不复制 `file-history/<ID>`（`/rewind` 用的快照）、项目级 `memory/` 以及其它账号级状态。
- 不直接启动目标账号，也不在两个账号之间共享整个 `projects/`。
- 不改写会话内容（会话 ID、`cwd` 字段原样保留）。

## 3. 假设与约束

以下 Claude Code 行为出自 2.1.286 二进制（`~/.local/share/claude/versions/2.1.286`；压缩后的 JS 名称随版本变化，只作为本次核对的出处）和本机实测：

- **目录名编码**：`function k(e){return e.replace(/[^a-zA-Z0-9]/g,"-")}`；`function tx(e){let n=k(e);if(n.length<=200)return n;return n.slice(0,200)+"-"+Math.abs(EQ(e)).toString(36)}`。超过 200 个字符时会接一段哈希，本方案不复刻这段哈希：目标账号直接沿用来源账号里已有的目录名。
- **会话形态**（本机 `~/.cc/jakoes.wu.us@gmail.com/projects/-Users-jakoes-VSCodeProjects-ai-multi-claude/`）：
  - `<ID>.jsonl`（0600）与同名目录 `<ID>/`（含 `subagents/`、`tool-results/`）；
  - 文件每行是一个 JSON 对象，多数行带 `cwd` 与 `sessionId`，文件以换行结尾；
  - 会话进行中时会继续追加，所以复制时可能读到半行。
- **会话里的环境变量**：Claude 给它启动的子进程设了 `CLAUDECODE=1`、`CLAUDE_CODE_SESSION_ID=<当前会话 ID>` 与 `CLAUDE_CONFIG_DIR`（本机实测：`CLAUDE_CODE_SESSION_ID` 等于当前会话的文件名）。default 身份账号的会话里没有 `CLAUDE_CONFIG_DIR`，但有 `CLAUDECODE`。
- **路径写法**：Claude 编码时用的工作目录写法（物理路径还是 `$PWD`）本次没有核实，所以两种都试：`os.getcwd()`（物理路径），以及与它指向同一目录的 `$PWD`。
- **续聊前提**：会话按工作目录分目录存放（见上方编码），推断 `claude --resume <ID>` 需要在同一工作目录下运行才找得到会话，所以打印的命令带 `cd`。`--resume` 的查找逻辑本次没有读源码核实，由 T14 实机兜底；多带一个 `cd` 在任何情况下都无害。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `a397796`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/sessions.py` | 新文件 | 新增 | 找项目目录、选会话、复制、冲突判定（§5.1.2–§5.1.5） |
| `src/multi_claude/cli.py` | `build_parser` :149-150 之间 | 新增 | `handoff` 子命令 |
| `src/multi_claude/cli.py` | `main` :236-238 之后 | 新增 | 分派 `handoff`：迁移未完成时拒绝，不加写锁（同 `mcp`，:239-244） |
| `src/multi_claude/cli.py` | `cmd_statusline` :799 之前 | 新增 | `cmd_handoff`：参数解析、来源识别、输出 |
| `src/multi_claude/completion.py` | `POSITIONAL_KINDS` :17-22、`NAME_OPTIONS` :26 | 修改 | `handoff` 第 1 个位置参数补账号名；`--from` 补账号名 |
| `tests/test_handoff.py` | 新文件 | 新增 | 见 §8 |
| `tests/test_completion.py` | `SUBCOMMANDS`、bash 用例 | 修改 | 加 `handoff` |
| `README.md`、`README.zh-CN.md` | 命令表（:88-89）、“Renaming accounts and managing MCP servers”一节（:179）之后 | 修改 | 新增命令行与小节 |
| `CHANGELOG.md` | Unreleased / Added | 修改 | 新增条目 |

复用：

- `statusline.account_for`（`src/multi_claude/statusline.py:197`）：按 `CLAUDE_CONFIG_DIR` 认账号；没有这个变量时取 default 身份账号；
- `accounts.account_dir`；
- `fsutil.copy_tree`（`src/multi_claude/fsutil.py:194`）：复制同名目录，跳过 socket 等特殊文件；
- `fsutil.build_manifest` / `diff_manifests`（:154、:230）：比较同名目录是否相同；
- `cli._registered`（:507）：目标账号未登记时退出 1。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 命令行

```text
multi-claude handoff TARGET [--from NAME] [--session ID] [--force] [--dry-run]
```

`--session` 只接受 `[A-Za-z0-9-]+`，防止路径穿越。

#### 5.1.2 来源账号

- **有 `--from`**：用它（未登记时退出 1）。
- **没有 `--from`、但环境里有 `CLAUDECODE`**：调用 `statusline.account_for(config, os.environ.get("CLAUDE_CONFIG_DIR"))`。认不出时报错退出 2，提示用 `--from`。
- **都没有**：报错退出 2，提示 `--from is required outside a Claude session`。不在普通终端里猜 default 账号：这里没有 `CLAUDE_CONFIG_DIR` 不能说明用的是 default 身份。
- **来源与目标是同一个账号**：退出 2。

#### 5.1.3 找项目目录（`sessions.project_dir(account_dir, cwd_variants)`）

1. 候选写法为 `os.getcwd()`，以及与它 `os.path.samefile` 的 `$PWD`，去重。
2. 对每种写法 `p` 计算 `k(p)`：
   - 长度 ≤ 200 时，候选目录是 `<账号目录>/projects/<k(p)>`；
   - 长度 > 200 时，候选目录是 `projects/` 下以 `k(p)[:200] + "-"` 开头的目录。
3. 结果：存在的候选目录恰好一个时就用它，并记下命中它的写法，供 §5.1.6 打印续聊命令；一个都没有时退出 1（`no sessions for <cwd> in <account>`）；多于一个时退出 1，并列出这些目录。

#### 5.1.4 选会话

- **会话 ID**：依次取 `--session`；在会话中、未给 `--from` 或 `--from` 与自动识别结果相同时，取 `CLAUDE_CODE_SESSION_ID`，且只在该目录里存在 `<ID>.jsonl` 时采用；都没有时，取该目录下修改时间最新的 `*.jsonl`。
- **找不到**：退出 1。
- **截掉半行**：读出会话文件的全部字节，截到最后一个 `\n`（含）为止，去掉会话进行中可能追加了一半的尾行。截完为空时退出 1。

#### 5.1.5 复制与冲突

目标项目目录是 `<目标账号目录>/projects/<来源目录名>`，不存在时创建。

**判定**（目标里已有 `<ID>.jsonl` 或 `<ID>/` 时）：

- 目标会话文件的字节与截后的来源字节相同，且两侧同名目录的 `build_manifest` 一致（都不存在也算一致）：`unchanged`，退出 0，不写任何文件。
- 否则没有 `--force` 时为 `conflict`：退出 3，不写任何文件，提示加 `--force`。
- 有 `--force` 时，先把目标里已有的项改名备份，再复制：`<ID>.jsonl` → `<ID>.jsonl.multi-claude-bak.<YYYYmmdd_HHMMSS>`，`<ID>/` → `<ID>.multi-claude-bak.<YYYYmmdd_HHMMSS>`。命名直接调用 `statusline._backup_path`（`src/multi_claude/statusline.py:127`，同秒重名时加序号）。备份文件名不以 `.jsonl` 结尾，不会被 Claude 当成会话列出。

**复制顺序**（会话文件最后落位，它出现就说明会话完整）：

1. 来源有 `<ID>/` 时，先删掉上次中途被杀留下的同名临时目录，再 `copy_tree` 到目标项目目录下的临时名 `.multi-claude-<ID>.tmp`，最后 `os.rename` 成 `<ID>/`。
2. 把截后的字节写到临时文件 `.multi-claude-<ID>.jsonl.tmp`（0600），`fsync` 后 `os.replace` 成 `<ID>.jsonl`。
3. 任一步失败时删除本次的临时项并退出 1。第 1 步已落位的目录保留：同 ID 再次运行时它会参与冲突判定，不会被静默覆盖。

**`--dry-run`**：只打印判定结果与将要做的动作，不创建目录、不备份、不写入。

#### 5.1.6 输出

```text
[multi-claude] handoff: copied session <ID> from <来源> to <目标> (<目标项目目录>)
[multi-claude] backup: <路径>              # 仅 --force 时
[multi-claude] continue with: cd '<cwd>' && claude-<目标> --resume <ID>
```

`unchanged` 时同样打印续聊命令。`<cwd>` 取 §5.1.3 中实际命中项目目录的那种写法（物理路径或 `$PWD`），并用 `shlex.quote`。若用另一种写法 cd，目标账号会按另一个目录名去找会话，就找不到复制过去的那份。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| CLI `multi-claude handoff TARGET [--from NAME] [--session ID] [--force] [--dry-run]` | 新增。退出码：0 已复制或无变化；1 账号未登记、目录或会话不存在、复制失败、迁移未完成；2 用法错误（无法确定来源、来源等于目标、会话 ID 不合法）；3 目标已有不同内容的同 ID 会话 | 新增 |
| 补全 | `handoff` 第 1 个参数和 `--from` 补账号名 | 新增 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **按“当前目录最新一条”选会话**：在会话里运行时，最新的一条可能是别的终端里同时开着的会话。`CLAUDE_CODE_SESSION_ID` 能精确指到当前会话，所以只把“最新一条”留作普通终端里的默认值。
- **复刻超过 200 字符时的哈希**：要复刻 Claude 内部的 `EQ` 哈希，版本一变就可能失效。沿用来源账号里已有的目录名不依赖这段算法，所以不复刻。
- **软链共享会话**：两个账号会同时写同一个文件，用户已选定只复制，不采用。

## 7. 影响分析

**正向**

- 新增的是一个独立命令，不改动现有命令与启动命令。
- 写入只发生在目标账号的 `projects/<目录名>/` 下，来源账号只读。

**反向**

- **来源会话正在写**：截到最后一个换行，复制的是那一刻之前的完整记录；之后在来源账号里继续聊的内容不会同步。再次运行 handoff 时内容已不同，会按冲突处理，需要 `--force`。
- **目标账号正在用同一 ID 的会话**：只有之前交接过、又在目标账号里续聊过才会出现。这时内容不同，默认拒绝；`--force` 会先备份再覆盖，正在写的那个进程之后的追加会落到新文件（改名后的备份保留着旧内容）。README 写明：`--force` 前先关掉目标账号里这条会话。
- **磁盘**：复制量等于会话文件加同名目录。本机本会话约 10 MB + 10 MB。
- **对外语义**：没有改动任何已有命令的输出或退出码。

**部署形态**

- default 身份账号的配置目录就是它的账号目录（`~/.claude` 指向它），`account_dir` 对两种身份都适用。
- 工作目录超过 200 个字符时按前缀找；找到多个时如实报错，不猜。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。会话文件在测试临时 HOME 里构造；“会话里”用环境变量 `CLAUDECODE=1`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CONFIG_DIR` 模拟（与 §3 实测的变量一致）。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 会话里交接当前会话（带同名目录，含子目录与文件） | 退出 0；目标出现逐字节相同的 `<ID>.jsonl`（0600）与同名目录（清单一致）；输出含 `cd '<cwd>' && claude-<目标> --resume <ID>`；来源不变 |
| T2 | default 身份账号的会话里（`CLAUDECODE=1`、无 `CLAUDE_CONFIG_DIR`） | 来源识别为 default 账号 |
| T3 | 普通终端：无 `--from` / 有 `--from` | 前者退出 2 并提示 `--from`；后者取修改时间最新的会话 |
| T4 | `--session` 指定一条非最新的会话；`--session ../x` | 前者复制指定会话；后者退出 2 |
| T5 | 尾行只写了一半 | 目标文件截到最后一个换行；全文无换行时退出 1 |
| T6 | 重复交接（内容相同） | 输出 `unchanged`，退出 0，目标修改时间不变 |
| T7 | 来源追加了内容后再交接 / 加 `--force` | 前者退出 3 且目标不变；后者生成 `*.multi-claude-bak.*` 备份并写入新内容 |
| T8 | `--dry-run`（首次与冲突两种） | 目标侧快照不变，无备份；退出码与实跑判定一致（0 / 3） |
| T9 | 错误：目标未登记、来源等于目标、无项目目录、找不到会话、迁移未完成 | 依次退出 1、2、1、1、1，且不写任何文件 |
| T10 | 工作目录经软链进入（`$PWD` 与 `os.getcwd()` 不同），会话目录按 `$PWD` 编码 | 能找到并复制；续聊命令 cd 到软链写法 |
| T10b | 目标项目目录里残留上次的 `.multi-claude-<ID>.tmp` | 复制成功，复制后不留临时项 |
| T11 | 工作目录编码后超过 200 个字符：来源目录名为 `前缀 + "-" + 任意后缀`，再造两个同前缀目录 | 一个时找到并沿用原名；两个时退出 1 并列出 |
| T12 | 补全 | 三种 shell 脚本含 `handoff`；bash 下 `handoff ` 与 `--from ` 补账号名 |
| T13 | 回归 | 现有全部用例通过（基线 230 个） |
| T14 | 实机（L3，需用户同意） | 在一个账号的会话里 `! multi-claude handoff <另一账号>`，在新终端运行打印出的命令，能看到原对话并继续 |

T1–T13 在编译机上闭合了软件控制流。T14 验证的是真实 Claude Code 能否 resume 复制过来的会话。

## 9. 日志 / 观测点

- **成功路径**：stdout 带 `[multi-claude]` 前缀，依次为 `handoff: copied session <ID> from <来源> to <目标> (<目录>)`、`backup: …`（`--force` 时）、`continue with: …`。
- **无变化**：`handoff: session <ID> is already in <目标> (unchanged)`。
- **冲突**：stderr `error phase=handoff path=<目标会话文件>`，原因 `<目标> already has a different copy of session <ID>; use --force to back it up and replace it`。
- **其它错误**：都带 `phase=handoff` 与相关路径（账号目录、项目目录或会话文件）。
