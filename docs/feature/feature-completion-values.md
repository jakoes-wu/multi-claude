# 补全参数取值（选项值、固定取值自动读取、共享项名）

> 2026-11-05 注记：已落地（分支 `feature/completion-values`），本机全量测试通过；入口 `src/multi_claude/completion.py` `value_spec`、`cli.py` `cmd_list(shared_items_only)`。复刻 multi-codex 0.15.0（其方案 `docs/feature/feature-completion-values.md`，PR #43）。以下 `file:line` 指基线 origin/main `3096b4f`（v0.17.0）。

## 1. 背景

`multi-claude completion bash|zsh|fish|powershell` 输出补全脚本（`src/multi_claude/completion.py`）。现状：

- 子命令名、各子命令的选项名、需要跟值的选项，生成时从 argparse 读出（`completion.py:49-66` `collect` / `_options` / `_value_options`）。
- 位置参数按手写表 `POSITIONAL_KINDS`（`completion.py:17-26`）补：账号名、目录、文件，以及 `FIXED_WORDS`（`completion.py:30-31`）里的固定取值 `shell`、`proxy`、`statusline`、`netlevel`、`clashapi`。
- 选项的值：`PATH_OPTIONS`（`completion.py:27`）补文件名，`NAME_OPTIONS`（`completion.py:29`，含 `--config-from`）补账号名；其余带值选项什么都不补（bash `completion.py:123`、zsh `:196`、PowerShell `:362` 的 `return`；fish `:273` 只加 `-r`）。

与 multi-codex 0.15.0 相比还补不到的位置：

| 位置 | 应补出 | 取值来源 |
| ---- | ---- | ---- |
| `netcheck` 的 `--clash` / `--clash-tun` / `--clash-global` 之后 | `on off` | argparse `choices`（`cli.py:379-385`） |
| `usage --by` 之后 | `day model` | argparse `choices`（`cli.py:431`） |
| `add` / `set` 的 `--shared-exclude` / `--shared-include` 之后 | 已配置的共享项名 | `config.shared_items`（`config.py:137`，默认 `config.py:18`） |

另外，`completion` 的 shell 名、`statusline` 的动作、`netcheck` 的档位在 argparse 里已有 `choices`（`cli.py:514`、`:519`、`:373`），却在 `FIXED_WORDS` 里又手写了一遍；新增带 `choices` 的参数时容易漏改补全。

multi-codex 0.15.0 的其余几处取值（`netcheck` 档位、`clash-api auto|off`、`--config-from` 账号名）multi-claude 在 0.17.0 及以前已经补了。

## 2. 目标 / 非目标

目标：

1. 上表 3 处在 bash / zsh / fish / PowerShell 四种补全脚本里都能补出候选。
2. argparse 里写了 `choices` 的位置参数与选项，取值在生成脚本时自动读出；以后新增带 `choices` 的参数不用再改 `completion.py`。
3. 已有补全行为不变：子命令、选项名、账号名、目录与文件位置、`proxy` 的 `off inherit`、`clash-api` 的 `auto off`、`--` 之后不补、`PATH_OPTIONS` 补文件名。

非目标：

- `--opt=value` 写法与选项缩写（如 `--clash-t`）的值补全：现有扫描只认完整写法，不改。
- `--country`、`--proxy`、`--days` 等自由取值；`init --shared-items` 的逗号列表。
- `claude-<名>` 启动命令的补全。
- 改 README 里四种 shell 的安装写法：四种写法都是把生成的脚本写成文件（快照），只提示升级后重新执行一次安装命令。

## 3. 假设与约束

- 只用 Python 3.8 标准库（argparse 在 3.8 上的 `_actions` / `choices` / `SUPPRESS` 行为以 CI 的 3.8 任务通过为判据）；bash 脚本要能在 macOS 自带 bash 3.2 上运行（不用关联数组）。
- PowerShell 脚本只含 ASCII（Windows PowerShell 5.1 按系统代码页读无 BOM 的脚本）；已有 `tests/test_windows.py` `test_script_is_ascii` 检查。
- `collect` 的签名与返回值不变：唯一调用点是 `completion.py:70`（`grep -rn "collect(" src tests` 无截断枚举，`cli.py:2643` 是 `artifacts.collect`，无关）。
- 共享项名的来源新增一个隐藏选项 `list --shared-items`。不放在 `completion` 下：`completion` 的 `shell` 位置参数是必填的（`cli.py:514`），而补全脚本取账号名已经用 `list --names`（`completion.py:131`），同一命令下加一个兄弟选项最一致。

