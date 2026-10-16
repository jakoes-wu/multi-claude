# multi-claude：跨账号查询已发布的 artifact `artifacts`

> 2026-10-15 注记：方案已定、代码待落地。基线：main `b9be958`（v0.11.1）。本方案是用户选定的“B 方案”（本地离线查询 + handoff 交接记录）；联网核对所有者的 `--online`（C 方案）另写方案、单独发版，不在本文范围内。

## 1. 背景

用户 2026-10-15 提出（原话）：“目前不同的账号都会生成自己的 artifact……依然记不清某个 artifact 在哪个账号下，以及路径是什么”，要求“模拟 multi-claude sessions，增加 multi-claude artifacts 子命令，查询不同账号下生成的 artifact”。可行性评估后，用户选定“先做 B，再做 C，分别发版”。

### 1.1 已核实的事实

数据取自 2026-10-15 本机 5 个登记账号的会话记录（共 1497 个 `.jsonl`、约 3.5 GB）。统计脚本只看键名、类型、计数与 URL，不输出对话内容。

| 事实 | 依据 |
| ---- | ---- |
| Claude Code 的 Artifact 工具每次调用，在会话记录里写一行 assistant 记录（`message.content` 里的 `tool_use` 块，`name` 为 `Artifact`，`input` 含 `file_path` / `description` / `url` / `action` 等）和一行 user 记录（`tool_result` 块，`tool_use_id` 对应前者） | 本机统计，各动作共 249 次调用 |
| 发布成功时，user 记录顶层的 `toolUseResult` 是对象，含 `url`、`artifact_id`、`title`、`path`（本地源文件）、`version`、`seq`、`updated`（是否为更新已有 artifact）、`audience`（`owner` / `public` / null） | 本机 180 次发布全部带这些键（另有 1 次 `type_url` 创建也带 `url`、`title`） |
| `tool_result` 文本首行形如 `Published <源文件> at <URL> (Version N…)`，失败时 `is_error` 为真、无 `url` | 本机格式枚举：3 次失败，均为 `is_error: true` |
| `quickstart` / `read` / `list` 动作的 `toolUseResult` 没有 `artifact_id` 与 `path` 同时出现的形态 | 本机格式枚举 |
| 会话记录行没有任何账号字段（顶层键只有 `cwd`、`sessionId`、`timestamp`、`uuid`、`version` 等） | 本机键名统计 |
| `handoff` 把来源会话文件（截到最后一个换行）与同名目录整份复制到目标账号同名项目目录 | `sessions.py:153-200`、`sessions.py:115-128` |
| 因此 handoff 过的会话里，交接前的发布记录在两个账号各有一份，内容逐字节相同。本机 52 个不同 URL 中 16 个同时出现在两个账号 | 本机统计 |
| 当前账号（wujianchao316）用 Artifact 工具 `list` 查到服务端 18 个 artifact：本地记录找到其中 16 个；本地记在该账号下、但服务端没有的 16 个，全部同时出现在别的账号（即 handoff 副本） | 2026-10-15 本会话实测 |
| 本地源文件多在 `/private/tmp/claude-501/.../scratchpad`，68 条发布记录里只剩约 20 个文件仍在 | 本机统计 |
| 子代理记录在 `projects/<项目>/<会话ID>/subagents/*.jsonl`，随 handoff 的同名目录一起复制 | 本机目录结构；`sessions.py:173-182` |
| 只读含 `"Artifact"` 或 `claude.ai/` 字节的行再解析 JSON，全量扫描 3.5 GB 耗时约 7.8 秒（本机，文件已在缓存中） | 原型脚本实测 |
| 改名账号不搬目录（目录名取账号的 `dir`） | `accounts.py:40-48` |
| 工具状态目录 `~/.config/multi-claude`（或 `$XDG_CONFIG_HOME/multi-claude`） | `platform.py:70-74` |
| `handoff` 不加写锁，迁移未完成时拒绝 | `cli.py:441-447` |

## 2. 目标 / 非目标

**目标**

