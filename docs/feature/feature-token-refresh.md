# list 自动刷新过期的访问令牌

> 2026-10-11 注记：已落地。凭据读写在 `src/multi_claude/identity.py:216`（`read_credentials`）、`:246`（`write_credentials`）、`:266`（钥匙串写入）；刷新在 `src/multi_claude/refresh.py:65`（`renew`）、`:153`（比对与写回）、`:229`（锁）；接入查询在 `src/multi_claude/plan.py:123`（`query_all`）、`:195`（收尾等待）、`:244`（互斥闸）、`:356`、`:417`（发送）；`list` 的选项与提示在 `src/multi_claude/cli.py:176`、`:1345`。验证：本机 macOS（Python 3.11）全量 427 个用例通过；编译机 Ubuntu 20、Python 3.8.10 全量 427 个通过、7 个按环境跳过；真实账号实测一次（§8 的 L2）通过。独立评审三轮，前两轮发现的问题（Ctrl-C 保护在 Python 3.9 至 3.11 上失效、只保护正在等待的账号、换到新令牌后因次要字段丢弃响应、提示打印失败打断等待）均已修复并有用例。编译机没有安装 pyflakes，静态检查只在本机跑过。

## 1. 背景

v0.9.0 的 `list` 用各账号的访问令牌向服务端查询套餐（`docs/feature/feature-live-plan.md`）。访问令牌过期时不刷新，`PLAN` 一格显示 `expired`。

2026-10-11 用户反馈：账号 `PLAN` 显示 `expired`，被理解成订阅过期。核实结果：该账号的访问令牌已过期 15.6 小时，刷新令牌还有 9 天才过期，显示的数据没错，写法有歧义。访问令牌的有效期实测为 8 小时左右（刚刷新的令牌剩余 27242 秒），所以几小时没启动过的账号都查不到套餐。

用户决定（2026-10-11，原话）：“处理待办1，改提示+自动刷新令牌”。

## 2. 目标 / 非目标

目标：

1. 访问令牌过期时，`list` 用刷新令牌换新令牌，写回凭据，再查询套餐。
2. 刷新过程与 Claude Code 自己的刷新互相兼容：同一把锁、同样的写回前比对，正在运行的会话不受影响。
3. 没刷新成的账号，`PLAN` 一格显示 `token expired`，提示里写明订阅不受影响。
4. 提供 `list --no-refresh`：照常查询，但不刷新、不写凭据（即 v0.9.0 的行为）。

非目标：

- 令牌没过期时不刷新，也不提前刷新。
- 不处理刷新令牌失效的账号：只提示重新登录，不清空、不改动凭据。
- 服务端对未过期令牌返回 401 时不刷新。
- 其它命令不变：仍不读、不写凭据，不联网。

## 3. 假设与约束

以下均取自 Claude Code 2.1.295 的程序字符串（函数名是压缩后的名字），括号里是依据。

