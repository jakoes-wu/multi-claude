# list 显示手填的续费日

> 2026-10-16 注记：方案已定，代码待落地。设计评审第 1 轮 0 高 5 中 5 低，已全部修订（D1–D10）；第 2 轮 0 高 1 中 2 低，已修订（D11–D13）；第 3 轮 0 高 0 中，收敛，可进入编码。基线：`docs/security-session-dirs` 分支，最新提交 `b9be958`（v0.11.1 合并后；本机已装 0.12.1）。

> 2026-10-17 注记：已落地，分支 `feature/renewal-date`（基于 `origin/main` 64d9479，v0.12.1；§4 行号仍是 b9be958 的锚点）。落地位置：`src/multi_claude/config.py:100`（属性）、`:393`（`validate_renews`）；`src/multi_claude/cli.py:103`（`_examples_lines`）、`:403`（`--renews`）、`:671`（写入）、`:1507`（`_renews_cell`）、`:1530`（`_report_renewals`）、`:1659`（`--json` 的 `renews`）。`--json` 路径改为由 `cmd_list` 取一次今天传给 `_list_data`，JSON 与 stderr 提示跨午夜也一致（代码评审低 5）。验证：本机 macOS 全量 512 条通过；编译机 Ubuntu 20 / Python 3.8.10 全量 512 条通过（跳过 7）；L1 本机实测显示 `11-10 (24d)`（10-17 当天），`--renews off` 后列消失、配置还原。代码评审 1 轮，0 高 0 中，可合并。无未修子项（低 2“子命令帮助示例行超过 80 列”沿用 §5.1.4 的示例，不改）。

## 1. 背景

用户希望 `multi-claude list` 显示每个账号套餐的到期时间与续订状态。

2026-10-16 评估（只读探针，4 个令牌有效的账号）：`list` 查套餐用的 `GET /api/oauth/profile`（`src/multi_claude/plan.py:41`）返回的 `organization` 里没有到期日、续费日、计费周期结束这类字段，和时间有关的只有 `subscription_created_at`、`subscription_status`、`claude_code_trial_ends_at`（后者全为空）。

用 `jakoes.wu@icloud.com` 对照苹果订阅页（用户提供截图：Claude Max 5x - Monthly，Renews November 10）：

| 项目 | 接口返回 | 苹果订阅页 |
| ---- | ---- | ---- |
| 到期日 | `subscription_created_at` 为 7 月 7 日，按整月推算是 11 月 7 日 | 11 月 10 日 |
| 状态 | `subscription_status` = `canceled` | 会自动续费 |

用户说明该账号中途停过订阅。接口这两个字段停留在中断前的旧订阅上，而且从返回内容里分辨不出一个账号是否停过，所以推算的日期和状态都不能显示。苹果、谷歌商店也没有给普通用户查询自己订阅的公开接口。

用户决定（2026-10-16）：改为**手填续费日**，`list` 显示它；`--help` 中显示所有示例用法。

## 2. 目标 / 非目标

**目标**

1. `add` / `set` 新增 `--renews DATE`（`YYYY-MM-DD`）与 `--renews off`，把续费日记在该账号的配置里。
2. `list`（简表、`--verbose`、`--json`）显示续费日与剩余天数；`--offline` 同样显示（不联网）。
3. `set`、`add`、`list` 三个子命令的 `--help` 末尾给出覆盖全部选项的示例。

**非目标**

- 不从服务端接口推算到期日，也不显示 `subscription_status`（理由见 §1）。
- 日期过了不自动顺延一个月：停过订阅的账号会被显示成仍在续费。
- 不发通知、不做定时提醒。
- 不改其它子命令的 `--help`（只改本方案涉及的 `set`、`add`、`list`）。

## 3. 假设与约束

