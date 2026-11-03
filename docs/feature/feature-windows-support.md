# multi-claude 支持原生 Windows

> 2026-10-29 注记：方案经 4 轮设计评审收敛（D1–D37 已修订）。基线 `origin/main` e3acd8b（v0.14.0 之后），分支 `feature/windows-support`。
>
> 2026-10-29 编码后注记：代码已按本方案落地（未提交），代码评审第 1 轮的问题已修。实现与正文的差异如下，以此为准：
>
> - 路径里含 `"`、`%`、`!` 时按**冲突**处理（`apply` 返回 3，与其它冲突一致），不是正文写的返回 2（`launcher.cmd_unsafe_reason`）。
> - `.cmd` 是按控制台代码页读取的：写进命令行的 `python` 与 `_entry.py` 路径**含非 ASCII 字符时**才改写（纯 ASCII 路径原样写入）：先试 `%USERPROFILE%`、`%LOCALAPPDATA%`、`%APPDATA%` 开头的写法（余下部分须为纯 ASCII，cmd 展开变量不经过代码页），不成立再换 8.3 短路径（`platform.short_path`），仍含非 ASCII 字符时报冲突。2026-11-03 实机核实：中文代码页下“用户”这类中文目录名本身就是合法的 8.3 名，Windows 不为它生成短名，只靠短名的话中文用户名下 pipx 默认安装位置一律报冲突；`_entry.py` 比较包目录时先 `realpath`（会展开短名），短名路径启动也不会误判“加载了另一份包”，提示把 multi-claude 装到不含这些字符的路径（`launcher.windows_command_paths`）。账号目录只出现在 `@rem` 注释行里，不受此限。
> - 程序内部调用的 `cmd.exe`、`powershell.exe` 一律用 `%SystemRoot%\System32` 下的绝对路径（`platform.system_program`），不靠 PATH 查找：不带路径时 Windows 会先查当前目录。
> - `launch`、`statusline-capture` 在 `main` 一开头就分派，不进 argparse，所以帮助、补全、拼错提示里都不会出现。
> - `launch` 遇到未登记的账号名时提示 `account '<名称>' is not registered; run multi-claude apply`，退出 1。
> - Windows 上 `run` / `code` 之外，`mcp`、`login` 调用 `claude` 也走同一个 `_call_command`（`which` 取完整路径 → `subprocess.call`）。
> - 迁移前的文件链接预检只看“登记账号时真正要新建的文件链接”：用 `accounts.plan` 只算不做，再排除搬迁后目标里已有的同名条目（`migrate._file_links_after_move`），不是共享目录里有文件就要求开发者模式。
> - CI 的 Windows 行只跑 `test_windows`：其它测试套件依赖 sh 启动命令、软链与权限位，原样搬到 Windows 上没有意义；`refresh` 锁等用例（原 §8 第 21 条后半）改列实机项。
> - “不得写死家目录”的检查仍用 `grep`（G5 原写改成 Python）：CI 已统一用 bash 执行，grep 在 Windows 行上同样可用。
> - 测试辅助：Windows 上假 `claude`、`security` 都是 Python 脚本外包一层 `.cmd`；POSIX 上仍用原来的 sh 版本（两者输出已逐字节对拍）。
> - 实机（Windows 11 中文版）补的一处：占用检查在 Windows 上不再输出“读不到 N 个进程的环境”的提示（Windows 读不到任何进程的环境，已按任何 Claude 进程都占用处理，这条提示只是噪音，且原文举的是 macOS 的例子）。
> - 实机结论（2026-11-03，Windows 11 中文版，PS 5.1，npm 版 Claude 2.1.195，第三方 API 无 OAuth 登录）：§8 第 8–15、18、19 条通过，第 12 条前后两半都已实测（有 Claude 运行时拒绝返回 4；全部关闭后不加跳过参数迁移成功，直接运行 `claude` 与 `claude-main` 正常；`restore` 同样）。第 16 条 R1 不满足：Ctrl+C 与 `/exit` 退出后 cmd 都会弹“终止批处理操作吗(Y/N)?”，随即自行消失；Claude 已退出、不丢内容，用户 2026-11-03 决定写进 README 限制、本版不改（备选是生成 `.ps1` 启动命令或每账号一个 `.exe`，前者受默认执行策略限制）。未实测：第 17 条 R2 凭据管理器后端（机器无 OAuth 登录）、第 22 条桌面版（未安装）、非管理员会话下的 1314 报错（SSH 会话为管理员）。
> - 实机补的第三处：状态栏后台采集进程的启动参数由 `DETACHED_PROCESS` 改为 `CREATE_NO_WINDOW`。pipx 环境的 `python.exe` 是转接程序，会再起真正的 `python.exe`（实机核实，并附带一个 conhost）；父进程没有控制台时子进程会得到一个可见的新控制台，表现为 Claude 每回复一次闪一下黑窗口。
> - 实机补的第二处：README 与帮助里的补全安装写法由 `>> $PROFILE` 改为 `| Add-Content $PROFILE`。Windows PowerShell 5.1 的 `>>` 按 UTF-16 追加，profile 已存在且为 ASCII / UTF-8 时追加后解析报错；`Add-Content` 在 profile 不存在、ASCII、UTF-8、UTF-16 四种情况下实测都能加载补全。
> - GitHub Actions 的 Windows 机器上补的三处：PowerShell 补全脚本只含 ASCII（Windows PowerShell 5.1 按 ANSI 读不带 BOM 的 `$PROFILE`）；路由路径必须带盘符或 UNC 前缀；进程列举超时由 30 秒放宽到 90 秒（WMI 冷启动实测超过 30 秒）。
> - Claude 桌面版的 `claude.exe` 可能被占用检查当成 Claude 进程，列为实机项（§8 第 22 条）；误判只会让迁移多拒绝一次，不会误放行。

## 1. 背景

multi-claude 目前只支持 macOS 与 Linux（`src/multi_claude/platform.py:53` `is_supported()`），主方案 `feature-account-manager.md` §11 把 Windows 列为二期。基线代码在原生 Windows 的 Python 上有以下问题（逐处核对过）：

- **包加载即失败**：`identity.py:22` 顶层 `import pwd`，`lock.py:8` 与 `artifacts.py:13` 顶层 `import fcntl`。`cli.py` 导入它们，`multi-claude` 在打印任何提示前就以 `ModuleNotFoundError` 退出。
- **启动命令不可执行**：`claude-<名称>`（`launcher.py:50-85`）与目录入口 `claude-auto`（`routes.py:78-121`）都是无扩展名的 `#!/bin/sh` 脚本。
- **误杀进程**：`platform.py:513-522` `_pid_alive` 用 `os.kill(pid, 0)` 判活。Windows 上 `os.kill` 对非控制台信号一律调用 `TerminateProcess`（Python 文档 `os.kill`：“Windows: … any other value for sig will cause the process to be unconditionally killed by the TerminateProcess API”），会结束 IDE 进程。
- **其它运行期失败**：`hook.py:60/71` 的 `os.fork` / `os.setsid`，`plan.py:224` 的 `signal.SIGHUP`，`bundle.py:120` 的 `os.fchmod`（3.13 前 Windows 没有），`cli.py:1160` 的 `os.execvpe`（Windows 上是“起子进程后父进程立即退出”，退出码丢失、两个进程抢控制台）。
- **路径判断**：多处用 `startswith("/")` 判断绝对路径（`config.py:336`、`platform.py:156/193`、`bundle.py:353`），用 `"/" in` 判断条目名（`cli.py:784/1001/1394`），前缀比较区分大小写且写死 `/`（`fsutil.py:28`、`routes.py:63`）。
- **链接**：`platform.create_link`（`:77-79`）与 `fsutil.py:215` 直接 `os.symlink`，不传 `target_is_directory`，没有开发者模式时报 WinError 1314；`fsutil.entry_kind`（`:41-61`）认不出 junction。
- **测试隔离**：`tests/helpers.py` 只设 `HOME`；Windows 上 `os.path.expanduser` 读 `USERPROFILE`，测试启动的子进程会读写真实的 `%USERPROFILE%\.claude`。

## 2. 目标 / 非目标

**目标**

1. 原生 Windows（Python ≥ 3.8，PowerShell 或 cmd）上，日常功能可用：`init` / `add` / `apply` / `list` / `doctor` / `remove` / `rename` / `set` / `env` / `proxy` / `share` / `route` / `run` / `code` / `mcp` / `login` / `sessions` / `handoff` / `export` / `import` / `usage` / `artifacts` / `statusline` / `completion`。电脑上原先装没装过 Claude Code 都能用。
2. 保守迁移：`migrate-default` 可以把已有的 `%USERPROFILE%\.claude` 迁成具名账号，迁移后登录不失效、直接运行 `claude` 的行为不变。只要电脑上还有任何 Claude 进程在运行就拒绝迁移。`restore` 可以撤回。不迁移时原默认安装完全不受影响。
3. macOS 与 Linux 的行为与输出不变：生成的启动命令、`claude-auto`、所有提示文字逐字节一致，现有测试全部通过。

