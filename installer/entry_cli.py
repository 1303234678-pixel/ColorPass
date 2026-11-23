"""打包入口（运行时）：把命令行原样交给 colorpass.cli。

这个文件只做一件事 —— 让 PyInstaller 有个「根」。所有子命令、参数解析、
退出码都在 colorpass.cli 里，和源码模式完全同一条链路，避免出现
「exe 版行为和 python -m 版不一样」这种最难查的分叉。
"""
import sys

from colorpass.cli import main

if __name__ == "__main__":
    sys.exit(main())
