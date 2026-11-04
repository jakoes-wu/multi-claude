# doctor --network：逐账号网络自检

> 2026-11-03 注记：代码已落地（未提交）：`src/multi_claude/netcheck.py`（`run`、`_plan_account`、`_attempt`、`_tunnel`、`_format`），入口 `doctor.py` `run_checks(network)`、`cli.py` `--network`；测试 `tests/test_netcheck.py`。代码评审第 1 轮可合并（0 阻塞 / 0 高 / 0 中 / 8 低，低级问题已全部修复）。基线：`origin/main` `f440e5e`（v0.15.0 之后，PR #58 合并点），分支 `feature/network-check`。
>
> 2026-11-03 第 1 轮设计评审（1 高 8 中 7 低）后修订：D1 `settings.json` 的 `env` 覆盖启动环境（原写反）；D2 走代理的测试目标改用假主机名；D3 帮助示例测试；D4 新增 `proxy_upstream_timeout` / `proxy_bad_reply` 与按阶段的总时限结论；D5 缺端口补默认端口、先解析代理值再看 `NO_PROXY`；D6 不显示无效代理原值；D7 去重键含认证头；D8 绕过代理时按直连结果定级；D9 自定义 API 地址或云厂商通道时加提示；D10 `settings.json` 读不出时提示；D11 行号；D12 文档核对范围；D13 超时异常兼容 3.8/3.9；D14 自签证书端到端用例；D15 修法按代理来源；D16 https 代理自查方法。
>
> 2026-11-03 第 2 轮设计评审（0 高 2 中 4 低）后修订：D17 T7 写明变量名，补“只设大写被小写压住”一例，修法写实际生效的变量名；D18 去重键改为目标协议 + 主机 + 端口；D19 其它协议归 `invalid_proxy`；D20 https 代理同样先看 `NO_PROXY`；D21 无状态码时的文案；D22 探测线程意外异常归 `internal_error`；附注：T8 目标用 `127.0.0.1`、经代理超时的修法。
>
> 2026-11-03 第 3 轮设计评审：收敛（0 高 0 中 2 低）。两个低级问题已顺手修：D23 引用函数名改为 `_query_into`；D24 T8 改用 `inherit` 模式。

## 1. 背景

用户要求在 multi-claude 里加一项“每个账号的网络自检”：按每个账号启动时实际使用的网络线路，检查能否连上 Claude Code 所需的 Anthropic 服务；连不上时说明卡在哪一步、怎么修。用户 2026-11-03 选定“在 `doctor` 里加一个需要手动打开的联网检查”（原话“按做法一实施”）。

现状事实：

- `doctor`（`src/multi_claude/doctor.py:32` `run_checks`）只做本地检查，不联网、不读凭据（`doctor.py:1-7` 模块说明）。
- 目前唯一的联网代码是 `plan.py`，只有 `list` 调用（`plan.py:3` 模块说明）。它按账号环境（`env.account_env`，`env.py:15`）取代理，连 `api.anthropic.com` 查套餐（`plan.py:41`），但必须先读到访问令牌（`plan.py:316-319`，未登录直接返回 `no_login`），失败只归为几个英文代号。
- `plan.send`（`plan.py:420-457`）用 `http.client` 的 `set_tunnel` 建隧道。代理端口没开（连接被拒）与代理收到 CONNECT 后回非 200（如 502）在 Python 里都抛 `OSError`（`http.client.HTTPConnection._tunnel` 中 `raise OSError(f"Tunnel connection failed: ...")`，本机 Python 源码已核），被同一个 `except OSError` 归为 `connect_failed`（`plan.py:452`）。用户看不出该去开代理软件还是该换线路。
- README 两份与 SECURITY.md 都写着“只有 `list` 联网”（`README.md:14`、`:42`、`:55`；`README.zh-CN.md:14`、`:42`、`:55`；`SECURITY.md:50-52`）。

官方依据（2026-11-03 读取）：