1. **刷新请求**（`Y_e`）：`POST https://platform.claude.com/v1/oauth/token`（`TOKEN_URL`），`Content-Type: application/json`，请求体 `{"grant_type": "refresh_token", "refresh_token": <刷新令牌>, "client_id": <客户端>, "scope": <以空格连接的范围>}`。`client_id` 取凭据里的 `clientId`，没有时用 `9d1c250a-e61b-44d9-88ed-5944d1962f5e`（`CLIENT_ID`）。Claude Code 自己的超时是 30 秒。
2. **刷新响应**：200 时取 `access_token`、`refresh_token`（缺省沿用旧的）、`expires_in`（秒）、`refresh_token_expires_in`（秒，可缺）、`scope`（空格分隔的字符串，`z$n`）。新的过期时刻是 `Date.now() + expires_in * 1000`。
3. **范围**：Claude Code 优先请求默认范围集，服务端回 `invalid_scope` 时退回凭据里保存的 `scopes` 重试。本方案直接用保存的 `scopes`，即它的回退路径。
4. **刷新令牌失效**（`X_e`）：状态码 400 或 401 且响应体的错误码为 `invalid_grant`。Claude Code 此时会把凭据里的三个令牌字段清空（`avr`）；本方案不清空。
5. **锁**（`KUn`、`Yvs`）：用 proper-lockfile，即 `mkdir` 一个目录作为锁。先取新锁 `<配置目录>/.oauth_refresh.lock`，再取旧锁 `<配置目录的真实路径>.lock`；`stale` 60 秒（目录的修改时间超过 60 秒没更新，别的进程可以删掉它重取），持有者每 5 秒更新一次修改时间。配置目录是 `CLAUDE_CONFIG_DIR` 的值，default 身份为 `~/.claude`（`Pw`）。
6. **抢占死锁**（`CT`）：锁被占用时，Claude Code 只有在配置目录里读到持有者记录 `.oauth_refresh.lock.owner` 且能证明该进程已死时才会提前接管；读不到记录时返回 `no_owner_record`，不提前接管。但修改时间超过 60 秒没更新的锁，proper-lockfile 仍会当作过期锁删掉重取，与有没有持有者记录无关。
7. **拿到锁之后**（`Hl`）：重新读凭据。访问令牌已经和进入时不同，说明别的进程刚刷新过，直接采用；否则才发刷新请求。
8. **写回前比对**（`ovr`）：重读凭据，只有保存着的 `refreshToken` 等于刚才发出去的那个、或者是空串时才写；否则说明别人已经写过，放弃写入。写回时保留凭据里的其它内容（`Rk`：`refreshTokenExpiresAt`、`subscriptionType`、`rateLimitTier` 缺省沿用旧值；顶层其它键如 `mcpOAuth` 不动）。失败重试 3 次。
9. **钥匙串写法**（`update`）：把凭据 JSON 转成十六进制，经 stdin 交给 `security -i`，内容是一行 `add-generic-password -U -a "<账户>" -s "<服务名>" -X "<十六进制>"`。这一行超过 4032 字节时改用命令行参数。
10. **运行中的会话**（`K$`）：请求得到 401 时重读凭据，发现访问令牌变了就直接用新的（`tengu_oauth_401_recovered_from_keychain`）。所以别的进程刷新并写回后，运行中的会话能自己跟上。
11. **凭据结构**（本机实测）：`claudeAiOauth` 含 `accessToken`、`refreshToken`、`expiresAt`、`refreshTokenExpiresAt`、`scopes`、`subscriptionType`、`rateLimitTier`；刷新令牌失效的账号三个令牌字段是空串、`expiresAt` 为 0。

约束：

- 刷新请求一旦发出，服务端就可能已经换掉了刷新令牌。新令牌没写回就退出，该账号必须重新登录。所以从发出请求到写回完成之间，进程不能因为总时限或 Ctrl-C 提前退出。
- 上述行为没有写进官方文档，随 Claude Code 版本可能变化（它已经有新旧两把锁）。只核对过 2.1.295；没有“锁内重读”的更早版本与本方案并用时有风险，README 写明依据的版本。
- `Thread.join(timeout)` 在 Python 3.9 至 3.11 上被 `KeyboardInterrupt` 打断后，会把仍在运行的线程标成已结束（独立评审用最小脚本复现：线程睡 3 秒，0.5 秒时发 SIGINT，随后 `is_alive()` 为假）。所以主线程不能用 `join` / `is_alive` 判断查询线程是否结束。
- 刷新接口的真实响应在编码前没有实测过。2026-10-11 用一个真实账号做了一次真实刷新（§8 的 L2）：状态码 200，响应的键为 `token_type`、`access_token`（字符串）、`expires_in`（整数 28800）、`refresh_token`（字符串，与旧值不同）、`scope`（字符串）、`token_uuid`、`refresh_token_expires_in`（整数）、`organization`、`account`，与第 2 条一致。

## 4. 涉及模块

行号是 `main`（`1dc2ce7`）当前状态。

