# sessions 文本输出：调整列顺序，续聊命令改为按需显示的 CMD 列

> **2026-10-13 修订（v0.11.0 发布之后）**：用户确认带 `--cmd` 时不再显示 `SESSION ID` 列，单行、两行两种排版都去掉。理由：续聊命令里总有 `--resume <会话ID>`，启动目录未知时也有（`src/multi_claude/cli.py` `_resume_command`），同一串 ID 不必在一条会话里出现两次。不带 `--cmd` 时该列保留。改动只在 `cmd_sessions` 带 `--cmd` 的分支里去掉表头与各行的最后一列；下文 §2、§5.1、§8 已按修订后的行为改写。

## 1. 背景

`multi-claude sessions` 的文本输出现在每条会话占两行：第一行是表格列，第二行缩进两格是续聊命令（`cli.py:1824-1838`，格式定义在 `docs/feature/feature-session-list.md` §5.2）。表格行与命令行交替出现，用户反馈排版乱。

用户 2026-10-13 的要求：

- 列顺序改为 `TITLE  ACCOUNT  CREATED  UPDATED  SESSION ID`；
- 增加一个开关，打开时才显示续聊命令，放在新的一列 `CMD`（用户起初叫 `--handoff`，后确认采用 `--cmd`，理由见 §6）；
- `CMD` 里的用户主目录用 `~` 代替；
- 排版按终端宽度自适应：放得下用 `CMD` 列，放不下保持命令单独一行。

## 2. 目标 / 非目标

目标：

1. 不带 `--cmd` 时，每条会话一行，五列，不显示续聊命令。
2. 带 `--cmd` 时显示续聊命令，并去掉 `SESSION ID` 列：整张表最宽的一行不超过终端宽度时，命令作为最后一列 `CMD`；否则每条会话下面单独一行（缩进两格）。
3. 文本输出的续聊命令里，主目录写成 `~`。

非目标：

- 不改 `--json` 的任何字段（`resume` 仍是绝对路径，`schema_version` 仍为 1）。
- 不改会话扫描、过滤、排序、`--limit`、`--grep` 的行为。
- 不改 `handoff` 子命令。

## 3. 假设与约束

- 标题列不再是最后一列，列宽必须按终端显示宽度算。用标准库 `unicodedata.east_asian_width`：`W`、`F` 记 2 格，组合字符（类别 `Mn`、`Me`）以及 `clean_title` 特意保留的 U+200C、U+200D（`sessions.py:412`）记 0 格，其余记 1 格。表情符号的组合序列和 `A`（宽度不定）类字符在部分终端里可能差一格，接受。
- 终端宽度取 `shutil.get_terminal_size((80, 24)).columns`。本机实测：输出接管道时为 80；设了环境变量 `COLUMNS=300` 时为 300。所以接管道时一般走“命令单独一行”，要强制单行可设 `COLUMNS`。
- 单行排版至少要约 150 列（账号、两个时间、36 位会话 ID、命令本身就占大半）；80～120 列的终端里带 `--cmd` 实际会走两行排版。
- `~/<带引号的路径>` 这种写法（如 `cd ~/'my proj'`）在 bash、zsh 里会展开主目录；fish 未验证。

## 4. 涉及模块

