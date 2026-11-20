"""达芬奇版本差异，集中在一处。

三条原则，都是被真实故障推出来的：

1. **不按版本号判断，只探测能力。**
   `GetVersionString()` 只说明它自称是谁；同一大版本内部也会增删方法。
   能不能干这件事，用 `hasattr` 问。

2. **方法签名会变长。**
   `TimelineItem.AddMarker` 从 5 个参数涨到 6 个（多一个 customData），
   `MediaPool.AppendToTimeline` 的 `recordFrame`、`Project.ExportLUT` 的导出类型
   也都是后加的。照新版写，老版本一调就 `TypeError`。统一用 `call_shrink`
   从尾部丢参数重试，调用点不必再各自写两层 try。

3. **常量会缺。**
   LUT 导出类型在脚本模块里叫 `EXPORT_LUT_33PTCUBE`，但有的版本只能写字面量
   `"33PTCube"`。`lut_type()` 兜这两个名字。

缺能力不该变成 traceback：要么降级（记进报告、面板上说明少了什么），
要么明确报「这版不支持」。`report()` 就是那张清单。
"""
import re
import sys

from . import resolve

# ---------------------------------------------------------------- 版本号
#
# 只用来显示和排查，**不用来做判断** —— 见模块开头第 1 条。
# 现实里的写法不止一种："20.3.1.0007"、"18.6.6"、"19.0.0b.25"、
# "DaVinci Resolve Studio 18.1.4"。取前三个数字，够排序也够展示。

_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text):
    """"18.6.6" → (18, 6, 6)；认不出返回 ()。"""
    m = _VER.search(text or "")
    if not m:
        return ()
    return tuple(int(g or 0) for g in m.groups())


def version_text(app=None):
    try:
        r = app if app is not None else resolve.scriptapp("Resolve")
        return (r.GetVersionString() or "") if r else ""
    except Exception:
        return ""


def product_text(app=None):
    try:
        r = app if app is not None else resolve.scriptapp("Resolve")
        return (r.GetProductName() or "") if r else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 能力表
#
# (键, 对象, 方法, 缺了会怎样)
#
# 「缺了会怎样」直接写给用户看，所以写的是后果，不是方法名。面板和 doctor
# 都原样引这句话 —— 用户不该看到 `SetClipEnabled` 这种词。
FEATURES = (
    ("page_nav", "app", "GetCurrentPage",
     "切不到调色页：LUT 照样能导，只是慢一些"),
    ("project_mgr", "app", "GetProjectManager",
     "拿不到工程管理器，送入和写回都做不了"),
    ("timeline_list", "project", "GetTimelineByIndex",
     "读不到工程里的时间线清单"),
    ("delete_timeline", "project", "DeleteTimelines",
     "同名空时间线删不掉，会换个名字建"),
    ("media_import", "mediapool", "ImportMedia",
     "素材导不进达芬奇，送入做不了"),
    ("empty_timeline", "mediapool", "CreateEmptyTimeline",
     "建不了时间线，送入做不了"),
    ("append", "mediapool", "AppendToTimeline",
     "片段摆不上时间线，送入做不了"),
    ("compound", "timeline", "CreateCompoundClip",
     "复合片段只能拆成多轨落下，回剪映后不再是一个整体"),
    ("track_lock", "timeline", "SetTrackLock",
     "锁不住轨道，在达芬奇里误改剪辑没有保护"),
    ("set_start_tc", "timeline", "SetStartTimecode",
     "时间线起点改不了 0，帧号会整体偏移（已自动补偿）"),
    ("timeline_marker", "timeline", "AddMarker",
     "时间线上写不了说明标记"),
    ("clip_enable", "item", "SetClipEnabled",
     "剪映里停用的段，在达芬奇里会出画面"),
    ("lut_export", "item", "ExportLUT",
     "导不出 LUT，写回拿不到调色"),
    ("item_marker", "item", "AddMarker",
     "片段上写不了段号，写回时归不了位"),
)

# 没有这些就走不下去；其余缺了是降级。
FATAL = ("project_mgr", "media_import", "empty_timeline", "append",
         "timeline_list", "lut_export", "item_marker")