1. 新增只读子命令 `multi-claude artifacts [NAME] [--grep TEXT] [--limit N] [--path] [--cmd] [--json]`：从本机所有账号（含未登记的 `~/.claude`）的会话记录里找出已发布的 artifact，每个 URL 一行，给出标题、所属账号、最后发布时间、URL；`--cmd` 给出回到发布它的那个会话的续聊命令；`--json` 给出完整字段（含本地源文件路径、项目目录、会话 ID）。
2. `handoff` 每次实际复制后，在工具状态目录追加一条交接记录；`artifacts` 用它判断交接前的发布记录属于来源账号，从而把 handoff 副本里的发布记录归回真正的所有者。
3. 没有交接记录可用时（本功能上线前的历史 handoff），同一 URL 出现在多个账号的，列出全部账号并标明“所有者不确定”。

**非目标**

- 不联网、不读凭据、不调用 Claude（`--online` 留给 C 方案）。
- 不查网页 / 桌面端发布的、会话文件已删除的、其它机器上的 artifact（本地没有记录）。
- 不利用会话记录里 Artifact `list` 动作的结果做归属判定（本机只有 2 次，收益小，C 方案会直接拿到权威清单）。
- 不改 `sessions`、`usage`、`list` 的行为与输出。
- 不做扫描结果缓存（见 §10）。
- 不扫 `handoff --force` 留下的备份（`<会话ID>.jsonl.multi-claude-bak.*`、`<会话ID>.multi-claude-bak.*/`）与中途被杀留下的临时目录（`.multi-claude-<ID>.tmp/`）：备份不是能续聊的会话。已知后果：A→B 后 A 又发布了新 artifact、再 B→A `--force`，A 那几次发布只留在备份里，`artifacts` 列不出来（写进 README 已知限制）。

## 3. 假设与约束

- 会话记录格式未写入官方文档。解析失败、字段缺失或类型不符的行一律跳过，不报错；`toolUseResult` 缺失时不从文本兜底解析（本机 180/180 都有，兜底只会引入第二套规则）。
- 交接记录只在 handoff 复制完成后写；计算大小与哈希、加锁、写入任一步失败都只警告，不影响 handoff 的退出码（会话已经复制成功）。
- 归属判定依赖“目标会话文件的开头仍是当时复制进去的字节”。Claude 是否只在末尾追加会话文件未在官方文档中写明；本方案用交接记录里的前缀哈希校验，校验不过就按“所有者不确定”处理。
- 交接记录从本功能上线后的第一次 handoff 才开始有。上线前的交接没有记录，追链会停在错误的账号上（例：上线前 A→B，上线后 B→A `--force`，A 的原件被改名成备份、不扫，A、B 两个副本都追到 B）。所以 §5.1.3 加了时间闸：发布早于开始记录时刻、又出现在多个账号里的记录，一律判不确定。代价是这类发布即使后来全部经过有记录的交接，也只能标不确定；`T0` 取第一条记录的时间，所以升级后第一次 handoff 之前写下的发布（包括这第一次交接所复制的会话里的发布）以后再交接，同样只能标不确定。不另写启动标记把 `T0` 提前：标记只能由 `handoff` 或 `artifacts` 在运行时写，`artifacts` 是只读查询，不该写状态，而且对升级后的第一次交接没有帮助。
- 交接记录写入失败（只警告，见上条）后，缺的那一环同样会让追链停错账号，而且时间闸挡不住（发布晚于开始记录时刻）。这种情况下该会话的归属可能误判；警告文字写明这一点（§9）。
- 账号按账号目录的真实路径（`os.path.realpath`）对应交接记录。`migrate-default` 之前对 `~/.claude` 的交接记录，迁移后真实路径变了，对应不上，按“所有者不确定”处理（已知限制，写进 README）。

## 4. 涉及模块

