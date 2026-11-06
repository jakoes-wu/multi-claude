"""`multi-claude completion SHELL`：生成 bash / zsh / fish / PowerShell 补全脚本（方案 v0.2 §5.1.5）。

子命令与选项从 argparse 解析器遍历得到，避免手写清单与实际参数漂移。遍历用到
`parser._actions` 与 `argparse._SubParsersAction.choices` 两个未写入文档的属性，
兼容性由测试在 Python 3.8 与 3.12 上兜底。账号名在补全时调用 `multi-claude list --names` 获取，
共享项名调用隐藏的 `multi-claude list --shared-items` 获取（方案 feature-completion-values）。
位置参数与选项的固定取值从 argparse 的 `choices` 读出，新增带 `choices` 的参数不用改本模块。
本模块只输出脚本文本，不修改任何 shell 配置文件。
"""

import argparse
import re
import shlex
from typing import Dict, List, NamedTuple, Tuple

SHELLS = ("bash", "zsh", "fish", "powershell")

# 各子命令第 N 个位置参数的候选类型：name 账号名、dir 目录、file 文件、fixed 固定取值
# （取自 argparse 的 choices 或 EXTRA_POSITIONAL）、none 不补（value_spec 合并时补齐空位用）。
# argparse 里带 choices、但这里没列的位置，value_spec 会自动当作 fixed。
POSITIONAL_KINDS = {
    "migrate-default": ["name"], "add": ["name"], "proxy": ["name", "fixed"], "env": ["name"],
    "args": ["name"], "remove": ["name"], "usage": ["name"], "completion": ["fixed"],
    "route": ["dir", "name"], "which": ["dir"], "rename": ["name"], "mcp": ["name"],
    "statusline": ["fixed", "file"], "handoff": ["name"], "login": ["name"], "set": ["name"],
    "run": ["name"], "path": ["name"], "restore": ["name"], "code": ["name", "file"],
    "export": ["name", "file"], "import": ["file", "name"], "sessions": ["name"], "artifacts": ["name"],
    "netcheck": ["name", "fixed"], "clash-api": ["fixed"],
    "agent-lock": ["lock"],
}
# fish 按 kind 读这张表。bash 与 zsh 的 kind 分支是手写的，不读它。
FIXED_WORDS = {"lock": ("on", "off")}
# argparse 里没有 choices 的固定取值：proxy 的值还可以是端口或 URL，clash-api 的地址还可以是任意地址，
# 所以不能写成 choices，只在补全时给出这几个关键字。同一位置 argparse 有 choices 时以 argparse 为准。
EXTRA_POSITIONAL = {("proxy", 1): ("off", "inherit"), ("clash-api", 0): ("auto", "off")}
# 选项值是路径、应按文件补全的选项。
PATH_OPTIONS = ("-f", "--file", "--root", "--bin-dir", "--shared-dir", "--wrapper")
# 选项值是账号名的选项。
NAME_OPTIONS = ("--default", "--from", "--config-from")
# 选项值是共享项名的选项；候选在按 Tab 时调用 `multi-claude list --shared-items` 现查，
# 这样用户用 init --shared-items 改过共享项后，补全不用重新生成。
SHARED_OPTIONS = ("--shared-exclude", "--shared-include")
# 能原样写进补全脚本的取值：bash 的 compgen -W 会对候选做参数展开与命令替换，zsh 数组字面量里
# 括号、分号、通配符都有特殊含义，所以只放行这些字符；以 - 开头的会被当成选项。
# cli 的 `list --shared-items` 也按它过滤共享项名。
SAFE_WORD = re.compile(r"[A-Za-z0-9._+][A-Za-z0-9._+-]*\Z")


class ValueSpec(NamedTuple):
    # {子命令: [各位置的候选类型]}：POSITIONAL_KINDS 合并 choices 之后的结果，四种脚本都只读这张表
    kinds: Dict[str, List[str]]
    # {(子命令, 位置序号): [取值]}：kinds 里标 fixed 的位置都在这里
    positional: Dict[Tuple[str, int], List[str]]
    # {(子命令, 选项写法): [取值]}：带 choices 的带值选项，每个写法（如 -x 与 --xx）各一条
    option_values: Dict[Tuple[str, str], List[str]]


