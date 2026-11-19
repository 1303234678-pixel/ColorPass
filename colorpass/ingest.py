"""达芬奇 → 剪映：把**当前时间线**写成一份剪映草稿。

与 send.py 相反的方向
--------------------
send.py 读剪映草稿、往达芬奇时间线上放；这里读达芬奇时间线、生成剪映草稿。
中间不落 XML、不渲染成片：直接把轨道与片段写成明文 `draft_content.json`，
剪映首页刷新就能看到并继续剪。

能带过去的
----------
- 轨道数与顺序（V1 当主轨，其余当画中画）、片段顺序与位置（按帧号，无累积误差）
- 源入点 / 出点、时长、倍速（由源帧数与时间线帧数推出来，不依赖 Speed 属性）
- 停用片段（达芬奇里 clip disabled → 剪映 `visible:false`，仍占时间）
- 项目帧率、分辨率

带不过去的（写进报告，不静默吞掉）
----------------------------------
转场、滤镜/节点调色、蒙版、字幕、独立音频轨、构图（缩放/位移/旋转）。
这些在剪映侧要么是另一套结构，要么需要逐帧烘焙；硬塞会做出「看着像、
其实不对」的草稿。需要完整保真时用 `--xml` 导出 FCPXML，走剪映自带的
「导入工程」，由它自己解释这些内容。
"""
import os

from . import draftbuild, paths, probe
from .resolve import lazy as _lazy

dvr = _lazy()          # 只在真正调用时才去加载 DaVinciResolveScript

SIDES = ("video",)
XML_TYPES = ("EXPORT_FCPXML_1_8", "EXPORT_FCPXML_1_9", "EXPORT_FCPXML_1_7",
             "EXPORT_FCPXML_1_6", "EXPORT_FCPXML_1_5", "EXPORT_FCPXML_1_4",
             "EXPORT_FCPXML_1_3")


# ---------------------------------------------------------------- 小工具
def _app():
    """拿到 Resolve 应用对象；没开达芬奇时给一句人话，而不是 AttributeError。"""
    app = dvr.scriptapp("Resolve")
    if app is None:
        raise RuntimeError("连不上达芬奇。请先打开达芬奇并进入一个工程，再点一次。")
    return app


def _int(v, default=0):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _prop(obj, name):
    """GetClipProperty 一律返回字符串，取不到时是 None 或 ''。"""
    try:
        return (obj.GetClipProperty(name) or "").strip()
    except Exception:
        return ""


def _res_of(mp):
    txt = _prop(mp, "Resolution")
    if "x" in txt:
        try:
            w, h = txt.lower().split("x", 1)
            return _int(w, 0), _int(h, 0)
        except ValueError:
            pass
    return 0, 0


def _has_audio(mp):
    for key in ("Audio Ch", "Audio Channels"):
        txt = _prop(mp, key)
        if txt:
            return _int(txt, 0) > 0
    return False


def _timeline_fps(tl):
    for key in ("timelineFrameRate", "timelineFrameRateString"):
        try:
            v = float(tl.GetSetting(key))
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 24.0


def _timeline_res(tl):
    w = _int(tl.GetSetting("timelineResolutionWidth"), 0)
    h = _int(tl.GetSetting("timelineResolutionHeight"), 0)
    return (w or 1920), (h or 1080)


# ---------------------------------------------------------------- 读时间线
def read_timeline(r=None, timeline=None):
    """把达芬奇当前时间线读成中间结构（不落盘，不需要剪映）。"""
    if timeline is None:
        app = r or _app()
        pm = app.GetProjectManager()
        proj = pm.GetCurrentProject() if pm else None
        if not proj:
            raise RuntimeError("达芬奇里没有打开的工程")
        timeline = proj.GetCurrentTimeline()
        if not timeline:
            raise RuntimeError("当前工程里没有打开的时间线")

    fps = _timeline_fps(timeline)
    width, height = _timeline_res(timeline)
    out = {
        "name": timeline.GetName() or "未命名时间线",
        "fps": fps, "width": width, "height": height,
        "tracks": [], "notes": [],
        "stats": {"clips": 0, "disabled": 0, "retimed": 0,
                  "no_media": 0, "no_source": 0},
    }

    n_v = _int(timeline.GetTrackCount("video"), 0)
    if not n_v:
        raise RuntimeError("这条时间线没有视频轨")

    for idx in range(1, n_v + 1):          # V1 是底层 → 剪映主轨
        items = timeline.GetItemListInTrack("video", idx) or []
        if not items:
            continue
        segs = []
        for it in items:
            mp = it.GetMediaPoolItem() if hasattr(it, "GetMediaPoolItem") else None
            path = _prop(mp, "File Path") if mp else ""
            if not path or not os.path.exists(path):
                out["stats"]["no_media"] += 1
                out["notes"].append("跳过「%s」：没有可用的素材文件路径"
                                    % (it.GetName() or "?"))
                continue

            start = _int(it.GetStart())
            dur = _int(it.GetDuration()) or (_int(it.GetEnd()) - start)
            if dur <= 0:
                out["stats"]["no_media"] += 1
                out["notes"].append("跳过「%s」：时长为 0" % (it.GetName() or "?"))
                continue

            src_start = src_end = 0
            if hasattr(it, "GetSourceStartFrame") and hasattr(it, "GetSourceEndFrame"):
                src_start = _int(it.GetSourceStartFrame())
                src_end = _int(it.GetSourceEndFrame())
            src_dur = max(0, src_end - src_start)
            if not src_dur:
                out["stats"]["no_source"] += 1
                src_dur = dur              # 取不到源区间就当原速
            speed = round(float(src_dur) / float(dur), 6) if dur else 1.0
            if abs(speed - 1.0) > 1e-3:
                out["stats"]["retimed"] += 1

            enabled = True
            if hasattr(it, "GetClipEnabled"):
                try:
                    enabled = bool(it.GetClipEnabled())
                except Exception:
                    enabled = True
            if not enabled:
                out["stats"]["disabled"] += 1

            cw, ch = _res_of(mp)
            total_f = _int(_prop(mp, "Frames"), 0) or (src_end or src_dur + src_start)

            segs.append({
                "path": path,
                "name": it.GetName() or os.path.basename(path),
                "tl_start_f": start,
                "dur_f": dur,
                "src_start_f": src_start,
                "src_dur_f": src_dur,
                "src_total_f": total_f,
                "speed": speed,
                "enabled": enabled,
                "width": cw, "height": ch,
                "has_audio": _has_audio(mp),
            })
            out["stats"]["clips"] += 1

        if segs:
            out["tracks"].append({"type": "video", "items": segs})

    if not out["tracks"]:
        raise RuntimeError("这条时间线上没有可迁移的视频片段"
                           "（片段可能引用了生成器、复合片段或离线素材）")
    out["notes"].append("转场、滤镜/调色、蒙版、字幕、独立音频轨、构图（缩放/位移）"
                        "不随草稿迁移；需要完整保真请用 --xml 导出 FCPXML，"
                        "在剪映里「导入工程」。")
    return out


