# multi-claude：默认共享目录与 `set` 命令（第三批）

> 2026-10-02 注记：plan-review 2 轮收敛；代码已按方案实现（`cli.py` `_add_account_options` / `set` / `_checked_shared_dir` / `_hint_empty_shared_dir`，`config.DEFAULT_SHARED_DIR`，`Action.quiet`），待独立代码评审与编译机验证，未提交。基线：main `b4b9e65`（第二批已合并）。用户 2026-10-02 的决定：
>
> - B2：默认共享目录为 `~/.claude-shared`；`add NAME --shared DIR` 修改全局共享目录，所有开启共享的账号的链接跟着改指向。
> - C3：新增 `set` 命令。
>
> 三批做完后一起发 v0.3.0。

## 1. 背景

- **开启共享要两步**：`add NAME --shared` 在 `shared.dir` 未设置时判为冲突（`shared.py:31-33`，提示先运行 `multi-claude init --shared-dir DIR`）。新用户要先 `init --shared-dir DIR`，再 `add NAME --shared`。
- **共享目录是空的时没有任何提示**：`plan_shared` 对共享目录里不存在的条目记 SKIP（`shared.py:36-40`）。第一批之后，SKIP 默认仍然会打印，但新用户看不出该往哪个目录放什么。
- **修改设置也要用 `add`**：`add`（`cli.py:436-455`）对已存在的账号是“修改选项”，命令名容易误导。

## 2. 目标 / 非目标

**目标**

1. `--shared` 不带值时：`shared.dir` 已设置就用它，未设置就设为 `~/.claude-shared`；`--shared DIR` 把全局 `shared.dir` 改为 DIR。
2. 开启共享后，共享目录里一个默认条目都没有时，提示该放什么。
3. 新增 `set NAME …`：只修改已登记的账号，选项与 `add` 相同（`--adopt` 除外）。

**非目标**

- 不自动创建共享目录，也不往里面复制任何内容。
- `env`、`args`、`proxy`、`rename`、`remove` 保持独立命令，不并入 `set`。
- `apply -f` 读入的配置里 `shared.dir` 为空时，不自动补默认值（声明式配置以文件为准）。

## 3. 假设与约束

