"""生成“把目录加进 PATH”的一行命令（方案 feature-clearer-help §5.1.3）。

只生成给用户看的提示文字，不改任何 shell 配置文件。按 $SHELL 选择配置文件：
macOS 的 bash 登录 shell 读 ~/.bash_profile，其它系统的交互 bash 读 ~/.bashrc；fish 用 fish_add_path。
install.sh 里有同一张表的 shell 实现，两边要保持一致。Windows 上不看 $SHELL，给 PowerShell 写法（方案 feature-windows-support F3）。
"""

import os
from typing import List, Optional

from . import platform


def add_to_path_command(directory: str, shell: Optional[str], system: str) -> Optional[str]:
    """返回可直接粘贴运行的命令；不认识的 shell、或目录里有单引号（无法安全写进单引号串）时返回 None。"""
    if "'" in directory:
        return None
    if system == "Windows":
        # 写进用户级 PATH，新开的终端才生效；pipx 装的 multi-claude 也可以用 `pipx ensurepath` 达到同样效果
        return ("[Environment]::SetEnvironmentVariable('Path', '{};' + "
                "[Environment]::GetEnvironmentVariable('Path', 'User'), 'User')".format(directory))
    name = os.path.basename(shell or "")
    export_line = "export PATH=\"{}:$PATH\"".format(directory)
    if name == "zsh":
        return "echo '{}' >> ~/.zshrc".format(export_line)
    if name == "bash":
        profile = "~/.bash_profile" if system == "Darwin" else "~/.bashrc"
        return "echo '{}' >> {}".format(export_line, profile)
    if name == "fish":
        return "fish_add_path '{}'".format(directory)
    return None


def path_hint(directory: str, shell: Optional[str], system: str) -> str:
    """提示用户把 directory 加进 PATH 的完整一句话（cli 与 doctor 共用）。"""
    command = add_to_path_command(directory, shell, system)
    if command is None:
        return "add it to PATH in your shell profile"
    return "run: {}, then open a new terminal".format(command)


def on_path(directory: str, path_value: str) -> bool:
    """directory 是否已在 PATH 里。Windows 上路径不分大小写，PATH 项还可能带引号或末尾的反斜杠。"""
    if not platform.IS_WINDOWS:
        entries: List[str] = [os.path.abspath(os.path.expanduser(entry))
                              for entry in path_value.split(os.pathsep) if entry]
        return directory in entries
    # 先去引号再展开：否则 "C:\x" 会先被拼到当前目录后面，引号就再也去不掉了
    wanted = os.path.normcase(os.path.normpath(directory))
    return any(os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(entry.strip('"'))))) == wanted
               for entry in path_value.split(os.pathsep) if entry.strip('"'))
