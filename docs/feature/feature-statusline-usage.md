# multi-claude：通过 statusLine 采集账号用量

> 2026-10-01 注记：plan-review 2 轮收敛，用户同意编码；代码已按方案实现（`src/multi_claude/statusline.py`、`usage.py` `read_account_usage`、`cli.py` `main` 开头的钩子拦截与 `cmd_statusline`），本机 230 个用例通过，待独立代码评审与编译机验证，未提交。来源：`docs/analysis/competitor-analysis.md` §6.2 “statusline 实时用量”。基线：main `da41835`。用户决定：注入方式为“包装现有 statusLine”，不新建 statusLine、不改启动命令。

## 1. 背景

- `multi-claude usage` 与 `list --json` 目前只读 Claude Code 写在 `.claude.json` 里的 `cachedUsageUtilization`（`src/multi_claude/usage.py:56-94`）。这份缓存可能很旧：本机实测正在使用的账号也有 45 小时没刷新。
- Claude Code 每次刷新状态栏时，会把一段 JSON 经 stdin 交给 `statusLine.command`，其中 `rate_limits.five_hour` / `rate_limits.seven_day` 带 `used_percentage`（0–100）与 `resets_at`（Unix 秒）。官方文档说明：仅 Pro/Max 订阅、且该会话收到第一次 API 响应后才有这两项，各窗口可能单独缺失。
- 本机用户的 statusLine 写在 `~/.claude-shared/settings.plugins.json`（经全局固定参数 `--settings` 传入，覆盖各账号 `settings.json` 里的同名键），命令是 claude-hud 的一段 `bash -c '…'`。

## 2. 目标 / 非目标

**目标**

1. `multi-claude statusline install FILE`：把 FILE 中已有的 `statusLine.command` 包成 `multi-claude statusline-hook <原命令>`；`statusline uninstall FILE` 原样还原。
2. 钩子在不改变状态栏显示的前提下，把 stdin 里的 `rate_limits` 存成该账号的用量快照。
3. `usage` 与 `list --json` 在快照比 `.claude.json` 缓存新时改用快照，并标出数据来源。

**非目标**

- FILE 中没有 statusLine 时替用户新建一个（install 直接拒绝）。
- 修改启动命令、`config.json` 结构或各账号的 `settings.json`。
- 联网查询用量、读取凭据。
- `doctor` 新增 statusLine 检查项。
- Windows（本工具本来就只支持 macOS / Linux）。

## 3. 假设与约束

以下 Claude Code 行为来自 2.1.286 二进制（`~/.local/share/claude/versions/2.1.286`，压缩后的 JS 名称随版本变化，只作为本次核对的出处）：

- **执行方式**：状态栏刷新函数调用通用的 hook 执行函数 `j0(h,"StatusLine","statusLine",…)`。非 Windows 分支为 `spawn(rn, [], {env:Zn, cwd:Qt, shell:!0, …})`，`rn` 是命令字符串（设了 `CLAUDE_CODE_SHELL_PREFIX` 时会先加上这个前缀）。`shell:true` 在 Unix 上就是 `/bin/sh -c <命令>`（Node `child_process` 官方文档，“Default shell: '/bin/sh' on Unix”）。Claude Code 原生版由 Bun 打包，本次没有读 Bun 源码核对它沿用同一语义，由 T15 实机兜底。所以钩子用 `/bin/sh -c <原命令>` 重放原命令，与 Claude 直接执行的方式一致。
- **stdin**：写入 `JSON + "\n"` 后关闭。
- **环境**：`Zn` 以 Claude 进程自身环境为底，再加上 `CLAUDE_PROJECT_DIR` 等少数键。启动命令导出的 `CLAUDE_CONFIG_DIR` 因此会传给 statusLine 命令。
- **显示**：退出码为 0 时取 stdout 显示，非 0 时不显示。
- **工作区信任**：未接受工作区信任时不执行 statusLine，此时也就采不到用量。

本方案自身的约束：