- https://code.claude.com/docs/en/network-config ：
  - `api.anthropic.com` 用于 Claude API 请求；`platform.claude.com` 用于 OAuth 令牌交换、续期与撤销，claude.ai 与 Console 登录都需要它。Claude Code 首次启动的连通性检查在连不上这两个地址时指向这一节。
  - 代理变量按 `https_proxy`、`HTTPS_PROXY`、`http_proxy`、`HTTP_PROXY` 顺序取第一个已设置的；`NO_PROXY` 支持逗号或空格分隔、`*` 表示全部绕过；不支持 SOCKS；代理可带 `user:password@` 基本认证。
  - 证书：同时信任自带的 Mozilla 根证书和操作系统证书库，另可用 `NODE_EXTRA_CA_CERTS` 追加。
  - 代理 URL 无法解析（如缺 `http://`）时 Claude Code 启动即报错退出。
- https://code.claude.com/docs/en/env-vars “Precedence”：“When the same variable is set in both your shell and a settings file `env` block, the settings file value applies in most sessions. Claude Code writes each `env` entry into the process environment, replacing the value inherited from the shell.” 例外是 Claude Desktop 等少数会话形态。设置文件不能删变量，只能设为空字符串（官方举例用于云厂商选择：“treats the empty value as unset for provider selection”）。托管设置（managed settings）优先于用户和项目设置。

## 2. 目标 / 非目标

目标：

1. `multi-claude doctor --network`：对每个账号，按它启动时会用的代理（或直连），分步连接 `api.anthropic.com` 与 `platform.claude.com`，报告能否连通、耗时，以及失败卡在哪一步。
2. 失败结果区分：代理地址连不上、代理需要认证、代理连不出去、代理迟迟不回、代理回的不是 HTTP、域名解析失败、直连连不上、超时、加密连接被掐断、证书无法验证、代理写法 Claude Code 不支持；被 `NO_PROXY` 绕过时注明。每条附按代理来源给出的修法。
3. 不加 `--network` 时 `doctor` 的行为、输出、退出码与现在完全相同。

非目标：

- 不判断账号状态、套餐、额度、登录是否有效；不读任何凭据（不读钥匙串、不读 `.credentials.json`）。
- 不查出口 IP 所在国家或地区，不连 Anthropic 以外的任何服务。
- 不检查官方表里的其它地址（`claude.ai`、`downloads.claude.ai`、`registry.npmjs.org` 等）。
- 不检查 `ANTHROPIC_BASE_URL` 指向的自定义地址，也不检查 Bedrock / Vertex / Foundry 通道；这类账号照常检查两个官方地址，另加一条 warn 说明结果不代表实际通道（§5.1.4 `custom_endpoint`）。
- 不改 `list` / `plan.py` 的查询与失败分类。
- 不检查 https 代理（向代理本身做 TLS 再在里面套 TLS）：报 warn“未检查”。
- 不模拟 `--settings` 固定参数、托管设置、项目级设置、Claude Desktop 会话：只读账号自己的 `settings.json`，结果里注明代理来源。

## 3. 假设与约束

- 只用 Python 标准库，兼容 Python 3.8（`pyproject.toml` 的 `requires-python`）。
- 出站路径与 `claude-<名称>` 一致：先取 `env.account_env(config, account, os.environ)`（启动命令导出的环境），再用账号 `settings.json` 的 `env` **覆盖**同名变量（官方 Precedence 规则，见 §1）。default 身份读 `~/.claude/settings.json`，其它账号读账号配置目录下的 `settings.json`，判定方式同 `plan.py:377-378`。
- Windows 上环境变量名不分大小写，`account_env` 只写大写键（`env.py:52-69`）；合并 `settings.json` 时按大写名比较替换。Claude Code 自身在 Windows 上的合并方式未核实，按 Windows 环境变量语义处理（§10 Q2）。
- 不用 urllib 的 `ProxyHandler`：理由同 `plan.py:9-11`（会读 macOS 系统代理例外清单）。
- 证书用 `ssl.create_default_context()`，合并后环境里有 `NODE_EXTRA_CA_CERTS` 时追加加载该文件。Python 读的根证书与 Claude Code 不完全相同，所以“证书无法验证”只报 warn。
- 每次套接字操作超时 10 秒（同 `plan.SOCKET_TIMEOUT_S`，`plan.py:57`），全部检查总时限 20 秒；连接类失败重试 1 次、间隔 0.5 秒（同 `plan.py:61-63` 的理由）。超时异常统一捕获 `(socket.timeout, TimeoutError)`（3.8/3.9 二者不同类，3.10 起为别名）。
- 测试模式（`MULTI_CLAUDE_TEST_MODE=1`，`platform.py:47`）下必须显式给出本地测试目标，否则不发请求（同 `plan._endpoint`，`plan.py:382`）。