| 区域 | 行号锚点（main `b9be958`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/artifacts.py` | 新文件 | 新增 | 扫描发布记录、读写交接记录、归属判定（§5.1.1–§5.1.4） |
| `src/multi_claude/cli.py` 子命令表 | `:58-59` | 新增 | 在 `sessions` 之后加 `("artifacts", "find the artifacts you published, across accounts")`，与 `sessions` 同在 Everyday 组 |
| `src/multi_claude/cli.py` 参数定义 | `:282-296` 之后 | 新增 | `p_artifacts`，description 为 `list the artifacts published from the Claude sessions of every account on this machine, newest first: artifacts [NAME] [--grep TEXT] [--limit N] [--path] [--cmd] [--json]`；参数 `name`、`--grep`、`--limit`、`--path`、`--cmd`、`--json` |
| `src/multi_claude/cli.py` 分派 | `:433-435` 之后 | 新增 | `artifacts` 只读，不加写锁、迁移未完成也放行 |
| `src/multi_claude/cli.py` `cmd_handoff` | `:1735-1737` | 修改 | `execute` 成功后调用 `artifacts.record_handoff(handoff, 来源目录, 目标目录)`，包在 `try/except (OSError, ValueError)` 里，失败只 `warn`；`execute` 本身不改 |
| `src/multi_claude/cli.py` `cmd_artifacts` | `_resume_command`（`:1869`）之前 | 新增 | 输出逻辑（§5.1.5），复用 `_session_sources`（`:1763`）、`_resume_command`、`_aligned`（`:1912`）、`_local_minute`（`:1923`）、`_iso`（`:1593`）、`_print_json`（`:1523`） |
| `src/multi_claude/completion.py` | `:23` | 修改 | 补全表加 `"artifacts": ["name"]` |
| `tests/test_artifacts.py` | 新文件 | 新增 | §8 各项 |
| `tests/test_handoff.py` | 不改 | — | handoff 写交接记录的用例（§8 R6–R9g）与归属用例放在一起，见 `tests/test_artifacts.py` 的 `OwnershipTest`、`HandoffLogTest`；现有 handoff 用例原样通过即为回归（R13） |
| `tests/test_completion.py` | `:10-12` `SUBCOMMANDS` | 修改 | 加 `"artifacts"` |
| `tests/test_help.py` | `:30` 之后 | 新增 | `artifacts --help` 含 description 关键句 |
| `README.md` / `README.zh-CN.md` | `Finding a session` 节（`README.md:307-326`）之后；命令表 `:501` 之后 | 新增 | 新节 `Finding an artifact` 与命令表一行；handoff 节（`:330-342`）补一句“会写交接记录” |
| `SECURITY.md` | `:36-44`（工作区有无关未提交改动，行号以提交时为准） | 修改 | 说明 `artifacts` 会打印 artifact 标题、URL、路径；`handoff` 会在状态目录写交接记录 |
| `CHANGELOG.md` | `[Unreleased]`（`:7`） | 新增 | `### Added` 两条 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 扫描发布记录

`artifacts.scan(account_dir) -> List[Publish]`：遍历 `<账号目录>/projects/<项目目录>/`，只认两种 `relpath`（相对项目目录）：主会话 `<SID>.jsonl`，与子代理 `<SID>/subagents/<名>.jsonl`（`<名>` 以 `.jsonl` 结尾、是常规文件）。`<SID>` 必须过 `sessions.SESSION_ID_PATTERN.fullmatch` 且不以 `-` 开头（与 `sessions.py:255-261` 同一道校验，否则拼进续聊命令会被 claude 当成选项）；其余文件与目录（备份、临时目录、其它层级）一律不看。不照抄 `history._session_files` 的 `os.walk`（它不过滤，会把备份目录里的子代理文件收进来）。

逐行二进制读取，记录每行的起始字节偏移；只解析含 `b'"Artifact"'` 或 `b'claude.ai/'` 的行：

1. assistant 行：`message.content` 里 `type == "tool_use"` 且 `name == "Artifact"` 的块，记 `id -> input`。
2. user 行：`message.content` 里 `type == "tool_result"`、`tool_use_id` 在上表中、`is_error` 不为真的块；同时顶层 `toolUseResult` 是对象，`url` 用 `^https://claude\.ai/(code/)?artifact/[A-Za-z0-9_-]+$` 做 fullmatch（不用 match，防结尾换行）。
3. 满足以上两条的，产出一条 `Publish`：

