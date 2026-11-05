# 启动前网络检查（复刻 multi-codex 0.14.0）

> 2026-11-05 注记：方案已定、代码待落地。第 1 轮设计评审 0 高 5 中 5 低，D1–D10 已修订；第 2 轮收敛（0 高 0 中），低级 D11–D13 已修订。基线 origin/main `c3f2345`（v0.16.0），本文 `file:line` 均指该基线。参照实现：multi-codex origin/main `ebef759`（v0.14.0）的 `docs/feature/feature-network-check.md`、`docs/feature/feature-clash-split.md` 与 `src/multi_codex/netcheck.py`、`clashfind.py`、`platform.py` `list_process_argv` / `split_ps_command`；下文写“同 multi-codex”的地方，行为与文案以那两份方案和代码为准，只把 `codex` / `multi-codex` / `MULTI_CODEX_` 换成 `claude` / `multi-claude` / `MULTI_CLAUDE_`。

## 1. 背景

用户要求把 multi-codex 0.13.0、0.14.0 的全部功能复刻到 multi-claude。逐项核对后：

| multi-codex 功能 | multi-claude 现状 |
| ---- | ---- |
| 0.13.0 `sessions [NAME] [--grep] [--limit] [--cmd] [--json]` | 已有（0.10.0 / 0.11.0 / 0.11.1，`src/multi_claude/sessions.py`） |
| 0.14.0 原生 Windows（`.cmd` 启动命令、junction、Claude 进程检查、占用重试、PowerShell 补全） | 已有（0.15.0，`platform.py` `retry_on_sharing_violation`、`completion powershell`） |
| 0.14.0 `usage --live` 结束整个进程树 | 不适用：multi-claude 没有 `usage --live` |
| 0.14.0 README 完整网址、平台徽章 | 已有（0.15.0 CHANGELOG） |
| 0.14.0 启动前网络检查 `netcheck NAME off\|basic\|strict`，不通过退出码 5 | **缺**；现有 `doctor --network`（0.16.0）只诊断、不拦启动 |
| 0.14.0 Clash 检查拆成 `--clash-tun` / `--clash-global` | **缺** |
| 0.14.0 `clash-api ADDRESS\|auto\|off` | **缺** |

所以本方案只做后三项。用户 2026-11-05 已拍板两处与 multi-codex 不同的地方：

1. 查出口国家的地址用 `https://api.anthropic.com/cdn-cgi/trace`（2026-11-05 本机实测返回 `h=api.anthropic.com`、`loc=US`）。
2. 端口检查与经代理查国家，检查的是 **Claude 实际会用的代理**：启动命令的代理设置，再按 Claude Code 规则用账号 `settings.json` 的 `env` 覆盖（与 `doctor --network` 同一算法，`src/multi_claude/netcheck.py:222-243` `_account_env`、`:262-290` `_parse_proxy`、`:293-301` `_route`）。

已核实、与 multi-codex 不同的事实：

- Claude Code 不支持 SOCKS 代理（`src/multi_claude/config.py:36`、`:505-506` 设置时拒绝；`netcheck.py:275-276`、`:331-332` 诊断时报 `unsupported_socks`），所以不需要 multi-codex 的 SOCKS5 握手实现；实际代理是 SOCKS 时判失败并说明。
- Claude Code 运行中是否会绕过代理直连，仓库内无依据，**未验证**；README 照 multi-codex 写“只在启动那一刻检查一次”，不声称 Claude 一定走代理。
- 现有 `netcheck.py` 是 `doctor --network` 的实现，模块名被占用；新检查放新模块 `launchgate.py`，子命令仍叫 `netcheck`（与 multi-codex 一致）。

## 2. 目标 / 非目标

**目标**

1. 账号可设网络检查：档位 `off`（默认）/ `basic`（实际代理端口能连通）/ `strict`（端口通，且直连与经代理访问 trace 地址得到的 `loc` 都等于配置国家，默认 `US`），以及 Clash 两项 `--clash-tun`（TUN 已开且模式不是 direct）、`--clash-global`（全局模式），`--clash on|off` 为两项一起的简写。开启后，§4 表中所有入口启动 Claude 之前检查，不通过就拒绝启动、说明哪一项没过、给出本次跳过的办法，退出码 5。
2. `clash-api` 命令：显示 / 手动设置 / `auto` 自动查找 / `off` 清除 Clash（mihomo）外部控制接口；开启 Clash 检查而接口未设置时自动查找一次。
3. 未开启检查的账号：启动命令内容逐字节不变、不加载联网模块；macOS / Linux / Windows 都可用，不引入第三方依赖。