def _checked_words(where: str, values) -> List[str]:
    words = [str(value) for value in values]
    for word in words:
        if not SAFE_WORD.match(word):
            raise ValueError("completion: {} value {!r} cannot be written into a completion script".format(
                where, word))
    return words


def value_spec(parser: argparse.ArgumentParser) -> ValueSpec:
    """从 argparse 读出位置参数与选项的固定取值，并与 POSITIONAL_KINDS 合并成各位置的候选类型。

    这里抛出的 ValueError 都是写代码时的错误（取值写不进脚本、类型表与 choices 冲突），
    生成任何一种补全脚本都会触发，测试能直接暴露；不用 assert，避免 python -O 下被跳过。
    """
    positional: Dict[Tuple[str, int], List[str]] = {}
    option_values: Dict[Tuple[str, str], List[str]] = {}
    commands = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            commands.add(name)
            index = 0
            for item in sub._actions:
                if item.option_strings:
                    # 隐藏选项不进补全；隐藏的位置参数仍占一个词，所以只在选项这里跳过，位置照常计数。
                    if item.choices and item.nargs != 0 and item.help != argparse.SUPPRESS:
                        for option in item.option_strings:
                            option_values[(name, option)] = _checked_words(
                                "{} {}".format(name, option), item.choices)
                    continue
                if item.choices:
                    if item.nargs in ("*", "+"):
                        # 位置计数假定每个位置参数只占一个词；可重复的位置后面的序号就对不上了。
                        raise ValueError("completion: {} positional {} takes several words".format(name, index))
                    # FIXED_WORDS 的 kind 不从 argparse choices 生成：fish 读表，bash / zsh 用手写分支。
                    marked = POSITIONAL_KINDS.get(name, ())
                    if not (index < len(marked) and marked[index] in FIXED_WORDS):
                        positional[(name, index)] = _checked_words(
                            "{} positional {}".format(name, index), item.choices)
                index += 1
    # 只合并解析器里真有的子命令：测试会用只含几个子命令的假解析器调用本函数。
    for key, words in EXTRA_POSITIONAL.items():
        if key[0] in commands:
            positional.setdefault(key, _checked_words("{} positional {}".format(*key), words))
    kinds = {name: list(kind_list) for name, kind_list in POSITIONAL_KINDS.items() if name in commands}
    for (name, index) in positional:
        kind_list = kinds.setdefault(name, [])
        kind_list.extend(["none"] * (index + 1 - len(kind_list)))
        if kind_list[index] not in ("fixed", "none"):
            raise ValueError("completion: {} positional {} has choices but is marked {}".format(
                name, index, kind_list[index]))
        kind_list[index] = "fixed"
    for name, kind_list in kinds.items():
        for index, kind in enumerate(kind_list):
            if kind == "fixed" and (name, index) not in positional:
                raise ValueError("completion: {} positional {} is marked fixed but has no values".format(name, index))
    return ValueSpec(kinds, positional, option_values)


def _shell_words(shell: str, words: List[str]) -> str:
    """给 candidates 赋值：bash 的 candidates 是字符串，zsh 的是数组（赋字符串会变成单个候选）。"""
    if shell == "zsh":
        return "candidates=({})".format(_words(words))
    return 'candidates="{}"'.format(_words(words))


def _positional_cases(spec: ValueSpec, shell: str, indent: str) -> str:
    """生成 `case "$cmd:$npos" in` 的分支：fixed 位置直接给出候选，其余按候选类型合并。"""
    lines = ["{}{}:{}) {} ;;".format(indent, name, index, _shell_words(shell, words))
             for (name, index), words in sorted(spec.positional.items())]
    by_kind: Dict[str, List[str]] = {}
    for command, kinds in sorted(spec.kinds.items()):
        for index, kind in enumerate(kinds):
            if kind not in ("fixed", "none"):
                by_kind.setdefault(kind, []).append("{}:{}".format(command, index))
    lines.extend("{}{}) kind={} ;;".format(indent, "|".join(keys), kind) for kind, keys in sorted(by_kind.items()))
    return "\n".join(lines)


