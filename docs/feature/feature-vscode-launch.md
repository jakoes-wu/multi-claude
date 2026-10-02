# multi-claude：按账号打开 VS Code（v0.6.0）

> 2026-10-02 注记：代码已按方案实现（`cli.py` `cmd_code`、`_exec_as`、`_private_dirs`；补全；README 双语、CHANGELOG、roadmap §2.6；`tests/test_vscode.py` 11 例），方案评审 M1/M2 与低级意见已处理。基线：main `4ff1a8a`（v0.5.0）。来源：用户 2026-10-02 同意的迭代计划 v0.6.0 `code NAME [PATH]`，以及用户授权的实测（“允许实测，不支持就不做”）。参考同作者项目 multi-codex 的 `code` 命令。

## 1. 背景

VS Code 里的 Claude Code 扩展只认 VS Code 启动时环境中的 `CLAUDE_CONFIG_DIR`。从 Dock 或 Finder 打开的 VS Code 没有这个变量，扩展用的是 `~/.claude`。目前没有命令能按账号打开 VS Code。

### 1.1 已核实的事实

| 事实 | 依据 |
| ---- | ---- |
| 扩展启动 claude 进程时的环境是 `{...process.env}`，再叠加用户设置 `claudeCode.environmentVariables`；设置里的 `CLAUDE_CONFIG_DIR` 只接受绝对路径，有值时覆盖进程环境 | 本机扩展 `anthropic.claude-code-2.1.286` 的 `extension.js`：函数 `Z$`（`Q={...process.env}`）、`g41`、`y41`（只读核对） |
| 扩展宿主自身也按 `process.env.CLAUDE_CONFIG_DIR` 计算钥匙串服务名和 `.claude.json` 的位置 | 同一文件中 `CLAUDE_CONFIG_DIR,X=J!==void 0?J.normalize("NFC")…sha256…substring(0,8)` 与 `CLAUDE_CONFIG_DIR\|\|homedir(),'.claude…json'` |
| 不同的 `--user-data-dir` 会开出新的 VS Code 实例；从命令行启动时，扩展宿主得到的是启动时的环境 | 2026-10-02 本机实测（VS Code 1.139.1）：用 `multi-claude run jakoes.wu@icloud.com -- code --user-data-dir <临时目录> --new-window <临时工作区>` 打开的新实例与已运行的实例并存；其扩展宿主进程（`Code Helper (Plugin)`）的环境中 `CLAUDE_CONFIG_DIR=~/.cc/jakoes.wu@icloud.com`、`HTTPS_PROXY` 为该账号的代理、`VSCODE_CLI=1` |
| 扩展按这个目录工作 | 同一实测：扩展激活后在 `~/.cc/jakoes.wu@icloud.com/ide/` 写了锁文件，内容含临时工作区路径；`~/.claude/ide/` 没有新文件；实例退出后锁文件被删除 |
| 换了用户数据目录后，扩展仍从 `~/.vscode/extensions` 加载 | 同一实测：新实例的扩展宿主拉起的子进程来自 `~/.vscode/extensions/...` |
| macOS 上 `code` 命令通过 `open -n -g --env K=V …` 拉起应用，环境变量会出现在 `open` 的命令行参数中 | 本机 VS Code 1.139.1 `out/cli.js` 的 darwin 分支：对每个环境变量 `push("--env", K=V)`，再执行 `open`（评审核对） |

未核实：扩展面板界面上显示的登录账号（需要打开面板，属界面操作，未做）；Linux 上的行为（只按同样的机制推断）；从 VS Code 集成终端运行时，`VSCODE_*`、`TERM_PROGRAM` 等变量会随 `--env` 进入新实例，影响未核实（`CLAUDECODE`、`CLAUDE_CODE_CHILD_SESSION` 会被扩展的 `Z$` 删除，已核实）。

## 2. 目标 / 非目标

**目标**

1. `multi-claude code NAME [PATH] [-- CODE_ARGS ...]`：以账号启动命令的环境（与 `run` 相同）打开一个独立的 VS Code 实例，用户数据目录按账号区分。

**非目标**

