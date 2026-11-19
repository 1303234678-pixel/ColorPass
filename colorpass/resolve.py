"""达芬奇脚本 API 的按需加载。

**不要在模块顶层 `import DaVinciResolveScript`。** 那个模块只存在于装了达芬奇
的机器上，放在顶层会让 `import colorpass.send` 在没有达芬奇的机器上直接炸——
CI、只想跑 `check` / `plan` 的人、单纯 `pip install` 看一眼的人，全都会被挡在门外。

这里改成延迟导入：模块级只拿一个代理对象，真正访问 `dvr.scriptapp(...)` 时才去找
达芬奇，找不到就抛 `ResolveUnavailable`，并说清试过哪些目录。
"""

import os
import sys

# 三个平台的标准位置。找不到不等于出错，只是要等到真正用 API 时才报。
MOD_DIRS = (
    r"C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules",
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
    "/Developer/Scripting/Modules",
    "/opt/resolve/Developer/Scripting/Modules",
    "/opt/resolve/libs/Fusion/Modules",
)

_loaded = None


class ResolveUnavailable(RuntimeError):
    """本机找不到 DaVinciResolveScript —— 多半没装达芬奇，或脚本模块被挪走。"""


def load():
    """返回 DaVinciResolveScript 模块（成功一次就记住）。"""
    global _loaded
    if _loaded is not None:
        return _loaded

    for d in MOD_DIRS:
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)

    try:
        import DaVinciResolveScript as mod
    except Exception as exc:            # ImportError，也可能被同名坏模块顶掉
        raise ResolveUnavailable(
            "找不到 DaVinciResolveScript：本机可能没装达芬奇，或脚本模块位置变了。\n"
            "已尝试：\n  " + "\n  ".join(MOD_DIRS) + "\n"
            "只有「送入 / 取色 / 写回 / 锁定」需要它；check、plan、gc 不需要。"
        ) from exc

    _loaded = mod
    return mod


def available():
    """本机能不能用达芬奇 API。给 CLI 决定要不要提前劝退用。"""
    try:
        load()
        return True
    except ResolveUnavailable:
        return False


def scriptapp(name="Resolve"):
    """等价于 `DaVinciResolveScript.scriptapp(name)`。"""
    return load().scriptapp(name)


class _Lazy:
    """长得像 DaVinciResolveScript 的代理，属性访问时才真正导入。

    `scriptapp` 单独定义成真方法，这样测试里 `mock.patch.object(send.dvr,
    "scriptapp", ...)` 拿得到原方法、不需要先装达芬奇。
    """

    def scriptapp(self, name="Resolve"):
        return load().scriptapp(name)

    def __getattr__(self, name):
        return getattr(load(), name)

    def __repr__(self):
        return "<DaVinciResolveScript lazy>"


def lazy():
    """给各模块用的模块级代理：`dvr = resolve.lazy()`。"""
    return _Lazy()