- **认账号**：钩子靠环境里的 `CLAUDE_CONFIG_DIR` 认账号，不新增环境变量，所以启动命令不必重新生成。`~/.zshrc` 里手写的、只设了 `CLAUDE_CONFIG_DIR` 的函数也能被认出来。
- **快照位置**：快照按账号的目录名 `dir` 存放（v0.2 起账号有 `dir` 字段，`src/multi_claude/config.py` `Account.__init__`），改名后快照仍对得上，不必搬文件。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `da41835`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/statusline.py` | 新文件 | 新增 | 包装 / 还原 statusLine、钩子采集、快照读写（§5.1.1–§5.1.4） |
| `src/multi_claude/usage.py` | `UsageReport` :35-39 | 修改 | 增加 `source` 字段，默认 `"cache"` |
| `src/multi_claude/usage.py` | `report_to_dict` :123-137 | 修改 | 输出 `source` |
| `src/multi_claude/usage.py` | `report_to_dict` 之后 | 新增 | `read_account_usage`：在缓存与快照中取较新的一份（§5.1.5） |
| `src/multi_claude/cli.py` | `main` :185-187 | 修改 | `argv[0] == "statusline-hook"` 时在一切解析之前转给钩子 |
| `src/multi_claude/cli.py` | `build_parser` :141-143 之前 | 新增 | `statusline` 子命令（`install` / `uninstall` + FILE，带 `--dry-run`，复用 `_add_dry_run` :146） |
| `src/multi_claude/cli.py` | `main` :219-220 之后 | 新增 | 分派 `statusline`（不加写锁） |
| `src/multi_claude/cli.py` | `_account_entries` :681-683、`cmd_usage` :725-727 | 修改 | 改用 `usage.read_account_usage` |
| `src/multi_claude/cli.py` | `_updated_cell` :763-771、:745-746 | 修改 | 来源为快照时标注 `(statusline)`；脚注提到快照 |
| `src/multi_claude/completion.py` | `POSITIONAL_KINDS` :16-20、`FIXED_WORDS` :25 | 修改 | `statusline` 第 1 个参数补 `install uninstall`，第 2 个补文件 |
| `src/multi_claude/completion.py` | bash :128-133、zsh :197-202、fish :263-271 | 修改 | 新增 `file` 与 `statusline` 两种候选 |
| `tests/test_statusline.py` | 新文件 | 新增 | 见 §8 |
| `tests/test_completion.py` | 追加用例 | 修改 | 见 §8 T13（合并与输出用例 T11、T12 放在 `tests/test_statusline.py`） |
| `README.md`、`README.zh-CN.md`、`CHANGELOG.md` | 命令表（README :82-91）、用量小节、Unreleased | 修改 | 文档同步 |

`read_usage` 的调用点全集（`grep -rn "read_usage(" src/`，不截断）：`cli.py:682`、`cli.py:727`，共 2 处，均改为 `read_account_usage`。`doctor.py` 不读用量。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 包装格式

包装后的命令：

```text
<shlex.quote(multi-claude 路径)> statusline-hook <shlex.quote(原命令)>
```

- **路径**：multi-claude 路径取 `shutil.which("multi-claude")` 的结果，不做 realpath，这样 pipx 或安装脚本升级后路径不变。找不到时 install 报错退出 1，提示把 multi-claude 放进 PATH。
- **识别**：判断一条命令是否已被包装：`shlex.split` 后恰为 3 段，第 2 段是 `statusline-hook`，第 1 段的 basename 是 `multi-claude`。
- **还原**：取第 3 段即原命令。`shlex.quote` 与 POSIX 模式的 `shlex.split` 互逆，原命令里的单引号、换行、`$`、反斜杠都能原样还原；`/bin/sh` 对单引号串不做任何展开，钩子收到的参数也与原命令逐字节相同。

#### 5.1.2 `statusline install FILE`

1. 目标文件取 `os.path.realpath(FILE)`。FILE 是软链时改它指向的文件，软链本身保留（`~/.claude-shared` 下的文件常被软链引用）。
2. 读取并 `json.loads`。以下情况都报错、退出 1、文件不动：文件不存在；不是 JSON 对象；没有 `statusLine`；`statusLine.type` 不是 `"command"`；`command` 不是非空字符串。
3. 判断是否已包装：
   - 已包装且路径与当前路径相同 → 打印 `unchanged`，退出 0。
   - 已包装但路径不同 → 用当前路径重新包装同一原命令。
   - 未包装 → 包装。
4. **写入**：
   - 先备份为 `<目标>.multi-claude-bak.<YYYYmmdd_HHMMSS>`，再按原权限位原子写入（`fsutil.atomic_write`，`src/multi_claude/fsutil.py:64`）。
   - 写入格式为 `json.dumps(…, indent=2, ensure_ascii=False) + "\n"`。键的顺序保留，但缩进可能与原文件不同，原文件留在备份里。
   - 只改 `statusLine.command`，`padding`、`refreshInterval` 等其它键不动。
5. **输出**：打印目标文件、备份路径与包装后的命令。带 `--dry-run` 时只打印将要做的改动（目标文件与包装后的命令），不备份、不写入，退出码与实跑相同（README `:91` 约定所有写命令都支持 `--dry-run`）。正在运行的会话是否立即改用新命令取决于 Claude Code 是否重新读取该设置文件（未核实）；没有生效时重开会话即可。

#### 5.1.3 `statusline uninstall FILE`

- 同样按 realpath 处理，校验规则同上。
- 已包装：备份后写回原命令，退出 0。
- 未包装：打印 `not installed`，退出 0（幂等）。

#### 5.1.4 钩子 `multi-claude statusline-hook ORIGINAL`

- **入口**：在 `main` 最开头拦截（`cli.py:185-187`，在 `_split_args_command` 与 argparse 之前）。它不是 argparse 子命令，不出现在帮助和补全里。参数个数不是 1 时退出 2。
- **步骤**：
  1. `data = sys.stdin.buffer.read()`，读到 EOF。
  2. 采集（§5.1.4.1），用 `try/except Exception` 包住：失败时静默，不向 stdout 写任何东西，因为 stdout 就是状态栏的内容。
  3. 把 `data` 写进 `tempfile.TemporaryFile()`（创建即 unlink），`seek(0)` 后 `os.dup2` 到 fd 0。刷新 stdout / stderr，然后执行 `os.execv("/bin/sh", ["/bin/sh", "-c", ORIGINAL])`。
- **为什么用 exec**：用 `exec` 而不是子进程，是为了让原命令接管本进程。这样 stdout、stderr、退出码、信号（Claude 刷新或超时时的终止）都与 Claude 直接执行原命令一致，钩子不需要转发任何东西。
- **exec 失败**：创建临时文件或 `os.execv` 抛 `OSError` 时（极少见，如临时目录全部不可写、`/bin/sh` 不存在），向 stderr 打一行原因，退出 127。

##### 5.1.4.1 采集

1. `json.loads(data)` 取 `rate_limits`。至少一个窗口（`five_hour` / `seven_day`）是对象，且 `used_percentage` 为有限数值，否则直接返回。
2. `load_config()`。配置不存在或损坏（`ConfigError`）时返回。
3. **认账号**：
   - 环境里有非空的 `CLAUDE_CONFIG_DIR` 时，取 `os.path.realpath(expand(它))`，与每个账号的 `os.path.realpath(accounts.account_dir(config, name))` 比较，相同即为该账号。
   - 没有 `CLAUDE_CONFIG_DIR` 时，取 `identity == "default"` 的账号。
   - 都没有就返回。
4. **快照路径**：`<platform.state_dir()>/usage/<account.dir>.json`。目录不存在时以 0700 创建。
5. **节流**：
   - 已有快照的 `rate_limits` 与本次相同，且 `captured_at` 距今不足 60 秒 → 不写，避免每 300 ms 刷新一次就落盘一次。
   - 否则读取该账号全局状态文件（`usage.global_state_path`，`usage.py:45-53`）的 `oauthAccount.accountUuid`，读不到记为 `null`。
6. **写入**：用 `atomic_write(path, …, mode=0o600)` 写入：

```json
{
  "schema_version": 1,
  "captured_at": "2026-10-01T08:00:00+00:00",
  "config_dir": "/Users/x/.cc/work",
  "account_uuid": "…或 null",
  "rate_limits": {
    "five_hour": {"used_percentage": 12.5, "resets_at": 1790000000},
    "seven_day": {"used_percentage": 40, "resets_at": 1790500000}
  }
}
```

- `rate_limits` 只保留两个窗口中合法的 `used_percentage` 与 `resets_at`（整数或浮点秒）。stdin 里的其它字段（模型、会话、工作目录、费用等）一律不落盘。
- `config_dir` 记的是该账号目录的 realpath（`os.path.realpath(accounts.account_dir(...))`），认出账号时比较的也是它。

#### 5.1.5 读取与合并（`usage.read_account_usage(config, account, now)`）

1. `cache = read_usage(global_state_path(...), now)`（现有逻辑不变，`source="cache"`）。
2. `snap = read_snapshot(...)`：读 `<state_dir>/usage/<account.dir>.json`。以下情况都视为无快照：
   - 文件不存在或读不出；
   - JSON 结构不符；
   - `config_dir` 与该账号目录的当前 realpath 不同（账号目录被换掉）；
   - `account_uuid` 与该账号全局状态文件当前的 `oauthAccount.accountUuid` 都是字符串但不相等（同一目录换了登录）；
   - `captured_at` 解析失败或超出平台时间范围。

   `resets_at` 秒数越界时，该窗口的 `resets_at` 记为 `None`。
3. **结果**：快照的 `status` 按 `STALE_AFTER`（`usage.py:18`）判 `ok` / `stale`，`source="statusline"`。
4. **择优**：两者都有时间且都可用（状态为 `ok` / `stale`）时，取时间较新的一份；只有一份可用时取那一份；都不可用时返回 `cache`，保持现有的 `no-data` / `unreadable` / `other-account` 语义。

#### 5.1.6 输出

- `report_to_dict` 增加 `"source": "cache" | "statusline"`。这是新增字段，`schema_version` 仍为 1。
- 表格的 UPDATED 列：来源为快照时在时间后加 ` (statusline)`，例如 `3m ago (statusline)`。
- 脚注改为：`Values come from Claude Code's own cache (.claude.json) or the statusline hook, and may be out of date; multi-claude never reads credentials.`

