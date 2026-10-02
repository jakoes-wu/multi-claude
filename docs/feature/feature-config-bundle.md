# multi-claude：不含凭据的账号配置导出 / 导入，与共享 MCP、插件的写法（v0.8.0）

> 2026-10-03 注记：代码已按方案实现（`bundle.py`；`cli.py` export/import；补全；`tests/test_bundle.py` 19 例），方案评审 H1、M1–M5、L1–L6 已处理。基线：main `d2b6a54`（v0.7.0）。来源：用户 2026-10-03 同意的迭代计划 v0.8.0，以及同日确认的三项决定（见 §3）。

## 1. 背景

把一个账号的设置、`CLAUDE.md`、skills、agents、commands 搬到另一个账号或另一台机器，目前只能手工复制，容易把凭据、会话、`.claude.json` 一起带走。`add --config-from`（v0.5.0）只复制 `settings.json`，而且只在同一台机器上。

### 1.1 已核实的事实

| 事实 | 依据 |
| ---- | ---- |
| 账号目录里与配置有关的条目：`settings.json`、`CLAUDE.md`、`agents/`、`commands/`、`skills/`；还有大量运行时状态（`projects`、`history.jsonl`、`sessions`、`file-history`、`cache`、`plugins/marketplaces` 等） | 2026-10-03 本机 4 个账号目录的条目名（只列名字） |
| 用户级 MCP 服务器存在 `~/.claude.json`（dir 身份账号是 `<账号目录>/.claude.json`）；该文件还存着登录会话信息、项目状态等 | 官方文档 Settings 页：“`~/.claude.json` … holds your sign-in session, MCP server configurations, per-project state” |
| `plugins/known_marketplaces.json` 等文件含源账号目录下的绝对路径；插件选择记在 `settings.json` 的 `enabledPlugins`、`extraKnownMarketplaces` | 本机文件结构；官方 Settings 页 |
| `--settings` 指定的文件与各级 settings 合并，同名键以它为准、其余键保留 | 官方 Settings 页 “Command line arguments” |
| `--mcp-config` 从 JSON 文件加载 MCP 服务器，可接多个值（空格分隔） | 官方 CLI reference |
| `CLAUDE_CODE_PLUGIN_CACHE_DIR` 覆盖插件根目录（市场与插件缓存都在其下），默认 `~/.claude/plugins` | 官方 Environment variables 页 |
| 本机 4 个账号的 `CLAUDE.md`、`agents`、`commands`、`skills` 都是共享软链，按本方案只会导出 `settings.json` 与 MCP 配置 | 本机条目类型（评审时只看软链与权限位） |

未核实：VS Code 扩展的同类代码会优先使用 `<配置目录>/.config.json`（旧版文件）或带后缀的 `.claude<后缀>.json`；本工具只处理 `.claude.json`（与 `usage.global_state_path` 一致）。硬链接与普通文件无法区分：用户自己硬链进 `skills/` 的文件会被导出。

## 2. 目标 / 非目标

**目标**

1. `export NAME FILE`：把账号的配置打成一个 `.tar.gz`，**不含凭据与运行时状态**。
2. `import FILE NAME [--force] [--dry-run]`：把配置包导入一个已登记的账号；默认遇到不同内容的同名条目就拒绝，`--force` 时先备份再覆盖。
3. README 增加“所有账号共用 MCP、插件、设置”的写法（只用现有功能与官方选项，不新增代码）。

**非目标**

- 不导出、不导入凭据（`.credentials.json`、钥匙串）、`.claude.json` 的其它内容、会话与历史、`plugins/` 下的缓存与 JSON。
- 不加密配置包；不上传；不提供跨机器传输。
- `import` 不创建账号（先用 `add`）；不合并 `settings.json` 的键（整文件比较）。

## 3. 假设与约束（用户 2026-10-03 的决定）

