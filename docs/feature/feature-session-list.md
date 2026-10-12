# multi-claude：跨账号、跨目录的会话查询 `sessions`

> 2026-10-11 注记：代码已按方案实现（`sessions.py` 的 `list_sessions`、`read_session`、`clean_title`；`cli.py` 的 `cmd_sessions`、`_session_sources`、`_resolve_command_alias`；`tests/test_sessions.py` 29 例）。方案评审 2 轮收敛，意见已并入正文。基线：main `71dcd48`（v0.9.1）。

## 1. 背景

用户 2026-10-11 提出（原话）：“有时候某个会话记不住是哪个账号或哪个目录下创建的了”，要求增加查询所有会话的命令，跨目录，输出至少包含账号、目录、会话名称、会话 ID、创建时间、最后更新时间。

现有命令的覆盖情况：

- `handoff` 只在当前工作目录对应的项目目录里找会话（`sessions.py:62-87`）。
- `usage --history` 只读会话记录里的 token 计数（`history.py:86-90`、`:100-124`）。
- `list` 的 `LAST USED` 只对会话文件做 `stat`（`history.py:154-178`）。

### 1.1 已核实的事实

数据取自 2026-10-11 本机 5 个登记账号加未迁移的 `~/.claude`，共 120 个会话文件。统计脚本只看键名、类型与计数，不输出对话内容。

| 事实 | 依据 |
| ---- | ---- |
| 会话文件在 `<账号目录>/projects/<编码后的目录>/<会话ID>.jsonl`；子代理记录在 `…/<会话ID>/subagents/` 下（本机 1189 个） | `sessions.py:3-5`；本机目录结构 |
| 文件名（去掉 `.jsonl`）与文件内 `sessionId` 一致 | 114/114 |
| 目录名编码是把非字母数字字符换成 `-`，不可逆 | `sessions.py:45-46` |
| 文件内第一个 `cwd` 按上述规则编码后等于所在项目目录名；27 个会话中途出现过别的 `cwd` | 114/114；27/114 |
| 标题行有两种：`{"type":"custom-title","customTitle":…}` 与 `{"type":"ai-title","aiTitle":…}`，会重复写入多次；两者同时存在的 27 个会话里内容都不相同 | 本机行类型统计 |
| 44 个会话没有任何标题行；其中 23 个能取到首条提问，另外 21 个是没有任何提问的空会话（文件只有几 KB） | 本机统计 |
| `user` 行的 `message.content` 是字符串或内容块列表；有 `isMeta` / `isSidechain` 标记的行、以 `<` 开头的内容（命令包装、系统提示）不是用户敲的提问 | 本机头部统计：普通文字 82、`<` 开头 43、带标记 19 |
| 时间戳形如 `2026-09-17T09:53:06.277Z`；标题行、`mode` 行等没有时间戳 | `history.py:52-62`；本机键名 |
| 文件创建时间不可靠：10 个文件与首行时间戳相差超过 1 小时（`handoff` 复制的副本是复制时间） | 10/113 |
| 文件修改时间不早于末行时间戳；37 个文件晚 1 小时以上（原因未核实） | 0 例早于；37/114 |
| 同一会话 ID 会出现在多个位置：6 个 ID 各有 2–3 份，其中 1 个在同一账号的两个项目目录下 | 本机统计；`sessions.py:143-190` |
| 存在超过 64 KB 的单行（大段粘贴），固定大小的头部读取会把它截成坏行 | 本机 1 例：首个带 `cwd` 的行起于偏移 714，行长超过 64 KB |
| 读法“尾部 64 KB + 头部逐行（上限 1 MiB）”取到的目录、创建时间、更新时间、标题，与全量逐行解析的结果完全一致 | 原型脚本，120/120 一致；耗时 0.1 秒（全量解析 3.8 秒，文件共 940 MB） |
| 续聊命令的写法 `cd <目录> && claude-<账号> --resume <ID>` | `cli.py:1687`（`handoff` 实测可续聊） |
| default 身份账号的启动命令同样是 `claude-<名称>` | `launcher.py:88-100` |

## 2. 目标 / 非目标

**目标**