- 不复制 VS Code 的设置、快捷键：每个账号的实例第一次打开时是默认设置。
- 不管理实例的生命周期（不关闭、不检查是否已在运行）。
- 不支持远程窗口（Remote-SSH 等），不支持其它编辑器（Cursor 等）。
- 同一账号的实例已在运行时，请求会转给它；之后改的代理、环境变量要等该实例退出后才生效（README 说明）。
- 不改 `claudeCode.environmentVariables` 等 VS Code 设置。

## 3. 假设与约束

- 每个账号的 VS Code 用户数据目录为 `<root>/.apps/<账号目录名>/vscode`，第一次使用时以 0700 创建（各级）。账号名与 `dir` 字段都必须以字母或数字开头（`config.py:31` `NAME_PATTERN`，`dir` 的校验在 `config.py:369-370`），`.apps` 不会与账号目录重名。`<root>/.apps` 已是软链时 `makedirs` 会跟随它，已是普通文件时抛 `OSError`、由 `main` 按 1 报告，不另做处理。按目录名而不是账号名存放：`rename` 后数据仍在原处。`remove` 不删除它。
- macOS 的 unix 套接字路径上限 104 字节，VS Code 在 macOS 上把主套接字放在用户数据目录下（文件名形如 `1.13-main.sock`，约 15 字节；VS Code `out/main.js` 构造套接字路径的函数），目录路径超过 80 字节时提示；Linux 设了 `XDG_RUNTIME_DIR` 时套接字不在这里，不提示。
- 实验功能：每次运行在 stderr 打一行，写明依赖未公开的行为与验证过的版本（VS Code 1.139.1、扩展 2.1.286）。
- 在新实例里把 `claudeCode.environmentVariables` 设成别的 `CLAUDE_CONFIG_DIR`，会覆盖本命令传入的值（§1.1）；README 说明。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `4ff1a8a`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `cli.py` `COMMAND_GROUPS` | :58 | 修改 | “Everyday” 增加 `code` 一行 |
| `cli.py` `build_parser` | `p_path` 定义之后 | 新增 | `code` 子命令：`name`、可选 `path` |
| `cli.py` `_split_args_command` | `run` 分支 | 修改 | `code` 与 `run` 一样从第一个 `--` 切出参数 |
| `cli.py` `main` | `run`/`path` 分派处 | 修改 | `code` 同样只读分派、迁移未完成时拒绝 |
| `cli.py` `cmd_run` | :810 起 | 修改 | 抽出 `_exec_as(config, account, command)`（检查账号可用、查找可执行文件、exec），`run` 与 `code` 共用 |
| `cli.py` | `cmd_path` 之后 | 新增 | `cmd_code` |
| `completion.py` `POSITIONAL_KINDS` | :22 | 修改 | `code`: `["name", "file"]`（PATH 可以是目录或文件） |
| `README.md`、`README.zh-CN.md` | “Running other commands as an account” 之后（:248-261）、命令表、Uninstalling（:434） | 新增 / 修改 | 新小节 “Opening VS Code for an account”；命令表一行；卸载一节说明 `.apps` 目录 |
| `CHANGELOG.md` | Unreleased | 修改 | Added |
| `docs/analysis/roadmap.md` | §2.5 之后 | 新增 | §2.6 |
| `tests/test_vscode.py` | 新文件 | 新增 | §8 |

## 5. 方案

### 5.1 实现要点

- 解析：`code NAME [PATH]`，第一个 `--` 之后的内容原样作为 VS Code 参数。
- `cmd_code`：
  1. 打印实验提示（stderr）。
  2. 账号未登记退出 1。
  3. 先用 `_usable_account_env` 确认账号可用（不可用退出 1，不建任何目录）；再计算并创建用户数据目录：`os.path.join(expand(config.root), ".apps", account.dir, "vscode")`，三级目录都 `makedirs(mode=0o700)` 后再 `chmod 0o700`（`makedirs` 的 mode 受 umask 影响）。
  4. macOS 上：目录路径长度超过 80 时 warn；并 warn “`code` 会把整个环境（含本账号的环境变量）交给 `open --env`，值会短暂出现在进程列表中”。
  5. 命令 `["code", "--user-data-dir", data_dir] + ([PATH] if PATH else []) + code_args`，交给 `_exec_as`。
