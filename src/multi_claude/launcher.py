"""启动命令 `<bin_dir>/claude-<名称>` 的生成与识别（方案 §5.1.4、§5.1.5）。

启动命令是由 config.json 推导出的产物，内容完全由账号名、身份、账号目录、代理、
额外环境变量和固定参数决定，所以“内容相同即 unchanged”就是幂等判断。第 2 行的受管标记
用来区分本工具生成的文件和用户自己的同名文件：没有标记的文件一律不覆盖、不删除。

`account_dir=` 那一行是 dir 身份账号的登录绑定依据（macOS 钥匙串服务名由它的 SHA-256 决定），
收敛引擎据此判断“会不会改变登录绑定”，所以该行的格式（`account_dir=` + shlex 转义值）
不能随意改动。

Windows 上启动命令是 `claude-<名称>.cmd`（方案 feature-windows-support §5.1 D）：只记录名称、账号目录和
Python 路径，运行时调用隐藏子命令 `launch`，由 Python 按配置组环境与参数。这样 cmd 里不用转义任何值。
"""

import ntpath
import os
import shlex
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from . import platform
from .config import IDENTITY_DEFAULT, NETCHECK_SKIP_ENV, PROXY_INHERIT, PROXY_OFF, PROXY_VARS
from .fsutil import KIND_FILE, entry_kind, read_text

MARKER_PREFIX = "# managed-by: multi-claude account="
LAUNCHER_PREFIX = "claude-"
LAUNCHER_MODE = 0o755
ACCOUNT_DIR_PREFIX = "account_dir="
# Windows .cmd 模板的两行标记：语义与 POSIX 的两行相同，只是注释写法换成 `@rem `
WIN_MARKER_PREFIX = "@rem managed-by: multi-claude account="
WIN_ACCOUNT_DIR_PREFIX = "@rem account_dir="
# 这几个字符在 .cmd 里无法可靠转义（引号会断开路径，% 与 ! 会被当成变量展开），出现时拒绝生成
CMD_UNSAFE_CHARS = ('"', "%", "!", "\n", "\r")

_NO_PROXY_HOSTS = "localhost,127.0.0.1,::1"

# 在 shell 里导出后，会让通过任何启动命令启动的账号都改用这一份凭据（官方 env-vars 页的凭据变量），
# 账号各自的登录就被绕过了，所以启动命令一律清除。`env` 命令本来就拒绝这些键，不会再被设回来。
AUTH_OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_CODE_OAUTH_REFRESH_TOKEN")

# claude --help（2.1.286）中取可变个值的选项：会吞掉后面所有不以 `-` 开头的参数。
VARIADIC_OPTIONS = frozenset(["--add-dir", "--allowedTools", "--allowed-tools", "--betas", "--disallowedTools",
                              "--disallowed-tools", "--file", "--mcp-config", "--tools"])
# 同一版本中写作 `[value]` 的可选值选项：放在末尾且没有值时，会把 "$@" 的第一个参数当成自己的值。
OPTIONAL_VALUE_OPTIONS = frozenset(["-d", "--debug", "-r", "--resume", "-w", "--worktree", "--remote-control",
                                    "--from-pr", "--teleport", "--prompt-suggestions", "--cloud"])


def launcher_path(bin_dir: str, name: str) -> str:
    return os.path.join(bin_dir, LAUNCHER_PREFIX + name + platform.launcher_suffix())