| 区域 | 行号锚点 | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/identity.py` | `:1-13` 模块说明 | 修改 | 说明新增的写入入口及其唯一调用方 |
| `src/multi_claude/identity.py` | `:163-185` `_parse_credentials` | 修改 | 拆出 `token_of()` 与 `is_token()` 供刷新复用；`read_oauth_token()` 的行为不变 |
| `src/multi_claude/identity.py` | 文件末尾 | 新增 | `Credentials`、`read_credentials()`、`write_credentials()`、`_write_keychain_secret()` |
| `src/multi_claude/identity.py` | `:66` `keychain_account` | 修改 | 账户名校验改用 `fullmatch`：账户名现在会拼进 `security -i` 的命令行，尾部换行不能放过 |
| `src/multi_claude/refresh.py` | 新文件 | 新增 | 锁、刷新请求、比对与写回 |
| `src/multi_claude/plan.py` | `:105-126` `query_all` | 修改 | 新参数 `refresh`；等待正在刷新的线程；Ctrl-C 时同样等待 |
| `src/multi_claude/plan.py` | `:136-167` `query` | 修改 | 过期时先尝试刷新 |
| `src/multi_claude/plan.py` | `:208-252` `_fetch` | 修改 | 拆出通用的 `send()`（方法、请求头、请求体可变），`_fetch` 改为调用它 |
| `src/multi_claude/plan.py` | `:82-96` `PlanResult`、`:293-316` 显示与提示 | 修改 | 新字段 `refresh`；`expired` 的显示文本改为 `token expired` |
| `src/multi_claude/cli.py` | `:176` list 解析器 | 新增 | `--no-refresh` |
| `src/multi_claude/cli.py` | `:1279` 调用 `query_all` | 修改 | 传入 `refresh` |
| `src/multi_claude/cli.py` | `:1338` `_report_plan_problems` | 修改 | 刷新失败的告警、过期提示的新文字 |
| `tests/helpers.py` | `:32-48` 假 security | 修改 | 支持 `-i`：从 stdin 读命令，把 `-X` 的十六进制解码后写入 `FAKE_SECURITY_SECRET` 指向的文件 |
| `tests/test_plan.py` | `FakeProfileServer`、T4、T5、T12 | 修改 | 假服务加刷新接口；过期用例的显示文本 |
| `tests/test_refresh.py` | 新文件 | 新增 | §8 的 F1 至 F18、R2 |
| `README.md`、`README.zh-CN.md` | 首段、功能列表两条、“Plan and billing” 一节、FAQ、命令表 | 修改 | 说明自动刷新与 `--no-refresh` |
| `SECURITY.md` | Scope notes 前三条 | 修改 | 凭据写入的范围 |
| `CHANGELOG.md` | Unreleased | 新增 | 条目 |
| `docs/feature/feature-live-plan.md` | 顶部注记 | 修改 | 注明“不刷新”已由本方案改变 |
| `docs/analysis/roadmap.md` | `:7` 凭据原则 | 修改 | 加上刷新这一例外 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 凭据的读与写（`identity.py`）

```python
class Credentials(NamedTuple):
    state: str               # TOKEN_OK / TOKEN_NONE / TOKEN_UNREADABLE
    source: str              # LOGIN_KEYCHAIN / LOGIN_FILE / ""：claudeAiOauth 是从哪里读到的
    document: Optional[dict] # 该来源的完整 JSON 对象

