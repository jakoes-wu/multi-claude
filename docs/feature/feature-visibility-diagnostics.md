# multi-claude v0.2：用量、诊断、补全与鉴权变量隔离

> 2026-10-01 注记：plan-review 2 轮收敛，用户同意编码；代码已按方案实现，待独立代码评审与编译机验证，未提交。来源：`docs/analysis/competitor-analysis.md` §6.1 与 §7 的用户决定（用量只用零凭据来源；启动命令默认清除鉴权覆盖变量）。基线：main `61e5d41`（v0.1.0）。

## 1. 背景

v0.1.0 已发布（`docs/feature/feature-account-manager.md`）。与竞品对比后，用户决定在 v0.2 补齐以下能力：

- 查看各账号的 5 小时 / 7 天用量；
- 一条命令检查多账号环境是否健康；
- shell 补全；
- 机器可读的 `list` 输出；
- 启动命令清除会让所有账号共用同一凭据的环境变量。

现状：

- `list`（`src/multi_claude/cli.py:396` `cmd_list`）只输出人读表格，没有用量、没有 JSON。
- 鉴权覆盖变量只在 `list` 与 `migrate-default` 时告警（`cli.py:398` 调 `migrate.warn_isolation_env`，列表在 `src/multi_claude/platform.py:26` `ISOLATION_BREAKING_ENV`），启动命令不处理。
- 没有诊断命令和补全。

## 2. 目标 / 非目标

**目标**

1. `multi-claude usage`：只读显示每个账号最近一次已知的 5 小时 / 7 天用量与数据年龄，不读凭据、不联网。
2. `multi-claude doctor`：只读检查配置、PATH、启动命令、账号目录、共享软链、登录与环境变量，`--json` 输出，有错误时退出码 1。
3. `multi-claude completion bash|zsh|fish`、`list --json`、`list --names`；启动命令清除 4 个鉴权覆盖变量。

**非目标**

- 实时用量（需要读 OAuth 令牌，用户已否决）；statusline 采集放在 v0.4 方案。
- doctor 自动修复；检测 shell 别名或函数对启动命令的遮蔽（需要运行用户的交互式 shell，有副作用）；钥匙串孤儿条目（需要枚举钥匙串）。
- 给需要 API key 的账号提供“保留鉴权变量”的开关（另行设计；`env` 命令仍拒绝这些键）。
- PowerShell 补全、Windows。

## 3. 假设与约束

- 只用 Python 标准库，兼容 3.8。
- 用量来源是 Claude Code 写在 `.claude.json` 里的 `cachedUsageUtilization`。这是未写入官方文档的内部字段，2026-10-01 在 Claude Code 2.1.286 上只读核实：
  - 结构：`{"fetchedAtMs": int, "accountUuid": str, "utilization": {"five_hour": {"utilization": int, "resets_at": str|null, ...}, "seven_day": {...}, "limits": [...] , ...}}`；
  - `resets_at` 形如 `2026-10-01T12:00:00.123456+00:00`，`datetime.fromisoformat` 可解析；
  - `accountUuid` 与同一文件 `oauthAccount.accountUuid` 一致；
  - 刷新不及时：开发机上正在使用的账号，缓存 `fetchedAtMs` 已是 45 小时前；Claude Code 何时刷新尚未核实。
  因此只能作为“最近一次已知值”展示，必须显示数据年龄，任何字段缺失或类型不符都降级为“无数据”而不是报错。
