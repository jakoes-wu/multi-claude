# multi-claude：Claude Code 多账号管理工具

> 2026-09-30 注记：方案已收敛，用户已同意编码；编码与本机、Linux 测试机回归已完成，独立代码评审结论为可合并（阻塞/高/中为 0），尚未提交。编码中实测补充一条事实（§3 依据 9：macOS 上读不出 Apple 自带程序的环境），已同步到 §5.1.7 第 3 条与 §8 第 12 条。§10 中 npm 形态、Linux supervisor 服务形态与代理实测三项仍未验证（Linux 测试机没有 node 与 claude）。仓库新建，§4 所有条目都是新增。用户决定：启动命令命名 `claude-<名称>`；启动命令不清除 `CLAUDE_CODE_CHILD_SESSION`。
>
> - 第 1 轮独立评审：无高级问题，中级 8 条（D1–D8）、低级 9 条（D9–D17），本版已全部修订。主要修订：占用检查改为拦同一 `HOME` 下的全部 Claude 进程（D1）；写明 settings 的 `env` 覆盖启动命令的代理并告警（D2）；扩充保留键、拒绝凭据类键（D3）；`args` 在 argparse 前手工切分 `--`（D4）；占用检查排除自身、识别调用方 shell、Linux PID 命名空间判为检查失败（D5、D6）；测试安全闸同时断言假 `claude`（D7）；daemon 的停止命令与 LaunchAgent 检测（D8）；沙箱问题按源码关闭（D9）。D6 原建议读 `/proc/1/ns/pid`，Linux 测试机实测普通用户读不了。
> - 第 2 轮复审：D1–D17 中 16 条确认修复；新发现中级 3 条（D18–D20）、低级 7 条（D21–D27），本版已全部修订。D18：第 1 轮改用的 `NSpid` 在 `bwrap --proc /proc` 中只有一个值（Linux 测试机实测确认），检测不到嵌套；改为“本工具环境含 `CLAUDE_CODE_CHILD_SESSION` 即拒绝”（第 0 条）加“`/proc/1/comm` 为 `bwrap` 即检查失败”。D19：凭据键改为显式清单加按下划线分段匹配，放行 `MAX_THINKING_TOKENS` 等。D20：预检直接检查本工具自身环境中的 `CLAUDE_CONFIG_DIR`。
> - 第 3 轮复审：D18–D27 全部确认修复；新发现中级 1 条（D28：Claude 拉起的后台会话进程 argv[0] 不是 `claude`，源码已核实），低级 5 条（D29–D33），本版已全部修订。评审结论为 D28 修完即可进入编码，不必再做整轮复审。
>
> 本项目参照 multi-codex（基线提交 `fb548a8`，v0.2.0）。multi-codex 的方案（`docs/feature/feature-account-manager.md`，经 4 轮设计评审、2 轮代码评审）中与 Claude 无关的设计——收敛模型、互斥锁、迁移事务记录与续跑表、回滚规则、共享链接、安装脚本——本方案原样沿用，文中只写“沿用 multi-codex §x”，不再重复；本文重点写与 Codex 不同的部分。

## 1. 背景

Claude Code 通过环境变量 `CLAUDE_CONFIG_DIR` 决定配置目录，未设置时使用 `~/.claude`。官方文档给出的多账号做法就是“每个账号一个目录，启动时设置 `CLAUDE_CONFIG_DIR`”（authentication 页 “Log in with multiple accounts” 一节）。实际需要手工维护：

- 各账号目录；
- 各账号的启动入口，以及入口上的代理变量、额外环境变量和固定参数；
- 各账号指向公共资源（`CLAUDE.md`、skills 等）的软链；
- 把已有的默认账号（`~/.claude`）纳入统一管理。

与 Codex 相比，Claude Code 的登录凭据（macOS）与配置目录的**路径字符串**绑定，默认账号又由两处文件组成，所以 multi-codex 的“目录改名、原处留软链”不能直接照搬。本项目把这些操作做成命令行工具，以开源形式发布到 GitHub（`jakoes-wu/multi-claude`）。

## 2. 目标 / 非目标

**目标**（与 multi-codex 对等）：

1. 把默认账号（`~/.claude`）迁移为一个具名账号，迁移后登录不失效，直接运行 `claude` 的行为不变。
2. 新增账号，或把已有目录登记为账号；为每个账号生成独立启动命令。
3. 每个账号单独设置代理：`inherit` / `off` / 端口 / URL。
4. 启动命令可以配置额外环境变量和固定参数（全局默认 + 账号级）。
5. 一键部署：`install.sh` 安装工具，带 `--config` 时装完立即 `apply`。
6. 所有写命令幂等并支持 `--dry-run`；可选共享资源，支持 `--adopt` 接管已有软链。

**非目标**：

- 一期不支持 Windows（二期见 §11）。WSL 内按 Linux 使用。
- 不读、不写、不复制、不删除任何凭据：不碰钥匙串条目的内容，不碰 `.credentials.json`。唯一的钥匙串操作是只读的存在性查询（§5.1.9）。
- 不支持改变已登记账号的目录路径（改根目录、改名、搬目录），因为这会让 macOS 上的登录失效（§5.1.8）。
- 不把默认账号转成“自带 `CLAUDE_CONFIG_DIR` 的普通账号”，也不移动 `~/.claude.json`（§5.1.6）。
- 不提供删除账号数据的命令；`remove` 只注销账号、删除启动命令和工具建立的共享链接。
- 不修改 shell 启动文件，不自动导入用户已有的多账号 shell 函数或 `claude` 别名。
- 不提供迁移后的撤销命令，手工回滚步骤写在 README。

## 3. 假设与约束

**运行环境**：与 multi-codex §3 相同（Python ≥ 3.8、只用标准库、macOS 与 Linux）。调用 Claude Code 时以 `PATH` 中的 `claude` 为准。

**默认路径**（都可以通过 `init` 的参数修改）

| 项目 | 路径 |
| ---- | ---- |
| 默认账号目录（迁移源） | `~/.claude`（固定，不可改，原因见 §5.1.6） |
| 账号根目录 | `~/.cc` |
| 启动命令目录 | `~/.local/bin` |
| 工具状态目录 | `~/.config/multi-claude/`（设置了 `XDG_CONFIG_HOME` 时以它为准） |
| 工具安装位置 | `~/.local/share/multi-claude` |

**外部行为依据**（Claude Code 2.1.286；“源码”指 `~/.local/share/claude/versions/2.1.286` 这个 bun 单文件里内嵌的 JS，用 `strings` 提取后检索给出的标识符可以复核）

1. **配置目录**：源码 `we()` = `(process.env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")).normalize("NFC")`。不做 realpath，不去尾部斜杠。
2. **钥匙串服务名**（macOS）：源码 `QN()`（检索 `var Mue="-credentials"`）：
   - 未设置 `CLAUDE_SECURESTORAGE_CONFIG_DIR` 时：`CLAUDE_CONFIG_DIR` 为空或未设置 → 服务名 `Claude Code-credentials`；否则 → `Claude Code-credentials-<h>`，`h` = `sha256(we())` 十六进制的前 8 位；
   - 完整形式为 `Claude Code` + `OAUTH_FILE_SUFFIX` + `-credentials` + 后缀。`OAUTH_FILE_SUFFIX` 来自源码 `F3()`：正式环境为空串；设置了 `CLAUDE_CODE_CUSTOM_OAUTH_URL` 时为 `-custom-oauth`（该变量只接受 FedStart 等白名单地址，见源码 `SIe`）；
   - 钥匙串账户名取 `$USER`，读不到或不匹配 `^[a-zA-Z0-9._-]+$` 时为 `claude-code-user`（源码 `ok()`），读取命令为 `security find-generic-password -a <账户名> -w -s <服务名>`；
   - `CLAUDE_SECURESTORAGE_CONFIG_DIR` 是未写入官方文档的内部变量：设置后改用它的值计算后缀（设为空串则不带后缀）。
   - 开发机上 4 个已有账号目录与钥匙串条目逐一核对，4/4 一致；`~/.claude` 对应不带后缀的条目。
