# multi-claude：降低 statusLine 钩子的显示延迟

> 2026-10-01 注记：已合并（PR #11），随 v0.2.1 发布。实测（真实解释器、claude-hud 命令 15 次）：直接 91 ms、经钩子 122 ms，差 31 ms，达到 ≤ 40 ms；安装版实测 97 → 131 ms。来源：v0.2.0 发布后的本机实测（statusline 方案 `docs/feature/feature-statusline-usage.md` §8 T15 之前）。基线：main `e785f68`（v0.2.0）。用户决定：先优化再做 T15。

## 1. 背景

本机实测（未改任何设置文件）：用本机 claude-hud 的 statusLine 命令和同一份 stdin 比较，经 `statusline-hook` 包装后输出逐字节相同，但每次刷新的耗时从中位数 90 ms 涨到 213 ms。拆开来看，有三处开销：

| 环节 | 实测 | 原因 |
| ---- | ---- | ---- |
| 版本管理器 shim | shim 空启动 86 ms，真实解释器 13 ms | `install.sh:93` 用 `command -v python3` 选解释器，本机得到 `~/.pyenv/shims/python3`（pyenv shim 是一段 shell 脚本），并把它写进 `~/.local/bin/multi-claude`（`install.sh:195`） |
| 模块导入 | `import multi_claude.statusline` 约 27 ms（`-X importtime`），整体比空启动多约 36 ms | `__main__.py:3` 先导入整个 `cli`；`statusline.py:23-25` 又导入 `accounts → config → platform`，`platform` 在顶层导入 `ctypes` |
| 采集本身 | — | `statusline.run_hook`（`statusline.py:137`）先同步完成采集（读配置、读 `.claude.json`、写快照），再 exec 原命令 |

## 2. 目标 / 非目标

**目标**

1. 钩子读完 stdin 后立即 exec 原命令，采集交给一个与 Claude 断开的后台进程；钩子入口在 exec 之前只导入标准库的少数模块。
2. `install.sh` 发现 `python3` 是版本管理器的 shim（路径中含 `/shims/`）时，改用该解释器的真实路径写进启动命令。
3. 用实测证明，经钩子比直接执行多出的耗时（真实解释器下的中位数）不超过 40 ms。

**非目标**

- 不改采集内容、快照格式、节流规则与读取合并（`statusline.capture`、`usage.read_account_usage` 不变）。
- 不改 `platform` 的导入方式（采集已移到后台，不再影响显示）。
- 不处理 Homebrew 等非 shim 安装的 Python（`command -v` 得到的已经是可直接执行的解释器）。

## 3. 假设与约束

- Node 子进程的 `close` 事件要等进程退出、且其 stdio 流全部关闭后才触发（Node 官方文档 child_process `'close'` 事件）。Claude Code 2.1.286 收集状态栏输出用的是 `close` 还是 `exit` 本次没有核实。后台进程在开始采集前先把继承来的 stdin / stdout / stderr 重定向到 `/dev/null`，两种情况下都不会让 Claude 等到采集结束；T2 用读到 EOF 的时间验证。
- macOS 上 fork 之后不 exec、继续运行 Python，只有在父进程已经初始化过 Objective-C 运行时的情况下才不安全。钩子进程在 fork 前只导入了 `os`、`sys`、`tempfile`，没有加载任何系统框架，所以安全。
- 后台进程用两次 `fork` 加 `setsid` 脱离：中间进程立即退出，并由钩子 `waitpid` 回收，不留僵尸；孙进程归 init 所有，不在 Claude 启动的进程组里。
- 快照写入本来就是原子替换（`fsutil.atomic_write`），后台进程被杀时不会留下半截文件。
- 版本管理器 shim 的识别规则是路径中含 `/shims/`，覆盖 pyenv（`~/.pyenv/shims`）、asdf（`~/.asdf/shims`）、mise（`~/.local/share/mise/shims`）。真实路径取 `"$PYTHON" -c 'import sys; print(sys.executable)'`。代价：之后若卸载了该 Python 版本，需要重跑 `install.sh`。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `e785f68`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `src/multi_claude/hook.py` | 新文件 | 新增 | 只依赖 `os`、`sys`、`tempfile` 的钩子入口：`HOOK_COMMAND`、`main(args)`、后台采集（§5.1.1） |
| `src/multi_claude/__main__.py` | :1-5 | 修改 | `sys.argv[1:2] == ["statusline-hook"]` 时只导入 `.hook` 并调用；否则照旧导入 `cli` |
| `src/multi_claude/statusline.py` | `HOOK_COMMAND` :27、`run_hook` :137-161 | 修改 / 删除 | `HOOK_COMMAND` 改为从 `hook` 导入；删除 `run_hook`（逻辑移入 `hook.py`）；`capture` 不变 |
| `src/multi_claude/cli.py` | `main` :213-218 | 修改 | 拦截改为调用 `hook.main(argv[1:])`，保留给以 `cli.main(argv)` 调用的场景 |
| `install.sh` | :93-99 之后 | 新增 | shim 解析为真实解释器，并做同样的单引号与版本检查 |
| `tests/test_statusline.py` | `HookTest` | 修改 | 采集改为异步后，正向用例轮询等待快照；负向用例用同步开关（§5.1.3） |
| `tests/test_install.py` | `InstallTest` | 新增用例 | 假 shim 场景 |
| `docs/feature/feature-statusline-usage.md` | §5.1.4、§7 | 修改 | 同步“先 exec、后台采集”和实测数字 |
| `README.md`、`README.zh-CN.md`、`CHANGELOG.md` | 安装一节、Unreleased | 修改 | shim 说明与条目 |