- `.claude.json` 的位置：dir 身份为 `<账号目录>/.claude.json`，default 身份为 `~/.claude.json`（与 `accounts.py:311-312` `settings_proxy_overrides` 的取法一致）。Claude 另有遗留路径 `<配置目录>/.config.json`，存在时优先（v0.1 方案 §3 依据 4）；本版本不读它，遇到时用量显示为 `no-data`。只读，不写。
- 读取 `.claude.json` 时只取 `cachedUsageUtilization` 与 `oauthAccount.accountUuid` 两处，不输出其它内容（该文件含邮箱、组织名等）。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `61e5d41`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/usage.py` | 新文件 | 新增 | 读取并解析用量缓存 |
| `src/multi_claude/doctor.py` | 新文件 | 新增 | 诊断检查项与输出 |
| `src/multi_claude/completion.py` | 新文件 | 新增 | 生成 bash / zsh / fish 补全脚本 |
| `src/multi_claude/launcher.py` | `render` :45-78，在 :65 `_identity_lines` 之后 | 修改 | 追加一行 `unset` 4 个鉴权变量 |
| `src/multi_claude/launcher.py` | 模块常量区 :26-31 邻近 | 新增 | `AUTH_OVERRIDE_ENV` 常量 |
| `src/multi_claude/platform.py` | `ISOLATION_BREAKING_ENV` :26-28 | 不改 | 仅被引用 |
| `src/multi_claude/migrate.py` | `warn_isolation_env` :209 | 修改 | 遍历范围改为 `ISOLATION_BREAKING_ENV` 与 `AUTH_OVERRIDE_ENV` 的并集；对启动命令已清除的变量改用新告警文字 |
| `src/multi_claude/cli.py` | `build_parser` :27-98 | 修改 | 新增 `usage`、`doctor`、`completion` 子命令；`list` 加 `--json`、`--names` |
| `src/multi_claude/cli.py` | `main` :132-185，:152 `pending_journal_notice()` 与 :155 `if args.command == "list"` 分支 | 修改 | 只读命令的入口分派（§5.1.6） |
| `src/multi_claude/cli.py` | `cmd_list` :396-439 | 修改 | 拆出“收集数据”与“表格输出”，供 `--json` 复用 |
| `tests/test_usage.py`、`tests/test_doctor.py`、`tests/test_completion.py` | 新文件 | 新增 | 见 §8 |
| `tests/test_accounts.py` | `LauncherTest` | 新增用例 | 鉴权变量清除 |
| `README.md`、`README.zh-CN.md`、`CHANGELOG.md` | 命令表、新增小节 | 修改 | 文档同步 |
| `src/multi_claude/__init__.py` | :3 | 修改 | 版本 0.2.0（发布时） |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 用量 `usage.py` 与 `multi-claude usage`

```python
class UsageWindow(NamedTuple):
    percent: float          # 0-100，取自 utilization（缓存里实测为整数，也接受小数）
    resets_at: Optional[datetime]  # 解析失败或为 null 时 None

class UsageReport(NamedTuple):
    status: str             # ok / stale / no-data / other-account / unreadable
    fetched_at: Optional[datetime]
    five_hour: Optional[UsageWindow]
    seven_day: Optional[UsageWindow]

