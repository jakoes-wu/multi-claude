# multi-claude

[English](README.md) | **简体中文**

在一台机器上同时使用多个 [Claude Code](https://code.claude.com/docs) 账号。

Claude Code 把设置、登录和历史记录放在 `CLAUDE_CONFIG_DIR` 指定的目录里（默认 `~/.claude`），官方文档给出的多账号做法就是“每个账号一个目录”。multi-claude 替你管理这些目录，并为每个账号生成独立的启动命令，可以分别设置代理、额外环境变量和固定参数：

```text
claude-work       -> CLAUDE_CONFIG_DIR=~/.cc/work      HTTPS_PROXY=http://127.0.0.1:7901
claude-personal   -> CLAUDE_CONFIG_DIR=~/.cc/personal  （沿用当前 shell 的代理设置）
claude-main       -> 原来的 ~/.claude 账号，登录不失效
claude            -> 行为不变
```

## 功能

- **迁移默认账号**：把已有的 `~/.claude` 移进账号根目录，登录不失效，直接运行 `claude` 的行为与之前完全一致。
- **新增账号**：创建账号目录和 `claude-<名称>` 启动命令，或把已有目录登记为账号。
- **每个账号单独设置代理**：本地端口、HTTP(S) 代理地址、`off` 或 `inherit`。
- **额外环境变量与固定参数**：全局默认值加账号级设置，例如 `--settings`、`--permission-mode`。
- **一键部署**：`install.sh --config accounts.json` 装好工具并创建文件里的全部账号。
- **幂等**：所有命令都可以放心重复执行。已是目标状态的报 `unchanged`；与不归 multi-claude 管理的文件冲突时只报告、不做任何修改；迁移中断后重跑会从实际状态继续。
- **可选的共享资源**：把一个共享目录里的 `CLAUDE.md`、`skills`、`agents`、`commands` 软链到指定账号。
- **不碰凭据**：multi-claude 从不读取、复制或删除登录信息，唯一的钥匙串操作只查询条目是否存在。

## 运行要求

- macOS 或 Linux（Windows 在计划中；WSL 内按 Linux 使用）
- Python 3.8 及以上（只用标准库）
- `PATH` 中有 Claude Code（`claude`）

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

也可以用 `pipx install git+https://github.com/jakoes-wu/multi-claude` 安装。

`./install.sh --help` 列出全部选项。

## 快速开始

```sh
# 把已有的 ~/.claude 变成名为 main 的账号（请在普通终端里运行，见下文）
multi-claude migrate-default main

# 新增一个走本地 7901 端口代理的账号
multi-claude add work --proxy 7901

# 启动它，用 /login 登录一次
claude-work

multi-claude list
```

## 命令

| 命令 | 作用 |
| ---- | ---- |
| `multi-claude init [--root DIR] [--bin-dir DIR] [--shared-dir DIR] [--shared-items A,B]` | 创建或修改全局设置 |
| `multi-claude migrate-default 名称 [--copy] [--keep-backup] [--proxy P] [--skip-process-check]` | 把 `~/.claude` 变成一个账号 |
| `multi-claude add 名称 [--proxy P] [--shared \| --no-shared] [--adopt]` | 新增账号、登记已有目录或修改其选项 |
| `multi-claude proxy 名称 端口\|URL\|off\|inherit` | 设置账号代理 |
| `multi-claude env (名称 \| --defaults) [K=V ...] [--unset K ...]` | 设置或删除额外环境变量 |
| `multi-claude args (名称 \| --defaults) -- [参数 ...]` | 整体替换固定参数；`--` 后面为空表示清空 |
| `multi-claude remove 名称` | 注销账号并删除其启动命令。**账号目录和登录都会保留** |
| `multi-claude apply [-f 文件]` | 按配置（或 `文件`）收敛全部账号 |
| `multi-claude list [--verbose \| --json \| --names]` | 列出账号、启动命令和登录状态；`--json` 额外包含用量，供脚本使用；`--names` 只输出名称 |
| `multi-claude usage [名称] [--json]` | 显示各账号最近一次已知的 5 小时 / 7 天用量 |
| `multi-claude doctor [--json] [--verbose]` | 检查配置、启动命令、账号目录、共享软链、登录与环境变量 |
| `multi-claude route 目录 名称`、`route 目录 --remove`、`route --default 名称`、`route --no-default` | 为 `claude-auto` 设置按目录选账号的规则 |
| `multi-claude which [目录]` | 显示 `claude-auto` 在该目录（默认当前目录）会用哪个账号 |
| `multi-claude completion bash\|zsh\|fish` | 输出 shell 补全脚本 |

所有写命令都支持 `--dry-run`。`env`、`args`、`proxy` 和 `usage 名称` 对未登记的账号返回 1。

### 默认位置

| 项目 | 默认值 |
| ---- | ---- |
| 账号目录 | `~/.cc/<名称>` |
| 启动命令 | `~/.local/bin/claude-<名称>` |
| 配置文件 | `~/.config/multi-claude/config.json`（设置了 `XDG_CONFIG_HOME` 时以它为准） |

账号名可以包含字母、数字和 `._@+-`，必须以字母或数字开头，不区分大小写（`Work` 与 `work` 是同一个账号），可以直接用邮箱。根目录下以 `.` 开头的条目不会被当成账号。

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

### 诊断与补全

`multi-claude doctor` 只读检查：配置与未完成的迁移；`claude` 和启动命令目录是否在 `PATH` 中；每个启动命令是否最新、有没有被同名文件遮住；账号目录是否存在、权限是否为 `0700`；默认账号的软链；断开的共享软链；是否有登录；以及 `ANTHROPIC_API_KEY` 这类会覆盖账号登录的变量。每个问题都附修复命令。有错误时退出码为 1；`--json` 输出供脚本使用。

```sh
multi-claude completion bash > ~/.local/share/bash-completion/completions/multi-claude
multi-claude completion zsh > "${fpath[1]}/_multi-claude"   # 或 fpath 中任意目录
multi-claude completion fish > ~/.config/fish/completions/multi-claude.fish
```

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

## 迁移 `~/.claude`

默认账号由三部分组成，Claude Code 只有在**未设置** `CLAUDE_CONFIG_DIR` 时才会找到它们：目录 `~/.claude`、文件 `~/.claude.json`，以及（macOS 上）钥匙串条目 `Claude Code-credentials`。macOS 上其它账号的钥匙串条目与 `CLAUDE_CONFIG_DIR` 的路径字符串一一对应，所以不能简单地让默认账号改用新路径。

因此 `multi-claude migrate-default main` 会：

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

```sh
rm ~/.claude                     # 删除软链（只删软链）
mv ~/.cc/main ~/.claude          # 把数据移回来
multi-claude remove main         # 注销账号
```

其它都不用改：登录和 `~/.claude.json` 从未被动过。复制模式下用了 `--keep-backup` 时，原目录保留在 `~/.claude.multi-claude-bak.<时间戳>`。

迁移报错停下、想放弃时：报错信息会写明完整数据在哪里，把它移回 `~/.claude`，再删除 `~/.config/multi-claude/migrate-journal.json`。

## 登录与账号路径

macOS 上，用 `add` 建立的账号，其登录存放在钥匙串里、名称由账号路径计算得出（`list --verbose` 可以看到）。所以 multi-claude 从不改变已登记账号的路径：

- 已有账号时修改根目录：冲突；
- 账号改名（哪怕只改大小写）：冲突；
- 已有启动命令里的账号路径会被改成另一个值（例如配置目录和启动命令目录写的是绝对路径，而 `HOME` 变了）：冲突；
- 在大小写不敏感的文件系统上，磁盘上的目录是 `Work`，却执行 `add work`：冲突，请用 `Work` 登记。

用 `add` 登记已有目录时，multi-claude 会只读地检查这个路径下是否有登录记录，没有时给出警告。如果以前用另一种路径写法（例如经过软链）登录过，请保持同一写法。

### 手工搬动账号目录

自己搬动账号目录后，它的启动命令会报“账号目录不存在”。macOS 上需要在新路径重新 `/login`。旧的钥匙串条目可以用 `security delete-generic-password -s 'Claude Code-credentials-<旧后缀>'` 删除（搬动前用 `list --verbose` 记下名称）。Linux 上登录是目录里的 `.credentials.json`，会随目录一起移动。

## 共享资源

```sh
multi-claude init --shared-dir ~/.claude-shared
multi-claude add work --shared
```

默认条目为 `agents`、`commands`、`skills`、`CLAUDE.md`，只链接 `shared.items` 中列出的条目。multi-claude 只创建缺少的软链，并记住哪些是它建的。关闭共享时只删除这些软链，你自己建的软链保持不动。软链位置上已有真实文件或目录时判为冲突，绝不覆盖。

如果之前已经手工把账号链接到了共享目录，`multi-claude add 名称 --shared --adopt` 会接管这些软链，不重建。

**`skills/synced/`。** Claude Code 把从 claude.ai 同步来的技能存放在 `skills/synced/` 下，按组织和账号分桶。账号的 `skills` 是真实目录时，开启共享判为冲突，multi-claude 不移动任何内容。你可以自己把该账号的 `skills/synced/<桶>` 移进共享目录的 `skills/synced/`：不同账号的桶名不同，不会互相覆盖；但此后这个账号同步下来的技能也会写进共享目录。

## 退出码

| 退出码 | 含义 |
| ---- | ---- |
| 0 | 成功，或已是目标状态 |
| 1 | 运行错误（读写失败、配置文件不合法、校验失败、另一条命令持有锁、账号未登记） |
| 2 | 命令行参数不合法 |
| 3 | 与不归 multi-claude 管理的文件冲突，或改动会让登录失效；未做任何修改 |
| 4 | 迁移源正被占用 |

## 卸载

```sh
./install.sh --uninstall
```

只删除工具本身。配置、账号目录、登录和 `claude-<名称>` 启动命令都会保留；启动命令不依赖 multi-claude，可以继续使用。

## 参与贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。运行测试：

```sh
python3 -m unittest discover -s tests -t tests
```

## 许可证

[MIT](LICENSE)