#### 5.1.7 补全

- `POSITIONAL_KINDS["statusline"] = ["statusline", "file"]`，并新增 `FIXED_WORDS["statusline"] = ("install", "uninstall")`。
- 三种 shell 模板各加两条分支：
  - bash：`file) COMPREPLY=($(compgen -f -- "$cur")); return 0 ;;` 与 `statusline) candidates="install uninstall" ;;`；
  - zsh：`file) _files; return ;;` 与 `statusline) candidates=(install uninstall) ;;`；
  - fish：`file` 用 `'(__fish_complete_path)'`，`statusline` 走现有 `FIXED_WORDS` 分支。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| CLI `multi-claude statusline install FILE` / `uninstall FILE` `[--dry-run]` | 新增。退出码：0 成功或无变化；1 文件或 statusLine 不合法、找不到 multi-claude；2 用法错误 | 新增 |
| CLI `multi-claude statusline-hook ORIGINAL` | 新增，内部使用，不出现在帮助中。退出码：原命令的退出码；参数个数错误为 2；exec 失败为 127 | 新增 |
| 文件 `<state_dir>/usage/<dir>.json` | 新增，0600，格式见 §5.1.4.1 | 新增 |
| `usage --json` / `list --json` 每账号 `usage.source` | 新增字段 | 只加字段，老解析方不受影响 |
| `usage` 表格 UPDATED 列与脚注 | 文字变化 | 面向人阅读，无契约 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **新增环境变量 `MULTI_CLAUDE_ACCOUNT` 认账号**：要改启动命令内容，所有启动命令在下次 `apply` 前都算过期，且认不出 `.zshrc` 手写函数启动的会话。用 `CLAUDE_CONFIG_DIR` 认账号没有这两个问题，因此不采用。
- **原命令以 `--` 之后的多个参数传入**：钩子需要把参数重新拼回命令串，引号与空白难以还原到逐字节一致。单个 `shlex.quote` 参数可以逆向还原，因此不采用。
- **子进程执行原命令并转发输出**：要自己转发 stdout、stderr 和退出码，Claude 终止钩子时原命令还会变成孤儿进程。`exec` 天然一致，因此不采用。
- **快照按账号名存放**：改名后对不上，需要在 `rename` 里搬文件。按 `dir` 存放天然稳定，因此不采用。

