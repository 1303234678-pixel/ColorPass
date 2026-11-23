"""达芬奇 21.2 专用：接口探测、能力自检、调用兜底。

**只支持 21.2。** 18/19/20 的分支已经删干净了 —— 那些版本缺多机位脚本接口、
缺真变速、片段类型也读不到，留着只会让每条路径都多一层永远走不到的分支，
还会把「这版不支持」和「这台机器装坏了」混成同一个提示。

但这个模块仍然不是空的，因为 21.2 自己也有几个坑：

1. **`hasattr` 在 21.2 上恒为真。**
   对不存在的方法也返回 True，`getattr` 拿到的是 None，一调就
   `TypeError: 'NoneType' object is not callable`。凡是用 hasattr 做分支的
   地方都会走进死路。唯一可靠的问法是「取出来，看它能不能调」——`alive()`。

2. **常量的值不一定和文档一样。**
   官方写 `resolve.EXPORT_LUT_33PTCUBE`，但 21.2 的 Python 绑定里取不到；
   传字符串 `"33PTCube"` 会静默返回 False（连文件都不建），真正认的是整数
   枚举 2（33 点）。`lut_type()` 负责给出正确值。

3. **「调用没报错」不等于「真的做了」。**
   `ImportMedia([{"FilePath": ...}])` 在 21.2.0.6 上返回 None、一个片段都不
   导入，却也不抛异常；`AppendToTimeline` 摆不上时返回 `[None]` 而不是空
   列表。所以这里一律**看效果**：导入前后数媒体池、append 完看列表里是不是
   真的对象（`import_media()` / `real_items()`）。

4. **导出 LUT 要调色页真的就绪。**
   `OpenPage("color")` 返回 True 只代表请求被接受；页面还没切过去时
   `ExportLUT` 直接返回 False，看起来像「这版不支持导出」。`ensure_page()`
   带 wait 参数就是为这个。

缺能力不该变成 traceback：要么降级（记进报告、面板上说明少了什么），
要么明确报「这版不支持」。`report()` 就是那张清单。
"""
import os
import re
import sys
import time

from . import resolve

# ---------------------------------------------------------------- 版本
#
# 只用来显示、以及在版本不对时把话说清楚 —— **不用来做能力判断**。
# 能力永远靠 alive() 探测：同一大版本内部也可能增删方法。

REQUIRED = (21, 2)
REQUIRED_TEXT = "达芬奇 21.2"

_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text):
    """"21.2.0.6" → (21, 2, 0)；认不出返回 ()。"""
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


def version_ok(app=None):
    """(这版够不够, 一句人话)。连不上时返回 (None, 说明)。"""
    txt = version_text(app)
    if not txt:
        return None, "连不上达芬奇，版本未验证"
    got = parse_version(txt)
    if not got:
        return None, "认不出达芬奇版本号：%s" % txt
    if got[:2] < REQUIRED:
        return False, ("本工具只支持 %s，这台是 %s。"
                       "旧版本的多机位/变速接口缺失，请升级到达芬奇 21.2。"
                       % (REQUIRED_TEXT, txt))
    return True, "达芬奇 %s" % txt


# ---------------------------------------------------------------- 能力表
#
# (键, 对象, 方法, 缺了会怎样)
#
# 「缺了会怎样」直接写给用户看，所以写的是后果，不是方法名。

FEATURES = (
    ("page_nav", "app", "GetCurrentPage",
     "切不到调色页：LUT 照样能导，只是慢一些"),
    ("project_mgr", "app", "GetProjectManager",
     "拿不到工程管理器，送入和写回都做不了"),
    ("timeline_list", "project", "GetTimelineByIndex",
     "读不到工程里的时间线清单"),
    ("media_folder", "mediapool", "GetRootFolder",
     "读不到媒体池，素材可能重复导入"),
    ("media_import", "mediapool", "ImportMedia",
     "素材导不进达芬奇，送入做不了"),
    ("empty_timeline", "mediapool", "CreateEmptyTimeline",
     "建不了时间线，送入做不了"),
    ("append", "mediapool", "AppendToTimeline",
     "片段摆不上时间线，送入做不了"),
    ("delete_timeline", "mediapool", "DeleteTimelines",
     "同名空时间线删不掉，会换个名字建"),
    ("multicam_create", "mediapool", "CreateMulticamClip",
     "不能用脚本创建多机位片段（复合片段这条路仍可用）"),
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
    ("item_type", "item", "GetType",
     "读不到片段原生类型，多机位只能按名称/媒体池推断"),
    ("clip_speed", "item", "SetSpeed",
     "不能直接设片段速度，变速靠预渲染"),
    ("clip_rename", "item", "SetName",
     "不能改片段名，段号只写在标记里"),
)

