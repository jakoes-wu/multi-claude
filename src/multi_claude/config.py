"""配置模型：读写 config.json，校验账号名、代理值、额外环境变量与启动参数。

config.json 是唯一的权威源，启动命令、共享链接都是从它推导出来的产物（方案 §5.1.1）。
本模块只负责数据本身，不创建或删除任何账号文件。
"""

import json
import os
import re
import urllib.parse
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import platform
from .fsutil import atomic_write, expand

CONFIG_VERSION = 1
DEFAULT_SHARED_ITEMS = ["agents", "commands", "skills", "CLAUDE.md"]
# 账号私有的状态：凭据、全局状态、会话与历史、运行时状态。共享出去会让一个账号的登录或会话
# 被所有账号读写，所以不允许出现在 shared.items 里（方案 feature-shared-exclude §5.1.4）。
UNSHAREABLE_ITEMS = (".credentials.json", ".claude.json", "settings.local.json", "projects", "history.jsonl",
                     "file-history", "sessions", "session-env", "shell-snapshots", "todos")


def is_unshareable(item: str) -> bool:
    # macOS 默认文件系统不区分大小写，`Projects` 指向的就是 `projects`。
    return item.casefold() in {name.casefold() for name in UNSHAREABLE_ITEMS}

# 以字母或数字开头：不会被当成命令行选项，也不会以 `.` 开头与 `.migration` 等目录混淆。
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")

PROXY_INHERIT = "inherit"
PROXY_OFF = "off"
# Claude Code 官方文档明确写着不支持 SOCKS 代理（方案 §3 依据 5），所以只接受 http/https。
PROXY_SCHEMES = ("http", "https")
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
              "https_proxy", "http_proxy", "all_proxy", "no_proxy")

# 账号身份（方案 §5.1.4、§5.1.6）：
# - dir：启动命令设置 CLAUDE_CONFIG_DIR=<账号目录>，macOS 登录绑定在这串路径上；
# - default：由 migrate-default 产生，启动命令不设 CLAUDE_CONFIG_DIR，沿用 ~/.claude、
#   ~/.claude.json 和不带后缀的钥匙串条目，与直接运行 `claude` 完全一致。
IDENTITY_DIR = "dir"
IDENTITY_DEFAULT = "default"
IDENTITIES = (IDENTITY_DIR, IDENTITY_DEFAULT)

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 改变账号身份的变量：写进启动命令会静默改变登录绑定（方案 §5.1.5）。
_IDENTITY_KEYS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_CUSTOM_OAUTH_URL",
                  "HOME", "USER")
# 官方 env-vars 页列出的凭据变量；启动命令是 0755 明文文件，写进去即泄露。
_CREDENTIAL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_AWS_API_KEY",
                    "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "MCP_CLIENT_SECRET")
# 按下划线分段后整段相等才算凭据：`MAX_THINKING_TOKENS` 的 `TOKENS` 不能误伤。
_CREDENTIAL_SEGMENTS = ("TOKEN", "SECRET", "PASSWORD", "PASSPHRASE")


class ConfigError(Exception):
    """配置文件内容不合法，对应退出码 1。"""


class RouteRule(NamedTuple):
    """按目录选账号的一条规则：当前目录位于 path 之下时用 account（方案 feature-directory-routing §5.1.1）。"""
    # 用户写法（如 "~/work"）；比较时经 expand 展开，claude-auto 启动时再求物理路径
    path: str
    account: str