**非目标**

- 不保证 Claude 运行中不直连，不做周期检查，不缓存结果；不改 Clash 任何设置（只读 `GET /configs`）。
- 不覆盖 Claude 桌面版、VS Code 插件自己启动的 Claude、不经本工具直接运行的 `claude`，以及 `mcp` 子命令（只改 Claude 的 MCP 配置）。
- 不改 `doctor --network` 的行为与输出。
- “实际代理”只算启动命令的设置与账号自己的 `settings.json` 的 `env`；不模拟账号固定参数或用户参数里的 `--settings`、托管设置（managed settings）、项目级 `.claude/settings*.json`（同 `doctor --network` 的非目标，`docs/feature/feature-network-check.md:47`）。POSIX 的 `netgate` 在 `/` 下运行，也看不到项目目录。README 局限节写明。
- 启动时不自动查找 Clash 接口（查找要枚举进程，同 multi-codex 非目标）。

## 3. 假设与约束

- **“实际代理”的计算**：`launchgate` 调用 `netcheck._account_env(config, account, os.environ, netcheck._settings_path(config, account))` 与 `netcheck._parse_proxy(env, sources)`，再对 `api.anthropic.com` 调 `netcheck._route`。这些是同包私有函数（另含 `_tunnel`、`_settings_path`、`_usable_ca`），本方案只给它们加不带下划线的公开别名（`account_route_env`、`parse_proxy`、`route`、`tunnel`、`settings_path`、`usable_ca`），原名与现有调用点、测试引用都不动；行为不变。
  - POSIX 启动命令里，检查段插在“设置账号环境”之前（§5.1.3），所以 `netgate` 子进程拿到的 `os.environ` 是启动命令的父环境；`env.account_env` 本来就是按“父环境 + 启动命令的设置”算出启动命令最终环境的（`doctor --network` 也这样用），结果相同。
- **判定规则**（basic / strict 共用第 0–3 条）：
  0. 账号 `settings.json` 读不出（JSON 损坏、顶层或 `env` 不是对象，即 `_account_env` 第三个返回值为真）→ `proxy-port` 失败，说明文件路径：gate 是放行判断，忽略这份设置可能放过 Claude 实际会走的线路（doctor 在同样情况下只 warn，`netcheck.py:317-319`）。文件不存在不算读不出。
  1. 实际代理为空（按 `_parse_proxy` 的顺序，所有代理变量最终都为空：`inherit` 且终端没设、`off`、或 settings.json 把它们都设成空）→ `proxy-port` 失败：`no proxy in effect`，并写明看的是哪几个来源。
  2. 实际代理解析失败（SOCKS、写法不对）→ `proxy-port` 失败，文案复用 `netcheck` 的 `unsupported_socks` / `invalid_proxy` 说明。
  3. `api.anthropic.com` 被 `NO_PROXY` 绕过（`_route` 返回 `bypassed`）→ `proxy-port` 失败（`api.anthropic.com is bypassed by NO_PROXY`）：此时 Claude 对这个目标实际不走代理，basic 与 strict 都拒绝。请求不发往 `api.anthropic.com` 的账号（见下条）不做本条判定：绕过一个它不访问的地址与它无关。
  4. `basic`：TCP 连实际代理的主机端口（3 秒），连上即过。
  5. `strict` 另加：实际代理是 `https://` → 失败（同 multi-codex：TLS 套 TLS 不实现）；否则 `direct-country`（不走代理直连 trace）与 `proxy-country`（经代理 CONNECT 后 TLS）各取 `loc` 比较。CONNECT 复用 `netcheck._tunnel`（带 `Proxy-Authorization`，407 等状态归类已有）。
