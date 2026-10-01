# 竞品分析与升级路线

> 2026-10-01 调研。范围：GitHub 上“同一台机器使用多个 Claude Code 账号”的工具，以及相邻的提供商切换、用量监控类工具。方法：`gh search repos` 初筛，再读约 20 个项目的 README 与关键源码；凡是关键机制都以源码为准，README 与源码不一致的地方单独注明。没有安装或运行任何竞品。星数为调研当日的值。

## 1. 竞品概览

按“怎么让多个账号共存”分成三类。

| 类别 | 机制 | 能否并行 | 代表项目 |
| ---- | ---- | ---- | ---- |
| A. 每账号一个配置目录 | 启动时设 `CLAUDE_CONFIG_DIR` | 能 | 本项目、kaitranntt/ccs、claude-profile-manager、dotclaude、cprof、silo、asterisk、claude-acct |
| B. 同一 `~/.claude` 换凭据 | 读出钥匙串或 `.credentials.json` 另存，切换时写回，并改写 `.claude.json` 的 `oauthAccount` | 不能，切换影响所有会话 | ming86/cc-account-switcher、Symbioose/claude-account-switcher、claude-swap 默认模式 |
| C. 代理或额度监控 | 本地代理替换请求头轮换账号，或读令牌查询额度 | 不适用 | loekj/claude-acct-switcher、ccquota、claude-code-account-rotation |

另有一类是 API 提供商切换器（farion1231/cc-switch，13.9 万星），只切换接口地址与密钥，不管理多个订阅账号，不是直接竞品。

主要竞品：

| 项目 | 星数 | 语言 | 类别 | 凭据处理 | 突出能力 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| realiti4/claude-swap | 2956 | Python | B + A（`cswap run`） | 读、写、复制，自行刷新令牌 | 用量看板、自动轮换、按目录选账号、历史共享、MCP 镜像、TUI、菜单栏、JSON 输出 |
| kaitranntt/ccs | 2868 | TypeScript | A | 只读令牌查额度；另有代理池 | 上下文共享组、doctor、四种 shell 补全、多 CLI、Web 看板 |
| ming86/cc-account-switcher | 451 | Bash | B | 读写复制，凭据经命令行参数传递 | 简单切换（2025-07 后停更） |
| Symbioose/claude-account-switcher | 65 | Python | B | 读写复制 | 菜单栏用量、到 100% 自动切 |
| JakubKontra/claude-profile-manager | 22 | Go | A | 只做存在性探测（`doctor --verify` 除外） | 目录绑定 + cd 钩子、settings 深合并、MCP 叠加与排除、doctor、补全 |
| ya-luotao/dotclaude | 2 | Bash | A | 不碰凭据 | 零凭据用量（读 Claude 的缓存）、doctor、按项目绑定、可回滚共享 |
| dcotelo/cprof | 5 | Bash | A | 读出令牌查额度，明文备份 | 启动时按 git 根与路径前缀选账号 |
| vika2603/ccs | 2 | Go | A | 读出并复制钥匙串密文 | 改名不掉登录（先写、回读校验、再删）、fork/share 共享状态机、加密备份 |
| xseman/gnome-claude-usage | 2 | TypeScript | 监控 | 默认不碰凭据 | 用官方 statusline 输入采集用量，安装可回滚 |

## 2. 功能对比

“是”表示有，“部分”见括号说明，“否”表示没有。

