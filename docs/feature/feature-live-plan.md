# list 实时显示套餐与订阅方式

> 2026-10-11 注记：已落地。读取令牌在 `src/multi_claude/identity.py:120`（`read_oauth_token`），查询在 `src/multi_claude/plan.py:105`（`query_all`）、`:136`（`query`）、`:185`（代理解析）、`:208`（请求与失败归类），接入 list 在 `src/multi_claude/cli.py:176`（`--offline`）、`:1279`、`:1338`（失败与过期提示）。验证：本机 macOS 全量 395 个用例通过，真实账号实测（§8 的 L1）通过；编译机 Ubuntu 20、Python 3.8.10 全量 395 个用例通过、7 个按环境跳过（都不在 `tests/test_plan.py` 内）。编译机没有安装 pyflakes，静态检查只在本机跑过。

## 1. 背景

`multi-claude list` 目前的简表列为 `NAME LOGIN PROXY SHARED 5H 7D LAST USED STATUS`（`src/multi_claude/cli.py:1325`），不显示账号的套餐。Claude Code 自己会把套餐缓存在凭据里（`claudeAiOauth.subscriptionType` / `rateLimitTier`），但缓存会过时。

2026-10-11 用本机 4 个账号实测（各走自己的代理，探针只输出字段名与套餐字段）：

| 账号 | 凭据里的缓存 | 服务端返回 |
| ---- | ---- | ---- |
| A | max、default_claude_max_5x | claude_pro、default_claude_ai、apple_subscription |
| B | max、default_claude_max_5x | claude_max、default_claude_max_5x、apple_subscription |
| C | 空 | claude_pro、default_claude_ai、google_play_subscription |
| D | max、default_claude_max_5x | 令牌已过期 9 小时，HTTP 401 `authentication_error` |

账号 A 的缓存与服务端不一致，账号 C 没有缓存。所以这两列不能取缓存。

用户要求（2026-10-11）：

- list 查询结果增加两列：当前套餐（如 pro、max (5x)）、订阅方式；
- 不使用缓存数据，用各账号目录自己的令牌向服务端实时查询；
- list 默认就查，另提供跳过查询的开关；
- 订阅方式指付款渠道；
- 访问令牌过期时不刷新，显示 `expired`。

## 2. 目标 / 非目标

目标：

1. `list` 的简表、`--verbose` 表与 `--json` 输出增加套餐与付款渠道，数据来自对服务端的实时查询。
2. 查询经过该账号的代理设置，与 `claude-<名称>` 启动命令的出站路径一致；配置了代理时失败不改走直连。
3. 提供 `list --offline`，输出与本次改动前完全相同，不读令牌、不联网。

非目标：

- 不刷新令牌，不写钥匙串、不写 `.credentials.json`。
- 不改 `usage`、`doctor`、`statusline-hook` 及其它命令：它们仍然不读令牌、不联网。
- 不显示订阅状态（`subscription_status`）、计费周期、额外用量开关等其它字段。
- 不支持经 `https://` 代理查询（见 §3）。

## 3. 假设与约束

以下均为已核对的事实，括号内是依据。

