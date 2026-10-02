# multi-claude：三项使用中发现的小改进

> 2026-10-02 注记：代码已按方案实现（`cli.py` `_misplaced_account_name` / `_looks_like_path`、`cmd_statusline` 提示，`roadmap.md` §2.4，README 双语），待独立代码评审与编译机验证，未提交。基线：main `5c0086d`（v0.3.0）。用户 2026-10-02 要求先做这三项。

## 1. 背景

1. **路线文档没有更新**：`docs/analysis/roadmap.md` 只记录到 v0.2.x（§2.1–§2.3），没有 v0.3.0 发布的三批上手改进（PR #15–#17）。另外，它的分组标题“v0.2 / v0.3 / v0.4”指的是路线分组，与实际版本号 v0.2.0 / v0.3.0 容易混淆。
2. **包装状态栏后要新开会话**：2026-10-01 实机观察到，包装前就在运行的会话持续多轮都没有生成快照，新开的会话收到第一次回复后才生成（推断：运行中的会话不会重新读取 `--settings` 文件，未读源码核实）。目前 README（`README.md:165-172` 一带）和 `statusline install` 的输出（`cli.py:1229-1236`）都没说明这一点。
3. **`add --shared work` 的报错让人看不懂**：选项写在账号名前面时，argparse 把 `work` 当成 `--shared` 的 DIR，然后报 `the following arguments are required: name`（第三批评审实测），看不出问题出在顺序上。

## 2. 目标 / 非目标

**目标**

1. 路线文档补上 v0.3.0 的内容，并说明分组名与版本号的区别。
2. README 与 `statusline install` 的输出都提示：要新开会话、且收到一次回复后才开始记录。
3. `add/set --shared <非路径的词>` 且缺少账号名时，直接提示把账号名放到前面。

**非目标**

- 不改状态栏钩子本身，也不尝试让运行中的会话重新加载设置。
- 不改 `--shared` 的取值规则（仍是“像路径才算 DIR”）。

## 3. 假设与约束

- 第 3 项的判定在 argparse 之前做，与 `_unknown_command`（`cli.py:313`）同一位置，只读 `argv`，不改变任何合法写法的解析结果。
- “像路径”的规则复用 `_checked_shared_dir` 的判定：含 `/`，或以 `~`、`.` 开头。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `5c0086d`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `docs/analysis/roadmap.md` | §2 标题 :23、§2.3 之后 | 修改 / 新增 | 说明分组与版本号的关系；新增 §2.4（v0.3.0 发布的上手改进） |
| `src/multi_claude/cli.py` | `cmd_statusline` :1229-1236 | 修改 | 包装成功（`wrapped` / `rewrapped`）且不是 `--dry-run` 时追加一行提示 |
| `src/multi_claude/cli.py` | `main` :313-316 之后 | 新增 | `_misplaced_account_name(argv)`：识别 `add/set --shared <词>` 且缺账号名 |
| `src/multi_claude/cli.py` | `_checked_shared_dir` 附近 | 修改 | 抽出 `_looks_like_path(value)` 供两处共用 |
| `README.md`、`README.zh-CN.md` | Usage 一节的 statusline 列表（:165-172 一带） | 修改 | 加一条“新开会话、收到一次回复后才开始记录” |
| `tests/test_usability.py` | 新文件 | 新增 | 见 §8 |
| `CHANGELOG.md` | Unreleased | 修改 | Changed 条目 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 路线文档

- §2 开头加一句：分组名“v0.2 / v0.3 / v0.4”是规划时的批次，全部随 v0.2.0、v0.2.1 发布；实际版本号见各行的“发布”列。
- 新增 §2.4：

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| 上手说明、拼错建议、`login`、下一步提示、只打印变化 | 第一批 | `feature-easier-onboarding.md` | v0.3.0（PR #15） |
| 帮助分组、`list` 简表、PATH 命令、README 重排 | 第二批 | `feature-clearer-help.md` | v0.3.0（PR #16） |
| `set`、默认共享目录、空目录提示 | 第三批 | `feature-set-command.md` | v0.3.0（PR #17） |
| 本方案三项 | — | `feature-usability-fixes.md` | 待发布 |

#### 5.1.2 状态栏提示

`cmd_statusline` 在 `result.state` 为 `wrapped` 或 `rewrapped`、且不是 `--dry-run` 时，打印：

```text
[multi-claude] note: Claude sessions that are already running keep the old status line; usage is recorded from new sessions after their first reply
```

README（中英）在“Only a file with … can be wrapped”那组列表里加一条同义说明。

#### 5.1.3 账号名放错位置

`_misplaced_account_name(argv) -> Optional[str]`：

1. 第一个非选项词是 `add` 或 `set`。
2. 其后的参数里有 `--shared`，下一个词存在、不以 `-` 开头、且不像路径。
3. 除这个词以外，没有别的位置参数；且参数里没有 `--version`、`-h`、`--help`、`--no-shared`（前三者让 argparse 直接输出后退出 0，后者会触发更准确的互斥报错）。计算时跳过 `--proxy`、`--shared-exclude`、`--shared-include` 的取值；`--shared=…` 形式不在此列。

满足时返回 `put the account name before the options, e.g. multi-claude add work --shared`（命令名按实际），`main` 报错并退出 2；否则返回 `None`，交给 argparse。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `statusline install` 输出 | 新增一行提示 | 人读 |
| `add/set --shared <词>` 缺账号名时的报错 | 文字改变，退出码仍为 2 | 兼容 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

无显著备选。

## 7. 影响分析

- **第 3 项**只在 argparse 本来就会报错（缺账号名）的输入上生效：判定要求“除该词外没有别的位置参数”，而缺少位置参数时 argparse 一定报错。所以合法写法的结果不变。
- **第 2 项**只多一行输出，不改包装逻辑；`unchanged`、`restored`、`--dry-run` 不打印。
- **第 1 项**只改文档。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | `statusline install` 新包装、换路径重新包装、重复执行、`--dry-run`、`uninstall` | 前两种输出含 `keep the old status line`；后三种不含 |
| T2 | `add --shared work`；`set --shared work`；`add --proxy 7901 --shared work` | 退出 2，报错含 `put the account name before the options`，命令名与输入一致 |
| T3 | `add work --shared`、`add --shared ~/s work`、`add work --shared ~/s`、`add work --shared work-dir` | 前三个照常成功；最后一个仍是原来的 `expects a directory path` 报错 |
| T4 | 回归 | 现有全部用例通过（基线 289 个） |
| T5 | 文档 | `roadmap.md` 含 §2.4 与版本号说明；README 两版含新开会话的说明 |

## 9. 日志 / 观测点

- `note: Claude sessions that are already running keep the old status line; …`
- `error: put the account name before the options, e.g. …`