3. **Linux 的凭据**：官方 authentication 页 “Credential management”：Linux 上凭据在 `~/.claude/.credentials.json`（0600）；设置 `CLAUDE_CONFIG_DIR` 时在该目录下。macOS 钥匙串写入失败时也退回到同一文件。源码 `u()` = `join(zS(), ".credentials.json")`，`zS()` 在未设置 `CLAUDE_SECURESTORAGE_CONFIG_DIR` 时等于 `we()`。所以 **Linux 上凭据随目录移动，与路径字符串无关**。
4. **`.claude.json` 的位置**：源码 `bZn()` = `join(process.env.CLAUDE_CONFIG_DIR || homedir(), ".claude" + F3() + ".json")`，正式环境即 `.claude.json`；另有遗留路径 `<配置目录>/.config.json`，存在时优先。即：设置了 `CLAUDE_CONFIG_DIR` → `<配置目录>/.claude.json`；未设置 → `~/.claude.json`。本机 `~/.claude/.config.json` 不存在。
5. **代理**：官方 network-config 页：读 `https_proxy`、`HTTPS_PROXY`、`http_proxy`、`HTTP_PROXY`（按此顺序取第一个），支持 `NO_PROXY`；原文 “Claude Code does not support SOCKS proxies.”；`ALL_PROXY` 未出现在文档中。源码里 Claude 自己调用 git 时会按 `HTTPS_PROXY || https_proxy || ALL_PROXY || all_proxy` 取代理（检索 `remote.origin.proxy`）。
6. **后台会话与代理**：network-config 页 “Apply network settings to background agents”：后台 supervisor 继承第一个启动它的 shell 的环境，官方建议把代理写进 `settings.json` 的 `env`。
7. **`skills/synced/`**：源码 `O5="synced"`、`.bucket-` 标记、`<组织UUID>_<账号UUID>` 命名规则：这是 Claude Code 内置的 claude.ai 同步技能存储，按组织 + 账号分桶，加载时只读当前登录账号那一桶（检索 `"syncedSkills"`）。同级的 `.trash`、`.staging` 也归同步机制所有。本机已共享 `skills` 的三个账号，各自的桶都落在 `~/.claude-shared/skills/synced/` 下（目前两个桶），互不覆盖。
8. **可变参数选项**：`claude --help`（2.1.286）中取可变个值的选项：`--add-dir`、`--allowedTools`/`--allowed-tools`、`--betas`、`--disallowedTools`/`--disallowed-tools`、`--file`、`--mcp-config`、`--tools`。它们会吞掉后面所有不以 `-` 开头的参数。
9. **进程与配置目录**：实测 4 个运行中的 claude 进程，`lsof` 查到它们在账号目录下打开的文件数为 0；但用 `ps -E -ww -p <pid>` 能读到各自的 `CLAUDE_CONFIG_DIR`。所以按路径查占用会漏掉正在运行的 claude，必须读进程环境（§5.1.7）。编码阶段（macOS 27.0.1）实测补充：`sysctl(KERN_PROCARGS2)` 与 `ps -E` 对 Apple 自带程序（`/bin/zsh`、`/bin/sleep` 等）只返回 argv、隐去全部环境变量；对第三方程序（claude、python、node）能读到完整环境。
10. **IDE 扩展**：VS Code 扩展把锁文件写在 `~/.claude/ide/<端口>.lock`，内容含 `pid`、`ideName`、`authToken` 等字段。源码 `Udn()` 显示：即使会话设置了 `CLAUDE_CONFIG_DIR`，也会额外读 `~/.claude/ide`。
11. **其它**：`claude` 本体（`~/.local/bin/claude`）是指向 `~/.local/share/claude/versions/<版本>` 的软链；从终端启动的进程 argv[0] 为 `claude`。Claude 自己拉起的子进程不一定如此：源码 `rke()` 在 `pinToCurrentBinary` 时直接用 `process.execPath`（原生安装即版本文件 `.../claude/versions/<版本>`）；后台会话的终端宿主进程（`Etr()`）以 `--bg-pty-host` 参数启动，并显式设置 `argv0:"claude bg-pty-host"`；Bun 子进程默认把可执行文件路径当作 argv[0]。官方 agent-view 页说明 `claude daemon stop --any --keep-workers` 会让这些后台会话在 supervisor 停掉后继续运行。shell 别名不会在脚本里展开。
12. **所有账号都会写 `$HOME/.claude`**：以下路径直接由 `homedir()` 拼出，与 `CLAUDE_CONFIG_DIR` 无关：`~/.claude/bridge-spawn`（源码 `DVn="bridge-spawn"`，会 mkdir）、`~/.claude/.device-keys.json`、`~/.claude/state/` 下的 `settings-review.json` 等（沙箱构建代码 `Nr=Qe(Tc(),".claude","state")` 与 `we()/state` 分别处理），以及依据 10 的 `~/.claude/ide`。所以**任何账号**的 Claude 进程都可能在迁移期间写 `~/.claude`。
13. **settings 中的 `env` 覆盖 shell 环境**：官方 env-vars 页原文 “When the same variable is set in both your shell and a settings file `env` block, the settings file value applies in most sessions.” 源码（检索 `appliedGlobalConfigEnv`）依次把 `.claude.json` 的 `env`、各级 settings（含 `--settings` 指定的文件）的 `env`、托管设置的 `env` 写入 `process.env`。
14. **后台 supervisor**：官方 agent-view 页原文 “If you set `CLAUDE_CONFIG_DIR`, the supervisor uses that directory instead of `~/.claude` and runs as a separate instance with its own sessions.” 停止命令为 `claude daemon stop --any`（按需启动的实例），已安装成系统服务的实例用 `claude daemon stop`。macOS 上安装形态是 LaunchAgent `~/Library/LaunchAgents/com.anthropic.claude-daemon.plist`（源码 `be="com.anthropic.claude-daemon"`），只带 `PATH` 环境变量，因此总是服务默认账号。
15. **沙箱写保护与软链**：源码 `NE(e)` 返回 `[realpath(e), e]`，沙箱禁止写入列表同时包含真实路径与字面路径，迁移后 `~/.claude` 变成软链不削弱该保护。
16. **嵌套会话**：官方 env-vars 页说明继承了 `CLAUDE_CODE_CHILD_SESSION` 的交互式会话不进入 `--resume`、历史与 `claude agents`。从某个 Claude 会话的 Bash 工具里运行启动命令即属此情形。

**其它约束**：账号名规则、`.` 开头条目不当账号、目录权限 0700、MIT 许可、双语 README，均沿用 multi-codex §3。根目录 `~/.cc` 下已有的 `.claude/`（某次在 `~/.cc` 下运行 claude 留下的项目级设置目录）按“`.` 开头不当账号”处理，工具不碰它。

## 4. 涉及模块

仓库新建，基线为空，下表所有行都是新增。“来源”列指 multi-codex `fb548a8` 中的文件，括号里的行号是该文件中被沿用或扩展的函数位置。

| 区域 | 文件 | 来源 | 改动点 |
| ---- | ---- | ---- | ---- |
| 文件工具 | `src/multi_claude/fsutil.py` | `fsutil.py` 原样复制 | 仅改临时文件前缀 |
| 互斥锁 | `src/multi_claude/lock.py` | `lock.py` 原样复制 | 无 |
| 动作与输出 | `src/multi_claude/actions.py` | `actions.py` 原样复制 | 日志前缀改为 `[multi-claude]` |
| 共享资源 | `src/multi_claude/shared.py` | `shared.py` 原样复制 | 提示文字中的命令名 |
| 配置 | `src/multi_claude/config.py` | `config.py` 扩展 | `Account`（:31）加 `identity`、`env`、`args`；`Config`（:52）加 `defaults`；`parse_config`（:121）/`_parse_account`（:175）校验新字段与保留键（§5.1.5）；`PROXY_SCHEMES`（:24）去掉 socks；`DEFAULT_SHARED_ITEMS`（:17）换成 Claude 条目 |
| 收敛引擎 | `src/multi_claude/accounts.py` | `accounts.py` 扩展 | `plan`（:38）新增大小写核对（§5.1.8）与 default 身份跳过建目录；`_plan_launcher`（:121）新增“路径行变化判冲突”；`launcher_status`（:192）随新模板渲染 |
| 启动命令 | `src/multi_claude/launcher.py` | `launcher.py` 扩展 | `render`（:28）换成 §5.1.4 模板；`_proxy_lines`（:57）不变；新增额外环境变量段、参数段、可变参数告警 |
| 默认账号迁移 | `src/multi_claude/migrate.py` | `migrate.py` 扩展 | `migrate_default` 去掉 `source` 形参、源固定 `~/.claude`；`_config_with_account`（:237）登记为 default 身份；`_check_registration_conflicts`（:225）加“已有 default 账号”；`_busy_check`（:252）换成 §5.1.7；`_warn_environment`（:178）换成 Claude 的变量；`warn_isolation_env`（:186）沿用，读 `platform.ISOLATION_BREAKING_ENV` |
| 平台适配 | `src/multi_claude/platform.py` | `platform.py` 扩展 | `default_source`（:23）返回 `~/.claude`、`default_root`（:27）返回 `~/.cc`；`find_busy_processes`（:72）保留为路径占用检查，新增进程枚举、IDE 锁、daemon 服务检测；`ISOLATION_BREAKING_ENV`（:16）保留，内容换成全局导出即破坏账号隔离的变量：`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`ANTHROPIC_PROFILE`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX`、`CLAUDE_CODE_USE_FOUNDRY` |
| 凭据定位 | `src/multi_claude/identity.py` | 新增 | 计算钥匙串服务名、凭据文件路径；只读探测登录状态 |
| 命令行 | `src/multi_claude/cli.py` | `cli.py` 扩展 | `build_parser`（:24）新增 `env`、`args`，去掉 `migrate-default --source`；`main`（:87）在 argparse 前按 `--` 切分 argv（§5.1.2）；`dispatch`（:140）、`_load_apply_file`（:202）处理新字段；`_warn_if_socks`（:234）删除；`cmd_list`（:247）新增列 |
| 包入口 | `src/multi_claude/__init__.py`、`__main__.py` | 同名文件 | 版本号 0.1.0 |
| 一键安装 | `install.sh` | `install.sh` | 全部 `multi-codex`/`MULTI_CODEX` 改名 |
| 测试 | `tests/test_*.py`、`tests/helpers.py` | 同名文件扩展 | 假 `claude`、假 `security`、测试安全闸（§8） |
| 开源标准件、CI、示例配置 | 与 multi-codex 同名的全部文件 | 同名文件 | 改名；示例配置换成 Claude 字段 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 收敛模型、互斥锁、退出码

