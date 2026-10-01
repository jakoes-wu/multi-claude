"""按账号构造进程环境，与启动命令 `claude-<名称>` 的语义一致（方案 feature-rename-mcp §5.1.4）。

`multi-claude mcp NAME …` 不能经启动命令转交：固定参数排在子命令之前，`--allowedTools` 这类取多个值的
选项会把 `mcp`、`add` 吞成自己的值。所以这里用 Python 复刻启动命令里与环境有关的部分（不含固定参数）。
两套实现必须保持一致：测试会对同一账号比较本模块的结果与启动命令实际导出的环境。
处理顺序与 launcher.render 相同：身份 → 清除鉴权覆盖变量 → 额外环境变量 → 代理。
"""

from typing import Dict, Mapping

from . import accounts, launcher
from .config import IDENTITY_DEFAULT, PROXY_INHERIT, PROXY_OFF, PROXY_VARS, Account, Config


def account_env(config: Config, account: Account, base: Mapping[str, str]) -> Dict[str, str]:
    env = dict(base)
    if account.identity == IDENTITY_DEFAULT:
        # default 身份靠“不设 CLAUDE_CONFIG_DIR”使用 ~/.claude 与不带后缀的钥匙串条目。
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    else:
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        env["CLAUDE_CONFIG_DIR"] = accounts.account_dir(config, account.name)
    for name in launcher.AUTH_OVERRIDE_ENV:
        env.pop(name, None)
    for key, value in config.effective_env(account).items():
        env[key] = launcher.expand_value(value)
    _apply_proxy(env, account.proxy)
    return env


def _apply_proxy(env: Dict[str, str], proxy: str) -> None:
    if proxy == PROXY_INHERIT:
        return
    if proxy == PROXY_OFF:
        for name in PROXY_VARS:
            env.pop(name, None)
        return
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[name] = proxy
    env.pop("ALL_PROXY", None)
    env.pop("all_proxy", None)
    # 与启动命令的 ${NO_PROXY:+$NO_PROXY,} 相同：原值为空时不留前导逗号。
    for name in ("NO_PROXY", "no_proxy"):
        current = env.get(name, "")
        env[name] = "{},{}".format(current, launcher._NO_PROXY_HOSTS) if current else launcher._NO_PROXY_HOSTS