class Account(object):
    """一个已登记的账号。

    managed_links 与 identity 是工具内部状态：前者记录本工具在该账号目录里建立过的共享链接，
    关闭共享时只删除这些链接；后者只能由 migrate-default 改为 default，apply -f 不接受外部改写。
    env / args 是启动命令额外设置的环境变量和固定参数，与全局 defaults 合并后生效。
    """

    def __init__(self, name: str, proxy: str = PROXY_INHERIT, shared: bool = False,
                 managed_links: Optional[List[str]] = None, identity: str = IDENTITY_DIR,
                 env: Optional[Dict[str, str]] = None, args: Optional[List[str]] = None,
                 dir: Optional[str] = None, shared_exclude: Optional[List[str]] = None) -> None:
        self.name = name
        # 账号目录在根目录下的名字。缺省等于账号名；rename 只改 name、不改 dir，
        # 目录路径不变，macOS 上按路径绑定的登录就不会丢（方案 feature-rename-mcp §3）。
        self.dir = dir or name
        self.proxy = proxy
        self.shared = shared
        self.managed_links = list(managed_links or [])
        # 共享开启时仍不链接的项（用户配置，apply -f 按文件生效）；本工具建过的这些项的链接会被删除。
        self.shared_exclude = list(shared_exclude or [])
        self.identity = identity
        self.env = dict(env or {})
        self.args = list(args or [])

    def copy(self) -> "Account":
        return Account(self.name, self.proxy, self.shared, list(self.managed_links), self.identity,
                       dict(self.env), list(self.args), self.dir, list(self.shared_exclude))

    def to_dict(self) -> dict:
        data = {"identity": self.identity, "proxy": self.proxy, "shared": self.shared,
                "managed_links": list(self.managed_links), "env": dict(self.env), "args": list(self.args)}
        # 只在与账号名不同时写出：没改过名的账号，配置内容与 v0.2 逐字相同。
        if self.dir != self.name:
            data["dir"] = self.dir
        # 同理只在非空时写出，没有退出项的账号配置内容不变。
        if self.shared_exclude:
            data["shared_exclude"] = list(self.shared_exclude)
        return data


class Config(object):
    def __init__(self, root: str, bin_dir: str, shared_dir: Optional[str], shared_items: List[str],
                 accounts: Dict[str, Account], defaults_env: Optional[Dict[str, str]] = None,
                 defaults_args: Optional[List[str]] = None, route_rules: Optional[List[RouteRule]] = None,
                 route_default: Optional[str] = None) -> None:
        self.root = root
        self.bin_dir = bin_dir
        self.shared_dir = shared_dir
        self.shared_items = list(shared_items)
        # 键保持账号名的原始大小写；查找一律经 find() 做大小写不敏感匹配。
        self.accounts = accounts
        self.defaults_env = dict(defaults_env or {})
        self.defaults_args = list(defaults_args or [])
        # 路由规则保持配置中的顺序：两条规则的物理路径相同时，靠前的一条生效。
        self.route_rules = list(route_rules or [])
        # 没有规则命中时 claude-auto 使用的账号名；None 表示直接运行 claude。
        self.route_default = route_default

    @property
    def routes_enabled(self) -> bool:
        return bool(self.route_rules) or self.route_default is not None

    def find(self, name: str) -> Optional[Account]:
        """按大小写不敏感匹配查找账号。

        macOS 默认文件系统不区分大小写，`Work` 与 `work` 实际指向同一个目录和同一个启动命令，
        所以必须视为同一个账号。
        """
        folded = name.casefold()
        for account in self.accounts.values():
            if account.name.casefold() == folded:
                return account
        return None

    def default_account(self) -> Optional[Account]:
        for account in self.accounts.values():
            if account.identity == IDENTITY_DEFAULT:
                return account
        return None

    def effective_env(self, account: Account) -> Dict[str, str]:
        """全局 defaults.env 再用账号 env 覆盖同名键（方案 §5.1.5）。"""
        merged = dict(self.defaults_env)
        merged.update(account.env)
        return merged

    def effective_args(self, account: Account) -> List[str]:
        """全局 defaults.args 在前、账号 args 在后；启动命令再把 "$@" 接在最后。"""
        return list(self.defaults_args) + list(account.args)

    def copy(self) -> "Config":
        return Config(self.root, self.bin_dir, self.shared_dir, self.shared_items,
                      {key: value.copy() for key, value in self.accounts.items()},
                      dict(self.defaults_env), list(self.defaults_args), list(self.route_rules), self.route_default)

    def to_dict(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "root": self.root,
            "bin_dir": self.bin_dir,
            "shared": {"dir": self.shared_dir, "items": list(self.shared_items)},
            "defaults": {"env": dict(self.defaults_env), "args": list(self.defaults_args)},
            "accounts": {name: account.to_dict() for name, account in self.accounts.items()},
            "routes": {"default": self.route_default,
                       "rules": [{"path": rule.path, "account": rule.account} for rule in self.route_rules]},
        }