| 区域 | 行号锚点（main `f614638`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/cli.py` 参数定义 | `281-291` | 修改 | 描述文字与用法串加 `--cmd`；在 `--limit` 之后新增 `--cmd` 参数 |
| `src/multi_claude/cli.py` 分发 | `430` | 修改 | 调用 `cmd_sessions` 时多传 `args.cmd` |
| `src/multi_claude/cli.py` `cmd_sessions` | `1770-1771` 签名与文档串，`1824-1838` 渲染 | 修改 | 新增 `show_cmd` 形参；重写文本渲染 |
| `src/multi_claude/cli.py` `_resume_command` | `1843-1848` | 修改 | 新增 `tilde` 形参，文本输出传真 |
| `src/multi_claude/cli.py` 新函数 | `_resume_command` 之后（`1849` 邻近） | 新增 | `_tilde_path`、`_display_width`、`_aligned` |
| `src/multi_claude/cli.py` import | `17`（`import sys`）之后 | 新增 | `import unicodedata` |
| `tests/test_sessions.py` | `77-94`（T1）、`150`、`196`、`234`、`221-222`、`246-247`（T7）、`263`、`266`、`377-380`、`389-390` | 修改 | 文本输出的内容、行尾、行数断言按新格式改；补新用例 |
| `README.md`、`README.zh-CN.md` | 各自 `308-325`、`449`、`498` | 修改 | 示例与说明；`449` 的“给出续聊命令”改为带 `--cmd`；`498` 命令总表的用法串加 `[--cmd]` |
| `docs/feature/feature-session-list.md` | `142-155` | 修改 | 文本输出格式指向本文档 |
| `CHANGELOG.md` | `7`（`[Unreleased]`）之下 | 新增 | Changed 条目 |
| `src/multi_claude/cli.py`（2026-10-13 修订） | `cmd_sessions` 带 `--cmd` 的分支（main `6b315c6` 的 `1846` 起）；`--cmd` 帮助文字 `292-294` | 修改 | 去掉表头与各行最后一列 `SESSION ID`；帮助文字说明该列不显示 |

补全脚本不用改：`completion.py:23` 只登记位置参数，选项不在表里。

## 5. 方案

### 5.1 实现要点

**参数**（`cli.py:289-291` 之间追加）：`--cmd`，`action="store_true"`，帮助文字 `also print the command that resumes each session (a CMD column when the terminal is wide enough, otherwise a line below each session)`。与 `--json` 同时给时不报错，JSON 输出不变（`resume` 字段本来就在）。

**列与对齐**（替换 `cli.py:1824-1838`）：

- 表头 `TITLE  ACCOUNT  CREATED  UPDATED  SESSION ID`；带 `--cmd` 时去掉 `SESSION ID`，走单行排版时再加 `CMD`。
- 每列宽度取该列各单元格显示宽度的最大值，列间两个空格，最后一列不补空格，行尾 `rstrip`。
- 标题仍按 `_TITLE_TEXT_CHARS`（`cli.py:1754`，60 个字符）截断，空标题显示 `-`。

**排版选择**（只在带 `--cmd` 时）：先按单行排版算出所有行（含表头），最宽一行的显示宽度不超过终端宽度（相等也算放得下）就用单行；只算表头与数据行，不算末尾的 `showing N of M sessions` 提示；否则整张表改用两行排版：表格行不含 `CMD` 列，每条会话下面一行 `"  " + 续聊命令`。按整张表决定，不逐行切换。

**启动目录未知**：`session.directory` 为 None 时，命令后面仍附 `  (start directory unknown; project <项目目录名>)`，单行排版时附在 `CMD` 单元格里。不带 `--cmd` 时不显示这段说明。

**主目录写成 `~`**（新函数 `_tilde_path(directory)`，返回 `cd` 后面的那一段）：

- `home = os.path.expanduser("~")`；`home` 不是绝对路径或等于 `/` 时不替换。
- `directory == home`，或去掉前缀 `home + "/"` 后为空（目录写成 `home/`）→ `~`。
- `directory` 以 `home + "/"` 开头 → `"~/" + shlex.quote(其余部分)`。波浪号必须留在引号外面，否则 shell 不展开。
- 其它情况 → `shlex.quote(directory)`，与现在相同。
- 按字符串前缀比较，不解析软链：会话记录的是当时的目录原文。

`_resume_command(source, session, tilde=False)`：文本输出传 `tilde=True`，JSON 输出不传。

### 5.2 接口变更

- 新增选项 `sessions --cmd`。
- 文本输出格式变化，不向后兼容：列顺序改变；默认不再打印续聊命令。按文本格式解析输出的脚本会受影响，机器读取应使用 `--json`（不变）。
- 不涉及 `docs/reference/*`（仓库没有该目录）。

## 6. 备选方案与决策

- **始终用 CMD 列**：示例行约 239 列，窄终端折行，折点落在命令中间。不采用。
- **`--wide` 开关控制单行 / 两行**：用户选了按宽度自适应。
- **开关叫 `--handoff`**：仓库已有子命令 `handoff`（把会话复制到另一个账号，`cli.py:1678`），同名但含义不同。改用 `--cmd`，与列名 `CMD` 一致。

## 7. 影响分析

- **调用方**：`cmd_sessions` 只有 `cli.py:430` 一处调用；`_resume_command` 有两处调用（`cli.py:1818` JSON、`cli.py:1827` 文本），JSON 那处保持默认参数，输出不变。
- **默认输出少了续聊命令**：原来不带参数就能看到命令，现在要加 `--cmd`。两份 README 各三处（示例与说明、`449`、`498`）同步改。
- **输出随终端宽度变化**：同一命令在宽窄不同的窗口里排版不同；接管道时宽度为 80，一般是两行排版。
- **`handoff` 子命令**：打印续聊命令走自己的代码（`cmd_handoff`，`cli.py:1678` 起），不经过 `_resume_command`，不受影响。
- **运行时**：只多了逐字符算宽度，行数上限默认 30，开销可忽略。不新增进程、文件或网络访问。

## 8. 回归测试

`python -m unittest discover -s tests`（全量，以全量结果为准）。`tests/test_sessions.py` 中：

| 编号 | 场景 | 预期 |
| ---- | ---- | ---- |
| R1 | 不带 `--cmd`，三条会话（改 T1） | 表头五列顺序正确；每条一行；输出里没有 `--resume` |
| R2 | `--cmd`，`COLUMNS=300` | 表头为 `TITLE  ACCOUNT  CREATED  UPDATED  CMD`（无 `SESSION ID`）；每条一行，行尾是续聊命令 |
| R3 | `--cmd`，`COLUMNS=40` | 表头为 `TITLE  ACCOUNT  CREATED  UPDATED`（无 `CMD`、无 `SESSION ID`）；每条会话下一行是 `"  " + 续聊命令` |
| R4 | 标题 `修复解析`（4 字 8 格）与 `abcdefgh`（8 格）两条会话，同账号；再加标题 `修复解析器`（10 格）一条 | 整行精确断言。前两条时两行标题后都恰好两个空格，其后的格式相同；加第三条后 `abcdefgh` 行标题后是 4 个空格。标题都比表头 `TITLE`（5 格）宽，列宽由数据决定 |
| R5 | 启动目录在 `HOME` 之下、等于 `HOME`、路径含空格（目录用测试的 `self.home` 拼，`tests/helpers.py:107-108`） | 分别为 `cd ~/sub`、`cd ~`、`cd ~/'my proj'`；JSON 的 `resume` 仍是绝对路径 |
| R6 | 启动目录不在 `HOME` 之下 | `cd /w/proj`，与现在相同 |
| R7 | 启动目录未知（改 T7） | `--cmd` 且 `COLUMNS=300`：该行行尾是命令加 `(start directory unknown; project -w-proj)`；`--cmd` 且 `COLUMNS=40`：这段在第二行；不带 `--cmd`：输出不含 `start directory unknown` |
| R8 | `--json --cmd` | 与 `--json` 输出完全相同 |
| R9 | 既有 JSON 断言（T16、T9、T19 等） | 不改即通过 |
| R10 | 既有文本断言（`150`、`234`、`263`、`377-380`、`389-390`） | 按新格式改后通过：默认每条会话一行，`--limit` 截断提示仍在末尾 |
| R11 | `--cmd`，标题与目录全用 ASCII；先用 `COLUMNS=300` 跑一次，取最长行的 `len()` 为 W，再分别用 `COLUMNS=W` 与 `COLUMNS=W-1` | 前者单行排版，后者两行排版 |

手工：本机 `multi-claude sessions --grep 净距` 与加 `--cmd` 各看一次；把一条 `cd ~/… && claude-… --resume …` 在普通终端里运行，确认能进入目录。

## 9. 日志 / 观测点

只读命令，不加日志。判断依据是输出本身：表头是否含 `CMD`、命令是否以 `cd ~/` 开头。既有的 stderr 告警（`skipped N session file(s)`）与末尾的 `showing N of M sessions` 不变。
