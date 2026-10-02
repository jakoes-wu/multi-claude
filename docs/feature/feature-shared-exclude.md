# multi-claude：共享项按账号退出与禁止共享清单

> 2026-10-01 注记：已合并（PR #9），随 v0.2.0 发布。来源：`docs/analysis/roadmap.md` §2.3。基线：main `a397796`。用户决定（2026-10-01）：
>
> - 配置写法：`add NAME --shared-exclude ITEM`（可重复），`--shared-include ITEM` 撤销；
> - 退出一项时：只删本工具建的软链，与关闭共享一致，不复制内容；
> - 禁止把不该共享的条目放进 `shared.items`。

## 1. 背景

- `shared.items` 是全局清单（默认 `agents`、`commands`、`skills`、`CLAUDE.md`，`src/multi_claude/config.py:17`）。每个账号只有 `shared` 一个开关，开启后清单里的每一项都会软链到共享目录（`src/multi_claude/shared.py:35`），不能让某个账号单独不共享其中一项。
- `shared.items` 目前只校验“是一级名称”（`config.py:323` `_valid_item`），凭据文件、`.claude.json` 这类账号私有状态也能被配置成共享项。

## 2. 目标 / 非目标

**目标**

1. 账号新增配置 `shared_exclude`（名称列表）。共享开启时跳过其中各项；本工具建过的这些项的软链按“该项关闭共享”删除。
2. 命令：`add NAME --shared-exclude ITEM` 与 `add NAME --shared-include ITEM`，两者都可重复；`list` 与 `list --json` 显示退出项。
3. 禁止共享清单：`shared.items` 里出现清单中的名称时，读取配置报错；`init --shared-items` 带这些名称时报用法错误。

**非目标**

- 退出时把共享内容复制成账号自己的目录。
- 按账号追加共享项（只能从全局清单里退出，不能多加）。
- 改动 `doctor` 的共享链接检查。

## 3. 假设与约束

- **禁止共享清单**按大小写不敏感比较（macOS 默认文件系统不区分大小写），内容如下：
  - `.credentials.json`：Linux 上的凭据文件，见 `identity.credentials_file`；
  - `.claude.json`：账号全局状态；
  - `settings.local.json`；
  - `projects`、`history.jsonl`、`file-history`：会话与历史；
  - `sessions`、`session-env`、`shell-snapshots`、`todos`：运行时状态。

  前 5 项是用户确认的，后 5 项同属会话与运行时状态，与 `projects` 同类。