1. **查询接口**：`GET https://api.anthropic.com/api/oauth/profile`，请求头 `Authorization: Bearer <accessToken>`，Claude Code 自己的超时是 10 秒（Claude Code 2.1.295 程序字符串中的函数 `I3e`；`BASE_API_URL:"https://api.anthropic.com"`）。本机实测用 `User-Agent: multi-claude/probe` 返回 200，不需要 `anthropic-beta` 头。
2. **响应字段**（实测 3 个账号的 200 响应）：顶层 `account`、`application`、`enabled_plugins`、`organization`；`organization` 含 `organization_type`、`rate_limit_tier`、`billing_type`（均为字符串）及 `uuid`、`name`、`seat_tier`、`subscription_status` 等。响应体长度 925 至 947 字节。
3. **套餐映射**（Claude Code 程序里的映射表 `sG`）：`claude_max` → max，`claude_pro` → pro，`claude_enterprise` → enterprise，`claude_team` → team。倍率来自 `rate_limit_tier`：`default_claude_max_5x`、`default_claude_max_20x`；pro 实测为 `default_claude_ai`，不带倍率。
4. **付款渠道取值**（Claude Code 程序里的集合 `Yk`）：`stripe_subscription`、`stripe_subscription_contracted`、`stripe_subscription_enterprise_self_serve`、`aws_marketplace`、`c4e_consumption_trial`、`apple_subscription`、`google_play_subscription`。实测见到 `apple_subscription` 与 `google_play_subscription`。
5. **凭据位置与结构**：macOS 在钥匙串，读取命令 `security find-generic-password -a <账户名> -w -s <服务名>`（`docs/feature/feature-account-manager.md:63`；服务名算法已在 `src/multi_claude/identity.py:36`）；Linux 在 `<账号目录>/.credentials.json`。内容是 JSON，`claudeAiOauth` 含 `accessToken`、`expiresAt`、`refreshToken`、`scopes` 等（实测 4 个账号一致）。`expiresAt` 是毫秒时间戳（实测为 int；Claude Code 的 `YI` 用 `Date.now()+300000>=e` 判断）。
6. **令牌寿命**：实测 3 个有效令牌剩余 1296 至 10343 秒，即几小时内没启动过的账号令牌多半已过期。过期令牌服务端返回 401。
7. **代理变量顺序**：Claude Code 按 `https_proxy`、`HTTPS_PROXY`、`http_proxy`、`HTTP_PROXY` 取第一个，支持 `NO_PROXY`（`docs/feature/feature-account-manager.md:68`）。
8. **账号的进程环境**：`src/multi_claude/env.py:16` 的 `account_env()` 已按启动命令的语义构造环境（含代理三态：`inherit` 不动、`off` 清除、地址则写入四个变量并追加本地 `NO_PROXY`）。
9. **标准库能力**：`http.client.HTTPConnection.set_tunnel()` 向代理发明文 `CONNECT`，之后 `HTTPSConnection` 在隧道内与目标握手。它不会先与代理做 TLS 握手，所以只能用 `http://` 代理。`urllib.request.proxy_bypass_environment(host, proxies)` 只看传入的 `proxies['no']`，不读 macOS 系统代理设置（本机实测：`'.anthropic.com'` 命中，`'localhost,127.0.0.1,::1'` 不命中）。

约束：

- 项目只用标准库，支持 Python 3.8 及以上（`pyproject.toml`）。
- 本机有网络闸门（claude 的流量只能经 Clash 出去）。multi-claude 自身不受闸门监管，所以查询必须自己保证走账号的代理，不能依赖闸门兜底。
- 不走 `urllib` 的 `ProxyHandler`：它在没有代理环境变量时会查询 macOS 系统代理的例外清单，可能把请求改成直连。

## 4. 涉及模块

行号是 `main`（`2b70c61`）当前状态。