## 7. 影响分析

**正向**

- statusLine 每次刷新多出一次 Python 启动与 `multi_claude.cli` 导入，耗时未实测，T15 时用 `time` 对比包装前后。Claude 对状态栏刷新有 300 ms 防抖，显示内容不变。
- 采集写盘受 60 秒节流约束：同一账号每分钟最多写一次约 400 字节的文件，读 `.claude.json` 也只在要写盘时发生。
- 钩子的采集失败被吞掉，exec 之后的一切都与原命令相同。只有 `/bin/sh` 无法执行时状态栏为空，这种情况下原命令本来也跑不起来。

**反向**

- **多会话并发**：同一账号的多个会话会同时写同一快照文件。`atomic_write` 保证读到的是完整的旧版或新版，最后写入者胜出，两者都是有效的近期数据。
- **其它读写方**：
  - `.claude.json` 只读不写；
  - `config.json` 只读、不加写锁，因此钩子不会与 `add`、`apply` 等命令抢锁；
  - 被包装的 FILE 只由 install / uninstall 改写。
- **多份 statusLine 设置**：Claude 会合并多个设置来源，只有优先级最高的那份 statusLine 会生效。用户包装了一份不生效的文件就采不到数据，也不会出错。README 需写明：包装实际生效的那份文件（本机是经 `--settings` 传入的 `~/.claude-shared/settings.plugins.json`）。

