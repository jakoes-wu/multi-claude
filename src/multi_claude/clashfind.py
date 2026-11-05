"""`multi-claude clash-api auto`：找本机正在运行的 Clash / mihomo 内核的外部控制接口（feature-launch-gate §5.1.5）。

候选来源按顺序：① 正在运行的 Clash 类进程的启动参数（-ext-ctl / -ext-ctl-unix / -ext-ctl-pipe / -secret / -d / -f）
及其配置文件；② Clash Verge 的已知配置文件。配置文件里写的地址不一定在用（本机实测 Clash Verge 用户目录
config.yaml 里的 unix 套接字并不存在），所以每个候选都要实际 GET /configs，能读到 mode 与 tun.enable 的才采信。

只读：列进程、读配置文件、发 GET 请求，不改 Clash 的任何设置。只连本机地址，读到的 secret 不发往别的主机。
"""

import ipaddress
import os
import re
import sys
import urllib.parse
from typing import Dict, List, Optional, Tuple

from . import platform
from .config import PIPE_PREFIX, ClashApi, normalize_clash_address
from .launchgate import CheckFailure, read_clash_state

# 只取这几个参数的值；mihomo 用 Go 标准 flag 包解析（main.go:61-77），写法有 -name value、-name=value、
# --name value、--name=value 四种。
_STRING_FLAGS = ("d", "f", "ext-ctl", "ext-ctl-unix", "ext-ctl-pipe", "secret")
_CONTROLLER_FLAGS = ("ext-ctl", "ext-ctl-unix", "ext-ctl-pipe")

# Clash Verge 的已知配置文件（只列已在本机 / 测试机上实测存在的位置）。
_VERGE_DIR = "io.github.clash-verge-rev.clash-verge-rev"

_CONFIG_KEY = re.compile(r"^(external-controller(?:-unix|-pipe)?|secret)\s*:(.*)$")

_UNSUPPORTED = "not supported on this platform"


class _Candidate(object):
    def __init__(self, address: str, secret: str, source: str) -> None:
        self.address = address
        self.secret = secret
        self.source = source

    @property
    def is_tcp(self) -> bool:
        return self.address.startswith("http://")


class _Collector(object):
    """按出现顺序收集候选，以规范化后的地址去重；同一地址多次出现时保留第一份带 secret 的。"""

    def __init__(self) -> None:
        self.candidates = {}  # type: Dict[str, _Candidate]
        self.notes = []  # type: List[str]

    def add_unix(self, path: str, home: Optional[str], source: str) -> None:
        if not os.path.isabs(path):
            # mihomo 按主目录解析相对路径（hub/route/server.go:260 → constant/path.go:80-84）；主目录未知就无法还原。
            if home is None:
                self.notes.append("unix:{} ({}): relative path and the home directory is unknown".format(path, source))
                return
            path = os.path.join(home, path)
        self._add("unix:" + path, "", source)

    def add_pipe(self, path: str, source: str) -> None:
        self._add(path, "", source)

    def add_tcp(self, value: str, secret: str, source: str) -> None:
        parts = urllib.parse.urlsplit("http://" + value)
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is None:
            self.notes.append("{} ({}): no port".format(value, source))
            return
        host = parts.hostname or ""
        if host in ("", "0.0.0.0"):
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if not _is_loopback(host):
            # 只找本机 Clash：别的主机上的接口请用 `clash-api ADDRESS` 手动设置，读到的 secret 不往外发。
            self.notes.append("{} ({}): not a local address, skipped".format(value, source))
            return
        self._add("[{}]:{}".format(host, port) if ":" in host else "{}:{}".format(host, port), secret, source)

    def _add(self, raw: str, secret: str, source: str) -> None:
        is_pipe = raw.lower().startswith(PIPE_PREFIX)
        if (is_pipe and not platform.IS_WINDOWS) or (raw.startswith("unix:") and platform.IS_WINDOWS):
            self.notes.append("{} ({}): {}".format(raw, source, _UNSUPPORTED))
            return
        try:
            address = normalize_clash_address(raw)
        except ValueError as exc:
            self.notes.append("{} ({}): {}".format(raw, source, exc))
            return
        key = address.lower() if is_pipe else address
        existing = self.candidates.get(key)
        if existing is None:
            self.candidates[key] = _Candidate(address, secret, source)
        elif secret and not existing.secret:
            self.candidates[key] = _Candidate(address, secret, source)


def find_controller() -> Tuple[Optional[ClashApi], str, List[str]]:
    """返回 (找到的设置, 读到的状态说明如 `mode=global, tun on`, 逐个候选的结果说明)；找不到时第一项为 None。

    结果说明只含地址与来源，不含 secret。unix 套接字与命名管道排在 TCP 前面：mihomo 不校验它们的 secret
    （hub/route/server.go 的 startUnix / startPipe），保存时 secret 一律为空。
    """
    collector = _Collector()
    try:
        processes = platform.list_process_argv()
    except (platform.BusyCheckError, OSError) as exc:
        collector.notes.append("cannot list processes: {}".format(exc))
        processes = []
    for argv in processes:
        _collect_process(collector, argv)
    for path in _known_config_files():
        if os.path.isfile(path):
            _collect_config(collector, path, os.path.dirname(path), None, "config " + path)
    ordered = sorted(collector.candidates.values(), key=lambda candidate: candidate.is_tcp)
    for candidate in ordered:
        secret = candidate.secret if candidate.is_tcp else ""
        try:
            mode, tun_on = read_clash_state(candidate.address, secret)
        except CheckFailure as exc:
            collector.notes.append("{} ({}): {}".format(candidate.address, candidate.source, _describe(exc)))
            continue
        detail = "mode={}, tun {}".format(mode, "on" if tun_on else "off")
        collector.notes.append("{} ({}): ok, {}".format(candidate.address, candidate.source, detail))
        return ClashApi(candidate.address, secret), detail, collector.notes
    return None, "", collector.notes