def _option_value_cases(spec: ValueSpec, shell: str, indent: str) -> str:
    """生成 `case "$cmd:$prev" in` 的分支：带 choices 的选项之后补它的取值；取值相同的合并成一条。"""
    by_words: Dict[Tuple[str, ...], List[str]] = {}
    for (name, option), words in sorted(spec.option_values.items()):
        by_words.setdefault(tuple(words), []).append("{}:{}".format(name, option))
    return "\n".join("{}{}) {} ;;".format(indent, "|".join(keys), _shell_words(shell, list(words)))
                     for words, keys in sorted(by_words.items(), key=lambda item: item[1]))


class CommandSpec(NamedTuple):
    options: List[str]
    # 需要跟一个值的选项（补全时要跳过它的值，才能数对位置参数）
    value_options: List[str]


def collect(parser: argparse.ArgumentParser) -> Dict[str, CommandSpec]:
    """返回 {子命令: CommandSpec}；键 "" 表示顶层（尚未输入子命令时）。"""
    specs = {"": CommandSpec(_options(parser), [])}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                specs[name] = CommandSpec(_options(sub), _value_options(sub))
    return specs


def _options(parser: argparse.ArgumentParser) -> List[str]:
    # help=argparse.SUPPRESS 的选项是给脚本用的隐藏入口（如 list --shared-items），不进补全。
    return [option for action in parser._actions if action.help != argparse.SUPPRESS
            for option in action.option_strings]


def _value_options(parser: argparse.ArgumentParser) -> List[str]:
    # nargs == 0 的是开关（store_true / help / version 等），其余选项都要跟值。
    return [option for action in parser._actions
            if action.option_strings and action.nargs != 0 and action.help != argparse.SUPPRESS
            for option in action.option_strings]


def render(shell: str, parser: argparse.ArgumentParser) -> str:
    specs = collect(parser)
    values = value_spec(parser)
    if shell == "bash":
        return _bash(specs, values)
    if shell == "zsh":
        return _zsh(specs, values)
    if shell == "powershell":
        return _powershell(specs, values)
    return _fish(specs, values)


def _words(items: List[str]) -> str:
    return " ".join(items)


def _bash(specs: Dict[str, CommandSpec], values: ValueSpec) -> str:
    commands = sorted(name for name in specs if name)
    option_cases = "\n".join("    {}) opts={}; vopts={} ;;".format(
        name, shlex.quote(_words(spec.options)), shlex.quote(_words(spec.value_options)))
        for name, spec in sorted(specs.items()) if name)
    return """# bash completion for multi-claude (generated by `multi-claude completion bash`)
_multi_claude() {
  # COMP_WORDBREAKS 默认含 @ 与 :，COMP_WORDS 会把账号名 a@b 拆开，所以自己从 COMP_LINE 取完整的当前词。
  local line="${COMP_LINE:0:COMP_POINT}"
  local cur="${line##* }"
  local before="${line%%"$cur"}"
  local -a words
  read -r -a words <<< "$before"
  # 第一个不以 - 开头的词是子命令。
  local cmd="" i w
  for ((i = 1; i < ${#words[@]}; i++)); do
    if [[ "${words[i]}" != -* ]]; then cmd="${words[i]}"; break; fi
  done
  local opts="" vopts=""
  case "$cmd" in
%(option_cases)s
  esac
  # 数子命令之后已输入的位置参数个数，跳过选项与它的值；遇到 -- 记为 -1（args 的参数列表不补全）。
  local npos=0 skip=0 prev=""
  for ((i = i + 1; i < ${#words[@]}; i++)); do
    w="${words[i]}"
    if ((skip)); then skip=0; prev=""; continue; fi
    if [[ "$w" == "--" ]]; then npos=-1; break; fi
    if [[ "$w" == -* ]]; then
      if [[ " $vopts " == *" $w "* ]]; then skip=1; prev="$w"; fi
      continue
    fi
    ((npos++))
  done
  local candidates="" kind=""
  if ((skip)); then
    # 先按“子命令:选项”查固定取值，再按选项查共享项名、文件名、账号名；都不是的自由取值不补。
    case "$cmd:$prev" in
%(option_value_cases)s
      *)
        case "$prev" in
          %(shared_options)s) candidates="$(multi-claude list --shared-items 2>/dev/null)" ;;
          %(path_options)s) COMPREPLY=($(compgen -f -- "$cur")); return 0 ;;
          %(name_options)s) kind=name ;;
          *) COMPREPLY=(); return 0 ;;
        esac ;;
    esac
  elif [[ -z "$cmd" ]]; then
    candidates="%(commands)s --help --version"
  elif ((npos < 0)); then
    COMPREPLY=(); return 0
  elif [[ "$cur" == -* ]]; then
    candidates="$opts"
  else
    case "$cmd:$npos" in
%(positional_cases)s
    esac
  fi
  case "$kind" in
    name) candidates="$(multi-claude list --names 2>/dev/null)" ;;
    dir) COMPREPLY=($(compgen -d -- "$cur")); return 0 ;;
    file) COMPREPLY=($(compgen -f -- "$cur")); return 0 ;;
    lock) candidates="on off" ;;
  esac
  COMPREPLY=($(compgen -W "$candidates" -- "$cur"))
  # bash 只替换当前词最后一个分词字符之后的片段，候选要去掉同样长的前缀。
  local j prefix=""
  for ((j = ${#cur} - 1; j >= 0; j--)); do
    if [[ "$COMP_WORDBREAKS" == *"${cur:j:1}"* ]]; then prefix="${cur:0:j+1}"; break; fi
  done
  if [[ -n "$prefix" ]]; then
    for i in "${!COMPREPLY[@]}"; do COMPREPLY[i]="${COMPREPLY[i]#"$prefix"}"; done
  fi
}
complete -F _multi_claude multi-claude
""" % {"option_cases": option_cases, "commands": _words(commands),
       "positional_cases": _positional_cases(values, "bash", "      "),
       "option_value_cases": _option_value_cases(values, "bash", "      "),
       "shared_options": "|".join(SHARED_OPTIONS), "path_options": "|".join(PATH_OPTIONS),
       "name_options": "|".join(NAME_OPTIONS)}