- 剩余天数按本机时区的“今天”计算：新增 `_local_today()` 返回 `datetime.now().astimezone().date()`，单元格函数 `_renews_cell(renews, today)` 以参数接收今天，便于测试，与 `5H` / `7D` 列已用本地时间显示的做法一致（`cli.py:1575`）。
- 配置里只在设置了续费日的账号上写出 `renews` 键；没设置的账号，`config.json` 与改动前逐字相同（沿用 `shared_exclude` 的做法，`config.py:106-110`）。
- 没有任何账号设置续费日时，简表与 `--verbose` 表格的输出与改动前完全相同（不加列）。

## 4. 涉及模块

行号为基线 `b9be958` 的当前状态。

| 文件 | 行号锚点 | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/config.py` | `:80-95` `Account.__init__` | 修改 | 新增参数与属性 `renews: Optional[str] = None` |
| `src/multi_claude/config.py` | `:97-99` `Account.copy` | 修改 | 复制 `renews` |
| `src/multi_claude/config.py` | `:101-111` `Account.to_dict` | 修改 | `renews` 非空时写出 |
| `src/multi_claude/config.py` | `:348-375` `_parse_account` | 修改 | 读取并校验 `renews` |
| `src/multi_claude/config.py` | `_parse_account` 之后 | 新增 | `validate_renews(value) -> str` |
| `src/multi_claude/cli.py` | `:49` `COMMAND_GROUPS` 的 `set NAME` 一行 | 修改 | 说明加上 renewal date |
| `src/multi_claude/cli.py` | `:72-77` `_EXAMPLES` | 修改 | 加一条 `set work --renews 2026-11-10` |
| `src/multi_claude/cli.py` | `:90-99` `_help_epilog` | 修改 | 示例列宽由固定 36 改为按最长示例加 2 计算（见 §5.1.4、§7 反向 4） |
| `src/multi_claude/cli.py` | `:136`、`:141-142`、`:171-172` 三个 `add_parser` | 修改 | 加 `epilog` 示例与 `RawDescriptionHelpFormatter`；`description` 手动用 `\n` 换行 |
| `src/multi_claude/cli.py` | `:304-327` `_add_account_options` | 修改 | 新增 `--renews` |
| `src/multi_claude/cli.py` | `:587` `_apply_shared_exclude` 调用之前 | 新增 | 写入 `account.renews` |
| `src/multi_claude/cli.py` | `:1157-1159` `_has_account_option` | 修改 | 计入 `--renews` |
| `src/multi_claude/cli.py` | `:1346`、`:1352-1354` 详细表 | 修改 | 有续费日时加 `RENEWS` 列 |
| `src/multi_claude/cli.py` | `:1417`、`:1423-1426` 简表 | 修改 | 同上 |
| `src/multi_claude/cli.py` | `:1329`（`--json`）、`:1365`（详细表）、`:1431`（简表）三处 `_report_plan_problems` 之后 | 新增 | 已过期续费日的 stderr 提示 |
| `src/multi_claude/cli.py` | `:1495` `_account_entries` | 修改 | 加 `renews` 键 |
| `src/multi_claude/cli.py` | `:1370` `_PLAN_HEADER` 附近 | 新增 | `_local_today()`、`_renews_cell(renews, today)`、`_renews_dict(renews, today)`、`_report_renewals(config, today)` |
| `tests/test_renewal.py` | 新文件 | 新增 | §8 的单元测试 |
| `README.md`、`README.zh-CN.md` | 两份行号相同：`:289` 套餐小节之后 | 新增 | “续费日”小节（填法、列的读法、`--json` 字段、`apply -f` 文件缺省会清除） |
| `README.md`、`README.zh-CN.md` | 两份行号相同：`:481` add、`:482` set、`:488` list 三行命令表 | 修改 | add / set 补 `[--renews DATE\|off]`；list 补 RENEWS 列与 `renews` 键 |
| `CHANGELOG.md` | 顶部未发布段 | 修改 | 记一条新功能，含“装回旧版本后再改设置会丢失续费日”的说明 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 配置字段

`Account` 新增属性 `renews`：`None` 或 `YYYY-MM-DD` 字符串。

`validate_renews(value)`（`config.py`，放在 `_parse_account` 之后）：

- 必须是字符串，整串匹配 `\d{4}-\d{2}-\d{2}`（用 `fullmatch`）；
- 再用 `datetime.strptime(value, "%Y-%m-%d")` 确认是真实日期（拒绝 `2026-02-30`）；
- 不合法抛 `ValueError("renewal date must be YYYY-MM-DD, e.g. 2026-11-10")`。

`_parse_account`（`config.py:348-375`）：`value.get("renews")` 为 `None` 时为 `None`；否则经 `validate_renews` 校验，失败转成 `ConfigError("{where}: 'renews' must be a date YYYY-MM-DD")`。`apply -f` 读入的配置走同一条路径，因此声明式配置同样支持这个字段。

`to_dict`（`config.py:101-111`）：`renews` 非空时写 `data["renews"] = self.renews`。`copy`（`:97-99`）把它传给新对象。`rename`（`cli.py:834-836`）移动的是同一个 `Account` 对象，续费日随账号保留，无需改动。

#### 5.1.2 命令行

`_add_account_options`（`cli.py:304`，`--shared-include` 之后、`_add_write_options` 之前）新增：

```python
# 续费日由用户照着 App Store / Google Play 的订阅页手填：服务端接口给不出准确的到期日（方案 feature-renewal-date §1）。
parser.add_argument("--renews", metavar="DATE",
                    help="renewal date of the subscription, YYYY-MM-DD as shown by the store; off to clear it")