- **请求不发往 `api.anthropic.com` 的账号**（设了 `ANTHROPIC_BASE_URL` 或开了 Bedrock / Vertex / Foundry，`netcheck._custom_endpoint` `:245-259` 判定）：照常按 `api.anthropic.com` 检查，通过行与失败行末尾附一句 `requests go to <去向>; checked api.anthropic.com only`，不拒绝也不改目标。
- **启动命令运行时读 `config.json`**：开启检查后，POSIX 启动命令经 `netgate` 在运行时读配置，路径随 `XDG_CONFIG_HOME` 变化（`platform.py:87-89`、`config.py:196-197`）；在另一种 XDG 环境下启动会找不到账号，`netgate` 报 `account 'X' is not registered in <配置路径>` 并退出 1（文案带上路径）。未开启检查的账号不受影响。
- **trace 地址**：`api.anthropic.com:443` `GET /cdn-cgi/trace`，响应 `key=value` 行，取 `loc=`。属 Cloudflare 通用诊断地址，格式非 Anthropic 文档承诺，外部依赖；实机核对见 §8 R1。
- **mihomo 外部控制接口契约**、自动查找的候选来源（进程参数 `-ext-ctl` / `-ext-ctl-unix` / `-ext-ctl-pipe` / `-secret` / `-d` / `-f`、Clash Verge 配置文件、只采信 `GET /configs` 返回 200 且含 `mode`、`tun.enable` 的候选）同 multi-codex `feature-network-check.md` §3 与 `feature-clash-split.md` §3、§5。
- **POSIX 检查版启动命令需要 `multi-claude` 在 PATH 上**；不在时报 127 并说明，只影响开启检查的账号。
- 配置文件以 0600 写入（`config.py` `save_config`），`clash_api.secret` 与账号 env 里的密钥同等保护。

## 4. 涉及模块

新增文件：