def default_config() -> Config:
    return Config(platform.default_root(), platform.default_bin_dir(), None,
                  DEFAULT_SHARED_ITEMS, {})


def config_path() -> str:
    return os.path.join(platform.state_dir(), "config.json")


def dump_config(config: Config) -> str:
    return json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"


def load_config() -> Tuple[Config, bool]:
    """读取 config.json，返回 (配置, 文件是否存在)。

    文件不存在时返回默认配置，第一次写命令会把它写出来（方案 §5.1.1 自动初始化）。
    """
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return default_config(), False
    except OSError as exc:
        raise ConfigError("cannot read {}: {}".format(path, exc))
    return parse_config(raw, path), True


def save_config(config: Config) -> None:
    atomic_write(config_path(), dump_config(config), mode=0o600)


def parse_config(raw: str, source: str) -> Config:
    """解析并校验配置文本；任何不合法内容都抛出 ConfigError，并指明出错的字段。"""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("{} is not valid JSON: {}".format(source, exc))
    if not isinstance(data, dict):
        raise ConfigError("{}: top level must be an object".format(source))
    if data.get("version", CONFIG_VERSION) != CONFIG_VERSION:
        raise ConfigError("{}: unsupported version {!r}".format(source, data.get("version")))

    defaults = default_config()
    root = _string_field(data, "root", defaults.root, source)
    bin_dir = _string_field(data, "bin_dir", defaults.bin_dir, source)

    shared = data.get("shared", {})
    if not isinstance(shared, dict):
        raise ConfigError("{}: 'shared' must be an object".format(source))
    shared_dir = shared.get("dir")
    if shared_dir is not None and (not isinstance(shared_dir, str) or not shared_dir):
        raise ConfigError("{}: 'shared.dir' must be a non-empty string or null".format(source))
    shared_items = shared.get("items", DEFAULT_SHARED_ITEMS)
    if not isinstance(shared_items, list) or not all(_valid_item(item) for item in shared_items):
        raise ConfigError("{}: 'shared.items' must be a list of plain file names".format(source))
    for item in shared_items:
        if is_unshareable(item):
            raise ConfigError("{}: 'shared.items' must not include {}: it holds account-specific state".format(
                source, item))

    launch_defaults = data.get("defaults", {})
    if not isinstance(launch_defaults, dict):
        raise ConfigError("{}: 'defaults' must be an object".format(source))
    defaults_env = _parse_env(launch_defaults.get("env", {}), "{}: defaults.env".format(source))
    defaults_args = _parse_args(launch_defaults.get("args", []), "{}: defaults.args".format(source))

    accounts_raw = data.get("accounts", {})
    if not isinstance(accounts_raw, dict):
        raise ConfigError("{}: 'accounts' must be an object".format(source))
    accounts: Dict[str, Account] = {}
    seen = {}
    default_name = None
    for name, value in accounts_raw.items():
        if not NAME_PATTERN.match(name):
            raise ConfigError("{}: invalid account name {!r}".format(source, name))
        folded = name.casefold()
        if folded in seen:
            raise ConfigError("{}: account names {!r} and {!r} differ only in case".format(
                source, seen[folded], name))
        seen[folded] = name
        account = _parse_account(name, value, source)
        if account.identity == IDENTITY_DEFAULT:
            # 一台机器只有一个 ~/.claude，不可能有两个 default 身份的账号。
            if default_name is not None:
                raise ConfigError("{}: accounts {!r} and {!r} both have identity 'default'".format(
                    source, default_name, name))
            default_name = name
        accounts[name] = account
    _check_dirs(accounts, source)
    route_rules, route_default = _parse_routes(data.get("routes", {}), source)
    return Config(root, bin_dir, shared_dir, shared_items, accounts, defaults_env, defaults_args,
                  route_rules, route_default)


