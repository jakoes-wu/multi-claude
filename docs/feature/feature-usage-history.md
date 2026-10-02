# multi-claude：本地 token 用量历史、最近使用时间与安装提示（v0.7.0）

> 2026-10-03 注记：代码已按方案实现（`history.py`；`cli.py` `cmd_usage_history`、`LAST USED`、安装提示；`doctor.py`；`tests/test_history.py` 14 例），方案评审 M1–M3 与低级意见已处理。基线：main `6909c38`。来源：用户 2026-10-03 同意的迭代计划 v0.7.0。

## 1. 背景

- `usage` 只显示 Claude Code 缓存在 `.claude.json` 里的 5 小时 / 7 天限额百分比（`usage.py:1-8`），没有按天、按模型的 token 用量。
- `list` 的简表（`cli.py:1279-1300`）看不出每个账号最近一次使用是什么时候。
- 找不到 `claude` 时，`mcp`、`login`（`cli.py:800`）、`run` 不给命令时（`cli.py:846` 经 `_exec_as`）、`doctor`（`doctor.py:103`）只说“不在 PATH 上 / 请安装”，没有给出安装方法。

### 1.1 已核实的事实

| 事实 | 依据 |
| ---- | ---- |
| 会话记录在 `<配置目录>/projects/<编码后的工作目录>/<会话ID>.jsonl`，子代理的在 `…/<会话ID>/subagents/agent-*.jsonl` | `sessions.py:3`；2026-10-03 本机目录结构（只看路径） |
| `type == "assistant"` 的行有 `message.id`、`message.model`、`message.usage`；`usage` 含 `input_tokens`、`output_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens` | 本机一份会话记录的键名（只读键名，未读内容） |
| 同一个 `message.id` 在记录里会重复出现（一条回复的多个内容块各写一行），单个 ID 最多重复 19 次；重复 ID 的各项计数按行序只增不减（0 例违反）；同一账号内没有 ID 跨文件出现；跨账号共享的 ID 存在（来自 handoff） | 本机 4 个账号的全部记录（评审时只统计键名与计数） |
| `usage.iterations` 是列表，与顶层计数的关系未核实，不使用 | 同上 |
| 时间戳形如 `2026-09-17T09:53:06.277Z`（UTC） | 同上 |
| `handoff` 会把整份会话（含同名目录）复制到另一个账号 | `sessions.py:143-190` |
| 官方安装命令（macOS、Linux、WSL）：`curl -fsSL https://claude.ai/install.sh \| bash`；其它方式见 `https://code.claude.com/docs/en/setup` | 官方文档 Advanced setup 页（2026-10-03 读取） |

## 2. 目标 / 非目标

**目标**

1. `usage --history [NAME] [--days N] [--by day|model] [--json]`：只读扫描各账号的会话记录，按天或按模型汇总 token 用量。
2. `list` 简表增加 `LAST USED` 列，`list --json` 增加 `last_used`。
3. 找不到 `claude` 时，multi-claude 自己的报错与 `doctor` 附上官方安装命令。

**非目标**

- 不建数据库、不缓存扫描结果、不联网、不读凭据；不估算费用。
- 不统计未登记为账号的目录（例如尚未迁移的 `~/.claude`）。
- 不改启动命令的内容（启动命令里的“claude not found”文字保持不变，否则所有启动命令都会变成过期）。

## 3. 假设与约束