## 4. 涉及模块

| 区域 | 行号锚点（基线 f440e5e） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/netcheck.py` | 新文件 | 新增 | 环境合并、线路解析、分步探测、结果分级与文案 |
| `src/multi_claude/doctor.py` | `:32` `run_checks` | 修改 | 增加参数 `network: bool = False`；为真且配置可读时在末尾追加 `netcheck.run(config)` 的结果 |
| `src/multi_claude/doctor.py` | `:1-7` 模块说明 | 修改 | 补一句：`--network` 时联网，只连两个官方地址，不读凭据 |
| `src/multi_claude/cli.py` | `:216-220` `doctor` 示例 | 修改 | 加 `multi-claude doctor --network` 示例 |
| `src/multi_claude/cli.py` | `:397-399` doctor 参数 | 新增 | `--network` 选项 |
| `src/multi_claude/cli.py` | `:652-658` 分派 | 修改 | 传入 `args.network` |
| `src/multi_claude/cli.py` | `:2511-2522` `cmd_doctor` | 修改 | 增加 `network` 参数并传给 `doctor.run_checks` |
| `src/multi_claude/completion.py` | `:50-64` 从 `parser._actions` 取选项 | 不改 | 补全自动包含 `--network`，§8 T16 顺带核对 |
| `src/multi_claude/plan.py` | `:3` 模块说明 | 修改 | “唯一联网”改为“`list` 的联网代码；另一处联网是 `netcheck.py`（`doctor --network`）” |
| `tests/test_netcheck.py` | 新文件 | 新增 | §8 用例 |
| `tests/test_help_examples.py` | `:36` `ALLOWED_EXIT` | 修改 | 登记 `multi-claude doctor --network` 允许退出码 1（沙箱测试模式无目标，每项为 `test_no_endpoint`） |
| `README.md` | `:14`、`:42`、`:55`；doctor 段 `:479`、`:584` | 修改 | 联网说法改为“`list` 与 `doctor --network`”；doctor 段补 `--network` |
| `README.zh-CN.md` | `:14`、`:42`、`:55`；doctor 段 `:471`、`:576` | 修改 | 同上 |
| `SECURITY.md` | `:50-52` | 修改 | 加 `doctor --network` 的说明：连哪两个地址、不读凭据、发什么请求 |
| `CHANGELOG.md` | `:7` `[Unreleased]` | 新增 | Added 一条 |
| `docs/analysis/roadmap.md` | 功能表 | 新增 | 一行，指向本方案 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 入口与输出

- `multi-claude doctor --network [--json] [--verbose]`。`--network` 为假时，`doctor.run_checks` 不调用 `netcheck`，输出与现在逐字节相同。
- 配置读不出（`config is None`，`doctor.py:38-39` 提前返回）时不做网络检查，已有的 config error 项已说明原因。
- 每个账号 × 每个目标地址产出一条 `Check`：`id = "network"`，`subject = 账号名`，`level` 与 `message` 见 §5.1.4。账号级提示（`settings_unreadable`、`custom_endpoint`、`invalid_proxy`、`unsupported_socks`）每个账号一条，`subject` 同为账号名。`--verbose` 时显示 ok 项（沿用 `format_text`，`doctor.py:204`）。
- `--json` 沿用 `to_dict`（`doctor.py:217`），`schema_version` 仍为 1。退出码沿用 `exit_code`（`doctor.py:213`）：有 error 返回 1。
- `netcheck.run` 内部不向外抛异常：账号级意外异常归为该账号一条 `internal_error`（error，不含异常文本），保证 `--json` 输出始终是合法 JSON（`cmd_doctor` 只捕获 `OSError`，`cli.py:2511-2522`）。
- 开始联网前在 stderr 打一行：`checking the network for N account(s) (api.anthropic.com, platform.claude.com)...`，`--json` 时也打在 stderr。

#### 5.1.2 每个账号的环境与线路（`netcheck._account_env`、`netcheck._route`）

**环境合并**：`env = account_env(config, account, os.environ)`；读账号 `settings.json`，其 `env` 为对象时，对其中以下键用设置文件的值覆盖 `env`（Windows 按大写名比较）：四个代理变量、`NO_PROXY`/`no_proxy`、`NODE_EXTRA_CA_CERTS`、`ANTHROPIC_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX`、`CLAUDE_CODE_USE_FOUNDRY`。值不是字符串的键忽略。设置文件中值为空字符串的键照样覆盖为空（后续“取第一个非空值”自然视为未设置）。其它键一律不读出、不保存。

- `settings.json` 不存在：静默跳过。
- 存在但读取或解析出任何异常（`OSError`、`UnicodeDecodeError`、`ValueError`）或顶层 / `env` 不是对象：跳过合并，并为该账号产出一条 `settings_unreadable` warn。

每个代理值记录来源与实际生效的变量名（如 `https_proxy`）：`settings.json`（被设置文件覆盖）、`launcher`（账号配置了代理端口或 URL，由 `claude-<名称>` 设置）、`shell`（`proxy inherit`，来自当前终端）。

**按目标主机解析**（顺序固定）：

1. 取第一个非空的 `https_proxy`、`HTTPS_PROXY`、`http_proxy`、`HTTP_PROXY`（同 `plan._PROXY_VARS`，`plan.py:87`）。没有 → 直连。
2. 解析代理值（先于 `NO_PROXY`：代理值写错时 Claude Code 启动即失败，与是否绕过无关）：
   - scheme 为 `socks`、`socks4`、`socks4a`、`socks5`、`socks5h` → `unsupported_socks`；
   - 无 scheme、或缺主机 → `invalid_proxy`；
   - 端口缺省时补默认端口（`http` 80、`https` 443）；端口不是合法整数 → `invalid_proxy`；
   - 其它 scheme（如 `ftp`）→ `invalid_proxy`；
   - `https` → 记为 https 代理（第 3 步未被绕过时报 `https_proxy_unchecked`）；
   - `http` → 可检查；带 `user:password@` 时生成 `Proxy-Authorization: Basic`（同 `plan.py:416`）。
3. 代理为 http 或 https 时看 `NO_PROXY`/`no_proxy`：空格换成逗号后交给 `urllib.request.proxy_bypass_environment(host, {"no": value})`（同 `plan.py:408`）。命中 → 直连探测，标记 `bypassed`；未命中且为 https 代理 → `https_proxy_unchecked`，不探测。

`unsupported_socks`、`invalid_proxy` 只产出账号级一条 error，不再探测；消息只显示变量名与来源，不显示原值（原值可能含密码）。

#### 5.1.3 分步探测（`netcheck._probe`）

目标：`https://api.anthropic.com/`、`https://platform.claude.com/`，端口 443。线程在推进时把当前阶段写入共享状态，失败时记录所在阶段：