def _zsh(specs: Dict[str, CommandSpec], values: ValueSpec) -> str:
    commands = sorted(name for name in specs if name)
    option_cases = "\n".join("    {}) opts=({}); vopts=({}) ;;".format(
        name, _words([shlex.quote(o) for o in spec.options]),
        _words([shlex.quote(o) for o in spec.value_options]))
        for name, spec in sorted(specs.items()) if name)
    return """#compdef multi-claude
# zsh completion for multi-claude (generated by `multi-claude completion zsh`)
_multi_claude() {
  local cmd="" i w
  local -a opts vopts candidates
  # zsh 的 $words 不按 @ 分词，可直接使用；第一个不以 - 开头的词是子命令。
  for ((i = 2; i < CURRENT; i++)); do
    if [[ "${words[i]}" != -* ]]; then cmd="${words[i]}"; break; fi
  done
  case "$cmd" in
%(option_cases)s
  esac
  local npos=0 skip=0 prev=""
  for ((i = i + 1; i < CURRENT; i++)); do
    w="${words[i]}"
    if ((skip)); then skip=0; prev=""; continue; fi
    if [[ "$w" == "--" ]]; then npos=-1; break; fi
    if [[ "$w" == -* ]]; then
      if (( ${vopts[(Ie)$w]} )); then skip=1; prev="$w"; fi
      continue
    fi
    ((npos++))
  done
  local cur="${words[CURRENT]}" kind=""
  if ((skip)); then
    # 与 bash 版相同的查表顺序；zsh 的 candidates 是数组，赋值都用 (...) 写法。
    case "$cmd:$prev" in
%(option_value_cases)s
      *)
        case "$prev" in
          %(shared_options)s) candidates=(${(f)"$(multi-claude list --shared-items 2>/dev/null)"}) ;;
          %(path_options)s) _files; return ;;
          %(name_options)s) kind=name ;;
          *) return ;;
        esac ;;
    esac
  elif [[ -z "$cmd" ]]; then
    candidates=(%(commands)s --help --version)
  elif ((npos < 0)); then
    return
  elif [[ "$cur" == -* ]]; then
    candidates=($opts)
  else
    case "$cmd:$npos" in
%(positional_cases)s
    esac
  fi
  case "$kind" in
    name) candidates=(${(f)"$(multi-claude list --names 2>/dev/null)"}) ;;
    dir) _files -/; return ;;
    file) _files; return ;;
    lock) candidates=(on off) ;;
  esac
  compadd -- $candidates
}
compdef _multi_claude multi-claude
""" % {"option_cases": option_cases, "commands": _words(commands),
       "positional_cases": _positional_cases(values, "zsh", "      "),
       "option_value_cases": _option_value_cases(values, "zsh", "      "),
       "shared_options": "|".join(SHARED_OPTIONS), "path_options": "|".join(PATH_OPTIONS),
       "name_options": "|".join(NAME_OPTIONS)}