def read_credentials(identity, account_dir) -> Credentials
def write_credentials(identity, account_dir, source, document) -> bool
```

- `read_credentials()`：来源顺序与现有 `read_oauth_token()` 相同。`TOKEN_OK` 的条件放宽为“该来源的 JSON 里有 `claudeAiOauth` 对象”，访问令牌是否可用由调用方判断（刷新要处理访问令牌已过期、但刷新令牌还在的情况）。
- `read_oauth_token()` 不改（现有用例 `tests/test_plan.py` 的 `KeychainTokenTest` 继续适用）；从它的解析里拆出 `token_of(document)` 与 `is_token(value)`，供刷新流程判断“这份凭据里的访问令牌可不可用”。
- `write_credentials()`：写回 `source` 指定的来源，返回是否成功。
  - 钥匙串：按 §3 第 9 条。命令行不超过 4032 字节时走 `security -i` 的 stdin；超过时改用参数（与 Claude Code 相同，这时十六进制内容在进程列表里短暂可见）。超时 10 秒，退出码非 0 即失败。
  - 文件：`fsutil.atomic_write(os.path.realpath(path), json, 0o600)`。取真实路径是因为凭据文件可能是软链，直接替换会把软链换成普通文件。
- `write_credentials()` 是全工具唯一写凭据的入口，只有 `refresh.py` 调用。

#### 5.1.2 刷新（`refresh.py`）

```python
class Outcome(NamedTuple):
    status: str                    # 见下表
    access_token: str              # 仅 REFRESHED / ADOPTED 时非空
    expires_at_ms: Optional[int]

