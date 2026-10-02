# multi-claude：账号改名与按账号管理 MCP

> 2026-10-01 注记：已合并（PR #6），随 v0.2.0 发布。来源：`docs/analysis/roadmap.md` §2.2。基线：main `193cef8`。用户决定：改名采用“账号名与目录解耦”，接受账号名与目录名不同；MCP 采用“代理 `claude mcp` 命令”，每个账号各管各的，不做跨账号共享。

## 1. 背景

- **改名**：账号名同时决定启动命令名 `claude-<名称>` 与目录 `<root>/<名称>`。macOS 上 dir 身份账号的登录绑定在目录路径字符串上，所以 v0.1 把改名一律判为冲突（v0.1 方案 §5.1.8）。读出钥匙串密文再写回来改名的做法与本项目“不读写凭据”冲突。本方案让账号记录自己的目录名，改名只改账号名与启动命令名，目录不动，登录自然保留。
- **MCP**：Claude Code 的 user 作用域 MCP 服务器由 `claude mcp add --scope user` 写在配置目录的 `.claude.json` 里。要给某个账号加 MCP，用户需要自己拼出该账号的 `CLAUDE_CONFIG_DIR` 再运行 `claude mcp`；用启动命令 `claude-<名称> mcp …` 也不可靠——固定参数排在子命令之前，例如 `--allowedTools` 这类取多个值的选项会把 `mcp`、`add` 当成自己的值吞掉（`claude --help` 2.1.286：`--allowedTools <tools...>`、`--mcp-config <configs...>`）。

## 2. 目标 / 非目标

**目标**

1. `multi-claude rename OLD NEW`：改账号名与启动命令名，目录不动、登录不掉；路由规则与默认路由跟着改。
2. `multi-claude mcp NAME [claude mcp 的参数…]`：用该账号的身份环境（配置目录、鉴权变量清除、额外环境变量、代理）运行 `claude mcp …`，不带固定参数。

**非目标**

- 跨账号共享 MCP 服务器；改写任何 `.claude.json`。
- 只改大小写的改名（`rename Work work`）：在大小写不敏感的文件系统上新旧启动命令是同一个文件，删旧建新会互相覆盖，判为用法错误。
- 搬迁账号目录（目录名改了 macOS 上就掉登录）。

## 3. 假设与约束

