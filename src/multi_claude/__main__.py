import sys

if sys.argv[1:2] == ["statusline-hook"]:
    # 状态栏每次刷新都会执行它：只导入轻量的 hook，exec 之前不加载 cli 与其它模块。
    # 这个字面量必须与 hook.HOOK_COMMAND 一致（测试断言）；这里不导入 hook 来比较，
    # 是为了让其它命令连 hook 也不加载。
    from .hook import main as hook_main
    sys.exit(hook_main(sys.argv[2:]))

from .cli import main  # noqa: E402

sys.exit(main())