- **`--shared` 改为可选值**：用 argparse 的 `nargs="?"`、`const=True`、`metavar="DIR"`，与 `--no-shared` 用不同的 `dest`，互斥关系保留（`--shared` 和 `--no-shared` 不能同时给）。写成 `add --shared work`（选项在账号名之前）时，`work` 会被当成 DIR，账号名缺失，argparse 报 `the following arguments are required: name`，退出 2。README 的写法一律是 `add NAME --shared`。
- **DIR 的形式**：必须像路径，即含 `/`，或以 `~`、`.` 开头；否则报用法错误，避免把账号名之类的词误当成目录。DIR 原样写入配置（与 `init --shared-dir` 相同，含 `~` 时使用时再展开）。
- **复用现有逻辑**：改 `shared.dir` 后各账号链接的改指向，由现有的 `old_shared_dir` 逻辑完成（`accounts.py:99`、`shared.py:67` 起的 UPDATE 分支），本方案不新增链接逻辑。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `b4b9e65`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/cli.py` | `build_parser` 的 `add` :128-144 | 修改 | `--shared [DIR]`；选项抽成 `_add_account_options(parser, adopt)`，供 `add` 与 `set` 共用 |
| `src/multi_claude/cli.py` | `build_parser`（`add` 之后） | 新增 | `set` 子命令 |
| `src/multi_claude/cli.py` | `dispatch` 的 `add` :436-455 | 修改 | `add` / `set` 共用一段；`set` 遇到未登记账号报错；`--shared` 的目录处理；空共享目录提示 |
| `src/multi_claude/cli.py` | `COMMAND_GROUPS` :40-61 | 修改 | “Account settings”组加 `set` |
| `src/multi_claude/cli.py` | `--adopt` 报错文字 :453 | 不变 | — |
| `src/multi_claude/completion.py` | `POSITIONAL_KINDS` | 修改 | `set` 的第 1 个参数补账号名 |
| `src/multi_claude/config.py` | `DEFAULT_SHARED_ITEMS` :17 之后 | 新增 | `DEFAULT_SHARED_DIR = "~/.claude-shared"` |
| `src/multi_claude/actions.py` | `Action.__init__` | 修改 | 新增 `quiet` 属性（§5.1.4） |
| `tests/test_accounts.py` | `test_toggle_shared` :461-466 | 修改 | 原来在默认输出里断言 `not present in shared dir`，改为默认不出现、`--verbose` 出现（§5.1.4 的直接后果） |
| `src/multi_claude/shared.py` | :39-40 | 修改 | “共享目录里没有该条目”的 SKIP 标为 `quiet` |
| `src/multi_claude/accounts.py` | `execute` | 修改 | 非 `--verbose` 时跳过 `quiet` 动作 |
| `tests/test_set.py` | 新文件 | 新增 | 见 §8 |
| `tests/test_completion.py` | `SUBCOMMANDS` | 修改 | 加 `set` |
| `README.md`、`README.zh-CN.md` | 命令表、快速开始、Shared resources 一节 | 修改 | `set`、默认共享目录、`--shared DIR` |
| `CHANGELOG.md` | Unreleased | 修改 | Added / Changed |

`args.shared` 的读取点全集（`grep -n "args.shared\b\|args\.shared " src/multi_claude/cli.py`，不截断）只有 `cli.py:447-448` 一处；`args.shared_dir` 只属于 `init`（`:425-426`），不受影响。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 选项

`_add_account_options(parser, adopt: bool)` 加入：

- `--proxy`
- 互斥组：`--shared [DIR]`（`dest="shared_to"`，`nargs="?"`，`const=True`）与 `--no-shared`（`dest="no_shared"`，`store_true`）
- `--adopt`：仅 `adopt=True` 时加入
- `--shared-exclude`、`--shared-include`
- 写命令公共选项（`_add_write_options`）

`--shared` 的帮助：`link the shared items into this account; with DIR, also make DIR the shared directory for every account (default ~/.claude-shared)`。

#### 5.1.2 `dispatch` 中的 `add` / `set`

```python
elif args.command in ("add", "set"):
    account = new.find(name)
    if account is None:
        if args.command == "set":
            raise _NotRegistered(name)      # 退出 1，提示里加上 “use add to create it”
        ...                                 # 原 add 新建逻辑
    if args.command == "set" and not _any_account_option(args):
        raise UsageError("nothing to set; give at least one option, e.g. `multi-claude set NAME --proxy 7901`")
    ...                                     # proxy 照旧
    if args.shared_to is not None:
        if args.shared_to is not True:
            new.shared_dir = _checked_shared_dir(args.shared_to)   # 改全局目录
        elif not new.shared_dir:
            new.shared_dir = DEFAULT_SHARED_DIR                     # "~/.claude-shared"
        account.shared = True
    elif args.no_shared:
        account.shared = False
    ...                                     # shared-exclude / adopt 照旧