| 区域 | 行号锚点 | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/identity.py` | 文件末尾 `:94` 之后 | 新增 | `OAuthToken`、`read_oauth_token()`、`_read_keychain_secret()`、`_parse_credentials()` |
| `src/multi_claude/identity.py` | `:1-10` 模块说明 | 修改 | 说明新增的读取入口及其唯一调用方 |
| `src/multi_claude/plan.py` | 新文件 | 新增 | 查询、并发、结果分类、两列的显示文本 |
| `src/multi_claude/cli.py` | `:22-23` import | 修改 | 加入 `plan` |
| `src/multi_claude/cli.py` | `:169-174` list 解析器 | 新增 | `--offline` 选项 |
| `src/multi_claude/cli.py` | `:400` 分发 | 修改 | 传入 `offline` |
| `src/multi_claude/cli.py` | `:1263-1313` `cmd_list` | 修改 | 新参数；`--verbose` 表加两列；失败告警 |
| `src/multi_claude/cli.py` | `:1316-1339` `_print_brief_list` | 修改 | 简表加两列；过期提示 |
| `src/multi_claude/cli.py` | `:1380-1406` `_account_entries` | 修改 | 每个账号带上 `plan` |
| `src/multi_claude/cli.py` | `:1409-1423` `_list_data` | 修改 | 传入 `offline` |
| `src/multi_claude/cli.py` | `:1465-1466`、`:1557-1558` | 修改 | 把 “multi-claude never reads credentials” 改成只说本命令 |
| `tests/test_plan.py` | 新文件 | 新增 | §8 的 T1 至 T16、R2、R3 |
| `tests/helpers.py` | `:32-37` 假 security | 修改 | 参数含 `-w` 且退出码为 0 时输出 `FAKE_SECURITY_SECRET` 指向的文件内容，未设置时输出 `{}`（真实命令在条目存在时一定有输出） |
| `tests/test_help.py` | `:46`、`:48` | 修改 | 简表表头与数据行的断言加两列 |
| `tests/test_help.py` | `:77` | 修改 | `--verbose` 表头断言加两列 |
| `tests/test_migrate.py` | `:183` | 修改 | `--verbose` 数据行的断言在 `LOGIN` 与 `LINK` 之间加两列 |
| `README.md`、`README.zh-CN.md` | `:13`、`:40-41`、`:54`、`:257`、`:287`、`:421-423`、`:443` | 修改 | 隐私说明、list 两列的说明、命令表 |
| `SECURITY.md` | `:16-19` | 修改 | 凭据读取的范围说明 |
| `CHANGELOG.md` | `:7` Unreleased | 新增 | 条目 |
| `docs/feature/feature-account-manager.md` | `:367` 附近 | 修改 | 加注记指向本方案 |
| `docs/analysis/roadmap.md` | `:7`、`:16` | 修改 | “不读写凭据”的边界说明加注记指向本方案 |
| `src/multi_claude/cli.py` | `:985` `cmd_login` 说明 | 修改 | “本工具不读写凭据”改为只说本命令 |

`completion.py` 不改：选项从 argparse 解析器遍历得到（`src/multi_claude/completion.py:45-58`），`--offline` 会自动进入补全。

## 5. 方案

### 5.1 实现要点

#### 5.1.1 读取令牌（`identity.py`）

```python
class OAuthToken(NamedTuple):
    state: str                 # TOKEN_OK / TOKEN_NONE / TOKEN_UNREADABLE
    access_token: str          # 仅 TOKEN_OK 时非空
    expires_at_ms: Optional[int]

def read_oauth_token(identity: str, account_dir: str) -> OAuthToken
```

- 顺序与 `probe()`（`identity.py:68`）相同：macOS 先读钥匙串，读不到或内容里没有访问令牌时再读 `.credentials.json`；其它平台只读文件。
- 钥匙串读取命令 `security find-generic-password -a <keychain_account()> -w -s <服务名>`，超时 10 秒，stdout 经管道读入内存，stderr 丢弃。
- 解析：JSON 对象的 `claudeAiOauth.accessToken` 必须是非空字符串，且只含可见 ASCII 字符（要放进 HTTP 请求头，含空白或控制字符的值不能用）；`expiresAt` 是有限数值时取整，否则为 `None`。令牌的格式同样用 `fullmatch` 校验。
- 每个来源（钥匙串、文件）各得出三种结果之一：有令牌；没有（条目或文件不存在，或内容是 JSON 但没有 `claudeAiOauth.accessToken`，如只用 API key 的账号）；读不了（`security` 退出码既非 0 也非 44、超时、文件打不开、内容不是 JSON 对象）。
- 合并：任一来源有令牌 → `TOKEN_OK`（钥匙串优先）；否则任一来源读不了 → `TOKEN_UNREADABLE`；否则 `TOKEN_NONE`。
- 本函数是全工具唯一读取凭据内容的入口，只有 `plan.py` 调用。令牌只存在于内存，不写入任何文件、日志、异常信息或输出。

#### 5.1.2 查询（`plan.py`）

```python
class PlanResult(NamedTuple):
    status: str                       # 见下表
    organization_type: Optional[str]
    rate_limit_tier: Optional[str]
    billing_type: Optional[str]
    reason: Optional[str]             # 仅 status == "error"
    route: Optional[str]              # 发过请求时为 "direct" 或代理的 "主机:端口"，否则 None
    elapsed_s: Optional[float]        # 发过请求时的耗时，含重试
    attempts: int                     # 实际发起的连接次数，重试过时为 2

