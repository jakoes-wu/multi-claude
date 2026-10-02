# multi-claude 升级路线

> 2026-10-02：v0.2–v0.4 三组全部完成，随 v0.2.0、v0.2.1 发布；降低上手门槛的三批改进随 v0.3.0 发布（§2.4）。各项的设计见对应的 `docs/feature/` 文档。

## 1. 原则

- 不读写凭据（钥匙串条目、`.credentials.json`、OAuth 令牌）。
- 不写 Claude Code 自己管理的文件（`.claude.json`、钥匙串）。需要改变 Claude 的行为时，优先用启动参数和环境变量。
- 每项都保持幂等、可 `--dry-run`、可回滚。

以下做法与这些原则冲突，不做：

| 做法 | 理由 |
| ---- | ---- |
| 在同一个 `~/.claude` 里切换凭据 | 会影响所有正在运行的会话，还要和 Claude 的令牌刷新抢锁，与本项目多账号并行的模型相反 |
| 读出、复制、导出凭据 | 违反“不读写凭据” |
| 自行刷新 OAuth 令牌 | 会与 Claude Code 本体争抢刷新，可能导致刷新令牌轮换失效 |
| 额度用尽时自动轮换账号 | 依赖读写凭据；用轮换账号规避用量限制违反使用政策 |
| 伪装 Claude Code 的请求特征 | 刻意规避服务端识别 |
| 运行远程脚本、把凭据上传到云端 | 安全风险 |
| 安装时静默写入权限放行规则 | 改动用户的安全设置且不告知 |

## 2. 路线与完成情况

§2.1–§2.3 的“v0.2 / v0.3 / v0.4”是规划时的分组，不是版本号：三组全部随 v0.2.0、v0.2.1 发布。实际版本号见各表的“发布”列。

### 2.1 v0.2：可见性与诊断

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| 用量 | `usage` 命令；`list --json` 带用量；只读 `.claude.json` 的用量缓存 | `feature-visibility-diagnostics.md` | v0.2.0（PR #4） |
| 诊断 | `doctor`，只读，`--json`，有错误时退出 1 | 同上 | v0.2.0（PR #4） |
| 补全与 JSON | `completion bash/zsh/fish`；`list --json`、`list --names` | 同上 | v0.2.0（PR #4） |
| 清除鉴权覆盖变量 | 启动命令默认 `unset` 四个鉴权变量 | 同上 | v0.2.0（PR #4） |

### 2.2 v0.3：日常便利

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| 按目录选账号 | 路由规则 + 生成的 `claude-auto` 入口；`route`、`which`；不装 cd 钩子 | `feature-directory-routing.md` | v0.2.0（PR #5） |
| 改名不动目录 | 账号新增 `dir` 字段；`rename` 只改账号名与启动命令名 | `feature-rename-mcp.md` | v0.2.0（PR #6） |
| 按账号管理 MCP | `mcp NAME ...` 以该账号的身份运行 `claude mcp`，不写 `.claude.json` | 同上 | v0.2.0（PR #6） |

### 2.3 v0.4：进阶

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| 状态栏实时用量 | `statusline install/uninstall` 包装现有 statusLine；钩子记录 `rate_limits` 快照，`usage` 取较新的一份 | `feature-statusline-usage.md` | v0.2.0（PR #7） |
| 钩子提速 | 先 exec 原命令、采集放到后台进程；安装脚本遇到版本管理器的 shim 时写入真实解释器 | `feature-hook-latency.md` | v0.2.1（PR #11） |
| 会话交接 | `handoff TARGET`：把一条会话复制到另一个账号，打印续聊命令 | `feature-session-handoff.md` | v0.2.0（PR #8） |
| 共享项按账号退出 | `add --shared-exclude/--shared-include`；禁止共享账号私有状态 | `feature-shared-exclude.md` | v0.2.0（PR #9） |

### 2.4 降低上手门槛

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| 第一批 | 无参数时的上手说明或账号表；拼错命令的建议；`login`；`add` 后的下一步提示；代理报错可读；写命令只打印变化 | `feature-easier-onboarding.md` | v0.3.0（PR #15） |
| 第二批 | 帮助分组与示例；`list` 简表（`--verbose` 为完整输出）；按 shell 给出 PATH 命令；README 重排 | `feature-clearer-help.md` | v0.3.0（PR #16） |
| 第三批 | `set` 命令；`--shared [DIR]` 与默认 `~/.claude-shared`；空共享目录提示 | `feature-set-command.md` | v0.3.0（PR #17） |
| 小改进 | 本文档补 v0.3.0；包装状态栏后提示新开会话；账号名写在选项后面时的报错 | `feature-usability-fixes.md` | v0.3.1（PR #19） |

### 2.5 常用命令（v0.5.0）

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| 以账号身份运行 | `run [NAME] [-- COMMAND ...]`、`path NAME` | `feature-everyday-commands.md` | v0.5.0（PR #26） |
| 撤销迁移 | `restore NAME`：按实际状态续跑，登录不变 | 同上 | v0.5.0（PR #26） |
| 两处便利 | `migrate-default` 名称可省（取登录邮箱）；`add --config-from` 复制 `settings.json` | 同上 | v0.5.0（PR #26） |

### 2.6 按账号打开 VS Code（v0.6.0）

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| `code NAME [PATH]` | 以账号环境与独立的用户数据目录启动 VS Code；扩展按该账号的 `CLAUDE_CONFIG_DIR` 工作（已实测） | `feature-vscode-launch.md` | v0.6.0（PR #28） |

### 2.7 本地用量历史（v0.7.0）

| 项 | 实现 | 设计文档 | 发布 |
| ---- | ---- | ---- | ---- |
| token 用量历史 | `usage --history`：只读扫描本机会话记录，按回复 ID 去重，按天或模型汇总 | `feature-usage-history.md` | 待发布 |
| 最近使用时间 | `list` 的 `LAST USED` | 同上 | 待发布 |
| 安装提示 | 找不到 claude 时给出官方安装命令 | 同上 | 待发布 |

## 3. 待定（未排期）

- `use NAME`（切换 `~/.claude` 指向的账号）：**不做**（用户 2026-10-03 决定）。直接运行 `claude` 时钥匙串登录（macOS）、`~/.claude.json` 与 default 账号启动命令的指向检查都不跟着 `~/.claude` 走，切换后会混用两个账号的状态；改用各账号的启动命令，或 `route --default NAME` 加 `claude-auto`。见 `feature-everyday-commands.md` §6、§10。

- Windows 支持（见 `feature-account-manager.md` 二期）。
- TUI 选择器、菜单栏：可以基于 `list --json` 由外部工具实现。
- VS Code `processWrapper` 入口：配合 `claude-auto`，让从图形界面启动的 IDE 也能按项目选账号；需要先确认 VS Code 扩展的配置项。
- 非凭据备份：导出 settings、skills、项目记忆。
- 新账号预填 `.claude.json` 白名单项，跳过首次引导。