def renew(identity, account_dir, config_dir, now_ms, post, on_commit) -> Outcome
```

`post(body: dict) -> (reason, status, body_bytes)` 由 `plan.py` 传入，负责经账号的代理把 JSON 发到刷新接口；`refresh.py` 自己不联网，也不依赖 `plan.py`。`config_dir` 是 Claude Code 眼里的配置目录（锁建在这里）：dir 身份是账号目录，default 身份是 `~/.claude`。`on_commit()` 在发出刷新请求之前调用一次：返回真表示“从现在起调用方会等我结束”；返回假表示调用方已经不等了，这时不发请求，结果为 `abandoned`。

流程：

1. **取锁**：配置目录不存在时返回 `no_refresh_token`。依次 `os.mkdir` 新锁与旧锁（§3 第 5 条），并记下每把锁的设备号与 inode。任一已存在 → 放掉已取到的，返回 `lock_busy`；因权限等原因建不了 → `lock_error`。不等待，不写持有者记录。已存在的锁只有在修改时间超过 300 秒没更新时才删掉重取一次（活着的持有者每 5 秒更新一次；这条用来清理本工具被强杀后留下的锁）。
2. **锁内重读**：`read_credentials()`。
   - 读不出 → `credentials_unreadable`。
   - 访问令牌可用且没过期 → 别的进程刚刷新过，返回 `adopted`，带上这个令牌。
   - `refreshToken` 不是非空字符串 → `no_refresh_token`。
3. **发请求**：先调用 `on_commit()`（返回假则以 `abandoned` 结束），再 `post()`。请求体按 §3 第 1、3 条；`scopes` 不是字符串列表时不带 `scope`。
4. **解析**：
   - 传输失败 → 原因原样返回（`timeout`、`connect_failed`、`tls_failed`、`proxy_unsupported` 等）。
   - 400 或 401 且响应体的 `error`（字符串，或对象里的 `code` / `type`）为 `invalid_grant` → `refresh_rejected`。
   - 其它非 200 → `http_<状态码>`。
   - 200 但 `access_token` 不是可用的令牌串 → `malformed_response`。
   - 200 且 `access_token` 可用 → 必须写回，其余字段只做降级，不再放弃：`refresh_token` 不是可用的令牌串时沿用发出去的那个；`expires_in` 读不成正的有限数（数字形式的字符串也接受）时把过期时刻写成收到响应的时刻，让 Claude Code 下次启动自己再刷一次；`refresh_token_expires_in`、`scope` 不合预期时不更新对应字段。理由：到这一步旧的刷新令牌多半已作废，丢掉响应等于让账号掉线，而真实响应的字段类型没有实测过。
5. **比对并写回**：重读凭据。保存着的 `refreshToken` 既不等于发出去的那个、也不是空串 → 不写；这时如果保存着的访问令牌可用且没过期就返回 `adopted`，否则返回 `credentials_changed`。否则在原 `claudeAiOauth` 上更新 `accessToken`、`refreshToken`（响应里没有就沿用旧的）、`expiresAt`，响应里有才更新 `refreshTokenExpiresAt` 与 `scopes`，其余字段与顶层其它键原样保留，写回同一个来源。
6. **校验**：写完再读一次，访问令牌等于新值才算成功。不成功就重写，共 3 次，间隔 0.1、0.2 秒；每次尝试里的意外异常只算这一次失败。3 次都不成 → `save_failed`，仍把新令牌返回给调用方用于本次查询。
7. **放锁**：`finally` 里先停掉并等保活线程结束，再按相反顺序 `os.rmdir`。放锁与保活都先核对该路径上的目录还是不是自己建的那个（设备号与 inode）：进程被挂起超过 60 秒时锁可能已被别的进程删掉重建，那就不再碰它。
8. **保活**：持锁期间由一个守护线程每 5 秒把两个锁目录的修改时间更新为当前时间（与 proper-lockfile 的 `update` 相同），避免持锁超过 60 秒时被别的进程当作过期锁删掉。刷新请求的套接字超时取 30 秒（与 Claude Code 相同）：请求可能已被服务端受理，超时放弃就等于丢掉新令牌，所以这里比套餐查询的 10 秒宽。

| status | 含义 | 写了凭据 |
| ---- | ---- | ---- |
| `refreshed` | 换到新令牌并已写回 | 是 |
| `adopted` | 别的进程已经刷新，直接用它的令牌 | 否 |
| `save_failed` | 换到新令牌，但没能写回 | 否（钥匙串写入与文件替换都是整体成功或整体失败，不会留下写了一半的凭据） |
| `abandoned`、`lock_busy`、`lock_error`、`no_refresh_token`、`credentials_unreadable`、`credentials_changed`、`refresh_rejected`、`malformed_response`、`http_<码>`、传输类原因 | 没换到 | 否 |

测试入口：`MULTI_CLAUDE_TEST_TOKEN_URL`（经 `platform.test_hook_value()`）。测试模式下没有设置时不发请求，原因为 `test_no_endpoint`。

#### 5.1.3 接入查询（`plan.py`）

- `send(url, proxy, method, headers, body, socket_timeout) -> (reason, status, body)`：从 `_fetch` 拆出的传输部分，异常归类规则不变（`feature-live-plan.md` §5.1.2）。`_fetch` 与刷新请求都用它。刷新请求不重试：重发可能把同一个刷新令牌用两次。
- `query()`：访问令牌过期且允许刷新时，调用 `refresh.renew()`。凭据里没有可用访问令牌的账号仍按 `no_login` 处理，不尝试刷新（本机实测这种账号的刷新令牌也是空的，§3 第 11 条）。
  - `refreshed`、`adopted`、`save_failed` → 用返回的令牌继续查询套餐。
  - 其它 → 结果为 `expired`，`PlanResult.refresh` 记下原因。
  - 不允许刷新（`--no-refresh`）→ 结果为 `expired`，`refresh` 为 `None`。
- `PlanResult.refresh: Optional[str]`：`None` 表示没有尝试刷新；否则是 `refresh.py` 的 status。
- **等待规则**（`query_all`、`_settle` 与 `_CommitGate`）：
  - “主线程不再等某个账号”与“该账号的线程开始刷新”在同一把锁下互斥，先到的生效。线程先到，主线程就等它；主线程先到，线程的 `on_commit()` 得到假，不发请求。
  - 主线程用每个线程结束时置位的事件等待，不用 `join`（§3 约束）。
  - 无论是总时限到期还是被打断，收尾都走 `_settle()`：先把所有还没开始刷新的账号一次性标记为放弃，再等所有正在刷新的账号结束。等待不设上限（刷新的每一步都有自己的超时）。刷新一返回线程就离开闸门，后面的套餐查询不占用这段等待。
  - 查询期间接管 SIGTERM、SIGHUP：处理函数只置一个标志，不抛异常（两个不同的信号几乎同时到达时，第二个异常会打断第一个的展开，把等待用的锁弄坏），主线程以 0.2 秒为一片等待并查看标志。有账号正在刷新时，这两个信号来多少次都先等写完；Ctrl-C 第一次只打印一行说明并继续等，第二次才退出。提示打印失败（终端已关）不影响等待。收到过中断时，写完后命令以被中断的方式结束，不打印表格。原本被忽略的信号（如 `nohup` 下的 SIGHUP）保持忽略。

#### 5.1.4 显示

| 情况 | PLAN | 提示（stderr） |
| ---- | ---- | ---- |
| 刷新成功后查到套餐 | 套餐文本 | 无 |
| `save_failed` | 套餐文本 | `error`：`the renewed login of <名称> could not be saved; if claude-<名称> asks you to log in, run multi-claude login <名称>` |
| 过期，没刷新成 | `token expired` | `warning`：`token refresh failed for <名称>: <原因>` |
| 过期，原因是 `refresh_rejected` 或 `no_refresh_token` | `token expired` | 同上，另加 `run multi-claude login <名称>` |
| 过期，`--no-refresh` | `token expired` | 无告警 |

有 `token expired` 的账号时，表格后打印一行：`token expired: the access token ran out, the subscription is not affected; start <claude-名称…> once, then run list again`。

`list --json` 的 `plan.status` 仍是 `expired`（不改已发布的取值），`plan` 对象新增 `refresh` 字段。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `list` 的行为 | 访问令牌过期时自动刷新并写回凭据 | README 与 SECURITY.md 的说明同步修改；`--no-refresh` 保留旧行为 |
| `list --no-refresh` | 新增 | 可与 `--verbose`、`--json` 同用；`--offline` 下无意义，允许同时给出 |
| `PLAN` 一格 | `expired` 改为 `token expired` | 解析表格的脚本受影响；脚本应使用 `--json` |
| `list --json` 的 `plan` 对象 | 新增 `refresh`：`null` 或 §5.1.2 的 status 字符串 | 只增不改 |
| 过期提示行 | 文字改变 | 无 |

本方案不涉及 `docs/reference/*`（仓库没有该目录）。

## 6. 备选方案与决策

| 备选 | 结论 | 理由 |
| ---- | ---- | ---- |
| 让 Claude Code 自己刷新：运行 `claude auth status` | 否决 | 读它的实现，只读本地状态，不触发刷新；2026-10-11 对一个令牌有效的账号运行，令牌没有变化 |
| 用 `claude -p` 发一条消息触发刷新 | 否决 | 会消耗该账号的用量 |
| 不取锁，只靠写回前比对 | 否决 | 与运行中的会话同时刷新时，输的一方会收到 `invalid_grant`，Claude Code 会清空凭据；虽然随后能恢复，但窗口可以用锁避免 |
| 提前刷新（快过期就刷） | 否决 | 写凭据的次数越少越好；过期才刷已满足需求 |

## 7. 影响分析

**正向**

1. `list` 从“只读凭据”变成“可能写凭据”。写入点只有 `identity.write_credentials()`，调用点只有 `refresh.renew()`，触发条件是访问令牌已过期、有刷新令牌、没有 `--offline` / `--no-refresh` / `--names`。
2. `multi-claude`（无参数）与 `login` 之后的自动 `list` 同样会刷新（`cli.py` 里经 `main(["list"])`）。
3. 耗时：每个过期账号多一次 `security` 读、一次 POST、一次写、一次读。
4. 账号目录里会短暂出现 `.oauth_refresh.lock` 目录，账号目录旁会短暂出现 `<目录>.lock`。进程被强杀（SIGKILL、断电）时可能留下：Claude Code 自己要刷新时会在 60 秒后把它当作过期锁清掉；multi-claude 再次运行时，5 分钟内报 `lock_busy`，之后清掉重取。

**反向**

1. 运行中的 Claude 会话：令牌被别的进程刷新后，它在下一次 401 时重读凭据（§3 第 10 条），不需要重新登录。
2. 两个 `multi-claude list` 同时运行：后到的取不到锁，报 `lock_busy`，该账号这一次显示 `token expired`。
3. `shared` 链接、`migrate-default`、`export` 不读写凭据内容，不受影响。`root` 目录下出现的 `<账号>.lock` 是否会被 `doctor` 或 `list` 当成异常条目：`_account_entries` 只遍历配置里的账号名（`cli.py:1380` 一带），不扫描目录，不受影响。
4. `feature-live-plan.md` 的结论“过期的令牌不发出去”仍成立：发出去的是刷新令牌，发往刷新接口。

**运行时**

- 刷新请求与套餐查询走同一条出站路径（该账号的代理），失败不改走直连。
- 最坏情况：刷新请求已被服务端受理，而新令牌没能写回（三次写入都失败、断电、被强杀）。结果是该账号需要重新登录。缓解：写回前后各读一次、重试三次、主线程等待提交中的线程、Ctrl-C 不立即退出。
- 钥匙串被锁住时写入失败，按 `save_failed` 处理。
- 凭据 JSON 较大（`mcpOAuth` 很多）时走参数方式写钥匙串，十六进制内容在本机进程列表里短暂可见，与 Claude Code 相同。
- default 身份：配置目录是 `~/.claude`。`accounts.account_dir()` 给出的路径与它可能互为软链，两条路径下的 `.oauth_refresh.lock` 是同一个目录项；旧锁路径取真实路径，同样一致。
- Linux 上凭据文件权限保持 0600。

**对外语义**

- 文档里“从不写入凭据”的说法不再成立，改为“只有 `list` 在访问令牌过期时刷新并写回；`--no-refresh` 与 `--offline` 不写”。

## 8. 回归测试

自动化用例在 `tests/test_refresh.py`，刷新接口是用例内的本地假服务；凭据用账号目录里的 `.credentials.json`，钥匙串分支用假 `security`。

| 编号 | 场景 | 判据 |
| ---- | ---- | ---- |
| F1 | 访问令牌过期，刷新接口返回新令牌 | `PLAN` 为套餐文本；套餐接口收到的是新令牌；刷新接口收到的请求体含 `grant_type`、旧刷新令牌、`client_id`、`scope`；凭据文件里三个字段已更新，`mcpOAuth` 等其它键与 `subscriptionType` 原样保留，权限 0600 |
| F2 | 响应里没有 `refresh_token`、`scope` | 刷新令牌与 `scopes` 沿用旧值 |
| F3 | `--no-refresh` | 刷新接口没有收到请求，凭据文件字节不变，`PLAN` 为 `token expired`，没有告警 |
| F4 | `--offline`、`--names` | 刷新接口没有收到请求，凭据文件不变 |
| F5 | 刷新接口返回 400 `invalid_grant` | `token expired`；告警含 `refresh_rejected` 与 `multi-claude login`；凭据文件不变 |
| F6 | 刷新接口返回 500；返回非 JSON；`access_token` 缺失或不是可用的令牌串 | 分别为 `http_500`、`malformed_response`、`malformed_response`；凭据文件不变 |
| F6b | 200 且 `access_token` 可用，但 `expires_in` 为字符串、0、布尔、极大值或缺失，`refresh_token` 为 `null` 或空串，`scope` 为数字 | 每种都写回了新访问令牌；刷新令牌沿用旧值；`expires_in` 不可用时过期时刻约等于当前时刻 |
| F7 | 新锁目录已存在；旧锁目录已存在 | `lock_busy`；没有发请求；别人的锁目录还在；自己取到的那把已放掉 |
| F7b | 残留锁的修改时间在 90 秒前；在 1 小时前 | 前者 `lock_busy`；后者被清掉，刷新成功 |
| F7c | 持锁期间两把锁被删掉重建（假服务在应答前做） | 运行结束后重建的两把锁都还在 |
| F7d | 配置目录只读；配置目录不存在；凭据文件是软链 | 分别为 `lock_error`、`no_refresh_token`（都没有发请求）、写回后仍是软链且指向的文件已更新 |
| F8 | 正常刷新后 | 两个锁目录都不存在 |
| F9 | 凭据里没有刷新令牌（空串） | `no_refresh_token`，没有发请求 |
| F10 | 发请求期间，凭据被别的进程换成了新的未过期令牌（假服务在应答前改写文件） | 不写凭据（文件保持别人写的内容）；用别人的令牌查到套餐；结果为 `adopted` |
| F11 | 写回一直失败（凭据文件所在目录只读） | 结果为 `save_failed`；`PLAN` 仍是套餐文本；stderr 有 `could not be saved` |
| F12 | 钥匙串分支：假 `security -i`；另一例凭据超过 4032 字节 | 前者 stdin 收到的一行与 §3 第 9 条一致，假钥匙串里的内容是更新后的 JSON，命令行参数里没有十六进制内容；后者改用参数形式并写入成功 |
| F13 | 刷新接口挂起 3 秒才应答，总时限设为 1 秒 | 命令等到刷新结束才退出；凭据已更新（没有因为总时限提前退出而丢掉新令牌）。该行显示套餐还是超时不作断言：总时限已过，之后的套餐查询赶不赶得上都算对 |
| F16 | `on_commit()` 返回假；锁内读到的令牌已经有效 | 前者结果为 `abandoned`，没有发请求；后者结果为 `adopted`，没有发请求；两者都放掉了锁 |
| F17 | 刷新请求已到假服务、尚未应答时：发 SIGINT、SIGTERM、SIGHUP 各一次；stderr 已关闭时连发两个 SIGHUP；SIGHUP 与 SIGTERM 紧挨着发；发两次 SIGINT | 前三例命令都等到写回完成才退出，凭据已更新，锁已放掉；最后一例第二次后退出 |
| F18 | 两个账号：先注册的令牌有效但套餐接口挂起，后注册的令牌过期且刷新接口 3 秒后应答；刷新途中发一次 SIGINT | 后一个账号的凭据已更新，锁已放掉 |
| F14 | 泄露检查 | F1、F5、F11 的 stdout 与 stderr 不含新旧访问令牌、刷新令牌 |
| F15 | `--json` | `plan.refresh` 在刷新成功、失败、未尝试三种情况下分别为 `refreshed`、原因字符串、`null`；`status` 取值不变 |
| R1 | 全量既有用例 | `python -m unittest discover -s tests -t tests` 通过；`python -m pyflakes src tests` 无输出 |
| R2 | 不写凭据的命令 | `doctor`、`usage`、`list --names`、`list --offline`、`list --no-refresh` 运行后，假 `security` 没有收到 `-i` 或 `add-generic-password` |
| L2 | 本机实测（macOS，真实账号，一次） | 选一个没有运行中会话的账号，令牌过期后运行 `list`：`PLAN` 显示套餐；钥匙串里的过期时刻已更新；随后 `claude-<名称> auth status` 仍显示已登录 |

F13 用新增的测试钩子 `MULTI_CLAUDE_TEST_PLAN_TOTAL_TIMEOUT`（秒，经 `platform.test_hook_value()` 读取）只改总时限，套接字超时保持默认；已有的 `MULTI_CLAUDE_TEST_PLAN_TIMEOUT` 仍同时改两者。

编译机（Ubuntu 20，Python 3.8）执行 F1 至 F18、R1、R2。L2 依赖真实账号与真实刷新接口，只能在本机执行，也是 §3 里“响应字段”唯一的实测。

## 9. 日志 / 观测点

- 刷新失败：stderr `token refresh failed for <名称>: <原因>`，原因取值固定（§5.1.2）。
- 写回失败：stderr `error` 级一行，含 `could not be saved`。
- `list --json` 的 `plan.refresh`。
- 任何输出都不含访问令牌、刷新令牌、响应体原文与异常文本。
