"""达芬奇脚本 API 的按需加载。

**不要在模块顶层 `import DaVinciResolveScript`。** 那个模块只存在于装了达芬奇
的机器上，放在顶层会让 `import colorpass.send` 在没有达芬奇的机器上直接炸——
CI、只想跑 `check` / `plan` 的人、单纯 `pip install` 看一眼的人，全都会被挡在门外。

这里改成延迟导入：模块级只拿一个代理对象，真正访问 `dvr.scriptapp(...)` 时才去找
达芬奇，找不到就抛 `ResolveUnavailable`，并说清试过哪些目录。
"""

import os
import sys

# 脚本模块的搜索位置。达芬奇三个平台的目录各有一套，用户级安装和系统级安装
# 也不一样，所以按「可能性从高到低」排一遍；都不存在就等于没装。
# 需要指定一份特定的安装（比如同机并存两个版本、逐个验）时用
# COLORPASS_SCRIPT_MODULES 指过去，不要再往这里加死路径。
MOD_DIRS = tuple(d for d in (
    os.environ.get("COLORPASS_SCRIPT_MODULES"),
    # Windows：系统级安装，路径与版本无关，升级后仍是这一处
    r"C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules",
    # macOS：系统级 / 用户级
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
    "/Developer/Scripting/Modules",
    os.path.expanduser("~/Library/Application Support/Blackmagic Design"
                       "/DaVinci Resolve/Developer/Scripting/Modules"),
    # Linux：官方包 + Fusion 自带的一份
    "/opt/resolve/Developer/Scripting/Modules",
    "/opt/resolve/libs/Fusion/Modules",
    os.path.expanduser("~/.local/share/DaVinciResolve/Developer/Scripting/Modules"),
) if d)

# 用到的对象方法。写在一处的原因：想确认「某个版本到底支持到哪」时，
# 直接照着这张表逐项 hasattr 就行，不用满仓库 grep（doctor 就是这么做的）。
API_GROUPS = {
    "app": ("GetVersionString", "GetProductName", "GetProjectManager",
            "GetCurrentPage", "OpenPage"),
    "project_manager": ("GetCurrentProject", "CreateProject", "GetProjectListInCurrentFolder"),
    "project": ("GetName", "GetSetting", "SetSetting", "GetTimelineCount",
                "GetTimelineByIndex", "GetCurrentTimeline", "SetCurrentTimeline",
                "GetMediaPool", "DeleteTimelines"),
    "mediapool": ("GetRootFolder", "GetClipList", "ImportMedia",
                  "CreateEmptyTimeline", "CreateCompoundClip"),
    "timeline": ("GetName", "GetStartFrame", "GetEndFrame", "GetTrackCount",
                 "GetItemListInTrack", "AppendToTimeline", "AddTrack",
                 "SetTrackLock", "GetIsTrackLocked", "AddMarker", "GetMarkers",
                 "DeleteMarkerAtFrame", "SetStartTimecode", "GetSetting"),
    "item": ("GetName", "GetStart", "GetEnd", "GetDuration", "GetSourceStartFrame",
             "GetMediaPoolItem", "GetClipProperty", "ExportLUT",
             "GetClipEnabled", "SetClipEnabled", "AddMarker", "GetMarkers",
             "GetFusionCompCount", "GetFusionCompByIndex", "AddFusionComp"),
}

_loaded = None
_loaded_path = None


class ResolveUnavailable(RuntimeError):
    """本机找不到 DaVinciResolveScript —— 多半没装达芬奇，或脚本模块被挪走。"""


def load():
    """返回 DaVinciResolveScript 模块（成功一次就记住）。"""
    global _loaded, _loaded_path
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
            "只有「送入 / 取色 / 写回 / 锁定」需要它；check、plan、gc、doctor 不需要。"
        ) from exc

    _loaded = mod
    _loaded_path = getattr(mod, "__file__", None)
    return mod


def module_path():
    """实际加载到的模块文件；没加载过就返回第一个存在的候选目录。"""
    if _loaded_path:
        return _loaded_path
    for d in MOD_DIRS:
        if os.path.isdir(d):
            return d
    return None


def existing_dirs():
    """本机存在的候选目录（doctor 用：一眼看出它会去哪些地方找）。"""
    return [d for d in MOD_DIRS if d and os.path.isdir(d)]


def surface(obj, group):
    """这个对象身上有哪些方法 —— 缺哪几个，就是那个版本不支持哪几个。

    版本号只用来展示，不用来做判断：达芬奇的 API 是增量加的，
    同一个大版本里也可能有增删，`hasattr` 比 `GetVersionString()` 可靠。
    对象为空时返回 None，表示「没验到」，不要当成「全都不支持」。
    """
    if obj is None:
        return None
    return {name: callable(getattr(obj, name, None)) for name in API_GROUPS.get(group, ())}


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