```python
class Publish(NamedTuple):
    url: str
    title: str            # toolUseResult.title，非字符串时为 ""
    path: Optional[str]   # toolUseResult.path（本地源文件）
    audience: Optional[str]
    description: str      # tool_use.input.description
    timestamp: Optional[datetime]  # 该 user 行的 timestamp（UTC）
    cwd: Optional[str]    # 该 user 行的 cwd，过 sessions._usable_cwd
    project: str          # 项目目录名
    relpath: str
    session_id: str       # relpath 第一段去掉 .jsonl
    offset: int           # 该 user 行的起始字节偏移
    record_id: str        # 该 user 行的 uuid；缺失时用 "<url>|<timestamp>"
```

调用顺序：先 `load_handoffs()`，从中算出每个 `(目标账号目录, project, relpath)` 最新一条交接记录的前缀长度，再 `scan(account_dir, prefixes)`。`prefixes` 是 `{(project, relpath): 长度}` 单值映射；扫描时边读边算该长度前缀的 SHA-256（不另外读一遍），文件短于该长度时记为不匹配。

#### 5.1.2 交接记录

文件：`<状态目录>/handoffs.jsonl`，权限 0600，一次交接一行。单行可能有几十 KB（本机单个会话最多 220 个子代理文件），不能指望一次 `write` 原子：用 `os.open(..., O_RDWR|O_APPEND|O_CREAT, 0o600)` 打开（要读末字节，不能只写）后 `fcntl.flock(LOCK_EX)`（阻塞等待，与 `lock.py` 同一机制，但锁的是本文件，不是工具写锁），加锁后先用 `os.fstat` 取大小、`os.pread(fd, 1, size-1)` 看文件末字节：文件非空且末字节不是 `\n`（上次被杀留下了半行），先补写一个 `\n`，免得新记录接在半行后面一起变成坏行；然后循环 `os.write` 直到整行写完，再 `fsync`、解锁。半行本身在读取时按坏行跳过。格式：

```json
{"v": 1, "time": "2026-10-15T08:00:00.123456Z", "session_id": "…", "project": "<项目目录名>",
 "source_dir": "<来源账号目录真实路径>", "target_dir": "<目标账号目录真实路径>",
 "files": {"<会话ID>.jsonl": [12345, "<sha256>"], "<会话ID>/subagents/a.jsonl": [678, "<sha256>"]}}
```

`files` 覆盖复制进目标的主会话文件和同名目录下符合 §5.1.1 的子代理文件。主会话文件的大小与哈希取 `handoff.content`（`sessions.py:119-124` 已在内存中）；子代理文件读复制落位后的目标文件（来源可能仍在被写，取来源会与目标对不上）。这些计算都在 `artifacts.record_handoff` 里做，不进 `execute`。`time` 只用于展示；交接的先后一律按文件里的行序（追加顺序），不比时间，避免同一秒内两次交接或时钟回拨排错顺序。

只有实际复制（`COPIED`，或 `CONFLICT` 加 `--force`）时写；`UNCHANGED` 与 `--dry-run` 不写（`cli.py:1727-1733` 这两种情况不调用 `execute`）。

读取：`artifacts.load_handoffs()` 按行序返回记录（行号即 `seq`），坏行、`v` 不是 1、字段类型不符的跳过并计数；文件不存在视为空。另返回开始记录时刻 `T0` = 有效记录里最早的 `time`（没有有效记录时 `T0 = +∞`）。第一条记录就是上线后的第一次 handoff，所以不需要另写启动标记。

#### 5.1.3 归属判定

对每条 `Publish`（来自账号目录 A）：

```text
owner(A, project, relpath, offset, before=+∞, top=True):
  e = 交接记录中 target_dir == realpath(A)、project、relpath ∈ files、seq < before 的 seq 最大的一条
  if e 不存在 or offset >= e.files[relpath].size:   return A          # 在 A 里实时写下的
  if top and 当前文件前 size 字节的 SHA-256 != e.files[relpath].sha: return 不确定
  return owner(e.source_dir, project, relpath, offset, e.seq, top=False)  # 复制来的，字节偏移不变，沿链追溯
```