def _fish(specs: Dict[str, CommandSpec], values: ValueSpec) -> str:
    commands = sorted(name for name in specs if name)
    lines = [
        "# fish completion for multi-claude (generated by `multi-claude completion fish`)",
        "complete -c multi-claude -f",
        "function __multi_claude_positionals",
        "    # 输出当前子命令之后已输入的位置参数个数（跳过选项与它的值）；遇到 -- 输出 -1；",
        "    # 光标正在补带值选项的值时输出 -2，免得位置参数的候选（如账号名）混进选项取值里。",
        "    set -l tokens (commandline -opc)",
        "    set -l value_options $argv",
        "    set -l seen 0",
        "    set -l skip 0",
        "    set -l count 0",
        "    for token in $tokens[2..-1]",
        "        if test $seen -eq 0",
        "            if not string match -q -- '-*' $token",
        "                set seen 1",
        "            end",
        "            continue",
        "        end",
        "        if test $skip -eq 1",
        "            set skip 0",
        "            continue",
        "        end",
        "        if test \"$token\" = '--'",
        "            echo -1",
        "            return",
        "        end",
        "        if string match -q -- '-*' $token",
        "            if contains -- $token $value_options",
        "                set skip 1",
        "            end",
        "            continue",
        "        end",
        "        set count (math $count + 1)",
        "    end",
        "    if test $skip -eq 1",
        "        echo -2",
        "        return",
        "    end",
        "    echo $count",
        "end",
        "complete -c multi-claude -n __fish_use_subcommand -a {}".format(shlex.quote(_words(commands))),
        "complete -c multi-claude -n __fish_use_subcommand -l help -l version",
    ]
    for name, spec in sorted(specs.items()):
        if not name:
            continue
        condition = "__fish_seen_subcommand_from {}".format(name)
        for option in spec.options:
            flag = "-l {}".format(option[2:]) if option.startswith("--") else "-s {}".format(option[1:])
            requires = " -r" if option in spec.value_options else ""
            files = " -F" if option in PATH_OPTIONS else ""
            names = " -a '(multi-claude list --names 2>/dev/null)'" if option in NAME_OPTIONS else ""
            # -x 即“要跟值、且不补文件名”；fish 生成文本里不能用 $(...)（ubuntu-22.04 的旧 fish 不支持）。
            if (name, option) in values.option_values:
                requires = " -x -a {}".format(shlex.quote(_words(values.option_values[(name, option)])))
            elif option in SHARED_OPTIONS:
                requires = " -x -a '(multi-claude list --shared-items 2>/dev/null)'"
            lines.append("complete -c multi-claude -n {} {}{}{}{}".format(
                shlex.quote(condition), flag, requires, files, names))
        vopts = _words(spec.value_options)
        for index, kind in enumerate(values.kinds.get(name, [])):
            if kind == "none":
                continue
            at_position = shlex.quote("{}; and test (__multi_claude_positionals {}) -eq {}".format(
                condition, vopts, index))
            if kind == "name":
                source = "'(multi-claude list --names 2>/dev/null)'"
            elif kind == "dir":
                source = "'(__fish_complete_directories)'"
            elif kind == "file":
                source = "'(__fish_complete_path)'"
            elif kind in FIXED_WORDS:
                source = shlex.quote(_words(list(FIXED_WORDS[kind])))
            else:
                source = shlex.quote(_words(values.positional[(name, index)]))
            lines.append("complete -c multi-claude -n {} -a {}".format(at_position, source))
    return "\n".join(lines) + "\n"


def _ps_quote(text: str) -> str:
    # PowerShell 单引号串里只有 ' 需要写成 ''
    return "'" + text.replace("'", "''") + "'"