| 阶段 | 直连 | 经 http 代理 |
| ---- | ---- | ---- |
| 1 解析 | `socket.getaddrinfo(host, 443)`；`gaierror` → `dns_failed` | 解析代理主机：`gaierror` → `proxy_dns_failed` |
| 2 建立 TCP | `socket.create_connection`；拒绝或其它 `OSError` → `connect_failed`；超时 → `timeout` | 连代理：拒绝 / `OSError` → `proxy_unreachable`；超时 → `proxy_timeout` |
| 3 隧道 | 无 | 手写 `CONNECT host:443 HTTP/1.1`（`Host`、可选 `Proxy-Authorization`），读状态行与头部（上限 64 KB）。200 → 继续；407 → `proxy_auth`；其它状态码 → `proxy_upstream_failed`（附状态码）；未回任何字节即关闭 → `proxy_upstream_failed`（无状态码）；首行不是 `HTTP/1.x <三位数字>` → `proxy_bad_reply`；超时 → `proxy_upstream_timeout` |
| 4 TLS | `ctx.wrap_socket(sock, server_hostname=host)`：`SSLCertVerificationError` → `cert_unverified`；`SSLEOFError`/`SSLZeroReturnError`/`ConnectionResetError` → `tls_closed`；其它 `SSLError` → `tls_failed`；超时 → `timeout` | 同左 |
| 5 应答 | 发 `HEAD / HTTP/1.1`（`Host`、`User-Agent: multi-claude/<版本>`、`Connection: close`；IPv6 地址加方括号），读到合法 HTTP 状态行即 `ok`（任何状态码都算连通）；读不到 → `no_response`；超时 → `timeout` | 同左 |