- **`shared_exclude` 是用户配置**，不是工具内部状态：`apply -f` 从文件读取，不沿用当前值（与 `managed_links` 不同，见 `cli.py` `_load_apply_file`）。
- **`to_dict` 只在列表非空时写出 `shared_exclude`**：没有退出项的账号，配置内容与现在逐字相同（同 `dir` 字段的做法，`config.py:92`）。
- 退出项允许写不在 `shared.items` 里的名称（以后加进清单时立即生效），`add` 时给出提示。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `a397796`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/config.py` | `DEFAULT_SHARED_ITEMS` :17 之后 | 新增 | `UNSHAREABLE_ITEMS` 与 `is_unshareable(item)` |
| `src/multi_claude/config.py` | `Account.__init__` :70-83、`copy` :84-86、`to_dict` :88-94 | 修改 | 新增 `shared_exclude` 字段 |
| `src/multi_claude/config.py` | `parse_config` :219-221 | 修改 | `shared.items` 含禁止项时 `ConfigError` |
| `src/multi_claude/config.py` | `_parse_account` :337-352 | 修改 | 解析 `shared_exclude`（名称校验同 `managed_links`） |
| `src/multi_claude/shared.py` | `plan_shared` :35 | 修改 | 遍历 `shared_items` 时跳过 `account.shared_exclude` 中的项 |
| `src/multi_claude/cli.py` | `build_parser` 的 `add` :65-73 | 新增 | `--shared-exclude ITEM`、`--shared-include ITEM`（`action="append"`） |
| `src/multi_claude/cli.py` | `dispatch` 的 `init` :311-315 | 修改 | `--shared-items` 含禁止项时 `UsageError` |
| `src/multi_claude/cli.py` | `dispatch` 的 `add` :327-334 | 修改 | 应用排除 / 撤销 |
| `src/multi_claude/cli.py` | `_account_entries` :690、`cmd_list` 表格 :654 | 修改 | JSON 新增 `shared_exclude`；表格 SHARED 列显示退出项 |
| `tests/test_shared_exclude.py` | 新文件 | 新增 | 见 §8 |
| `README.md`、`README.zh-CN.md` | 命令表 `add` 行、“Shared resources / 共享资源”一节（:299） | 修改 | 文档同步 |
| `CHANGELOG.md` | Unreleased | 修改 | Added 与 Changed 各一条 |

`shared_items` 的读取点全集（`grep -rn "shared_items" src/`，不截断）：

- `config.py:105`、`:148`、`:157`、`:219-221`、`:253`：构造、复制、写出、解析；
- `shared.py:35`：建链接，本方案修改；
- `cli.py:315`：`init` 写入，本方案加校验；
- `doctor.py:166`：只检查断开的软链，不改（非目标）。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 配置

```json
"accounts": {
  "work": {"shared": true, "shared_exclude": ["skills"], "...": "..."}
}
```

`shared_exclude` 每项须满足 `_valid_item`，否则报 `'shared_exclude' must be a list of names`；重复项去重并保持首次出现的顺序。

#### 5.1.2 建链接（`shared.plan_shared`）

`desired` 只收 `item in new.shared_items and item not in account.shared_exclude` 的项（`shared.py:35` 循环处加一个跳过）。

已在 `managed_links` 中、但被排除的项走现有的“不在 `desired` 中”分支（`shared.py` 删除循环）：

- 仍指向共享目录的受管软链被删除，原因文字沿用 `sharing disabled for this item`；
- 已被用户换成别的东西的，记 SKIP，不动。

所以“退出只删本工具建的软链”不需要新逻辑。

#### 5.1.3 命令

```text
multi-claude add NAME --shared-exclude ITEM [--shared-exclude ITEM ...] [--shared-include ITEM ...]
```

- **应用顺序**：先移除 `--shared-include` 的项，再追加 `--shared-exclude` 的项（不重复追加）。
- **用法错误（退出 2）**：同一名称同时出现在两个选项里；名称不满足 `_valid_item`。
- **提示**：排除的名称不在 `shared.items` 里时打印 `note: ITEM is not in shared.items; it takes effect only if added there`；账号未开启共享时打印 `note: sharing is off for NAME; the exclusion applies once it is turned on`。
- **不需要共享开启**：只改配置，其余照常走现有 `add` 的收敛流程（共享开启时立即删 / 不建对应链接）。

#### 5.1.4 禁止共享清单

```python
UNSHAREABLE_ITEMS = (".credentials.json", ".claude.json", "settings.local.json", "projects", "history.jsonl",
                     "file-history", "sessions", "session-env", "shell-snapshots", "todos")
