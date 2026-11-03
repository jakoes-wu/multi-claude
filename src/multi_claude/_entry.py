"""Windows 启动命令（claude-<名称>.cmd、claude-auto.cmd）与状态栏后台进程的入口脚本。

调用方式固定为 `"<python>" -I "<包目录>\\_entry.py" ...`（方案 feature-windows-support §5.1 D2）：
- 用 `-I` 而不用 `-m`：`-m` 会把当前目录放进模块搜索路径，用户项目里的 json.py 之类文件会顶替标准库；
  `-I` 也让脚本所在的包目录不进搜索路径，包里的 platform.py 等就不会顶替同名标准库模块。
- `-I` 同时忽略 PYTHONPATH，源码检出里运行时解释器找不到本包，所以这里把包的上一级目录追加到搜索路径末尾。

本文件只能用标准库，且必须在导入本包之前完成搜索路径的设置。
"""

import os
import runpy
import sys


def _same_dir(a: str, b: str) -> bool:
    # 启动命令里的路径可能是 8.3 短名（MULTI_~1），而包从解释器原有的长路径加载：
    # 只比 normcase 会把同一目录判成两个。realpath 在 Windows 上会展开短名与链接。
    if os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _main() -> None:
    package_dir = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(package_dir)
    # 追加到末尾而不是插到最前：已安装的同名标准库模块仍优先于包目录之外的任何东西
    if parent not in sys.path:
        sys.path.append(parent)
    import multi_claude
    loaded = os.path.dirname(os.path.abspath(multi_claude.__file__))
    if not _same_dir(loaded, package_dir):
        # 解释器的 site-packages 里另装了一份 multi_claude：继续运行就是在用另一个版本
        sys.stderr.write("multi-claude: loaded the package from {} instead of {}; reinstall multi-claude\n".format(
            loaded, package_dir))
        sys.exit(1)
    runpy.run_module("multi_claude", run_name="__main__", alter_sys=True)


_main()