- 账号新增字段 `dir`：账号目录在根目录下的名字。缺省等于账号名；`to_dict` 只在 `dir` 与账号名不同时写出，所以 v0.2 的配置读入再写回内容不变。
- `dir` 与账号名一样遵守 `NAME_PATTERN`（`config.py` `NAME_PATTERN`），不能含 `/`。
- 所有账号的 `dir` 按大小写不敏感互不相同；一个账号的名字也不能等于另一个账号的 `dir`（否则 `add` 新账号会落进别人的目录）。
- `dir` 是工具内部状态：`apply -f` 对已登记账号沿用当前值（与 `identity`、`managed_links` 相同，`cli.py` `_load_apply_file`）；未登记账号取文件中的值（缺省等于名字）。
- `claude mcp` 的行为（写哪个文件、作用域）由 Claude Code 决定，本工具只负责环境与转交参数。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `193cef8`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/config.py` | `Account.__init__` :69、`copy` :80、`to_dict` :84、`_parse_account` :303-324、`parse_config` 账号循环 | 修改 | 新增 `dir` 字段；校验格式与唯一性 |
| `src/multi_claude/accounts.py` | `account_dir` :40-45 | 修改 | 用 `config.find(name).dir`（找不到时用 name） |
| `src/multi_claude/accounts.py` | `plan` :90、:103 | 修改 | 两处直接拼接改为用账号的 `dir` |
| `src/multi_claude/accounts.py` | `_plan_account_dir` :177、:183 | 修改 | 大小写核对的比较对象由账号名改为 `account.dir` |
| `src/multi_claude/accounts.py` | `plan` 账号循环之前 | 新增 | 目录唯一性冲突（§5.1.2） |
| `src/multi_claude/cli.py` | `build_parser`、`dispatch` | 修改 | 新增 `rename`、`mcp` |
| `src/multi_claude/cli.py` | `_split_args_command` :138-160 | 修改 | `mcp NAME` 之后的全部内容原样作为 claude 参数 |
| `src/multi_claude/cli.py` | `_load_apply_file` | 修改 | 已登记账号沿用当前 `dir` |
| `src/multi_claude/env.py` | 新文件 | 新增 | 按账号构造启动环境（与启动命令同语义），供 `mcp` 使用 |
| `src/multi_claude/completion.py` | `POSITIONAL_KINDS` | 修改 | `rename`、`mcp` 的第 1 个位置参数补账号名 |
| `tests/helpers.py` | `FAKE_CLAUDE` :26-29 | 修改 | 假 claude 按 `FAKE_CLAUDE_RC` 返回退出码 |
| `tests/test_rename.py`、`tests/test_mcp.py` | 新文件 | 新增 | 见 §8 |
| `README.md`、`README.zh-CN.md`、`CHANGELOG.md` | 命令表、新增小节、“手工搬迁”一节 | 修改 | 文档同步 |

`account_dir` 的调用点全集（`grep -rn "account_dir(" src/`，不截断，基线共 8 处）：`accounts.py:54`、`:232`、`:315`、`:354`，`cli.py:275`、`:578`、`:638`，`doctor.py:137`。改 `account_dir` 的实现即全部生效；另有 `accounts.py:90`、`:103` 两处与 `migrate.py:137` 一处直接拼接，其中 migrate 是新建账号（`dir` 等于名字），不改。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 配置

```json
"accounts": {
  "job": {"dir": "work", "proxy": "7901", ...}
}
```

- `Account.dir: str`，构造缺省为 `name`。`to_dict` 仅当 `dir != name` 时写出 `"dir"`。
- `_parse_account`：`dir` 缺省为 name；存在时必须是字符串且匹配 `NAME_PATTERN`，否则 `ConfigError`。
- `parse_config` 在账号循环后检查：所有账号的 `dir.casefold()` 互不相同；任一账号的 `name.casefold()` 不等于另一个账号的 `dir.casefold()`。违反时 `ConfigError`（这类配置只能来自手写文件）。

#### 5.1.2 收敛计划

- `account_dir(config, name)` 改为 `os.path.join(expand(config.root), dir_of(name))`，`dir_of` 取 `config.find(name).dir`，找不到时用 name。
- `plan` :90 改为 `os.path.join(new_root, account.dir)`；:103 改为 `os.path.join(old_root, old_account.dir)`。
- `_plan_account_dir` 的大小写核对改为与 `account.dir` 比较（`accounts.py:177`、`:183` 中的 `account.name` 换成 `account.dir`），提示文字不变。
- 新冲突：新配置中某账号的 name 等于另一个账号的 dir（大小写不敏感）→ `CONFLICT config`，原因 `account {name!r} would use the directory of account {other!r}`。典型触发：`rename work job` 之后再 `add work`。这条在计划里判（`add` 路径），配置解析的同类检查覆盖手写文件。

#### 5.1.3 `rename OLD NEW`

1. OLD 必须已登记（未登记返回 1）；NEW 必须合法（否则 2）。
2. `NEW.casefold() == OLD.casefold()` → 用法错误 2：`only the letter case differs; renaming that way is not supported`。
3. NEW 已是另一个账号的名字 → 冲突 3（命令直接输出 `account NEW already exists`，不进入收敛）；NEW 等于另一个账号的目录 → 冲突 3（计划阶段 `_plan_dir_owner_conflict`，与 `config._check_dirs` 同一套规则：目录相同、或账号名等于另一个账号的目录，保证写出的配置下次仍能加载）。
4. 新配置：账号对象换键 NEW、`name=NEW`、`dir` 保持原值（第一次改名时即为 OLD）；所有 `route_rules` 中 `account` 等于 OLD 的改为 NEW，`route_default` 同理。
5. 收敛（`orphan_scope` 含 OLD）：旧启动命令 `claude-OLD` 作为“账号移除”的启动命令被删除（`accounts.py:99-104` 的既有路径），新启动命令 `claude-NEW` 创建；目录、共享软链、登录不变。`plan` 中旧账号的 `plan_remove_links` 会对 `old_account` 的受管链接计划删除——改名时新旧账号是同一个目录，必须跳过：当新配置中存在 `dir` 相同的账号时，不对旧账号计划删除共享链接。
6. 输出：`renamed OLD to NEW (directory <root>/<dir> unchanged)`。

由于 dir 身份启动命令中的 `account_dir=` 行不变，`_plan_launcher` 的 login-bound 冲突（`accounts.py:229-237`）不会触发。

#### 5.1.4 `mcp NAME [参数…]`

- 解析：`_split_args_command`（`cli.py:138`）增加：子命令为 `mcp` 时，`mcp` 之后的第一个词是账号名（以 `-` 开头则用法错误 2），其后全部内容原样作为 `claude mcp` 的参数，不经 argparse。
- `env.py` 提供：

  ```python
  def account_env(config: Config, account: Account, base: Mapping[str, str]) -> Dict[str, str]
  ```

  在 base（`os.environ` 的副本）上按启动命令的同一顺序处理：身份（dir：设 `CLAUDE_CONFIG_DIR` 为 `account_dir`、清 `CLAUDE_SECURESTORAGE_CONFIG_DIR`；default：清 `CLAUDE_CONFIG_DIR` 与 `CLAUDE_SECURESTORAGE_CONFIG_DIR`）→ 清 `launcher.AUTH_OVERRIDE_ENV` → 设置 `effective_env`（`~` 展开规则同 `launcher.expand_value`）→ 代理（与 `launcher._proxy_lines` 相同：inherit 不动、off 清 8 个、URL 设 4 个并清 `ALL_PROXY` 大小写、`NO_PROXY`/`no_proxy` 追加 `localhost,127.0.0.1,::1`）。
- default 身份：先做与启动命令相同的检查——`~/.claude` 的物理路径必须等于账号目录（`accounts.default_link_status == "ok"`），否则输出与启动命令相同的提示并返回 1。dir 身份：账号目录不存在返回 1。
- `PATH` 中找不到 `claude` 返回 127。
- 运行 `subprocess.call(["claude", "mcp", *参数], env=...)`，返回它的退出码；不加写锁（与 `list` 一样在 `pending_journal_notice` 之后分派），但存在未完成的迁移时拒绝执行并返回 1，避免 `claude` 写进迁移到一半的目录。
- 不带固定参数：固定参数里可能有取多个值的选项，会吞掉子命令。

### 5.2 接口变更

- 配置：账号新增可选字段 `dir`。旧版本读到带 `dir` 的配置会忽略它、按账号名找目录——启动命令的 `account_dir=` 行与之不符，旧版本的 login-bound 检查（`accounts.py:229-237` 同款逻辑，v0.1 起即有）会判冲突而不是改写，不会掉登录；README 写明降级前不要有改过名的账号。
- 新增子命令 `rename OLD NEW [--dry-run]`、`mcp NAME [参数…]`。
- `list --verbose` 与 `list --json` 的 `dir` 字段本来就是完整路径，改名后显示实际目录；`list` 表格不变。

本项目没有 `docs/reference/*`，不涉及 reference 章节的 sibling 回补检查。

## 6. 备选方案与决策

- **改名时搬目录并迁移钥匙串条目**：需要读出凭据，违反原则。不做。
- **`mcp` 通过启动命令转交（`claude-NAME mcp …`）**：固定参数在子命令之前，取多个值的选项会吞掉子命令。不做。
- **`mcp` 在 shell 里拼环境**：与启动命令重复一套 sh；Python 构造环境便于测试，并用“同一账号下 `mcp` 与启动命令给出的环境逐项相同”的对拍测试防止两套实现漂移。

## 7. 影响分析

**正向推**

- `account_dir` 实现改变影响全部 8 个调用点：未改名的账号 `dir == name`，路径与之前逐字相同，行为不变（v0.2 与路由的全部用例回归）。
- 改名后启动命令名变化：用户的别名或脚本里的 `claude-OLD` 失效；README 说明。路由入口 `claude-auto` 因规则改指 NEW 而被重新生成。
- `shared.plan_remove_links` 在改名时被跳过（§5.1.3 第 5 条），否则会删掉仍在使用的共享软链。

**反向推**

- 账号目录大小写核对（`_actual_entry_name`）的比较对象换成 `dir`，原有“`add work` 命中磁盘上的 `Work/`”冲突语义不变（`dir == name`）。
- `migrate-default` 的目标目录仍是 `<root>/<名称>`，新账号 `dir` 缺省等于名字，一致。
- `usage`、`doctor`、`identity.probe` 都经 `account_dir` 取目录，改名后仍指向原目录。

**运行时**

- `mcp` 新启动一个 `claude mcp` 子进程，等它结束；不新增常驻进程。

**部署形态**

- 降级到不认识 `dir` 的版本：见 §5.2，旧版本判冲突、不改写。

## 8. 回归测试

1. **配置**：`dir` 非法、重复（大小写不敏感）、账号名等于另一账号的 dir → `apply -f` 返回 1；无 `dir` 的旧配置读入再写回逐字不变。
2. **rename**：
   - `rename work job`：`claude-job` 生成、`claude-work` 删除，目录 `<root>/work` 与其中文件不变，共享软链仍在且仍记录为受管；假 claude 经 `claude-job` 收到的 `CLAUDE_CONFIG_DIR` 仍是 `<root>/work`；假 security 收到的服务名与改名前相同；
   - 路由规则与默认路由跟着改名，`claude-auto` 更新；
   - 再次执行返回 1（OLD 已不存在）；`--dry-run` 不写任何文件；
   - `rename job work` 改回：`dir` 与名字相同，配置中不再写 `dir`；
   - `rename work Work` → 2；`rename work main`（已存在）→ 3；`rename nobody x` → 1；
   - 改名后 `add work` → 3（会落进 job 的目录）；
   - default 身份账号改名：启动命令仍不设 `CLAUDE_CONFIG_DIR`，`~/.claude` 软链不变；
   - `apply -f` 文件中把已登记账号的 `dir` 写成别的值：沿用当前值。
3. **mcp**：
   - `mcp work list`：假 claude 收到的参数恰为 `mcp list`（不含固定参数），环境中的 `CLAUDE_CONFIG_DIR`、代理变量、额外环境变量与经 `claude-work` 启动时逐项相同，4 个鉴权变量被清除；
   - `mcp work add x -- npx foo --flag`：参数原样传递（含 `--` 与以 `-` 开头的参数）；
   - default 身份：不设 `CLAUDE_CONFIG_DIR`；`~/.claude` 软链被删时返回 1、假 claude 未执行；
   - 假 claude 返回 5 时 `mcp` 返回 5；`PATH` 中无 claude 返回 127；未登记账号返回 1；`mcp` 后无账号名或账号名以 `-` 开头返回 2；
   - 对拍：对 dir 身份（代理端口、off、inherit 各一）与 default 身份，`account_env` 的结果与启动命令实际导出的环境在“身份、鉴权、额外环境变量、代理”相关键上完全一致。
4. **补全**：`rename `、`mcp ` 后补账号名。
5. **回归**：既有全部用例通过；未改名时 `list`、`list --json`、启动命令内容与 `193cef8` 一致。

## 9. 日志 / 观测点

- `rename`：动作行（`delete launcher …/claude-OLD`、`create launcher …/claude-NEW`、`update config`、`update router`）与一行 `renamed OLD to NEW (directory … unchanged)`。
- `mcp`：不额外输出，`claude mcp` 的输出原样透传；失败时退出码即 `claude` 的退出码。
- `list --verbose` / `--json` 的 `dir` 显示实际目录，用于确认改名后指向。