# 没有这些就走不下去；其余缺了只是「少一种做法」。
FATAL = ("project_mgr", "media_import", "empty_timeline", "append",
         "timeline_list", "lut_export", "item_marker", "item_marker_read")


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
      version    达芬奇版本字符串
      version_ok 这版够不够（True/False/None=没验到）
      features   {键: {state: ok/missing/unknown, why, fatal}}
      missing    缺的能力（键 → 后果），面板/doctor 直接引用
      state      ok / degraded / broken / unknown
      ok         致命能力是否齐全
    """
    out = {"version": "", "product": "", "studio": False,
           "version_ok": None, "version_note": "",
           "features": {}, "missing": {}, "state": "unknown", "ok": False}
    try:
        a = app if app is not None else resolve.scriptapp("Resolve")
    except Exception:
        a = None
    if a is None:
        # 一条都没验到，但清单还是要给全 —— 面板要拿它显示「共 N 项」，
        # 空字典会让报告读成「0/0」，看着像没事。
        out["why"] = "达芬奇没开，或脚本接口连不上：能力未验证"
        out["features"] = {k: {"state": "unknown", "why": w, "fatal": k in FATAL}
                           for k, _o, _m, w in FEATURES}
        return out

    out["version"] = version_text(a)
    out["product"] = product_text(a)
    out["studio"] = "Studio" in (out["product"] or "")
    ok, why = version_ok(a)
    out["version_ok"], out["version_note"] = ok, why

    cache = {}
    unknown = 0
    for key, obj_name, method, why in FEATURES:
        if obj_name not in cache:
            cache[obj_name] = _obj(obj_name, app=a)
        obj = cache[obj_name]
        fatal = key in FATAL
        if obj is None:
            # 拿不到对象（比如工程里还没有时间线）≠ 这版不支持。
            out["features"][key] = {"state": "unknown", "why": why, "fatal": fatal}
            unknown += 1
            continue
        if alive(obj, method):
            out["features"][key] = {"state": "ok", "why": "", "fatal": fatal}
        else:
            out["features"][key] = {"state": "missing", "why": why, "fatal": fatal}
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
    if rep.get("version_ok") is False:
        return rep.get("version_note") or ("需要 %s" % REQUIRED_TEXT)
    if rep.get("state") == "ok":
        return "达芬奇 %s · 接口齐全" % ver
    missing = rep.get("missing") or {}
    head = "、".join(_short(v) for v in list(missing.values())[:2])
    more = "" if len(missing) <= 2 else " 等 %d 项" % len(missing)
    return "达芬奇 %s · 缺 %s%s" % (ver, head, more)


def _short(why):
    """取后果那句的前半截，面板只有一行位置。"""
    return (why or "").split("，")[0].split("：")[0][:24]


# ---------------------------------------------------------------- 调用兜底


def alive(obj, name):
    """对象上这个方法能不能调。**不要用 hasattr。**

    21.2 的绑定把没实现的方法也「给」了出来：`hasattr` 返回 True，
    `getattr` 却是 None，一调就 `TypeError: 'NoneType' object is not
    callable`。唯一可靠的问法是「取出来，看它能不能调」。
    """
    if obj is None:
        return False
    try:
        return callable(getattr(obj, name, None))
    except Exception:
        return False


def call_if(obj, name, *args):
    """有这个方法就调，没有就返回 None（不算错误）。"""
    fn = getattr(obj, name, None) if obj is not None else None
    if not callable(fn):
        return None
    return fn(*args)


def lut_type(app=None):
    """LUT 导出类型的枚举值：21.2 认整数 2（33 点 cube）。

    官方文档写 `resolve.EXPORT_LUT_33PTCUBE`，但这个常量在 Python 绑定里
    取不到；传字符串 "33PTCube" 会返回 False、连文件都不建 —— 看起来像
    「这版不支持导出 LUT」。实测 21.2 认的是 1=17pt、2=33pt、3=65pt、
    4=Panasonic VLUT。拿到常量就用常量，拿不到用 2。
    """
    holder = app if app is not None else resolve.scriptapp("Resolve")
    for name in ("EXPORT_LUT_33PTCUBE", "EXPORT_LUT_33_POINT_CUBE"):
        val = getattr(holder, name, None) if holder is not None else None
        if val:
            return val
    return 2


class Imported(list):
    """导入结果：本身是个列表（可以直接 for / len），额外带两件事。

    * `mode`  哪种入参形态**真的**把素材导进来了
    * `tried` 依次试过哪些形态（报告里写出来，下台机器就不用再猜）

    做成 list 子类是为了不改调用方的写法：`for it in import_media(...)` 照旧。
    """

    def __init__(self, items=(), mode="", tried=()):
        super().__init__(items)
        self.mode = mode
        self.tried = list(tried)


def pool_clips(mp):
    """媒体池里能看到的片段：`[(规范化路径, 片段)]`，按路径去重。

    同时看根目录和当前目录：`ImportMedia` 导进的是**当前目录**，只看根目录
    会在「用户把素材分了文件夹」的工程里误判成「一个都没导进来」。
    没有 File Path 的条目（时间线本身、多机位片段等）路径给空串。
    """
    out, seen = [], set()
    for getter in ("GetRootFolder", "GetCurrentFolder"):
        try:
            folder = getattr(mp, getter, None)
            folder = folder() if callable(folder) else None
            clips = (folder.GetClipList() or []) if folder is not None else []
        except Exception:
            continue
        for c in clips:
            try:
                p = c.GetClipProperty("File Path") or ""
            except Exception:
                p = ""
            key = os.path.normcase(os.path.normpath(p)) if p else ""
            dedup = key or id(c)
            if dedup in seen:
                continue
            seen.add(dedup)
            out.append((key, c))
    return out


def import_media(mp, paths):
    """把素材导进媒体池，返回**真的进来了**的那些片段。

    **21.2.0.6 的坑：官方推荐的 `[{"FilePath": p}]` 是静默空操作。**
    它不抛异常、不返回 None 以外的任何东西，但一个片段也不导入 —— 只看
    「调用没报错」会以为成功了，接着每一段都摆不上，最后报成「素材不在媒体
    池」，看起来像素材路径错了。实测这台机器上只有**裸路径**（字符串列表或
    单个字符串）真的导入。

    所以这里不认返回值，认**媒体池里多了什么**：每换一种形态就重数一次，
    谁真的导进来了就用谁。返回的 `Imported` 上带 `.mode` / `.tried`，
    报告里写清楚，省得下次又从头猜。
    """
    paths = [str(p) for p in paths if p]
    if not paths:
        return Imported()

    want = {os.path.normcase(os.path.normpath(p)): p for p in paths}
    have = {k for k, _c in pool_clips(mp)}
    missing = [p for k, p in want.items() if k not in have]
    if not missing:
        return Imported(mode="already", tried=["已在池中"])

    forms = (
        # 官方文档推荐的形态（别的机器/以后的版本可能要它）
        ("clipinfo", lambda ps: [{"FilePath": p} for p in ps]),
        # 实测 21.2.0.6 真正认的形态
        ("paths", lambda ps: list(ps)),
    )
    tried = []
    for name, make in forms:
        try:
            mp.ImportMedia(make(missing))
        except Exception as exc:
            tried.append("%s 抛 %s" % (name, type(exc).__name__))
            continue
        got = [c for k, c in pool_clips(mp)
               if k in want and k not in have]
        tried.append("%s 导入 %d/%d" % (name, len(got), len(missing)))
        if got:
            return Imported(got, mode=name, tried=tried)

    # 整批不收就逐个来：坏一个不该拖垮其余十三个。
    got = []
    for p in missing:
        try:
            mp.ImportMedia(p)
        except Exception:
            continue
        new = [c for k, c in pool_clips(mp) if k in want and k not in have]
        if new:
            got += [c for c in new if c not in got]
    tried.append("逐个导入 %d/%d" % (len(got), len(missing)))
    return Imported(got, mode="one-by-one" if got else "", tried=tried)


def delete_timelines(mp, timelines):
    """删时间线。21.2 的 `DeleteTimelines` 在 MediaPool 上，不在 Project 上。"""
    tl = list(timelines or [])
    if not tl:
        return True
    if not alive(mp, "DeleteTimelines"):
        return False
    try:
        return bool(mp.DeleteTimelines(tl))
    except Exception:
        return False


def real_items(got):
    """把 `AppendToTimeline` 的返回值收成「真的摆上去的片段」。

    21.2 摆不上时返回的是 **`[None]`** —— 长度是 1，`if not got` 判不出来，
    接着 `got[0].GetStart()` 直接 `AttributeError: 'NoneType'`。所以每次都要
    过这一层，别直接信列表长度。
    """
    return [x for x in (got or []) if x is not None]


def project_id(proj):
    """工程唯一 id（取不到返回空串）。

    用它而不是工程名来认「还是不是同一个工程」：用户完全可以另存一份同名
    工程，或者在别的库里建同名工程 —— 名字对得上、内容却不是那份，取色就会
    从一条看起来一样的时间线上读出一个完全无关的调色。
    """
    try:
        return proj.GetUniqueId() or ""
    except Exception:
        return ""


def rendering(proj):
    """达芬奇是不是正在渲染交付。

    渲染时 ExportLUT 经常直接返回 False（引擎在解码占用），看起来像「这版
    不能导 LUT」。与其让用户对着一个假失败排查，不如提前问一句。
    取不到当 False：不能因为读不到状态就拒绝干活。
    """
    try:
        return bool(proj.IsRenderingInProgress())
    except Exception:
        return False
# ---------------------------------------------------------------- 片段种类
#
# 达芬奇没有 `IsMulticamClip()` 这种直白的方法：多机位在时间线上仍是一个
# TimelineItem。所以只能多信号推断。判断只用于**报告和放宽匹配**，不用于
# 跳过检查：认不出来时仍按普通片段走严格核对，宁严不松。

_ANGLE = re.compile(r"\s-\sAngle\s+\d+\s*$", re.I)

# 媒体池的 Type 是**本地化**的：中文界面给的是「多机位」，不是 "Multicam"。
# 只认英文会在中文机器上判错，而判错的代价是退回严格核对 —— 多机位就写不
# 回去了。各语言都收一遍，宁可多认一个词。
_MC_WORDS = ("multicam", "multi cam", "多机位", "マルチカム", "멀티캠",
             "multicámara", "multicaméra")


def _looks_multicam(text):
    low = (text or "").lower()
    return any(w in low for w in _MC_WORDS)


def _media_type(item):
    """媒体池片段的 Type 属性（中文界面是「多机位」）。"""
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
    if _looks_multicam(mt):
        return True, "媒体池类型=%s" % mt
    if alive(item, "GetType"):
        try:
            t = (item.GetType() or "").lower()
            if _looks_multicam(t):
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
    if alive(item, "GetType"):
        try:
            out["type"] = item.GetType() or ""
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 页面切换


def ensure_page(page="color", app=None, wait=0.0):
    """切到达芬奇某个页面。返回是否成功（失败不算错误，只是慢一点）。

    wait>0 时切换后还会轮询到页面真的变成目标页为止。`OpenPage` 返回 True
    只代表请求被接受；紧接着调 ExportLUT 会得到 False，看起来像「这版不支持
    导出」。调色页首次打开要几秒，所以取色前都带 wait。
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
# 给排查用。把这一段整块贴回来，就知道是哪台机器、缺什么。


def text_report(app=None, extra=None):
    """多行纯文本自检报告（面板「诊断」按钮和 `colorpass compat` 都用它）。"""
    from . import __version__, paths

    rep = report(app)
    ver = rep.get("version") or "未连上"
    prod = rep.get("product") or "—"
    ok = sum(1 for f in rep["features"].values() if f["state"] == "ok")
    tot = len(rep["features"])
    rows = [
        "ColorPass %s 运行环境自检" % __version__,
        "平台     %s %s · Python %s%s"
        % (sys.platform, _arch(), sys.version.split()[0],
           " · 打包版" if getattr(sys, "frozen", False) else " · 源码"),
        "达芬奇   %s（%s）" % (ver, prod),
        "支持范围 %s（本工具只支持这一版）" % REQUIRED_TEXT,
        "脚本模块 %s" % (resolve.module_path() or "没找到"),
        "数据目录 %s" % paths.BASE,
        "能力     %d/%d 可验证通过" % (ok, tot),
    ]
    if rep.get("version_ok") is False:
        rows.append("版本     %s" % rep.get("version_note"))
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