复制是逐字节的，所以同一条记录在来源文件里的偏移与在目标里相同，可以沿链往前追（A→B→A 加 `--force` 这种来回交接也能追到最早写下它的账号）。只校验当前文件；链上更早的环节按记录可信。`source_dir` 不对应任何当前账号目录（账号已删除，或 `migrate-default` 前的 `~/.claude`）时，该副本判为不确定，不把路径当成账号名输出。

每个副本（某账号目录里的某一行）各算一次 owner。然后按 `record_id` 合并副本：该记录的 owner 集合 = 各副本判出的账号的并集；若并集为空（所有副本都不确定），该记录为不确定。

时间闸（对应 §3）：一条记录的发布时间早于 `T0`（或缺时间），且它的副本出现在两个及以上账号目录里，该记录判为不确定，不论追链结果。理由：`T0` 之后能把一条记录复制进别的账号的交接一定有记录，追链可信；`T0` 之前的交接可能没有记录，追链终点不可信。只出现在一个账号里的早期记录照常判该账号。

再按 URL 归组：

- 每个 URL 的所有者 = 其各条记录的 owner 去掉“不确定”后的集合。
- 若所有记录都能确定、且集合只有一个账号：`ownership = "single"`（副本全在所有者账号里）或 `"recorded"`（副本出现在别的账号、或所有者账号里已没有副本，靠交接记录判定）。
- 否则（有记录判不出，或判出多个账号）：`ownership = "uncertain"`，`accounts` 列出出现过该 URL 的全部账号。

#### 5.1.4 每个 artifact 的汇总字段

同一 URL 的记录按时间排序后取：

- `title`：最后一条非空标题；`description`：最后一条非空说明；均来自会话内容，输出前过 `sessions.clean_title`。
- `first_published` / `last_published`：最早 / 最晚时间戳；`publishes`：去重后的记录数。
- `directory`、`project`、`session_id`、`path`、`audience`、续聊命令：统一取“所有者账号目录里时间最晚的那个副本”；所有者不确定时取全局时间最晚的副本（时间相同按账号登记顺序取第一个）。所有者账号里已没有副本时（交接后删了原会话），退回全部副本里时间最晚的那个，续聊命令指向还在的副本。下文“代表副本”均指它。
- `path_exists`：`os.path.isfile(path)`。

#### 5.1.5 `cmd_artifacts`

结构照 `cmd_sessions`（`cli.py:1775-1866`）：

- `NAME`：校验与“未登记”报错同 `cmd_sessions`（`:1789-1801`）；过滤条件是“所有者是该账号，或所有者不确定且该账号目录里有副本”。扫描仍覆盖全部账号（归属判定需要）。
- `--grep`：不区分大小写，匹配标题、说明、URL、目录（或项目目录名）、源文件路径、会话 ID。
- 排序：`last_published` 新的在前，再按 URL。`--limit` 规则同 `sessions`：文本默认 30，`--json` 默认全部，0 为全部，负数报用法错误。
- 文本表头 `TITLE  ACCOUNT  UPDATED  URL`；`ACCOUNT` 在 `uncertain` 时写成各账号用 `,` 连接再加 ` ?`（如 `us,icloud ?`），表下加一行说明 `? = owner unknown: the session was copied with handoff before multi-claude recorded handoffs`。
- `--cmd`：每行下方缩进打印续聊命令。复用 `_resume_command` 不改它：用代表副本所在账号的 `_SessionSource`，并用代表副本的真实值构造 `sessions.SessionInfo(session_id, directory, project, "", TITLE_NONE, None, last_published 或 epoch, 主会话文件路径)`（`_resume_command` 只读 `session_id` 与 `directory`，`cli.py:1874-1878`）。子代理里发布的，`session_id` 是父会话 ID。`directory` 为 None 时同 `sessions` 追加 `(start directory unknown; project …)`。不做 `sessions --cmd` 那样的宽表合并（URL 列已占宽）。
- `--path`（2026-10-15 用户追加）：每行下方缩进打印代表副本的 `path`（过 `clean_title`），文件不存在时追加 `  (missing)`，没有路径时打印 `  (no source file)`；与 `--cmd` 同时给时路径在前。只影响文本输出。
- `--json`：`{"schema_version": 1, "artifacts": [...], "total": N, "skipped": K}`，每项含 `url`、`title`、`description`、`accounts`、`ownership`、`first_published`、`last_published`、`publishes`、`directory`、`project`、`session_id`、`path`、`path_exists`、`audience`、`resume`。
- 读不了的文件计入 `skipped` 并 `warn`，同 `sessions`；没有结果打印 `no artifacts found`，退出 0。