def _check_dirs(accounts: Dict[str, Account], source: str) -> None:
    """账号目录互不相同，且账号名不能等于另一个账号的目录（否则新账号会落进别人的目录）。"""
    owners: Dict[str, str] = {}
    for account in accounts.values():
        folded = account.dir.casefold()
        if folded in owners:
            raise ConfigError("{}: accounts {!r} and {!r} use the same directory {!r}".format(
                source, owners[folded], account.name, account.dir))
        owners[folded] = account.name
    for account in accounts.values():
        owner = owners.get(account.name.casefold())
        if owner is not None and owner != account.name:
            raise ConfigError("{}: account {!r} has the name of the directory of account {!r}".format(
                source, account.name, owner))


def _parse_routes(value: object, source: str) -> Tuple[List[RouteRule], Optional[str]]:
    """只校验形状；“规则引用的账号已登记”放到收敛计划里判冲突，remove 与 apply -f 才能得到同样的退出码 3。"""
    if not isinstance(value, dict):
        raise ConfigError("{}: 'routes' must be an object".format(source))
    default = value.get("default")
    if default is not None and (not isinstance(default, str) or not NAME_PATTERN.match(default)):
        raise ConfigError("{}: 'routes.default' must be an account name or null".format(source))
    rules_raw = value.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ConfigError("{}: 'routes.rules' must be a list".format(source))
    rules: List[RouteRule] = []
    seen = {}
    for item in rules_raw:
        if not isinstance(item, dict) or set(item) != {"path", "account"}:
            raise ConfigError("{}: each route must be an object with exactly 'path' and 'account'".format(source))
        path, account = item["path"], item["account"]
        if not isinstance(account, str) or not NAME_PATTERN.match(account):
            raise ConfigError("{}: route {!r}: invalid account name {!r}".format(source, path, account))
        try:
            expanded = validate_route_path(path)
        except ValueError as exc:
            raise ConfigError("{}: {}".format(source, exc))
        if expanded in seen:
            raise ConfigError("{}: routes {!r} and {!r} point to the same directory".format(
                source, seen[expanded], path))
        seen[expanded] = path
        rules.append(RouteRule(path, account))
    return rules, default


def validate_route_path(path: object) -> str:
    """规则目录必须是绝对路径（可用 ~）且不是 /；返回 expand 后的路径。"""
    if not isinstance(path, str) or not path:
        raise ValueError("route path must be a non-empty string")
    if not (path.startswith("/") or path == "~" or path.startswith("~/")):
        raise ValueError("route path {!r} must be absolute or start with ~/".format(path))
    validate_value(path)
    expanded = expand(path)
    if expanded == "/":
        raise ValueError("route path must not be / (use `route --default` instead)")
    return expanded