| 区域 | 行号锚点 | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/launchgate.py` | 新文件 | 新增 | `run_checks` / `gate` / `skip_mode` / `scope_text` / `failure_text`，端口、出口国家、Clash 判定，`read_clash_state`（TCP / unix / 命名管道） |
| `src/multi_claude/clashfind.py` | 新文件 | 新增 | `find_controller()`，从 multi-codex 照搬 |
| `tests/test_launchgate.py` | 新文件 | 新增 | §8.1 用例 |
| `tests/data/launchgate-cert.pem`、`launchgate-key.pem` | 新文件 | 新增 | 复制 multi-codex `tests/data/netcheck-*.pem`（127.0.0.1 / localhost 自签，满足 3.13 严格校验） |

修改既有文件：

| 区域 | 行号锚点（c3f2345） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `config.py` `Account` | `:82-121`（`__init__` `:82`、`copy` `:103`、`to_dict` `:108`） | 修改 | 新增 `net_check: NetCheck`，只在开启时写出 `"net_check": {"level","country","clash_tun","clash_global"}` |
| `config.py` `Config` | `:124-189`（`__init__` `:124`、`copy` `:173`、`to_dict` `:178`） | 修改 | 新增 `clash_api: Optional[ClashApi]`，只在设置时写出 |
| `config.py` 解析 | `_parse_account` `:375-408`、`parse_config` `:224-284` | 修改 | 解析校验 `net_check`（兼容旧字段 `clash`）、`clash_api` |
| `config.py` `validate_env_key` | `:465-477` | 修改 | 保留名加入 `MULTI_CLAUDE_SKIP_NETCHECK` |
| `config.py` 新增 | `:530` `_check_port` 之后 | 新增 | `NetCheck`、`ClashApi`、`NET_CHECK_LEVELS`、`NETCHECK_SKIP_ENV`、`normalize_country`、`normalize_clash_address` |
| `launcher.py` `render` | `:163-197`（插入点 `:183` `_identity_lines` 之后、`:184` `unset AUTH_OVERRIDE_ENV` 之前） | 修改 | 新增只能按关键字传的 `net_check: bool = False`，为真时插入检查段 |
| `accounts.py` `render_launcher` | `:55-60` | 修改 | POSIX 分支传 `net_check=account.net_check.active`；`_plan_launcher` `:253` 与 `launcher_status` `:356` 都经它，自动一致 |
| `accounts.py` 退出码 | `:29-33` | 新增 | `EXIT_NETCHECK = 5` |
| `netcheck.py` | `:222`、`:262`、`:293`、`:469`、`:180` | 修改 | `_account_env` / `_parse_proxy` / `_route` / `_tunnel` / `_settings_path` 增加公开别名（`account_route_env` 等），原名保留；不改逻辑 |
| `platform.py` | `:871` `parse_win32_processes` 之后 | 新增 | `list_process_argv()`、`split_ps_command()`（列出所有用户的进程参数；macOS 用 `ps -axww -o command=`，因 Clash Verge 内核属主是 root，现有 `_list_processes` 只看同 uid） |
| `cli.py` `main` | `:624-627`（`launch` 拦截之后） | 新增 | 隐藏子命令 `netgate NAME` 在 argparse 前拦截 |
| `cli.py` 命令分组 / 示例 / 参数 | `COMMAND_GROUPS` `:44-72`、示例字典（`:79` 起）、`build_parser` `:313-520` | 新增 | `netcheck`（Account settings 组）、`clash-api`（Setup and checks 组）及每个选项的示例（`tests/test_help_examples.py` 要求覆盖全部选项） |
| `cli.py` 只读分派 | `:668-720` 区 | 新增 | `netcheck NAME`（无修改项）与 `clash-api`（无参）只读、不加锁 |
| `cli.py` 加锁前 | `:721-723` 之间 | 新增 | `_prepare_clash_find(args)`：`clash-api auto` 或开启 Clash 而无接口时，加锁前跑完查找 |
| `cli.py` `dispatch` | `:765` 起 | 新增 | `netcheck` / `clash-api` 写形态，`converge` 后打印结果 |
| `cli.py` `_load_apply_file` | `:1581-1621` | 修改 | `clash_api` 是本机状态：沿用当前值，文件里写了就忽略并 warn |
| `cli.py` 入口拦截 | `cmd_run` `:1155` 与 `_exec_as`、`cmd_launch` `:1220-1221` 之间、`cmd_code` `:1281-1282` 之间、`_account_process_env` `:1098-1111` | 修改 | 账号可用之后调用 `_net_gate(config, account, hint)`；`_account_process_env` 加可选参数 `gate_hint`，`cmd_login` `:1338` 传入、`cmd_mcp` `:1322` 不传 |
| `completion.py` | `:20-21` 子命令位置参数表 | 修改 | `netcheck` 补全账号名、档位与选项；`clash-api` 补全 `auto` / `off` |
| `README.md` / `README.zh-CN.md` | 命令节、退出码表、Windows 节、安全说明 | 修改 | 新命令、局限、退出码 5、跳过变量 |
| `SECURITY.md` | `:36-62` | 修改 | 开启检查的账号每次启动会联网（trace、本机 Clash 接口），不带凭据 |
| `CHANGELOG.md` | Unreleased | 修改 | 记一条 |
| `.github/workflows/ci.yml` | `:60` | 修改 | Windows 步骤加跑 `test_launchgate` |
| `tests/helpers.py` | 基础环境 `self.env`（`:181` 起）、`run_cli` `:255` | 修改 | 基础环境默认把 `MULTI_CLAUDE_TEST_CLASH_PROCESSES` 指向空列表文件，避免任何测试扫到开发机上真实运行的 Clash；`run_cli` 加可选 `input`，未给时标准输入为空 |
| `tests/test_help_examples.py` | E1 子命令数、`SHELL_OPERATORS`、`ALLOWED_EXIT`、`prepare`、`run_example` | 修改 | 子命令数 28 → 30；示例里的 `< 文件` 按标准输入重定向处理；`clash-api auto` 在沙箱里找不到 Clash，允许退出 1 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 配置

- `NetCheck(level="off", country="US", clash_tun=False, clash_global=False)`，`active = level != "off" or clash_tun or clash_global`；`level` 非法、`country` 不匹配 `^[A-Za-z]{2}$`、布尔字段不是布尔 → `ConfigError`；国家存大写。旧字段 `"clash": true` 读作两项都开，新旧同时出现以新为准；写出一律用新字段。`active` 为假时不写出（未开启账号的配置逐字节不变）。`Account.copy` 带上它（`rename` 与 `config.copy()` 经它；`migrate-default` 新建 `Account`（`migrate.py:289`），用默认值即可：被迁移的默认目录本来没有检查设置）。
- `ClashApi(address, secret)`；`normalize_clash_address`：`HOST:PORT` / `http://HOST:PORT` → `http://HOST:PORT`；`unix:/绝对路径`（仅 POSIX）；`\\.\pipe\名` 或 `pipe:\\.\pipe\名`（仅 Windows）；`https://` 拒绝。
- 跳过变量 `MULTI_CLAUDE_SKIP_NETCHECK`（常量放 `config.py`，cli 不导入 `launchgate` 也能用）。