```

- `_any_account_option`：`proxy`、`shared_to`、`no_shared`、`shared_exclude`、`shared_include` 任一有值。
- `_NotRegistered` 的提示：`set` 时为 `account 'x' is not registered; use multi-claude add x to create it`。
- `--shared DIR` 改了已有的 `shared.dir` 时，收敛完成后打印 `shared directory is now <DIR> (was <旧值>); links of every shared account point there`。

#### 5.1.3 空共享目录提示

收敛成功（退出 0、不是 `--dry-run`）且该账号开启了共享时：若 `expand(shared.dir)` 不存在，或 `shared.items` 中没有任何一项存在于共享目录里，打印：

```text
[multi-claude] note: <共享目录> has none of agents, commands, skills, CLAUDE.md yet; put what every account should share there, then run multi-claude apply
```

条目名取 `config.shared_items`，不写死。

#### 5.1.4 共享目录里缺少的条目不再逐条刷屏

默认共享清单有 4 项，共享目录里缺哪一项，每条命令都会为每个共享账号打印一行 `skip shared-link … (not present in shared dir …)`（`shared.py:39-40`）。`Action` 新增 `quiet` 属性（`actions.py` `Action.__init__`），这类 SKIP 标为安静项；`accounts.execute` 在非 `--verbose` 时与 `unchanged` 一样跳过。其它 SKIP（例如“链接已被用户改掉、不再管理”）照常打印。空共享目录由 §5.1.3 的提示覆盖。

#### 5.1.5 `set` 的位置

- **帮助分组**：`COMMAND_GROUPS` 的 Account settings 组第一行改为 `("set NAME", "change an account: proxy and sharing")`，`proxy, env, args` 一行保留。
- **README**：命令表加 `set`；“Shared resources”一节改为 `multi-claude add work --shared`（默认目录）与 `--shared DIR`；`init --shared-dir` 保留为另一种写法。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `add/set --shared [DIR]` | 由开关变为可带值；不带值且 `shared.dir` 未设置时，不再判冲突，而是设为 `~/.claude-shared` | 原来会冲突的命令现在会成功；`add --shared` 的其它情况不变 |
| `set NAME …` | 新增；未登记账号退出 1，没有选项退出 2 | 新增 |
| `config.json` `shared.dir` | 可能被写成 `~/.claude-shared` | 与 `init --shared-dir` 写入的格式相同 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **`--shared` 保持开关，另加 `--shared-dir DIR`**：用户已选定由 `--shared DIR` 修改全局目录。
- **自动创建共享目录**：工具会替用户新建一个空目录，但链接仍然不会出现（没有条目可链）。只提示更直接，也符合“不写用户没要求的东西”，因此不采用。

## 7. 影响分析

**正向**

- 只改 `add` 的参数解析与 `dispatch` 的 `add` 分支；收敛计划、链接逻辑都不变。
- 之前因 `shared.dir` 未设置而冲突的 `add --shared` 现在会成功，并写入默认目录。

**反向**

- **测试**：`grep -rn "shared.dir is not configured" tests src`（不截断）只命中 `shared.py:33` 的冲突文字本身，现有用例里没有断言这一冲突的，不需要改测试；该冲突路径仍保留给手工改过、`shared.dir` 为空的配置（`apply`）。
- **`--shared DIR` 改全局目录**：所有开启共享的账号的受管链接会改指向新目录（现有 UPDATE 逻辑）；新目录里缺少的条目按 SKIP 处理，旧链接按现有规则删除或保留。输出里会列出这些动作。
- **`apply -f`**：不受影响（非目标）。
- **跳过提示变安静**：§5.1.4 让 `test_accounts.py:466` 原有的断言失效，改为分别断言默认输出与 `--verbose` 输出。
- **补全与帮助分组**：测试强制分组覆盖全部子命令，加 `set` 后必须同步。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 无 `shared.dir` 时 `add work --shared` | 退出 0；配置 `shared.dir` 为 `~/.claude-shared`；`~/.claude-shared` 存在且有 skills 时建立链接 |
| T2 | 已设 `shared.dir` 时 `add work --shared` | 沿用原目录，不改配置里的 `shared.dir` |
| T3 | `add work --shared ~/s2`（已有两个共享账号指向旧目录） | `shared.dir` 改为 `~/s2`；两个账号的受管链接都指向新目录；输出含 `shared directory is now` |
| T4 | `--shared work-dir`（不像路径）；`--shared --no-shared` | 退出 2，配置不变 |
| T5 | 共享目录不存在或为空 | 输出含 `has none of agents, commands, skills, CLAUDE.md`；有条目时不输出 |
| T6 | `set work --proxy 7901`、`set work --no-shared`、`set work --shared-exclude skills` | 与对应的 `add` 写法结果相同 |
| T7 | `set nobody --proxy 7901`；`set work`（无选项）；`set work --adopt` | 依次退出 1（提示 `use multi-claude add`）、2、2；配置与文件系统不变 |
| T8 | 帮助与补全 | `--help` 的 Account settings 组含 `set`；三种 shell 补全含 `set`，bash 下 `set ` 补账号名 |
| T9 | 回归 | 现有全部用例通过（基线 280 个） |
| T10 | 共享目录缺少条目时的输出 | 默认不出现 `not present in shared dir`；`--verbose` 时出现 |

## 9. 日志 / 观测点

- **改全局目录**：`shared directory is now <DIR> (was <旧值>); links of every shared account point there`。
- **空目录提示**：`note: <目录> has none of …`。
- **`set` 的报错**：`account 'x' is not registered; use multi-claude add x to create it`、`nothing to set; …`。