## 4. 涉及模块

| 区域 | 行号锚点（基线 `3096b4f`） | 改动类型 | 改动点 |
| ---- | ---- | ---- | ---- |
| 补全表 | `src/multi_claude/completion.py:17-31` | 修改 | `POSITIONAL_KINDS` 里 `shell`、`statusline`、`netlevel`、`proxy`、`clashapi` 改为 `fixed`；删 `FIXED_WORDS`，改为手写表 `EXTRA_POSITIONAL = {("proxy", 1): off inherit, ("clash-api", 0): auto off}`；新增 `SHARED_OPTIONS = ("--shared-exclude", "--shared-include")` |
| 读取规格 | `completion.py:49-66` 之后 | 新增 | `value_spec(parser)`，返回 `(kinds, positional, option_values)`，见 §5.1 |
| 读取规格 | `completion.py:59-66` `_options` / `_value_options` | 修改 | 跳过 `help=argparse.SUPPRESS` 的选项，隐藏选项不进补全 |
| 入口 | `completion.py:69-77` `render` | 修改 | 另调用 `value_spec`，把三张表传给四个生成函数 |
| 生成 | `completion.py:34-40` `_positional_cases` | 修改 | 加参数 `shell`：`fixed` 位置在 bash 生成 `cmd:i) candidates="词…" ;;`，在 zsh 生成 `cmd:i) candidates=(词…) ;;`；其余照旧生成 `kind=…`；类型改从 `value_spec` 合并后的表取 |
| bash | `completion.py:84-159` `_bash` | 修改 | `skip` 分支（`:120-124`）改为先查 `case "$cmd:$prev"` 取值表与共享项选项，再走原 `PATH_OPTIONS` / `NAME_OPTIONS`；删 `kind` 分支里 `shell`、`proxy`、`statusline`、`netlevel`、`clashapi` 五行（`:133-138` 一带） |
| zsh | `completion.py:162-224` `_zsh` | 修改 | 结构同 bash（`:193-197`、`:206-211` 一带），但所有候选赋值用数组写法：取值 `candidates=(on off)`，共享项 `candidates=(${(f)"$(multi-claude list --shared-items 2>/dev/null)"})`，见 §5.1 |
| fish | `completion.py:227-291` `_fish` | 修改 | 选项行（`:270-277`）：有取值表的选项生成 `-x -a '词…'`，共享项选项生成 `-x -a '(multi-claude list --shared-items 2>/dev/null)'`；位置行（`:279-290`）的类型改从合并后的表取，`fixed` 从取值表取词；生成文本不得含 `$(`（ubuntu-22.04 的旧 fish 不支持）；`__multi_claude_positionals` 在补选项值时输出 -2，免得位置候选混进选项取值 |
| PowerShell | `completion.py:308-382` `_powershell` | 修改 | 新增 `$optionValues`（键 `cmd:opt`）、`$sharedOptions`；`$fixedWords` 改为键 `cmd:i`；`skip` 分支（`:359-362`）与位置分支（`:366-371`、`$positionals` `:379`）按合并后的表取 |
| 命令行 | `src/multi_claude/cli.py:417` `--names` 之后 | 新增 | `list_mode` 互斥组里加隐藏选项 `--shared-items`（`help=argparse.SUPPRESS`） |
| 命令行 | `cli.py:728` `cmd_list` 调用、`cli.py:1971-1980` `cmd_list` | 修改 | 新参数 `shared_items_only`：输出共享项名，规则见 §5.1 |
| 测试 | `tests/test_completion.py` | 新增 | bash 行为、zsh 查表、fish（装了才跑）、`value_spec` 自动读取、`list --shared-items`，见 §8 |
| 测试 | `tests/test_windows.py:491` `test_powershell_completion_parses` 之后 | 新增 | PowerShell `TabExpansion2` 行为用例 |
| 测试 | `tests/test_help_examples.py:122-131` | 修改 | 跳过 `help=argparse.SUPPRESS` 的选项（隐藏选项不需要帮助示例） |
| 测试 | `tests/test_renewal.py:213-226` `test_t8_examples_parse_and_cover_options` | 修改 | 同样跳过 `help=argparse.SUPPRESS` 的选项（`:222-226` 要求每个选项都出现在示例里） |
| CI | `.github/workflows/ci.yml:50-52` | 修改 | Linux 的 apt 安装改为 `shellcheck lsof zsh fish`，zsh / fish 行为用例在 Linux CI 上必跑 |
| 文档 | `README.md:517-522`、`README.zh-CN.md:509-514` 补全代码块之后 | 修改 | 加一句：补全覆盖子命令、选项、账号名以及选项与参数的取值（如 `netcheck` 档位、`on`/`off`、共享项名）；四种写法都是生成的快照，升级后重新执行一次上面的安装命令（PowerShell 先从 `$PROFILE` 删掉之前追加的那段，`Add-Content` 再执行会重复追加） |
| 文档 | `CHANGELOG.md:7` Unreleased | 修改 | 一条 Added |

