"""多机位 / 合并片段自检（只读）。

为什么单独一条命令
------------------
「在达芬奇里做了多机位，还能不能写回剪映」这件事，以前只能靠真的走一遍
写回去试 —— 失败时看到的是「时间线与计划不一致」，分不清是片段被动了、
还是插件根本不认多机位。这条命令把判断摊开：

  · 每个片段是什么（多机位 / 普通 / 带不带段号）
  · 和送入计划对不对得上（覆盖了几段、能不能唯一确定）
  · 真要导出时，ExportLUT 在它身上能不能用（--export 才试）

它**不改时间线、不写工程**。`--export` 只往数据目录的 multicam-check/
里写 cube，不碰剪映，也不碰达芬奇的调色。
"""
import json
import os
import time
from pathlib import Path

from . import compat, paths, pull, resolve

dvr = resolve.lazy()


def _item_row(it, lane):
    kind = compat.item_kind(it)
    try:
        sid = pull.seg_marker(it)
    except Exception:
        sid = None
    try:
        enabled = not pull.is_disabled(it)
    except Exception:
        enabled = True
    return {
        "lane": lane,
        "start": it.GetStart(),
        "dur": it.GetDuration(),
        "name": (it.GetName() or "")[:60],
        "segment_id": sid,
        "multicam": kind["multicam"],
        "multicam_why": kind["why"],
        "type": kind["type"],
        "media_type": kind["media_type"],
        "enabled": enabled,
        "_obj": it,
    }


def scan(tl):
    rows = []
    for lane in range(1, (tl.GetTrackCount("video") or 0) + 1):
        for it in sorted((tl.GetItemListInTrack("video", lane) or []),
                         key=lambda x: x.GetStart()):
            rows.append(_item_row(it, lane))
    return rows


def _plan_match(tl, rows):
    """和最近一次送入的计划比一遍；没有计划就给 None。"""
    from . import snapshot
    ptr = snapshot.current()
    if not ptr:
        return None, "还没有完成过一次「送入达芬奇」，没有可对照的计划"
    did = ptr.get("draft_id")
    run_dir = ptr.get("run_dir") or paths.draft_run_dir(did)
    plan_path = os.path.join(run_dir, "plan.json")
    if not os.path.exists(plan_path):
        return None, "缺少 plan.json（那一轮没有走完），没有可对照的计划"
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    issues, placed = pull.verify_alignment(tl, plan, int(plan.get("start_frame") or 0))
    return {
        "draft": ptr.get("draft_name"),
        "timeline": plan.get("timeline"),
        "segments": len(plan.get("segments") or []),
        "ok": not issues,
        "issues": issues,
        "coverage": pull.coverage_groups(placed),
        "matched": len(placed),
    }, ""


def _try_export(rows, app=None):
    """对每个多机位片段真的导一次 LUT。只写本地临时文件。"""
    outdir = os.path.join(paths.BASE, "multicam-check")
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception as exc:
        return [{"why": "建不了临时目录：%s" % exc}]
    try:
        compat.ensure_page("color", wait=10.0)
        time.sleep(0.5)
    except Exception:
        pass
    lut = compat.lut_type(app=app)
    out = []
    for r in rows:
        if not r["multicam"]:
            continue
        dst = os.path.join(outdir, "V%d_%d.cube" % (r["lane"], r["start"]))
        ok = False
        try:
            ok = bool(r["_obj"].ExportLUT(lut, dst))
        except Exception as exc:
            out.append({"lane": r["lane"], "start": r["start"],
                        "name": r["name"], "ok": False,
                        "why": "ExportLUT 抛异常：%s" % str(exc)[:100]})
            continue
        if not ok or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            out.append({"lane": r["lane"], "start": r["start"],
                        "name": r["name"], "ok": False,
                        "why": "ExportLUT 返回 False 或产出空文件"})
            continue
        m = pull.measure_cube(dst)
        out.append({"lane": r["lane"], "start": r["start"], "name": r["name"],
                    "ok": m is not None, "bytes": os.path.getsize(dst),
                    "cube": dst,
                    "identity_dev_max": (m or {}).get("dev_max"),
                    "why": "" if m else "cube 不完整（不是 33³）"})
    return out