- 每次尝试发一次 `HEAD /`（失败可重试一次，见下条），不带任何凭据、Cookie 或账号信息；代理网址里的用户名密码只放进发给代理的 `Proxy-Authorization`。
- 重试：`connect_failed`、`proxy_unreachable`、`proxy_upstream_failed`、`tls_closed`、`no_response` 隔 0.5 秒重试一次；重试成功记为 `ok_after_retry`。
- 并发与去重：键为 `(代理主机, 代理端口, Proxy-Authorization 头原文或 None, 目标协议, 目标主机, 目标端口, NODE_EXTRA_CA_CERTS 路径)`，键只在内存中比较、不输出。同键只探一次，结果共享。每个键一个守护线程，线程体整体包 `try/except Exception`，意外异常为该键记 `internal_error` 并立即结束（同 `plan.py:293-305` 的 `_query_into`），不让主线程空等到总时限；主线程最多等总时限 20 秒。
- 总时限到期仍未完成的键，按线程最后记录的阶段出结论：阶段 1 → `dns_timeout`（直连）/ `proxy_dns_timeout`（代理），阶段 2 → `timeout` / `proxy_timeout`，阶段 3 → `proxy_upstream_timeout`，阶段 4、5 → `timeout`。`getaddrinfo` 无超时参数，DNS 卡住只能靠总时限，即此处的 `dns_timeout`。线程只做只读探测，放弃等待无副作用。
- 测试钩子：测试模式下只认 `MULTI_CLAUDE_TEST_NETCHECK_TARGETS`（逗号分隔的 `http://主机:端口` 或 `https://主机:端口`），未设置时不发请求、每项记为 `test_no_endpoint`；`http` 目标跳过阶段 4。`MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT` 同时替换套接字超时与总时限（同 `plan._endpoint` 的钩子习惯，`plan.py:382`）。

#### 5.1.4 结果分级与文案

消息格式：`<目标主机> via <线路>: <结论>; <修法>`。线路为 `direct`、`127.0.0.1:7901 (launcher)`、`proxy.example.com:8080 (shell)`、`… (settings.json)`；只显示 `主机:端口`，不显示用户名密码。

修法按代理来源给出（下表 `<修代理>`）：

- `launcher` → `start the proxy app, or change the port with: multi-claude proxy <名称> <端口>`
- `shell` → `start the proxy app, or fix <变量名> in your terminal`
- `settings.json` → `start the proxy app, or fix <变量名> in <settings.json 路径>`

`<变量名>` 是实际生效的那个变量（按 §5.1.2 第 1 步取到的），不固定写 `HTTPS_PROXY`。

| 结果 | 级别 | 结论与修法（英文，示意） |
| ---- | ---- | ---- |
| `ok` | ok | `reachable (0.42s)` |
| `ok_after_retry` | warn | `reachable on the second try; the connection is unstable` |
| `proxy_dns_failed` / `proxy_dns_timeout` / `proxy_unreachable` / `proxy_timeout` | error | `cannot reach the proxy at <host:port>; <修代理>` |
| `proxy_auth` | error | `the proxy asks for a user name and password (407)` |
| `proxy_upstream_failed` | error | `the proxy is running but could not reach <host> (<状态码>)`，无状态码时为 `the proxy closed the connection instead of reaching <host>`；修法均为 `switch to another proxy line or node` |
| `proxy_upstream_timeout` | error | `the proxy accepted the request but did not connect to <host> in time; switch to another proxy line or node` |
| `proxy_bad_reply` | error | `the proxy did not answer like an http proxy; check the port, or switch the proxy app to http mode` |
| `dns_failed` / `dns_timeout` | error | `cannot resolve <host>; check the network or DNS, or set a proxy`（被 `NO_PROXY` 绕过时不带 `or set a proxy`） |
| `connect_failed` / `timeout` / `no_response` | error | `cannot connect to <host>` / `timed out` / `no answer`；修法：直连时为“检查网络或改用代理”（被 `NO_PROXY` 绕过时只写 `check the network`，因为账号已配代理，问题在 `NO_PROXY`），经代理时为 `switch to another proxy line or node` |
| `tls_closed` | error | `the connection was cut during the secure handshake; the network or proxy may be blocking <host>` |
| `tls_failed` | error | `secure connection failed` |
| `cert_unverified` | warn | `Python could not verify the certificate; a network or app may be intercepting HTTPS. Claude Code also trusts the system certificate store and NODE_EXTRA_CA_CERTS, so it may still work` |
| `unsupported_socks`（账号级） | error | `<变量名> (<来源>) is a SOCKS proxy, which Claude Code does not support; use an http proxy` |
| `invalid_proxy`（账号级） | error | `<变量名> (<来源>) is not a valid proxy URL and Claude Code will fail to start; use http://host:port` |
| `https_proxy_unchecked` | warn | `https proxies are not checked by multi-claude; run claude --debug or /status to see whether it connects` |
| `ca_file_unreadable` | warn | `NODE_EXTRA_CA_CERTS points to a file that cannot be read`（仍按默认证书探测） |
| `settings_unreadable`（账号级） | warn | `could not read <settings.json 路径>; proxy settings in it were not considered` |
| `custom_endpoint`（账号级） | warn | `this account sends requests to <ANTHROPIC_BASE_URL 的主机> / Bedrock / Vertex / Foundry; the results below only cover api.anthropic.com and platform.claude.com` |
| `internal_error` | error | `unexpected error while checking`（不含异常文本，同 `plan.py:301-302`） |
| `test_no_endpoint` | error | 仅测试模式出现 |