## 5. 方案

### 5.1 实现要点

**`value_spec(parser)`**：遍历每个子命令的 `_actions`，返回 `(kinds, positional, option_values)`：

- `positional = {(子命令, 序号): [词]}`：位置参数按出现顺序编号 0、1、…；`choices` 非空时记入。再合入 `EXTRA_POSITIONAL`（argparse 里没有 `choices` 的关键字，如 `proxy` 的 `off inherit`、`clash-api` 的 `auto off`），两边同一位置都有时以 argparse 为准。
- `option_values = {(子命令, 选项写法): [词]}`：带值选项 `choices` 非空时，每个写法都记入。
- 生成时报错（`ValueError`，不用 `assert`）的情况：取值不符合白名单 `SAFE_WORD`（只含 `[A-Za-z0-9._+-]`、不以 `-` 开头，与 `list --shared-items` 同一份）；带 `choices` 的位置参数 `nargs` 是 `*` / `+`；带取值的位置在 `POSITIONAL_KINDS` 里被标成 `name` / `dir` / `file`；`POSITIONAL_KINDS` 标了 `fixed` 却没有取值。这些都是写代码时的错误，测试会在生成脚本时暴露。
- `POSITIONAL_KINDS` 里没列、但 argparse 有 `choices` 的位置，也按 `fixed` 生成（目标 2：新命令不用改补全表）。
- 另返回合并后的类型表 `kinds = {子命令: [类型…]}`：以 `POSITIONAL_KINDS` 为底，有取值的位置记为 `fixed`，中间缺的位置补 `none`。四个生成函数（bash / zsh 的 `_positional_cases`、fish 位置行、PowerShell 的 `$positionals` 与位置分支）一律用这张表，不再直接读 `POSITIONAL_KINDS`。`none` 位置不补：bash / zsh 不生成分支，fish 不出行，PowerShell 在位置分支直接 return。

**候选决定顺序**（四种 shell 一致，扫描规则不变）：

1. 还没有子命令 → 子命令名（不变）；
2. `--` 之后 → 不补（不变）；
3. 光标前一个词是带值选项（`skip`，`prev` 为该选项）→ 依次查：`option_values[(cmd, prev)]` 有则用它；`prev` 在 `SHARED_OPTIONS` → 调 `multi-claude list --shared-items 2>/dev/null`；`PATH_OPTIONS` → 文件名；`NAME_OPTIONS` → 账号名；都不是 → 不补（不变）；
4. 当前词以 `-` 开头 → 选项名（不变）；
5. 否则按 `$cmd:$npos` 查位置表：`fixed` 用取值表的词，`name` / `dir` / `file` 照旧。

bash 生成结果示例（`skip` 分支，替换 `completion.py:120-124`）：

```bash
  if ((skip)); then
    case "$cmd:$prev" in
      netcheck:--clash|netcheck:--clash-tun|netcheck:--clash-global) candidates="on off" ;;
      usage:--by) candidates="day model" ;;
      *)
        case "$prev" in
          --shared-exclude|--shared-include) candidates="$(multi-claude list --shared-items 2>/dev/null)" ;;
          -f|--file|--root|--bin-dir|--shared-dir) COMPREPLY=($(compgen -f -- "$cur")); return 0 ;;
          --default|--from|--config-from) kind=name ;;
          *) COMPREPLY=(); return 0 ;;
        esac ;;
    esac
```

位置表生成结果示例（`_positional_cases`）：

```bash
      clash-api:0) candidates="auto off" ;;
      completion:0) candidates="bash zsh fish powershell" ;;
      netcheck:1) candidates="off basic strict" ;;
      proxy:1) candidates="off inherit" ;;
      statusline:0) candidates="install uninstall" ;;
      add:0|args:0|...) kind=name ;;
```