#### 5.1.2 检查（`launchgate.py`）

- 检查项顺序与结构同 multi-codex `run_checks`：`clash` → `proxy-port` → `direct-country` → `proxy-country`；`CheckResult(item, ok, detail, elapsed_ms)`；超时：端口 3 秒、trace 8 秒、Clash 3 秒；不重试。
- 端口与国家按 §3 判定规则，代理取自“实际代理”。`detail` 里代理只写 `scheme://host:port` 与变量名、来源（`launcher` / `shell` / `settings.json`），不写用户名密码（同 `netcheck.py` 输出纪律）。
- 直连请求用 `http.client.HTTPSConnection`（不读环境代理）；经代理：TCP 连代理 → `netcheck._tunnel` → `ssl` 包装（`server_hostname=api.anthropic.com`）→ 发 `GET /cdn-cgi/trace`，读响应（上限 64 KiB），状态非 200 / 无 `loc` / `loc` 不等 → 失败。TLS 证书校验失败附“Python 找不到系统 CA 证书”提示；实际 env 里设了 `NODE_EXTRA_CA_CERTS` 且文件可用时一并加载（与 `netcheck._usable_ca` 一致，公司代理换证书的场景）。
- Clash：`read_clash_state(address, secret)` 返回 `(mode, tun)`，TCP / unix 套接字 / 命名管道三种取法与 401、非 200、缺字段的报错同 multi-codex；`clash_tun` 要求 `tun is True and mode != "direct"`，`clash_global` 要求 `mode == "global"`；未设接口 → 失败，提示 `multi-claude clash-api auto`。
- `gate(config, account, hint)`、`skip_mode()`、通过行 / 失败文案、`scope_text`、Windows 两行跳过写法、`KeyboardInterrupt` → 130：同 multi-codex，前缀 `multi-claude:`，跳过变量 `MULTI_CLAUDE_SKIP_NETCHECK`（只接受 `1`：打印一行后跳过；值先 `strip()`，其它值视为未设置）。multi-codex 的 `quiet` 档只供它的 `usage --live` 使用，multi-claude 没有该命令，不实现。失败文案示例：

```text
multi-claude: network check failed for account work (strict, US): proxy-country: loc is JP, expected US (via http://127.0.0.1:7905 from HTTPS_PROXY (launcher)) (412 ms)
  passed: proxy-port, direct-country
  skip once: MULTI_CLAUDE_SKIP_NETCHECK=1 claude-work
  turn off:  multi-claude netcheck work off
```

- 测试注入（只在 `MULTI_CLAUDE_TEST_MODE=1` 时生效，与 `netcheck.TEST_TARGETS_HOOK` 同一约定）：`MULTI_CLAUDE_TEST_TRACE=<host>:<port>` 替换 trace 目标，`MULTI_CLAUDE_TEST_CA=<pem>` 替换 CA。

#### 5.1.3 POSIX 启动命令

`render(..., *, net_check=False)` 为真时在 `_identity_lines` 之后、`unset AUTH_OVERRIDE_ENV` 之前插入：

```sh
if ! command -v multi-claude >/dev/null 2>&1; then
  echo 'multi-claude: multi-claude not found in PATH; it runs the network check of this account' >&2
  exit 127
fi
(cd / && exec multi-claude netgate 'work') || exit $?
unset MULTI_CLAUDE_SKIP_NETCHECK
```

- 放在目录检查与 default 身份链接检查之后，报错顺序为“目录 → 链接 → 网络检查 → claude 是否存在”；放在设置环境之前，`netgate` 看到的是父环境（§3）。`cd /` 原因同 multi-codex（`python -m` 包装会把当前目录放进模块搜索路径）。
- 为假时输出与基线逐字节相同（`tests/test_platform.py` 等现有启动命令断言不变）。只有“开启 ↔ 关闭”会改变启动命令内容，由 `netcheck` 写形态经 `converge` 重写；`launcher_status` 按同一 `render_launcher` 计算期望内容，开启后旧启动命令显示 stale，`apply` 可修。
- `claude-auto`（`routes.py:129` `exec "$target"`）执行的就是账号启动命令，自动覆盖。