**部署形态**

- 卸载 multi-claude 而没有先 `statusline uninstall` 时，包装命令找不到可执行文件，`/bin/sh` 返回 127，状态栏变为空。README 写明卸载顺序；备份文件可随时手工恢复。
- 升级 multi-claude 时路径不变，不受影响。路径变了（换了安装方式）时，重新运行 `install` 会更新路径。
- 非 Pro/Max 订阅、会话尚未收到 API 响应、未接受工作区信任：stdin 里没有 `rate_limits`，不写快照，`usage` 回落到缓存。行为与现在相同。
- default 身份账号（不设 `CLAUDE_CONFIG_DIR`）能被认出。直接运行 `claude` 而又没有 default 身份账号时，不写快照。

**对外语义**：JSON 只新增 `source` 字段。表格的 `UPDATED` 文字变长，没有脚本契约。

**隐私**：快照只含百分比、重置时间、配置目录路径和账号 UUID（`usage.py` 本来就读这个 UUID），权限 0600，不含 stdin 中的提示词、费用或工作目录。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。快照目录用测试临时目录下的 `XDG_CONFIG_HOME`；`PATH` 中放一个 `multi-claude` 包装脚本（`exec python3 -m multi_claude "$@"`）。“模拟 Claude 执行”的用例统一用 `subprocess.run(["/bin/sh", "-c", <包装后的命令>], input=<JSON 字节>)`，与 §3 中 Claude 的 `spawn(…, {shell:true})` 等价。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 钩子透传：原命令为 `cat; printf x >&2; exit 7` | stdout 与输入逐字节相同，stderr 为 `x`，退出码 7 |
| T2 | 采集：设 `CLAUDE_CONFIG_DIR` 为账号目录（也测经软链的路径），stdin 带两个窗口 | 生成 `usage/<dir>.json`，权限 0600，内容含两个窗口，不含 stdin 中的其它字段 |
| T3 | default 身份：不设 `CLAUDE_CONFIG_DIR`，有 default 身份账号 / 没有 | 前者写入该账号的快照，后者不写 |
| T4 | 失败路径：无 `rate_limits`、stdin 不是 JSON、`config.json` 损坏、快照目录不可写、`CLAUDE_CONFIG_DIR` 不属于任何账号 | 都不写快照；stdout 与原命令单独执行完全相同；退出码不变 |
| T5 | 节流：相同 `rate_limits` 在 60 秒内连续两次 / 数值变化 | 前者不改写文件（`captured_at` 不变），后者改写 |
| T6 | install：合法文件 / 重复执行 / 换 multi-claude 路径后再执行 | 首次包装并生成备份，其它键不变；重复执行输出 `unchanged`、不再备份；换路径后改写为新路径且原命令不变 |
| T7 | install 拒绝：文件不存在、非 JSON、没有 statusLine、type 不是 command、command 为空、PATH 中无 multi-claude | 退出 1，文件内容与修改时间不变 |
| T8 | 软链 FILE 与权限位 | 软链仍是软链，目标文件被更新，权限位与原来相同 |
| T9 | uninstall：已包装 / 未包装 | 前者恢复的命令与原命令逐字节相同；后者输出 `not installed`，退出 0 |
| T9b | install / uninstall 加 `--dry-run` | 输出将做的改动；文件内容、修改时间不变，不生成备份 |
| T10 | 引号往返：原命令取本机 claude-hud 那段 `bash -c '…'`（含 `'"'"'`、`$`、`\t`），以及含换行的命令 | install→uninstall 后与原命令相同；“模拟 Claude 执行”包装命令与直接执行原命令的 stdout 和退出码相同 |
| T11 | 合并：快照较新 / 较旧 / `config_dir` 不符 / `account_uuid` 不符 / 窗口已过重置时间 / 快照损坏 | 依次为：取快照（`source=statusline`）、取缓存、忽略快照、忽略快照、显示 `reset`、取缓存 |
| T12 | `usage`、`usage --json`、`list --json` | JSON 每账号有 `source`；表格 UPDATED 列在快照来源时含 `(statusline)` |
| T13 | 补全 | bash / zsh / fish 脚本含 `statusline`，`install uninstall` 候选与文件补全分支齐全；现有语法测试通过；帮助与补全中都不出现 `statusline-hook` |
| T14 | 回归 | 现有全部用例通过（基线 208 个），`apply` 生成的启动命令内容不变（启动命令相关测试不改即通过） |
| T15 | 实机（L3，需用户同意后在本机做） | 对 `~/.claude-shared/settings.plugins.json` 执行 install，开一个账号会话发一条消息：状态栏显示与之前相同；`~/.config/multi-claude/usage/<dir>.json` 出现；`multi-claude usage` 该账号显示 `(statusline)`；之后可 uninstall 还原 |

T1–T14 在编译机上闭合了软件控制流。T15 验证的是真实 Claude Code 的执行方式与环境继承（§3 中来自二进制阅读的结论），只能在装有真实 Claude 的会话里做。

## 9. 日志 / 观测点

- **install / uninstall**：stdout 打印 `statusline: wrapped <目标>`（或 `restored` / `unchanged` / `not installed`）与备份路径；错误信息带上目标路径与原因（如 `no command statusLine in <path>`）。
- **钩子**：成功与采集失败都不输出，因为 stdout 是状态栏、stderr 会进 Claude 的调试日志。排查时看快照文件是否存在、`captured_at` 是否在更新。exec 失败时 stderr 有 `multi-claude statusline-hook: cannot run /bin/sh: …`，在 `claude --debug` 的日志里能看到。
- **用量**：`usage --json` 的 `source` 与 `fetched_at` 说明当前展示的数值来自哪一份数据。