1. MCP 服务器：只从 `.claude.json` 抽出 `mcpServers` 一项导出；导入时只按服务器名合并这一项，其它键原样保留。
2. 敏感值：原样导出并警告。配置包以 0600 写出；导出后列出看起来像密钥的**键名**（`settings.json` 的 `env`、各 MCP 服务器的 `env` 与 `headers` 中，键名含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD`、`AUTH`，不区分大小写），不显示值。
3. 冲突：默认退出 3、不改任何东西；`--force` 时把被覆盖的条目改名为 `<条目>.multi-claude-bak.<时间戳>` 再写入。

另外：

- 账号目录中的条目是软链（共享条目或用户自建的链接）时：导出时跳过并说明 `skip <条目> (link)`；导入时目标是软链则一律退出 3（即使 `--force`），提示先用 `--shared-exclude` 取消共享。
- 条目内部的软链（例如 `skills/` 里指向别处的链接）不导出，计数提示。
- 修改 `.claude.json` 时 Claude Code 可能正在写它，运行中的会话也可能在下次保存时用自己的副本覆盖导入的 MCP 配置：写入前在 stderr 提示重启该账号的会话；写入用原子替换并保持原文件权限（新建为 0600）；`.claude.json` 是软链时写到链接目标；只有替换已有服务器时才先把原文件复制为 `.claude.json.multi-claude-bak.<时间戳>`。
- 目标 `.claude.json` 存在但读不出、不是 JSON 或顶层不是对象：退出 1，什么都不改（包括白名单条目）。顶层 `mcpServers` 存在但不是对象：退出 3（`--force` 也不处理）。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `d2b6a54`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/bundle.py` | 新文件 | 新增 | §5.1.1、§5.1.2 |
| `cli.py` `COMMAND_GROUPS` | :51 之后 | 修改 | “Account settings” 增加 `export, import` |
| `cli.py` `build_parser` | `p_restore` 之后（:236 之后） | 新增 | `export NAME FILE`、`import FILE NAME [--force] [--dry-run]` |
| `cli.py` `dispatch` | :474 之前 | 新增 | `export` 与 `import` 的分派（在写锁内；迁移未完成时两者都由 `_blocked_by_migration` 拒绝，退出 1） |
| `cli.py` `_blocked_by_migration` | :467 | 修改 | 用 `getattr(args, "dry_run", False)`：`export` 没有 `--dry-run`，直接读属性会抛 AttributeError |
| `completion.py` `POSITIONAL_KINDS` | :22 | 修改 | `export: ["name", "file"]`、`import: ["file", "name"]` |
| `README.md`、`README.zh-CN.md` | “Shared resources”（:157）之后 | 新增 | “Exporting and importing settings”、“Sharing MCP servers, plugins and settings”；命令表 |
| `CHANGELOG.md`、`docs/analysis/roadmap.md` | Unreleased；§2.7 之后 | 修改 / 新增 | Added；§2.8 |
| `tests/test_bundle.py` | 新文件 | 新增 | §8 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 导出（`bundle.export_account`）

- 条目白名单 `ITEMS = ("settings.json", "CLAUDE.md", "agents", "commands", "skills", "output-styles")`；只有出现在账号目录里、且不是软链的才导出；软链的条目输出 `skip <条目> (shared link)`。
- `mcpServers`：读 `.claude.json`（default 身份为 `~/.claude.json`，`usage.global_state_path`，`usage.py:53-61`），取顶层 `mcpServers`（必须是对象，否则视为没有），写成包内 `mcp-servers.json`。读失败按没有处理并告警。
- 包内布局：`manifest.json`（`{"format": "multi-claude-config", "version": 1, "created": ISO, "source_account": NAME, "items": [...], "mcp_servers": [服务器名...], "multi_claude": 版本}`）、`items/<条目>`、`mcp-servers.json`（可选）。
- 遍历目录条目时不跟随软链；目录内的软链、socket 等非普通文件跳过并计数。
- 写出：先写同目录临时文件（0600），完成后 `os.replace` 到 FILE。FILE 已存在（`os.path.lexists` 检查原始路径，不先 resolve）时退出 3，不覆盖；父目录不存在退出 1。
- 输出：每个条目一行 `export <条目>`，最后 `wrote FILE`；有疑似密钥时 `warn("FILE contains values under keys that look like secrets: …; keep it private")`。