**非目标**

- 不写 `install.ps1`。Windows 只支持 `pipx install multi-claude-cli`（PyPI 包已有 `[project.scripts]` 入口，`pyproject.toml`）。
- 不在 Windows 上逐进程判断“它用的是哪个账号”：读其它进程的环境变量要用未公开的 NT 接口，不做。
- 不支持 Windows 上 `statusLine` 配置里写了 `"shell": "powershell"` 的情况（见 §5.1 F1）。
- 不修复与 Windows 无关的既有问题，例如 `sessions.encode` 没有实现 Claude 对超过 200 字符的路径的截断。

## 3. 假设与约束

**外部行为依据**：Claude Code 2.1.306，从本机 `~/.local/share/claude/versions/2.1.306` 用 `strings` 提取内嵌 JS 后检索，可复核。macOS 构建里包含 Windows 分支的代码。

| 编号 | 事实 | 检索依据 |
| ---- | ---- | ---- |
| W1 | 凭据默认存在 `<配置目录>\.credentials.json`。服务端功能开关 `tengu_windows_credman` 为真，或设了 `CLAUDE_CODE_FORCE_WINDOWS_CREDMAN=1` 时，改用 Windows 凭据管理器，存储方式名为 `windows-credman-with-plaintext-fallback`。该开关从 `<配置目录>\.claude.json` 的 `cachedGrowthBookFeatures` 读取。 | `function vGo()`、`"windows-credman-with-plaintext-fallback"` |
| W1a | 凭据条目名与 macOS 钥匙串同一个函数 `RB()`：不设 `CLAUDE_CONFIG_DIR` 时不带后缀，设了时后缀为配置目录路径 sha256 的前 8 位。凭据管理器后端是否用 `RB()` 的结果作为条目名，没有在源码中找到直接调用点，按“与钥匙串相同”从严处理。 | `function RB(e=""){` |
| W2 | 状态栏与 hook 命令在 Windows 上默认用 Git Bash 执行（`requires bash but Git Bash was not found`）；配置里写 `"shell": "powershell"` 时用 PowerShell。Git Bash 的查找顺序：`CLAUDE_CODE_GIT_BASH_PATH` → `C:\Program Files\Git\bin\bash.exe` → `C:\Program Files (x86)\Git\bin\bash.exe` → PATH 上 `git` 的 `..\..\bin\bash.exe`。 | hook 执行函数 `aU`，`function RQ()` |
| W3 | `projects\` 下的目录名由 `replace(/[^a-zA-Z0-9]/g,"-")` 生成，各平台相同，`C:\x\y` 编为 `C--x-y`。与 `sessions.py:55` 的实现一致。 | `function k(e){return e.replace(` |
| W4 | 原生安装的可执行文件为 `claude.exe`；npm 安装为 `<prefix>\claude.cmd`，加上包内的 `node_modules\@anthropic-ai\claude-code\bin\claude.exe`。Windows 上后台会话宿主不设 `argv0`，命令行里带 `--bg-pty-host`。 | `execPaths:[x(e,"claude.cmd"),x(e,"claude.exe")]`，`O()!=="windows"&&{argv0:` |
| W5 | `claude daemon install` 只有 launchctl / systemd 两种形态，且当前版本已停用；Windows 上没有服务形态，只有按需启动的后台进程。 | `Install as a launchctl/systemd service` |
| W6 | Claude 的配置目录取 `CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")`，不做 realpath。Node 的 `os.homedir()` 在 Windows 上读 `USERPROFILE`。 | 主方案依据 1 |

**由以上依据推出的约束**

- **迁移不掉登录**：默认账号迁移后仍“不设 `CLAUDE_CONFIG_DIR`”，Claude 仍以字符串 `%USERPROFILE%\.claude` 找凭据。不论 W1 用的是哪种后端，条目名和文件路径都不变（文件经 junction 落到新目录）。这与 macOS 的规则（主方案 §5.1.8）相同。
- **已登记账号不能换路径**：凭据管理器后端下条目名随路径变化，与 macOS 相同。工具本来就不支持改路径。
- **Python 行为**（Python 3.8 起）：`os.readlink` 能读 junction；`os.path.islink` 对 junction 返回 False；`os.path.realpath` 会解析 junction；删除 junction 用 `os.rmdir`，不递归删除目标。Python 3.12 起有 `os.path.isjunction`，3.8–3.11 用 `os.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT` 判断。
- **junction 不需要特权**：`_winapi.CreateJunction(target, link)` 是 CPython 测试套件使用的私有接口（`Lib/test/test_os.py`），3.8 起存在。私有接口有变动风险，所以加一层兜底：`_winapi` 没有该函数时，改用 `cmd /c mklink /J`。
- **文件符号链接需要特权**：没有开启“开发者模式”且不是管理员时，`os.symlink` 报 `OSError`，`winerror == 1314`。
- **环境变量**：Windows 的 `os.environ` 键不区分大小写，`dict(os.environ)` 得到的都是大写键；在子进程环境里同时放 `HTTPS_PROXY` 和 `https_proxy` 会产生两条只差大小写的变量，哪条生效取决于被调程序。

**仍需实机确认**（不阻塞编码，结论写进 §8 实机项）

- R1：`.cmd` 启动命令里运行 Claude，按 Ctrl+C 或退出时，cmd 会不会弹出“终止批处理操作吗(Y/N)?”。
- R2：凭据管理器后端开启的机器上，迁移后登录是否保持（推导见上，需要实测）。
- R3：Windows Defender 等程序短暂占用文件时，原子替换的重试次数是否够用。
- R4：`refresh.py:224-325` 的锁目录靠 `os.utime(目录, ns=…)` 写入时间、再用 `(st_dev, st_ino, st_mtime_ns)` 认出是不是自己的锁。Windows 上 CPython 的 `os.utime` 对目录可用、`os.stat` 的 `st_ino` 取 NTFS 文件 ID，但时间精度为 100 ns，写入值会被截断。方案不改代码：`refresh` 的现有单测在 Windows CI 上会覆盖“写入后读回指纹一致”与“重建后指纹不同”；若失败，再在 Windows 分支把写入的纳秒值取整到 100 ns。

## 4. 涉及模块

行号是基线 e3acd8b 的锚点。

| 区域 | 行号锚点 | 类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| `platform.py` 平台判断 | `:53` `is_supported` | 修改 | 加入 `win32` |
| `platform.py` 默认路径 | `:70-74` `state_dir` | 修改 | Windows 用 `%APPDATA%\multi-claude` |
| `platform.py` 链接 | `:77-79` `create_link` | 修改 | Windows：目录建 junction，文件建符号链接，权限不足抛 `LinkPermissionError` |
| `platform.py` 链接 | `:79` 之后 | 新增 | `remove_link`、`is_junction`、`is_link` |
| `platform.py` 文件锁 | 新增于 `create_link` 之后 | 新增 | `lock_fd` / `unlock_fd`（POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking`）；`fcntl` 改为函数内导入 |
| `platform.py` 占用检查 | `:107-121` `find_busy_processes` | 修改 | Windows 分支：返回空列表，由 `find_claude_users` 的进程全表与 rename 失败兜底 |
| `platform.py` 进程识别 | `:235-257` `is_claude_process` | 修改 | 先把 `\` 换成 `/`、去掉末尾 `.exe` 再比较（POSIX 上这两步是空操作） |
| `platform.py` 进程识别 | `:260-265` `config_dir_of`、`:268-277` `daemon_stop_command` | 修改 | Windows 读 `USERPROFILE`；停止命令给 PowerShell 写法 |
| `platform.py` 进程枚举 | `:354-360` `_list_processes` | 修改 | 新增 `_list_processes_windows`（PowerShell `Get-CimInstance Win32_Process`） |
| `platform.py` 进程枚举 | `:280-330` `find_claude_users` | 修改 | Windows：环境一律读不出，任何 Claude 进程都判占用；不因 `readable == 0` 抛错 |
| `platform.py` 判活 | `:513-522` `_pid_alive` | 修改 | Windows 用 `OpenProcess` + `GetExitCodeProcess`，不调用 `os.kill` |
| `platform.py` 新增 | 文件末尾 | 新增 | `IS_WINDOWS`、`launcher_suffix()`、`find_git_bash()`、`retry_on_sharing_violation()` |
| `lock.py` | `:8`、`:30`、`:32-34`、`:45` | 修改 | 去掉 `import fcntl`，改调 `platform.lock_fd/unlock_fd`；`os.open` 加 `O_BINARY`；锁定后回到偏移 0 再写进程号 |
| `artifacts.py` | `:13`、`:247-253` | 修改 | 去掉 `import fcntl`，改调 `platform.lock_fd(blocking=True)`；`os.open` 加 `O_BINARY`；`:253` 的 `os.pread`（Windows 没有）改为 `platform.read_at` |
| `identity.py` | `:22`、`:61-66` | 修改 | `pwd` 改为在 `keychain_account` 函数内导入（调用点 `:86/134/223/273` 都在 darwin 分支） |
| `identity.py` | `:79-93` `probe` | 修改 | Windows 上没有凭据文件、且凭据管理器开关打开时返回 `unknown`，不判“未登录” |
| `__main__.py` | `:1-12` | 修改 | 平台闸门挪到导入 `cli` 之前；不支持时打印一行并返回 1 |
| `fsutil.py` | `:28-38` `is_under` | 修改 | 两边先 `os.path.normcase`，按 `os.sep` 拼接（POSIX 上 `normcase` 不改变字符串） |
| `fsutil.py` | `:41-61` `entry_kind` | 修改 | junction 判为 `KIND_LINK` |
| `fsutil.py` | `:64-90` `atomic_write` | 修改 | Windows：`os.replace` 遇共享冲突时退避重试；跳过 `os.chmod` |
| `fsutil.py`、`history.py` | `fsutil.py:124` `remove_path`；`fsutil.py:137/164/207`、`history.py:46` 的 `os.walk` | 修改 | 删除链接改 `remove_link`；遍历时把 junction 子目录从 `dirnames` 剔除（`os.walk` 对 junction 会深入） |
| `fsutil.py` | `:172`、`:215` | 修改 | `os.readlink` 改 `platform.read_link`；`os.symlink` 改 `platform.create_link` |
| `launcher.py` | `:39` `launcher_path` | 修改 | 文件名加 `platform.launcher_suffix()`（Windows 为 `.cmd`，POSIX 为空串） |
| `launcher.py` | `:50` `render` 之后 | 新增 | `render_windows(name, account_dir, python, entry)` |
| `src/multi_claude/_entry.py` | 新文件 | 新增 | Windows 启动命令与 hook 后台进程的入口（见 D2） |
| `launcher.py` | `:151-190` `rendered_account_dir`、`managed_account`、`scan_managed` | 修改 | 同时识别 `.cmd` 模板的标记行 |
| `routes.py` | `:25`、`:29-37`、`:78` | 修改 | 路由入口文件名加后缀；新增 `render_router_windows` |
| `routes.py` | `:42-46` `normalize_path`、`:63` | 修改 | 只在 Windows 分支：认 `~\`；前缀比较不区分大小写、按 `\` 切分。POSIX 原写法保留 |
| `accounts.py` | `:233` | 修改 | Windows 跳过 `chmod 0o700` |
| `config.py` | `:52`、`:332-350` `validate_route_path` | 修改 | 身份键加 `USERPROFILE` 等；路由路径用 `os.path.isabs` 判断，拒绝盘符根目录 |
| `env.py` | `:32-46` `_apply_proxy` | 修改 | Windows 只写大写代理变量 |
| `cli.py` | `:604-640` `main`、`:428` 附近子命令注册 | 修改 / 新增 | 只在 Windows 上注册隐藏子命令 `launch`、`statusline-capture` |
| `cli.py` | `:1146-1161` `_exec_as`、`:1232`、`:1251` | 修改 | Windows 用“`which` 取完整路径 → `subprocess.call`”代替 `execvpe` 与裸 `"claude"` |
| `cli.py` | `:784`、`:1001`、`:1394` | 修改 | 条目名同时拒绝 `\` 与 `:`；`_looks_like_path` 认 `\` 与盘符 |
| `cli.py` | `:1142`、`:1465`、`:2036`、`:2309-2335`、`:2073` | 修改 | 安装提示、PATH 提示、续聊命令、默认启动命令的 Windows 写法 |
| `doctor.py` | `:103`、`:171-173`、`:192-195` | 修改 | 安装提示；junction 断链；Windows 跳过权限位检查 |
| `migrate.py` | `:195`、`:316`、`:346`、`:356` | 修改 | 提示文字的 Windows 写法 |
| `migrate.py` | `:508`、`:584`、`:640`、`:673` | 修改 | `os.rename` 遇 `PermissionError` 按“被占用”处理 |
| `restore.py` | `:83`、`:86`、`:120` | 修改 | `os.unlink` 改 `platform.remove_link`；`:86` rename 遇占用见 E2；`:120` 改 `read_link` |
| `shared.py` | `:105`、`:114-130` | 修改 | 删除链接（`_replace_link`、`_unlink`）改为 `platform.remove_link` |
| `accounts.py` | `:280-316` `execute`，`:299` 之前 | 修改 | 只在 Windows：冲突判断与 dry-run 之后、`save_config` 之前做文件链接权限预检 |
| `hook.py` | `:16`、`:21-80` | 修改 | Windows 分支：不 fork、不 exec，用 Git Bash 跑原命令 |
| `statusline.py` | `:52-66` `wrap` / `unwrap`、`:68` `install` | 修改 | 可执行文件路径写成正斜杠；识别 `multi-claude.exe`；`"shell": "powershell"` 时拒绝包装 |
| `plan.py` | `:224` | 修改 | `SIGHUP` 用 `getattr` 取 |
| `bundle.py` | `:120`、`:353` | 修改 | `fchmod` 只在有该函数时调用；成员名拒绝盘符 |
| `sessions.py` | `:170-196` | 修改 | rename 与原子替换遇共享冲突时重试 |
| `shellpath.py` | `:28` `path_hint` | 修改 | Windows 给 PowerShell 写法 |
| `completion.py` | `:13`、`:67` `render` | 修改 / 新增 | 新增 `powershell` |
| `tests/helpers.py` | 全文、`:169` `run_launcher` | 修改 | 隔离 `USERPROFILE` / `APPDATA` / `LOCALAPPDATA`；假 `claude` 改为 Python 脚本；Windows 上启动命令加 `.cmd` |
| `tests/*.py` | 各处 | 修改 | POSIX 专属用例按平台跳过；新增 `tests/test_windows.py` |
| `.github/workflows/ci.yml` | 矩阵、`:33-41` | 修改 | 增加 `windows-latest` × 3.8 / 3.12；job 级 `defaults.run.shell: bash` |
| `pyproject.toml` | classifiers | 修改 | 加 `Operating System :: Microsoft :: Windows` |
| `README.md`、`README.zh-CN.md` | 安装一节 | 修改 | 删去“请用 WSL”，加 Windows（pipx）安装与限制 |
| `docs/feature/feature-account-manager.md` | §11 | 修改 | 指向本方案 |
| `CHANGELOG.md` | Unreleased | 新增 | 一条 Added |

## 5. 方案

### 5.1 实现要点

所有 Windows 分支都以 `platform.IS_WINDOWS`（`sys.platform == "win32"`）为条件，POSIX 分支保持原代码，不改字符串、不改顺序。

#### A. 能加载、不崩溃

- **A1 延迟导入**：
  - `identity.py:22` 的 `import pwd` 挪进唯一用到它的函数 `keychain_account`（`:61`），它只在 darwin 路径上被调用（`:86/134/223/273`）。
  - `lock.py`、`artifacts.py` 不再导入 `fcntl`，改调 `platform.lock_fd(fd, blocking)` / `platform.unlock_fd(fd)`。
  - 这两个函数在 POSIX 上函数内 `import fcntl`，调用 `flock(LOCK_EX | LOCK_NB)` 或 `flock(LOCK_EX)`，与原实现一致。
  - Windows 上：先 `os.lseek(fd, LOCK_OFFSET, SEEK_SET)`，再 `msvcrt.locking(fd, LK_NBLCK, 1)` 锁 1 字节；`unlock_fd` 同样先回到该偏移再 `LK_UNLCK`。`lock_fd` / `unlock_fd` 在成功与失败两种情况下都在 `finally` 里把文件位置恢复到 0：`lock.py:31-34` 加锁失败后直接 `os.read(fd, 64)` 读占用者进程号，位置留在 0x7FFFFFFF 会读到空。这样 `lock.py` 的读写位置逻辑不用改。`LOCK_OFFSET = 0x7FFFFFFF`，远超文件长度：Windows 的字节锁是强制锁，锁住的区域别的进程读不了；锁在文件末尾之外，`lock.py` 读写持锁进程号、`artifacts.load_handoffs`（`artifacts.py:284`）从头读交接日志都不受影响。Windows 允许锁定文件末尾之后的区域。阻塞模式下循环调用 `LK_NBLCK`，每次间隔 50 ms，避免 `LK_LOCK` 重试 10 次后抛错。失败抛 `BlockingIOError`，与 `flock` 非阻塞失败时的异常类型一致，调用方不改。
  - `lock.py` 读写持锁进程号的位置不变（偏移 0），两个平台相同，调用方不改。
  - `tests/test_accounts.py:697` 断言报错含 `pid <进程号>`，Windows CI 上同样运行；它在 `:699` 用 `send_signal(signal.SIGKILL)`，改为 `holder.kill()`（POSIX 上即发 SIGKILL，行为相同）。
  - `lock.py:30`、`artifacts.py:247` 的 `os.open` 加 `getattr(os, "O_BINARY", 0)`，避免 Windows 文本模式把 `\n` 写成 `\r\n`。POSIX 上该值为 0。
  - `artifacts.py:253` 的 `os.pread`：新增 `platform.read_at(fd, n, offset)`，POSIX 调 `os.pread`，Windows 用 `os.lseek` + `os.read`（此时已持锁，不与其它进程竞争；`O_APPEND` 写入不受读位置影响）。
- **A2 闸门前移**：`__main__.py` 在导入 `cli` 之前检查 `platform.is_supported()`，不支持时打印 `multi-claude: unsupported platform <sys.platform>` 并返回 1。`platform.py` 本身不导入 `pwd` / `fcntl`。`cli.py:633/1256` 原有的闸门保留。
- **A3 运行期崩溃**：
  - `plan.py:224` 改为 `(signal.SIGTERM, getattr(signal, "SIGHUP", None))`，并跳过 `None`。
  - `bundle.py:120` 改为 `if hasattr(os, "fchmod"): os.fchmod(...)`。Windows 上权限由用户目录的 ACL 保证，不额外设置。
- **A4 判活不杀进程**：`platform._pid_alive` 在 Windows 上用 ctypes：
  - `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)`：句柄为空且 `GetLastError() == ERROR_INVALID_PARAMETER`（87）时返回 False；为空但是其它错误（如拒绝访问）时返回 True，与 POSIX 的 `PermissionError` 分支一致。
  - 句柄非空时，`GetExitCodeProcess` 得到 `STILL_ACTIVE`（259）返回 True，否则返回 False，最后 `CloseHandle`。
  - 新增测试：用 `subprocess.Popen` 起一个 `python -c "import time; time.sleep(30)"`，调用 `_pid_alive` 后断言该进程仍在运行（`poll() is None`）。

#### B. 路径与默认位置

- **B1 默认路径**：
  - `state_dir()` 在 Windows 上返回 `%APPDATA%\multi-claude`；`APPDATA` 未设置时退回 `~\AppData\Roaming\multi-claude`。
  - `default_root()` 仍为 `~/.cc`，`default_bin_dir()` 仍为 `~/.local/bin`。后者正是 pipx 在 Windows 上的默认 `PIPX_BIN_DIR`，`pipx ensurepath` 会把它加进用户 PATH。
  - `default_source()` 仍为 `~/.claude`，`expanduser` 在 Windows 上读 `USERPROFILE`，与 W6 一致。
- **B2 绝对路径与条目名**：
  - `config.validate_route_path`：POSIX 分支不改。Windows 分支接受带盘符或 UNC 前缀的绝对路径（`os.path.isabs(path)` 且 `os.path.splitdrive(path)[0]` 非空；`\work` 这种随当前盘符漂移的写法拒绝），或 `~`、`~/`、`~\` 开头的写法；展开后等于盘符根目录（`os.path.splitdrive(p)[1] in ("\\", "/")`）时拒绝；拒绝 `\x00`。
  - `cli.py:784/1001`：条目名同时拒绝 `\` 与 `:`。
  - `cli._looks_like_path`：加上 `\`，以及 `re.match(r"^[A-Za-z]:", value)`。
  - `bundle.py:353`：已拒绝 `\`，再拒绝第二个字符为 `:` 的成员名。
  - `platform.py:156/193` 只在 lsof 和 `/proc` 分支里运行，Windows 不会走到，不改。
- **B3 前缀比较**：
  - `fsutil.is_under(path, root)`：比较前两边都做 `os.path.normcase`，拼接用 `os.sep`。POSIX 上 `normcase` 原样返回、`os.sep` 就是 `/`，行为不变。
  - `routes.resolve`（`:63`）：POSIX 保留原写法 `(here + "/").startswith(physical + "/")`。它与 `is_under` 不等价：规则目录解析成 `/` 时，原写法只命中 `/` 本身（与 sh 的 `case "$here/" in "$r"/*` 一致），`is_under(path, "/")` 命中所有路径。Windows 分支用同样的结构，只把两边换成 `normcase` 后的值、分隔符换成 `\`；盘符根目录 `C:\` 已被 B2 拒绝，不会出现。
  - `routes.normalize_path`（`:42-46`）：Windows 分支额外认 `~\` 开头的写法；POSIX 不改。
  - `routes.resolve` 里的 `os.access(target, os.X_OK)` 在 Windows 上对目录恒为真，等价于只判断目录存在，不改。
- **B4 身份变量**：`config._IDENTITY_KEYS` 增加 `USERPROFILE`、`HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA`，`env` 命令拒绝设置它们，理由与 `HOME` 相同。这是新增拒绝项：如果已有用户在 macOS 上设了同名变量，`apply` 会报错。在 §7 单列，并在 CHANGELOG 说明。
- **B5 代理变量**：`env._apply_proxy` 在 Windows 上只写、只删大写键（`HTTPS_PROXY`、`HTTP_PROXY`、`NO_PROXY`、`ALL_PROXY`）。删除时按 `name.upper()` 比较所有键，覆盖用户环境中的小写写法。
- **B6** `platform.config_dir_of`：Windows 上 `HOME` 换成 `USERPROFILE`。
- **B7 登录状态（依据 W1）**：凭据管理器后端下没有 `.credentials.json`，现有 `identity.probe`（`:79-93`）会判为 `none`。Windows 分支：没有凭据文件时，读该账号的全局配置文件（默认账号为 `~\.claude.json`，其它为 `<目录>\.claude.json`，与 `accounts.py:386-387` 同一规则）里 `cachedGrowthBookFeatures.tengu_windows_credman`，或环境变量 `CLAUDE_CODE_FORCE_WINDOWS_CREDMAN`；任一为真时返回 `unknown`，否则返回 `none`。
  - 由此 `list`、`doctor` 显示 `unknown`，`add` 不误提示“next: login”（`cli.py:1384` 只在 `none` 时提示，编码时核对）。
  - `read_oauth_token` / `read_credentials` 在凭据管理器后端下读不到令牌，`list` 的套餐与 `usage` 刷新按现有“读不到凭据”的分支显示（不读凭据管理器，属非目标）。README 写明这一限制。

#### C. 链接

- **C1 `platform.create_link(target, link_path)`**：
  - POSIX 不变。
  - Windows 上，`target` 是目录（`os.path.isdir(target)`）时建 junction：先用 `_winapi.CreateJunction(target, link_path)`，`_winapi` 没有该属性时用 `subprocess.run(["cmd", "/c", "mklink", "/J", link_path, target])`，返回码非 0 时抛 `OSError`。
  - `target` 是文件时调用 `os.symlink(target, link_path)`。捕获 `OSError` 且 `winerror == 1314` 时，抛 `LinkPermissionError(OSError)`，消息为：`creating a file link needs Developer Mode or an administrator shell: Settings > System > For developers > Developer Mode`。
  - junction 只能指向本机的绝对路径。目标在网络路径（`\\` 开头）上时抛 `OSError`，并说明原因。
- **C2 识别与删除**：
  - `platform.is_junction(path)`：3.12 起用 `os.path.isjunction`，否则用 `lstat().st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT`（`0xA0000003`）；非 Windows 恒为 False。
  - `platform.is_link(path)` 定义为 `os.path.islink(path) or is_junction(path)`。
  - `platform.remove_link(path)`：junction 用 `os.rmdir`；目录符号链接在 Windows 上也用 `os.rmdir`；其它情况用 `os.unlink`。
  - `fsutil.entry_kind` 遇到 junction 返回 `KIND_LINK`。所有按 `KIND_LINK` 分支的调用方（shared、accounts、doctor、bundle、migrate）自动得到正确分类。
  - `os.readlink` 对 junction 返回目标路径。在 Windows 上它可能带 `\\?\` 前缀，统一在 `platform.read_link` 里去掉该前缀；POSIX 上 `read_link` 就是 `os.readlink`。
  - 替换点：`shared.py:122`（`_replace_link`）与 `:127-130`（`_unlink`，`:105` 经它删除）、`restore.py:83` 的 `os.unlink`；`doctor.py:171`、`sessions.py:148` 的 `os.path.islink`；`fsutil.py:172`、`:215`、`restore.py:120` 的 `os.readlink`，改为调用上述函数。`fsutil.py:124` `remove_path` 对 `KIND_LINK` 的 `os.unlink` 也改为 `platform.remove_link`（junction 改判为 `KIND_LINK` 后会走到这里）。
  - **遍历不进 junction**：`os.walk(followlinks=False)` 靠 `os.path.islink` 决定是否深入（CPython 3.11 `os.py:418`、3.13 `os.py:433`），而 `islink` 对 junction 返回 False，所以会深入。`fsutil.py:137`（补权限）、`:164`（清单）、`:207`（复制）三处在每层遍历后加 `dirnames[:] = [n for n in dirnames if not platform.is_junction(join(dirpath, n))]`；POSIX 上 `is_junction` 恒为 False，行为不变。`history.py:46` 的 `os.walk(projects)` 同样剪枝（`projects\` 里本不该有 junction，剪掉只防止统计重复）。`bundle.py:147/489` 已按 `entry_kind(...) == KIND_DIR` 剪枝，`entry_kind` 认出 junction 后自然跳过，不再改。
- **C3 `fsutil.py:215`**：复制目录树时遇到链接，改调 `platform.create_link(platform.read_link(full), target)`；Windows 上源是 junction 时目标也建 junction（目标目录不存在时 junction 仍可建立，与 POSIX 悬空软链一致）。
- **C4 报错出口**：默认共享条目含文件 `CLAUDE.md`（`config.py:18`），普通 Windows（未开开发者模式）上建不了文件符号链接。`shared.py:17-106` 只规划动作，执行在 `accounts.execute`（`accounts.py:280-316`）：先 `save_config`（`:299-305`），再逐条 `action.run()`（`:306-315`）。为避免“配置已写、链接只建了一半”：只在 Windows 上，在 `execute` 的冲突判断与 dry-run 返回之后、`:299` 写配置之前，检查动作里是否有 kind 为共享链接、状态为新建或更新、且源不是目录的项；有的话在 `%TEMP%` 里试建一个文件符号链接再删掉。失败就什么都不写，返回 1，点名涉及的账号，提示两条出路：开启开发者模式，或运行 `multi-claude set <账号> --shared-exclude CLAUDE.md` 把文件排除。
  - 该预检在 `execute` 里，`migrate-default` 的登记阶段（`migrate.py:610` `_register`，此时数据已移动、junction 已建好）也会走到；失败时按现有出口报 `data is in place but registration failed`，排除或开启开发者模式后重跑即可。为避免这种半途状态，`migrate.py:542` 的移动前预检也做同一检查（只在 Windows、只在有其它账号的共享文件链接待建时）。`_create_link` 仍捕获 `LinkPermissionError` 兜底。migrate 的 `~\.claude` 是目录，走 junction，不会遇到这个错误。

#### D. 启动命令与目录入口

- **D1 文件名**：`launcher.launcher_path` 与 `routes.router_path` 加上 `platform.launcher_suffix()`。Windows 为 `.cmd`（在 PATHEXT 里，cmd 与 PowerShell 都能直接运行 `claude-work`），POSIX 为空串。
- **D2 模板**：`launcher.render_windows(name, account_dir, python)` 生成：

```bat
@echo off
@rem managed-by: multi-claude account=<名称>
@rem account_dir=<目录>
"<python>" -I "<包目录>\_entry.py" launch <名称> -- %*
exit /b %ERRORLEVEL%
```

  - `python` 取 `sys.executable`（pipx 虚拟环境里的 `python.exe`），不依赖 PATH 上的 `multi-claude`。
  - 必须带 `-I`，且不用 `-m`：`-m` 会把当前目录插到 `sys.path` 最前面，用户在含 `json.py`、`subprocess.py` 等文件的项目目录里运行启动命令时，这些文件会顶替标准库被执行。`-I` 不把当前目录或脚本所在目录加入 `sys.path`，也忽略 `PYTHON*` 环境变量与用户 site-packages（CPython 3.8 `pymain_run_python` 在 isolated 时跳过 path0）。
  - 因为 `-I` 同时忽略 `PYTHONPATH`，只靠解释器默认的 `sys.path` 找包，源码检出（测试经 `PYTHONPATH=src` 运行，`tests/helpers.py:113`）里会找不到 `multi_claude`。所以入口不用 `-m multi_claude`，而是包内新增的 `_entry.py`（绝对路径在 `apply` 时取 `os.path.dirname(multi_claude.__file__)`）。它做三件事：① 把包的上一级目录**追加**到 `sys.path` 末尾（已在其中则不加；追加而非插到最前，避免 site-packages 里的同名模块顶替标准库）；② `import multi_claude` 后断言 `multi_claude.__file__` 落在 `_entry.py` 所在目录，不一致时报错退出 1（防止解释器的 site-packages 里另装了旧版、测到别的版本）；③ `runpy.run_module("multi_claude", run_name="__main__", alter_sys=True)`，与 `python -m multi_claude` 走同一份 `__main__.py`（含 `statusline-hook` 的轻量分流与平台闸门）。现有 `__main__.py` 没有 `main()` 函数、整份在模块顶层执行，所以不能写 `from multi_claude.__main__ import main`。安装副本与源码检出走同一条路，测试不需要 `pip install .`。
  - `-I` 还有一个必要理由：`_entry.py` 在包目录里，包里有 `platform.py`、`config.py` 等与标准库同名的模块；不带 `-I` 时脚本所在目录会成为 `sys.path[0]`，`multi_claude/platform.py` 会顶替标准库 `platform`。（`-I` 自 3.4 起可用。）
  - 包目录路径同样受 D2 下一条的字符限制。
  - 名称已由 `config` 校验，只含 `[A-Za-z0-9._@+-]`；生成前断言目录与 `python` 路径里没有 `"`、`%`、`!`、换行，出现时按冲突处理，`apply` 返回 3 并说明原因。这三个字符在 `.cmd` 里无法可靠转义。
  - `python` 与 `_entry.py` 路径含非 ASCII 字符时，写进命令行前先试改写成 `%USERPROFILE%` 等变量开头，再试 8.3 短路径；仍含非 ASCII 字符时按冲突处理：cmd.exe 按控制台代码页读 `.cmd`，非 ASCII 路径会读成乱码。
  - 标记行与 POSIX 版语义相同（`MARKER_PREFIX` 去掉 `# ` 前缀后加 `@rem `）。`managed_account` / `rendered_account_dir` 按文件扩展名选择解析方式。
- **D3 隐藏子命令 `launch NAME [-- ARGS…]`**：
  - 在 `cli.py` 注册为 `argparse.SUPPRESS` 帮助的子命令，参数手工切分（与 `run` 的 `--` 处理相同，`cli.py:359`）。
  - 行为等同 `run NAME` 加用户参数：`command = ["claude"] + [expand_value(a) for a in effective_args(account)] + ARGS`，然后走 D4 的运行方式。
  - 不打印“using account”提示：启动命令的输出必须与直接运行 `claude` 相同。
  - `launch` 不带名称时供 `claude-auto.cmd` 使用（`.cmd` 最后一行为 `"<python>" -I "<包目录>\_entry.py" launch -- %*`），按 `routes.resolve(config, os.getcwd())` 的结果逐分支对照 `routes.render_router`（`:78-121`）：
    - 命中规则或默认账号，且账号已登记：等同 `launch <该账号>`。
    - 命中规则或默认账号，但账号未登记（用户手改了 config.json）：POSIX 版会 `exec` 一个不存在的启动命令，sh 报 `not found`、退出 127。Windows 版打印 `multi-claude: account '<名称>' used by the route is not registered; run multi-claude doctor`，退出 127。
    - 都没命中：以原环境（不加账号环境、不加固定参数）运行 `claude` 加用户参数，不打印任何提示；找不到 `claude` 时打印 `multi-claude: claude not found in PATH`、退出 127，与 `routes.py:114-117` 相同，不带安装提示。
  - `launch NAME` 的失败文案逐条对齐 POSIX 模板（`launcher.py:59-97`），不复用 `_usable_account_env` / `_exec_as` 的报错（它们带 `[multi-claude] error:` 前缀与安装提示）：账号目录不存在时打印 `multi-claude: account directory does not exist: <目录>`（与 `launcher.py:63-66` 同文）；默认身份的链接检查失败时同 `launcher.py:94-97` 的文案；前两种失败与模板一样退出 1（`launcher.py:67`、`:97`）；找不到 `claude` 时打印 `multi-claude: claude not found in PATH`、退出 127。编码时逐条从模板抄文案与退出码，并加单测比对。
  - 两个隐藏子命令只在 Windows 上注册：`argparse.SUPPRESS` 只隐藏帮助，`completion.collect`（`completion.py:50-53`）和 `_unknown_command` 的相近词提示（`cli.py:1304-1310`）会遍历全部子命令。只在 Windows 注册，POSIX 的补全脚本与拼错提示逐字节不变；Windows 上这两处另外排除这两个名字。
- **D4 Windows 上运行子程序**：`cli._exec_as`、`cmd_mcp`（`:1232`）、`cmd_login`（`:1251`）在 Windows 上：
  1. `exe = platform.which(command[0], env["PATH"])`：按 `PATH` 各项与 `PATHEXT` 逐个查找，**不查当前目录**，能找到 `claude.exe`，也能找到 npm 的 `claude.cmd`。不用 `shutil.which`：它在 Windows 上会先查当前目录（3.8–3.11 无条件；3.12 起由 `NoDefaultCurrentDirectoryInExePath` 决定，默认仍查），项目里放一个 `claude.cmd` 就会被执行；POSIX 的 `execvpe` 与 PowerShell 都不查当前目录。`statusline install` 找 `multi-claude` 时同样改用它。命令名带路径分隔符时原样使用。
  2. `signal.signal(SIGINT, SIG_IGN)`：Ctrl+C 会同时发给同一控制台里的所有进程，父进程忽略它，交给 Claude 自己处理；
  3. `rc = subprocess.call([exe] + command[1:], env=env)`，返回 `rc`。
  - `.cmd` 目标（如 npm 的 `claude.cmd`、VS Code 的 `code.cmd`）由 `subprocess` 交给 `cmd.exe` 执行，参数按 `list2cmdline` 规则转义。参数中含 `"`、`%`、`^` 时 cmd 的二次解析可能改变参数，这是 Windows 上调用 `.cmd` 的固有限制，在 README 中注明。
  - POSIX 仍用 `execvpe`。
- **D5 过期判断**：Windows 启动命令只含名称、目录和 Python 路径；环境、代理、固定参数在运行时从配置读取。所以 `list` 的“启动命令过期”只在这三项变化时出现，改代理后不需要 `apply` 就会生效。这与 POSIX 不同，在 README 中写明。

#### E. 保守迁移

- **E1 占用检查（Windows）**：
  - `find_busy_processes` 在 Windows 上返回空列表：Windows 没有 lsof，也没有 `/proc`。按路径查的占用由 E2 的 rename 失败兜底。
  - `_list_processes_windows` 运行 `powershell -NoProfile -NonInteractive -Command "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"`，超时 90 秒（CI 实测 WMI 冷启动第一次查询超过 30 秒）。
    - 命令行用 `subprocess.list2cmdline` 的逆过程拆成 argv：实现 `CommandLineToArgvW` 的 ctypes 调用，不自己写解析。
    - 环境一律记为 `None`。
    - 启动失败、超时、输出不是 JSON 时抛 `BusyCheckError`，迁移拒绝执行（与现有“检查本身失败”的处理相同）。
  - `find_claude_users` 在 Windows 上：任何 `is_claude_process` 为真的进程都判为占用，`path` 记为 `(any Claude process blocks migration on Windows)`；不因“一个环境都读不出”而抛错；保留 IDE 锁检查（用 A4 的判活）；`_daemon_service` 因为 `daemon_service_path()` 返回 None 而不报占用（W5）。
  - `is_claude_process` 比较前把 argv[0] 与 exe 中的 `\` 换成 `/`，并去掉末尾 `.exe`，不区分大小写。npm 的 `claude.cmd` 进程本身是 `cmd.exe`，它拉起的 `claude.exe` 会被识别。
  - `_check_pid_namespace` 本来只在 Linux 上运行，不改。
  - `migrate.py:310-322` 里的 `CHILD_SESSION_ENV` 检查（第 0 条）照常生效。
- **E2 rename 失败即占用**：Windows 上 `os.rename` 遇到 `PermissionError` 且 `winerror in (5, 32)` 时，提示统一为 `<路径> is in use by another program (winerror N); close all Claude sessions, IDEs and terminals opened in it, then retry`。各调用点所处阶段不同，逐点写定：
  - `migrate.py:508`（移动步骤，此时数据还没动）：与同函数 `:497-502` 占用检查失败时相同，先 `_delete_journal()` 再 `raise MigrationStop(4)`。不能留下 planned 阶段的事务记录，否则会挡住其它写命令。
  - `migrate.py:584`（源目录仍完好）：保留事务记录，`raise MigrationStop(4)`，关闭占用后重跑 `migrate-default` 可继续。
  - `migrate.py:640`、`:673`（回滚路径里）：不改出口，仍由现有 `try/except` 转成返回 1 并打印现场；只在提示里补上占用原因与 winerror。
  - `restore.py:86`：返回 4，此时 `:83` 已删掉原处的链接，所以先用 `platform.create_link` 把链接建回再返回；建回也失败时打印现场（两个路径）并返回 1。
  - `_run` 对 `MigrationStop` 与普通异常的分流（`migrate.py:417-425`）不改。
- **E3 原处的链接**：`migrate.py:600` 用 C1 建 junction；`restore.py:83` 用 `remove_link` 删除。
- **E4 提示文字**（只改 Windows 分支）：
  - `migrate.py:195/346`：`unset CLAUDE_CONFIG_DIR` → `Remove-Item Env:CLAUDE_CONFIG_DIR`。
  - `migrate.py:316`：`env -u X multi-claude ...` → `$env:X=$null; multi-claude ...`。
  - `migrate.py:356`：launchd / systemd 一句在 Windows 上不会出现（W5）。
  - `platform.daemon_stop_command` 的 Windows 写法不做：`find_claude_users` 在环境读不出时直接 `continue`（`platform.py:303-308`），Windows 上环境一律读不出，它永远不会被调用。改为：Windows 上占用列表非空时，`migrate.py:350-354` 处固定多打一行 `if a background Claude session is running, stop it with: claude daemon stop --any`。
- **E5 没装过 Claude**：`migrate-default` 在 `~\.claude` 不存在时的现有行为不变；`add` 建新账号不依赖 `~\.claude`。

#### F. 其它日常功能

- **F1 状态栏**（依据 W2）：
  - `statusline.wrap`：Windows 上可执行文件路径先把 `\` 换成 `/`（Git Bash 能识别 `C:/…`），再用 `shlex.quote`，原命令仍用 `shlex.quote`。这样包装后的命令在 Git Bash 中的解析与 POSIX `/bin/sh` 相同。
  - `statusline.unwrap`：比较 `basename` 前去掉 `.exe`，不区分大小写。
  - `statusline.install` 判“已包装且路径相同”（`statusline.py:72` `wrapped[0] == executable`）：Windows 上两边都转成正斜杠并 `normcase` 后再比，否则每次都判成重新包装并多出一份备份。
  - `statusline.install`：Windows 上，settings 的 `statusLine.shell == "powershell"` 时拒绝包装，返回 1，提示 `statusline wrapping supports the default Git Bash shell only`。
  - Git Bash（MSYS）调用 Windows 原生程序时，会把以 `/` 开头的参数改写成 Windows 路径（如 `/c/Users/me/x.sh` → `C:/Users/me/x.sh`）。原命令作为一个参数传给 `multi-claude.exe`，被改写后再交给 Git Bash 执行，语义可能改变。所以 Windows 的包装命令前缀为 `MSYS2_ARG_CONV_EXCL='*' `，关闭该改写；`unwrap` 识别时先去掉这一前缀。
  - 这个变量会随环境传给原命令及其调用的程序，改变它们的路径改写行为（如原脚本里 `node /c/Users/...`）。所以前缀实际为 `MULTI_CLAUDE_MSYS_ORIG="${MSYS2_ARG_CONV_EXCL-__unset__}" MSYS2_ARG_CONV_EXCL='*' `：hook 的 Windows 分支起 bash 前，按 `MULTI_CLAUDE_MSYS_ORIG` 还原 `MSYS2_ARG_CONV_EXCL`（值为 `__unset__` 时删除），并删掉 `MULTI_CLAUDE_MSYS_ORIG`，原命令看到的环境与不包装时相同。单测断言 bash 子进程环境中两个变量的取值。该 MSYS 行为仓库内无法验证，列为实机项（§8 第 18 条）。
  - `hook.main` 的 Windows 分支：
    - 不 `fork`：用 `subprocess.Popen([sys.executable, "-I", <包目录>\_entry.py, "statusline-capture"], stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)` 把 stdin 内容交给后台采集，写完立即关闭其 stdin。stdout / stderr 必须显式给 `DEVNULL`：Windows 上只要设了一个标准句柄，其余未给的会继承父进程的（CPython `subprocess.py` `_get_handles`），后台进程就会一直握着 Claude 读状态栏输出的管道，与 `hook.py:51-56` 特意改到 `/dev/null` 的意图相反。`statusline-capture` 是只在 Windows 上注册的隐藏子命令，复用 `statusline.capture`。
    - 再用 `platform.find_git_bash()`（实现 W2 的查找顺序）运行 `[bash, "-c", 原命令]`，stdin 写入同一份数据，`sys.exit(rc)`。
    - 找不到 Git Bash 时返回 127，并向 stderr 写明原因（与现有 exec 失败的出口相同）。
    - POSIX 分支不改。
- **F2 doctor**：Windows 跳过 `:192` 的权限位检查；`:171` 的断链检查改为 `platform.is_link`。
- **F3 PATH 与安装提示**：
  - `shellpath.path_hint`：`system == "Windows"` 时返回 `run: [Environment]::SetEnvironmentVariable('Path', '<目录>;' + [Environment]::GetEnvironmentVariable('Path','User'), 'User'), then open a new terminal`；或者在已用 pipx 安装时，提示 `pipx ensurepath`。
  - `cli.py:1142`、`doctor.py:103` 的 Claude 安装提示在 Windows 上为 `irm https://claude.ai/install.ps1 | iex`，并附官方安装页地址。
  - `bin_dir` 与 PATH 各项比较时先做 `normcase`。
- **F4 续聊命令**（`cli.py:2036`、`:2309-2335`）：Windows 上输出 PowerShell 写法 `Set-Location '<目录>'; claude-<名称> --resume <id>`，路径用单引号（PowerShell 单引号串内只需把 `'` 写成 `''`），不输出 `~/…` 形式。
- **F5 原子写重试**：新增 `platform.retry_on_sharing_violation(fn)`。Windows 上遇到 `PermissionError` 且 `winerror in (5, 32)` 时，按 50/100/200/400/800 ms 退避重试 5 次，仍失败就抛出原异常；POSIX 直接调用 `fn`。用于 `fsutil.atomic_write` 的 `os.replace`、`sessions.py:170/180/193`、`bundle.py:135/458`。
- **F6 补全**：`completion.SHELLS` 加 `powershell`，生成一段 `Register-ArgumentCompleter -Native -CommandName multi-claude -ScriptBlock {…}`。脚本复用 `collect()` 得到的子命令与选项表；账号名用 `multi-claude list --names`（如果没有这个选项，编码时按现有 bash 生成器取账号名的方式实现）。安装方法写进帮助：`multi-claude completion powershell | Add-Content $PROFILE`（不用 `>>`：Windows PowerShell 5.1 按 UTF-16 追加，接在已有 ASCII / UTF-8 的 profile 后面会把它弄坏，实机已复现）。
- **F7 默认启动命令**：`cli.py:2073` 的 `_DEFAULT_LAUNCHER`（`env -u … claude`）在 Windows 上换成 PowerShell 写法 `$env:CLAUDE_CONFIG_DIR=$null; $env:CLAUDE_SECURESTORAGE_CONFIG_DIR=$null; claude`（两个变量都清，与 POSIX 写法一致），只用于显示。

#### G. 测试与 CI

- **G1 隔离**：`tests/helpers.py` 构造子进程环境时，同时把 `HOME`、`USERPROFILE`、`APPDATA`、`LOCALAPPDATA` 指向临时目录，删除 `HOMEDRIVE` / `HOMEPATH`。`assert_safe_env` 增加断言：在该环境下 `os.path.expanduser("~")` 落在临时目录内（Windows 上用子进程求值）。
- **G2 假程序**（shebang 写 `sys.executable` 的绝对路径，不用 `/usr/bin/env python3`：测试 PATH 只有 fakebin 与系统目录（`helpers.py:75`），会找到系统 python3 而不是当前解释器；输出格式与原 sh 版逐字节相同：原版用 `env` 打印环境，假程序按 `env` 的格式逐行输出 `k=v`，值含换行时原样输出）：假 `claude`、假 `security` 改为 Python 脚本（首行 `#!<sys.executable 的绝对路径>` 加 `chmod 755`），Windows 上另生成同名 `.cmd`，内容为 `@"<sys.executable>" "<脚本>" %*`。假 `security` 只在 darwin 用例中使用，Windows 上不生成。测试 PATH 在 Windows 上为 `fakebin` 加 `%SystemRoot%\System32`。
- **G3 平台跳过**：依赖 sh 模板内容、`os.geteuid`、`os.mkfifo`、`AF_UNIX`、权限位、`/proc` 的用例加 `@unittest.skipIf(sys.platform == "win32", "<原因>")`。`test_sessions.py:355` 装饰器里的 `os.geteuid()` 改成 `getattr(os, "geteuid", lambda: -1)()`。
- **G4 新增 `tests/test_windows.py`**：其中大部分用例在 POSIX 上也能跑，通过 mock `IS_WINDOWS` 测试纯函数。
  - `render_windows` 的内容与标记识别；拒绝 `"`、`%`、`!`。
  - `launch` 的参数顺序：固定参数在前，用户参数在后。
  - `is_claude_process` 识别 Windows 形态的 argv / exe。
  - `CommandLineToArgvW` 解析的输入输出；PowerShell 输出 JSON 的解析，以及解析失败抛 `BusyCheckError`。
  - `validate_route_path` 接受 `C:\work`，拒绝 `C:\`。
  - `is_under` 不区分大小写；`statusline.wrap` / `unwrap` 的往返；`retry_on_sharing_violation` 的重试次数。
  - 只在 Windows 上运行的：junction 的建立、识别、删除（删除后目标仍在）；源目录内含 junction 时 `copy_tree` / `build_manifest` 不深入；交接日志连续两次追加；当前目录有 `claude.cmd` 时 `platform.which` 不选它；`statusline-capture` 后台进程不继承 stdout（hook 在 2 秒内返回）；文件链接缺权限时抛 `LinkPermissionError`（mock `os.symlink` 抛 `winerror=1314`）；`msvcrt` 锁的互斥；`_pid_alive` 不结束目标进程；`migrate` 在 rename 抛 `PermissionError(winerror=32)` 时返回 4。
- **G5 CI**：
  - 矩阵加 `windows-latest` × 3.8 与 3.12。
  - “测试中不得写死家目录”的检查从 `grep` 改为一个 Python 单行命令，各平台通用。
  - job 级加 `defaults: run: shell: bash`：windows-latest 默认用 PowerShell 执行 `run`，而“README links”等步骤是 bash 语法（`ci.yml:33-38`）；runner 自带 Git Bash。
  - shellcheck、lsof 安装步骤只在 Linux 上运行。
  - `tests/helpers.py:169` `run_launcher` 在 Windows 上执行 `claude-<名称>.cmd`。

### 5.2 接口变更

| 接口 | 变更 | 兼容性 |
| ---- | ---- | ---- |
| 支持平台 | 新增 Windows | 新增 |
| 启动命令文件名 | Windows 为 `claude-<名称>.cmd`、`claude-auto.cmd` | POSIX 不变 |
| 子命令 `launch`、`statusline-capture` | 新增，只在 Windows 上注册，帮助、补全、拼错提示中都不出现 | POSIX 子命令集合不变 |
| `completion powershell` | 新增取值 | 新增 |
| `env` 拒绝的键 | 增加 `USERPROFILE`、`HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA` | 收紧，见 §7 第 6 条 |
| 路由路径 | Windows 接受 `C:\…` | POSIX 不变 |
| 工具状态目录 | Windows 为 `%APPDATA%\multi-claude` | 新增 |
| 退出码 | Windows 上 rename 遇占用返回 4；共享文件链接缺权限返回 1 | 与现有语义一致 |
| 登录状态 | Windows 凭据管理器后端下 LOGIN 显示 `unknown` | 新增取值场景，值域不变 |

**reference sibling 回补检查**：N/A，本仓库没有 `docs/reference/` 目录。

## 6. 备选方案与决策

- **Windows 启动命令把环境写死在 `.cmd` 里**（与 POSIX 模板对等）：要在 cmd 里转义任意值，`%`、`"`、`!`、`^` 无法全部可靠处理，还要再维护一套模板。否决，改为 `.cmd` 只转交给 Python（D2/D3），环境由已有的 `env.account_env` 生成。代价是每次启动多一次 Python 启动（约 0.1–0.3 秒），以及 D5 的过期语义差异。
- **占用检查引入 psutil**：能列出打开的文件，但违反“只用标准库”（主方案 §3）。否决，改用 PowerShell CIM 查询加 rename 失败兜底。
- **文件链接退化为复制或硬链接**：复制会让共享失去意义；硬链接在编辑器原子保存后会断开，而且不能跨卷。否决，缺权限时报错并提示开启开发者模式。
- **`install.ps1`**：用户决定只用 pipx。

## 7. 影响分析

1. **POSIX 行为不变**：
   - 所有改动都放在 `IS_WINDOWS` 分支里，或者是在 POSIX 上等价的改写：`normcase` 原样返回；`os.sep == "/"`；`.exe` 后缀与 `\` 替换在 POSIX 路径上不改变内容；`getattr(signal, "SIGHUP")` 等于原值；`launcher_suffix() == ""`。
   - 等价改写是本方案对既有函数体的修改，逐条在 §8 第 1、2 条回归。
   - 受影响的调用链：`is_under` 被 `platform._scan_with_lsof`、`_proc_usage`、`migrate`、`routes`（改写后）调用；`entry_kind` 被 shared、accounts、doctor、bundle、migrate 调用（只是新增 junction 分支，POSIX 上 `is_junction` 恒为 False）。
2. **文件锁实现挪到 platform**：
   - `lock.WriteLock` 被所有写命令使用，`artifacts` 的追加写被 handoff / artifacts 使用。
   - POSIX 上仍是 `fcntl.flock` 同一调用，持锁进程号的位置不变。
   - 新旧版本同时运行时（旧版进程持锁），锁的语义仍兼容。
3. **`routes.resolve`**：POSIX 分支原样保留，只新增 Windows 分支；`claude-auto`、`run` 不带名称、`route` 的列表与测试都经过这里，由 §8 第 3 条兜底。
3a. **隐藏子命令**：只在 Windows 上注册，POSIX 上 argparse 的子命令集合不变，补全脚本与拼错提示逐字节不变（§8 第 2 条）。
3b. **遍历剪枝**：`fsutil.py:137/164/207` 新增的剪枝在 POSIX 上过滤条件恒为假，`copy_tree`、清单、补权限的结果不变；这三个函数被 migrate 复制模式（`migrate.py:531/552/577`）、`sessions.py:150/179`、`accounts` 删除流程调用。
3c. **`O_BINARY`、`read_at`**：POSIX 上 `O_BINARY` 取 0、`read_at` 即 `os.pread`，不变。
4. **`env` 拒绝新的身份键**：配置文件里已经设了 `USERPROFILE` 等键的用户（macOS / Linux 上几乎不会有，但不能排除），升级后 `apply` 会报错退出 2。这是收紧，在 CHANGELOG 中写明；报错信息给出要删除的键名。
5. **测试 helpers 改写**：
   - 假 `claude` 从 sh 改为 Python，所有调用假 `claude` 的用例都受影响：它记录参数与环境的输出格式必须与原 sh 版完全一致，由全量测试兜底。
   - POSIX 上测试耗时增加：每次调用假 `claude` 多一次 Python 启动，估计全量从约 8 分钟增加到 9–10 分钟。
6. **Windows 运行时特性**：
   - 每次启动账号多一次 Python 进程，约 0.1–0.3 秒。
   - 父进程常驻，等待 Claude 退出；内存约 15 MB。
   - Ctrl+C 交给 Claude 处理（R1 实机确认）。
7. **凭据管理器后端**：已登记账号若改了目录（工具不支持，但用户可能手动搬），在凭据管理器后端下会丢登录，与 macOS 相同。README 中补充 Windows 适用。
8. **CI 时长**：增加两个 Windows 作业，并行执行，不增加总时长的关键路径之外的等待。
9. **PyPI 元数据**：classifiers 增加 Windows，不影响已安装的用户。

## 8. 回归测试

**POSIX 回归（不影响现有功能）**

1. macOS 本机：`python3 -m unittest discover -s tests` 全部通过，跳过数与基线（540 个，跳过 4 个）相比只增加 Windows 专属用例的跳过；pyflakes 无告警。
2. 用基线与新代码分别对同一份示例配置运行 `apply`，生成的全部启动命令与 `claude-auto` 逐字节相同（`cmp`）；`completion bash/zsh/fish` 的输出、`multi-claude lauch` 的拼错提示也逐字节相同。
3. `routes.resolve` 等价性：POSIX 分支的判断式与改前逐字相同（只是先存进变量 `matched`，`git diff` 可核），由现有路由用例兜底；Windows 分支由 `test_windows` 的“不分大小写、按路径段匹配”用例覆盖。
4. 编译机 Linux 跑同一份源码快照的全量测试，全部通过。
5. 锁：两个进程同时运行写命令，后到的一个按现有提示退出（已有用例），POSIX 与 Windows 都跑。

**Windows（CI，`windows-latest` × 3.8 / 3.12）**

6. `test_windows` 全部通过，其中只在 Windows 上运行的用例全部执行、没有被跳过（Windows 行只跑这一个套件，见文首注记）。
7. `python -m multi_claude --help` 退出 0（包能加载）。
7a. 各平台（含 macOS / Linux）：在含 `json.py`、`platform.py`、`runpy.py`（内容均为抛 `SystemExit`）的临时目录下，并把该目录设为 `PYTHONPATH`，运行 `sys.executable -I src/multi_claude/_entry.py --version`，退出 0、输出版本号（`test_windows.EntryIsolationTest`）。

**Windows 实机**（由用户安排，逐条记录通过、未通过或不可达）

8. 全新机器（没有 `%USERPROFILE%\.claude`）：`pipx install` → `multi-claude init` → `add work` → `apply` → `claude-work` 登录 → `%USERPROFILE%\.cc\work\.credentials.json` 存在，或凭据管理器里有带后缀的条目；`%USERPROFILE%\.claude` 不受影响。
9. 代理：`proxy work 7890` 后 `multi-claude run work -- cmd /c set HTTPS_PROXY` 显示该代理；`off` 时没有代理变量。
10. `share`：共享目录生成 junction，Claude 能读到共享的 skills；共享文件在开发者模式关闭时报错、返回 1，开启后成功。
11. `route add C:\proj work` → 在 `C:\proj\sub` 中运行 `claude-auto`，进入 work 账号。
12. `migrate-default main`：有 Claude 会话打开时被拒绝，返回非 0 并列出进程；全部关闭后迁移成功，`%USERPROFILE%\.claude` 变成 junction，直接运行 `claude` 不需要重新登录，`claude-main` 同样不需要。
13. `restore`：恢复原目录，登录仍然有效。
14. 迁移过程中故意用资源管理器打开 `%USERPROFILE%\.claude\projects`，rename 失败返回 4，原目录完好。
15. `list`、`doctor`、`sessions`、`handoff`（续聊命令能在 PowerShell 中直接粘贴运行）、`statusline install`（状态栏正常显示并采集用量）、`completion powershell`。
16. R1：在 `claude-work` 中按 Ctrl+C、`/exit` 退出，不出现“终止批处理操作吗”；不满足时把现象记入 README 限制。
17. R2：在凭据管理器后端开启的机器上重复第 12 条；并看 `list` / `doctor` 的 LOGIN 列为 `unknown`，`add` 不提示 login。
18. 状态栏原命令写成 `/c/Users/<用户>/.claude/statusline.sh` 这种 `/` 开头的形式，`statusline install` 后状态栏照常显示（验证 MSYS 改写已关闭）。
19. 在含 `json.py`（内容 `raise SystemExit("hijacked")`）和 `claude.cmd` 的目录里运行 `claude-work`：正常进入 Claude，不出现 hijacked，不运行当前目录的 `claude.cmd`。
20. 共享目录内含 junction 时 `export`、`migrate-default` 复制模式、`handoff`：不深入 junction，目标处建为 junction；`handoff` 连续两次，交接日志两行完整（`os.pread` 替换）。
21. 持锁写交接日志期间，另一个进程 `artifacts` 能读日志（D24）；`test_refresh` 的锁用例在 Windows 上全部通过（R4）；`statusline install` 后 hook 在 2 秒内返回（后台采集不继承 stdout）；`launch` 的提示文字与 POSIX 启动命令一致。
22. 打开 Claude 桌面版（不开 Claude Code）时运行 `migrate-default --dry-run`：记录是否被判为占用；被判占用时在 README 写明“迁移前先退出桌面版”。

## 9. 日志 / 观测点

- `[multi-claude]` 前缀的现有日志不变。
- 新增：
  - `create_link` 失败：`cannot create link <链接> -> <目标>: <原因>`。
  - 占用检查：Windows 上列出的每个 Claude 进程打印 `pid`、可执行文件路径、`usage=claude-process`。
  - rename 占用：`... is in use by another program`，同时打印原始 `winerror`。
  - 重试：`retry_on_sharing_violation` 每次重试在设置了 `MULTI_CLAUDE_DEBUG=1` 时打印一行；不设时不打印（避免刷屏）。编码时如果仓库里没有该变量的约定，就只在最终失败时打印。
  - 隐藏子命令 `launch` 找不到 `claude` 时打印 `multi-claude: claude not found in PATH`、退出 127，与 POSIX 版 `claude-auto`（`routes.py:114-117`）相同。
- CI 的 Windows 作业日志：`unittest` 汇总行中的 `Ran N tests`、`skipped=M`。

## 10. 开放问题

- Q1 `cli.py:1193` `code` 子命令在 darwin 上的 socket 路径长度检查：Windows 上 VS Code 用命名管道，不受该限制。默认在 Windows 上跳过这项检查，不再另行处理。
- Q2 `sessions.encode` 在 Windows 上，`os.getcwd()` 返回的盘符大小写与 Claude 记录的是否一致：默认按原样比较；实机第 15 条若发现不一致，再追加不区分大小写的匹配。