def query_all(config: Config, base_env: Mapping[str, str], now: datetime) -> Dict[str, PlanResult]
def plan_cell(result: Optional[PlanResult]) -> str
def billing_cell(result: Optional[PlanResult]) -> str
def to_dict(result: PlanResult) -> dict
```

单个账号的流程：

1. `identity.read_oauth_token()`。`TOKEN_NONE` → `no_login`；`TOKEN_UNREADABLE` → `error`，原因 `credentials_unreadable`。
2. `expires_at_ms` 不为空且不晚于当前时间 → `expired`，不发请求（实测过期令牌必然 401，没必要把它发出去）。
3. 用 `account_env(config, account, base_env)` 得到该账号的环境，再解析代理：
   - 按 `https_proxy`、`HTTPS_PROXY`、`http_proxy`、`HTTP_PROXY` 取第一个非空值；
   - 没有值，或 `urllib.request.proxy_bypass_environment("api.anthropic.com", {"no": <no_proxy 或 NO_PROXY>})` 命中 → 直连；
   - 值的协议不是 `http`，或解析不出主机与端口 → `error`，原因 `proxy_unsupported`，不发请求；
   - 账号自己配置的代理地址不含用户名密码（`config.py:468` 拒绝），但 `inherit` 账号从环境继承的地址可能带。带了就用 `set_tunnel()` 的 `headers` 参数发 `Proxy-Authorization: Basic …`。`route` 只记主机与端口，用户名密码不进入任何输出。
4. 发请求：有代理时 `HTTPSConnection(代理主机, 代理端口)` 加 `set_tunnel("api.anthropic.com", 443)`；直连时 `HTTPSConnection("api.anthropic.com", 443)`。TLS 用 `ssl.create_default_context()`。请求头 `Authorization: Bearer <令牌>`、`User-Agent: multi-claude/<版本>`、`Accept: application/json`。套接字超时 10 秒。最多读 65536 字节响应体。任何分支都关闭连接。只有 `connect_failed` 重试 1 次，间隔 0.5 秒，走同一条路径（同一个代理或同样直连）；请求是只读的 GET，重发没有副作用。超时不重试，否则会超过总时限。依据：2026-10-11 本机实测，首次运行时 5 个账号里有 2 个在约 5 秒后失败；之后不带令牌并发握手 48 次复现到 3 次 `ssl.SSLEOFError`（均为 5.0 秒，同一个代理端口）和 2 次握手超时（10 秒），其余成功。
5. 结果分类：

| 情况 | status | reason |
| ---- | ---- | ---- |
| 200 且 `organization` 是对象 | `ok` | 无 |
| 401 | `expired` | 无 |
| 403 | `rejected` | 无 |
| 其它状态码 | `error` | `http_<状态码>` |
| 套接字超时 | `error` | `timeout` |
| 证书校验等 TLS 错误（`ssl.SSLError`，不含下一行的两类） | `error` | `tls_failed` |
| 连接被拒、DNS 失败、代理拒绝 `CONNECT`、连接被对端断开等其它 `OSError`；握手或读取途中连接被断开（`ssl.SSLEOFError`、`ssl.SSLZeroReturnError`） | `error` | `connect_failed` |
| 200 但不是 JSON、`organization` 不是对象或响应体超长；`http.client.HTTPException`（状态行或响应头不合法） | `error` | `malformed_response` |

另有两个不属于对端的原因：代理或目标的主机名不合法（编码时抛 `UnicodeError`）归为 `connect_failed`；本模块自己的意外错误归为 `internal_error`，不带出站路径。

异常的捕获顺序：`socket.timeout` → `ssl.SSLEOFError` / `ssl.SSLZeroReturnError` → `ssl.SSLError` → `OSError` → `http.client.HTTPException`。前三项都是 `OSError` 的子类，顺序反了会被归错。

`ok` 时三个字段各自校验：整串匹配 `[a-z][a-z0-9_]{0,63}`（用 `fullmatch`，`^…$` 配 `match` 会放过尾部换行）的字符串才保留，否则记为 `None`。服务端返回的字符串不经校验不进入终端输出。

并发：每个账号一个守护线程，主线程等到全部结束或总时限 15 秒。到时限仍未返回的账号记 `error` / `timeout`。用守护线程是为了超时后进程能直接退出，不被卡住的连接拖住。

测试入口：`MULTI_CLAUDE_TEST_PROFILE_URL`（经 `platform.test_hook_value()` 读取，只有 `MULTI_CLAUDE_TEST_MODE=1` 时生效）把目标换成本地地址，协议可以是 `http`。`MULTI_CLAUDE_TEST_MODE=1` 而没有设置这个变量时不发请求，记 `error` / `test_no_endpoint`，保证测试套件永远连不到真实服务。`MULTI_CLAUDE_TEST_PLAN_TIMEOUT`（秒，同样经 `platform.test_hook_value()` 读取）同时替换套接字超时与总时限，供超时用例使用。

#### 5.1.3 两列的显示文本

| status | PLAN | BILLING |
| ---- | ---- | ---- |
| `ok` | 见下 | 见下 |
| `no_login` | `-` | `-` |
| `expired` | `expired` | `-` |
| `rejected` | `rejected` | `-` |
| `error` | `error` | `-` |

- PLAN：`organization_type` 按 §3 第 3 条映射；不在映射表里的值原样显示；为 `None` 显示 `unknown`。`rate_limit_tier` 以 `_<数字>x` 结尾时追加倍率，如 `max (5x)`、`max (20x)`。
- BILLING：`billing_type` 按 `_` 切开，去掉 `subscription` 一段，再用 `-` 连接：`stripe`、`stripe-contracted`、`stripe-enterprise-self-serve`、`aws-marketplace`、`c4e-consumption-trial`、`apple`、`google-play`。为 `None` 显示 `-`。

#### 5.1.4 接入 list（`cli.py`）

- `cmd_list(verbose, as_json, names_only, offline)`：`names_only` 分支不变（不查询）。其余分支在 `offline` 为假时调用一次 `plan.query_all(config, os.environ, now)`，把结果交给 `_account_entries(config, with_usage, plans)`；`offline` 为真时传 `None`。
- `_account_entries`：`plans` 不为 `None` 时每个条目加 `"plan": plan.to_dict(...)`，为 `None` 时不加这个键。
- 简表：`plans` 不为 `None` 时表头为 `NAME LOGIN PLAN BILLING PROXY SHARED 5H 7D LAST USED STATUS`；`--offline` 时保持原 8 列。
- `--verbose` 表：`plans` 不为 `None` 时在 `LOGIN` 后加 `PLAN`、`BILLING`。
- `STATUS` 列与退出码不受查询结果影响。
- 表格之后的提示（stderr，沿用 `warn()` / `info()`）：
  - 每个 `error` 账号一行：`plan lookup failed for <名称>: <reason>`，发过请求的再加 ` (via <route>, <耗时>s)`，重试过的写成 ` (via <route>, <耗时>s, 2 attempts)`；到总时限仍未返回的账号只有 `timeout`，不带出站路径（主线程拿不到线程里的中间状态）；
  - 有 `expired` 账号时一行：`expired: start claude-<名称> once to renew the login, then run list again`（列出全部过期账号）。
- 无参数运行 `multi-claude`（`cli.py:1003`）会走 `list`，因此同样会查询。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `multi-claude list` 简表 | 新增 `PLAN`、`BILLING` 两列，位于 `LOGIN` 之后 | 按列位置解析表格的脚本会受影响；脚本应使用 `--json` |
| `multi-claude list --verbose` 表 | 新增同样两列，位于 `LOGIN` 之后 | 同上 |
| `multi-claude list --json` | 每个账号新增 `plan` 对象 | 只增不改，`schema_version` 仍为 1 |
| `multi-claude list --offline` | 新增选项，可与 `--verbose` / `--json` / `--names` 同用 | 输出与改动前相同；JSON 中没有 `plan` 键 |
| 行为 | `list` 默认读取各账号的访问令牌并向 `api.anthropic.com` 发请求 | README 与 SECURITY.md 的隐私说明同步修改 |

`plan` 对象：

```json
{
  "status": "ok",
  "plan": "max (5x)",
  "billing": "apple",
  "organization_type": "claude_max",
  "rate_limit_tier": "default_claude_max_5x",
  "billing_type": "apple_subscription",
  "reason": null
}
```

`status` 取值 `ok`、`no_login`、`expired`、`rejected`、`error`。非 `ok` 时 `plan`、`billing` 与三个原始字段为 `null`；`reason` 只在 `error` 时非空，取值见 §5.1.2。

本方案不涉及 `docs/reference/*`（仓库没有该目录）。

## 6. 备选方案与决策

| 备选 | 结论 | 理由 |
| ---- | ---- | ---- |
| 读凭据里缓存的 `subscriptionType` / `rateLimitTier` | 否决 | 实测缓存与服务端不一致，且没有付款渠道字段；用户明确要求不用缓存 |
| 调用 `claude` 子命令让它代查 | 否决 | 每个账号要起一个 Claude Code 进程，耗时以秒计，输出格式没有公开契约 |
| 用 `urllib.request` 加 `ProxyHandler` | 否决 | 没有代理环境变量时会读 macOS 系统代理的例外清单，可能改走直连 |
| 令牌过期时自动刷新 | 否决 | 用户决定不刷新；刷新会让旧的刷新令牌作废，与运行中的会话竞争 |

## 7. 影响分析

**正向（改了什么，谁会受影响）**

1. `list` 从纯本地命令变成会读令牌、会联网的命令。调用点全集：`cli.py:400`（命令分发）与 `cli.py:1003`（无参数运行）。补全脚本调用的是 `list --names`，该分支不查询。
2. `list` 的耗时：改动前只有本地文件与每账号一次 `security` 调用；改动后每账号多一次 `security -w` 调用和一次 HTTPS 请求（实测 0.9 至 1.9 秒），各账号并发，总时限 15 秒。
3. 网络流量：每个有效令牌的账号一次 GET，响应体不到 1 KB。
4. 表格列位置变化：`PROXY` 及其后的列右移两列。
5. 文案：README 首段、功能列表、常见问题与 SECURITY.md 中 “从不读取登录信息、不联网” 的说法不再成立，必须改成 “只有 `list` 会读访问令牌并向 Anthropic 查询套餐，`--offline` 可关闭；其余命令不读、不联网”。`cli.py:1466`、`:1558` 的同类输出同步改。

**反向（没改的功能是否被波及）**

1. `identity.probe()` 不改，`doctor`（`doctor.py:175`）、`login` 提示（`cli.py:1107`）、迁移检查（`cli.py:1201`）的行为不变；它们调用 `security` 时仍然不带 `-w`。
2. `usage`、`usage --history`、`statusline-hook` 不调用 `plan.py`，仍不读令牌、不联网。
3. `account_env()` 只被读取，不改；`mcp`、`run`、`code` 不受影响。
4. 钥匙串与 `.credentials.json` 只读不写，Claude Code 的登录状态不变。

**运行时**

- 线程：`list` 进程内新增至多 N 个守护线程（N 为账号数），只在本次命令内存在。线程之间不共享可变状态，各自把结果写入以账号名为键的字典项。
- 代理为 `inherit` 的账号使用调用 `list` 的那个 shell 的代理变量，与在同一 shell 里运行 `claude-<名称>` 一致。在某个账号的 Claude 会话里运行 `list` 时，继承的是那个会话的代理变量。
- 代理为 `off` 的账号直连。
- 代理是 `https://` 地址的账号查不了，显示 `error`，原因 `proxy_unsupported`。
- 账号的 `settings.json` 等文件里用 `env` 另设了代理时，Claude Code 以那里为准（README “settings 文件优先”一段），本查询只看启动命令语义下的环境，两者的出站路径可能不同。已有的告警（`accounts.warn_launch_settings`，`cli.py:1338`）会提示这种配置。
- TLS 根证书取自运行 multi-claude 的 Python。从 python.org 安装、没有运行过证书安装脚本的 Python 会握手失败，显示 `error` / `tls_failed`；README 写明处理办法。
- 设置了 `CLAUDE_CODE_CUSTOM_OAUTH_URL` 时（`cli.py:1321` 已有告警），钥匙串服务名与正式环境不同，读到的令牌可能不属于当前登录；查询仍发往正式服务，结果可能是 `expired` 或与实际不符。沿用已有告警，不另处理。
- macOS 钥匙串：Claude Code 自己用 `security` 命令读写该条目，所以 `security -w` 读取时不弹授权窗口（本机实测 4 个账号均未弹窗）。其它安装方式下是否弹窗未验证。
- Linux：`.credentials.json` 权限为 0600，属主即当前用户，读取不需要额外权限。

**既有测试**

- 断言简表或 `--verbose` 表列的用例：`tests/test_help.py:46`、`:48`、`:77` 与 `tests/test_migrate.py:183`，均需改。`tests/test_accounts.py:371` 的行正则止于 `LOGIN` 列，仍然匹配。这份清单以全量测试的实际失败为准：按表头关键字 `grep` 只找到前三处，`test_migrate.py:183` 的行正则不含表头字样，是全量运行时暴露的。
- 既有用例里的凭据文件内容都是 `{}` 或不含 `claudeAiOauth`，不会触发请求。
- macOS 上既有用例用 `FAKE_SECURITY_RC=0` 表示“已登录”：改动后假 `security -w` 输出 `{}`，结果是 `no_login`，两列为 `-`，没有告警。`FAKE_SECURITY_RC=1`（`tests/test_accounts.py:376`）下结果是 `error` / `credentials_unreadable`，多一行 stderr 告警，该用例只断言 stdout。
- `tests/test_everyday.py:224` 统计的是 `restore` 前后的 `security` 调用次数，`restore` 不调用 `plan.py`，不受影响。

**对外语义**

- `list --json` 的消费方按旧结构解析仍然正确（只多一个键）。
- 按空白切列解析表格的脚本会错位，应改用 `--json` 或加 `--offline`。

## 8. 回归测试

自动化用例在 `tests/test_plan.py`，以子进程运行 CLI，目标服务用用例内启动的本地 HTTP 服务代替。

| 编号 | 场景 | 判据 |
| ---- | ---- | ---- |
| T1 | 凭据文件含有效令牌，服务返回 `claude_max` / `default_claude_max_5x` / `apple_subscription` | 简表表头含 `PLAN BILLING`，该行为 `max (5x)`、`apple`；服务收到的 `Authorization` 等于 `Bearer <令牌>`，`User-Agent` 以 `multi-claude/` 开头 |
| T2 | 映射：pro 加 `default_claude_ai`；max 加 `_20x`；未知套餐类型；各付款渠道 | `pro`、`max (20x)`、原样显示、`stripe` / `google-play` / `aws-marketplace` 等 |
| T3 | 没有凭据；凭据是 `{}`；凭据不是 JSON | 前两种两列都是 `-`；第三种 `error` / `credentials_unreadable`；服务都没有收到请求 |
| T4 | `expiresAt` 已过 | `expired`，服务没有收到请求，stderr 有续期提示 |
| T5 | 服务返回 401、403、500 | 分别为 `expired`、`rejected`、`error`（stderr 含 `http_500`） |
| T6 | 服务返回非 JSON；`organization` 缺失；字段含控制字符 | 前两种 `error` / `malformed_response`；第三种不把原始字符串输出到终端 |
| T7 | 账号配置了代理，本地有一个只支持 `CONNECT` 的假代理；另一例是 `inherit` 账号，环境里的代理地址带用户名密码 | 假代理收到 `CONNECT`，目标服务收到请求，结果正常；第二例假代理收到 `Proxy-Authorization`，stdout 与 stderr 不含该密码 |
| T8 | 账号配置的代理端口没有监听；代理协议是 `https` | 前者 `error` / `connect_failed`，重试后仍失败，目标服务没有收到请求（没有改走直连）；后者 `error` / `proxy_unsupported`，代理与目标服务都没有收到请求 |
| T16 | 假代理第一次 `CONNECT` 后直接断开，第二次正常 | 结果正常，假代理收到 2 次 `CONNECT`，没有告警 |
| T9 | 账号代理为 `off`，环境里有 `HTTPS_PROXY` 指向假代理 | 假代理没有收到请求，目标服务收到请求 |
| T10 | 服务挂起不响应（用测试钩子缩短超时） | `error` / `timeout`，命令在时限内退出，退出码 0 |
| T11 | `--offline`，分别配合默认、`--verbose`、`--json` | 表头与改动前相同；JSON 没有 `plan` 键；服务没有收到请求；假 `security` 没有收到 `-w` |
| T12 | `--json` | `plan` 对象的键与取值符合 §5.2 |
| T13 | macOS 分支：假 `security -w` 输出凭据 JSON | 查询成功；`security` 收到的参数恰为 `find-generic-password -a <账户名> -w -s <服务名>` |
| T14 | 测试模式下没有设置测试地址 | `error` / `test_no_endpoint`，不发请求 |
| T15 | 输出与日志泄露检查 | T1、T5、T8 的 stdout 与 stderr 都不含令牌字符串 |
| R1 | 全量既有用例 | `python -m unittest discover -s tests -t tests` 全部通过；`python -m pyflakes src tests` 无输出 |
| R2 | 既有不读令牌的命令 | `doctor`、`usage`、`usage --history`、`list --names` 运行后假 `security` 的调用记录里没有 `-w`（T11 与 `tests/test_identity.py:64` 覆盖 `probe`） |
| R3 | 补全 | `completion bash` 的输出含 `--offline` |
| L1 | 本机实测（macOS，真实账号） | `multi-claude list` 显示的套餐与付款渠道与 §1 表格中的服务端返回一致；令牌过期的账号显示 `expired`；`list --offline` 输出为原 8 列 |

编译机（Ubuntu 20，Python 3.8）执行 T1 至 T15、R1 至 R3，覆盖 Linux 的凭据文件分支与最低支持的 Python 版本。T13 用假 `security` 在任何平台都能跑。L1 依赖真实账号与 macOS 钥匙串，只能在本机执行。真实 TLS 握手只在 L1 覆盖。

## 9. 日志 / 观测点

- 查询失败：stderr `plan lookup failed for <名称>: <reason>`，发过请求的带 ` (via <route>, <耗时>s)`，前缀沿用 `warn()`。`reason` 取值固定（§5.1.2），可以直接检索。
- 令牌过期：stderr 一行续期提示。
- `list --json` 的 `plan.status` 与 `plan.reason` 给脚本判断。
- 假服务与假代理在测试里记录收到的请求行与请求头，用来断言 “发了什么、经过哪里、没发什么”。
- 任何输出都不含令牌、响应体原文与异常对象的字符串形式（异常只按类型归类成 `reason`）。
