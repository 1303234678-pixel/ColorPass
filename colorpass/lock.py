"""轨道锁：调色前锁死剪辑，避免达芬奇侧误改导致 LUT 与段错配。

实测（Resolve 20.3.0.10）：
  · SetTrackLock 对 V/A 全部轨道生效，且能挡住脚本层的 DeleteClips（返回 False）
  · 锁定后 SetCDL / ExportLUT 完全不受影响 —— 调色照常
"""
import os
import time

from . import draftio, paths, resolve

# 延迟加载：没装达芬奇的机器照样能 import 本模块（check / plan 不需要 API）。
dvr = resolve.lazy()


def run(state="on"):
    on = str(state).lower() in ("on", "1", "true", "lock")
    rep = {"action": "lock", "state": "on" if on else "off", "ok": False}
    resolve = dvr.scriptapp("Resolve")
    proj = resolve.GetProjectManager().GetCurrentProject()
    tl = None
    for _ in range(15):
        tl = proj.GetCurrentTimeline()
        if tl and (tl.GetItemListInTrack("video", 1) or []):
            break
        time.sleep(0.4)
    if not tl:
        rep["error"] = "没有当前时间线"
        return rep

    rows = []
    for t in ("video", "audio"):
        for i in range(1, (tl.GetTrackCount(t) or 0) + 1):
            name = "%s%d" % (t[0].upper(), i)
            try:
                tl.SetTrackLock(t, i, on)
                rows.append({"track": name,
                             "locked": bool(tl.GetIsTrackLocked(t, i))})
            except Exception as e:
                rows.append({"track": name, "locked": False, "error": str(e)})
    rep["tracks"] = rows
    rep["locked"] = sum(1 for r in rows if r["locked"])

    # 同步时间线第 0 帧的说明标记
    try:
        note_text = ("剪映往返 · 只含视频轨与内嵌音频。"
                     + ("所有轨道已上锁（改剪辑前先解锁）。" if on
                        else "轨道当前未锁 —— 可改剪辑，但改完需重新送入。"))
        marks = tl.GetMarkers() or {}
        for k, v in marks.items():
            # "BRIDGE" 是改名前的说明标记，见到就顺手清掉
            if (v or {}).get("name") in ("ColorPass", "BRIDGE"):
                tl.DeleteMarkerAtFrame(int(k))
                break
        tl.AddMarker(0, "Purple", "ColorPass", note_text, 1)
        rep["note"] = note_text
    except Exception as e:
        rep["note_error"] = str(e)

    rep["ok"] = rep["locked"] == len(rows) if on else rep["locked"] == 0
    # 报告落在「最近一次送入」的目录里。以前这里直接读剪映内存拿草稿 ID，
    # 于是剪映没开时整条命令在**已经锁好轨道之后**抛 NoDraft —— 动作成功了
    # 却报失败。锁定本来就不依赖剪映，所以改成优先用指针、退回指针目录。
    try:
        from . import snapshot
        ptr = snapshot.current() or {}
        did = ptr.get("draft_id") or "unknown"
        rep["draft"] = ptr.get("draft_name")
        draftio.dump_json(rep, os.path.join(paths.draft_run_dir(did), "lock.json"))
    except Exception as exc:
        rep["report_error"] = "%s: %s" % (type(exc).__name__, exc)
    return rep