```

`add` 与 `set` 都有这个选项（共用函数）。处理位置：`cli.py:587` 之前插入：

```python
if args.renews is not None:
    account.renews = None if args.renews == "off" else _checked_renews(args.renews)
```

`_checked_renews` 把 `ValueError` 转成 `UsageError`（退出码 2），与 `_checked_proxy` 的做法一致。`_has_account_option`（`:1157-1159`）加上 `args.renews is not None`，使 `set NAME --renews …` 不再报 “nothing to set”。

只改续费日时，`accounts.plan` 只会因 `dump_config(old) != dump_config(new)`（`accounts.py:121`）产生一条写配置的动作，不涉及任何文件链接或启动命令；`--dry-run` 照常只打印不写。

#### 5.1.3 list 的显示

列名 `RENEWS`。只要有一个账号设置了续费日就加这一列：

- 简表：位置在 `PLAN BILLING` 之后（`--offline` 时在 `LOGIN` 之后）、`PROXY` 之前；
- `--verbose`：位置在 `LINK` 之前。

单元格（今天 = 本机时区的日期，`days = (renews - today).days`）：

| 情况 | 显示 | 例子 |
| ---- | ---- | ---- |
| 没设置 | `-` | `-` |
| 今年，`days > 3` | `MM-DD (Nd)` | `11-10 (25d)` |
| 今年，`1 <= days <= 3` | `MM-DD (Nd!)` | `10-19 (3d!)` |
| `days == 0` | `MM-DD (today!)` | `10-16 (today!)` |
| `days < 0` | `MM-DD (Nd ago)` | `10-10 (6d ago)` |
| 与今天不同年 | 日期写成 `YYYY-MM-DD`，括号规则同上 | `2027-01-05 (81d)` |

有已过期日期的账号时，表格之后向 stderr 打一行（与 `token expired` 提示同样走 stderr，不混进 `--json`）：

```text
[multi-claude] renewal date has passed for work, home: check the store's subscription page, then run multi-claude set NAME --renews YYYY-MM-DD (or --renews off)
```

`--json`：`_account_entries`（`cli.py:1495` 之后）每个账号总是带 `renews` 键：

```json
"renews": {"date": "2026-11-10", "days_left": 25}
```

没设置时为 `null`。`days_left` 可为负数。`--names` 不变。

#### 5.1.4 帮助与示例

`set`、`add`、`list` 三个 `add_parser` 加 `epilog` 并设 `formatter_class=argparse.RawDescriptionHelpFormatter`（保留换行，与主帮助 `cli.py:110` 相同）。这种格式下 `description` 不再自动折行，所以三者的 `description` 都像主帮助（`cli.py:106-107`）一样手动加 `\n`，每行不超过 80 字符。示例覆盖各自全部选项（`--help` 除外；`--verbose` 只在 `list` 里是独立功能，`set` / `add` 的 `--verbose` 来自公共写选项 `cli.py:331-334`，不要求示例）：

`multi-claude set --help` 末尾：

```text
Examples:
  multi-claude set work --proxy 7901                set the proxy to http://127.0.0.1:7901
  multi-claude set work --proxy off                 start claude-work without a proxy
  multi-claude set work --shared                    link the shared items (default ~/.claude-shared)
  multi-claude set work --shared ~/claude-common    make ~/claude-common the shared directory for every account
  multi-claude set work --no-shared                 remove the shared links of this account
  multi-claude set work --shared-exclude skills     do not link skills into this account
  multi-claude set work --shared-include skills     link skills again
  multi-claude set work --renews 2026-11-10         record the renewal date shown by the App Store / Google Play
  multi-claude set work --renews off                forget the renewal date
  multi-claude set work --renews 2026-11-10 --dry-run   show what would change without writing