`netgate NAME`（`cli.py` `main` 在 `launch` 拦截之后、argparse 之前）：读配置、找账号；不存在 → 退出 1；`_net_gate(config, account, "claude-" + name)`，失败 5、通过 0、配置错误 1。

#### 5.1.4 Python 内的入口

`_net_gate(config, account, hint) -> Optional[int]`：账号未开启检查、或账号目录不存在（交给原有路径报错）→ `None`，不导入 `launchgate`；否则 `launchgate.gate`，失败打印并返回 5，中断返回 130。

| 入口 | 插入位置 | hint |
| ---- | ---- | ---- |
| `claude-<名>.cmd` / `claude-auto.cmd` | `cmd_launch` `:1220` default 链接检查之后、`:1221` 组命令之前 | `claude-NAME` |
| `run` | `_exec_as`（`:1237` 起）加可选参数 `gate_hint`，在 `_usable_account_env` 成功之后、找命令之前检查；`cmd_run` `:1155` 传入，`cmd_code` 不传（它在 `:1281` 后自己检查） | `multi-claude run NAME` |
| `code` | `cmd_code` `:1281` `_usable_account_env` 成功之后、`:1282` 找 code 之前（不建用户数据目录就拒绝） | `multi-claude code NAME` |
| `login` | `_account_process_env(name, gate_hint)` 内 `_usable_account_env` 成功之后、找 claude 之前；`cmd_login` `:1338` 传 hint | `multi-claude login NAME` |

`run` 的 `_exec_as` 在 POSIX 上直接 exec 目标命令、不经启动命令，所以必须在 Python 里检查；`gate` 检查后从 `os.environ` 删掉跳过变量，但 `_usable_account_env` 已先算出 `process_env`，所以 `_net_gate` 返回后调用方再从 `process_env` 里删除该键（`run` / `code` / `login` / `launch` 四处），子进程不继承“跳过”。

#### 5.1.5 命令

```text
multi-claude netcheck NAME                       # 显示设置并立即检查一次（逐项列出，不在第一个失败处停）
multi-claude netcheck NAME off|basic|strict [--country CC] [--clash on|off] [--clash-tun on|off] [--clash-global on|off]
multi-claude clash-api                           # 显示（secret 只显示 set / not set）
multi-claude clash-api ADDRESS [--secret-stdin]
multi-claude clash-api auto
multi-claude clash-api off
```

- 只读形态退出码：未开启 0；全部通过 0；有失败 5。写形态支持 `--dry-run` / `-v`，进 `dispatch` + `converge`。
- 设置时：`--clash` 与 `--clash-tun` / `--clash-global` 同时给 → `UsageError`；只开全局不开 TUN → warn；开启 Clash 而无接口 → 加锁前自动查找一次，找不到只 warn。与 multi-codex 不同：**不在设置时因代理是 `inherit` / `off` 拒绝**（实际代理可能来自终端或 settings.json，只能在启动时判定）；设置完成后若此刻按 §3 判定规则第 0–3 条算不出可用线路（读不出 settings.json、没有代理、SOCKS / 写错、被 `NO_PROXY` 绕过），或 strict 档遇到非 `http://` 代理，只 warn（`launchgate.setting_warning`）。
- `--country` 在非 strict 档也照存，切到 strict 时生效；未开启检查（`active` 为假）时给 `--country` 提示“不会保存”。

### 5.2 接口变更

- 新增子命令 `netcheck`、`clash-api`；隐藏子命令 `netgate`（两平台都接受，只由 POSIX 检查版启动命令调用，不进帮助与补全）。
- 新增退出码 5：网络检查未通过、拒绝启动（`claude-<名>`、`claude-auto`、`run`、`login`、`code`、`netcheck NAME`）。
- 新增环境变量 `MULTI_CLAUDE_SKIP_NETCHECK`（`1` 跳过一次）。
- `config.json`：账号可选字段 `net_check`，全局可选字段 `clash_api`；未使用时文件内容不变。旧版 multi-claude 读到新字段会忽略（`_parse_account` 只取已知键），即旧版视为未开启检查，且旧版改任何设置会把这两个字段丢掉；CHANGELOG 写明不要在同一台机上混用版本。
- 开启检查的账号的 POSIX 启动命令多一段检查（内容见 5.1.3）。
- 无 `docs/reference/*` 改动，sibling 回补检查不适用。