沿用 multi-codex §5.1.1 与 §5.2：`config.json` 唯一权威源；写命令“加锁 → 算新配置 → 生成动作 → 有冲突全不写（返回 3）→ 先写配置再执行动作”；`--dry-run` 只打印动作；迁移事务未完成时只放行 `migrate-default`、`list` 和 `--dry-run`。退出码 0/1/2/3/4 含义不变。

#### 5.1.2 命令一览

| 命令 | 作用 | 与 multi-codex 的差异 |
| ---- | ---- | ---- |
| `init [--root] [--bin-dir] [--shared-dir] [--shared-items]` | 全局设置 | 无 |
| `migrate-default <名称> [--copy] [--keep-backup] [--proxy] [--skip-process-check]` | 把 `~/.claude` 迁移成 default 身份的账号 | 去掉 `--source`（§5.1.6）；占用检查见 §5.1.7 |
| `add <名称> [--proxy P] [--shared \| --no-shared] [--adopt]` | 新增或登记 dir 身份的账号 | 登记已有目录时做登录探测（§5.1.9） |
| `proxy <名称> <端口 \| URL \| off \| inherit>` | 设置代理 | 拒绝 socks（§5.1.3） |
| `env (<名称> \| --defaults) [K=V ...] [--unset K ...]` | 设置额外环境变量 | 新增（§5.1.5） |
| `args (<名称> \| --defaults) -- [参数 ...]` | 整体替换固定参数，`--` 后为空表示清空 | 新增（§5.1.5） |
| `remove <名称>` | 注销账号 | 不碰目录、钥匙串、`.credentials.json`、`~/.claude.json` |
| `apply [-f 文件]` | 收敛全部账号 | `-f` 文件中的 `identity` 规则见 §5.1.6 |
| `list` | 只读列出状态 | 新增 LOGIN、LINK 列（§9） |

`env`、`args` 对未登记的账号名返回 1（与 `proxy` 一致）。

**`args` 的解析**：不能用 `argparse.REMAINDER`——实测（Python 3.10/3.11/3.13）可选位置参数加 `--defaults` 加 REMAINDER 时，`args --defaults -- --settings s` 会被解析成账号名 `--settings`。做法：`main` 在调用 argparse 之前，若子命令是 `args`，就在 argv 中找第一个 `--`，把它之后的全部内容取出作为参数列表，只把之前的部分交给 argparse；没有 `--` 时返回 2（要求显式写 `--`，避免把参数误当成账号名）。`env` 不接受以 `-` 开头的 `K=V`，不需要这样处理。

#### 5.1.3 代理

取值、展开、URL 校验与 multi-codex §5.1.3 相同，唯一区别：**协议只允许 `http`、`https`**。`socks5://`、`socks5h://` 在命令行中返回 2，写在配置文件中 `apply` 返回 1，报错引用官方文档原文 “Claude Code does not support SOCKS proxies”（§3 依据 5）。