之后的 `compgen -W "$candidates"` 与 `@` / `:` 前缀处理（`completion.py:141-150`）不变，账号名、取值、共享项名都经过它。

zsh：`candidates` 是数组（`local -a candidates`，`completion.py:165`），赋一个字符串会成为单个候选，所以 zsh 版本由同一函数按 `shell="zsh"` 生成数组写法：

```zsh
    case "$cmd:$prev" in
      netcheck:--clash|netcheck:--clash-tun|netcheck:--clash-global) candidates=(on off) ;;
      usage:--by) candidates=(day model) ;;
      *)
        case "$prev" in
          --shared-exclude|--shared-include) candidates=(${(f)"$(multi-claude list --shared-items 2>/dev/null)"}) ;;
          ...
        esac ;;
    esac
```

位置表同理：`completion:0) candidates=(bash zsh fish powershell) ;;`。

fish：有取值的选项**只生成一行**，例如 `complete -c multi-claude -n '__fish_seen_subcommand_from netcheck' -l clash -x -a 'on off'`；共享项选项 `-x -a '(multi-claude list --shared-items 2>/dev/null)'`；其它带值选项仍为 `-r`。位置行的 `fixed` 用 `shlex.quote` 后的取值。

PowerShell：拼键用 `($command + ':' + $prev)`、`($command + ':' + $count)`，不用 `"$command:$prev"`（`$command:` 会被当成带作用域的变量名）。

**`list --shared-items`**（隐藏，给补全脚本用）：

- 每行输出一个 `config.shared_items` 条目；没有配置文件时输出默认共享项（`load_config` 在文件不存在时返回 `default_config()`，`config.py:226-227`、`:204-206`）。
- 白名单：只输出完全由 `[A-Za-z0-9._+-]` 组成、且不以 `-` 开头的名字；其余（含空白、引号、`$`、反引号、`:`、`@` 等）不输出。原因：bash 的 `compgen -W` 会对候选做参数展开与命令替换，`$` 与反引号会被执行；`:`、`@` 会与前缀处理冲突。
- 配置读不了（`ConfigError`、`OSError`）时静默输出空、退出 0：在 `cmd_list` 里用 `try` 只包住 `load_config`（`cli.py:1975`）与取 `shared_items`，与补全脚本 `2>/dev/null` 的用法配合。迁移记录损坏的提示（`cli.py:723-725` `pending_journal_notice`，在分派前执行）不在“退出 0”的保证范围内，与 `--names` 现状一致。
- 与 `--names` 等在同一互斥组，不能同时给。

### 5.2 接口变更

- 新增隐藏选项 `multi-claude list --shared-items`：不出现在帮助与补全里，供补全脚本调用。
- 补全脚本内容变化：README 里四种安装写法都把生成的脚本写成文件（快照），升级后要重新执行一次安装命令才能用到新补全；旧快照照常可用，只是没有新取值。
- 不涉及配置文件、退出码、其它命令输出。

**reference sibling 回补检查**：N/A——本方案不涉及 `docs/reference/*`。

## 6. 备选方案与决策

- 继续手写 `FIXED_WORDS`，只补上 `--clash` 等三处：新增带 `choices` 的参数还要记得同步，`--clash` 系列就是这样漏掉的。否决。
- 按 Tab 时调用 `multi-claude` 现算候选：每次 Tab 都要起一次 Python（现在只有账号名、共享项名位置才起），Windows 上明显变慢。否决。
- 共享项名在生成脚本时写死：用户用 `init --shared-items` 改过共享项后，补全不跟着变。否决。

## 7. 影响分析