## 6. 备选方案与决策

1. **检查账号代理设置（multi-codex 做法） vs 实际代理**：用户选实际代理，理由是 settings.json 的 env 会盖过启动命令的代理，只查设置可能放行一条 Claude 不走的线路。
2. **POSIX 启动命令整体改走 Python vs 只插一段子进程检查**：同 multi-codex 决策，后者让未开启账号逐字节不变、开启账号的环境设置仍由 sh 完成。
3. **复用 `doctor --network` 的 `run()` 直接判通断 vs 新写 trace 请求**：`run()` 只发 `HEAD /` 判连通、不取国家，且一次测全部账号、总时限 20 秒，不适合每次启动；只复用其中的代理解析与 CONNECT 函数。

## 7. 影响分析

1. **未开启检查的账号**：`render` 不插段（快照不变）；`_net_gate` 首行判 `active` 后返回，不导入 `launchgate`、不联网；`config.json` 不写新字段。→ R-A1、R-A2。
2. **`netcheck.py` 私有函数加公开别名**：只加名字、不改逻辑；`doctor --network` 输出不变。→ 现有 `tests/test_netcheck.py` 全过。
3. **启动耗时**：开启 basic 的账号每次启动多一次本机 TCP 连接加一次 Python 进程（约 100～200 ms）；strict 多两次 HTTPS（通常 0.3～2 秒，最长约 8 秒×2）；Clash 一次本机请求。通过行打印实际毫秒。
4. **跳过变量的继承**：开启检查的账号的启动路径不让 Claude 继承它——POSIX 由 sh `unset`，Python 入口从 `os.environ` 与 `process_env` 删除。以下情况变量会留在环境里，嵌套会话中再启动开启检查的账号时会被静默跳过：未开启检查的账号（启动命令不插段，`_net_gate` 首行返回）；`claude-auto` 无目标、直接 `exec claude` 的路径（`routes.py:129-135`）；账号 env 自己写了该变量（POSIX 的 env 行在 `unset` 之后会把它导出）。账号 env 写该变量由 `validate_env_key` 新增的单独分支拒绝（文案 `MULTI_CLAUDE_SKIP_NETCHECK is reserved; set it on the command line for one run`，不复用身份键分支的文案）；该函数在读配置时也执行（`config.py:439`），升级前已写了该变量的配置升级后会读不出，因变量是本版新增，实际不会出现，CHANGELOG 不另写。另有一种不拦截的情况：账号 `settings.json` 的 `env` 写了该变量，Claude 会把它传给子进程（`_SETTINGS_KEYS` 不含它，本工具不改用户的 settings.json），README 局限节写明。→ R-B5、R-C4。
5. **嵌套运行**：在已开启检查账号的 Claude 会话里运行另一个开启检查账号的启动命令，会再检查一次；这是预期（不缓存结果）。
6. **配置兼容**：旧版读新配置当作未开启（§5.2）；`apply -f` 文件里的 `clash_api` 被忽略，`net_check` 按文件生效（与 `proxy` 等账号字段一致）。→ R-C3、R-C4。
7. **Windows**：`cmd_launch` 在 Python 内检查，不改 `.cmd` 内容；命名管道读取在守护线程里、主线程限时等待。→ CI Windows 跑 `test_launchgate`。
8. **安全**：联网只发不带凭据的 `GET /cdn-cgi/trace` 与本机 `GET /configs`；secret 只从标准输入读，不出现在命令行与输出里。→ R-B6。

## 8. 回归测试

### 8.1 自动测试（`tests/test_launchgate.py`，三平台）

照 multi-codex `tests/test_netcheck.py` 的结构与假服务器（假 HTTP 代理、假 trace（TLS，测试证书）、假 mihomo（TCP / unix / 管道））改写，至少覆盖：