#### 5.1.2 导入（`bundle.import_account`）

- 读包：用 `tarfile` 打开；校验 `manifest.json` 的 `format`、`version`；每个成员必须是普通文件或目录，路径必须在 `items/<白名单条目>/…`、`manifest.json`、`mcp-servers.json` 之内，按分量拒绝空分量、`.`、`..`、绝对路径、反斜杠、NUL，拒绝软链、硬链、设备文件、重复路径；`settings.json`、`CLAUDE.md` 必须是单个文件，其余条目必须是目录；成员数不超过 20000、解压总量不超过 100 MB。任一不满足退出 1、不改任何东西。
- 不用 `extract`/`extractall`（编译机 Python 3.8.10 没有 `filter="data"`）：逐个成员用 `extractfile` 读出自己写，文件权限统一为 0600（带执行位的为 0700），目录 0700，不保留属主、setuid 等位。
- 先清掉同一账号目录下上次残留的 `.multi-claude-import.*`（调用方持有写锁，不会有并发导入），再解到新的临时目录 `.multi-claude-import.<随机>`（0700）；临时目录在 `try/finally` 中删除（含 `--dry-run`、冲突与异常路径）。再逐条比较：
  - 目标不存在 → 将写入；
  - 目标是软链 → 冲突（不可 `--force`）；
  - 目标内容相同（文件按字节；目录按“相对路径 + 类型 + 内容”的清单）→ 不变；
  - 其它 → 冲突；`--force` 时备份后替换。
- `mcpServers`：按服务器名合并到目标 `.claude.json`：名字不存在 → 添加；值相同 → 不变；值不同 → 冲突（`--force` 时替换）。有任何改动时，先把 `.claude.json` 复制为 `.claude.json.multi-claude-bak.<时间戳>`（仅在 `--force` 替换时）或直接原子写入（只新增时）。目标 `.claude.json` 不存在时新建只含 `mcpServers` 的文件（0600）。
- 有冲突且没有 `--force`：列出全部冲突，退出 3，删除临时目录，不改任何东西。
- 执行顺序：所有检查通过后逐条处理：需要替换的先 `os.rename` 为备份，再 `os.replace` 落位（目录不能直接替换非空目录，所以必须先改名）；`.claude.json` 最后写。中途被打断时重跑同一命令：已落位的条目为 unchanged；改名后尚未落位的条目缺失，显示 create，备份保留；没处理到的照常比较。
- `--dry-run`：只输出将要做的动作。
- 目标账号未登记退出 1（提示先 `multi-claude add NAME`）；账号目录不存在退出 1。
- 迁移未完成时拒绝（与其它写命令相同，经 `_blocked_by_migration`）。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `export NAME FILE` | 新增；退出码 0、1、2、3 | 新增 |
| `import FILE NAME [--force] [--dry-run]` | 新增；退出码 0、1、2、3 | 新增 |
| 配置包格式 | `manifest.json` 的 `format: multi-claude-config`、`version: 1` | 新增；以后不兼容的改动提升 `version`，旧版本读到不认识的版本退出 1 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **导出整个账号目录再排除**：排除名单会随 Claude Code 版本变化而漏掉新的状态文件；白名单更安全。
- **合并 `settings.json` 的键**：冲突语义复杂，且与 `--settings` 的合并规则重复；整文件比较更可预期，需要共用时用 README 的 `--settings` 写法。
- **导出 `plugins/` 文件**：其中有源账号目录的绝对路径，搬到别处会指错；插件选择已在 `settings.json` 里。

## 7. 影响分析

