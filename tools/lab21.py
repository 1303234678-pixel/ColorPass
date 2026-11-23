"""在真机上试 21.2 的接口：哪些能用、哪些是「返回 None / [None] 悄悄不干活」。

背景：`ImportMedia([{"FilePath": ...}])` 在 21.2.0.6 上是**静默空操作**，
`AppendToTimeline` 失败时返回 `[None]` 而不是空列表 —— 只看「有没有报错」
或被返回值的长度骗过去，都会把失败当成成功。所以每一步都前后对比真实状态。

自己建一条 LAB21 时间线做实验，结束时把它删掉，不动工程设置、不碰别的时间线。

用法：python tools/lab21.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from colorpass import resolve  # noqa: E402


def call(label, fn, *a, **kw):
    try:
        v = fn(*a, **kw)
    except Exception as exc:
        print("  %-24s EXC %s: %s" % (label, type(exc).__name__, str(exc)[:110]))
        return None
    if isinstance(v, list):
        print("  %-24s list[%s]" % (label, ", ".join(
            "None" if x is None else type(x).__name__ for x in v) or ""))
    elif isinstance(v, dict):
        print("  %-24s dict(%d)" % (label, len(v)))
    else:
        print("  %-24s %r" % (label, v))
    return v


def main():
    app = resolve.scriptapp("Resolve")
    if not app:
        print("连不上达芬奇")
        return 1
    proj = app.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()
    print("工程 %s / 页面 %s" % (proj.GetName(), app.GetCurrentPage()))

    clips = [c for c in (mp.GetRootFolder().GetClipList() or [])
             if (c.GetClipProperty("File Path") or "")]
    if not clips:
        print("媒体池里没有可用素材")
        return 1
    mi = clips[0]
    print("素材 %s (%s 帧 / %s fps)" % (mi.GetName(), mi.GetClipProperty("Frames"),
                                      mi.GetClipProperty("FPS")))

    # 自己造一条干净的时间线，免得受别的时间线（可能锁了轨道）影响
    for i in range(1, proj.GetTimelineCount() + 1):
        t = proj.GetTimelineByIndex(i)
        if t and t.GetName() == "LAB21":
            mp.DeleteTimelines([t])
            break
    tl = mp.CreateEmptyTimeline("LAB21")
    proj.SetCurrentTimeline(tl)
    print("实验时间线 %s start=%d fps=%s" % (tl.GetName(), tl.GetStartFrame(),
                                          tl.GetSetting("timelineFrameRate")))
    base = tl.GetStartFrame()

    print("\n[1] AppendToTimeline：成功和失败的返回值不一样吗")
    call("append 正常", mp.AppendToTimeline,
              [{"mediaPoolItem": mi, "startFrame": 0, "endFrame": 20,
                "recordFrame": base, "trackIndex": 1}])
    print("    V1=%d" % len(tl.GetItemListInTrack("video", 1) or []))
    dup = call("append 重叠(应失败)", mp.AppendToTimeline,
               [{"mediaPoolItem": mi, "startFrame": 0, "endFrame": 20,
                 "recordFrame": base + 5, "trackIndex": 1}])
    print("    V1=%d  返回元素=%r" % (len(tl.GetItemListInTrack("video", 1) or []),
                                     dup[0] if dup else None))
    call("append 裸 MediaPoolItem", mp.AppendToTimeline, [mi])
    print("    V1=%d" % len(tl.GetItemListInTrack("video", 1) or []))

    items = tl.GetItemListInTrack("video", 1) or []
    if not items:
        print("!! 没东西可测")
        return 1
    it = items[0]
    print("  片段 %r start=%d dur=%d nodes=%s" %
          (it.GetName(), it.GetStart(), it.GetDuration(), it.GetNumNodes()))

    print("\n[2] 属性表（GetProperties 里有什么能读能写）")
    props = call("GetProperties", it.GetProperties)
    if isinstance(props, dict):
        for k in sorted(props):
            print("   %-26s %r" % (k, props[k]))

    print("\n[3] 标记：同一个位置重复 AddMarker 会叠吗")
    call("AddMarker(seg-A)", it.AddMarker, 0, "Blue", "CP", "", 1, "seg-A")
    call("AddMarker(seg-B 同位置)", it.AddMarker, 0, "Blue", "CP", "", 1, "seg-B")
    marks = it.GetMarkers() or {}
    print("    标记 %d 个: %s" % (len(marks), [(f, m.get("customData"))
                                            for f, m in marks.items()][:4]))
    print("\n[4] 按 customData 查 / 改")
    call("GetMarkerByCustomData", it.GetMarkerByCustomData, "seg-B")
    call("UpdateMarkerCustomData", it.UpdateMarkerCustomData, 0, "seg-C")
    print("    改后 %s" % [(f, m.get("customData"))
                          for f, m in (it.GetMarkers() or {}).items()][:4])

    print("\n[5] ExportLUT")
    tmp = os.path.join(tempfile.gettempdir(), "cp_lab_lut.cube")
    if os.path.exists(tmp):
        os.remove(tmp)
    n0 = it.GetNumNodes()
    call("ExportLUT(2, path)", it.ExportLUT, 2, tmp)
    print("    文件=%s 大小=%d 节点 %s -> %s" %
          (os.path.exists(tmp), os.path.getsize(tmp) if os.path.exists(tmp) else 0,
           n0, it.GetNumNodes()))

    print("\n[6] GetLUT / SetLUT")
    call("GetLUT", it.GetLUT)
    if os.path.exists(tmp):
        call("SetLUT(path)", it.SetLUT, tmp)
        call("GetLUT(after)", it.GetLUT)
        print("    节点数 %d  LUT 生效=%r" % (it.GetNumNodes(), it.GetLUT()))

    print("\n[7] 速度：SetSpeed 有用吗（决定还要不要 ffmpeg 预渲染）")
    d0, s0 = it.GetDuration(), it.GetSpeed()
    print("    改前 dur=%d speed=%r" % (d0, s0))
    call("SetSpeed(0.5)", it.SetSpeed, 0.5)
    time.sleep(0.4)
    print("    改后 dur=%d speed=%r" % (it.GetDuration(), it.GetSpeed()))
    call("SetSpeed(2.0)", it.SetSpeed, 2.0)
    time.sleep(0.4)
    print("    再改 dur=%d speed=%r" % (it.GetDuration(), it.GetSpeed()))
    call("SetSpeed(1.0)", it.SetSpeed, 1.0)
    time.sleep(0.3)
    print("    复位 dur=%d speed=%r" % (it.GetDuration(), it.GetSpeed()))
    call("GetSourceStartFrame", it.GetSourceStartFrame)
    call("GetSourceEndFrame", it.GetSourceEndFrame)

    print("\n[8] 构图（剪映的缩放/位移能不能对上）")
    for key, val in (("ZoomX", 1.5), ("Pan", 0.2), ("Tilt", -0.1)):
        call("SetProperty(%s)" % key, it.SetProperty, key, val)
        call("GetProperty(%s)" % key, it.GetProperty, key)

    print("\n[9] 启用/停用")
    call("SetClipEnabled(False)", it.SetClipEnabled, False)
    call("GetClipEnabled", it.GetClipEnabled)
    call("SetClipEnabled(True)", it.SetClipEnabled, True)

    print("\n[10] 类型 / 多机位线索")
    call("GetType", it.GetType)
    call("GetTrackTypeAndIndex", it.GetTrackTypeAndIndex)

    print("\n[11] 收尾：删掉实验时间线")
    call("DeleteTimelines", mp.DeleteTimelines, [tl])
    return 0


if __name__ == "__main__":
    sys.exit(main())