def entry_script() -> str:
    """Windows 启动命令调用的入口脚本：包目录里的 _entry.py（见该文件说明为何不用 `-m`）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "_entry.py")


def windows_command_paths() -> Tuple[str, str]:
    """写进 .cmd 启动命令的 (Python 路径, 入口脚本路径)。"""
    return _cmd_path(sys.executable), _cmd_path(entry_script())


# 含非 ASCII 的路径优先改写成以这些变量开头：cmd 展开变量时用的是 Unicode 值，不经过代码页，
# 中文用户名（C:\Users\张三）下 pipx 的默认目录因此也能写进 .cmd
_CMD_PATH_VARS = ("LOCALAPPDATA", "APPDATA", "USERPROFILE")


def _is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def _cmd_path(path: str) -> str:
    """cmd 按控制台代码页读 .cmd，含非 ASCII 字符的路径会读成乱码，只有这时才改写路径。

    纯 ASCII 路径必须原样写入：短名（如 SITE-P~1）会让 _entry.py 看到的包目录与实际加载的长路径对不上，
    也让启动命令难以辨认。非 ASCII 时先试 `%USERPROFILE%` 这类变量开头的写法（非 ASCII 只在用户名里时成立），
    不成立再换 8.3 短名。中文代码页下中文目录名本身就是合法的 8.3 名，Windows 不为它生成短名，
    所以中文用户名只能靠变量写法（2026-11-03 实机核实）。
    """
    if _is_ascii(path):
        return path
    variable = _env_var_form(path)
    if variable is not None:
        return variable
    return platform.short_path(path)


def _env_var_form(path: str) -> Optional[str]:
    """path 位于某个 _CMD_PATH_VARS 目录下、且余下部分是纯 ASCII 时，返回 `%变量%` 加余下部分；否则 None。

    取最长的匹配目录（LOCALAPPDATA 在 USERPROFILE 之下）。展开结果不会再被展开，Windows 路径也不能含 `"`，
    所以变量值里的字符不影响 .cmd 的解析；唯一例外是 `!`：用户在注册表里打开了 cmd 的延迟展开时，展开后的 `!` 仍会被解析，
    所以含 `!` 的变量值不用。启动时的变量值与生成时不同，启动命令就会找不到 Python，
    此时 doctor 会报启动命令过期，apply 重新生成即可。
    """
    best = None
    for name in _CMD_PATH_VARS:
        value = os.environ.get(name, "").rstrip("\\/")
        if not value or not ntpath.isabs(value) or "!" in value:
            continue
        prefix = ntpath.normcase(value) + "\\"
        if not ntpath.normcase(path).startswith(prefix):
            continue
        # 按原始变量值的长度切：个别字符（如土耳其字母 İ）转小写后会变长，按 prefix 的长度切会多切掉字符
        rest = path[len(value) + 1:]
        if _is_ascii(rest) and (best is None or len(value) > best[0]):
            best = (len(value), "%{}%\\{}".format(name, rest))
    return best[1] if best else None


def _without_var_prefix(value: str) -> str:
    """去掉 _cmd_path 加上的 `%变量%` 开头，剩下的部分才需要按字符规则检查。"""
    for name in _CMD_PATH_VARS:
        token = "%{}%".format(name)
        if value.startswith(token):
            return value[len(token):]
    return value


def cmd_unsafe_reason(values: List[str], commands: Sequence[str] = ()) -> Optional[str]:
    """values / commands 里有 .cmd 无法安全写入的字符时返回说明，否则返回 None。

    commands 是 cmd 真正要执行的路径（Python 与入口脚本）：cmd 按控制台代码页读 .cmd，非 ASCII 字符会被读错，
    所以它们还必须是纯 ASCII（调用方已先取短名）。values 只出现在 `@rem` 注释行里，cmd 不解析，不限非 ASCII。
    """
    commands = [_without_var_prefix(value) for value in commands]
    for value in list(values) + commands:
        for char in CMD_UNSAFE_CHARS:
            if char in value:
                return "path {!r} contains {!r}, which cannot be written safely into a .cmd launcher".format(
                    value, char)
    for value in commands:
        if any(ord(char) > 127 for char in value):
            return ("path {!r} has non-ASCII characters and no 8.3 short name, so cmd.exe cannot run it from a "
                    ".cmd launcher; reinstall multi-claude with pipx under a path without them".format(value))
    return None


def render_windows(name: str, account_dir: str, python: str, entry: str) -> str:
    """生成 Windows 的 `claude-<名称>.cmd`。

    必须用 `-I` 运行脚本而不是 `-m`：`-m` 会把当前目录放进模块搜索路径，用户在含 json.py 之类文件的项目里
    启动时，这些文件会顶替标准库被执行。python / entry 取自 windows_command_paths，调用前须用 cmd_unsafe_reason 检查。
    """
    return "\n".join([
        "@echo off",
        WIN_MARKER_PREFIX + name,
        WIN_ACCOUNT_DIR_PREFIX + account_dir,
        '"{}" -I "{}" launch {} -- %*'.format(python, entry, name),
        "exit /b %ERRORLEVEL%",
        "",
    ])


def expand_value(value: str) -> str:
    """值恰好是 `~` 或以 `~/` 开头时展开成绝对路径；其它写法（如 `--settings=~/x`）原样保留。"""
    if value == "~" or value.startswith("~/"):
        return os.path.expanduser(value)
    return value


def _agent_lock_prefix(wrapper: str) -> List[str]:
    """标记行之后的切账户前缀。已经是 agentnet 时条件为假，不会再次 exec。不跑 netgate。"""
    return [
        'if [ "$(/usr/bin/id -un)" != "agentnet" ]; then',
        "  exec {} \"$0\" \"$@\"".format(shlex.quote(wrapper)),
        "fi",
    ]


def render(name: str, identity: str, account_dir: str, default_dir: str, proxy: str,
           env: Dict[str, str], args: List[str], *, net_check: bool = False,
           agent_lock: Optional[str] = None) -> str:
    """生成 POSIX sh 启动命令的完整内容。所有值都经 shlex.quote 转义。

    net_check 为真时（账号开启了启动前网络检查）插入一段检查（_net_check_lines）；为假时输出与旧版逐字节相同。
    agent_lock 为切换器绝对路径时，在受管标记的下一行插入 `id -un` 前缀；为 None 时与不传该参数逐字节相同。

    account_dir 必须是 expand() 的结果（只展开 `~` 并转绝对路径，不做 realpath），
    因为 dir 身份账号的 macOS 登录绑定在这串字符上。default_dir 是 `$HOME/.claude` 展开后的路径，
    只在 default 身份下使用。
    """
    lines = [
        "#!/bin/sh",
        MARKER_PREFIX + name,
    ]
    if agent_lock:
        lines.extend(_agent_lock_prefix(agent_lock))
    lines.extend([
        "# Generated by multi-claude. Do not edit; use `multi-claude` commands to change it.",
        ACCOUNT_DIR_PREFIX + shlex.quote(account_dir),
        # claude 遇到不存在的配置目录会当成新账号，这里提前给出明确提示。
        'if [ ! -d "$account_dir" ]; then',
        # 用 printf 而不是 echo：dash 的 echo 会解释路径里的反斜杠。
        '  printf \'multi-claude: account directory does not exist: %s\\n\' "$account_dir" >&2',
        "  exit 1",
        "fi",
    ])
    lines.extend(_identity_lines(identity, default_dir))
    if net_check:
        lines.extend(_net_check_lines(name))
    lines.append("unset " + " ".join(AUTH_OVERRIDE_ENV))
    for key in sorted(env):
        lines.append("{}={}; export {}".format(key, shlex.quote(expand_value(env[key])), key))
    lines.extend(_proxy_lines(proxy))
    command = ["exec", "claude"] + [shlex.quote(expand_value(arg)) for arg in args] + ['"$@"']
    lines.extend([
        "if ! command -v claude >/dev/null 2>&1; then",
        '  echo "multi-claude: claude not found in PATH" >&2',
        "  exit 127",
        "fi",
        " ".join(command),
        "",
    ])
    return "\n".join(lines)


def _net_check_lines(name: str) -> List[str]:
    """启动前网络检查段（方案 feature-launch-gate §5.1.3）。

    位置有讲究：放在目录与 default 链接检查之后，报错顺序是“目录 → 链接 → 网络检查 → claude 是否存在”；
    放在设置账号环境之前，netgate 看到的是父环境，再由它按与启动命令相同的算法求 Claude 实际会用的代理。
    `cd /`：`python -m` 形式的 multi-claude 包装会把当前目录放进模块搜索路径，在任意目录启动都不能让
    当前目录里的同名文件被导入。`|| exit $?` 把 5（未通过）、130（中断）、1（配置错误）原样传出。
    检查通过后 unset 跳过变量，Claude 和它启动的子进程不会继承“跳过”。
    """
    return [
        "if ! command -v multi-claude >/dev/null 2>&1; then",
        "  echo 'multi-claude: multi-claude not found in PATH; it runs the network check of this account' >&2",
        "  exit 127",
        "fi",
        "(cd / && exec multi-claude netgate {}) || exit $?".format(shlex.quote(name)),
        "unset " + NETCHECK_SKIP_ENV,
    ]


def _identity_lines(identity: str, default_dir: str) -> List[str]:
    if identity == IDENTITY_DEFAULT:
        return [
            "default_dir=" + shlex.quote(default_dir),
            # default 身份靠“不设 CLAUDE_CONFIG_DIR”保住原登录，前提是 ~/.claude 仍指向账号目录；
            # 用户删掉或改指了这条软链时，直接运行会落到一个空目录，所以拒绝启动。
            # 用 cd -P + pwd -P 比较真实路径：[ -ef ] 不属于 POSIX，shellcheck 会报 SC3013。
            'if [ "$(cd -P -- "$default_dir" 2>/dev/null && pwd -P)" != "$(cd -P -- "$account_dir" && pwd -P)" ]; then',
            "  printf 'multi-claude: %s no longer points to %s; restore the link or run "
            "multi-claude migrate-default again\\n' \"$default_dir\" \"$account_dir\" >&2",
            "  exit 1",
            "fi",
            # 从别的账号的 Claude 会话里运行时会继承它的 CLAUDE_CONFIG_DIR，必须清掉才能落到默认账号。
            "unset CLAUDE_CONFIG_DIR CLAUDE_SECURESTORAGE_CONFIG_DIR",
        ]
    return [
        # 继承来的 CLAUDE_SECURESTORAGE_CONFIG_DIR 会改写钥匙串服务名的计算依据，必须清除。
        "unset CLAUDE_SECURESTORAGE_CONFIG_DIR",
        'CLAUDE_CONFIG_DIR="$account_dir"',
        "export CLAUDE_CONFIG_DIR",
    ]


def _proxy_lines(proxy: str) -> List[str]:
    if proxy == PROXY_INHERIT:
        # 不动任何代理变量，沿用启动时 shell 里的值。
        return []
    if proxy == PROXY_OFF:
        # 清掉全部 8 个变量；只是“不导出”挡不住从父进程继承来的代理。
        return ["unset " + " ".join(PROXY_VARS)]
    q_proxy = shlex.quote(proxy)
    return [
        "HTTPS_PROXY={0}; HTTP_PROXY={0}; https_proxy={0}; http_proxy={0}".format(q_proxy),
        "export HTTPS_PROXY HTTP_PROXY https_proxy http_proxy",
        # Claude 自己调用 git 时会退而使用 ALL_PROXY（方案 §3 依据 5）；清掉继承来的值，
        # 免得父进程里的另一个代理把部分流量带走。
        "unset ALL_PROXY all_proxy",
        # 在用户原有的 NO_PROXY 后追加本地地址，不覆盖用户原来的内网例外；原值为空时不留前导逗号。
        'NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}{}"'.format(_NO_PROXY_HOSTS),
        'no_proxy="${{no_proxy:+$no_proxy,}}{}"'.format(_NO_PROXY_HOSTS),
        "export NO_PROXY no_proxy",
    ]


def variadic_warning(args: List[str]) -> Optional[str]:
    """固定参数的最后一个选项会吞掉运行时传入的参数时，返回告警文字；否则返回 None。

    `--allowedTools a b` 之后接 "$@"，用户传入的提示词或子命令会被当成工具名；
    `--resume` 之类的可选值选项放在末尾且没有值时，会把 "$@" 的第一个参数当成自己的值。
    """
    last_index = None
    for index, arg in enumerate(args):
        if arg.startswith("-"):
            last_index = index
    if last_index is None:
        return None
    option = args[last_index].split("=", 1)[0]
    has_values = last_index < len(args) - 1 or "=" in args[last_index]
    if option in VARIADIC_OPTIONS or (option in OPTIONAL_VALUE_OPTIONS and not has_values):
        return ("{} takes the arguments you pass to the launcher (a prompt or a subcommand) as its own "
                "values; put another option after it".format(option))
    return None


def rendered_account_dir(content: str) -> Optional[str]:
    """从启动命令内容里取出 `account_dir=` 行的值（已去掉 shlex 转义）；取不到时返回 None。

    Windows 的 .cmd 模板里该行是 `@rem account_dir=<目录>`，值原样写入、不转义。
    """
    for line in content.split("\n"):
        if platform.IS_WINDOWS and line.startswith(WIN_ACCOUNT_DIR_PREFIX):
            return line[len(WIN_ACCOUNT_DIR_PREFIX):].rstrip("\r") or None
        if line.startswith(ACCOUNT_DIR_PREFIX):
            try:
                parts = shlex.split(line[len(ACCOUNT_DIR_PREFIX):])
            except ValueError:
                return None
            return parts[0] if len(parts) == 1 else None
    return None


def managed_account(path: str) -> Optional[str]:
    """若 path 是本工具生成的启动命令，返回其中记录的账号名；否则返回 None。

    只认普通文件：软链、目录或读不出文本的文件都视为“不归本工具管理”。
    """
    if entry_kind(path) != KIND_FILE:
        return None
    content = read_text(path)
    if content is None:
        return None
    lines = content.split("\n", 2)
    if len(lines) < 2:
        return None
    # Windows 标记只在 Windows 上认：POSIX 上的识别规则保持不变
    for prefix in (MARKER_PREFIX, WIN_MARKER_PREFIX) if platform.IS_WINDOWS else (MARKER_PREFIX,):
        if lines[1].startswith(prefix):
            return lines[1][len(prefix):].strip() or None
    return None


def scan_managed(bin_dir: str) -> Dict[str, str]:
    """列出 bin_dir 中所有受管启动命令，返回 {账号名: 路径}。bin_dir 不存在时返回空。"""
    found: Dict[str, str] = {}
    try:
        names = os.listdir(bin_dir)
    except OSError:
        return found
    for entry in sorted(names):
        if not entry.startswith(LAUNCHER_PREFIX):
            continue
        path = os.path.join(bin_dir, entry)
        account = managed_account(path)
        if account is not None:
            found[account] = path
    return found