```

`multi-claude add --help` 末尾：

```text
Examples:
  multi-claude add work                             create account work and its launcher claude-work
  multi-claude add work --proxy 7901 --shared       with a proxy and the shared items
  multi-claude add work --no-shared                 keep the account without shared links
  multi-claude add work --shared-exclude skills     share everything except skills
  multi-claude add work --shared-include skills     share skills again
  multi-claude add work --renews 2026-11-10         also record the renewal date
  multi-claude add work --shared --adopt            take over shared links that already exist
  multi-claude add work --config-from home          copy settings.json from account home once
  multi-claude add work --dry-run                   show what would be created
```

`multi-claude list --help` 末尾：

```text
Examples:
  multi-claude list                    accounts, plan, usage; RENEWS appears once a renewal date is set
  multi-claude list --offline          no token read, no network (RENEWS still shown)
  multi-claude list --no-refresh       never renew an expired access token
  multi-claude list --verbose          every field and the global settings
  multi-claude list --json             for scripts; each account has "renews": {"date", "days_left"} or null
  multi-claude list --names            account names only

RENEWS: the date you recorded with `multi-claude set NAME --renews YYYY-MM-DD`;
(25d) days left, (3d!) three days or less, (today!) renews today,
(6d ago) passed: update it after the store renews.
```

主帮助：`COMMAND_GROUPS` 里 `set NAME` 的说明（`cli.py:49`）改为 `change an account: proxy, sharing, renewal date`；`_EXAMPLES`（`cli.py:72`）加一条 `multi-claude set work --renews 2026-11-10`，说明 `record when the plan renews`。该示例 41 字符，超过 `_help_epilog`（`cli.py:97`）固定的 36 列宽，说明会粘在命令后面；因此把 `:97` 的 `{:<36}` 改为按 `max(len(command)) + 2` 计算列宽，现有示例只是说明列整体右移。

`set` 的 `description`（`cli.py:141-142`）改为 `change an existing account: proxy, sharing and renewal date (use env, args, rename for the rest)`。

示例里的选项必须是真实存在的选项，测试逐条解析（见 §8 T8）。各子命令的示例用与主帮助相同的格式：每行两个空格缩进，命令与说明之间至少两个空格。

### 5.2 接口变更

| 接口 | 类型 | 说明 | 兼容性 |
| ---- | ---- | ---- | ---- |
| `add` / `set` 的 `--renews DATE\|off` | 新增 | 记录或清除续费日 | 新增选项，旧用法不变 |
| `config.json` 账号的 `renews` 键 | 新增 | `YYYY-MM-DD` 字符串，可省略 | 旧配置没有此键，读入为 `None`；旧版本程序读到新配置时忽略未知键（`_parse_account` 只按名取值，`config.py:351-374`） |
| `list` 表格的 `RENEWS` 列 | 新增 | 有账号设置时才出现 | 未设置时输出不变 |
| `list --json` 每账号的 `renews` 键 | 新增 | 对象或 `null` | 新增键，`schema_version` 仍为 1 |
| stderr 的 `renewal date has passed` 提示 | 新增 | 只在有过期日期时出现 | 不影响 stdout 与退出码 |

本方案不涉及 `docs/reference/*`（仓库没有该目录），无需 sibling 回补检查。

## 6. 备选方案与决策

| 方案 | 结论 | 理由 |
| ---- | ---- | ---- |
| 按 `subscription_created_at` 推算到期日 | 否决 | 实测差 3 天；停过订阅的账号分辨不出，错了没有提示 |
| 显示 `subscription_status` | 否决 | 实测与苹果订阅页相反 |
| 新起子命令 `renewals` | 否决 | 用户问过“放 list 还是单起子命令”，结论是放 list；续费日是账号设置，归 `set` |
| 用 `--no-renews` 清除 | 否决 | 与 `--proxy off` 的写法统一，用 `--renews off` |

## 7. 影响分析

**正向**

1. `Account` 多一个属性 → `to_dict` / `dump_config` → `accounts.plan` 判断配置是否变化（`accounts.py:121`）。只有设置过续费日的账号写出新键，其余账号的配置文本不变，所以升级后第一次运行任何写命令不会产生多余的“配置已改”。
2. `_add_account_options` 被 `add`、`set` 共用（`cli.py:143` 与 `:137`）；`add` 也获得 `--renews`，在 `:562-596` 同一分支处理，无需另写。
3. 补全脚本从 parser 读选项（`completion.py:4` 说明），`--renews` 自动出现在 `add` / `set` 的补全里，三种 shell 都是。
4. `list` 表格列由 `_account_entries` 统一提供（`cli.py:1474-1503`），简表、详细表、JSON 口径一致。

**反向**

1. `migrate-default`（`migrate.py:286`）、`restore`（`restore.py:54`、`:148`）用 `config.copy()` 复制配置：`Account.copy` 必须带上 `renews`，否则这两个命令会把续费日清掉。这是 §8 T5 的验证项。
2. `export` / `import`（`bundle.py`）只处理账号目录里的文件与 MCP 设置，不读写 `config.json` 的账号字段，不受影响。
3. `doctor`、`statusline`、`hook`、启动命令内容（`render_launcher`）都不读这个字段，启动 `claude-NAME` 的行为不变。
4. `_help_epilog` 列宽改为动态：主帮助的 `Examples:` 段各行说明列位置改变，命令分组段（`:96` 的 `{:<18}`）不变。现有测试只检查分组标题（`tests/test_help.py:21`），不受影响。
5. `apply -f`：`_load_apply_file`（`cli.py:1266-1271`）只沿用 `identity`、`managed_links`、`dir` 三个内部状态，`renews` 与 `shared_exclude` 一样属于用户配置，以文件为准：文件里没写 `renews` 的账号会被清除。这是声明式配置的既定语义，不改代码，在 README 续费日小节写明。
6. 装回不认识 `renews` 的旧版本后，任何写命令都会按旧版 `to_dict` 重写配置，续费日丢失。降级不在支持范围内，CHANGELOG 中提一句。

**运行时**

- 不联网、不读凭据；每个账号多一次日期计算。
- 输出宽度：简表多一列，最长约 `2027-01-05 (81d)` 16 字符；只在用户设置后出现。
- 时区：用本机时区。用户在美国、续费日按商店页面写的日期填，两者口径相同；跨时区出差时剩余天数可能差一天，可接受。

## 8. 回归测试

单元测试新文件 `tests/test_renewal.py`。CLI 用例沿用 `tests/helpers.py` 的临时 HOME 与子进程调用（`helpers.py:152-155`），子进程里的“今天”无法替换，所以日期按测试运行当天的相对天数构造（如 `today + 25 天`）；单元格的固定日期用例直接在测试进程里调用 `_renews_cell(renews, today)`。

| 编号 | 场景 | 期望 |
| ---- | ---- | ---- |
| T1 | `set work --renews 2026-11-10` | 配置里 `accounts.work.renews` 为该值；退出码 0 |
| T2 | `set work --renews off` | 配置里没有 `renews` 键 |
| T3 | `--renews 2026-02-30`、`--renews 11-10`、`--renews tomorrow` | 退出码 2，stderr 含 `YYYY-MM-DD`，配置不变 |
| T4 | `apply -f` 读入 `renews: "x"` / `renews: 5` | 报 `ConfigError`，退出码 1；合法值被接受 |
| T5 | 设置后执行 `rename`、`set --proxy`、`migrate-default`、`restore` 用到的 `config.copy()` | `renews` 保留 |
| T6 | 没有任何账号设置续费日 | 简表表头仍匹配 `tests/test_help.py:44` 的正则且不含 `RENEWS`；`--verbose` 表头不含 `RENEWS`；stderr 不含 `renewal date`；`--json` 每账号 `renews` 为 `null` |
| T7 | 进程内调用 `_renews_cell`，今天取 2026-10-16，日期分别为 11-10、10-19、10-16、10-10、2027-01-05 | 依次为 `11-10 (25d)`、`10-19 (3d!)`、`10-16 (today!)`、`10-10 (6d ago)`、`2027-01-05 (81d)` |
| T7b | CLI：一个账号设为真实今天 +25 天，另一个为 -6 天；测试在调用前后各取一次今天，两次不同（跨午夜）就重跑一次 | 简表有 `RENEWS` 列，两格分别以 `(25d)`、`(6d ago)` 结尾；stderr 含 `renewal date has passed for` 与过期账号名；`--json` 的 stdout 可被 `json.loads` 解析，`days_left` 分别为 25、-6 |
| T8 | `set --help`、`add --help`、`list --help`、主帮助 | 含 `Examples:`。取 `Examples:` 之后、以两个空格开头的行，按 `re.split(r"\s{2,}", line.strip())` 取第一列，`shlex.split` 后去掉开头的 `multi-claude`，交给 `build_parser().parse_args`：含 `--help` 的示例（主帮助的 `add --help`）断言抛出 `SystemExit(0)`，其余都能解析。覆盖口径：从该子命令 parser 的 `_actions` 取全部 `option_strings`，去掉 `-h`/`--help`，`set` / `add` 再去掉 `--verbose`，余下每个选项都至少在一条示例里出现 |
| T9 | `list --offline` | 仍有 `RENEWS` 列 |
| T10 | `set work --renews 2026-11-10 --dry-run` | 配置不变 |
| T11 | 补全 | bash 下 `multi-claude set x --ren` 补出 `--renews` |
| T13 | `apply -f` 文件里该账号没写 `renews` | 配置里的续费日被清除（与 `shared_exclude` 语义一致） |
| T14 | 主帮助 `Examples:` 段 | 对 `_EXAMPLES` 中说明非空的每一条，帮助输出里有一行包含 `command + "  "` 且以说明结尾（说明为空的条目被 `cli.py:98` 的 `rstrip()` 去掉尾部空格，不检查） |
| T12 | 既有测试全集 | 本机 macOS 全量通过；编译机 Ubuntu 20、Python 3.8 全量通过 |
| L1 | 本机实测 | `multi-claude set jakoes.wu@icloud.com --renews 2026-11-10` 后 `multi-claude list` 该行显示 `11-10 (25d)`（按当天日期）；`--renews off` 后列消失 |

## 9. 日志 / 观测点

- `set` / `add` 写入续费日后，沿用现有收敛输出（配置变更行）；`--dry-run` 打印同样的计划。
- 参数错误：stderr `[multi-claude] error: renewal date must be YYYY-MM-DD, e.g. 2026-11-10`（`actions.py:59-65` 的 `error()` 格式），退出码 2。
- 配置文件错误：`ConfigError` 文本带账号名与 `'renews'` 字段名，退出码 1。
- 续费日已过：stderr `[multi-claude] renewal date has passed for …`。
- 脚本判断：`list --json` 的 `accounts[].renews.days_left`。