| 功能 | 本项目 | claude-swap | ccs | profile-manager | dotclaude | cprof |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 多账号并行 | 是 | 部分（`run` 模式） | 是 | 是 | 是 | 是 |
| 不读写凭据 | 是 | 否 | 部分（查额度时读） | 部分（`doctor --verify` 读） | 是 | 否 |
| 迁移 `~/.claude` 且不掉登录 | 是（事务化、可续跑） | 否 | 否 | 否 | 否（保留为默认） | 部分（`--native` 原地沿用，不迁移） |
| 每账号代理 | 是 | 否 | 未核实 | 部分（通用 env） | 否 | 否 |
| 每账号环境变量与固定参数 | 是 | 否 | 未核实 | 是 | 否 | 否 |
| 防止改动掉登录（路径绑定冲突） | 是 | 否 | 否 | 否 | 否 | 否 |
| 声明式配置 + `apply` | 是 | 否 | 否 | 部分（toml + install） | 否 | 否 |
| 迁移前检查 Claude 进程 / IDE 锁 / 后台服务 | 是 | 部分（持锁写入） | 否 | 否 | 否 | 否 |
| 用量显示 | 否 | 是 | 是 | 否 | 是（缓存） | 是 |
| 按目录或项目选账号 | 否 | 是 | 否 | 是 | 是 | 是 |
| doctor 诊断 | 否 | 部分 | 是 | 是 | 是 | 是 |
| shell 补全 | 否 | 否 | 是 | 是 | 否 | 否 |
| `--json` 输出 | 否 | 是 | 未核实 | 是（doctor） | 否 | 否 |
| MCP 共享或按账号管理 | 否 | 是（镜像） | 否 | 是（叠加与排除） | 否 | 否 |
| 会话历史共享或交接 | 否 | 是（可选） | 是（共享组） | 是（默认共享） | 是（可选） | 否 |
| 改名或换目录 | 否 | 是（换槽位） | 否 | 否 | 否 | 否 |
| 启动前清理鉴权覆盖变量 | 否（只告警） | 是 | 未核实 | 是 | 部分（doctor 告警） | 未核实 |
| TUI / 菜单栏 / 看板 | 否 | 是 | 是 | 部分（选择器） | 否 | 部分（状态栏） |
| Windows | 否 | 是 | 部分（有 PowerShell 补全，未逐项核实） | 否 | 否 | 否 |

## 3. 本项目的优势

这些是竞品普遍缺少、且与“可靠、不碰凭据”定位一致的能力，升级时不能削弱：

1. **绝不读写凭据**。只用 `security find-generic-password` 不带 `-w` 查条目是否存在。claude-swap、cc-account-switcher、Symbioose、cprof、vika2603/ccs 都会读出或复制令牌，部分还把令牌放进命令行参数或明文文件。
2. **默认账号迁移不掉登录**。default 身份不设 `CLAUDE_CONFIG_DIR`，迁移有事务记录和续跑表。竞品要么保留 `~/.claude` 不动，要么 `cp -R` 后在 macOS 上掉登录（claudenv、jaydip-18s 的软链方案）。
3. **路径绑定保护**。改根目录、改名、`HOME` 变化、大小写不一致都判冲突，不会悄悄改掉钥匙串服务名。ghackk/claude-multi-account 的改名会 `mv` 目录导致掉登录。
4. **迁移前的完整占用检查**，覆盖 Claude 进程（含 npm 原生程序拉起的子进程）、继承配置目录的进程、IDE 锁、macOS LaunchAgent 与 Linux systemd 服务、在 Claude 会话内运行。竞品里 cc-account-switcher 的进程检测被注释掉，其它多数没有。
5. **每账号代理与 settings 覆盖告警**。没有竞品专门处理代理。
6. **声明式、幂等、可 dry-run**。其它项目大多是命令式。

## 4. 竞品亮点与差距

按对本项目的价值排序。每条注明是否需要读凭据：需要读凭据的不能直接照搬。

1. **用量显示**（claude-swap、ccs、cprof、dotclaude、gnome-claude-usage）。竞品里有六种取数方式，能给出限额百分比又不碰凭据的只有两种：
   - Claude Code 写在 `<配置目录>/.claude.json` 里的 `cachedUsageUtilization`（dotclaude）。本机核实：字段包括 `fetchedAtMs`、`utilization.five_hour/seven_day.utilization`、`resets_at`、`limits[]`。但它不是实时数据：本机正在使用的账号，缓存已有 45 小时没有刷新，Claude Code 在什么时机刷新它尚未核实。字段未写入官方文档。
   - 官方 statusline 输入里的 `rate_limits.five_hour/seven_day.used_percentage` 与 `resets_at`（statusline 文档原文：仅 claude.ai Pro/Max 订阅，会话中第一次 API 响应后出现，窗口过了 `resets_at` 会被去掉）。需要给各账号安装 statusLine 包装命令，把收到的输入另存一份（gnome-claude-usage）。
   - 解析会话记录估算花费（YlunoZup 借助 claude-powerline）也不碰凭据，但反映不了限额百分比。
   - 其余三种都要读 OAuth 令牌：调 `/api/oauth/usage`、发 `max_tokens=1` 的请求读响应头、代理读真实流量的响应头。其中 ccquota 和 loekj 还伪装 Claude Code 的 User-Agent。