1. `multi-claude sessions [NAME] [--grep TEXT] [--limit N] [--json]`：只读列出各账号全部项目目录下的会话，给出账号、目录、名称、会话 ID、创建时间、最后更新时间；`conversations` 是同一命令的别名。
2. 包含未登记的默认目录 `~/.claude`，账号显示为 `default`。
3. 每条会话附一条可以直接复制运行的续聊命令。

**非目标**

- 不输出对话正文，不做全文检索；只取标题，没有标题时取首条提问的开头。
- 不删除、不移动、不改写会话文件；不建索引、不缓存。
- 不列子代理记录。
- 不合并同一会话 ID 的多份副本（各行照常列出）。
- 不联网、不读凭据。

## 3. 假设与约束

- 会话记录格式未写入官方文档。任何一行解析失败、字段缺失或类型不符都跳过该行；整份文件取不到的字段按 §5.1.2 的兜底处理，不报错。
- `custom-title` 优先于 `ai-title`：依据是键名与“两者同时存在时内容不同”；Claude Code 自己的会话选择器显示哪一个未核实。这个优先级只在同一个读取窗口内成立：尾部窗口里的标题先于头部窗口里的（§5.1.2），所以 `custom-title` 只在头部、`ai-title` 在尾部的大文件会显示 `ai-title`。
- 续聊必须先 `cd` 到会话启动时的目录，沿用 `handoff` 的做法（`sessions.py:34`）。
- 兼容 Python 3.8（编译机）。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `71dcd48`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/sessions.py` | :1-8 模块说明 | 修改 | 说明里加上“会话列表” |
| `src/multi_claude/sessions.py` | :10-17 导入、:21 邻近、:195 之后 | 新增 | 导入 `json`、`unicodedata`、`datetime` 与 `history`（`history` 不导入 `sessions`，无循环导入）；常量 `TAIL_BYTES`、`HEAD_BUDGET` 加在 :21 邻近；`SessionInfo`、`list_sessions`、`read_session`、`clean_title` 加在 :195 之后（§5.1.1–§5.1.3） |
| `cli.py` `COMMAND_GROUPS` | :57 之后 | 新增 | Everyday 组加 `("sessions", "find a session across accounts and directories")` |
| `cli.py` `build_parser` | :279 `return parser` 之前 | 新增 | `sessions` 子命令与选项 |
| `cli.py` `main` | :370 与 :371 之间 | 新增 | 把别名 `conversations` 换成 `sessions`（§5.1.5） |
| `cli.py` `main` | :414 `which` 分支之前 | 新增 | 分派到 `cmd_sessions` |
| `cli.py` | :1705 `cmd_statusline` 之前 | 新增 | `cmd_sessions`、`_session_sources`（§5.1.4） |
| `completion.py` `POSITIONAL_KINDS` | :18-24 | 修改 | 加 `"sessions": ["name"]` |
| `README.md`、`README.zh-CN.md` | :307 之前、:427 之前、:473 之后 | 新增 | 新小节“查找会话”（含隐私说明）、常见问题一条、命令表一行。:13、:40、:441 讲的是登录信息与联网，本命令两样都不碰，原文仍然成立，不改 |
| `SECURITY.md` | :33 所在条目之后 | 新增 | 说明 `sessions` 会把会话标题与首条提问打印到终端 |
| `CHANGELOG.md` | :7 `[Unreleased]` | 新增 | Added |
| `docs/analysis/roadmap.md` | :96 之后 | 新增 | §2.10 |
| `tests/test_sessions.py` | 新文件 | 新增 | §8 |
| `tests/test_completion.py` | :11-12、:85 邻近 | 修改 | `SUBCOMMANDS` 加 `sessions`，补一条账号名补全断言 |

不改动 `handoff` 的任何函数。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 枚举会话文件

`list_sessions(account_dir) -> (List[SessionInfo], 读不了而跳过的文件数)`：

- 只看 `projects/<项目目录>/` 第一层的常规文件，要求文件名以 `.jsonl` 结尾且去掉后缀后整串匹配 `SESSION_ID_PATTERN`（`sessions.py:21`，用 `fullmatch`：`match` 加 `$` 会放过结尾的换行）、不以 `-` 开头（拼进续聊命令会被当成选项）。这样不进 `subagents/`，也排除 `handoff` 的临时文件（`.multi-claude-*.jsonl.tmp`）与备份（不以 `.jsonl` 结尾，`sessions.py:156`）。
- `projects/` 不存在视为没有会话。项目目录读不了时跳过；单个文件 `stat` / `open` / 读取抛 `OSError` 时跳过并计数。

#### 5.1.2 读取一个会话

`read_session(path) -> SessionInfo`，字段：`session_id`、`directory`（可为 None）、`project`（项目目录名）、`title`、`title_source`、`created`、`updated`（UTC `datetime`）、`path`。

读法分两步，都以二进制读、逐行 `json.loads`，不是对象的行跳过：

1. **尾部**：读文件最后 64 KB（`TAIL_BYTES`），按行解析（被截断的首行自然解析失败）。记录最后一个 `custom-title`、最后一个 `ai-title`、最后一个能解析的 `timestamp`。
2. **头部**：从文件开头用带上限的 `readline(剩余额度 + 1)` 逐行读，累计上限 1 MiB（`HEAD_BUDGET`）；读到的一行不以换行结尾且累计已超过额度（说明这一行比剩余额度长）时停止，这样单行再长也不会整行读进内存。记录第一个 `cwd`、第一个能解析的 `timestamp`、途中遇到的标题行、第一条“普通提问”。满足“已有 `cwd`、已有时间戳、并且（尾部已有标题，或已取到提问）”就停。

   由此带来一个已知限制：标题行如果既不在尾部 64 KB 内、又排在首条提问之后，就读不到，名称退到首条提问。标题行会随会话进行反复写入（§1.1），本机 120 个文件没有这种情况。

“普通提问”的判定：`type == "user"`、`isMeta` 与 `isSidechain` 的值都不为真（键不存在或值为 `false` 都算普通行）、`message.content` 是字符串，或内容块列表里第一个 `text` 块；去掉首尾空白后非空且不以 `<` 开头。

取值与兜底：

| 字段 | 取值 | 取不到时 |
| ---- | ---- | ---- |
| `title` / `title_source` | 尾部 `custom-title`（`custom`）→ 尾部 `ai-title`（`ai`）→ 头部扫描停下之前遇到的 `custom-title`（`custom`）→ 头部遇到的 `ai-title`（`ai`）→ 首条普通提问（`prompt`） | 空字符串，`none` |
| `directory` | 头部第一个 `cwd` | 尾部各行的 `cwd` 中，按 `encode`（`sessions.py:45-46`）编码后等于项目目录名的最后一个；仍没有则为 None（文本显示见 §5.1.4）。项目目录名超过 200 个字符时带哈希后缀（`sessions.py:19-20`），编码结果不会相等，这条兜底不生效 |
| `created` | 头部第一个时间戳 | None |
| `updated` | 尾部最后一个时间戳 | 文件修改时间 |

`cwd` 必须是绝对路径，且不含 §5.1.3 列出的不可打印字符：目录要原样拼进续聊命令，`shlex.quote` 不会去掉换行与控制字符，`Cf` 类的双向覆盖符能让终端把命令倒着显示，相对路径（含 `-`）换个地方 `cd` 就不是原来的目录。头部第一个非空的 `cwd` 不满足这一条时，目录直接记为未知：既不再往后找（后面的 `cwd` 可能是会话中途换到的别的目录），也不做尾部兜底（编码有损，`/a\nb` 与 `/a/b` 编码相同，尾部凑巧相等的值不可信）。尾部兜底只在头部根本没读到 `cwd` 时执行，候选值同样要满足这一条。

时间解析复用 `history._parse_time`（`history.py:52-62`）。

#### 5.1.3 标题清洗

`clean_title(text, limit)`：把连续空白（含换行）压成一个空格，删除 Unicode 类别为 `Cc`、`Cf`、`Zl`、`Zp` 的字符（控制字符、格式字符、行与段分隔符），去掉首尾空白；`Cf` 里保留 U+200C 与 U+200D（波斯语等文字的拼写与组合表情要用它们，它们不改变别的字符的显示），超过 `limit` 个字符时截断并接 `…`。

必须清洗：标题与提问来自会话内容，可能含换行和终端转义序列，原样打印会破坏表格，转义序列还可能改写终端显示。JSON 输出的上限是 200 个字符，文本输出是 60 个字符。

#### 5.1.4 `cmd_sessions`

**数据来源**（`_session_sources`）：

- 配置里登记的每个账号，目录取 `accounts.account_dir`（`accounts.py:40-47`），续聊命令的启动器是 `claude-<账号名>`。
- 未登记的默认目录：`accounts.default_dir()`（`accounts.py:50-52`）下有 `projects/` 目录，且默认目录本身的真实路径（`os.path.realpath`）不等于任何登记账号目录的真实路径时加入，账号名显示 `default`，`registered` 为假。它的续聊命令是 `cd <目录> && env -u CLAUDE_CONFIG_DIR -u CLAUDE_SECURESTORAGE_CONFIG_DIR claude --resume <ID>`：去掉这两个变量后，在别的账号的 Claude 会话里运行也会落到默认目录，与 default 身份启动命令的做法相同（`launcher.py:99-100`）。已做过 `migrate-default` 时 `~/.claude` 是指向账号目录的软链，真实路径相同，不会重复列出。
- 没有 `config.json` 时只看默认目录。

**参数**：

- `NAME`：只看一个账号。先过 `_checked_name`（`cli.py:1255-1260`），不合法的名字退出 2（没有配置文件时也一样）；再按 `config.find` 匹配（大小写不敏感，`config.py:135-146`）。没有登记、但名字不分大小写等于 `default` 且默认目录已被加入时，选中默认目录；其它未登记的名字报 `account 'x' is not registered`，退出 1（与 `usage NAME` 一致，`cli.py:1580-1583`）。
- 登记账号本身叫 `default`、同时还有未登记的默认目录时：`sessions default` 按登记账号优先，只列登记账号；不带 `NAME` 时两者都列出，账号列都是 `default`，靠续聊命令（`claude-default` 与 `env -u … claude`）和 JSON 的 `registered` 区分。这种组合下未登记的默认目录不能单独选中，接受。
- `--grep TEXT`：不区分大小写的子串匹配，对象是清洗后未截断的标题、目录（`directory` 为 None 时用项目目录名）、会话 ID，命中任一即保留。
- `--limit N`：最多输出 N 条，0 表示不限；文本输出默认 30，`--json` 默认不限；负数退出 2。
- `--json`：机器可读输出。

**排序**：按 `updated` 从新到旧，相同时按账号名、会话 ID。

**文本输出**：每条会话两行，第一行是表格列，第二行缩进两格是续聊命令：

```text
ACCOUNT              UPDATED           CREATED           SESSION ID                            TITLE
work@example.com     2026-10-11 17:42  2026-10-11 16:05  3c588340-1219-48df-a231-509866789487  Evaluate the session list feature
  cd /Users/me/proj && claude-work@example.com --resume 3c588340-1219-48df-a231-509866789487