def _describe(exc: CheckFailure) -> str:
    """把候选验证失败写成一句结果说明；连不上时保留原因（如 connection refused / timed out）。"""
    if exc.kind == "unreachable":
        text = str(exc)
        # 消息形如 "Clash controller ADDRESS: 原因"，只留原因，地址已在行首。
        return text.split(": ", 1)[1] if ": " in text else text
    if exc.kind == "auth":
        return "needs a secret"
    if exc.kind == "http":
        return "HTTP " + str(exc).rsplit(" ", 1)[-1]
    if exc.kind == "not-clash":
        return "not a Clash controller"
    return str(exc)


def _collect_process(collector: _Collector, argv: List[str]) -> None:
    """从一个进程的参数里收集候选；只处理 Clash 类进程（程序名含 mihomo / clash，或带 -ext-ctl*）。"""
    if not argv:
        return
    flags = _parse_flags(argv[1:])
    program = os.path.basename(argv[0].replace("\\", "/")).lower()
    if "mihomo" not in program and "clash" not in program and not any(name in flags for name in _CONTROLLER_FLAGS):
        return
    source = "process " + (os.path.basename(argv[0].replace("\\", "/")) or argv[0])
    # 主目录：-d 缺省时 mihomo 用自己的默认目录、相对路径按进程当前目录解析（main.go:126-129），这两种都拿不到。
    home = flags.get("d")
    if home is not None and not os.path.isabs(home):
        home = None
    secret = flags.get("secret")
    if "ext-ctl-unix" in flags:
        collector.add_unix(flags["ext-ctl-unix"], home, source)
    if "ext-ctl-pipe" in flags:
        collector.add_pipe(flags["ext-ctl-pipe"], source)
    config_path = None  # type: Optional[str]
    if "f" in flags:
        # 相对 -f 按进程当前目录解析（main.go:160-163），拿不到，跳过。
        config_path = flags["f"] if os.path.isabs(flags["f"]) else None
    elif home is not None:
        config_path = os.path.join(home, "config.yaml")  # mihomo 默认配置文件名（constant/path.go:51）
    file_secret = ""
    if config_path is not None:
        file_secret = _collect_config(collector, config_path, home, secret, "{} config {}".format(source, config_path))
    if "ext-ctl" in flags:
        # -secret 覆盖整个控制接口的 secret（main.go:212-214），没有时才用配置文件里的。
        collector.add_tcp(flags["ext-ctl"], secret if secret is not None else file_secret, source)


def _collect_config(collector: _Collector, path: str, home: Optional[str], secret_override: Optional[str],
                    source: str) -> str:
    """读一份配置文件里的接口地址加入候选，返回文件里的 secret（读不到为空）。读不了的文件静默跳过。"""
    values = _read_config(path)
    if values is None:
        return ""
    file_secret = values.get("secret", "")
    if values.get("external-controller-unix"):
        collector.add_unix(values["external-controller-unix"], home, source)
    if values.get("external-controller-pipe"):
        collector.add_pipe(values["external-controller-pipe"], source)
    if values.get("external-controller"):
        collector.add_tcp(values["external-controller"],
                          secret_override if secret_override is not None else file_secret, source)
    return file_secret


def _read_config(path: str) -> Optional[Dict[str, str]]:
    """只读顶层（行首无缩进）的四个键，不引入 YAML 库；文件读不了返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    values = {}  # type: Dict[str, str]
    for line in lines:
        match = _CONFIG_KEY.match(line)
        if match and match.group(1) not in values:
            values[match.group(1)] = _yaml_scalar(match.group(2))
    return values


def _yaml_scalar(raw: str) -> str:
    """取一个单行标量：成对引号内的内容（双引号处理 \\\\ 与 \\" 转义，单引号处理 ''），否则去掉 ` #` 之后的注释。"""
    text = raw.strip()
    if text[:1] == '"':
        out = []
        index = 1
        while index < len(text) and text[index] != '"':
            if text[index] == "\\" and index + 1 < len(text):
                index += 1
            out.append(text[index])
            index += 1
        return "".join(out)
    if text[:1] == "'":
        # 单引号里两个 '' 表示一个 '。
        out = []
        index = 1
        while index < len(text):
            if text[index] == "'":
                if text[index + 1:index + 2] != "'":
                    break
                index += 1
            out.append(text[index])
            index += 1
        return "".join(out)
    if text.startswith("#"):
        return ""
    return text.split(" #", 1)[0].strip()


def _parse_flags(args: List[str]) -> Dict[str, str]:
    flags = {}  # type: Dict[str, str]
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if not token.startswith("-") or token in ("-", "--"):
            continue
        name = token[2:] if token.startswith("--") else token[1:]
        if "=" in name:
            name, value = name.split("=", 1)
            if name in _STRING_FLAGS:
                flags[name] = value
        elif name in _STRING_FLAGS and index < len(args):
            flags[name] = args[index]
            index += 1
    return flags


def _known_config_files() -> List[str]:
    if platform.IS_WINDOWS:
        appdata = os.environ.get("APPDATA")
        return [os.path.join(appdata, _VERGE_DIR, "config.yaml")] if appdata else []
    if sys.platform == "darwin":
        return [os.path.join(os.path.expanduser("~"), "Library", "Application Support", _VERGE_DIR, "config.yaml")]
    return []


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