- R-A1 未开启账号：`render` 输出与基线相同；`config.json` 无 `net_check`；`run` / `launch` 不导入 `launchgate`（`sys.modules` 断言）。
- R-A2 开启 / 关闭 `netcheck` 后启动命令被重写 / 还原，`list` 不报 stale。
- R-B1 basic：代理端口开 → 通过行；关 → 退出 5、失败文案含 `skip once` 与 `turn off`；`NO_PROXY` 含 `api.anthropic.com` → basic 同样失败。
- R-B2 实际代理：settings.json 损坏 → `proxy-port` 失败并写出路径；设了 `ANTHROPIC_BASE_URL` → 通过行带 `checked api.anthropic.com only`；账号 `inherit` + 终端有代理 → 查终端代理；settings.json 的 `env.HTTPS_PROXY` 覆盖启动命令代理 → 查 settings 的端口、文案标 `settings.json`；settings 把代理设成空 → `no proxy in effect`；SOCKS → 失败说明。
- R-B3 strict：直连与经代理 `loc` 相同且匹配 → 过；经代理 `loc` 不同 → `proxy-country` 失败；代理 407 → 失败说明认证；`https://` 代理 → 失败说明。
- R-B4 Clash：global+TUN 过；`rule` 模式下 `--clash-global` 失败、`--clash-tun` 过；TUN 关失败；`direct` 下 `--clash-tun` 失败；401；未设接口提示 `clash-api auto`；旧字段 `"clash": true` 兼容。
- R-B5 `MULTI_CLAUDE_SKIP_NETCHECK=1` 跳过并打印一行；` 1 `（带空格）同样生效；`quiet` 等其它值不跳过。全部入口（`claude-<名>`、`claude-auto`、`run`、`login`、`code`、Windows `launch`）启动的假 `claude` / `code` 环境里都没有该变量。
- R-B6 `clash-api 127.0.0.1:9097 --secret-stdin` 写入、`clash-api` 显示 `secret set` 且不打印 secret；`clash-api off` 清除；`clash-api auto` 用假进程参数 / 假配置文件找到可用候选（`clashfind` 单测照搬）。
- R-C1 入口全集：`claude-<名>`（POSIX 真跑 sh）、`claude-auto`、`run`、`login`、`code`、`netgate`、Windows `launch` 都在失败时退出 5 且未启动假 `claude` / `code`；`mcp` 不检查。
- R-C2 报错顺序：账号目录不存在时先报目录错误、退出 1，不做网络检查。
- R-C3 `apply -f` 文件含 `clash_api` → warn 并沿用当前值；含 `net_check` → 生效。
- R-C4 配置解析：非法 level / country / 布尔、非法 clash 地址 → `ConfigError`；国家转大写；`env work MULTI_CLAUDE_SKIP_NETCHECK=1` 被拒。
- R-C5 `--help` 示例覆盖新选项（现有 `tests/test_help_examples.py` 自动纳入）；补全脚本含 `netcheck` / `clash-api`。

全量：`python -m unittest discover -s tests -t tests`、pyflakes 与 CI 的 README 链接检查本地先跑（项目记忆“推送前本地跑 CI 的 lint”）。

### 8.2 本机实测（macOS，当前 Clash Verge 服务模式）

- R1 `multi-claude netcheck <测试账号> strict`：直连与经代理都返回 `loc=US`，通过行打印耗时。
- R2 `multi-claude clash-api auto` 找到 `/var/run/clash-verge-service/users/501/verge-mihomo.sock`（或当时实际接口），`netcheck <账号> --clash on` 后 `netcheck <账号>` 显示当前模式与 TUN 状态，判定与 Clash 界面一致。
- R3 把账号代理改成无人监听的端口，`claude-<账号> --version` 退出 5；`MULTI_CLAUDE_SKIP_NETCHECK=1 claude-<账号> --version` 正常输出版本。
- R4 测试完把账号设置还原（`netcheck <账号> off --clash off`、代理改回），`list` 无 stale。

Windows 实机（`.cmd` 入口、命名管道 Clash 接口）本次只靠 CI 的 Windows 单测，不做实机；需要时另约测试机。

## 9. 日志 / 观测点

- stderr：通过行 `multi-claude: network check passed for account <名> (<范围>; <毫秒> ms)`；失败多行（项、详情、已通过项、跳过与关闭命令）；跳过行 `network check skipped for this run (MULTI_CLAUDE_SKIP_NETCHECK=1)`；中断行 `network check interrupted`。
- 退出码 5 只表示“网络检查未通过”。
- `netcheck NAME` 逐项 `ok|fail <项> <详情> (<毫秒> ms)`。
- `doctor` 不新增检查项（非目标）。

## 10. 开放问题

无。