def _obj(name, app=None, proj=None, tl=None, item=None):
    """按名字取一个 API 对象；取不到就 None（=「没验到」，不等于「不支持」）。"""
    try:
        if name == "app":
            return app if app is not None else resolve.scriptapp("Resolve")
        if name == "project":
            if proj is None:
                a = _obj("app", app)
                pm = a.GetProjectManager() if a else None
                proj = pm.GetCurrentProject() if pm else None
            return proj
        if name == "mediapool":
            p = _obj("project", app, proj, tl)
            return p.GetMediaPool() if p else None
        if name == "timeline":
            p = _obj("project", app, proj, tl)
            return tl if tl is not None else (p.GetCurrentTimeline() if p else None)
        if name == "item":
            t = _obj("timeline", app, proj, tl)
            if item is not None:
                return item
            if not t:
                return None
            lanes = t.GetItemListInTrack("video", 1) or []
            return lanes[0] if lanes else None
    except Exception:
        return None
    return None


def report(app=None):
    """本机的能力清单。没有达芬奇也能调 —— 全部记成 unknown。

    返回：
      ok        致命能力是否齐全
      state     ok / degraded / unknown
      features  {键: {state, why, fatal}}
      missing   缺的能力（键 → 后果），面板/dcotor 直接引用
    """
    out = {"version": "", "product": "", "studio": False,
           "features": {}, "missing": {}, "state": "unknown", "ok": False}
    try:
        a = app if app is not None else resolve.scriptapp("Resolve")
    except Exception:
        a = None
    if a is None:
        # 一条都没验到，但清单还是要给全 —— 面板要拿它显示「共 14 项」，
        # 空字典会让报告读成「0/0」，看着像没事。
        out["why"] = "达芬奇没开，或脚本接口连不上：能力未验证"
        out["features"] = {k: {"state": "unknown", "why": w, "fatal": k in FATAL}
                           for k, _o, _m, w in FEATURES}
        return out

    out["version"] = version_text(a)
    out["product"] = product_text(a)
    out["studio"] = "Studio" in (out["product"] or "")

    cache = {}
    unknown = 0
    for key, obj_name, method, why in FEATURES:
        if obj_name not in cache:
            cache[obj_name] = _obj(obj_name, app=a)
        obj = cache[obj_name]
        fatal = key in FATAL
        if obj is None:
            # 拿不到对象（比如工程里还没有时间线）≠ 这版不支持。
            out["features"][key] = {"state": "unknown", "why": why,
                                    "fatal": fatal}
            unknown += 1
            continue
        if callable(getattr(obj, method, None)):
            out["features"][key] = {"state": "ok", "why": "", "fatal": fatal}
        else:
            out["features"][key] = {"state": "missing", "why": why,
                                    "fatal": fatal}
            out["missing"][key] = why

    bad = [k for k, v in out["features"].items() if v["state"] == "missing"]
    out["ok"] = not any(out["features"][k]["fatal"] for k in bad)
    if bad:
        out["state"] = "degraded" if out["ok"] else "broken"
    elif unknown and unknown == len(out["features"]):
        out["state"] = "unknown"
    else:
        out["state"] = "ok"
    return out


def line(rep=None):
    """一行话，给面板和命令行用。"""
    rep = rep if rep is not None else report()
    ver = rep.get("version") or "版本未知"
    if rep.get("state") == "unknown":
        return "达芬奇 %s · 能力未验证（工程/接口没连上）" % ver
    if rep.get("state") == "ok":
        return "达芬奇 %s · 接口齐全" % ver
    missing = rep.get("missing") or {}
    head = "、".join(_short(v) for v in list(missing.values())[:2])
    more = "" if len(missing) <= 2 else " 等 %d 项" % len(missing)
    return "达芬奇 %s · 缺 %s%s" % (ver, head, more)


def _short(why):
    """取后果那句的前半截，面板只有一行位置。"""
    return (why or "").split("，")[0].split("：")[0][:24]


# ---------------------------------------------------------------- 签名兜底