default              2026-10-09 08:12  2026-10-09 08:01  0b1f6c1e-52f0-4a55-9d2c-0c2f6f1f4a10  -
  cd /Users/me/tmp && env -u CLAUDE_CONFIG_DIR -u CLAUDE_SECURESTORAGE_CONFIG_DIR claude --resume 0b1f6c1e-52f0-4a55-9d2c-0c2f6f1f4a10
```

- 时间按本机时区显示为 `YYYY-MM-DD HH:MM`；`created` 为 None 时显示 `-`。
- 标题为空显示 `-`。标题放最后一列，中文标题不影响前面各列对齐。
- 目录出现在续聊命令的 `cd` 之后，用 `shlex.quote`。`directory` 为 None 时第二行写成 `  <不带 cd 的续聊命令>  (start directory unknown; project <项目目录名>)`；项目目录名来自磁盘上的目录名，同样按 §5.1.3 清洗。
- 不用 `_print_table`（`cli.py:1411-1414`）：它一次打印全部行，插不进续聊行；列宽在 `cmd_sessions` 里自行计算。
- 因 `--limit` 截掉了一部分时，末尾打印 `showing 30 of 120 sessions; use --limit 0 for all, or --grep TEXT to narrow`。
- 没有任何会话时打印 `no sessions found`，退出 0。
- 有读不了的文件时，在 stderr 告警 `skipped N session file(s) that could not be read`，退出码仍为 0。

**只读**：不加写锁，迁移未完成时照常运行（与 `usage` 相同，`cli.py:404`）。

#### 5.1.5 别名 `conversations`

在 `main` 里、`build_parser()` 之前，把第一个非选项参数 `conversations` 换成 `sessions`。不用 argparse 的 `aliases`：`test_help.py:26` 要求 `COMMAND_GROUPS` 与解析器的子命令名完全一致，别名进了解析器就得进分组帮助；`args.command` 也会变成别名，分派处要写两遍。

别名写在 `sessions --help` 的说明里；补全脚本只补 `sessions`。别名不在 `_unknown_command` 的候选名里，所以拼错的别名得不到指向 `sessions` 的建议（例如 `conversation` 会被建议成 `completion`，`cli.py:1034`），接受，不为别名另加拼写纠正。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `sessions [NAME] [--grep TEXT] [--limit N] [--json]` | 新增 | 新增命令 |
| `conversations …` | 新增，等同 `sessions` | 新增 |
| 补全脚本 | `sessions` 的第一个位置参数补账号名 | 需重新生成补全脚本才生效 |

`sessions --json` 的结构：

```json
{
  "schema_version": 1,
  "sessions": [
    {
      "account": "work@example.com",
      "registered": true,
      "directory": "/Users/me/proj",
      "project": "-Users-me-proj",
      "session_id": "3c588340-1219-48df-a231-509866789487",
      "title": "Evaluate the session list feature",
      "title_source": "custom",
      "created": "2026-10-11T08:05:12Z",
      "updated": "2026-10-11T09:42:40Z",
      "file": "/Users/me/.cc/work@example.com/projects/-Users-me-proj/3c588340-1219-48df-a231-509866789487.jsonl",
      "resume": "cd /Users/me/proj && claude-work@example.com --resume 3c588340-1219-48df-a231-509866789487"
    }
  ],
  "total": 120,
  "skipped": 0
}
```

- `title_source` 取 `custom`、`ai`、`prompt`、`none`；`directory`、`created` 可为 null。
- `project` 是磁盘上的项目目录名。`directory` 为 null 时，`resume` 是不带 `cd` 的续聊命令（仍是可执行的命令，不带括号说明），使用方要自己先进入会话的启动目录。
- `total` 是过滤后、截断前的条数；`skipped` 是读不了的文件数。
- 时间用 `_iso` 的格式（`cli.py:1554-1555`）。

退出码：0 成功（含没有会话）；1 账号未登记；2 参数错误（含不合法的账号名、负的 `--limit`）。

不涉及 `docs/reference/*`（仓库没有该目录）。

## 6. 备选方案与决策

- **账号在前的词序（`multi-claude NAME sessions`）**：现有命令全部是子命令在前，`_unknown_command` 把第一个非选项词当子命令（`cli.py:1029-1041`）；账号名只保留了 `auto`（`routes.py:22`），账号在前会和名为 `list`、`add` 的账号冲突。用户已同意子命令在前。
- **全量解析每个文件**：实现最简单，但本机要 3.8 秒且随会话增长；头尾读法结果一致，0.1 秒。不采用。
- **固定读头部 64 KB**：遇到超过 64 KB 的单行会丢 `cwd`（本机有实例）。改为逐行读、按累计字节封顶。
- **建索引或缓存**：本工具不保存派生数据（与 `feature-usage-history.md` §6 一致）。不采用。
- **目录作为单独一列**：账号、两个时间、36 位会话 ID、标题已经很宽；目录本来就要出现在续聊命令里，再加一列是重复。不采用。

## 7. 影响分析

- **现有命令**：`sessions.py` 只追加函数，`handoff` 用到的函数不改；`cli.py` 只加分组条目、子命令、别名替换、一条分派与一个命令函数。
- **别名替换的波及**：只在第一个非选项参数恰好是 `conversations` 时生效。改动前这个写法会被 `_unknown_command` 判为未知命令退出 2，所以没有现有用法受影响。`statusline-hook` 的快速路径在它之前返回（`cli.py:362-367`），不经过替换。
- **帮助与补全**：分组帮助多一行；`test_help.py:26` 核对分组与子命令一致，新增子命令必须同时进 `COMMAND_GROUPS`。补全脚本由解析器生成，自动包含新命令与选项。
- **运行时**：每个会话文件最多读 64 KB 加 1 MiB，常见情况头部几 KB 就停；本机 120 个文件 0.1 秒。不新启进程，不加锁，不阻塞其它命令。正在追加的会话文件，尾部半行解析失败后被跳过，取到上一行的时间戳。
- **隐私（对外语义扩大）**：这是第一个把会话内容片段（标题、首条提问的开头）打印到终端的命令。README 与 SECURITY.md 必须写明：只打印到本机终端或 `--json` 的标准输出，不保存、不发往任何地方；`--json` 的输出重定向到文件时内容随之落盘。`usage --history` “只读计数”的说法不变（`README.md:285`）。
- **部署形态**：
  - 没有 `config.json`：只列默认目录。
  - 默认目录已迁移：按真实路径去重，不重复。
  - 账号目录不存在或没有 `projects/`：该账号没有会话，不报错。
  - Linux 与 macOS 行为相同（不使用文件创建时间）。
- **续聊命令的限制**：会话启动目录已被删除时 `cd` 会失败，命令本身不检查目录是否存在。未登记默认目录的命令用 `env -u` 去掉两个配置目录变量（§5.1.4），不受当前终端环境影响。

## 8. 回归测试

**环境**：本机（Python 3.11）与编译机 ubuntu20（Python 3.8.10）全量 `unittest`；本机 `pyflakes src tests`。测试用合成会话文件，放在测试沙箱的家目录里。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 两个账号、三个项目目录的合成会话，文本输出 | 每条会话的账号、会话 ID、标题、两个时间（`TZ=Asia/Shanghai` 下换算）与续聊命令正确；按更新时间从新到旧 |
| T2 | 标题优先级：同时有 `custom-title` 与 `ai-title`；只有 `ai-title`；都没有但有提问；空会话 | `title_source` 依次为 `custom`、`ai`、`prompt`、`none`；空会话文本显示 `-` |
| T3 | 首条提问的判定：前面有 `isMeta: true` 行、`isSidechain: true` 行、`<command-name>` 开头的行；普通提问行带 `"isSidechain": false`；内容是字符串 / 内容块列表 | 取到第一条普通文字提问 |
| T4 | 标题含换行、制表符、`ESC[31m` 转义序列、超过 60 个字符 | 输出里没有控制字符与换行；文本截到 60 个字符加 `…`；JSON 上限 200 |
| T5 | 第一个带 `cwd` 的行长度超过 64 KB | `directory` 仍能取到 |
| T5b | 第一个带 `cwd` 的行超过 1 MiB：尾部有编码后等于项目目录名的 `cwd` / 尾部没有 | `directory` 取尾部那个 / 为 null；都不报错，退出 0 |
| T5c | `cwd` 含换行符 | 当作没有 `cwd`，输出仍是每条会话两行 |
| T6 | 标题行只在文件开头（首条提问之前）、尾部 64 KB 内没有 | 仍取到标题 |
| T6b | 标题行在首条提问之后、且不在尾部 64 KB 内 | `title_source` 为 `prompt`（§5.1.2 的已知限制） |
| T7 | 尾部没有时间戳的文件；头部没有 `cwd` 的文件；坏行；末尾半行 | `updated` 等于文件修改时间；`directory` 为 null、`project` 是项目目录名、`resume` 不含 `cd`，文本第二行含 `start directory unknown`；坏行与半行被跳过 |
| T7b | 异常内容不让命令崩溃：嵌套数万层的 JSON 行；时间戳为 `0001-01-01T00:00:00Z`、`9999-12-31T23:59:59Z`（分别在 UTC 以西、以东的时区下）；只含控制字符的 `custom-title` 后面还有 `ai-title`；含 U+202E 的标题与目录；相对路径的 `cwd` | 都退出 0、stderr 没有 `Traceback`；越界的时间在文本里显示 `-`；标题取 `ai-title`；输出里没有 U+202E，目录记为未知；含中文、空格、组合表情（U+200D）的标题与目录原样保留 |
| T8 | 子代理文件 `…/<ID>/subagents/agent-x.jsonl`、`.multi-claude-<ID>.abc.jsonl.tmp`、备份 `<ID>.jsonl.multi-claude-bak.20261011_120000`（`statusline.py:124-131` 的格式） | 都不出现在结果里；文件名结尾带换行、或以 `-` 开头的 `.jsonl` 文件同样不出现 |
| T9 | 同一会话 ID 在两个账号里（模拟 `handoff`） | 列出两行，各自的续聊命令用各自账号 |
| T10 | 未登记的 `~/.claude` 有会话 | 账号显示 `default`、`registered` 为 false、续聊命令含 `env -u CLAUDE_CONFIG_DIR -u CLAUDE_SECURESTORAGE_CONFIG_DIR claude --resume <ID>`；`sessions default` 与 `sessions DEFAULT` 只列它 |
| T10b | 登记账号叫 `default`，同时有未登记的 `~/.claude` | 不带 `NAME` 列出两者（一个 `registered` 为 true、命令用 `claude-default`，一个为 false）；`sessions default` 只列登记账号 |
| T11 | 做过 `migrate-default` 的环境（`~/.claude` 是指向账号目录的软链） | 会话只列一次，账号是登记的名字 |
| T12 | 没有 `config.json`：默认目录有会话 / 没有 | 列出默认目录的会话 / 打印 `no sessions found`；都退出 0 |
| T13 | `sessions NAME`；`sessions nosuch`；`sessions default`（默认目录没有 `projects/`）；`sessions 'bad name'`（有配置、无配置各一次） | 只列该账号；退出 1；退出 1；退出 2 |
| T14 | `--grep`：按标题（大小写不同）、目录片段、会话 ID 前缀 | 各自只留下命中的会话 |
| T15 | `--limit 1`；`--limit 0`；默认 30（造 31 个会话）；`--limit -1` | 1 条并打印 `showing 1 of N`；全部；30 条并打印 `showing 30 of 31`；退出 2 |
| T16 | `--json` | 结构、`total`、`skipped` 与数值正确；默认不限条数 |
| T17 | 一个没有读权限的会话文件（以 root 运行时跳过，先例 `tests/test_refresh.py:333`） | stderr 告警 `skipped 1 session file`，其余照常，退出 0 |
| T18 | `conversations`、`conversations NAME --json` | 与 `sessions` 的输出相同；`convrsations` 仍报未知命令并退出 2 |
| T19 | 目录路径含空格 | 续聊命令里的目录被引号包住 |
| T20 | 迁移未完成（存在迁移记录）时运行 `sessions` | 照常输出，退出 0 |
| T21 | 帮助与补全：`--help` 的 Everyday 组、`sessions --help`、bash 补全 `multi-claude sessions ` | 含 `sessions`；说明里提到 `conversations`；补出账号名 |
| T22 | 只读：运行前后对账号目录做快照 | 没有文件被创建、修改或删除 |
| T23 | 回归 | 现有全部用例通过 |
| T24 | 本机真实数据（手工，不进自动化） | `sessions --json --limit 0` 的条数等于 `projects/*/*.jsonl` 的文件数；抽 3 条运行续聊命令能打开对应会话 |

## 9. 日志 / 观测点

- 截断提示：`showing 30 of 120 sessions; use --limit 0 for all, or --grep TEXT to narrow`
- 没有会话：`no sessions found`
- 告警（stderr）：`skipped N session file(s) that could not be read`
- 报错（stderr）：`account 'x' is not registered`、`--limit must be 0 or more`
- `--json` 的 `total` 与 `skipped` 可供脚本核对。