## 5. 方案

### 5.1 实现要点

#### 5.1.1 `hook.main(args)`

1. `len(args) != 1` 时：向 stderr 写用法，返回 2。
2. `data = sys.stdin.buffer.read()`。
3. 后台采集（任何 `OSError` 都只跳过采集）：
   - `pid = os.fork()`。
   - **中间进程**：`os.setsid()`；再 `fork` 一次，然后自己 `os._exit(0)`。
   - **孙进程**：
     - 先把 fd 0、1、2 都 `dup2` 到 `/dev/null`。
     - 在 `try` 里 `from . import statusline` 并调用 `statusline.capture(data, os.environ, now)`；`now` 在孙进程里取 `datetime.now(timezone.utc)`，`datetime` 也在孙进程里导入。
     - 不论成败都 `os._exit(0)`，绝不回到父进程的代码路径。
   - **钩子进程**：`os.waitpid(pid, 0)` 回收中间进程（它立即退出，不会阻塞）。
4. 把 `data` 写进 `tempfile.TemporaryFile()` 并 `dup2` 到 fd 0，然后 `os.execv("/bin/sh", ["/bin/sh", "-c", original])`。与 v0.2.0 相同；失败时向 stderr 写原因，返回 127。

**同步开关**：环境变量 `MULTI_CLAUDE_HOOK_SYNC=1` 时不 fork，在钩子进程里直接调用 `capture`（v0.2.0 的行为）。它只供测试判定“不写快照”的负向场景，README 不写。

#### 5.1.2 入口

`__main__.py`：

```python
import sys

if sys.argv[1:2] == ["statusline-hook"]:
    # 状态栏每次刷新都会执行它：只导入轻量的 hook，exec 之前不加载 cli 与其它模块。
    from .hook import main as hook_main
    sys.exit(hook_main(sys.argv[2:]))

from .cli import main

sys.exit(main())
```

字面量 `"statusline-hook"` 与 `hook.HOOK_COMMAND` 必须一致，由测试断言。`__main__` 里不导入 `hook` 来比较，是为了让非钩子命令连 `hook` 也不加载；这只是取舍，加载它的代价本身很小。

#### 5.1.3 测试改造

- **正向用例**（T2、T3 的写入一侧、T5、`test_captured_snapshot_is_read_back`）：走真实的异步路径，用 `wait_for(path)` 轮询，最长 10 秒。
- **T5 节流**：在两次调用之间等待前一次快照出现，避免两个后台进程交错。
- **负向用例**（T3 的不写入一侧、T4 的全部场景）：设 `MULTI_CLAUDE_HOOK_SYNC=1`，判定确定、无需等待。
- **新增 `test_capture_does_not_hold_output`**：后台采集期间钩子的 stdout 已经关闭。做法：用 `subprocess.Popen` 运行包装命令，在 1 秒内读到 stdout 的 EOF，并且之后快照仍会出现。

#### 5.1.4 `install.sh`

插入位置：`install.sh:99` 之后。

```sh
case "$PYTHON" in
  */shims/*)
    # pyenv / asdf / mise 的 shim 是一段脚本，每次启动多花几十毫秒；状态栏钩子每次刷新都要启动一次。
    REAL_PYTHON="$("$PYTHON" -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
    if [ -n "$REAL_PYTHON" ] && [ -x "$REAL_PYTHON" ]; then
      log "using ${REAL_PYTHON} instead of the shim ${PYTHON}"
      PYTHON="$REAL_PYTHON"
    fi
    ;;
esac
```