- 被 `NO_PROXY` 绕过（`bypassed`）：级别与结论按直连结果定（ok / warn / error 不变），ok 时升为 warn；消息末尾都追加 `(NO_PROXY covers this host, so Claude Code connects directly instead of through <代理主机:端口>; remove it from NO_PROXY if that is not intended)`。
- `custom_endpoint` 判定：合并后环境里 `ANTHROPIC_BASE_URL` 非空，或三个 `CLAUDE_CODE_USE_*` 任一为非空且不是 `0`/`false`。`ANTHROPIC_BASE_URL` 只显示主机名。

### 5.2 接口变更

- 新增命令行选项：`multi-claude doctor --network`。不加时行为不变。
- `doctor --json`：新增 `id` 为 `network` 的检查项，字段与已有项相同（`id/level/subject/message`），`schema_version` 不变。只认识既有 `id` 的脚本会看到新 id，属于追加。
- 新增测试钩子环境变量 `MULTI_CLAUDE_TEST_NETCHECK_TARGETS`、`MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT`，仅测试模式生效。
- 对外承诺变化：“只有 `list` 联网”改为“只有 `list` 与 `doctor --network` 联网”；后者只连 `api.anthropic.com` 与 `platform.claude.com`，每个地址发一次不带凭据的 `HEAD /`（失败时重试一次）。README 两份、SECURITY.md 同步。

reference sibling 回补检查：N/A——本仓库没有 `docs/reference/`，对外接口文档是 README 与 SECURITY.md，已列入 §4。

## 6. 备选方案与决策

- **A. 只把 `list` 的失败提示改成大白话**：做不到未登录时检查、分不清“代理没开”与“代理连不出去”（同为 `OSError`）、也不查 `platform.claude.com`。否决。
- **B. 复用 `plan.send` 并解析 `OSError` 文本区分隧道失败**：依赖 `http.client` 的错误消息字符串，且分不清 TLS 前后的阶段。否决，改为 §5.1.3 手写分步。
- **C. 在 `list` 里顺带做**：`list` 的输出与时限针对套餐查询，混入后两者都难读。否决。

## 7. 影响分析