```

- **`parse_config`**：任何 `shared.items` 项满足 `is_unshareable` 时，报 `ConfigError("{source}: 'shared.items' must not include {item}: it holds account-specific state")`。该错误会走现有的配置错误通道，`doctor` 的 config 检查也会报出。
- **`init --shared-items`**：同一判定，报 `UsageError`（退出 2）。
- **`shared_exclude`**：不做此限制，排除禁止项无害。

#### 5.1.5 输出

- **`list --json`**：每个账号新增 `"shared_exclude": [...]`。
- **`list` 表格**：SHARED 列在共享开启且有退出项时显示 `yes (not: skills,commands)`，其余情况仍为 `yes` / `no`。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `config.json` 账号字段 `shared_exclude` | 新增，可省略 | 旧配置不受影响。v0.1.0 的 `_parse_account` 只取已知键，读到它会忽略；但旧版本一旦写配置就会丢掉它（`to_dict` 只写已知字段），降级后需重新设置 |
| `config.json` `shared.items` | 收紧：禁止项报错 | 含禁止项的旧配置升级后无法加载，需要手工删掉该项；记入 CHANGELOG Changed |
| `add --shared-exclude / --shared-include` | 新增 | 新增 |
| `init --shared-items` | 含禁止项时退出 2 | 收紧 |
| `list --json` 每账号 `shared_exclude` | 新增字段 | 只加字段 |
| `list` 表格 SHARED 列 | 可能出现 `yes (not: …)` | 面向人阅读 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **新子命令 `share`**：与 `add` 已有的 `--shared`、`--no-shared`、`--adopt` 职责重叠，用户选定放在 `add` 上。
- **退出时复制共享内容**：会产生两份需要各自维护的内容，用户选定只删链接。

## 7. 影响分析

**正向**

- `plan_shared` 只多一个跳过条件；没有退出项的账号，计划结果与现在逐项相同。
- 删除路径完全复用现有逻辑，没有新增文件操作。

**反向**

- **`managed_links` 的顺序与内容**：被排除的项不再进入 `kept`，`managed_links` 随之移除这些项（现有逻辑，`shared.py` 末尾）。之后 `--shared-include` 撤销时按 CREATE 重建链接，前提是账号里该位置为空：期间用户若在那里建了真实目录，会按现有规则判冲突，不会覆盖。
- **`apply -f`**：文件里没写 `shared_exclude` 的账号会清空它的退出项，这是声明式配置的预期语义，README 写明。
- **禁止清单收紧**：本机配置 `shared.items` 为 `agents, commands, skills, CLAUDE.md`，不受影响。其它用户若配置了禁止项，升级后 `list` 等命令报配置错误并指出该项，按提示删掉即可。
- **`doctor`**：只检查断开的软链。退出项的受管软链已被删除，不会产生新告警。

**运行时**：无新进程、无新 IO 路径。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 共享账号 `add a --shared-exclude skills` | `a/skills` 受管软链被删除；其它共享项不变；`managed_links` 不含 `skills`；共享目录内容不变；配置写出 `shared_exclude: ["skills"]` |
| T2 | 已退出的项在账号里有用户自建的真实目录或指向别处的软链 | 原样保留，无冲突 |
| T3 | `--shared-include skills` 撤销 | 重新建立软链，`managed_links` 恢复；`shared_exclude` 为空时配置不写该键 |
| T4 | 共享关闭的账号加 `--shared-exclude`，再 `--shared` | 第一步只改配置并有提示；第二步不建 `skills` 链接 |
| T5 | 同名同时 include 与 exclude；名称带 `/` | 退出 2，配置与文件系统不变 |
| T6 | 排除不在 `shared.items` 中的名称 | 有提示，退出 0 |
| T7 | `config.json` 的 `shared.items` 含 `.credentials.json`（以及大小写变体 `Projects`） | `list` 退出 1 并指出该项；`doctor` 的 config 检查报错 |
| T8 | `init --shared-items skills,history.jsonl` | 退出 2，配置不变 |
| T9 | `list` 与 `list --json` | 表格 SHARED 列为 `yes (not: skills)`；JSON 含 `shared_exclude` |
| T10 | `apply -f` 写了 / 没写 `shared_exclude` | 分别按文件生效 |
| T11 | `--dry-run` | 输出含 `shared-link` 删除动作与 `sharing disabled for this item`，文件系统与配置不变 |
| T12 | 回归 | 现有全部用例通过 |

## 9. 日志 / 观测点

- **收敛动作**：沿用现有输出 `[multi-claude] <状态> shared-link <路径> (<原因>)`（`actions.print_action`），删除时原因为 `sharing disabled for this item`，`--dry-run` 时状态后带 `(dry-run)`。
- **提示**：`note: …`（§5.1.3）。
- **配置错误**：`'shared.items' must not include <item>: it holds account-specific state`。