def call_shrink(fn, *args, **kwargs):
    """老版本方法签名短，多给一个参数会 TypeError：从尾部一个个丢，直到能调。

    为什么不做「先查签名」：这些对象是跨进程代理，inspect 拿不到真签名。
    只能真的调一次看它认不认 —— 这也正是老版本的真实形态。

    **只在「末尾参数是可选的」时用。** 如果最后一个参数携带的是必需信息
    （比如片段标记里的段号），丢掉它换来的是一次「成功但结果不对」的调用，
    比直接失败更糟。那种情况请显式分层调用，参考 `send.mark_item`。
    """
    last = None
    for n in range(len(args), -1, -1):
        try:
            return fn(*args[:n], **kwargs)
        except TypeError as exc:
            last = exc
    raise last


def lut_type(mod=None):
    """LUT 导出类型的常量值。

    新版脚本模块给 `EXPORT_LUT_33PTCUBE`，老版本没有这个属性，但同一个 API
    认字面量 "33PTCube"。两个都试，别让 `AttributeError` 冒到用户面前。
    """
    holder = mod
    if holder is None:
        try:
            holder = resolve.load()
        except resolve.ResolveUnavailable:
            holder = None
    for name in ("EXPORT_LUT_33PTCUBE", "EXPORT_LUT_33_POINT_CUBE"):
        val = getattr(holder, name, None) if holder is not None else None
        if val:
            return val
    return "33PTCube"


def ensure_page(page="color", app=None):
    """切到达芬奇某个页面。返回是否成功（失败不算错误，只是慢一点）。

    这里曾经写的是 `resolve.OpenPage("color")` —— `resolve` 是包内模块，
    根本没有这个函数，于是每次调用都 AttributeError 然后被 `except: pass`
    吞掉。也就是说「送入后自动切到调色页」从来没生效过。OpenPage 在 Resolve
    对象上，不在脚本模块上。
    """
    r = app
    if r is None:
        try:
            r = resolve.scriptapp("Resolve")
        except Exception:
            return False
    if r is None:
        return False
    get_cur = getattr(r, "GetCurrentPage", None)
    if callable(get_cur):
        try:
            if (get_cur() or "").lower() == page:
                return True
        except Exception:
            pass
    open_page = getattr(r, "OpenPage", None)
    if not callable(open_page):
        return False
    try:
        return bool(open_page(page))
    except Exception:
        return False


# ---------------------------------------------------------------- 文本报告
#
# 给测试同学看的。他只要把这一段整块贴回来，就知道是哪一版、缺什么。

def text_report(app=None, extra=None):
    """多行纯文本自检报告（面板「诊断」按钮和 `colorpass compat` 都用它）。"""
    from . import __version__, paths

    rep = report(app)
    ver = rep.get("version") or "未连上"
    prod = rep.get("product") or "—"
    ok = sum(1 for f in rep["features"].values() if f["state"] == "ok")
    tot = len(rep["features"])
    rows = [
        "ColorPass %s 兼容性自检" % __version__,
        "平台     %s %s · Python %s%s"
        % (sys.platform, _arch(), sys.version.split()[0],
           " · 打包版" if getattr(sys, "frozen", False) else " · 源码"),
        "达芬奇   %s（%s）" % (ver, prod),
        "脚本模块 %s" % (resolve.module_path() or "没找到"),
        "数据目录 %s" % paths.BASE,
        "能力     %d/%d 可验证通过" % (ok, tot),
    ]
    if rep.get("state") == "unknown":
        rows.append("结论     %s" % (rep.get("why") or "未验证"))
    elif rep.get("missing"):
        rows.append("缺项     " + "；".join(
            "%s（%s）" % (k, v) for k, v in rep["missing"].items()))
        rows.append("结论     " + ("会影响功能，见上面缺项" if not rep["ok"]
                                   else "可降级使用，见上面缺项"))
    else:
        rows.append("结论     接口齐全，无需处理")
    for k, v in (extra or {}).items():
        rows.append("%-8s %s" % (k, v))
    return "\n".join(rows)


def _arch():
    try:
        import platform
        return platform.machine() or ""
    except Exception:
        return ""