1. **补全脚本整体**（正向）：四个生成函数都改，补全脚本是唯一消费方。现有 `tests/test_completion.py:62-98` 的行为用例必须照旧通过；`shell`、`statusline`、`netlevel` 的取值来源改为 argparse，输出词与顺序必须不变（`choices` 声明顺序与原 `FIXED_WORDS` 一致：`completion.SHELLS`、`("install", "uninstall")`、`NET_CHECK_LEVELS`）。
2. **`collect` 跳过隐藏选项**（反向）：基线没有任何 `help=argparse.SUPPRESS` 的选项（`grep -n SUPPRESS src/multi_claude/cli.py` 为 0），这一改动只影响新增的 `--shared-items`。
3. **帮助示例测试**：`test_help_examples.py:122-131` 与 `test_renewal.py:222-226` 都要求每个选项出现在示例里；两处都跳过隐藏选项后，其它命令的要求不变。
4. **按 Tab 的耗时**：新增起 Python 的位置只有 `--shared-exclude` / `--shared-include` 之后（读一次配置），与账号名位置同量级；`on off`、`day model` 是脚本内常量。
5. **bash 3.2 兼容**：只用 `case` 与普通变量。zsh 用数组写法，fish 不用 `$(...)`。
6. **PowerShell 只含 ASCII**：新增内容只含 ASCII。
7. **`list` 的其它模式**：`--shared-items` 走在 `migrate.warn_isolation_env()` 之前单独返回（与 `--names` 一样在 `cli.py:1976` 一带），不读令牌、不联网、本分支不打告警（分派前 `cli.py:723-725` 的迁移记录提示除外）。
8. **CI 安装 zsh / fish**：Linux 任务多装两个包，只影响 CI 时长（几秒），不影响发布物。

## 8. 回归测试

bash（`tests/test_completion.py`，用现有 `bash_complete`）：

| 编号 | 输入 | 期望 |
| ---- | ---- | ---- |
| C1 | `multi-claude netcheck work strict --clash ` | `on off`；`--clash-tun `、`--clash-global ` 同 |
| C2 | `multi-claude usage --history --by ` | `day model` |
| C3 | `multi-claude set work --shared-exclude ` | 默认共享项 `agents commands skills CLAUDE.md`；`--shared-include sk` → `skills` |
| C4 | 配置里 `shared.items` 含 `a b`、`x:y`、`$HOME`、`-x` | 这几项不出现，其余照常 |
| C5 | `multi-claude netcheck work --country ` | 空 |
| C6 | 现有 `test_completion.py` 行为用例 | 全部照旧，含 `netcheck work ` → `off basic strict`、`clash-api ` → `auto off`、`proxy work ` → `off inherit`、`completion ` → 四个 shell、`statusline ` → `install uninstall` |
| C7 | `multi-claude list --shared-items` | 每行一项；配置损坏时输出空、退出 0；`list --shared-items --names` 退出 2 |
| C8 | `value_spec` 自动读取 | 给一个假解析器加带 `choices` 的位置参数（不在 `POSITIONAL_KINDS` 里）与选项，生成的四种脚本都含其取值 |
| C9 | 生成时报错 | 取值以 `-` 开头、`nargs="*"` 带 `choices` → `ValueError` |
| C10 | 隐藏选项 | 四种脚本都不含 `--shared-items` 作为可补的选项名（只出现在 `list --shared-items` 调用里） |

zsh（`tests/test_completion.py`，装了 zsh 才跑；Linux CI 必装，所以 CI 上必跑）：`zsh -n` 语法检查；行为用例用 `zsh -f -c` 加载生成脚本，把 `compdef` 换成空函数、`compadd` 换成逐个打印参数的函数，设好 `words` / `CURRENT` 后调用补全函数（与 fish 用例一样复用 `bash_complete` 的 fakebin 转调脚本与 PATH 设置，`tests/test_completion.py:52-56`），核 C1（候选是 `on`、`off` 两个，不是一个 `on off`）、C3 与 `completion ` → 四个 shell。

fish（装了 fish 才跑；Linux CI 必装）：`fish --no-config -c 'source <脚本>; complete -C "<行>"'` 核 C1、C3、`netcheck work ` 的档位，以及 `usage --history --by ` 只补出 `day`、`model`，不混进账号名。另核生成的 fish 脚本不含 `$(`。

PowerShell（`tests/test_windows.py`，Windows CI）：在 fakebin 写 `multi-claude.cmd` 转调当前源码；先核 `proxy work ` 能出候选（证明补全器被调到），再用 `TabExpansion2` 核 C1、C2、C3。另加一条：生成的 PowerShell 脚本只含 ASCII（所有平台都跑）。

另需：本机全量测试、pyflakes、README 链接检查、CI 三平台全过。

## 9. 日志 / 观测点

补全脚本不打日志（按 Tab 时的任何输出都会弄乱命令行）。观测方式：

- `multi-claude completion bash | grep -n 'cmd:\$prev' -A6` 查看选项取值表；
- `multi-claude list --shared-items` 直接看共享项来源；
- §8 的测试。