2. **按目录或项目自动选账号**（claude-swap `map`、profile-manager `link`+钩子、cprof、dotclaude `bind`、claudenv）。不碰凭据，和本项目的启动命令模型契合。cprof 的“启动时解析”最干净：不需要 cd 钩子，不改 shell 全局状态，天然支持并行。解析顺序是“显式指定 > git 仓库根精确匹配 > 最长路径前缀 > 默认”。
3. **doctor 诊断**（profile-manager、ccs、dotclaude、silo）。全部检查都可以不读凭据：claude 与启动命令目录是否在 PATH、启动命令是否被同名别名或函数遮蔽、断开的共享软链、启动命令是否过期、账号目录权限是否为 0700、鉴权覆盖变量是否已设置、登录探测、钥匙串孤儿条目（有条目、没有对应目录）。profile-manager 的 `--json` 输出加“有错退出码 1”便于脚本化。
4. **启动前清理鉴权覆盖变量**（profile-manager、silo、claude-swap）。`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN` 在 shell 里导出后，会让所有账号改用同一个凭据。本项目目前只在 `list` 与 `migrate-default` 时告警。
5. **MCP 按账号管理**（profile-manager 的“全局减排除加账号叠加”、claude-swap 的镜像、asterisk 的 `claude mcp` 代理）。竞品多数直接改写账号的 `.claude.json`，而这个文件由 Claude Code 自己频繁写入。本项目可以改用 `--mcp-config <文件>` 参数注入，不写 Claude 的文件；现有“账号参数”机制已经能做到，缺的是文档和便捷命令。
6. **会话交接或历史共享**（Nandeep 的单条会话复制交接、claude-swap 的可选历史共享、ccs 的共享组）。单条会话复制最可控：把当前目录最新一条会话的 jsonl 复制到目标账号，再提示 `claude --resume <id>`。整目录共享 `projects/` 会混合各账号历史，竞品里也是可选项。
7. **shell 补全与 JSON 输出**（ccs、profile-manager、claude-acct）。补全账号名与子命令；`list --json` 带版本号，方便状态栏或看板消费。
8. **改名不动目录**（借鉴 vika2603/ccs 的需求，换一种实现）。vika2603 靠读出密文再写回保住登录，与本项目原则冲突。本项目可以把“账号名”与“目录名”解耦：改名只改启动命令名和配置键，目录不动，登录自然不受影响。
9. **共享项扩展与按账号退出**（ccs 的 bare 模式、claude-swap 的共享清单、dotclaude 的不可共享清单）。本项目的 `shared.items` 是全局的，不能让某个账号单独不共享某一项。
10. **其它**：Windows（claude-swap、ccs、ftery0）；TUI 选择器（claude-swap、YlunoZup）；VS Code `processWrapper` 入口（claudenv），让从图形界面启动的 IDE 也能按项目选账号；`.claude.json` 白名单播种，新账号跳过首次引导（profile-manager）。

## 5. 不采纳的做法

| 做法 | 出现在 | 不采纳的理由 |
| ---- | ---- | ---- |
| 在同一 `~/.claude` 里换凭据 | cc-account-switcher、Symbioose、claude-swap 默认模式 | 会影响所有正在运行的会话，要和 Claude 的令牌刷新抢锁；与本项目的并行模型相反 |
| 读出、复制、导出凭据 | 多数 B 类与 cprof、vika2603/ccs | 违反本项目“绝不读写凭据”的原则；竞品中已有把密文放进命令行参数、明文落盘的实例 |
| 自行刷新 OAuth 令牌 | claude-swap、loekj、ccquota、claude-code-account-rotation | 用 Claude Code 的 client_id 与本体竞争刷新，可能导致刷新令牌轮换失效 |
| 额度用尽自动轮换账号 | loekj、claude-swap `auto`、Symbioose、cprof fallback | 依赖读写凭据；claude-acct 与 dotclaude 的 README 都把“轮换账号规避用量限制”列为违反使用政策的用法 |
| 伪装 Claude Code 的请求特征 | loekj、ccquota | 刻意规避服务端识别 |
| 远程脚本 `eval`、凭据上云 | ghackk/claude-multi-account | 安全风险 |
| 安装时静默写入权限放行规则 | julianleopold/claude-profiles | 改用户的安全设置且不告知 |

## 6. 升级路线

原则：不读写凭据；不写 Claude Code 自己管理的文件（`.claude.json`、钥匙串），需要改变 Claude 行为时优先用启动参数和环境变量；每项都保持幂等、可 `--dry-run`、可回滚。

### 6.1 v0.2：可见性与诊断（低风险，建议先做）