def run(export=False):
    rep = {"ok": False, "action": "multicam", "export": bool(export)}
    ok, why = compat.version_ok()
    if ok is False:
        rep["error"] = why
        return rep
    try:
        app = dvr.scriptapp("Resolve")
    except Exception as exc:
        rep["error"] = "连不上达芬奇脚本接口：%s" % str(exc)[:120]
        return rep
    if not app:
        rep["error"] = "达芬奇没有响应脚本接口（可能没开）"
        return rep
    try:
        page = app.GetCurrentPage()
    except Exception:
        page = None
    if not page:
        rep["error"] = "达芬奇停在项目管理器，先打开一个工程再自检"
        return rep
    try:
        proj = app.GetProjectManager().GetCurrentProject()
        tl = proj.GetCurrentTimeline() if proj else None
    except Exception as exc:
        rep["error"] = "读工程失败：%s" % str(exc)[:120]
        return rep
    if not tl:
        rep["error"] = "当前没有时间线，先「送入达芬奇」或打开一条时间线"
        return rep

    rows = scan(tl)
    n_mc = len([r for r in rows if r["multicam"]])
    rep.update({
        "ok": True,
        "timeline": tl.GetName(),
        "count": len(rows),
        "multicam": n_mc,
        "coverage_capable": compat.alive(proj.GetMediaPool(),
                                        "CreateMulticamClip"),
        "items": [{k: v for k, v in r.items() if k != "_obj"} for r in rows],
    })
    match, note = _plan_match(tl, rows)
    rep["plan"] = match
    if note:
        rep["plan_note"] = note
    if export:
        rep["exports"] = _try_export(rows, app)
    return rep


def text(rep):
    """人读版。终端一屏能看完，重点在前三行。"""
    if rep.get("error"):
        return "多机位自检失败：%s" % rep["error"]
    lines = ["ColorPass 多机位自检", "时间线   %s" % (rep.get("timeline") or "?")]
    lines.append("片段     %d 个（多机位 %d 个）"
                 % (rep.get("count", 0), rep.get("multicam", 0)))
    for r in [x for x in rep.get("items") or [] if x["multicam"]]:
        lines.append("  V%d %d-%d  %s" % (
            r["lane"], r["start"], r["start"] + r["dur"], r["name"]))
        lines.append("        %s%s%s" % (
            r["multicam_why"] or "多机位",
            "" if r["segment_id"] else "；没有段号（写回按覆盖段匹配）",
            "" if r["enabled"] else "；已禁用"))
    p = rep.get("plan")
    if p:
        lines.append("匹配     计划 %d 段，对上 %d 段%s" % (
            p["segments"], p["matched"], "，可写回" if p["ok"] else "，**不能写回**"))
        for c in p["coverage"]:
            lines.append("  覆盖  V%d %d 起 %d 帧 / %d 段%s" % (
                c["lane"], c["start"], c["dur"], c["count"],
                "（多机位）" if c["multicam"] else ""))
        for i in p["issues"][:3]:
            lines.append("  问题  %s" % i.get("why"))
    elif rep.get("plan_note"):
        lines.append("匹配     %s" % rep["plan_note"])
    if rep.get("exports") is not None:
        ok = len([e for e in rep["exports"] if e.get("ok")])
        lines.append("导出     %d/%d 成功" % (ok, len(rep["exports"])))
        for e in rep["exports"]:
            if not e.get("ok"):
                lines.append("  失败  V%d %d：%s"
                             % (e["lane"], e["start"], e.get("why")))
    if not rep.get("coverage_capable"):
        lines.append("提示     这台机器上没有 CreateMulticamClip，不能用脚本建多机位；"
                     "已有时间线里的多机位仍可写回")
    return "\n".join(lines)