- `_exec_as`：从 `cmd_run` 原样抽出“`_usable_account_env` → 在账号环境的 PATH 中查找 `command[0]`（找不到退出 127）→ flush → `os.execvpe`”这一段。`code` 找不到时的报错额外提示：在 VS Code 里运行 “Shell Command: Install 'code' command in PATH”。
- default 身份账号：环境里没有 `CLAUDE_CONFIG_DIR`，扩展用 `~/.claude`，与直接打开 VS Code 相同；仍会得到独立实例与该账号的代理、环境变量。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `code NAME [PATH] [-- CODE_ARGS ...]` | 新增；退出码 0（exec 后由 VS Code 决定）、1、2、127 | 新增 |
| `<root>/.apps/` | 新增目录（按需创建） | 新增；其它命令不读它 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **不传 `--user-data-dir`**：VS Code 已在运行时，请求会转给已运行的实例，新窗口沿用那个实例的环境，账号不生效（multi-codex 方案 §1.1 对 `mainIPCHandle` 的核对）。因此必须按账号区分用户数据目录。
- **写 `claudeCode.environmentVariables` 到工作区设置**：会修改用户的仓库文件（`.vscode/settings.json`），且只对该工作区生效；不采用。

## 7. 影响分析

- 新命令只读配置，不加写锁，不改 `config.json`；唯一的写入是创建 `<root>/.apps/<目录名>/vscode`（0700）。
- `run` 的行为不变：`_exec_as` 是原样抽出的同一段逻辑，`test_everyday` 的 run 用例全部保留。
- 其它命令不遍历 `<root>`（`accounts.py:216` 的 `listdir` 只在账号目录的父目录中按名查找该账号自己的目录项），`.apps` 不影响收敛、`doctor` 与迁移。
- 进程：exec 成为 `code` 命令；macOS 上它很快退出，VS Code 在后台运行，与本工具无关。

## 8. 回归测试

**环境**：本机（Python 3.11）与编译机 ubuntu20（Python 3.8.10）各跑全量 `unittest`，并跑 CI 的 lint（`pyflakes src tests`、tests 路径检查）。测试用假 `code` 脚本（放在 fakebin，记录参数与环境），不启动真实 VS Code。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | `code work` 与 `code work ~/proj -- --new-window` | 假 code 收到 `--user-data-dir <root>/.apps/work/vscode`、路径与参数；环境含 `CLAUDE_CONFIG_DIR=<work 目录>` 与代理；stderr 有实验提示 |
| T2 | 用户数据目录 | 三级目录权限均为 0700；重复执行不报错 |
| T3 | `rename work client` 后 `code client` | 仍用 `.apps/work/vscode` |
| T4 | 失败：未登记、PATH 中没有 `code`、迁移未完成、缺 NAME、账号目录不存在；`run` 找不到命令 | 退出 1、127（提示 Install 'code' command）、1、2、1 且未创建 `.apps`；`run` 的报错不含 Install 提示 |
| T5 | default 身份账号；其 `~/.claude` 被改指；`remove` 之后；macOS 的两条提示 | 环境中没有 `CLAUDE_CONFIG_DIR`；退出 1 且未建目录；`.apps/<目录名>` 保留；`open --env` 提示总有，长 root 时有路径过长提示 |
| T6 | 帮助与补全 | `--help` 含 `code`；三种 shell 的补全含 `code` |
| T7 | 回归 | 现有 329 个用例全部通过（含 `run` 的全部用例） |
| T8 | 实机（已做，见 §1.1） | 扩展宿主环境与 IDE 锁文件指向该账号目录 |

## 9. 日志 / 观测点

- stderr：`[multi-claude] warning: experimental: code relies on undocumented behaviour of VS Code and the Claude Code extension (verified with VS Code 1.139.1, extension 2.1.286)`
- macOS：`… is long; VS Code's socket path inside it may exceed the 104-byte limit`；`code passes the whole environment … to open --env …`
- 找不到 `code`：`error: code not found in PATH; in VS Code run "Shell Command: Install 'code' command in PATH"`