1. **`doctor` 无 `--network` 时**：`run_checks` 新参数默认为假，分支不进入，`netcheck` 模块只被 import；import 期只有常量与函数定义，无副作用。回归：§8 T1。
2. **联网承诺**：README、SECURITY.md 的“只有 list 联网”变为不准确，必须同步改。回归：§8 T20。
3. **凭据**：`netcheck` 不调用 `identity` 的任何函数、不调用 `read_oauth_token`（只从 `plan` 复用代理变量顺序与超时常量）；只从 `settings.json` 的 `env` 取 §5.1.2 列出的键，其它键（可能含令牌）不读出、不输出。代理密码只进 `Proxy-Authorization` 头与内存中的去重键。回归：§8 T9、T10。
4. **与启动命令的线路一致性**：依赖 `env.account_env`（`env.py:15`）与官方 settings 覆盖规则。`proxy off` 时启动命令删掉全部代理变量（`env.py:32-49`），账号 `settings.json` 若有 `HTTPS_PROXY` 仍会生效；`launcher` / `inherit` 时 `settings.json` 的同名值同样胜出。Windows 按大写名合并。回归：§8 T2、T3、T7。
5. **`NO_PROXY` 默认值**：`launcher` 模式会在 `NO_PROXY` 追加 `localhost,127.0.0.1,::1`（`env.py:32-49`），所以走代理的测试目标不能用 `127.0.0.1`，测试改用假主机名 `netcheck.test`，由假 CONNECT 代理转到本地假服务（同 `tests/test_plan.py:28-30` 的 `PROXIED_HOST` 做法）。
6. **`--json` 合法性**：`netcheck.run` 吞掉所有异常并转为 `internal_error`；`settings.json` 的任何读取或解析异常转为 `settings_unreadable`。回归：§8 T11、T15。
7. **帮助示例测试**：`tests/test_help_examples.py` 会真的运行每条示例并要求退出码为登记值（`:212`）；新示例在沙箱里每项为 `test_no_endpoint`，退出 1，登记进 `ALLOWED_EXIT`。回归：§8 T17。
8. **运行时**：只在用户加 `--network` 时运行。每个去重键最多 2 次连接，每次发约 100～200 字节，读回上限 64 KB。5 个账号同代理时只有 2 个键。总耗时上限约 20 秒。线程均为守护线程。
9. **代理侧**：对代理发标准 CONNECT，与 Claude Code 自己的请求方式相同。
10. **`plan.py`**：只改模块说明文字。回归：现有 `tests/test_plan.py` 全过。
11. **退出码**：`doctor --network` 有网络 error 时退出 1，这是该选项的预期语义；不加选项时不受影响。

## 8. 回归测试