def read_usage(path: str, now: datetime) -> UsageReport
```

判定顺序：

1. 文件不存在 → `no-data`；读取或 JSON 解析失败 → `unreadable`。
2. `cachedUsageUtilization` 不是对象，或 `fetchedAtMs` 不是整数 → `no-data`。
3. `cachedUsageUtilization.accountUuid` 与 `oauthAccount.accountUuid` 都是字符串且不相等 → `other-account`（缓存属于以前登录的账号），不展示数值。任一方缺失时不做此判断。
4. 取 `utilization.five_hour`、`utilization.seven_day`：对象且 `utilization` 为 int 或 float（排除 bool）且有限时生成 `UsageWindow`，否则该窗口为 None。表格显示四舍五入到整数，JSON 原样输出数值。两个窗口都为 None → `no-data`。
5. `now - fetched_at` 超过 `STALE_AFTER = 1 小时` → `stale`，否则 `ok`。`stale` 仍展示数值。
6. 窗口的 `resets_at` 早于 `now` 时，该窗口展示为“已重置”（数值不再有意义），与官方 statusline 文档“过了 `resets_at` 即丢弃该窗口”的语义一致。

命令输出（人读）：

```text
NAME                    5H         7D         UPDATED
jakoes.wu.us@gmail.com  23% 14:00  41% Fri    45h ago (stale)
work                    -          -          no data (start claude-work once)
```

- 百分比后是重置时间：24 小时内显示本地 `HH:MM`，否则显示固定英文星期缩写（用 `Mon`…`Sun` 常量按 `weekday()` 取，不用 `%a`，避免受系统语言影响）。
- 表下一行固定说明：“Values come from Claude Code's own cache (.claude.json) and may be out of date; multi-claude never reads credentials.”
- `usage NAME` 只显示一个账号；未登记返回 1。
- `usage --json` 输出 `{"schema_version": 1, "configured": bool, "accounts": [{"name": ..., "usage": {...}}]}`，`usage` 的结构与 §5.1.4 相同：`status`、`fetched_at`、`age_seconds`（无数据时为 null）、`five_hour` / `seven_day`（各含 `percent`、`resets_at`、`reset`：是否已过重置时间）。

#### 5.1.2 诊断 `doctor.py` 与 `multi-claude doctor`

每项结果 `Check(id, level, subject, message)`，`level` 为 `ok`、`warn`、`error`。检查项全部只读：

| id | 对象 | error 条件 | warn 条件 |
| ---- | ---- | ---- | ---- |
| `config` | 配置文件 | 存在但解析失败 | 不存在（尚未使用） |
| `journal` | 迁移事务记录 | 存在未完成的迁移 | — |
| `claude-on-path` | `claude` | `shutil.which("claude")` 为空 | — |
| `bin-on-path` | 启动命令目录 | — | `expand(bin_dir)` 不在 `PATH` 中 |
| `launcher` | 每个账号 | `accounts.launcher_status` 为 missing / conflict / stale | — |
| `launcher-shadowed` | 每个账号 | — | `shutil.which("claude-<名称>")` 不是本工具生成的那个文件 |
| `account-dir` | 每个账号 | 目录不存在，或路径存在但不是目录 | 权限对组或其他用户可读写（`mode & 0o077`） |
| `default-link` | default 身份账号 | `default_link_status` 为 broken / missing | — |
| `shared-link` | 每个账号的每个 `shared.items` 条目 | — | 是软链但目标不存在（断链，无论是否本工具所建） |
| `login` | 每个账号 | — | `identity.probe` 为 `none` 或 `unknown` |
| `auth-env` | 当前环境 | — | `ISOLATION_BREAKING_ENV` 与 `AUTH_OVERRIDE_ENV` 的并集中任一变量已设置；对 `AUTH_OVERRIDE_ENV` 中的 4 个，提示“启动命令不受影响，但直接运行 `claude` 会用它” |

输出：每项一行 `[ok|warn|error] <id> <subject>: <message>`，`ok` 项默认只汇总计数，`--verbose` 时逐条列出；`--json` 输出 `{"schema_version": 1, "checks": [...], "summary": {"ok": n, "warn": n, "error": n}}`。退出码：有 `error` 返回 1，否则 0（只有 `warn` 也返回 0）。

doctor 不修复任何东西，每条 error / warn 的 message 里写清修复命令（例如 `multi-claude apply`、`chmod 700 <目录>`）。

#### 5.1.3 鉴权覆盖变量

`launcher.py` 新增：

```python
# 在 shell 里导出后，会让通过任何启动命令启动的账号都改用这一份凭据（官方 env-vars 页的凭据变量）。
AUTH_OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_REFRESH_TOKEN")
```

插入位置：`launcher.py:65` `lines.extend(_identity_lines(...))` 之后、:66 额外环境变量循环之前，追加一行 `unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_REFRESH_TOKEN`，两种身份都加。额外环境变量不会再设回它们：`config.validate_env_key`（`config.py:298`）已拒绝这 4 个键。

`migrate.warn_isolation_env`（`migrate.py:209`）改为遍历 `ISOLATION_BREAKING_ENV` 与 `AUTH_OVERRIDE_ENV` 的并集（去重、保持原顺序后接新增项，即补上 `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`），告警文字分两种：在 `AUTH_OVERRIDE_ENV` 中的变量写“launchers clear it, but running `claude` directly uses it”；其余变量保持原文字。doctor 的 `auth-env` 检查用同一个并集。

升级影响：启动命令内容改变，升级后已有启动命令在 `list` 中显示 `stale`、`doctor` 报 error，运行一次 `multi-claude apply` 即全部更新。`account_dir=` 行不变，不触发 login-bound 冲突（`accounts.py:186-193`）。

#### 5.1.4 `list --json` 与 `list --names`

`cmd_list` 拆成 `_collect_list(config, with_usage) -> dict` 与表格输出两部分。`--json` 输出：

```json
{
  "schema_version": 1,
  "configured": true,
  "root": "/Users/x/.cc",
  "bin_dir": "/Users/x/.local/bin",
  "shared_dir": null,
  "accounts": [
    {"name": "work", "identity": "dir", "dir": "/Users/x/.cc/work", "dir_exists": true,
     "proxy": "inherit", "shared": false, "launcher": "ok", "login": "keychain", "link": null,
     "keychain_service": "Claude Code-credentials-1a2b3c4d", "credentials_file": "/Users/x/.cc/work/.credentials.json",
     "usage": {"status": "stale", "fetched_at": "2026-09-29T08:00:00+00:00", "age_seconds": 187200,
               "five_hour": {"percent": 23, "resets_at": "2026-10-01T14:00:00+00:00", "reset": false},
               "seven_day": null}}
  ]
}
```

- 配置不存在时输出 `{"schema_version": 1, "configured": false, "accounts": []}`，退出码 0。
- `--json` 时不输出表格，告警仍走 stderr。
- `--names` 每行输出一个账号名，不输出其它内容（给补全用）；配置不存在时无输出。
- `--json`、`--names`、`--verbose` 互斥（argparse 互斥组）。

#### 5.1.5 补全 `completion.py` 与 `multi-claude completion SHELL`

- 输出对应 shell 的补全脚本到 stdout，用户自行 `source` 或写入补全目录；README 给出三种 shell 的安装方法。不修改任何 shell 配置文件。
- 补全内容：子命令；各子命令的选项（由 `build_parser()` 遍历 argparse 结构生成，避免手写清单与实际参数不一致。遍历要用 `parser._actions` 与 `argparse._SubParsersAction.choices` 这两个未写入文档的属性，Python 3.11 实测可用；兼容性由 §8 第 6 条在 3.8 与 3.12 上的测试兜底）；接受账号名的位置参数（`migrate-default`、`add`、`proxy`、`env`、`args`、`remove`、`usage` 的 NAME）调用 `multi-claude list --names 2>/dev/null` 取账号名；`completion` 后补 `bash zsh fish`；`proxy NAME` 的值补 `off inherit`。
- 账号名含 `@`、`+`、`.`。bash 的 `COMP_WORDBREAKS` 默认含 `@` 与 `:`，`COMP_WORDS` 会把 `a@b` 拆开，所以 bash 脚本不依赖 `COMP_WORDS` 取当前词，也不依赖 bash-completion 包：①用 `${COMP_LINE:0:COMP_POINT}` 截取光标前文本，取最后一个空格之后的部分作为完整当前词 `cur`；②`compgen -W "$names" -- "$cur"` 得到完整候选；③若 `cur` 含 `COMP_WORDBREAKS` 中的字符，把每个候选去掉“`cur` 中最后一个分词字符及其之前的部分”的同长前缀，再放进 `COMPREPLY`（bash 只替换最后一个分词片段）。zsh、fish 不存在此问题，直接用完整名称。

#### 5.1.6 只读命令的入口

`main`（`cli.py:132`）现有顺序是：先调 `migrate.pending_journal_notice()`（:152，迁移记录损坏时抛 `JournalError`，由 :164 捕获后返回 1），再在 :155 分派 `list`；配置损坏时 `load_config` 抛 `ConfigError`，由 :180 捕获后返回 1。新规则：

- `completion`：在 :152 之前分派，不读配置、不读迁移记录，任何状态下都返回 0。
- `doctor`：同样在 :152 之前分派。它自己调用 `migrate.load_journal()` 与 `config.load_config()`，把 `JournalError`、`ConfigError` 转成 `journal` / `config` 检查项的 error，继续执行不依赖配置的检查（`claude-on-path`、`auth-env`），`--json` 输出始终是合法 JSON；配置读不出时跳过按账号的检查项。
- `list`、`usage`：沿用 :152 之后、:155 处的分派，不加写锁，迁移未完成时放行；迁移记录或配置损坏时与现在的 `list` 一样输出错误并返回 1（即使带 `--json` 也不输出 JSON，退出码非 0 即表示无结果）。

### 5.2 接口变更

- 新增子命令：`usage [NAME] [--json]`、`doctor [--json] [--verbose]`、`completion {bash,zsh,fish}`。
- `list` 新增 `--json`、`--names`。表格输出不变。
- 启动命令内容新增一行 `unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_REFRESH_TOKEN`。行为变化：从导出了这些变量的 shell 启动 `claude-<名称>`，不再使用这些变量。
- 退出码：`doctor` 有 error 返回 1；`usage NAME` 未登记返回 1；其余沿用。
- JSON 输出契约：顶层 `schema_version: 1`，字段只增不删；字段改名或删除时版本号加 1。
- 配置文件格式：无变化。

本项目没有 `docs/reference/*`，不涉及 reference 章节的 sibling 回补检查。

## 6. 备选方案与决策

- **用量来源**：读 OAuth 令牌调 `/api/oauth/usage` 能得到实时数据，用户已否决（违反“不读凭据”）。statusline 采集需要改 settings，放到 v0.4。
- **doctor 自动修复（`--fix`）**：修复动作（`chmod`、`apply`）已有对应命令，doctor 只给出命令更简单、无副作用。不做。
- **补全脚本手写 vs 由 argparse 生成**：手写容易与实际参数漂移。选择从 `build_parser()` 生成。
- **鉴权变量：清除 vs 只告警**：用户决定默认清除。

## 7. 影响分析

**正向推**

- 启动命令内容变化 → 所有已有启动命令 `stale`（`accounts.launcher_status`，`accounts.py:252-262`）→ `apply` 时逐个 `update`。`_plan_launcher` 的 login-bound 比对只看 `account_dir=` 行（`accounts.py:186-193`，`launcher.rendered_account_dir` :145），该行不变，不会判冲突。
- 启动命令新增的 `unset` 只影响通过启动命令启动的进程；直接运行 `claude` 不受影响。
- `cmd_list` 拆分后表格输出必须逐字节不变（已有测试 `tests/test_accounts.py` `ListTest` 的正则断言）。
- `usage` 与 `list --json` 每次读每个账号的 `.claude.json`：开发机上该文件约 200KB，4 个账号读取与解析在百毫秒内。
- `doctor` 对每个账号调用一次 `identity.probe`（macOS 上一次 `security`），与 `list` 相同。

**反向推**

- `.claude.json` 由 Claude Code 频繁写入。本工具只读；Claude 用“写临时文件再 rename”还是原地写尚未核实，读到半写内容时 JSON 解析失败，归为 `unreadable`，不影响其它账号。
- `main` 中只读命令绕过迁移闸门：这几个命令不写任何文件，与 `list` 现有行为一致。
- `migrate.warn_isolation_env` 文字变化：`tests/test_accounts.py` `EnvironmentWarningTest` 只断言变量名出现，需确认仍通过。

**运行时**

- 不新增进程（`security` 调用沿用）；不联网。

**对外语义**

- 用户若依赖“在 shell 里导出 `ANTHROPIC_API_KEY` 后用 `claude-<名称>` 启动”的行为，升级后该行为消失。CHANGELOG 与 README 写明，并说明替代做法：直接运行 `claude`，或把 key 写进账号 `settings.json` 的 `env` / 使用 `apiKeyHelper`。

## 8. 回归测试

沿用 `tests/helpers.py` 的安全闸（临时 HOME、假 claude、假 security、无 `CLAUDE_*` 变量）。

1. **用量解析**（`test_usage.py`，用合成的 `.claude.json`）：
   - 正常：5h / 7d 百分比与重置时间正确，`fetchedAtMs` 在 10 分钟前为 `ok`，2 小时前为 `stale`；
   - 文件不存在 → `no-data`；非法 JSON → `unreadable`；`cachedUsageUtilization` 缺失、`fetchedAtMs` 非整数、`utilization` 非数字 → `no-data` 或对应窗口为 null；
   - `accountUuid` 与 `oauthAccount.accountUuid` 不同 → `other-account`，输出中不出现数值；任一缺失时照常显示；
   - `resets_at` 早于当前时间 → 显示“已重置”；`resets_at` 为 null → 只显示百分比；
   - default 身份读 `~/.claude.json`；
   - 输出中不出现 `.claude.json` 里的其它值（构造含 `emailAddress` 的文件，断言输出不含该邮箱）。
2. **`usage` 命令**：表格列与说明行；`usage NAME` 未登记返回 1；`--json` 结构与 `schema_version`。
3. **doctor**（`test_doctor.py`）：逐项构造 error / warn 场景并断言 id、level、退出码：
   - 配置非法 → error，退出码 1；无配置 → warn，退出码 0；
   - 未完成的迁移事务 → error；
   - PATH 中没有 claude → error；bin_dir 不在 PATH → warn；
   - 启动命令被删除 / 内容过期 / 被非受管文件占用 → error；PATH 中更靠前处有同名可执行文件 → warn；
   - 账号目录不存在 → error；目录权限 0755 → warn；
   - default 账号软链被删 → error；
   - 共享条目断链 → warn；
   - 假 security 返回 44 → login warn；
   - `ANTHROPIC_API_KEY` 已设置 → auth-env warn，且输出不含其值；
   - 一切正常 → 全部 ok、退出码 0；`--json` 的 summary 计数正确。
4. **鉴权变量清除**（`test_accounts.py`）：父环境设置 4 个变量后运行 dir 身份与 default 身份启动命令，假 claude 都看不到它们；其它变量（如 `MAX_THINKING_TOKENS`）照常传递；升级场景：用旧模板写一个启动命令，`list` 显示 stale，`apply` 后 ok，且不报 login-bound 冲突。
5. **list**：`--json` 的字段与类型；无配置时的输出；`--names` 每行一个名称、无配置时无输出；`--json` 与 `--names` 同时给出返回 2；原表格输出与 v0.1.0 一致（现有用例不改）。
6. **补全**（`test_completion.py`）：三种 shell 的脚本都能被对应 shell 语法检查（`bash -n`、`zsh -n`、`fish --no-execute`，未安装的 shell 跳过）；脚本包含全部子命令；bash 下用 `COMP_WORDS` 模拟补全 `multi-claude proxy ` 能得到账号名（含 `@` 的名称完整出现）。
7. **只读命令不受迁移闸门影响**：存在未完成的迁移事务时，`usage`、`list --json`、`completion bash` 返回 0；`doctor` 返回 1 并报 journal error。迁移记录损坏或配置损坏时：`completion bash` 返回 0；`doctor --json` 输出合法 JSON、对应检查项为 error、退出码 1；`list --json`、`usage` 返回 1。
8. **全量回归**：v0.1.0 的全部用例通过；shellcheck 检查新模板生成的两种身份启动命令。

## 9. 日志 / 观测点

- `usage`、`doctor` 的输出本身即观测结果；`doctor` 每条非 ok 结果带修复命令。
- 告警沿用 `[multi-claude] warning:` 前缀（`actions.warn`）。
- 任何输出都不包含环境变量的值（`CLAUDE_CONFIG_DIR`、`HOME` 除外）与 `.claude.json` 中除用量外的内容。

## 10. 开放问题

- `cachedUsageUtilization` 的刷新时机未核实，数据可能长期不更新。v0.4 的 statusline 采集用于补足；本版本只如实显示年龄。