| 项 | 做法要点 | 涉及模块 | 规模 |
| ---- | ---- | ---- | ---- |
| `usage` 命令与 `list` 用量列 | 只读每个账号 `.claude.json` 的 `cachedUsageUtilization`（default 身份读 `~/.claude.json`），显示 5 小时 / 7 天百分比、重置时间、数据年龄；超过阈值（如 1 小时）标注为过期；字段缺失或结构变化时显示“无数据”，不报错 | 新增 `usage.py`；`cli.py` | 小 |
| `doctor` | 第 4 节第 3 条的检查项；`--json`；有错误时退出码 1；只读，不修复 | 新增 `doctor.py`；复用 `identity`、`accounts` | 中 |
| shell 补全 | `multi-claude completion bash\|zsh\|fish` 输出脚本，补全子命令、选项、账号名 | `cli.py` | 小 |
| `list --json` | 输出带 `schema_version` 的结构化结果 | `cli.py` | 小 |

### 6.2 v0.3：日常便利

| 项 | 做法要点 | 涉及模块 | 规模 |
| ---- | ---- | ---- | ---- |
| 按目录选账号 | `config.json` 增加 `routes`（git 仓库根精确匹配 + 路径前缀规则）；生成一个 `claude-auto` 启动命令，启动时解析出账号后 `exec` 对应的 `claude-<名称>`；命令 `multi-claude route add/remove/which`；不安装 cd 钩子 | `config.py`、`launcher.py`、`cli.py` | 中 |
| 鉴权覆盖变量的处理 | 启动命令可选择清除 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`；默认值需要决定（见 §7） | `launcher.py`、`config.py` | 小 |
| MCP 按账号配置 | 文档化并提供便捷命令：把共享或账号专属的 MCP 配置文件以 `--mcp-config` 加入账号参数；不写 `.claude.json` | `cli.py`、README | 小 |
| 改名不动目录 | 账号增加不可变的 `dir` 字段（默认等于名称）；`rename` 只改启动命令名与配置键 | `config.py`、`accounts.py`、`cli.py` | 中 |

### 6.3 v0.4：进阶

| 项 | 做法要点 | 涉及模块 | 规模 |
| ---- | ---- | ---- | ---- |
| statusline 实时用量采集（可选开启） | 给账号注入 statusLine 包装命令：把收到的输入原子写入 `~/.local/state/multi-claude/<账号>.json`，再转交原有 statusLine 命令；优先通过启动参数注入而不是改写 settings 文件（多个 `--settings` 能否叠加需先核实），用户已有的 statusLine 要串接保留 | 新增采集脚本；`launcher.py`；`usage.py` | 中 |
| 会话交接 | `multi-claude handoff <目标账号>`：把当前目录最新一条会话 jsonl 复制到目标账号对应的 `projects/<编码目录>/`，提示 `claude --resume <id>`；只复制，不软链、不删除 | 新增 `sessions.py` | 中 |
| 共享项按账号退出 | 账号级 `shared_exclude`；明确不可共享清单（凭据、`.claude.json`、`settings.local.json`、历史） | `shared.py`、`config.py` | 小 |

### 6.4 待定（需求或平台条件未明）

- Windows 支持：已在设计文档 §11 二期中。
- TUI 选择器、菜单栏：可基于 `list --json` 由外部工具实现，本项目先不内置。
- VS Code `processWrapper` 入口：配合 `claude-auto` 可以做，需要先确认 VS Code 扩展的配置项。
- 非凭据备份（settings、skills、项目记忆的导出）。

## 7. 需要用户决定的问题

> 2026-10-01 用户决定：第 6 节 v0.2、v0.3（按目录选账号、改名、MCP 便捷命令）、v0.4 全部纳入；启动命令默认清除鉴权覆盖变量；用量只用零凭据来源（缓存 + statusline），不读令牌。各项按开发工作流分别写 `docs/feature/` 方案。

1. 升级路线中哪些项进入下一版，以及先后顺序。
2. 启动命令是否默认清除 `ANTHROPIC_API_KEY` 等鉴权覆盖变量。清除能避免“所有账号被同一个 API key 接管”，但会让有意用 API key 的用户需要在账号的环境变量里显式设置（目前该类键被 `env` 命令拒绝，若开放需要另行设计）。
3. 用量显示是否只用零凭据来源（缓存 + statusline）。如果需要实时数据，唯一途径是读令牌调 `/api/oauth/usage`，与现有原则冲突，建议不做。