# ---------------------------------------------------------------- 导出 XML
def export_xml(timeline=None, dest=None, r=None):
    """导出 FCPXML 给剪映的「导入工程」用（保真度最高的一条路）。"""
    if timeline is None:
        app = r or _app()
        pm = app.GetProjectManager()
        proj = pm.GetCurrentProject() if pm else None
        timeline = proj.GetCurrentTimeline() if proj else None
    if not timeline:
        raise RuntimeError("达芬奇里没有打开的时间线")
    if not dest:
        dest = os.path.join(paths.ensure(), "colorpass_%s.fcpxml"
                            % (timeline.GetName() or "timeline"))
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last = ""
    for kind in XML_TYPES:
        try:
            if timeline.Export(dest, kind):
                return {"ok": True, "path": dest, "type": kind}
        except Exception as exc:
            last = "%s: %r" % (kind, exc)
    return {"ok": False, "error": last or "时间线导出接口不可用（需要 Studio 版）"}


# ---------------------------------------------------------------- 主流程
def plan(info, name=None):
    """只算不写：给 CLI 的 dry-run 与面板的状态行用。"""
    name = name or default_name(info["name"])
    clips = sum(len(t["items"]) for t in info["tracks"])
    end_us = 0
    for tr in info["tracks"]:
        for it in tr["items"]:
            end_us = max(end_us, int(round(it["tl_start_f"] / info["fps"] * 1e6))
                         + int(round(it["dur_f"] / info["fps"] * 1e6)))
    return {
        "name": name,
        "timeline": info["name"],
        "tracks": len(info["tracks"]),
        "clips": clips,
        "duration_s": round(end_us / 1e6, 3),
        "fps": info["fps"], "width": info["width"], "height": info["height"],
        "stats": info["stats"],
        "notes": info["notes"],
        "segments": [[{k: it[k] for k in ("path", "tl_start_f", "dur_f",
                                          "src_start_f", "speed", "enabled")}
                      for it in tr["items"]] for tr in info["tracks"]],
    }


def default_name(timeline_name):
    return "%s_达芬奇" % (timeline_name or "时间线")


def run(name=None, dry_run=False, xml_path=None, root=None, r=None):
    """达芬奇时间线 → 剪映草稿。返回报告 dict。"""
    rep = {"ok": False, "action": "ingest"}
    try:
        info = read_timeline(r)
    except Exception as exc:
        rep["error"] = str(exc)
        return rep

    p = plan(info, name)
    rep.update(p)
    if dry_run:
        rep["ok"] = True
        rep["dry_run"] = True
        return rep

    if xml_path:
        rep["xml"] = export_xml(dest=xml_path, r=r)

    if not probe.user_data_dir().is_dir():
        rep["error"] = "找不到剪映的用户目录：%s" % probe.user_data_dir()
        return rep

    tpl = os.path.join(paths.PKG_DIR, "templates")
    try:
        built = draftbuild.build_timeline(
            tpl, p["name"], info["tracks"], fps=info["fps"],
            width=info["width"], height=info["height"], root=root)
    except Exception as exc:
        rep["error"] = "生成草稿失败：%r" % exc
        return rep

    rep["build"] = built
    if not built.get("ok"):
        rep["error"] = built.get("error") or "生成草稿失败"
        return rep
    rep["ok"] = True
    rep["draft"] = {"name": p["name"], "folder": built.get("folder")}
    return rep
