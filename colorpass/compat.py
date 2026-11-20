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

3. **常量的值也会和文档不一样。**
   官方写 `resolve.EXPORT_LUT_33PTCUBE`，但 20.3 的 Python 绑定里根本取不到
   这个属性；传字符串 `"33PTCube"` 会静默返回 False（连文件都不建），
   真正认的是整数枚举 2。`lut_type()` 负责给出正确值，调用点再准备一次
   字符串兜底 —— 见 `pull.run` 的重试。

缺能力不该变成 traceback：要么降级（记进报告、面板上说明少了什么），
要么明确报「这版不支持」。`report()` 就是那张清单。
"""
import re
import sys
import time

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
    ("media_folder", "mediapool", "GetRootFolder",
     "读不到媒体池，素材可能重复导入"),
    ("media_import", "mediapool", "ImportMedia",
     "素材导不进达芬奇，送入做不了"),
    ("empty_timeline", "mediapool", "CreateEmptyTimeline",
     "建不了时间线，送入做不了"),
    ("append", "mediapool", "AppendToTimeline",
     "片段摆不上时间线，送入做不了"),
    ("add_track", "timeline", "AddTrack",
     "轨道不够时摆不下（多轨草稿、复合片段内部轨道）"),
    ("compound", "timeline", "CreateCompoundClip",
     "复合片段只能拆成多轨落下，回剪映后不再是一个整体"),
    ("track_lock", "timeline", "SetTrackLock",
     "锁不住轨道，在达芬奇里误改剪辑没有保护"),
    ("set_start_tc", "timeline", "SetStartTimecode",
     "时间线起点改不了 0，帧号会整体偏移（已自动补偿）"),
    ("timeline_marker", "timeline", "AddMarker",
     "时间线上写不了说明标记"),
    ("timeline_marker_read", "timeline", "GetMarkers",
     "读不到时间线标记（只影响排查）"),
    ("clip_enable", "item", "SetClipEnabled",
     "剪映里停用的段，在达芬奇里会出画面"),
    ("get_clip_enable", "item", "GetClipEnabled",
     "读不到片段启用状态，停用段会照样取色"),
    ("lut_export", "item", "ExportLUT",
     "导不出 LUT，写回拿不到调色"),
    ("item_marker", "item", "AddMarker",
     "片段上写不了段号，写回时归不了位"),
    ("item_marker_read", "item", "GetMarkers",
     "读不到片段上的段号，写回只能靠位置核对"),
    # ---- 可选增强：缺了不影响往返，只是少一种更省事的做法 ----
    ("multicam_create", "mediapool", "CreateMulticamClip",
     "这版不能用脚本创建多机位片段（21.1 起才有）"),
    ("multicam_flatten", "item", "FlattenMulticam",
     "这版不能把多机位片段摊平成单个角度（21.1 起才有）"),
    ("item_type", "item", "GetType",
     "这版读不到片段原生类型（21.1 起才有），按名称/媒体池推断"),
    ("clip_speed", "item", "SetSpeed",
     "这版不能直接设片段速度（21.1 起才有），变速靠预渲染"),
    ("clip_rename", "item", "SetName",
     "这版不能改片段名（20.2 起才有）"),
)

# 缺了只是「少一种更省事的做法」，不影响送入/取回/写回。
OPTIONAL = frozenset((
    "multicam_create", "multicam_flatten", "item_type", "clip_speed",
    "clip_rename", "timeline_marker_read",
))

# 没有这些就走不下去；其余缺了是降级。
FATAL = ("project_mgr", "media_import", "empty_timeline", "append",
         "timeline_list", "lut_export", "item_marker", "item_marker_read")

# 官方脚本 API 增量里与这条链路有关的项。用来解释「这版为什么没有这个能力」，
# 以及生成静态兼容矩阵 —— **不参与运行期判断**（判断永远靠 hasattr）。
# 来源：各版 Developer/Scripting 文档与 21.1 changelog。
ADDED_IN = {
    "clip_rename": (20, 2),
    "multicam_create": (21, 1),
    "multicam_flatten": (21, 1),
    "item_type": (21, 1),
    "clip_speed": (21, 1),
}

# 我们承诺「能用」的四个大版本。只用于生成对照表与报告文字。
# 21 取 21.2（当前稳定版）：21.0 的读写链路也通，但多机位脚本接口要到 21.1。
TARGET_VERSIONS = ((18, 0), (19, 0), (20, 0), (21, 2))


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
      ok           致命能力是否齐全
      state        ok / degraded / broken / unknown
      features     {键: {state, why, fatal, optional}}
      missing      缺的必需能力（键 → 后果），面板/doctor 直接引用
      optional_missing  这版没有的增强能力（不影响往返）
    """
    out = {"version": "", "product": "", "studio": False,
           "features": {}, "missing": {}, "optional_missing": {},
           "state": "unknown", "ok": False}
    try:
        a = app if app is not None else resolve.scriptapp("Resolve")
    except Exception:
        a = None
    if a is None:
        # 一条都没验到，但清单还是要给全 —— 面板要拿它显示「共 N 项」，
        # 空字典会让报告读成「0/0」，看着像没事。
        out["why"] = "达芬奇没开，或脚本接口连不上：能力未验证"
        out["features"] = {k: {"state": "unknown", "why": w,
                               "fatal": k in FATAL,
                               "optional": k in OPTIONAL}
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
        optional = key in OPTIONAL
        if obj is None:
            # 拿不到对象（比如工程里还没有时间线）≠ 这版不支持。
            out["features"][key] = {"state": "unknown", "why": why,
                                    "fatal": fatal, "optional": optional}
            unknown += 1
            continue
        if callable(getattr(obj, method, None)):
            out["features"][key] = {"state": "ok", "why": "", "fatal": fatal,
                                    "optional": optional}
        else:
            out["features"][key] = {"state": "missing", "why": why,
                                    "fatal": fatal, "optional": optional}
            if optional:
                # 18–20 没有多机位脚本接口、20.x 没有 SetSpeed……这些是
                # 「少一种做法」，不是「用不了」。混进 missing 会让面板天天
                # 报缺项，用户就学会忽略它了 —— 那比不报更糟。
                out["optional_missing"][key] = why
            else:
                out["missing"][key] = why

    bad = [k for k, v in out["features"].items()
           if v["state"] == "missing" and not v["optional"]]
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


def lut_type(mod=None, app=None):
    """LUT 导出类型的枚举值。

    官方文档写 `resolve.EXPORT_LUT_33PTCUBE`，但这个常量在 Python 绑定里
    通常**取不到**（20.3 实测 app 与模块上都没有），传字符串 "33PTCube"
    会返回 False、连文件都不建 —— 看起来像「这版不支持导出 LUT」。
    实测 20.3 认的是整数枚举：1=17pt、2=33pt、3=65pt、4=Panasonic VLUT。

    所以：能拿到常量就用常量，拿不到用 2（33 点）。调用方还应准备一次
    字符串兜底（极老版本可能只认字面量），参考 `pull.run` 的重试。
    """
    for holder in (app, mod):
        if holder is None:
            continue
        for name in ("EXPORT_LUT_33PTCUBE", "EXPORT_LUT_33_POINT_CUBE"):
            val = getattr(holder, name, None)
            if val:
                return val
    return 2


def import_media(mp, paths):
    """把素材导进媒体池，返回 (items, 形态)。

    21.1 起官方把「路径字符串数组」标成 deprecated，推荐
    `[{"FilePath": ...}]`；可 18–20 只认前者。所以先按全版本都认的写法调，
    空了再试新形态 —— 现在的 21.1/21.2 两种都收，将来若真移除旧形态，
    这里会自动走新的。
    """
    paths = [str(p) for p in paths if p]
    if not paths:
        return [], "none"
    try:
        got = mp.ImportMedia(paths)
    except TypeError:
        got = None
    except Exception:
        got = None
    if got:
        return list(got), "paths"
    try:
        got = mp.ImportMedia([{"FilePath": p} for p in paths])
    except Exception:
        got = None
    return (list(got) if got else []), "clipinfo"


# ---------------------------------------------------------------- 片段种类
#
# 达芬奇没有 `IsMulticamClip()` 这种直白的方法：多机位在时间线上仍是一个
# TimelineItem，21.1 的 GetType() 也只返回 video/audio/generator/transition。
# 所以只能多信号推断。判断只用于**报告和放宽匹配**，不用于跳过检查：
# 认不出来时仍按普通片段走严格核对，宁严不松。

_ANGLE = re.compile(r"\s-\sAngle\s+\d+\s*$", re.I)


def _media_type(item):
    """媒体池片段的 Type 属性（多机位在 21.1 里是 'Multicam'）。"""
    try:
        mi = item.GetMediaPoolItem()
    except Exception:
        return ""
    if mi is None:
        return ""
    for key in ("Type", "Clip Type"):
        try:
            v = mi.GetClipProperty(key)
        except Exception:
            continue
        if v:
            return str(v)
    return ""


def is_multicam(item):
    """(是不是多机位, 证据)。证据给报告用，用户能看懂「凭什么这么判」。"""
    if item is None:
        return False, ""
    mt = _media_type(item)
    if "multicam" in mt.lower():
        return True, "媒体池类型=%s" % mt
    try:
        if callable(getattr(item, "GetType", None)):
            t = (item.GetType() or "").lower()
            if "multicam" in t:
                return True, "片段类型=%s" % t
    except Exception:
        pass
    try:
        name = item.GetName() or ""
    except Exception:
        name = ""
    if _ANGLE.search(name):
        return True, "名称为多机位角度（%s）" % name[-12:]
    return False, ""


def item_kind(item):
    """给报告用的片段画像；任何一项取不到都不抛。"""
    mc, why = is_multicam(item)
    out = {"multicam": mc, "why": why, "type": "", "media_type": _media_type(item)}
    try:
        if callable(getattr(item, "GetType", None)):
            out["type"] = item.GetType() or ""
    except Exception:
        pass
    return out


def has(feature, app=None):
    """单个能力是否可用（True/False/None=没验到）。"""
    rep = report(app)
    v = (rep.get("features") or {}).get(feature) or {}
    state = v.get("state")
    return {"ok": True, "missing": False}.get(state)


# ---------------------------------------------------------------- 版本矩阵
#
# 这是一张**文档表**，不是运行期判断：说明「按官方 API 增量，这版应该有什么」。
# 真机上永远以 report() 的 hasattr 结果为准 —— 同一大版本内部也会增删方法。
# 值：True=有，False=没有，None=这版没有这个方法但链路有别的做法。

def matrix():
    """四版 × 关键能力的静态对照表。"""
    rows = []
    for major, minor in TARGET_VERSIONS:
        ver = (major, minor)
        cells = {}
        for key, _o, _m, _w in FEATURES:
            if key in OPTIONAL:
                added = ADDED_IN.get(key)
                cells[key] = True if not added else ver >= added
            else:
                # 链路必需项：18.0 起就在（18.0 是这条链路的最低承诺）。
                cells[key] = True
        rows.append({"version": "%d" % major, "cells": cells})
    return rows


def matrix_text():
    """人看的四版对照表（终端宽度内，不画大表格）。"""
    lines = ["版本  必需能力   多机位  真变速  片段类型"]
    for row in matrix():
        c = row["cells"]
        def mark(k, yes="✓"):
            return yes if c.get(k) else "—"
        lines.append("%-5s %-9s %-6s %-6s %s" % (
            row["version"], mark("append"), mark("multicam_create", "21.1+"),
            mark("clip_speed", "21.1+"), mark("item_type", "21.1+")))
    lines.append("（必需能力 = 送入/取回/写回用到的 API；18.0 起均提供。"
                 "多机位创建与真变速是 21.1 新增，缺了有降级路径。）")
    return "\n".join(lines)


def ensure_page(page="color", app=None, wait=0.0):
    """切到达芬奇某个页面。返回是否成功（失败不算错误，只是慢一点）。

    这里曾经写的是 `resolve.OpenPage("color")` —— `resolve` 是包内模块，
    根本没有这个函数，于是每次调用都 AttributeError 然后被 `except: pass`
    吞掉。也就是说「送入后自动切到调色页」从来没生效过。OpenPage 在 Resolve
    对象上，不在脚本模块上。

    wait>0 时切换后还会轮询到页面真的变成目标页为止。调色页首次打开要几秒
    （实测 20.3 新工程约 2–4 秒），`OpenPage` 返回 True 只代表请求被接受；
    紧接着调 ExportLUT 会得到 False，看起来像「这版不支持导出」。
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

    def _cur():
        try:
            return (get_cur() or "").lower() if callable(get_cur) else ""
        except Exception:
            return ""

    if _cur() == page:
        return True
    open_page = getattr(r, "OpenPage", None)
    if not callable(open_page):
        return False
    try:
        ok = bool(open_page(page))
    except Exception:
        return False
    if ok and wait > 0:
        deadline = time.time() + wait
        while time.time() < deadline:
            if _cur() == page:
                return True
            time.sleep(0.25)
        return False
    return ok


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
    om = rep.get("optional_missing") or {}
    if om:
        rows.append("增强     %d 项这版没有（不影响往返）：%s"
                    % (len(om), "、".join(_short(v) for v in om.values())))
    rows.append("版本承诺 达芬奇 18 / 19 / 20 / 21（链路必需 API 18.0 起齐全）")
    for k, v in (extra or {}).items():
        rows.append("%-8s %s" % (k, v))
    return "\n".join(rows)


def _arch():
    try:
        import platform
        return platform.machine() or ""
    except Exception:
        return ""