def _ps_array(items: List[str]) -> str:
    return "@(" + ", ".join(_ps_quote(item) for item in items) + ")"


def _ps_table(table: Dict[str, List[str]], indent: str) -> str:
    return "\n".join("{}{} = {}".format(indent, _ps_quote(key), _ps_array(list(value)))
                     for key, value in sorted(table.items()))


def _powershell(specs: Dict[str, CommandSpec], values: ValueSpec) -> str:
    """PowerShell 补全（方案 feature-windows-support F6）。用法：`multi-claude completion powershell | Add-Content $PROFILE`。

    数位置参数的规则与 bash 版相同：跳过选项和它的值，遇到 -- 不再补全。补全器什么都不返回时，
    PowerShell 会退回按文件名补全，所以目录、文件类的位置参数直接返回空。
    """
    commands = sorted(name for name in specs if name)
    return """# PowerShell completion for multi-claude (generated by `multi-claude completion powershell`)
Register-ArgumentCompleter -Native -CommandName multi-claude, multi-claude.exe -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = %(commands)s
    $options = @{
%(options)s
    }
    $valueOptions = @{
%(value_options)s
    }
    $positionals = @{
%(positionals)s
    }
    # Keys are 'command:position' and 'command:option'
    $fixedWords = @{
%(fixed)s
    }
    $optionValues = @{
%(option_values)s
    }
    $sharedOptions = %(shared_options)s
    $pathOptions = %(path_options)s
    $nameOptions = %(name_options)s
    # Words fully typed before the cursor; the first one is the command name itself
    $before = @($commandAst.CommandElements | Where-Object { $_.Extent.EndOffset -lt $cursorPosition } |
        ForEach-Object { $_.Extent.Text })
    $command = ''
    $count = 0
    $skip = $false
    $prev = ''
    foreach ($token in ($before | Select-Object -Skip 1)) {
        if ($command -eq '') {
            if (-not $token.StartsWith('-')) { $command = $token }
            $prev = $token
            continue
        }
        if ($skip) { $skip = $false; $prev = $token; continue }
        if ($token -eq '--') { return }
        if ($token.StartsWith('-')) {
            if ($valueOptions[$command] -contains $token) { $skip = $true }
            $prev = $token
            continue
        }
        $count++
        $prev = $token
    }
    $candidates = @()
    if ($command -eq '') {
        if ($wordToComplete.StartsWith('-')) { $candidates = $options[''] } else { $candidates = $commands }
    } elseif ($skip) {
        # Completing the value of the previous option; build keys with + because "$command:" would parse as a scoped variable
        $key = ($command + ':' + $prev)
        if ($optionValues.ContainsKey($key)) { $candidates = $optionValues[$key] }
        elseif ($sharedOptions -contains $prev) { $candidates = @(multi-claude list --shared-items 2>$null) }
        elseif ($nameOptions -contains $prev) { $candidates = @(multi-claude list --names 2>$null) }
        else { return }
    } elseif ($wordToComplete.StartsWith('-')) {
        $candidates = $options[$command]
    } else {
        $kinds = $positionals[$command]
        if (-not $kinds -or $count -ge $kinds.Count) { return }
        $kind = $kinds[$count]
        if ($kind -eq 'name') { $candidates = @(multi-claude list --names 2>$null) }
        elseif ($kind -eq 'fixed') { $candidates = $fixedWords[($command + ':' + $count)] }
        else { return }
    }
    $candidates | Where-Object { $_ -and $_.StartsWith($wordToComplete, [System.StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }
}
""" % {
        "commands": _ps_array(commands),
        "options": _ps_table({name: spec.options for name, spec in specs.items()}, " " * 8),
        "value_options": _ps_table({name: spec.value_options for name, spec in specs.items() if name}, " " * 8),
        "positionals": _ps_table(values.kinds, " " * 8),
        "fixed": _ps_table({"{}:{}".format(*key): words for key, words in values.positional.items()}, " " * 8),
        "option_values": _ps_table({"{}:{}".format(*key): words for key, words in values.option_values.items()},
                                   " " * 8),
        "shared_options": _ps_array(list(SHARED_OPTIONS)),
        "path_options": _ps_array(list(PATH_OPTIONS)),
        "name_options": _ps_array(list(NAME_OPTIONS)),
    }