- 会话记录的格式未写入官方文档；任何一行解析失败、字段缺失或类型不符都跳过该行，不报错。
- 同一 ID 的多行，每项计数取最大值（重复行的 `usage` 相同或逐步增大，取最大值不会重复累加）。
- 日期按本机时区换算（与 `usage` 现有的本地时间显示一致）。
- 性能：只解析含 `"usage"` 子串的行；文件 mtime 早于“统计起点减一天”的跳过。依据：文件 mtime 不早于其中任何一行的时间戳（追加与重写都成立，本机 4 个账号 0 例违反；`handoff` 用 `copy2` 保留的 mtime 同样满足），只有 mtime 被人为回拨时才会漏；多留一天余量。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `6909c38`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/history.py` | 新文件 | 新增 | 扫描与汇总（§5.1.1） |
| `cli.py` `build_parser` | :175-178 | 修改 | `usage` 增加 `--history`、`--days`、`--by` |
| `cli.py` `main` | :383-384 | 修改 | `--history` 时分派到 `cmd_usage_history` |
| `cli.py` `cmd_usage` 之后 | :1394 起 | 新增 | `cmd_usage_history` |
| `cli.py` `_print_brief_list` | :1288 | 修改 | 增加 `LAST USED` 列 |
| `cli.py` `_account_entries` | :1342-1366 | 修改 | 增加 `last_used`（ISO 时间或 null） |
| `cli.py` | :800、`_exec_as`（:849 起）、`cmd_run` :846 | 修改 | 找不到 `claude` 时附安装提示（`_CLAUDE_NOT_FOUND_HINT`） |
| `doctor.py` | :103 | 修改 | 同上 |
| `README.md`、`README.zh-CN.md` | Usage / 用量（:199 起）、命令表 | 修改 | 用量历史说明、`LAST USED` |
| `CHANGELOG.md`、`docs/analysis/roadmap.md` | Unreleased、§2.6 之后 | 修改 / 新增 | Added；§2.7 |
| `tests/test_history.py` | 新文件 | 新增 | §8 |
| `tests/test_help.py` | :46、:48 | 修改 | 简表表头与数据行加上 `LAST USED` 列 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 `history.py`

- `scan(account_dir, since_utc) -> (Dict[message_id, Reply], 跳过的文件数)`：遍历 `projects/` 下所有 `*.jsonl`（含 `subagents/`），跳过 mtime 早于 `since_utc - 1 天` 的文件；逐行：不含 `"usage"` 子串跳过；`json.loads` 失败跳过；`type != "assistant"`、`message.id` 不是字符串、`usage` 不是对象跳过；`model` 为 `<synthetic>` 跳过，缺失或不是字符串时记为 `unknown`；计数不是非负整数时按 0；时间戳解析失败跳过。**先**按 ID 合并（四项取最大值、时间取最早），**再**按最早时间过滤掉早于 `since_utc` 的回复（跨过起点的回复按其最早时间算在窗口外）。
- 失败路径：`projects/` 不存在视为没有数据；`os.walk` 用默认的 `onerror=None`，忽略读不了的目录；单个文件 `stat`/`open`/读取时 `OSError`（扫描中被删除、没有读权限）跳过并计数，命令在 stderr 告警 `skipped N session file(s) that could not be read`，退出码仍为 0。
- `Reply`：`timestamp`（UTC）、`model`、`input`、`output`、`cache_read`、`cache_write`。
- `summarize(replies, by, tz) -> List[Row]`：按（本地日期）或（模型）分组，累加四项与回复数。本地日期逐条用 `astimezone()` 换算，跨夏令时也不会分错天。
- 总计：跨账号按 `message.id` 去重（`handoff` 复制过的回复在两个账号里都有）。每个账号自己的行仍各自计入，README 说明“交接过的会话在两个账号里都会出现，总计只算一次”。

#### 5.1.2 `usage --history`

- 选项：`--days N`（默认 7，范围 1–366，超出退出 2）、`--by day|model`（默认 day）、`NAME`（只看一个账号，未登记退出 1）、`--json`。`--days`、`--by` 没有 `--history` 时退出 2。
- 统计起点：本地今天 0 点往前 `N-1` 天。
- 文本输出：每个账号一段，表头 `DATE`（或 `MODEL`）、`INPUT`、`OUTPUT`、`CACHE READ`、`CACHE WRITE`、`REPLIES`，数字千分位；最后一段 `TOTAL`（跨账号去重）。没有数据的账号输出 `no replies in the last N days`。末尾一行说明：数据来自本机会话记录，不含其它机器、不含已删除的会话，不联网、不读凭据。
- JSON：`{"schema_version":1,"days":N,"by":"day","accounts":[{"name":…,"rows":[{"key":…,"input":…,"output":…,"cache_read":…,"cache_write":…,"replies":…}]}],"total":[…]}`。
- 只读：不加写锁，迁移未完成时照常运行（与 `usage` 相同）。没有 `config.json` 时，文本输出 `no configuration yet …`，`--json` 输出空的 `accounts`，退出 0。

#### 5.1.3 `LAST USED`

- `last_used(account_dir)`：取 `history.jsonl` 与 `projects/*/*.jsonl`（只看第一层会话文件，不进 `subagents/`）中最大的 mtime；都没有时为 None。
- 简表列在 `STATUS` 之前，显示 `_format_age`（`cli.py:1456`）的结果，如 `3h ago`；None 显示 `-`。`--json` 给 ISO 8601 UTC 字符串或 null。
- `handoff` 复制会话会刷新目标账号的这个时间（README 说明）。

#### 5.1.4 安装提示

提示只在要运行的命令是 `claude` 时附上（`command[0] == "claude"`，包括 `run work -- claude`）。账号用 `env` 改过 `PATH` 时，这条提示可能误导（claude 其实装了），接受，不另加判断。

`_CLAUDE_NOT_FOUND_HINT = "; install Claude Code: curl -fsSL https://claude.ai/install.sh | bash (other ways: https://code.claude.com/docs/en/setup)"`，用于 `cli.py:800`、`run` 不给命令时（`_exec_as` 的 `not_found_hint`，仅当命令是 `claude`）、`doctor.py:103`。`code` 的提示不变。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `usage --history [--days N] [--by day\|model]` | 新增 | 新增；不带 `--history` 时行为不变 |
| `list` 简表 | 新增 `LAST USED` 列 | 人读输出变化 |
| `list --json` | 每个账号新增 `last_used` | 新增字段，`schema_version` 不变 |
| 报错文字 | 找不到 claude 时多一段提示 | 人读 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **建 SQLite 历史库**：能保留已删除会话的数据，但多了状态、迁移与一致性问题；本工具不保存派生数据，不采用。
- **读 `stats-cache.json`**：格式未知且可能过期；不采用。
- **`LAST USED` 用目录 mtime**：目录 mtime 只在增删条目时变化，不可靠；不采用。

## 7. 影响分析

- `usage` 不带 `--history`、`list --verbose` 的输出不变；简表多一列。
- 运行时：`usage --history` 读本机会话记录，本机账号约 150 个文件、2.5 万行，单次扫描为秒级以内；mtime 过滤跳过旧文件。`list` 的 `LAST USED` 只对 `projects/*/*.jsonl` 做 stat，不读内容。
- 隐私：读取会话记录文件，但只解析计数字段，不输出、不保存任何内容；不联网。README 写明。
- 报错文字变长；退出码不变（mcp/login 127、run 127、doctor error）。

## 8. 回归测试

**环境**：本机（Python 3.11）与编译机 ubuntu20（Python 3.8.10）全量 `unittest`；本地 `pyflakes src tests` 与 tests 路径检查。测试用合成会话记录。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 两个账号的合成记录（含同 ID 重复行、子代理文件、`<synthetic>`、坏行、非 assistant 行） | 按天汇总的四项与回复数等于预期；重复行不重复累加；坏行被跳过 |
| T2 | `--by model` | 按模型汇总正确 |
| T3 | `--days 1` 与早于起点的记录、mtime 很旧的文件 | 只统计窗口内的回复 |
| T4 | 同一回复在两个账号中（模拟 handoff） | 两个账号各计一次，TOTAL 只计一次 |
| T5 | `--json` | 结构与数值正确 |
| T6 | 参数错误：`--days 0`、`--days 400`、`--days abc`、`--by x`、未带 `--history` 用 `--days` 或 `--by`、`usage --history nosuch` | 退出 2、2、2、2、2、1 |
| T7 | 账号没有任何记录；没有 `config.json` | 输出 `no replies in the last 7 days`；输出 `no configuration yet`、`--json` 的 `accounts` 为空；均退出 0 |
| T7b | 读不了的会话文件；跨过统计起点的回复；缺少 `model`；`TZ=Asia/Shanghai` 下 UTC 15:30 与 16:30 的回复 | 告警 `skipped 1 session file` 且其余照常；该回复不计入窗口；记为 `unknown`；分在两天 |
| T8 | `list`：有 `history.jsonl` 与会话文件 / 都没有 | `LAST USED` 显示相对时间 / `-`；`list --json` 的 `last_used` 为 ISO 字符串 / null |
| T9 | PATH 中没有 claude：`login`、`mcp`、`run work`、`doctor`；反例 `run work -- nosuch`、`code work` | 前四个报错含 `curl -fsSL https://claude.ai/install.sh`，退出码 127、127、127、1；反例不含该提示 |
| T9b | `list --verbose` | 不含 `LAST USED`（输出不变） |
| T10 | 回归 | 现有 340 个用例全部通过（`test_help.py:46`、`:48` 的表头与数据行断言同步加上 `LAST USED`） |

## 9. 日志 / 观测点

- `usage --history` 末尾说明行：`Counted from the session files on this machine …`
- 报错：`error: claude not found in PATH; install Claude Code: curl -fsSL https://claude.ai/install.sh | bash (…)`