全部在 `tests/test_netcheck.py`（T17 在 `tests/test_help_examples.py`），用本地假服务与假 CONNECT 代理（参照 `tests/test_plan.py:34`、`:79` 的 `_ThreadingServer`、`FakeConnectProxy`），测试模式下不连外网。直连用例目标用 `127.0.0.1`；走代理的用例目标用 `netcheck.test`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 不加 `--network` 的 `doctor --json` | 无 `network` 项；假服务收到 0 个请求；现有 `test_doctor.py` 全过 |
| T2 | 账号 `proxy off`、目标为本地 http 假服务 | 每个目标一条 ok，消息含 `via direct` 与耗时；假服务收到 `HEAD /` 且无 `Authorization`、`Cookie` 头 |
| T3 | 账号代理指向假 CONNECT 代理 | ok，消息含 `via 127.0.0.1:<端口> (launcher)`；代理收到 `CONNECT netcheck.test:<端口>` |
| T4 | 代理端口无人监听（`launcher`） | error `cannot reach the proxy`，含 `multi-claude proxy <名称>` |
| T5 | 代理对 CONNECT 回 502 | error `could not reach`，含 `502`；与 T4 文案不同 |
| T6 | 代理回 407 | error `user name and password` |
| T7 | settings 覆盖：① `proxy off` + `settings.json` 设 `HTTPS_PROXY` 指向假代理；② `launcher` 指向无人端口 + `settings.json` 设 `https_proxy` 指向假代理；③ `inherit` + 终端设 `https_proxy` 为无人端口 + `settings.json` 设 `https_proxy` 指向假代理；④ `launcher` 指向无人端口 + `settings.json` 只设大写 `HTTPS_PROXY` 指向假代理（POSIX） | ①②③ 经假代理 ok，来源 `(settings.json)`；④ 小写 `https_proxy` 仍是 launcher 值、先被取到（`env.py:40` 两套都写），error `cannot reach the proxy`、来源 `(launcher)` |
| T8 | `inherit` 模式（终端设代理与 `NO_PROXY`，避开 launcher 自动追加的 `127.0.0.1`）+ `NO_PROXY` 含目标主机（目标用 `127.0.0.1`，直连可解析）（逗号与空格分隔各一）：① 直连可达；② 直连不可达 | ① warn，含 `NO_PROXY covers this host`；② error `cannot connect`，同样带 `NO_PROXY` 说明 |
| T9 | 账号未登录与有凭据文件；`settings.json` 另含 `ANTHROPIC_API_KEY` 等非代理键 | 两种情况结果一致；非代理键的值不出现在 stdout、stderr、JSON |
| T10 | 三种代理写法都带 `alice:SECRET@`：`socks5://alice:SECRET@h:1`、`alice:SECRET@h:1`（无 scheme）、`http://alice:SECRET@<假代理>`（407） | 分别为 error `SOCKS`、error `not a valid proxy URL`、error `407`；stdout、stderr、`--json` 输出均不含 `SECRET` |
| T11 | `settings.json` 内容为非法 JSON、为 GBK 字节、`env` 为数组 | 各为 warn `could not read`；`--json` 输出可被 `json.loads` 解析 |
| T12 | 假服务接受连接但不应答，`MULTI_CLAUDE_TEST_NETCHECK_TIMEOUT=1` | error `timed out`；命令在约 3 秒内返回 |
| T13 | 假代理接受 CONNECT 但不回应 | error `did not connect to <host> in time`（`proxy_upstream_timeout`） |
| T14 | 假代理对 CONNECT 回非 HTTP 字节（如 `\x05\x00`） | error `did not answer like an http proxy` |
| T15 | 代理第一次 CONNECT 断开、第二次正常 | warn `second try` |
| T16 | 两个账号代理相同（无认证）；另两个账号同代理但认证不同；两个目标同为 `netcheck.test`、端口不同 | 前者代理只收到每个目标各 1 次 CONNECT；后者各自探测；不同端口的目标各探一次；补全脚本含 `--network` |
| T17 | 帮助示例 | `multi-claude doctor --network` 示例在沙箱中退出 1 且已登记，不为 2 |
| T18 | 自签证书的本地 https 假服务，目标 `https://127.0.0.1:<端口>` | warn `could not verify the certificate`（`cert_unverified`） |
| T19 | TLS 分类单元测试 | `_classify_tls` 对 `SSLCertVerificationError`、`SSLEOFError`、`SSLError`、`socket.timeout`、`TimeoutError` 分别得到 `cert_unverified`、`tls_closed`、`tls_failed`、`timeout`、`timeout` |
| T20 | 文档 | `grep -n` 核对 `README.md:14,42,55`、`README.zh-CN.md:14,42,55`、`SECURITY.md:50-52` 附近的联网说法都已提到 `doctor --network`，全文不再有只说 `list` 的“唯一联网”表述 |
| T21 | 其它分级 | `ANTHROPIC_BASE_URL` 设在 `settings.json` → 账号级 warn `custom_endpoint`，只显示主机名；`https://` 代理 → warn `not checked` 含 `claude --debug`，`NO_PROXY` 覆盖目标时改为直连探测并带 `NO_PROXY` 说明；`ftp://h:1` → `invalid_proxy`；探测函数被替换为抛异常 → 该项 `internal_error` 且命令在 1 秒内返回；代理对 CONNECT 不回字节即关闭 → 文案 `closed the connection`；测试模式未设目标 → `test_no_endpoint` 且假服务收 0 请求；`--json` 合法、`schema_version` 为 1、开始提示只在 stderr；有 error 退出 1、全 ok 退出 0 |
| T22 | 真实网络（本机手动） | 本机 5 个账号跑 `multi-claude doctor --network --verbose`：代理开着时全部 ok；关掉代理软件后对应账号出现 `cannot reach the proxy`；恢复后再 ok |

另跑：全量 `python3 -m unittest discover -s tests`，以及 CI 的 lint（pyflakes 与 tests 路径检查，见 `.github/workflows/ci.yml`）。

## 9. 日志 / 观测点

- stderr 开始提示：`checking the network for N account(s) (...)`，确认进入联网分支。
- 每条 `network` 结果带目标主机、线路（含来源）、结论；ok 项带耗时，重试过的标明 `second try`，绕过代理的带 `NO_PROXY` 说明。
- `--json` 的 `network` 项可按 `level` 统计。
- 代理软件日志里可看到对 `api.anthropic.com:443`、`platform.claude.com:443` 的 CONNECT，用于核对确实走了该账号的代理。

## 10. 开放问题

- Q1：Claude Code 自己匹配 `NO_PROXY` 的细则（前导点、端口、通配）未核实；本方案用 Python 标准库的匹配，个别写法可能与 Claude Code 判定不同。只影响 `bypassed` 提示，不影响探测本身；T22 实测时若发现差异再修。
- Q2：Claude Code 在 Windows 上合并 `settings.json` `env` 是否不分大小写未核实；本方案按 Windows 环境变量语义（不分大小写）处理。