### 5.2 接口变更

- 新增子命令 `artifacts`（参数见 §5.1.5），退出码：0 成功；2 用法错误；1 账号未登记。
- 新增状态文件 `<状态目录>/handoffs.jsonl`（格式见 §5.1.2，`v: 1`）。
- `handoff` 的命令行、输出与退出码不变；交接记录写失败时多一行 `warning:`。
- 无 `docs/reference/*` 改动，不适用 sibling 回补检查。

## 6. 备选方案与决策

| 方案 | 结论 |
| ---- | ---- |
| A：不记交接，只把多账号出现的 URL 标为不确定 | 作为无交接记录时的兜底保留；单用它无法消除以后的 handoff 副本 |
| 交接记录写成目标会话旁边的附属文件 | 否决：附属文件会被下一次 `--force` 交接的备份 / 覆盖逻辑牵连，追链时要读多个账号目录；集中一个文件更简单 |
| 交接时直接把“副本里每条发布记录的所有者”算好写进记录 | 否决：handoff 要额外通读会话、和 artifact 解析耦合；按字节偏移追链只需大小与哈希 |
| 用文件创建时间判断谁是副本 | 否决：Linux 上 Python 3.8 取不到创建时间，且来回交接时判断错误 |

## 7. 影响分析

- **`handoff`**：成功后多一次子代理文件哈希计算与一次加文件锁的追加写。哈希读的是刚复制的目标文件，量级与复制本身相同；任何一步失败只警告，不改退出码，续聊命令照常打印（R9）。两个 handoff 并发时在 `handoffs.jsonl` 的文件锁上排队，不碰工具写锁（`cli.py:446` 仍不加写锁）。`execute` 不改。
- **会话文件**：`artifacts` 只读，不改任何会话文件；交接记录不写进 Claude 的目录，Claude 不会读到它。
- **其它命令**：`sessions` / `usage` / `list` 不读交接记录，行为不变。`completion` 多一个子命令。
- **运行时**：`artifacts` 每次全量扫描，本机约 8 秒、单线程、内存只保留发布记录（几百条）；不联网。
- **对外语义**：新命令会打印 artifact 标题与说明（由 Claude 写在会话里，属对话内容片段）和本地路径，`SECURITY.md` 同步说明。交接记录含会话 ID、项目目录名、账号目录、文件哈希，不含对话内容，权限 0600。
- **部署形态**：未登记 `~/.claude` 照 `_session_sources` 纳入；无配置文件时也能运行（只扫默认目录）。

## 8. 回归测试

本机 macOS 与编译机（Ubuntu 20、Python 3.8，源码快照 + 一次性容器或宿主 venv）各跑一遍全量 `python -m pytest`。