- 新命令；其它命令不变。`import` 只写目标账号目录中白名单条目、`.claude.json` 的 `mcpServers` 与备份文件；不改 `config.json`、启动命令与共享软链。
- 隐私：配置包可能含 `settings.json` 与 MCP 配置里的密钥（用户决定原样导出），以 0600 写出并告警；绝不含凭据。
- 运行中的 Claude 会话：导入 `.claude.json` 时可能与 Claude 的写入冲突（后写者覆盖）；README 提示先关闭该账号的会话。
- 资源：skills 等目录可能较大，打包与比较都是线性读取，不常驻内存（tarfile 流式）。

## 8. 回归测试

**环境**：本机（Python 3.11）与编译机 ubuntu20（Python 3.8.10）全量 `unittest`；本地 `pyflakes src tests` 与 tests 路径检查。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 导出：账号有 settings.json、CLAUDE.md、skills（含子目录与一个内部软链）、`.credentials.json`、`projects/`、`.claude.json`（含 mcpServers 与 oauthAccount） | 包内只有白名单条目与 `mcp-servers.json`；没有凭据、会话、`oauthAccount`；内部软链被跳过并提示；包权限 0600 |
| T2 | 导出：共享软链的条目；输出文件已存在（含断开的软链）；账号未登记 | 跳过并提示；退出 3 且不写；退出 1 |
| T3 | 疑似密钥告警 | `env` 里的 `API_KEY`、MCP `headers` 的 `Authorization` 被列出键名，输出不含其值 |
| T4 | 导入到空账号 | 条目与 `mcpServers` 写入，内容与源相同；`.claude.json` 其它键保留 |
| T5 | 重复导入 | 全部 unchanged，退出 0 |
| T6 | 冲突：已有不同的 settings.json、同名不同的 MCP 服务器 | 退出 3，列出冲突，什么都没改；`--force` 后备份文件存在、内容被替换 |
| T7 | 目标条目是软链（共享） | 即使 `--force` 也退出 3 |
| T8 | 恶意包：`../x`、绝对路径、软链成员、不在白名单的路径、缺 manifest、版本不认识 | 退出 1，目标账号目录不变，无临时目录残留 |
| T9 | `--dry-run` | 只输出计划，目录不变 |
| T10 | 目标未登记、迁移未完成 | 退出 1、1 |
| T11 | default 身份账号的导出与导入 | `mcpServers` 读写的是 `~/.claude.json` |
| T12 | 帮助与补全 | `--help` 含 `export, import`；补全含两个命令 |
| T14 | 目标 `.claude.json` 不是合法 JSON；`mcpServers` 不是对象 | 退出 1 且什么都没改；退出 3（含 `--force`） |
| T15 | 只新增 MCP 服务器；`.claude.json` 是软链 | 权限保持 0600、没有备份、stderr 有重启会话的提示；写到链接目标，软链保留 |
| T16 | 包的形状：重复成员、`settings.json` 是目录、`skills` 是文件；成员权限 4777 / 666 | 退出 1；导入后为 0700 / 0600 |
| T17 | 残留临时目录；`--force --dry-run`；目标账号目录不存在 | 残留被清理、目录不变；输出 replace 计划且未改动；退出 1 |
| T18 | 输出路径：已存在的普通文件、已存在的目录、`link/../out.tgz`（`link` 指向另一目录的子目录）、缺参数 | 退出 3、3；写在内核解析出的真实位置；退出 2 |
| T13 | 回归 | 现有 354 个用例全部通过 |

## 9. 日志 / 观测点

- `[multi-claude] export settings.json` … `wrote FILE`；`skip skills (shared link)`；`skipped N link(s) inside skills`
- `[multi-claude] import settings.json (unchanged|create|replace, backup …)`；`mcp server github: add`
- `warning: FILE contains values under keys that look like secrets: env.API_KEY, mcpServers.github.headers.Authorization; keep it private`
- `error: … conflicts; nothing was changed (use --force to back up and replace)`