| 取值 | 启动命令中的行为 |
| ---- | ---- |
| `inherit`（默认） | 不动任何代理变量 |
| `off` | 清除 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY`、`NO_PROXY` 的大小写共 8 个变量 |
| 端口 / URL | 设置 `HTTPS_PROXY`、`HTTP_PROXY` 大小写；清除继承的 `ALL_PROXY` 大小写（Claude 调 git 时会退而使用它，见依据 5）；`NO_PROXY`、`no_proxy` 在原值后追加 `localhost,127.0.0.1,::1` |

代理只作用于从该启动命令启动的进程。

**与 settings 中 `env` 的优先级**：settings 文件（账号的 `settings.json`、`--settings` 指定的文件、`.claude.json`、托管设置）的 `env` 里出现同名代理变量时，以 settings 为准，启动命令设置的值被覆盖（依据 13）。工具不改 settings，只在以下检查发现冲突时告警（stderr，不改退出码），时机为 `add`、`proxy`、`apply`、`list`：

- 检查文件：账号目录下的 `settings.json`、`.claude.json`（default 身份为 `~/.claude.json`），以及生效参数中每个 `--settings <文件>` / `--settings=<文件>` 指向的文件（值以 `{` 开头时按内联 JSON 解析）。托管设置（位置与下发方式由管理员决定）不检查，README 说明；
- 只读取顶层 `env` 对象的键名，不读、不输出值；文件不存在或不是合法 JSON 时跳过（`list` 中注明）；
- 键名（不分大小写）属于 §5.1.3 的 8 个代理变量、且账号代理不是 `inherit` 时告警：“`<文件>` sets `<变量>` in env, which overrides the proxy set by claude-<名称>”。

后台 supervisor 的代理见 §7。

#### 5.1.4 启动命令 `<bin-dir>/claude-<名称>`

**命名建议**：`claude-<名称>`。理由：

- 官方文档的多账号示例就是 `claude-work`；
- 与 multi-codex 的 `codex-<名称>` 对称；
- 与用户可能已有的多账号 shell 函数不同名，可以并存，逐个切换；
- 账号名必须以字母或数字开头，不会生成名为 `claude` 的文件，`exec claude` 不会找回自己。

备选 `cc-<名称>`（短，但 `cc` 是 C 编译器名，含义不清）、`claude.<名称>`（与现有函数命名风格一致，但带点的命令名在部分补全脚本里处理不好）。2026-09-30 用户确认采用 `claude-<名称>`。

**模板**（POSIX sh，0755；所有值用 `shlex.quote`；第 2 行为受管标记 `# managed-by: multi-claude account=<名称>`，受管判定与孤儿清理沿用 multi-codex §5.1.1）：

```sh
#!/bin/sh
# managed-by: multi-claude account=<名称>
# Generated by multi-claude. Do not edit; use `multi-claude` commands.
account_dir='<账号目录绝对路径>'
if [ ! -d "$account_dir" ]; then
  printf 'multi-claude: account directory does not exist: %s\n' "$account_dir" >&2
  exit 1
fi
# ---- 身份段，二选一，见下 ----
# ---- 额外环境变量：K='V'; export K（按键名排序）----
# ---- 代理段：按 §5.1.3 ----
if ! command -v claude >/dev/null 2>&1; then
  echo "multi-claude: claude not found in PATH" >&2
  exit 127
fi
exec claude <固定参数...> "$@"
```

**身份段**

- `dir` 身份（`add` 建立的账号）：

  ```sh
  unset CLAUDE_SECURESTORAGE_CONFIG_DIR
  CLAUDE_CONFIG_DIR="$account_dir"
  export CLAUDE_CONFIG_DIR
  ```

  必须清除继承来的 `CLAUDE_SECURESTORAGE_CONFIG_DIR`：它会改写钥匙串后缀的计算依据（依据 2）。在 Claude 会话的 Bash 工具里运行启动命令时，父进程环境里本来就带着别的账号的 `CLAUDE_CONFIG_DIR`，这里一律覆盖。

- `default` 身份（`migrate-default` 建立的账号）：

  ```sh
  default_dir='<迁移源绝对路径，即 $HOME/.claude 展开后>'
  if [ "$(cd -P -- "$default_dir" 2>/dev/null && pwd -P)" != "$(cd -P -- "$account_dir" && pwd -P)" ]; then
    printf 'multi-claude: %s no longer points to %s; restore the link or run multi-claude migrate-default again\n' "$default_dir" "$account_dir" >&2
    exit 1
  fi
  unset CLAUDE_CONFIG_DIR CLAUDE_SECURESTORAGE_CONFIG_DIR
  ```

  不设 `CLAUDE_CONFIG_DIR`，让 Claude 按默认规则使用 `~/.claude`（即兼容软链）、`~/.claude.json` 和不带后缀的钥匙串条目——与迁移前、与直接运行 `claude` 完全一致。比较用 `cd -P` + `pwd -P` 而不用 `[ -ef ]`，因为 `-ef` 不属于 POSIX，shellcheck 会报 SC3013。

**账号目录路径字符串**：`account_dir` 取 `os.path.join(expand(root), 名称)`，`expand` 只做 `expanduser` + `abspath`，不做 realpath（与 Claude 的 `we()` 一致，依据 1）。这个字符串就是 dir 身份账号的钥匙串绑定依据，§5.1.8 规定它一经生成不得被工具改变。

**固定参数与 `"$@"` 的顺序契约**：`exec claude` 之后依次是：全局默认参数、账号参数、`"$@"`。`"$@"` 永远在最后。可变参数选项的处理见 §5.1.5。

#### 5.1.5 额外环境变量与固定参数

**配置**

```json
"defaults": {"env": {"CLAUDE_CODE_PLUGIN_CACHE_DIR": "~/.claude-shared/plugins"},
             "args": ["--settings", "~/.claude-shared/settings.plugins.json"]},
"accounts": {"work": {"env": {}, "args": [], ...}}
```

- 生效值：环境变量 = `defaults.env` 再用账号 `env` 覆盖同名键；参数 = `defaults.args` + 账号 `args`。
- 值以 `~/` 开头（或恰好是 `~`）时，渲染启动命令时展开成绝对路径；其它情况原样写入（经 `shlex.quote`，不做 `$` 展开）。`--settings=~/x` 这种写法不展开，README 说明。
- 键名必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`。以下键一律拒绝（命令行返回 2，配置文件返回 1），比较时不分大小写：
  - **改变账号身份的键**：`CLAUDE_CONFIG_DIR`、`CLAUDE_SECURESTORAGE_CONFIG_DIR`、`CLAUDE_CODE_CUSTOM_OAUTH_URL`（改变服务名中段与 `.claude.json` 文件名，依据 2、4）、`HOME`（default 身份与依据 12 的全部路径）、`USER`（钥匙串账户名）；
  - **代理变量**：§5.1.3 的 8 个，由 `proxy` 字段管理；
  - **凭据**：显式列出官方 env-vars 页中的凭据变量 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`CLAUDE_CODE_OAUTH_REFRESH_TOKEN`、`ANTHROPIC_FOUNDRY_API_KEY`、`ANTHROPIC_AWS_API_KEY`、`ANTHROPIC_FOUNDRY_AUTH_TOKEN`、`AWS_BEARER_TOKEN_BEDROCK`、`MCP_CLIENT_SECRET`；再加一条按下划线分段的通用规则：键名按 `_` 切分后任一段等于 `TOKEN`、`SECRET`、`PASSWORD`、`PASSPHRASE`，或键名等于 `API_KEY`、以 `_API_KEY` 结尾。分段匹配是为了放行 `MAX_THINKING_TOKENS`、`CLAUDE_CODE_MAX_OUTPUT_TOKENS`、`CLAUDE_CODE_API_KEY_HELPER_TTL_MS` 等非凭据的常用变量。启动命令是 0755 明文文件、`config.json` 会被复制到别的机器，凭据写进去即泄露，与“不碰凭据”的非目标冲突；报错提示改用账号 `settings.json` 的 `env` 或 `apiKeyHelper`。
- 值不允许含换行和 NUL；参数同理。

**命令行**

- `env <名称> K=V ...`：设置或覆盖；`--unset K`：删除；两者可同时出现，先删后设。键不存在时 `--unset` 不报错（幂等）。
- `args <名称> -- a b c`：整体替换；`args <名称> --`：清空。
- `--defaults` 代替名称时作用于全局默认值，变更会让所有账号的启动命令更新。

**可变参数告警**：渲染前扫描生效参数，找出最后一个以 `-` 开头的参数；如果它（去掉 `=值` 部分后）属于依据 8 的可变参数选项，或属于以下“可选值”选项且后面没有跟值，就在 stderr 警告。可选值选项（`claude --help` 2.1.286 中写作 `[value]` 的）：`-d`/`--debug`、`-r`/`--resume`、`-w`/`--worktree`、`--remote-control`、`--from-pr`、`--teleport`、`--prompt-suggestions`、`--cloud`；编码时以 `claude --help` 中所有 `<x...>` 与 `[x]` 形式的选项为准，抄成常量并注明版本。告警文字：“`<选项>` 会把运行 `claude-<名称>` 时传入的提示词或子命令当成自己的值吞掉；请在它后面再放一个选项”。只警告，不改退出码。常见的写法是把 `--settings` 放在 `--allowedTools` 之后，用来截断这个可变参数。

**迁移现有设置的示例**（README 中写成通用示例，不出现个人路径）：

```sh
multi-claude args --defaults -- --ide --permission-mode acceptEdits \
  --allowedTools "Bash(git status),Bash(git diff),Bash(git log:*),Read" \
  --settings ~/.claude-shared/settings.plugins.json
multi-claude env --defaults CLAUDE_CODE_PLUGIN_CACHE_DIR=~/.claude-shared/plugins
```

#### 5.1.6 默认账号迁移 `migrate-default`

**默认账号由什么组成**

| 组成部分 | 位置 | 由谁决定 |
| ---- | ---- | ---- |
| 配置目录 | `~/.claude` | `CLAUDE_CONFIG_DIR` 未设置（依据 1） |
| 全局状态文件 | `~/.claude.json` | `CLAUDE_CONFIG_DIR` 未设置（依据 4） |
| 凭据（macOS） | 钥匙串 `Claude Code-credentials`（无后缀） | `CLAUDE_CONFIG_DIR` 未设置（依据 2） |
| 凭据（Linux，及 macOS 退回时） | `~/.claude/.credentials.json` | 位于配置目录内（依据 3） |

三者都由“`CLAUDE_CONFIG_DIR` 未设置”这一条件决定。所以迁移的核心设计是：**移动目录，但保持“未设置 `CLAUDE_CONFIG_DIR`”这个身份不变**。

**做法**

1. 用 multi-codex §5.1.5 的完整机制（事务记录、rename / copy 校验、park、兼容软链、续跑表、回滚）把 S=`~/.claude` 移到 T=`<root>/<名称>`，在 S 留指向 T 的绝对路径软链。
2. 登记账号时 `identity` 记为 `default`，启动命令按 §5.1.4 的 default 身份段生成：不设 `CLAUDE_CONFIG_DIR`。
3. `~/.claude.json` **原地不动**，不移动、不建软链。
4. 钥匙串、`.credentials.json` 都不碰。Linux 的 `.credentials.json` 随目录进入 T，经 S 的软链照常读到。

**逐项推演**

| 场景 | 配置目录 | `.claude.json` | 凭据 | 结果 |
| ---- | ---- | ---- | ---- | ---- |
| 迁移后直接运行 `claude` | `~/.claude` → 软链 → T | `~/.claude.json` | 无后缀条目 / T 内文件 | 与迁移前相同 |
| 运行 `claude-<名称>` | 同上 | 同上 | 同上 | 与直接运行 `claude` 相同，外加代理、环境变量、参数 |
| 从另一账号的会话里运行 `claude-<名称>` | 启动命令清除了继承的 `CLAUDE_CONFIG_DIR` | 同上 | 同上 | 正确落到默认账号；但继承的 `CLAUDE_CODE_CHILD_SESSION` 会让这个会话不进入历史与 `--resume`（依据 16，是否清除见 §10） |
| 手工运行 `CLAUDE_CONFIG_DIR=T claude` | T | `T/.claude.json`（不存在） | 带后缀条目（不存在） | 等同全新账号，需要登录。README 明确写“默认账号只能用 `claude` 或 `claude-<名称>` 启动” |
| 用户删掉了 S 处的软链 | `claude` 会新建空的 `~/.claude` | 不变 | macOS 仍有效，Linux 丢失 | 启动命令检测到 S 与 T 不一致，报错退出 1，不启动 |

**为什么不移动 `~/.claude.json`**：

- 只要不设 `CLAUDE_CONFIG_DIR`，Claude 就从家目录读它（依据 4），留在原处就是对的；
- 移走再建软链，取决于 Claude 写这个文件时会不会用“临时文件 + rename”把软链换成普通文件——未验证，而这一步对目标没有任何好处；
- 代价：默认账号的数据分在两处。`list` 对 default 身份的账号额外显示 `~/.claude.json` 的位置，README 写明。

**为什么固定源为 `~/.claude`、去掉 `--source`**：default 身份依赖“Claude 不设变量时用 `$HOME/.claude`”，迁移其它目录没有意义；要登记其它目录用 `add`。

**备选方案的否决理由**

- **启动命令设 `CLAUDE_CONFIG_DIR=T` 并设 `CLAUDE_SECURESTORAGE_CONFIG_DIR=`（空串）保住无后缀条目**：依赖未写入文档的内部变量，Claude 改名或删掉它时登录会静默失效；同时还得移动 `~/.claude.json` 并处理软链被替换的问题；直接运行 `claude` 与启动命令看到的配置目录字符串不同，行为可能分叉。不采用。
- **把钥匙串条目复制成带后缀的新条目**：需要读出凭据明文，违反非目标；OAuth 刷新令牌轮换后两份副本会互相失效。不采用。
- **迁移后要求重新登录**：仍需决定 `.claude.json` 去向，且直接运行 `claude` 的行为会改变。不采用。
- **一期不做迁移**：按上面的设计，迁移不改变登录绑定所依赖的任何条件，剩余风险与 multi-codex 相同（数据移动本身），已由事务记录与续跑表覆盖。保留在一期。

**预检相对 multi-codex 的增量**

- 已有另一个 `identity=default` 的账号时判冲突（同一台机器只有一个默认账号）。
- 本工具自身环境中的 `CLAUDE_CONFIG_DIR`：非空且不指向 S 时警告（当前 shell 直接运行 `claude` 用的不是 `~/.claude`）；realpath 指向 S 时返回 4（`usage=config-dir`，`pid` 为本工具自身），提示先 `unset CLAUDE_CONFIG_DIR`。这一条覆盖 `CLAUDE_CONFIG_DIR=~/.claude multi-claude …` 这种只对单条命令生效的写法——占用检查第 3 条排除了本工具自身，看不到它。`CLAUDE_SECURESTORAGE_CONFIG_DIR` 已设置时同样警告。
- 续跑表第 2 行“S 已是指向 T 的软链、账号未登记”时补登记为 default 身份（覆盖用户事先手工迁移过的情况）。

**`apply -f` 与 identity**：`identity` 是迁移的产物，`dir` 与 `default` 之间的转换只能由 `migrate-default` 完成：

- 文件中已登记账号的 `identity` 一律忽略，沿用当前配置（与 `managed_links` 相同）；
- 文件中**未登记**的账号写了 `identity: "default"`（例如把配置搬到新机器）：不登记、不建目录、不生成启动命令，输出一行 `skip account <名称> (identity is default; run multi-claude migrate-default <名称> to create it, then set its proxy/env/args/shared again)`——文件中该账号的 `proxy`、`env`、`args`、`shared` 不会写进配置，迁移完成后需要重新设置（`migrate-default --proxy` 可一并设代理）；其它账号照常收敛，退出码不受影响。之后执行 `migrate-default <名称>` 时 T 不存在，迁移可以正常进行；
- 未写 `identity` 或写 `dir` 的新账号为 `dir`。

`examples/config.example.json` 中写一个 `identity: "default"` 的示例账号，并在 README 说明上面第二条。

#### 5.1.7 占用检查

只在 `migrate-default` 中执行（它是唯一移动数据的命令），时机沿用 multi-codex §5.1.5 第 2 步（首次、移动前、copy 模式 park 前）。设 R = realpath(S)，H = realpath(本工具进程的 `HOME`)。以下任一命中即判占用，返回 4。

**前提：迁移期间不能有任何 Claude 进程。** 依据 12：不论用哪个账号，Claude 进程都会写 `$HOME/.claude` 下的 `bridge-spawn`、`state/`、`ide/` 等。rename 窗口内写入会重建真实的 S（迁移停在建软链前）；copy 模式下复制完成到 park 之间写入的内容会随备份 B 一起被删除。所以本工具必须从**没有任何 Claude 会话的普通终端**运行，包括不能在某个 Claude 会话的 Bash 工具里运行。

**第 0 条：本工具自己是否运行在 Claude 会话中**。本工具环境中存在 `CLAUDE_CODE_CHILD_SESSION`（官方 env-vars 页：只由 Claude Code 自己在 Bash、PowerShell、Monitor 工具、hook、status line 的子进程中设置；`CLAUDECODE` 在 IDE 集成终端里也会被设置，不能用）时，直接返回 4，`usage=inside-claude-session`，提示从普通终端运行；同时提示：该变量可能被 screen、tmux 或由 Claude 启动的后台程序继承，确认不在 Claude 会话里时可用 `env -u CLAUDE_CODE_CHILD_SESSION multi-claude …` 只绕过这一条。这一条先于其它检查执行，也覆盖了下面“检查失败”中的命名空间场景。README 同时提示迁移前退出 Claude Desktop（它是否写 `~/.claude` 未核实）。

1. **路径占用**：沿用 multi-codex `find_busy_processes`——`lsof -n -P -F pcfn`（Linux 无 lsof 时走 `/proc`），工作目录、可执行文件或打开的文件落在 R 之下。`usage` 取值沿用（`cwd`、`executable`、`mapped`、`fd N`）。
2. **Claude 进程**：枚举本用户（与本工具相同 uid）的全部进程，读出 argv 与环境变量：
   - macOS：`ps -axo pid=,uid=` 取进程列表，再对每个同 uid 进程调用 `sysctl(CTL_KERN, KERN_PROCARGS2, pid)`（ctypes 调用 libc），结果按 NUL 分隔，精确无歧义；
   - Linux：`/proc/<pid>/cmdline` 与 `/proc/<pid>/environ`。

   满足任一条即视为 Claude 进程（依据 11）：
   - argv[0] 的文件名（`os.path.basename`）等于 `claude`，或以 `claude ` 开头（后台终端宿主进程的 `claude bg-pty-host`）；
   - argv[0] 或可执行文件路径（macOS 取 `KERN_PROCARGS2` 开头的 exec_path，Linux 取 `/proc/<pid>/exe`）含 `/claude/versions/`；
   - argv 中出现 `--bg-pty-host`；
   - argv[0] 的文件名为 `node` 或 `bun`，且 argv[1] 的文件名等于 `claude` 或 argv[1] 路径含 `@anthropic-ai/claude-code`（npm 形态，见 §7）。

   不单看 argv[1]，以免把 `ssh claude` 之类的进程误判进来。它环境中 `HOME` 的 realpath 等于 H（没有 `HOME` 时按等于处理）就判占用，`usage=claude-process`，`path` 显示它的配置目录（`CLAUDE_CONFIG_DIR` 或 `<HOME>/.claude`）。按 `HOME` 而不是按配置目录比较，是因为依据 12 的写入只取决于 `HOME`；同时这也让测试的临时 HOME 不会把真实机器上的 claude 进程算进来。
3. **继承了配置目录的其它进程**：任何同 uid 进程（不限 Claude 进程）的环境中 `CLAUDE_CONFIG_DIR` 非空且 realpath 等于 R，判占用，`usage=config-dir`（Claude 会话派生的 shell 会继承这个变量）。macOS 上 Apple 自带程序的环境读不出（依据 9 补充），所以系统自带 shell 里导出的该变量看不到；这类进程计入“读不出环境”的汇总提示。第 2 条不受影响：Claude 进程是第三方程序，环境可读；万一读不出、但 argv 能认出是 Claude 进程，按占用处理。本工具自身及其子进程（lsof、ps）不参与判断。命中的是本工具的父进程时，单独提示“当前 shell 导出了 `CLAUDE_CONFIG_DIR=<值>`，请先 `unset CLAUDE_CONFIG_DIR`”。
4. **IDE 扩展**：读 `S/ide/*.lock`，只解析 JSON 的 `pid` 与 `ideName` 两个字段（不读取、不输出 `authToken`），pid 存活即判占用，`usage=ide-lock`。文件不是合法 JSON 时跳过并警告。
5. **后台 supervisor**：运行中的 supervisor 是 Claude 进程，由第 2 条覆盖。supervisor 按配置目录各一个实例（依据 14），报错时对第 2 条命中的每个进程按它的配置目录给出停止命令：配置目录为 `<HOME>/.claude`（未设变量）时写 `env -u CLAUDE_CONFIG_DIR claude daemon stop --any`，否则写 `CLAUDE_CONFIG_DIR=<目录> claude daemon stop --any`（按需启动的实例需要 `--any`；不指定变量会停掉当前 shell 所指账号的实例）。macOS 上存在 `~/Library/LaunchAgents/com.anthropic.claude-daemon.plist` 时，不论进程是否在运行都判占用（`usage=daemon-service`）：该服务会被 launchd 拉起并写 S；提示先运行 `claude daemon uninstall`，迁移完成后可再安装。Linux 上的服务安装形态（是否为 systemd user 服务、单元名）本次提取的是 macOS 构建，未核实；一期 Linux 只靠第 2 条拦运行中的进程，§10 记录待核。

输出格式沿用 multi-codex：每个进程一行 `pid=… command=… usage=… path=…`。

**检查失败**（拒绝迁移，提示可用 `--skip-process-check` 跳过第 0–5 条全部检查，风险由用户自负）：

- 第 1 条沿用 multi-codex 的判定；
- 第 2、3 条：同 uid 进程一个都读不出环境；
- Linux：`/proc/1/comm` 为 `bwrap`，说明本工具运行在 bwrap 建的 PID 命名空间中，看不到宿主进程。开启 `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` 的 Claude 会话正是用 `bwrap --unshare-pid --proc /proc` 隔离子进程（源码检索 `--unshare-pid`），该场景通常已被第 0 条拦下，这里是第二道防线。

  判定方式的取舍（Linux 测试机 Ubuntu 20.04 实测）：宿主上普通用户读 `/proc/1/ns/pid` 失败；在 `bwrap --unshare-pid --proc /proc` 或 `unshare -p --mount-proc` 中，`/proc/self/status` 的 `NSpid` 只有一个值、`/proc/1` 的属主显示为 root，这两种信号都检测不到嵌套；`bwrap` 中 `/proc/1/comm` 为 `bwrap`。所以只用 `/proc/1/comm`。其它容器形态（docker 等）中本工具本来就只能看到容器内的进程，与容器内的 Claude 同处一个命名空间，不属于本条要拦的情形。

#### 5.1.8 账号路径绑定：哪些操作会改变路径，如何处理

dir 身份账号在 macOS 上的登录绑定在启动命令里那串 `account_dir` 上（依据 2）。工具内所有可能改变它的操作：

| 操作 | 处理 |
| ---- | ---- |
| `init --root` / `apply -f` 修改根目录，且已有账号 | 冲突，返回 3（沿用 multi-codex） |
| 账号改名，包括只改大小写 | 冲突，返回 3（沿用 multi-codex） |
| 根目录写法不同但展开后相同（如 `~/.cc` 与 `/Users/x/.cc`） | 展开后字符串相同，`unchanged` |
| 启动命令里已写的路径与新渲染的不同（典型原因：`HOME` 变了而 `XDG_CONFIG_HOME` 与 `bin_dir` 写的是绝对路径，工具仍读到同一份配置、找到同一个启动命令） | **新增规则**：已存在的受管启动命令中 `account_dir=` 那一行与新渲染的不同 → 冲突，原因为 “would change the login-bound account path”。只有启动命令不存在时才按新值创建。`HOME` 变化而 `bin_dir`、状态目录都跟着 `HOME` 走时，工具读到的是另一份配置，本规则不触发，也不需要触发 |
| 在大小写不敏感的文件系统上 `add` 已有目录，账号名与磁盘上的目录名只差大小写 | **新增规则**：计划阶段当 `os.path.isdir(join(root, 名称))` 为真、但 `os.listdir(<root>)` 中没有与名称逐字相同的条目时判冲突（只在大小写不敏感的文件系统上可能成立；大小写敏感的文件系统上 `Work/` 与 `work/` 并存时 `add work` 不受影响），原因说明“启动命令会写成 `<账号名>`，与登录时使用的 `<实际名>` 不同，macOS 上会失去登录”，提示改用实际名称登记 |
| `init --bin-dir`、`shared.dir`、代理、环境变量、参数变化 | 不影响路径，正常 `update` |
| 用户手工搬动账号目录 | 启动命令报“账号目录不存在”；README 说明 macOS 上需要重新登录或搬回原处 |

工具不提供“搬账号”的命令，README 的“手工搬迁”一节说明：macOS 上搬完后需要在新路径重新 `/login`，旧钥匙串条目可用 `security delete-generic-password -s 'Claude Code-credentials-<旧后缀>'` 手工清理（`list --verbose` 显示每个账号的服务名）。

default 身份账号不受本节约束：它的绑定条件是“不设变量”，与 T 的路径无关。

#### 5.1.9 登录状态探测（只读）

`identity.py` 提供：

- `service_name(identity, account_dir)`：default → `Claude Code-credentials`；dir → `Claude Code-credentials-` + `sha256(unicodedata.normalize("NFC", account_dir).encode("utf-8")).hexdigest()[:8]`。这里假定正式环境（`OAUTH_FILE_SUFFIX` 为空，依据 2）；检测到 `CLAUDE_CODE_CUSTOM_OAUTH_URL` 已设置时，`list` 与 `add` 提示“登录状态探测按正式环境计算，结果可能不准”；
- `keychain_account()`：与 Claude 的 `ok()` 相同——取 `USER`（缺失时用 `pwd.getpwuid(os.getuid()).pw_name`），不匹配 `^[a-zA-Z0-9._-]+$` 时为 `claude-code-user`；
- `credentials_file(account_dir)` → `<account_dir>/.credentials.json`；
- `probe(identity, account_dir)` → `keychain`（macOS 上条目存在）、`file`（凭据文件存在）、`none`、`unknown`（查询本身失败）。

macOS 的查询命令固定为 `security find-generic-password -a <keychain_account()> -s <服务名>`（与 Claude 的查询条件一致），**不带 `-w`、`-g`**，只看退出码（0 = 存在，44 = 不存在，其它 = unknown），stdout 与 stderr 丢弃。代码中只有这一处调用 `security`，并有单元测试断言假 `security` 收到的参数（§8）。`security` 通过 `PATH` 查找，测试用假脚本替换。

用途：

- `add` 登记已有目录时（目录存在且计划为 `unchanged`），macOS 上探测结果为 `none` 时警告：“该路径字符串下没有登录记录，首次启动需要登录；如果以前用别的写法登录过，请保持同一写法”；不改退出码。
- `list` 的 LOGIN 列。
- 不在任何写操作的判定中使用（只提示）。

#### 5.1.10 共享资源

沿用 multi-codex §5.1.6 与 `feature-adopt-links.md`（含 `--adopt`），差异只有默认条目：`agents`、`commands`、`skills`、`CLAUDE.md`。只软链 `shared.items` 中列出的条目，共享目录里其它内容（如 `todo/`、`playbook/`）不会被链接。

**真实 `skills` 目录与 `synced/`**：账号里的 `skills` 是真实目录时，开启共享判冲突、整条命令返回 3、不做任何修改（沿用 multi-codex §5.1.7）。工具不移动 `synced/`。README 说明它是什么（依据 7）以及用户自行处理时的事实：各账号的同步桶按组织 + 账号命名，放进共享目录的 `skills/synced/` 下不会与别的账号冲突；共享后，该账号以后同步下来的技能也会写进共享目录。是否这样处理由用户决定。

#### 5.1.11 一键安装 `install.sh`

沿用 multi-codex §5.1.8，全部名称替换：`multi-codex` → `multi-claude`，`MULTI_CODEX_*` → `MULTI_CLAUDE_*`，安装到 `<prefix>/share/multi-claude` 与 `<prefix>/bin/multi-claude`，仓库 `jakoes-wu/multi-claude`。`pyproject.toml` 声明 `multi-claude` 入口，可用 `pipx install git+https://github.com/jakoes-wu/multi-claude` 安装。

### 5.2 接口变更

全新项目，没有存量兼容问题。对外契约：

- **命令行**：§5.1.2 的全部子命令与参数；退出码同 multi-codex §5.2。
- **配置文件 `config.json`**（`version: 1`）：

  ```json
  {
    "version": 1,
    "root": "~/.cc",
    "bin_dir": "~/.local/bin",
    "shared": {"dir": "~/.claude-shared", "items": ["agents", "commands", "skills", "CLAUDE.md"]},
    "defaults": {"env": {}, "args": []},
    "accounts": {
      "main": {"identity": "default", "proxy": "inherit", "shared": false, "managed_links": [], "env": {}, "args": []},
      "work": {"identity": "dir", "proxy": "http://127.0.0.1:7901", "shared": true, "managed_links": ["skills"], "env": {}, "args": []}
    }
  }
  ```

  `identity` 与 `managed_links` 是工具内部状态，`apply -f` 忽略文件中的值。`env`、`args`、`defaults` 缺省为空。
- **启动命令**：文件名 `claude-<名称>`，第 2 行受管标记；身份段、`"$@"` 永远在最后的顺序契约见 §5.1.4。
- **事务记录**：内部格式；字段与 multi-codex 相同（`source` 字段仍记录 S 的路径，即 `~/.claude` 展开后），去掉命令行参数 `--source` 的记录，增加 `identity: "default"`。
- **测试钩子环境变量**：`MULTI_CLAUDE_TEST_FAIL_AT`、`MULTI_CLAUDE_TEST_CRASH_AT`、`MULTI_CLAUDE_TEST_CRASH_SWAP`，语义同 multi-codex；新增 `MULTI_CLAUDE_TEST_PROC1_COMM`（Linux 上代替 `/proc/1/comm` 的内容）。所有 `MULTI_CLAUDE_TEST_*` 钩子只在同时设置 `MULTI_CLAUDE_TEST_MODE=1` 时才被读取，避免在生产使用中被误设而关掉检查；README 不介绍这些变量。

本项目没有 `docs/reference/*`，不涉及 reference 章节的 sibling 回补检查。

## 6. 备选方案与决策

- **默认账号迁移方式**：见 §5.1.6 备选方案。
- **socks 代理：拒绝还是像 multi-codex 那样只警告**：官方文档明确不支持；接受后只会得到连接失败。拒绝。
- **占用检查读进程环境的方式（macOS）**：`ps -E` 把环境变量用空格拼在命令行后面，路径含空格时无法可靠切分；`KERN_PROCARGS2` 返回 NUL 分隔的原始数据，精确。采用后者，`ps -E` 不作为后备（宁可判“检查失败”也不做不可靠的解析）。
- **占用检查只拦“配置目录等于 S”的 Claude 进程**：漏掉依据 12 中其它账号对 `$HOME/.claude` 的写入，copy 模式下会丢数据。改为拦同一 `HOME` 下的全部 Claude 进程，代价是迁移时要关闭所有账号的会话；迁移是一次性操作，可以接受。
- **代理改用 `--settings '{"env":{...}}'` 注入以压过 settings 文件**：内联 settings 与账号自己的 settings、`--settings` 共享文件的合并顺序还要另行核实，且会改变用户 `defaults.args` 中 `--settings` 的语义。一期只告警（§5.1.3）。
- **与 multi-codex 共用一个库**：两项目发布节奏独立，共用代码会让一方的修改牵连另一方的发布和测试。复制并在 §4 标注来源。
- **一期支持 Windows**：同 multi-codex，放二期。

## 7. 影响分析

**对 Claude Code 本身**：只通过环境变量与命令行参数影响 Claude，不改 Claude 的任何数据文件。迁移只移动 `~/.claude` 目录本身。

**正向推（工具改了什么 → 谁受影响）**

- 启动命令 `claude-<名称>`：只影响通过它启动的进程。dir 身份设置 `CLAUDE_CONFIG_DIR` 为工具生成时的字符串；对登记的已有账号，只要原来的启动方式用同一种路径写法（如 `"$HOME/.cc/<名称>"` 展开结果），这个字符串就与之相同（`expand` 不做 realpath），钥匙串后缀不变。
- 迁移后的 `~/.claude` 软链，经它落到 T 的读写方：
  - 不设 `CLAUDE_CONFIG_DIR` 的进程：直接运行的 `claude`、`claude-<名称>`（default 身份）、按需启动的默认账号 supervisor、LaunchAgent 形态的 supervisor（依据 14）；
  - **所有账号**的进程对 `$HOME/.claude` 的固定写入：`bridge-spawn`、`.device-keys.json`、`state/`、`ide/`（依据 10、12）；
  - 这些路径字符串都不变，只是多经过一层软链。沙箱写保护同时包含真实路径（依据 15），不受影响。
- daemon socket 路径按 `sha256(resolve(we()))` 计算，default 身份下 `we()` 仍为 `~/.claude`，迁移前后不变。
- `claude` 写入 `~/.claude.json`：不受影响（文件未动）。

**反向推（没改的东西会不会被波及）**

- 其它账号的登录：工具不碰钥匙串与凭据文件；dir 身份账号的路径在 §5.1.8 规则下不会被工具改变。
- 其它账号的会话：迁移要求它们全部关闭（§5.1.7）。迁移完成后它们对 `$HOME/.claude` 的写入经软链落到 T，即默认账号的目录里——与迁移前落在 `~/.claude` 实体目录中是同一回事。
- 共享目录：只软链列出的条目；`skills` 共享后，同步技能写入共享目录，这是 Claude 自身行为，开启共享前已经如此（三个已共享账号的桶都在共享目录里）。
- 用户已有的 shell 函数与 `claude` 别名：工具不修改，可以并存。
- 代理：settings 文件中的代理变量优先于启动命令（依据 13），工具只告警（§5.1.3）。后台会话：supervisor 按配置目录各一个实例，继承第一个启动它的 shell 的环境（依据 6、14）——用 `claude-<名称>` 启动的前台会话冷启动了该账号的 supervisor 时，代理随之进入；否则不进入。需要后台会话稳定走代理的用户，应把代理写进该账号自己的 `settings.json` 的 `env`，并把该账号在工具中的代理设为 `inherit`，避免两处设置互相覆盖；README 写明这一取舍。

**运行时**

- 启动命令：额外一次 `exec`；default 身份多两次 `cd -P`。
- 迁移：同盘 rename 与目录大小无关；跨盘 copy 需要一份额外空间并全量计算 SHA-256（开发机实测 `~/.claude` 约 904M）。
- 占用检查：枚举本用户全部进程，各做一次 sysctl 或读两个 `/proc` 文件，毫秒级。
- `list`：macOS 上每个账号调用一次 `security`，并读每个账号的 settings 文件做代理冲突检查。

**部署形态**

- Linux 上凭据随目录走，§5.1.8 的冲突规则偏保守（Linux 上搬目录不会丢登录），一期不区分平台，统一按冲突处理。
- 只装了 npm 版 Claude 时，按 shebang（`#!/usr/bin/env node`）的执行机制，进程 argv[0] 为 `node`、argv[1] 为 `.../bin/claude`，可被 §5.1.7 第 2 条的 argv[1] 规则覆盖——这是推断，开发机没有 npm 版，未实测；编码阶段在 Linux 测试机上用 `npm i -g --prefix <临时目录>` 装一份、用临时 HOME 启动后验证一次（不登录）。
- 在 Claude 会话中运行 `migrate-default`（含开启了 env scrub、子进程处在 bwrap PID 命名空间中的情形）：被第 0 条拒绝；第 0 条未命中而 PID 1 为 `bwrap` 时判为检查失败（§5.1.7）。

**对外语义**：§3 依据 9、12 证明 multi-codex 的路径占用检查对 Claude 不充分，本方案用第 2–5 条补齐；第 3 条对“任何带该 `CLAUDE_CONFIG_DIR` 的进程”判占用，会把用户手工 `export` 了该变量的终端也算进去——这是有意的保守行为，报错信息说明原因。

## 8. 回归测试

**测试环境与安全闸**

- 沿用 multi-codex：unittest、每个用例独立临时 HOME、CLI 以子进程运行、CI 矩阵（ubuntu 3.8/3.12、macOS 3.10/3.12）、pyflakes、Linux 上 shellcheck。
- 假 `claude`：把参数、全部环境变量写到文件。假 `security`：把收到的参数追加到记录文件，按测试设定的退出码返回。
- **安全闸**（`helpers.py` 的 `setUp`，任一不满足即让用例失败而不是跳过）：
  - 子进程 `PATH` 固定为 `<临时目录>/fb:/usr/bin:/bin:/usr/sbin:/sbin`（`install.sh` 测试另需系统 `PATH` 时，把假脚本目录放在最前）；用 `shutil.which` 在该 `PATH` 下断言第一个 `claude` 与第一个 `security` 都是假脚本；
  - `HOME`、`XDG_CONFIG_HOME` 指向临时目录；
  - 子进程环境中删除 `CLAUDE_CONFIG_DIR`、`CLAUDE_SECURESTORAGE_CONFIG_DIR`、`CLAUDE_CODE_CUSTOM_OAUTH_URL`、`CLAUDECODE` 以及所有 `CLAUDE_CODE_*` 变量（其中 `CLAUDE_CODE_CHILD_SESSION` 不删的话，在 Claude 会话里跑测试时每个迁移用例都会被第 0 条拦下）；
  - CI 里 grep 检查 `tests/` 不含 `/Users/`、`/home/` 字面量（`helpers.py` 中构造临时路径的代码除外）。
- 发布前在两台机器完整跑一遍：开发机（macOS）与 Linux 测试机（Ubuntu 20.04，Python 3.8），都用源码快照、临时 HOME，不碰真实账号。

**用例**（multi-codex §8 的 1–16 条全部移植，名称替换；以下为新增或改动）

1. **幂等**：新增 `env`、`args` 各连续执行两次，第二次全部 `unchanged`，文件 mtime 不变。
2. **dir 身份启动命令**：
   - 假 claude 看到的 `CLAUDE_CONFIG_DIR` 与 `<root>/<名称>` 字符串完全一致（不含尾部斜杠、未经 realpath）；根目录经软链访问时仍写原字符串；
   - 父环境带 `CLAUDE_CONFIG_DIR=其它` 与 `CLAUDE_SECURESTORAGE_CONFIG_DIR=x` 时，前者被覆盖、后者被清除；
   - 参数顺序：`默认参数 + 账号参数 + "$@"`，`"$@"` 中带空格与以 `-` 开头的参数原样传递。
3. **default 身份启动命令**：
   - 假 claude 看不到 `CLAUDE_CONFIG_DIR`（父环境带了也被清除）；
   - 删除 S 处的软链、或改指别处：返回 1，假 claude 未被执行，stderr 含提示；
   - S、T 路径含空格与单引号时正确。
4. **服务名计算**：`service_name` 对固定输入与 `printf %s <路径> | shasum -a 256` 的前 8 位一致（测试中用合成路径）；含组合字符（NFD 的 `é`）的路径与其 NFC 形式得到同一服务名；default 身份为无后缀；`keychain_account` 对 `USER=a b` 返回 `claude-code-user`。
5. **登录探测**：假 `security` 返回 0 / 44 / 1 时分别得到 `keychain` / `none` / `unknown`；目录内有 `.credentials.json` 时为 `file`；**断言假 `security` 收到的参数恰为 `find-generic-password -a <账户名> -s <服务名>`，不含 `-w`、`-g`**；Linux 上不调用 `security`。
6. **代理**：`socks5://`、`socks5h://` 在命令行返回 2、在 `apply -f` 文件中返回 1，且文件系统不变；http 代理清除继承的 `ALL_PROXY`；`off` 清除 8 个变量。
7. **代理与 settings 冲突告警**：账号 `settings.json` 的 `env` 含 `HTTPS_PROXY`、或 `defaults.args` 中 `--settings` 文件的 `env` 含 `https_proxy`、或内联 `--settings '{"env":{"HTTP_PROXY":"x"}}'`：账号代理为端口时 `list` 与 `apply` 告警，告警中不出现值 `x`；代理为 `inherit` 时不告警；settings 文件不是合法 JSON 时不报错。
8. **环境变量与参数**：
   - 拒绝（返回 2）：`CLAUDE_CONFIG_DIR`、`CLAUDE_SECURESTORAGE_CONFIG_DIR`、`CLAUDE_CODE_CUSTOM_OAUTH_URL`、`HOME`、`USER`、`https_proxy`（小写）、`ANTHROPIC_API_KEY`、`CLAUDE_CODE_OAUTH_REFRESH_TOKEN`、`MCP_CLIENT_SECRET`、`MY_TOKEN`、`DB_PASSWORD`、`X_API_KEY`、非法键名、含换行的值；写在 `apply -f` 文件中时返回 1；
   - 接受：`MAX_THINKING_TOKENS`、`CLAUDE_CODE_MAX_OUTPUT_TOKENS`、`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`、`CLAUDE_CODE_PLUGIN_CACHE_DIR`；
   - `~/x` 展开为绝对路径、`--settings=~/x` 不展开；账号 `env` 覆盖 `defaults.env` 同名键；`--unset` 不存在的键返回 0 且 `unchanged`；
   - `args <名称> --` 清空；`args --defaults -- --ide --settings s` 得到 `["--ide","--settings","s"]`；`args <名称> -- --resume x` 正确；`args <名称>`（缺 `--`）返回 2。
9. **可变参数告警**：`args -- --allowedTools a b` 告警；`args -- --allowedTools a --settings s` 不告警；`--allowed-tools=a` 告警；`args -- --resume`、`args -- --cloud`（可选值选项在末尾无值）告警；告警不改退出码。
10. **路径绑定**：
    - 前置：`XDG_CONFIG_HOME` 与 `bin_dir` 设为绝对路径；生成启动命令后改变 `HOME`（root 仍为 `~/.cc`）再 `apply`：返回 3，原因含 “login-bound”，启动命令未变；
    - 修改 root 时已有账号返回 3（移植）；
    - 大小写不敏感的文件系统上（macOS CI 默认 APFS；Linux 上跳过）：磁盘上有 `Work/`，执行 `add work`：返回 3，文件系统不变；Linux 上 `Work/`、`work/` 并存时 `add work` 正常登记。
11. **迁移（default 身份）**：
    - rename 后 S 为指向 T 的软链；`~/.claude.json` 的 inode、mtime、内容都不变；
    - 账号 `identity` 为 `default`，启动命令为 default 身份段；
    - 已有另一个 default 账号时返回 3，S 未动；
    - `CLAUDE_CONFIG_DIR` 设为另一个临时目录（不等于 S）时输出警告，迁移照常完成；设为 S 时返回 4 并提示 `unset CLAUDE_CONFIG_DIR`；
    - `migrate-default --source` 返回 2（参数已不存在）；
    - `apply -f` 文件中把已登记账号的 `identity` 写成别的值：导入后仍沿用原值；文件中有未登记的 `identity: "default"` 账号：输出 skip、不建目录、不写进 `config.json`、退出码 0，之后 `migrate-default` 该名称可以正常完成；
    - 续跑表、回滚、copy 模式、EXDEV 用例全部移植。
12. **占用检查**（子进程用 `exec -a claude sleep 60` 伪装成 Claude 进程）：
    - 伪 Claude 进程 `HOME` 为测试临时 HOME、`CLAUDE_CONFIG_DIR` 为另一个目录：返回 4，`usage=claude-process`（依据 12 的场景）；
    - 伪 Claude 进程 `HOME` 为另一个临时目录：不算占用；
    - 伪进程的构造：写一个循环 sleep 的 Python 脚本，路径为 `<临时目录>/x/claude`，用 `bash -c 'exec -a <名字> <sys.executable> <脚本>'` 启动（macOS 上 `sys.executable` 必须是真实解释器，不能用 `/usr/bin/python3` 这个 xcrun 跳转程序，它会再次 exec、丢掉 `-a` 设的名字）。`-a ssh`：只有 argv[1] 为 `claude`，不算 Claude 进程；`-a node`：算；
    - argv[0] 为 `<临时目录>/claude/versions/9.9.9`（同样用 `exec -a` 构造）、`HOME` 为测试临时 HOME：返回 4；argv[0] 为 `claude bg-pty-host`：返回 4；
    - 报错中按每个伪 Claude 进程的配置目录给出对应的 `claude daemon stop --any` 命令；
    - 普通常驻进程（用 Python 写，macOS 上 `/bin/sleep` 的环境读不出）环境带 `CLAUDE_CONFIG_DIR=S`：返回 4，`usage=config-dir`；带别的值：不算占用；
    - 本工具自身与其子进程不被判为占用（运行迁移时环境中 `CLAUDE_CONFIG_DIR` 未设置的正常情况下，迁移完成）；
    - `S/ide/1.lock` 中 `pid` 为存活的测试子进程：返回 4，`usage=ide-lock`，输出中不含 `authToken` 的值；`pid` 为已退出进程：不算占用；lock 文件不是合法 JSON：跳过并警告；
    - macOS：临时 HOME 下存在 `Library/LaunchAgents/com.anthropic.claude-daemon.plist`：返回 4，`usage=daemon-service`，提示含 `claude daemon uninstall`；
    - 本工具环境中带 `CLAUDE_CODE_CHILD_SESSION=1`：返回 4，`usage=inside-claude-session`，S 未动；只带 `CLAUDECODE=1`：不受影响；
    - Linux：测试钩子 `MULTI_CLAUDE_TEST_MODE=1` 加 `MULTI_CLAUDE_TEST_PROC1_COMM=bwrap`：判为检查失败；不设 `MULTI_CLAUDE_TEST_MODE` 时钩子不生效；判为检查失败时返回 1 并提示 `--skip-process-check`；值为 `systemd`：不影响；
    - 路径占用（打开 S 下的文件、工作目录在 S 下）移植；
    - `--skip-process-check` 跳过全部检查。
13. **标准库错误通道**（multi-codex 教训）：S 下放一个权限 000 的子目录：copy 模式校验失败、S 完好、T 被删除、返回 1；删除含 000 子目录的备份不抛 `TypeError`。
14. **共享**：默认条目为 `agents`、`commands`、`skills`、`CLAUDE.md`；共享目录里有 `todo/` 时不被链接；账号 `skills` 为含 `synced/` 的真实目录时开启共享返回 3、目录内容不变；`--adopt` 用例移植。
15. **install.sh**：移植 multi-codex 用例；shellcheck 同时检查两种身份、带额外环境变量与参数的启动命令样例。

## 9. 日志 / 观测点

沿用 multi-codex §9（每个动作一行、前缀改为 `[multi-claude]`、英文输出、错误带 `phase=` 与路径、迁移失败输出 S/T/B 状态与下一步命令、事务记录损坏处理）。新增：

- `list` 列：`NAME  IDENTITY  DIR  PROXY  SHARED  LAUNCHER  LOGIN`，default 身份另加 `LINK`（`ok` / `broken` / `missing`）并在表下注明 `~/.claude.json` 的位置；`list --verbose` 额外显示每个账号的钥匙串服务名与凭据文件路径。
- 冲突原因：`would change the login-bound account path (<旧> -> <新>)`、`another account already has the default identity`、`SOCKS proxies are not supported by Claude Code`、`directory exists as <实际名> (differs only in case)`。
- 警告：可变参数告警、代理被 settings 覆盖、`ISOLATION_BREAKING_ENV` 中的变量已在当前 shell 导出（`migrate-default` 与 `list` 时，沿用 multi-codex）、`CLAUDE_CONFIG_DIR` / `CLAUDE_SECURESTORAGE_CONFIG_DIR` / `CLAUDE_CODE_CUSTOM_OAUTH_URL` 已设置、登录探测为 `none`、IDE 锁文件无法解析。
- 跳过：`skip account <名称> (identity is default; run multi-claude migrate-default <名称> to create it, then set its proxy/env/args/shared again)`（与 §5.1.6 一致）。
- 占用输出新增 `usage=inside-claude-session`、`claude-process`、`config-dir`、`ide-lock`、`daemon-service`；进程环境读取失败的进程数在检查结束时汇总一行。
- 任何输出都不打印环境变量的值（`CLAUDE_CONFIG_DIR`、`HOME` 除外）、`authToken`、settings 中 `env` 的值或凭据内容。

## 10. 开放问题

- **启动命令命名**：2026-09-30 用户决定采用 `claude-<名称>`。
- **启动命令是否清除 `CLAUDE_CODE_CHILD_SESSION`**：2026-09-30 用户决定不清除。背景：从某个 Claude 会话的 Bash 工具里运行 `claude-<名称>` 时，继承的该变量会让新会话不进入历史、`--resume` 与 `claude agents`（依据 16）。清除它，嵌套启动的会话就和终端里启动的一样；不清除，保持 Claude 对“子会话”的默认处理。一期默认不清除。
- **`CLAUDE_CODE_HTTP_PROXY` / `CLAUDE_CODE_HTTPS_PROXY`**：源码中出现（为子进程设置代理时读取），文档未列出。`off` 一期不清除它们；如用户环境中设置了，`list` 给出提示。2026-09-30 用户决定保持不清除。
- **Linux 上 supervisor 的服务安装形态**：本次只提取了 macOS 构建，Linux 上 `claude daemon install` 是否装成 systemd user 服务、单元名为何未核实。编码阶段在 Linux 测试机上提取 Linux 构建的字符串核实（只读，不执行 install）；核实前一期 Linux 只靠占用检查第 2 条拦运行中的进程。不阻塞编码。
- **代理实测**：契约依据官方文档。发布前在 Linux 测试机上用记录连接的代理，按 multi-codex §10 的方法实测 http 代理与 `off`；需要该机已登录 Claude，若未登录则只测到“请求经过代理”为止。不阻塞编码。

## 11. 二期：Windows 支持（不在本期编码范围）

沿用 multi-codex §11 的框架（cmd 启动命令、junction、`install.ps1`）。Claude 特有的两点：

- 官方文档：Windows 凭据在 `%USERPROFILE%\.claude\.credentials.json`，随目录走，与路径字符串无关；
- 源码中存在 Windows 凭据管理器后端（`CLAUDE_CODE_FORCE_WINDOWS_CREDMAN`、功能开关 `tengu_windows_credman`），开启后凭据可能也会按路径区分。二期先在实机上确认后端，再决定是否套用 §5.1.8 的规则。
