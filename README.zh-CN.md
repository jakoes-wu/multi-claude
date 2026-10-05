# multi-claude

[English](README.md) | **简体中文**

[![Release](https://img.shields.io/github/v/release/jakoes-wu/multi-claude)](https://github.com/jakoes-wu/multi-claude/releases)
[![CI](https://github.com/jakoes-wu/multi-claude/actions/workflows/ci.yml/badge.svg)](https://github.com/jakoes-wu/multi-claude/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/multi-claude-cli)](https://pypi.org/project/multi-claude-cli/)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

在一台机器上同时使用多个 [Claude Code](https://code.claude.com/docs) 账号。每个账号有自己的登录、设置和历史记录，还可以有自己的代理，不用再反复退出、重新登录。

它从不读取你的登录信息，不联网，也不改你的 shell 配置文件。

```sh
claude-work        # 用工作账号运行 Claude Code
claude-personal    # 在另一个终端里，同时用个人账号运行
multi-claude       # 查看你的账号、登录状态和用量
```

![multi-claude 演示：新增两个账号并查看](https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/docs/assets/demo.gif)

<sub>演示中的账号均为示例。</sub>

## 工作原理

Claude Code 把设置、登录和历史记录放在 `CLAUDE_CONFIG_DIR` 指定的目录里（默认 `~/.claude`），官方文档给出的多账号做法就是“每个账号一个目录”。multi-claude 替你管理这些目录，并为每个账号生成独立的启动命令，可以分别设置代理、额外环境变量和固定参数：

```text
claude-work       -> CLAUDE_CONFIG_DIR=~/.cc/work      HTTPS_PROXY=http://127.0.0.1:7901
claude-personal   -> CLAUDE_CONFIG_DIR=~/.cc/personal  （沿用当前 shell 的代理设置）
claude-main       -> 原来的 ~/.claude 账号，登录不失效
claude            -> 行为不变
```

启动命令是普通的 shell 脚本，即使卸载了 multi-claude 也能继续使用。

## 功能

- **不读取登录信息**：唯一的钥匙串操作只查询条目是否存在，从不打开钥匙串条目或 `.credentials.json` 的内容。`migrate-default` 和 `restore` 会整体搬动账号目录，Linux 上凭据文件随目录一起移动，内容不会被打开。
- **不联网**：multi-claude 本身不发起任何网络连接，没有统计上报，也不检查更新。只有安装脚本、pipx 或 Homebrew 会下载安装包。
- **不改你的 shell**：需要修改 `PATH` 时，只打印要加的那一行，不会改 `~/.zshrc` 或 `~/.bashrc`。
- **只改你要它改的**：它只写自己的配置与状态文件、启动命令、账号目录（包括 `code` 放在 `<root>/.apps` 里的 VS Code 数据）和共享软链。Claude Code 的设置文件只在两种情况下被修改：`statusline install`（你指定的文件，会先备份）和 `add --config-from`（新账号的 `settings.json`）。
- **迁移默认账号**：把已有的 `~/.claude` 移进账号根目录，登录不失效，直接运行 `claude` 的行为与之前完全一致。
- **新增账号**：创建账号目录和 `claude-<名称>` 启动命令，或把已有目录登记为账号。
- **每个账号单独设置代理**：本地端口、HTTP(S) 代理地址、`off` 或 `inherit`。
- **额外环境变量与固定参数**：全局默认值加账号级设置，例如 `--settings`、`--permission-mode`。
- **一键部署**：`install.sh --config accounts.json` 装好工具并创建文件里的全部账号。
- **幂等**：所有命令都可以放心重复执行。写命令只输出有变化的项（加 `--verbose` 列出全部）；与不归 multi-claude 管理的文件冲突时只报告、不做任何修改；迁移中断后重跑会从实际状态继续。
- **可选的共享资源**：把一个共享目录里的 `CLAUDE.md`、`skills`、`agents`、`commands` 软链到指定账号。

## 前置条件

**服务器**：不需要。multi-claude 只在你自己的电脑上运行，不联网。

**你的电脑**：

| | 必需 | 可选 |
| ---- | ---- | ---- |
| 操作系统 | macOS 或 Linux（WSL 按 Linux 算） | 无 |
| 工具 | Python 3.8+（`python3`，只用标准库）；Claude Code（`claude`）；安装脚本需要 `curl` 或 `wget`，以及 `tar` | `pipx` 或 Homebrew：从 PyPI 或 tap 安装时用；`lsof`：`migrate-default` 和 `restore` 用它查找占用 `~/.claude` 的进程（Linux 上没有时改读 `/proc`）；VS Code 的 `code` 命令：`multi-claude code` 时用 |

缺什么装什么：

```sh
# macOS（需要 Homebrew，https://brew.sh）；curl、tar、lsof 系统自带
brew install python
curl -fsSL https://claude.ai/install.sh | bash    # 还没装 Claude Code 时

# Debian / Ubuntu / WSL
sudo apt update
sudo apt install -y python3 curl tar lsof
curl -fsSL https://claude.ai/install.sh | bash    # 还没装 Claude Code 时
```

暂不支持 Windows 原生运行，请用 WSL。Claude Code 的其它安装方式：https://code.claude.com/docs/en/setup

## 安装

从克隆的仓库安装：

```sh
git clone https://github.com/jakoes-wu/multi-claude.git
cd multi-claude
./install.sh
```

或直接安装：

```sh
curl -fsSL https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/install.sh | sh
```

工具装到 `~/.local/share/multi-claude`，`multi-claude` 命令装到 `~/.local/bin`。用 `--prefix DIR` 可以装到别处。请确认 `~/.local/bin` 在 `PATH` 中；安装脚本只给出提示，从不修改 shell 配置文件。

`multi-claude` 命令用安装时 `PATH` 上找到的 `python3` 运行。若它是版本管理器的 shim（pyenv、asdf、mise），安装脚本会改写成它背后的真实解释器：shim 每次启动要多花几十毫秒，装了状态栏钩子后每次刷新状态栏都要启动一次。以后卸载了这个 Python 版本，请重新运行安装脚本。

也可以从 PyPI 安装：`pipx install multi-claude-cli`（升级用 `pipx upgrade multi-claude-cli`），命令仍是 `multi-claude`。pipx 和安装脚本二选一，不要同时用：两者都把同一个命令放进 `~/.local/bin`。

用 Homebrew：`brew install jakoes-wu/tap/multi-claude`（升级用 `brew upgrade multi-claude`）。它装在 Homebrew 自己的目录里，先卸掉其它方式装的版本，免得 `PATH` 上有两个 `multi-claude` 命令。

**下载校验**：从 v0.4.0 起，每个 release 都附带 `multi-claude-<tag>.tar.gz` 和 `SHA256SUMS`。远程安装会下载这个包，先校验 SHA-256，不一致就停止安装。安装分支或更早的版本时没有校验，安装脚本会明确提示；设置 `MULTI_CLAUDE_REQUIRE_CHECKSUM=1` 可以拒绝这种安装。校验和与安装包放在同一个 release 里，只能发现下载过程中的损坏或篡改，不能防范 GitHub 账号本身被攻破。

`./install.sh --help` 列出全部选项。

## 快速开始

### 1. 每个登录建一个账号

```sh
multi-claude add work --proxy 7901   # 新建账号 work 及其启动命令，走本地 7901 端口代理
multi-claude add personal            # 再建一个，沿用 shell 的代理设置
```

### 2. 每个账号登录一次

```sh
multi-claude login work              # 以该账号的身份运行 `claude auth login`
multi-claude login personal
```

### 3. 用启动命令代替 `claude`

```sh
claude-work                          # 在一个终端里
claude-personal                      # 在另一个终端里，同时运行
```

### 4. 检查一切是否正常

```sh
multi-claude                         # 账号、登录、用量，以及有没有问题
multi-claude doctor                  # 完整检查，每个问题都给出修复方法
```

### 已经在用 Claude Code？

已有的 `~/.claude` 不会被改动，直接运行 `claude` 照常使用它。想把它也作为账号管理，见[迁移 `~/.claude`](#迁移-claude)（可选；需在关闭所有 Claude 会话后、在普通终端里运行）。

## 常见操作

下面各节彼此独立，按需查看。完整选项见[命令参考](#命令)。

### 代理取值

| 取值 | 启动命令中的行为 |
| ---- | ---- |
| `inherit`（默认） | 不动任何代理变量，沿用当前 shell 的值 |
| `off` | 清除 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写形式 |
| `7901` | 等同于 `http://127.0.0.1:7901` |
| `http://主机:端口`、`https://主机:端口` | 设置 `HTTPS_PROXY`/`HTTP_PROXY`（大小写都设）；清除继承来的 `ALL_PROXY`（Claude Code 自己调用 git 时会退而使用它）；在原有 `NO_PROXY` 后追加 `localhost,127.0.0.1,::1` |

SOCKS 地址一律拒绝：[Claude Code 不支持 SOCKS 代理](https://code.claude.com/docs/en/network-config)。代理地址不能带用户名和密码，因为启动命令是所有人可读的明文文件。

**settings 文件优先。** 账号的 `settings.json`、`.claude.json` 或 `--settings` 指定的文件里，如果 `env` 中设置了代理变量，Claude Code 以它为准，启动命令设的值会被覆盖。multi-claude 发现这种情况会告警（只读取变量名，不读取值）。管理员下发的托管设置不检查。

**后台会话。** 每个账号的后台 supervisor 继承第一个启动它的 shell 的环境。需要后台会话稳定走代理时，把代理写进该账号自己的 `settings.json` 的 `env`，并把 multi-claude 中该账号的代理设为 `inherit`，避免两处设置互相覆盖。

`CLAUDE_CODE_HTTP_PROXY` 和 `CLAUDE_CODE_HTTPS_PROXY` 不受启动命令影响，`off` 也不清除它们；设置了这两个变量时 `list` 会提示。

### 环境变量与参数

```sh
multi-claude args --defaults -- --ide --permission-mode acceptEdits \
  --allowedTools "Bash(git status),Read" \
  --settings ~/.claude-shared/settings.json
multi-claude env --defaults CLAUDE_CODE_PLUGIN_CACHE_DIR=~/.claude-shared/plugins
multi-claude env work MAX_THINKING_TOKENS=8000 --unset OLD_VAR
```

- 账号的值覆盖同名的默认值；参数顺序为默认参数、账号参数，运行 `claude-<名称>` 时传入的内容永远在最后。
- 值恰好是 `~` 或以 `~/` 开头时展开成绝对路径，其它值原样写入（不做 `$` 展开）。`--settings=~/x` **不会**展开，请写成 `--settings ~/x`。
- 决定使用哪个登录的变量（`CLAUDE_CONFIG_DIR`、`CLAUDE_SECURESTORAGE_CONFIG_DIR`、`CLAUDE_CODE_CUSTOM_OAUTH_URL`、`HOME`、`USER`）、代理变量（请用 `proxy`）以及凭据（API key、token、secret、password 之类）一律拒绝。凭据请写进账号 `settings.json` 的 `env` 或使用 `apiKeyHelper`。
- `args` 必须显式写 `--`。`--allowedTools`、`--add-dir` 这类取多个值的选项会吞掉后面所有不以 `-` 开头的参数；如果它是固定参数里的最后一个选项，运行启动命令时传入的提示词会被它吞掉，multi-claude 会告警。请在它后面再放一个选项（如上例的 `--settings`）。
- 启动命令一律清除 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CODE_OAUTH_REFRESH_TOKEN`：这些变量在 shell 里导出后，会让所有账号改用同一份凭据、绕过各自的登录。直接运行 `claude` 不受影响。

### 共享资源

```sh
multi-claude add work --shared              # 从 ~/.claude-shared（或已设置的共享目录）共享
multi-claude set work --shared ~/claude-common  # 把 ~/claude-common 设为所有账号的共享目录
```

把希望所有账号都能看到的 `agents`、`commands`、`skills` 或 `CLAUDE.md` 放进共享目录；multi-claude 不会创建或填充它，里面一项都没有时会提示。用 `--shared DIR`（或 `init --shared-dir DIR`）更换目录时，所有开启共享的账号的链接都会跟着改。

默认条目为 `agents`、`commands`、`skills`、`CLAUDE.md`，只链接 `shared.items` 中列出的条目。multi-claude 只创建缺少的软链，并记住哪些是它建的。关闭共享时只删除这些软链，你自己建的软链保持不动。软链位置上已有真实文件或目录时判为冲突，绝不覆盖。

如果之前已经手工把账号链接到了共享目录，`multi-claude add 名称 --shared --adopt` 会接管这些软链，不重建。

想让某个账号单独不共享某一项（例如用它自己的 `skills`）：

```sh
multi-claude set work --shared-exclude skills    # 删除 multi-claude 建立的 skills 软链
multi-claude set work --shared-include skills    # 重新链接
```

其它项照常共享，共享目录不受影响；你自己在那个位置放的目录或软链不会被动。`list` 显示 `yes (not: skills)`。用 `apply -f` 时，文件里没写 `shared_exclude` 的账号会恢复共享全部项。

保存账号自身状态的条目不能共享：`.credentials.json`、`.claude.json`、`settings.local.json`、`projects`、`history.jsonl`、`file-history`、`sessions`、`session-env`、`shell-snapshots`、`todos`（不区分大小写）。`shared.items` 里出现其中任何一项，配置都会被拒绝。

**`skills/synced/`。** Claude Code 把从 claude.ai 同步来的技能存放在 `skills/synced/` 下，按组织和账号分桶。账号的 `skills` 是真实目录时，开启共享判为冲突，multi-claude 不移动任何内容。你可以自己把该账号的 `skills/synced/<桶>` 移进共享目录的 `skills/synced/`：不同账号的桶名不同，不会互相覆盖；但此后这个账号同步下来的技能也会写进共享目录。

### 导出与导入设置

```sh
multi-claude export work ~/work-settings.tar.gz     # 在这台机器上导出
multi-claude import ~/work-settings.tar.gz client   # 导入到另一个（已创建的）账号，本机或别的机器都可以
```

`export` 把账号的 `settings.json`、`CLAUDE.md`、`agents`、`commands`、`skills`、`output-styles`，以及 `.claude.json` 里的 MCP 服务器（只取 `mcpServers` 一项）写进一个只有你能读的 `.tar.gz`。登录信息、`.claude.json` 的其它内容、会话、历史和插件缓存一律不包含。是软链的条目（共享条目）会被跳过。`settings.json` 或 MCP 配置里有 `API_KEY`、`Authorization` 这类名字下的值时，`export` 会列出这些名字（不显示值）：请像对待密码一样保管这个文件。

`import` 把这些条目复制到一个已用 `add` 创建的账号，并把 MCP 服务器加进它的 `.claude.json`。已存在且内容不同的条目或 MCP 服务器算冲突：什么都不改，并列出全部冲突。加 `--force` 时，每一项先改名为 `<名称>.multi-claude-bak.<时间>` 再替换。是共享软链的条目永远不会被替换，请先对它取消共享。`--dry-run` 只显示计划。导入 MCP 服务器后，请重启该账号正在运行的 Claude 会话：它们持有自己的 `.claude.json` 副本，可能把改动覆盖掉。

插件的选择记在 `settings.json`（`enabledPlugins`、`extraKnownMarketplaces`）里，会随之一起导入；Claude Code 会在新账号里重新下载插件。

### 让所有账号共用 MCP 服务器、插件和设置

共享软链只能共享文件和目录。所有账号都要有的设置和 MCP 服务器，改为加在每个启动命令的参数里：

```sh
multi-claude args --defaults -- --mcp-config ~/.claude-shared/mcp.json --settings ~/.claude-shared/settings.json
multi-claude env --defaults CLAUDE_CODE_PLUGIN_CACHE_DIR=~/.claude-shared/plugins
```

- `--settings` 与每个账号自己的 `settings.json` 合并：共享文件里有的键以它为准，其它键保留。`enabledPlugins`、`extraKnownMarketplaces`、`permissions`、`env` 都可以放在这里。
- `--mcp-config` 从 JSON 文件（`{"mcpServers": {...}}`）加载 MCP 服务器，与每个账号自己的服务器一起生效。它接受多个值，所以后面要再跟一个选项（如上面的 `--settings`）；否则 multi-claude 会提示。
- `CLAUDE_CODE_PLUGIN_CACHE_DIR` 改变插件目录（插件市场与插件缓存），插件只需为所有账号下载一次。

### 按目录选账号

```sh
multi-claude route ~/work work           # ~/work 下的任何目录都用 claude-work
multi-claude route ~/work/client-a ca    # 多条规则命中时取最长的目录
multi-claude route --default main        # 可选：没有规则命中时使用
claude-auto                              # 用当前目录对应的账号启动 Claude Code
multi-claude which                       # 只显示会选哪个账号，不启动
```

规则存在 `config.json` 里，并生成 `~/.local/bin/claude-auto`。比较的是物理路径（软链会被解析），所以 `~/work` 不会匹配 `~/workshop`；规则目录不存在时该规则被忽略。没有规则命中、也没设默认账号时，`claude-auto` 直接运行 `claude`。参数原样传递：`claude-auto -p "hello"`。

由于 `claude-auto` 本身是一个启动命令名，有规则时不能有名为 `auto` 的账号；账号仍被规则引用时也不能删除（都判为冲突，退出码 3）。`claude-auto` 不改动你的 shell 配置；想让裸 `claude` 也按规则选账号，可以自己加 `alias claude=claude-auto`。

降级到不支持路由的版本时，配置仍能读取，但下一次写命令会丢掉 `routes`，`claude-auto` 也会留在原处；降级前请先删除规则（或手工删除 `claude-auto`）。

### 用量

```sh
multi-claude usage
```

```text
NAME   5H         7D       UPDATED
work   23% 14:00  41% Fri  12m ago
home   -          -        no data (start claude-home once)
```

数值是 Claude Code 自己缓存在该账号 `.claude.json`（default 身份为 `~/.claude.json`）里的最近一次结果。multi-claude 不读凭据、不联网，所以数据可能已经过时：`UPDATED` 显示数据年龄，超过一小时标注 `stale`。这份缓存由 Claude Code 自己写入，何时刷新未见官方说明。已过重置时间的窗口显示 `reset`。

想要更新的数值，可以让状态栏顺带记录。Claude Code 每次重绘状态栏时，会把 5 小时与 7 天用量交给 `statusLine` 命令（仅 Pro / Max，且会话收到第一次回复之后才有）。包装实际生效的那份设置文件里的 `statusLine` 命令：

```sh
multi-claude statusline install ~/.claude/settings.json
```

命令会变成 `<multi-claude 路径> statusline-hook '<原命令>'`。钩子按 `CLAUDE_CONFIG_DIR` 认出当前账号，把用量存进 `~/.config/multi-claude/usage/`，再用 `/bin/sh -c` 执行原命令，状态栏的显示与行为不变。只保存百分比与重置时间，状态栏数据里的其它内容不落盘。之后 `usage` 取缓存与状态栏两者中较新的一份，后者标注 `(statusline)`；`--json` 有 `source` 字段。

- 只能包装已有 `"type": "command"` 状态栏的文件；每次改动前在同目录备份（`*.multi-claude-bak.*`），只改 `statusLine.command`。文件是软链时改它指向的文件。
- 已经在运行的 Claude 会话仍用旧的状态栏；`install` 之后新开的会话收到第一次回复后，才开始记录用量。
- 多个设置文件都定义了 `statusLine` 时，包装生效的那份（例如启动命令固定参数里用 `--settings` 传入的文件）。
- 卸载 multi-claude 前先运行 `multi-claude statusline uninstall 文件`，否则包装命令找不到可执行文件，状态栏会变空。multi-claude 换了位置后再运行一次 `install` 即可更新路径。

#### token 用量历史

```sh
multi-claude usage --history                   # 所有账号最近 7 天每天的 token 用量
multi-claude usage --history work --days 30 --by model
```

```text
work:
  DATE        INPUT   OUTPUT  CACHE READ  CACHE WRITE  REPLIES
  2026-10-01  1,246  499,681  273,254,361   5,926,527      612
```

`--history` 统计本机上该账号会话记录（`projects/**/*.jsonl`，含子代理）里的回复：按天（本地时间）或按模型汇总输入、输出、缓存读、缓存写 token。只读这几项计数，对话里的其它内容既不输出也不保存，也不发往任何地方。其它机器上的会话和已删除的会话不计入。用 `handoff` 复制过的会话在两个账号里都会出现，`TOTAL` 一段里每条回复只算一次。`--json` 输出同样的数字，供脚本使用。

`list` 会显示每个账号最近一次使用的时间（`LAST USED`），取自账号目录里最新的会话文件或输入历史；`handoff` 复制到某个账号也算一次使用。

### 把会话交给另一个账号

每个账号的会话各自存放，在一个账号里 `claude --resume` 看不到另一个账号的会话。`handoff` 把一条会话（`.jsonl` 文件以及旁边存放子代理记录和工具输出的同名目录）复制到另一个账号的同一项目目录下：

```sh
# 在账号 work 的 Claude 会话里：
! multi-claude handoff home
# [multi-claude] continue with: cd '/path/to/project' && claude-home --resume 0f3c…
```

- 在 Claude 会话里用 `!` 运行时，复制的是当前账号的当前会话；在普通终端里需要用 `--from` 指定账号，复制当前目录里最新的一条会话。`--session ID` 可指定别的会话。
- 不移动、不删除任何东西，来源账号只读；还在写入的半行不会复制。来源账号之后的新消息不会同步过去。
- 目标里已有内容不同的同一会话时，`handoff` 以退出码 3 停下；加 `--force` 会先把已有的那份改名为 `*.multi-claude-bak.*` 再复制，之前请先关掉目标账号里的这条会话。
- 不复制 `/rewind` 用的快照（`file-history`）和项目记忆。

### 改名与管理 MCP

```sh
multi-claude rename work client-a          # claude-work 变成 claude-client-a；~/.cc/work 不变
multi-claude mcp client-a add --scope user github -- npx -y @modelcontextprotocol/server-github
multi-claude mcp client-a list
```

`rename` 保留账号目录，登录不受影响，路由规则会跟着改名。你自己的别名或脚本里用到旧启动命令名的，需要自行更新。存在改过名的账号时不要降级后执行写命令：旧版本会报冲突，不会改写启动命令。

`mcp` 用该账号的配置目录、代理和额外环境变量运行 `claude mcp ...`，但不带它的固定参数（否则 `--allowedTools` 这类选项会把 `mcp` 子命令吞掉）。服务器配置由 Claude Code 自己写入。希望该账号所有项目都能用的服务器，请加 `--scope user`。

### 以账号身份运行其它命令

```sh
multi-claude run work -- claude --version    # 任意命令，环境与 claude-work 完全相同
multi-claude run work                        # 等同于 claude-work
multi-claude run -- env                      # 不给账号名：用 claude-auto 在这里会选的账号
cd "$(multi-claude path work)"               # 账号目录
multi-claude add client-b --config-from work # 新账号 client-b 复制一份 work 的 settings.json
```

`run` 设置与启动命令相同的 `CLAUDE_CONFIG_DIR`、代理和额外环境变量；第一个 `--` 之后的内容原样运行，退出码与该命令相同。不给命令时运行 `claude`，带上账号的固定参数。不给账号名时按 `claude-auto` 的路由选账号，并在 stderr 上注明选了哪个；没有匹配的路由也没有默认账号时，要求给出账号名。账号目录不存在时 `path` 以 1 退出。

`--config-from` 只复制一次 `settings.json`，之后两份互不影响。新账号里已有内容不同的 `settings.json`，或者这个账号共享了 `settings.json`，都算冲突，不做任何修改。希望所有账号保持一致的设置，请用[共享资源](#共享资源)。

### 按账号打开 VS Code（实验功能）

```sh
multi-claude code work ~/projects/app             # 以 work 账号打开一个独立的 VS Code 窗口
multi-claude code personal . -- --disable-gpu     # -- 之后的参数交给 VS Code
```

VS Code 里的 Claude Code 扩展只认 VS Code 启动时环境中的 `CLAUDE_CONFIG_DIR`，所以从 Dock 打开的 VS Code 总是用 `~/.claude`。`code` 以账号的环境（与 `run` 相同）和独立的用户数据目录 `<root>/.apps/<账号目录名>/vscode` 启动 VS Code。用户数据目录必须分开：否则 VS Code 会把请求转给已经在运行的实例，用的是那个实例的环境。每个账号的实例第一次打开时是 VS Code 的默认设置；扩展装在 `~/.vscode/extensions`，各实例共用。

注意：

- 该账号的实例已在运行时，请求会转给它；之后修改的代理或环境变量，要等它退出后才生效。
- 在这个实例里把扩展设置 `claudeCode.environmentVariables` 中的 `CLAUDE_CONFIG_DIR` 设成别的值，会覆盖账号。
- macOS 上 `code` 命令把整个环境交给 `open --env`，这些值会短暂出现在进程列表里。
- 已在 macOS 上用 VS Code 1.139.1 和 Claude Code 扩展 2.1.286 验证；它依赖未公开的行为，运行时会打印提示。需要 `code` 命令在 `PATH` 上（在 VS Code 里运行 “Shell Command: Install 'code' command in PATH”）。

### 诊断与补全

`multi-claude doctor` 只读检查：配置与未完成的迁移；`claude` 和启动命令目录是否在 `PATH` 中；每个启动命令是否最新、有没有被同名文件遮住；账号目录是否存在、权限是否为 `0700`；默认账号的软链；断开的共享软链；是否有登录；以及 `ANTHROPIC_API_KEY` 这类会覆盖账号登录的变量。每个问题都附修复命令。有错误时退出码为 1；`--json` 输出供脚本使用。

```sh
multi-claude completion bash > ~/.local/share/bash-completion/completions/multi-claude
multi-claude completion zsh > "${fpath[1]}/_multi-claude"   # 或 fpath 中任意目录
multi-claude completion fish > ~/.config/fish/completions/multi-claude.fish
```

## 迁移 `~/.claude`

默认账号由三部分组成，Claude Code 只有在**未设置** `CLAUDE_CONFIG_DIR` 时才会找到它们：目录 `~/.claude`、文件 `~/.claude.json`，以及（macOS 上）钥匙串条目 `Claude Code-credentials`。macOS 上其它账号的钥匙串条目与 `CLAUDE_CONFIG_DIR` 的路径字符串一一对应，所以不能简单地让默认账号改用新路径。

因此 `multi-claude migrate-default main` 会（不给名称时，用 `~/.claude.json` 里登录账号的邮箱作为名称）：

1. 把 `~/.claude` 移到 `~/.cc/main`（同一文件系统上直接改名；否则复制、逐个文件校验 SHA-256，再把原目录改名为 `~/.claude.multi-claude-bak.<时间戳>`）；
2. 在原处留下软链 `~/.claude -> ~/.cc/main`；
3. 以 *default 身份* 登记 `main`：它的启动命令**不**设置 `CLAUDE_CONFIG_DIR`，所以 `claude-main` 与直接运行的 `claude` 使用同一个目录、同一个 `~/.claude.json` 和同一个登录，与迁移前一致。

`~/.claude.json` 留在原处，所以默认账号的数据分在两个地方，`list` 会注明位置。这个账号只能用 `claude` 或 `claude-main` 启动：`CLAUDE_CONFIG_DIR=~/.cc/main claude` 会去找另一个登录和另一个 `.claude.json`，表现得像一个全新账号。

**迁移前请关闭所有账号的全部 Claude Code 会话。** 不论 `CLAUDE_CONFIG_DIR` 是什么，所有账号都会写 `~/.claude`（IDE 锁、bridge 和状态文件）。请在普通终端里运行，不要在 Claude Code 会话里运行。发现以下情况时 multi-claude 拒绝开始（退出码 4）：

- 有进程在 `~/.claude` 内打开文件、以它为工作目录或从中执行程序；
- 有在你的家目录下运行的 Claude Code 进程，包括后台会话（报错会按每个配置目录给出对应的 `claude daemon stop --any` 命令）；
- 有进程的 `CLAUDE_CONFIG_DIR` 指向 `~/.claude`；
- `~/.claude/ide/` 中有仍然存活的 IDE 扩展锁；
- 安装了后台服务：macOS 上是 `~/Library/LaunchAgents/com.anthropic.claude-daemon.plist`，Linux 上是 `${XDG_CONFIG_HOME:-~/.config}/systemd/user/com.anthropic.claude-daemon.service`（先运行 `claude daemon uninstall`，迁移完成后可再安装）；
- 本工具自身环境里有 `CLAUDE_CODE_CHILD_SESSION`，说明它运行在某个 Claude Code 会话中。如果确认不是（这个变量可能经 screen、tmux 或由 Claude Code 启动的程序继承下来），可以用 `env -u CLAUDE_CODE_CHILD_SESSION multi-claude ...`。

同时请退出 Claude 桌面应用。macOS 上 Apple 自带程序（例如系统自带的 shell）不向其它进程公开环境变量，所以在这类 shell 里导出的 `CLAUDE_CONFIG_DIR` 检测不到；迁移时会输出有多少个进程无法检查。`--skip-process-check` 跳过以上全部检查，风险自负。但它不跳过一项预检：运行 multi-claude 的 shell 里 `CLAUDE_CONFIG_DIR` 指向 `~/.claude` 时，请先 `unset CLAUDE_CONFIG_DIR`。

进度记录在 `~/.config/multi-claude/migrate-journal.json`。迁移中断后重跑同一条命令，会从磁盘上的实际状态继续。迁移未完成期间，其它写命令拒绝执行。

复制模式下不复制 socket 和 FIFO。macOS 上复制模式不保留扩展属性。

### 手工撤销迁移

`multi-claude restore main` 可以撤销迁移：删除软链，把 `~/.cc/main` 移回 `~/.claude`，并注销这个账号（删除它的启动命令；目录里的共享软链保留）。它和 `migrate-default` 一样检查是否有 Claude Code 会话在运行（`--skip-process-check` 跳过），中断后可以重跑。路由仍在使用这个账号、`~/.claude` 指向别处、或两个目录不在同一文件系统时，它拒绝执行，不做任何修改。手工做的话，步骤如下：

```sh
rm ~/.claude                     # 删除软链（只删软链）
mv ~/.cc/main ~/.claude          # 把数据移回来
multi-claude remove main         # 注销账号
```

其它都不用改：登录和 `~/.claude.json` 从未被动过。复制模式下用了 `--keep-backup` 时，原目录保留在 `~/.claude.multi-claude-bak.<时间戳>`。

迁移报错停下、想放弃时：报错信息会写明完整数据在哪里，把它移回 `~/.claude`，再删除 `~/.config/multi-claude/migrate-journal.json`。

## 常见问题

### 怎么升级 multi-claude？

用 pipx 安装的，运行 `pipx upgrade multi-claude-cli`；用 Homebrew 安装的，运行 `brew upgrade multi-claude`；否则重新运行安装脚本：`curl -fsSL https://raw.githubusercontent.com/jakoes-wu/multi-claude/main/install.sh | sh`，或在更新后的克隆目录里运行 `./install.sh`。它只替换工具本身，配置、账号和启动命令都保留。新版本改变了启动命令的内容时，`multi-claude doctor` 会报告启动命令已过期，运行 `multi-claude apply` 即可重写。

### 直接运行 `claude` 用的是哪个账号？

和以前一样，用 `~/.claude`：multi-claude 不会改变它。[迁移 `~/.claude`](#迁移-claude) 之后就是迁移出来的那个账号。想按目录自动选账号，用 `claude-auto`，见[按目录选账号](#按目录选账号)。

没有“切换直接运行 `claude` 所用账号”的命令。除了 `~/.claude`，直接运行的 `claude` 还会读钥匙串里的登录（macOS）和 `~/.claude.json`，这两样都不会跟着切换，结果是两个账号的东西混在一起。请用各账号的启动命令（如 `claude-work`），或者 `multi-claude route --default work` 后运行 `claude-auto`。

### 怎么把对话交给另一个账号继续？

在 Claude 会话里运行 `! multi-claude handoff 另一个账号`，再在新终端里运行它打印出的命令。见[把会话交给另一个账号](#把会话交给另一个账号)。

### 为什么 `usage` 显示的数字是旧的？

默认情况下，数字来自 Claude Code 写在 `.claude.json` 里的缓存，可能已经过了好几个小时。运行 `multi-claude statusline install 文件` 后，每个新会话都会从状态栏记录最新的数字，见[用量](#用量)。

### 怎么让所有账号共用 skills 和 `CLAUDE.md`？

`multi-claude set 名称 --shared` 会从 `~/.claude-shared`（或你设置的共享目录）链接过来。见[共享资源](#共享资源)。

### multi-claude 会读取或复制我的登录信息吗？

不会读取：唯一的钥匙串操作只查询条目是否存在，也从不打开 `.credentials.json`。它不导出、不上传、不备份登录信息。`migrate-default` 和 `restore` 会整体搬动账号目录；Linux 上凭据文件就在目录里，会随目录一起移动（复制模式下随整个目录一起复制、校验，然后删除原目录，除非加了 `--keep-backup`），内容同样不会被打开。macOS 上登录与账号目录的路径绑定，所以 `rename` 不会改动目录。

## 参考

### 命令

| 命令 | 作用 |
| ---- | ---- |
| `multi-claude init [--root DIR] [--bin-dir DIR] [--shared-dir DIR] [--shared-items A,B]` | 创建或修改全局设置 |
| `multi-claude migrate-default [名称] [--copy] [--keep-backup] [--proxy P] [--skip-process-check]` | 把 `~/.claude` 变成一个账号；不给名称时用其登录邮箱 |
| `multi-claude restore 名称 [--skip-process-check] [--dry-run]` | 撤销 `migrate-default`：把账号移回 `~/.claude` 并注销，登录保持不变 |
| `multi-claude export 名称 文件` | 把账号的设置、`CLAUDE.md`、agents、commands、skills、output styles 和 MCP 服务器写进文件；不含登录与会话 |
| `multi-claude import 文件 名称 [--force] [--dry-run]` | 把这样的文件导入已有账号；有冲突时拒绝，除非加 `--force`（会先备份） |
| `multi-claude add 名称 [--proxy P] [--shared [目录] \| --no-shared] [--adopt] [--shared-exclude 项] [--shared-include 项] [--config-from 其它账号]` | 新增账号、登记已有目录或修改其选项；`--config-from` 从另一个账号复制一次 `settings.json`；`--shared 目录` 同时把它设为所有账号的共享目录（默认 `~/.claude-shared`）；`--shared-exclude` 让这个账号不共享某一项，`--shared-include` 撤销 |
| `multi-claude set 名称 [--proxy P] [--shared [目录] \| --no-shared] [--shared-exclude 项] [--shared-include 项]` | 修改已有账号，选项同 `add`（没有 `--adopt`）；账号未登记时返回 1 |
| `multi-claude proxy 名称 端口\|URL\|off\|inherit` | 设置账号代理 |
| `multi-claude env (名称 \| --defaults) [K=V ...] [--unset K ...]` | 设置或删除额外环境变量 |
| `multi-claude args (名称 \| --defaults) -- [参数 ...]` | 整体替换固定参数；`--` 后面为空表示清空 |
| `multi-claude remove 名称` | 注销账号并删除其启动命令。**账号目录和登录都会保留** |
| `multi-claude apply [-f 文件]` | 按配置（或 `文件`）收敛全部账号 |
| `multi-claude list [--verbose \| --json \| --names]` | 列出账号、启动命令和登录状态；`--json` 额外包含用量，供脚本使用；`--names` 只输出名称 |
| `multi-claude usage [名称] [--json]` | 显示各账号最近一次已知的 5 小时 / 7 天用量 |
| `multi-claude usage --history [名称] [--days N] [--by day\|model] [--json]` | 按天或按模型统计本机会话记录里的 token 用量（默认 7 天） |
| `multi-claude doctor [--json] [--verbose]` | 检查配置、启动命令、账号目录、共享软链、登录与环境变量 |
| `multi-claude route 目录 名称`、`route 目录 --remove`、`route --default 名称`、`route --no-default` | 为 `claude-auto` 设置按目录选账号的规则 |
| `multi-claude which [目录]` | 显示 `claude-auto` 在该目录（默认当前目录）会用哪个账号 |
| `multi-claude rename 旧名 新名` | 给账号及其启动命令改名，目录与登录不变 |
| `multi-claude login 名称 [参数 ...]` | 登录该账号（以它的身份运行 `claude auth login 参数 ...`；账号名形如邮箱时会用 `--email` 预填） |
| `multi-claude mcp 名称 [参数 ...]` | 以该账号的身份运行 `claude mcp 参数 ...` |
| `multi-claude run [名称] [-- 命令 ...]` | 以启动命令的环境运行命令（默认运行带固定参数的 `claude`）；不给名称时用 `claude-auto` 会选的账号 |
| `multi-claude path 名称` | 打印账号目录 |
| `multi-claude code 名称 [路径] [-- 参数 ...]` | 以账号环境打开一个独立的 VS Code 实例（实验功能） |
| `multi-claude handoff 目标 [--from 名称] [--session ID] [--force]` | 把一条会话复制到另一个账号，并打印在那边续聊的命令 |
| `multi-claude statusline install\|uninstall 文件` | 包装设置文件里的 statusLine 命令，让 `usage` 拿到更新的数值；或还原它 |
| `multi-claude completion bash\|zsh\|fish` | 输出 shell 补全脚本 |

所有写命令都支持 `--dry-run`，并且只输出有变化的项；加 `--verbose` 会同时列出已是最新的项。不带参数运行 `multi-claude` 会显示上手说明或账号列表。`env`、`args`、`proxy` 和 `usage 名称` 对未登记的账号返回 1。

### 默认位置

| 项目 | 默认值 |
| ---- | ---- |
| 账号目录 | `~/.cc/<名称>` |
| 启动命令 | `~/.local/bin/claude-<名称>` |
| 配置文件 | `~/.config/multi-claude/config.json`（设置了 `XDG_CONFIG_HOME` 时以它为准） |

账号名可以包含字母、数字和 `._@+-`，必须以字母或数字开头，不区分大小写（`Work` 与 `work` 是同一个账号），可以直接用邮箱。根目录下以 `.` 开头的条目不会被当成账号。

### 用 `apply` 声明式配置

参见 [`examples/config.example.json`](examples/config.example.json)：

```json
{
  "version": 1,
  "root": "~/.cc",
  "bin_dir": "~/.local/bin",
  "shared": {"dir": "~/.claude-shared", "items": ["agents", "commands", "skills", "CLAUDE.md"]},
  "defaults": {"env": {}, "args": []},
  "accounts": {
    "main": {"identity": "default", "proxy": "inherit"},
    "work": {"proxy": "http://127.0.0.1:7901", "shared": true}
  }
}
```

```sh
multi-claude apply -f accounts.json
# 或在新机器上一步完成：
./install.sh --config accounts.json
```

`apply -f` 用文件内容替换整个配置。文件中没有的账号会被注销（目录保留）。只要有冲突，就什么都不写。

`identity` 是工具内部状态，只能由 `migrate-default` 改变。已登记账号在文件里写的 `identity` 一律忽略。尚未登记、但写了 `"identity": "default"` 的账号会被跳过并给出提示：请在那台机器上运行 `multi-claude migrate-default <名称>`，再重新设置它的代理、环境变量、参数和共享。引用该账号的路由（含默认账号）也会一并跳过并逐条提示，之后用 `multi-claude route` 重新设置。

### 登录与账号路径

macOS 上，用 `add` 建立的账号，其登录存放在钥匙串里、名称由账号路径计算得出（`list --verbose` 可以看到）。所以 multi-claude 从不改变已登记账号的路径：

- 已有账号时修改根目录：冲突；
- `rename 旧名 新名` 只改账号名与启动命令名，目录不变，所以登录不受影响（`list --verbose` 显示实际目录）；只改大小写的改名不支持，账号名也不能等于另一个账号的目录；
- 已有启动命令里的账号路径会被改成另一个值（例如配置目录和启动命令目录写的是绝对路径，而 `HOME` 变了）：冲突；
- 在大小写不敏感的文件系统上，磁盘上的目录是 `Work`，却执行 `add work`：冲突，请用 `Work` 登记。

用 `add` 登记已有目录时，multi-claude 会只读地检查这个路径下是否有登录记录，没有时给出警告。如果以前用另一种路径写法（例如经过软链）登录过，请保持同一写法。

#### 手工搬动账号目录

自己搬动账号目录后，它的启动命令会报“账号目录不存在”。macOS 上需要在新路径重新 `/login`。旧的钥匙串条目可以用 `security delete-generic-password -s 'Claude Code-credentials-<旧后缀>'` 删除（搬动前用 `list --verbose` 记下名称）。Linux 上登录是目录里的 `.credentials.json`，会随目录一起移动。

### 退出码

| 退出码 | 含义 |
| ---- | ---- |
| 0 | 成功，或已是目标状态 |
| 1 | 运行错误（读写失败、配置文件不合法、校验失败、另一条命令持有锁、账号未登记） |
| 2 | 命令行参数不合法 |
| 3 | 与不归 multi-claude 管理的文件冲突，或改动会让登录失效；未做任何修改 |
| 4 | 要迁移或恢复的目录正被占用 |

## 卸载

```sh
./install.sh --uninstall
```

只删除工具本身。配置、账号目录（包括 `code` 在 `<root>/.apps` 下建的 VS Code 数据）、登录和 `claude-<名称>` 启动命令都会保留；启动命令不依赖 multi-claude，可以继续使用。包装过状态栏的，先运行 `multi-claude statusline uninstall 文件`。

## 参与贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。运行测试：

```sh
python3 -m unittest discover -s tests -t tests
```

## 许可证

[MIT](LICENSE)