真实路径含单引号（启动命令无法转义）或解析失败时，保留已通过 :95-97 检查的 shim，不报错。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| `statusline-hook` | 采集改为后台进行；原命令的 stdout、stderr、退出码不变 | 快照最多晚几十毫秒出现；`usage` 读的是文件，不受影响 |
| 环境变量 `MULTI_CLAUDE_HOOK_SYNC` | 新增，内部用于测试 | 不写进 README |
| `install.sh` 生成的 `~/.local/bin/multi-claude` | 解释器为 shim 时写真实路径 | 需要重跑安装脚本才生效；`--uninstall` 不受影响 |
| `statusline.run_hook` | 删除（模块内部函数，无外部调用方，`grep -rn run_hook src tests` 只有 `cli.py:218` 一处） | 内部 |

不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- **只把重模块改成延迟导入、仍然同步采集**：采集要读配置和 `.claude.json`，再写文件，仍在显示路径上；`platform` 的 `ctypes` 也要改。后台采集能把它们一起移出显示路径，因此不采用。
- **安装脚本始终解析真实解释器**：Homebrew 的 `python3` 是稳定的软链，解析后会变成带小版本号的路径，`brew upgrade` 后就失效。所以只对 shim 解析。
- **包装命令里直接写真实解释器与 `PYTHONPATH`**：包装格式会变成多段，`unwrap` 的判定与已包装的文件都要迁移，代价大于改安装脚本。

## 7. 影响分析

**正向**

- 显示路径：解释器启动 + 读 stdin + 两次 `fork` + exec。
- 采集仍在每次刷新时触发，并受原有 60 秒节流约束：数值没变时，后台进程只读一次快照就退出。

**反向**

- **进程数**：每次刷新多出一个短命的后台进程（约 300 ms 一次的刷新频率下，同时存在的通常不超过 1–2 个）。节流命中时，它只读一个小文件就退出。
- **并发写**：两个后台进程可能同时写同一份快照。原子替换保证读到的都是完整文件，最后写入的胜出；两者都是近期数据。
- **测试环境**：后台进程继承测试的环境变量（`HOME`、`XDG_CONFIG_HOME`），只写进测试临时目录。
- **`--uninstall` / 降级**：v0.2.0 的钩子仍是同步采集，功能不变。
- **shim 解析**：pyenv 的 `global` 版本以后再变，multi-claude 仍用安装时那个版本，不受影响；只有卸载该版本时需要重跑安装脚本。README 写明。

**部署形态**：Linux（ubuntu20、CI）与 macOS 都支持 `fork`、`setsid`。

## 8. 回归测试

**环境**：本机与编译机 ubuntu20（Python 3.8.10）各跑一遍 `python3 -m unittest discover -s tests`。

| 编号 | 用例 | 判据 |
| ---- | ---- | ---- |
| T1 | 原有 statusline 用例（T1–T14）按 §5.1.3 改造后 | 全部通过 |
| T2 | `test_capture_does_not_hold_output` | 1 秒内读到 stdout EOF；随后快照出现 |
| T3 | `__main__` 字面量与 `hook.HOOK_COMMAND` 一致；`python -X importtime -m multi_claude statusline-hook true` 的导入列表不含 `multi_claude.cli`、`multi_claude.accounts` | 断言通过 |
| T4 | `install.sh` 遇到假 shim（`<tmp>/shims/python3` 转调真实 python） | 生成的启动命令写真实解释器路径，输出含 `instead of the shim`；非 shim 路径不变（现有用例） |
| T5 | 回归 | 现有全部用例通过（基线 255 个） |
| T6 | 性能（本机，记录数字，不进 CI） | 真实解释器下，经钩子与直接执行的中位数差不超过 40 ms；用重装后的启动命令再测一次 claude-hud 命令 |
| T7 | 实机 T15（发版、本机重装后） | 见 statusline 方案 §8 T15 |

## 9. 日志 / 观测点

- **安装脚本**：`[multi-claude-install] using <真实路径> instead of the shim <shim 路径>`。
- **钩子**：成功路径不输出；后台采集的 stdout / stderr 都已指向 `/dev/null`，排查时看快照文件的 `captured_at`。exec 失败时 stderr 输出 `multi-claude statusline-hook: cannot run /bin/sh: …`（与 v0.2.0 相同）。