R6、R7、R9b、R11b、R9f 都先写入一条时间更早、与被测会话无关的有效交接记录，让 `T0` 早于被测发布。否则被测的那次交接就是第一条记录，交接前的发布会被时间闸判成不确定（这种情形单独由 R9g 覆盖）。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| R1 | 解析：正常发布、`type_url` 创建、更新已有 URL | 各产出一条 `Publish`，字段齐 |
| R2 | 解析：`is_error` 结果、`quickstart`/`read`/`list` 结果、非 Artifact 工具的结果里带 claude.ai URL、坏 JSON 行、URL 带结尾换行 | 均不产出 |
| R3 | 子代理文件里的发布 | 产出，`session_id` 为父会话，`relpath` 正确 |
| R4 | 只在一个账号出现 | `ownership == "single"` |
| R5 | 两账号有相同记录、无交接记录 | `uncertain`，`accounts` 含两者，文本 `ACCOUNT` 带 ` ?` 且有说明行 |
| R6 | 真实跑 `handoff` 后，目标里交接前的发布，与交接后在目标新发布的另一个 URL | 前者归来源且 `recorded`；后者只在目标里有，归目标且 `single` |
| R7 | A→B，再 B→A `--force` | A 最早的发布仍归 A |
| R8 | 交接后目标文件开头被改写，且来源账号里已没有该会话（来源还在时按 R11b 的合并口径归来源） | 该记录判为不确定 |
| R9 | `handoff` 的 `UNCHANGED` 与 `--dry-run` 不写交接记录；状态目录不可写时、`record_handoff` 抛 `OSError` 时（进程内调用 `cli.main`，`mock.patch("multi_claude.artifacts.record_handoff", side_effect=OSError)`；`run_cli` 走子进程打不了桩），handoff 都仍退出 0、打印续聊命令并 `warning:` | 文件内容 / 退出码 / stdout / stderr |
| R9b | 同一秒内 A→B、B→A `--force`（不 sleep） | 追链按行序，A 最早的发布仍归 A |
| R9c | `--force` 产生的备份目录里的子代理发布、`.multi-claude-*.tmp/` 里的文件、`-` 开头的会话 ID | 都不产出 |
| R9d | 交接记录最后一行被截成半行，然后再跑一次 handoff | 半行按坏行跳过并 `warning:`；新记录另起一行、照常生效 |
| R9e | 先手工复制会话、不写记录（模拟上线前 A→B），再真实跑 B→A `--force` | 交接前在 A 写下的发布为 `uncertain`，不判给 B |
| R9f | 预置更早的无关记录后 A→B，发布时间晚于 `T0` | 归 A，`recorded` |
| R9h | 有记录的 A→B 之后删掉 A 的会话 | 仍归 A 且 `recorded`，不崩溃；`resume` 指向 `claude-b` |
| R9g | 不预置记录，第一次 handoff 就是 A→B | 交接前的发布判 `uncertain`（时间闸的已知代价） |
| R10 | 交接记录坏行、`v: 2` | 跳过，不报错 |
| R11 | CLI：`NAME` 过滤（含不确定时命中副本账号）、未登记名退出 1、`--limit -1` 退出 2、`--grep` 各字段、默认 30 条与截断提示、`--json` 字段与 `total`/`skipped`、`--cmd` 续聊命令（含目录未知）、无 `config.json` 时只扫 `~/.claude`、`handoffs.jsonl` 不存在 | 断言输出 |
| R11b | 合并口径：同一 record_id 一个副本判 A、另一个副本哈希不过 | 判 A，`ownership == "recorded"` |
| R11c | `--path`：存在的文件、已不存在的文件、没有路径的发布；与 `--cmd` 同时给 | 分别显示路径、`(missing)`、`(no source file)`，路径行在续聊命令之前；不带 `--path` 时不显示 |
| R12 | 标题含控制字符 | 输出前被清洗 |
| R13 | `sessions`、`usage --history`、`handoff`、`test_help`（分组帮助覆盖每个命令）现有用例 | 全部通过 |
| R14 | `tests/test_completion.py` 的 `SUBCOMMANDS` 加 `artifacts` 后，补全脚本含它 | 通过 |
| R15 | 本机实测：`multi-claude artifacts --json` | 示例 `9GwmwTUZsiSYKj4fLD7XdB` 归 `jakoes.wu.us@gmail.com`；wujianchao316 服务端 18 个中本地有的 16 个都列在它名下或 `uncertain` 里含它；耗时记录在验证报告 |

## 9. 日志 / 观测点

- `artifacts`：读不了的文件数走现有 `warning: skipped N session file(s) that could not be read`；交接记录坏行数大于 0 时 `warning: ignored N unreadable line(s) in <handoffs.jsonl 路径>`。
- `handoff`：写交接记录失败时 `warning: could not record the handoff in <路径>: <错误>; artifacts may show the wrong owner for this session`。
- `--json` 的 `ownership` 字段是判断归属是否可信的观测点。

## 10. 开放问题

- **扫描缓存**：全量扫描本机约 8 秒。默认本版不做缓存；若用户觉得慢，下一版可按 `(设备号, inode, 大小, 末尾 4 KB 哈希)` 做增量缓存，只读新增部分。