def _string_field(data: dict, key: str, default: str, source: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError("{}: '{}' must be a non-empty string".format(source, key))
    return value


def _valid_item(item: object) -> bool:
    # 共享条目只能是账号目录下的一级名称，不能带路径分隔符或指向上级目录。
    return isinstance(item, str) and item not in ("", ".", "..") and "/" not in item


def _parse_account(name: str, value: object, source: str) -> Account:
    if not isinstance(value, dict):
        raise ConfigError("{}: account {!r} must be an object".format(source, name))
    proxy_raw = value.get("proxy", PROXY_INHERIT)
    try:
        proxy = normalize_proxy(proxy_raw if proxy_raw is not None else PROXY_INHERIT)
    except ValueError as exc:
        raise ConfigError("{}: account {!r}: {}".format(source, name, exc))
    shared = value.get("shared", False)
    if not isinstance(shared, bool):
        raise ConfigError("{}: account {!r}: 'shared' must be true or false".format(source, name))
    links = value.get("managed_links", [])
    if not isinstance(links, list) or not all(_valid_item(item) for item in links):
        raise ConfigError("{}: account {!r}: 'managed_links' must be a list of names".format(source, name))
    identity = value.get("identity", IDENTITY_DIR)
    if identity not in IDENTITIES:
        raise ConfigError("{}: account {!r}: 'identity' must be one of {}".format(
            source, name, ", ".join(IDENTITIES)))
    where = "{}: account {!r}".format(source, name)
    env = _parse_env(value.get("env", {}), where + " env")
    args = _parse_args(value.get("args", []), where + " args")
    directory = value.get("dir", name)
    if not isinstance(directory, str) or not NAME_PATTERN.match(directory):
        raise ConfigError("{}: 'dir' must be a directory name like an account name".format(where))
    exclude = value.get("shared_exclude", [])
    if not isinstance(exclude, list) or not all(_valid_item(item) for item in exclude):
        raise ConfigError("{}: 'shared_exclude' must be a list of names".format(where))
    return Account(name, proxy, shared, links, identity, env, args, directory, _unique(exclude))


def _unique(items: List[str]) -> List[str]:
    """去重并保持首次出现的顺序。"""
    seen: List[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _parse_env(value: object, where: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError("{} must be an object".format(where))
    result: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise ConfigError("{}: value of {!r} must be a string".format(where, key))
        try:
            validate_env_key(key)
            validate_value(item)
        except ValueError as exc:
            raise ConfigError("{}: {}".format(where, exc))
        result[key] = item
    return result


def _parse_args(value: object, where: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError("{} must be a list of strings".format(where))
    for item in value:
        try:
            validate_value(item)
        except ValueError as exc:
            raise ConfigError("{}: {}".format(where, exc))
    return list(value)


def validate_name(name: str) -> None:
    if not NAME_PATTERN.match(name):
        raise ValueError(
            "invalid account name {!r}: use 1-64 characters from [A-Za-z0-9._@+-], "
            "starting with a letter or digit".format(name))


def validate_env_key(key: str) -> None:
    """拒绝不能写进启动命令的环境变量名（方案 §5.1.5），比较时不分大小写。"""
    if not isinstance(key, str) or not ENV_KEY_PATTERN.match(key):
        raise ValueError("invalid environment variable name {!r}".format(key))
    upper = key.upper()
    if upper in _IDENTITY_KEYS:
        raise ValueError("{} decides which login the account uses and cannot be set per launcher".format(key))
    if upper in (var.upper() for var in PROXY_VARS):
        raise ValueError("{} is managed by the proxy setting; use `multi-claude proxy`".format(key))
    if (upper in _CREDENTIAL_KEYS or upper == "API_KEY" or upper.endswith("_API_KEY")
            or any(segment in _CREDENTIAL_SEGMENTS for segment in upper.split("_"))):
        raise ValueError("{} looks like a credential; launchers are world-readable files, so put it in the "
                         "account's settings.json env or use apiKeyHelper instead".format(key))


def validate_value(value: str) -> None:
    # 换行和 NUL 写进 sh 脚本会破坏单行赋值的结构，也传不进 exec 的参数。
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("values must not contain line breaks or NUL characters")


def normalize_proxy(value: object) -> str:
    """把用户输入的代理值规范化为 `inherit`、`off` 或 `scheme://host:port`。

    只写端口号时展开为 http://127.0.0.1:<端口>。
    带用户名密码的 URL 一律拒绝：启动命令是所有人可读的明文文件，写进去就泄露了。
    """
    if not isinstance(value, str) or not value:
        raise ValueError("proxy must be a port number, a URL, 'off' or 'inherit'")
    if value in (PROXY_INHERIT, PROXY_OFF):
        return value
    if value.isdigit():
        port = int(value)
        _check_port(port)
        return "http://127.0.0.1:{}".format(port)
    parts = urllib.parse.urlsplit(value)
    if parts.scheme in ("socks5", "socks5h", "socks4", "socks4a", "socks"):
        raise ValueError("SOCKS proxies are not supported by Claude Code "
                         "(https://code.claude.com/docs/en/network-config); use an HTTP proxy")
    if parts.scheme not in PROXY_SCHEMES:
        raise ValueError("unsupported proxy scheme {!r}; use one of {}".format(
            parts.scheme, ", ".join(PROXY_SCHEMES)))
    if parts.username is not None or parts.password is not None:
        raise ValueError("proxy URL must not contain credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("proxy URL must not contain a path, query or fragment")
    host = parts.hostname
    if not host:
        raise ValueError("proxy URL must contain a host")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("proxy URL has an invalid port")
    if port is None:
        raise ValueError("proxy URL must contain a port")
    _check_port(port)
    if ":" in host:
        host = "[{}]".format(host)
    return "{}://{}:{}".format(parts.scheme, host, port)


def _check_port(port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("proxy port must be between 1 and 65535, got {}".format(port))
