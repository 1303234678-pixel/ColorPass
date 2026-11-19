"""全项体检 —— 在动手之前把能想到的坑都扫一遍，只读，不写任何东西。

这一模块的每一条检查都对应一个真实踩过的坑：

  素材不在盘上           → send 会跳过这段，时间线出现空洞
  源区间超出素材长度      → 达芬奇会「保末帧」，那一段变成静止画面
  同轨重叠但没有转场      → 剪映理论上不该出现，出现就是数据异常
  转场造成的重叠          → 需要收短前段，属于正常但必须报出来
  变速段数值不自洽        → source_duration ≈ target_duration × speed，
                            差太多说明 speed 字段和区间有一个是旧的
  复合片段内部素材缺失    → 整段都摆不进去，比单个段丢更严重
  复合片段内部长度 ≠ 占位 → 合成后时长会短，必须补首尾帧
  帧率换算累积误差        → 每段 floor 掉的小数会在末尾攒成几帧
  达芬奇不是空白工程      → 会改掉用户工程帧率、覆盖已有片段
"""
import os
from pathlib import Path

from . import draftio, paths, resolve

FPS = paths.FPS


def us2f(us):
    return int(round((us or 0) / 1e6 * FPS))


def _media_map(draft):
    out = {}
    for k in ("videos", "images"):
        for m in (draft.get("materials") or {}).get(k) or []:
            if isinstance(m, dict) and m.get("id"):
                out[m["id"]] = m
    return out


def _speed_map(draft):
    return draftio.speeds_by_ref(draft)


def scan_draft(draft, deep=False, speed_mode="bake"):
    """纯结构体检。返回 (blocking, warnings, facts)。

    speed_mode 决定「慢放段越界」算不算致命：默认的 bake 会先把变速段预渲染成
    等长素材，越界读根本不发生；只有 slot/source 这两种「1x 硬摆」模式才是
    真会串画面的硬问题。老写法一律记 blocking，于是体检把一个默认就会被
    自动化解的问题报成「不能送入」，自己吓自己。
    """
    blocking, warn, facts = [], [], {}
    mats = draft.get("materials") or {}
    media = _media_map(draft)
    spd = _speed_map(draft)
    cindex = draftio.compound_index(draft)
    trans_ids = {m.get("id") for m in mats.get("transitions") or []
                 if isinstance(m, dict)}

    v_tracks = [t for t in (draft.get("tracks") or [])
                if isinstance(t, dict) and t.get("type") == "video"]
    total_seg = 0
    per_lane = {}
    dur_total = int(draft.get("duration") or 0)

    for lane, tr in enumerate(v_tracks, 1):
        rows = []
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            total_seg += 1
            tt = sg.get("target_timerange") or {}
            st = sg.get("source_timerange") or {}
            sid = str(sg.get("id") or "?")
            rec = us2f(tt.get("start"))
            want = us2f(tt.get("duration"))
            src_us = int(st.get("start") or 0)
            src_dur = int(st.get("duration") or 0)
            refs = sg.get("extra_material_refs") or []
            speed = draftio.segment_speed(sg, spd)
            comp = draftio.compound_of(sg, cindex)
            row = {"segment_id": sid, "lane": lane, "rec": rec, "want": want,
                   "speed": round(speed, 3), "kind": "compound" if comp else "video"}

            if want <= 0:
                blocking.append({"code": "zero_duration", "lane": lane,
                                 "segment_id": sid, "why": "目标时长为 0"})
            if rec < 0:
                blocking.append({"code": "negative_rec", "lane": lane,
                                 "segment_id": sid, "why": "目标起点为负"})

            if comp is not None:
                inner, how = draftio.compound_inner(comp)
                parts = draftio.expand_compound(inner) if inner else []
                # 内部没有素材路径的段不算「可摆放」：Path("") 在 Python 里
                # 会退化成 Path(".")，exists() 为真 —— 老写法因此漏报。
                parts = [p for p in parts if p.get("path")]
                row["inner_parts"] = len(parts)
                row["inner_source"] = how
                if not inner:
                    blocking.append({"code": "compound_unreadable", "lane": lane,
                                     "segment_id": sid,
                                     "why": "复合片段内部时间线读不到"
                                            "（既无内嵌 draft，也读不到子草稿文件）"})
                elif not parts:
                    blocking.append({"code": "compound_empty", "lane": lane,
                                     "segment_id": sid,
                                     "why": "复合片段内部没有可摆放的视频段"})
                else:
                    miss = [p for p in parts if not Path(p["path"]).exists()]
                    if miss:
                        blocking.append({"code": "compound_missing_media",
                                         "lane": lane, "segment_id": sid,
                                         "why": "内部素材不在盘上：%s"
                                                % "、".join(Path(p["path"]).name
                                                           for p in miss[:3])})
                    span = max(us2f(p["rec_us"] + p["dur_us"]) for p in parts)
                    row["inner_span_frames"] = span
                    row["want_frames"] = want
                    if span != want:
                        warn.append({"code": "compound_span_mismatch",
                                     "lane": lane, "segment_id": sid,
                                     "want": want, "inner_span": span,
                                     "why": "复合片段内部跨度与占位差 %d 帧，"
                                            "合成后需要补首尾帧" % (want - span)})
                    sp_in = [p for p in parts if abs(p.get("speed", 1.0) - 1.0) > 1e-6]
                    if sp_in:
                        warn.append({"code": "speed_inside_compound",
                                     "lane": lane, "segment_id": sid,
                                     "why": "复合片段内部还有 %d 段变速，"
                                            "合成后表现为 1x" % len(sp_in)})
            else:
                m = media.get(sg.get("material_id")) or {}
                p = str(m.get("path") or "")
                row["path"] = p
                if not p:
                    blocking.append({"code": "no_path", "lane": lane,
                                     "segment_id": sid,
                                     "why": "素材没有文件路径（可能是占位素材）"})
                elif not os.path.exists(p.replace("\\", "/")):
                    blocking.append({"code": "missing_media", "lane": lane,
                                     "segment_id": sid, "path": p,
                                     "why": "素材不在盘上"})
                else:
                    mdur = int(m.get("duration") or 0)
                    if mdur:
                        need = src_us + src_dur
                        if need > mdur + 40000:
                            warn.append({"code": "over_source", "lane": lane,
                                         "segment_id": sid,
                                         "need_us": need, "media_us": mdur,
                                         "why": "源区间超出素材长度 %d ms，"
                                                "达芬奇会保末帧"
                                                % round((need - mdur) / 1000)})
                    if abs(speed - 1.0) > 1e-6:
                        exp = want * 1e6 * speed
                        diff = abs(src_dur - exp) / 1e6
                        row["speed_gap_ms"] = round(diff, 1)
                        if diff > 80:
                            warn.append({"code": "speed_inconsistent",
                                         "lane": lane, "segment_id": sid,
                                         "speed": speed,
                                         "src_ms": round(src_dur / 1000),
                                         "expect_ms": round(exp / 1000),
                                         "why": "变速数值与区间不自洽（差 %.0f ms）"
                                                % (diff * 1000)})
                        # 达芬奇不能改速度，只能 1x 播放。1x 播放「占位帧数」
                        # 需要吃源区间里同样多的帧：
                        #   want > 源区间帧数  → 慢放段，1x 摆不下，必须越界读
                        #                         出点之后的画面（真 BUG，会串画面）
                        #   want < 源区间帧数  → 加速段，只显示前段，后面的
                        #                         内容被截掉（信息缺失）
                        range_f = us2f(src_dur)
                        row["range_frames"] = range_f
                        row["want_frames"] = want
                        if range_f and want > range_f:
                            ent = {
                                "code": "speed_overrun", "lane": lane,
                                "segment_id": sid, "speed": round(speed, 3),
                                "want": want, "range": range_f,
                                "overrun_frames": want - range_f,
                                "why": "慢放段（%.3fx）按 1x 摆放需要 %d 帧，"
                                       "但剪映这段只用了 %d 帧源素材；1x 硬摆会"
                                       "越界读到出点之后的画面（串画面）"
                                       % (speed, want, range_f)}
                            if speed_mode == "bake":
                                ent["why"] += "。送入会先预渲染成等长素材，不会发生"
                                warn.append(ent)
                            else:
                                ent["why"] += "。当前模式 %s 无法回避" % speed_mode
                                blocking.append(ent)
                        elif range_f and want < range_f:
                            row["truncated_frames"] = range_f - want

            if any(r in trans_ids for r in refs):
                row["transition"] = True
            rows.append(row)
        per_lane[lane] = rows

    # 同轨重叠
    overlap_rows = []
    for lane, rows in per_lane.items():
        rows.sort(key=lambda x: x["rec"])
        for a, b in zip(rows, rows[1:]):
            if b["rec"] < a["rec"] + a["want"]:
                ov = a["rec"] + a["want"] - b["rec"]
                ent = {"lane": lane, "prev": a["segment_id"],
                       "segment_id": b["segment_id"], "overlap_frames": ov,
                       "transition": bool(a.get("transition") or b.get("transition"))}
                overlap_rows.append(ent)
                if not ent["transition"] and ov >= min(a["want"], b["want"]):
                    blocking.append({"code": "hard_overlap", "lane": lane,
                                     "segment_id": b["segment_id"],
                                     "why": "同轨整段重叠且没有转场"})

    # 帧率换算误差 / 时间线末尾是否有画面。
    # 老写法只比「视频轨最靠后的那段」和草稿总长，于是把「文字轨挂到片尾」
    # 这种最常见的情况全报成漂移（实测：视频 352 帧、文字轨 368 帧、草稿 368 帧，
    # 一切正常却报 duration_drift）。正确判据是**所有轨**的最晚结束帧：
    #   · 没有任何轨到草稿末尾  → 真漂移（导出会缺尾巴）
    #   · 视频轨比草稿短、但有别的轨到了末尾 → 正常，不报
    if dur_total:
        want_end = us2f(dur_total)
        all_ends = []
        for tr in draft.get("tracks") or []:
            if not isinstance(tr, dict):
                continue
            end = 0
            for sg in tr.get("segments") or []:
                if not isinstance(sg, dict):
                    continue
                tt = sg.get("target_timerange") or {}
                end = max(end, us2f(tt.get("start")) + us2f(tt.get("duration")))
            if end:
                all_ends.append((end, str(tr.get("type") or "?")))
        if all_ends:
            end, ttype = max(all_ends)
            if abs(end - want_end) > 2:
                warn.append({"code": "duration_drift", "track": ttype,
                             "segments_end": end, "draft_end": want_end,
                             "diff": end - want_end,
                             "why": "最晚的一条轨（%s）结束在 %d 帧，草稿总长 "
                                    "%d 帧，差 %+d 帧（末尾可能缺画面）"
                                    % (ttype, end, want_end, end - want_end)})

    facts.update({
        "video_tracks": len(v_tracks),
        "video_segments": total_seg,
        "layers": draftio.layers(draft),
        "features": draftio.feature_counts(draft),
        "draft_duration_us": dur_total,
        "draft_duration_frames": us2f(dur_total),
        "overlaps": overlap_rows,
        "per_lane": per_lane,
    })
    return blocking, warn, facts


def scan_resolve():
    """达芬奇侧体检。连不上就返回 available=False，不算错。"""
    try:
        dvr = resolve.load()
    except resolve.ResolveUnavailable as e:
        return {"available": False, "why": "脚本模块导入失败：%s" % str(e).splitlines()[0]}
    try:
        r = dvr.scriptapp("Resolve")
    except Exception as e:
        return {"available": False, "why": "接口异常：%s" % e}
    if not r:
        return {"available": False, "why": "达芬奇没有响应脚本接口（未启动/还在启动）"}
    pm = r.GetProjectManager()
    p = pm.GetCurrentProject() if pm else None
    out = {"available": True,
           "product": r.GetProductName(), "version": r.GetVersionString(),
           "studio": "Studio" in (r.GetProductName() or ""),
           "page": r.GetCurrentPage()}
    if not p:
        out["project"] = None
        out["blank"] = None
        return out
    names = []
    for i in range(1, (p.GetTimelineCount() or 0) + 1):
        tl = p.GetTimelineByIndex(i)
        names.append((tl.GetName() or "") if tl else "")
    own = [n for n in names if paths.is_own_timeline(n)]
    out.update({
        "project": p.GetName(),
        "timelines": names,
        "own_timelines": own,
        "foreign_timelines": [n for n in names if not paths.is_own_timeline(n)],
        # 可送入 = 工程里没有别人的时间线。自己上一轮建的 `CP · …` 不算阻塞：
        # send 建时间线时会自动换名（空名复用、有内容加序号），不会覆盖。
        # 判据要跟 preflight.daVinci 一致，别各写一套。
        "blank": not [n for n in names if not paths.is_own_timeline(n)],
        "fps": p.GetSetting("timelineFrameRate"),
        "width": p.GetSetting("timelineResolutionWidth"),
        "height": p.GetSetting("timelineResolutionHeight"),
        "color_science": p.GetSetting("colorScience"),
    })
    return out


def run(deep=False, speed_mode="bake"):
    rep = {"ok": False, "action": "audit", "blocking": [], "warnings": [],
           "facts": {}, "speed_mode": speed_mode}
    try:
        draft, cur, fresh = draftio.read(allow_stale=True)
    except draftio.NoDraft as e:
        rep["error"] = str(e)
        rep["blocking"] = [{"code": "no_draft", "why": str(e)}]
        return rep
    except draftio.StaleDraft as e:
        rep["error"] = str(e)
        rep["blocking"] = [{"code": "stale", "why": str(e)}]
        return rep

    rep["draft"] = cur.get("name")
    rep["draft_id"] = draft.get("id")
    rep["freshness"] = fresh
    if fresh.get("stale"):
        rep["blocking"].append({
            "code": "stale", "plaintext_bytes": fresh["plaintext_bytes"],
            "payload_bytes": fresh["disk_payload_bytes"],
            "delta_bytes": fresh["delta_bytes"],
            "why": "内存明文与磁盘密文体积对不上，说明内存是旧版本"})

    blocking, warn, facts = scan_draft(draft, deep, speed_mode=speed_mode)
    rep["blocking"] += blocking
    rep["warnings"] += warn
    # 「有但不送」清单：体检时也列出来，省得用户以为达芬奇里能看到这些。
    facts["unsent"] = draftio.unsupported(draft)
    facts["masks_not_sent"] = draftio.mask_segments(draft)
    facts["transforms_not_sent"] = draftio.transform_segments(draft)
    rep["facts"] = facts
    rep["davinci"] = scan_resolve()
    dv = rep["davinci"]
    if dv.get("available"):
        if not dv.get("studio"):
            rep["blocking"].append({"code": "needs_studio",
                                    "why": "免费版不开外部脚本接口"})
        if dv.get("page") is None:
            rep["blocking"].append({
                "code": "project_manager",
                "why": "达芬奇停在项目管理器，还没进入任何工程。送入前要打开一个"
                       "空白工程（或运行 colorpass newproj 让工具自己建）。"})
        elif dv.get("blank") is False:
            rep["blocking"].append({
                "code": "not_blank",
                "timelines": dv.get("foreign_timelines"),
                "why": "达芬奇当前工程「%s」里有 %d 条不是本工具建的时间线（%s）；"
                       "送入会改工程帧率并往时间线上放片段，可能弄乱你正在剪的"
                       "东西。请换一个空白工程。"
                       % (dv.get("project"),
                          len(dv.get("foreign_timelines") or []),
                          "、".join((dv.get("foreign_timelines") or [])[:3]))})
        try:
            fps_ok = abs(float(dv.get("fps") or 0) - float(FPS)) < 1e-6
        except (TypeError, ValueError):
            fps_ok = True
        if dv.get("own_timelines") and dv.get("fps") and not fps_ok:
            # 已有本工具的时间线时改工程帧率会让它们整体重算 —— 和
            # preflight 一样按阻塞处理，不是「提醒一下」。
            rep["blocking"].append({
                "code": "fps_mismatch", "davinci_fps": dv["fps"],
                "why": "工程帧率是 %s（要 %d）。工程里已有本工具建的时间线，"
                       "改帧率会让它们重算。请新建一个空白工程再送入。"
                       % (dv["fps"], FPS)})
    else:
        rep["warnings"].append({"code": "no_resolve", "why": dv.get("why")})

    rep["summary"] = {
        "blocking": len(rep["blocking"]),
        "warnings": len(rep["warnings"]),
        "segments": facts.get("video_segments"),
        "tracks": facts.get("video_tracks"),
        "speed": facts.get("features", {}).get("segments_with_speed"),
        "compound": facts.get("features", {}).get("segments_with_compound"),
        "transitions": facts.get("features", {}).get("segments_with_transition"),
        "keyframes": facts.get("features", {}).get("segments_with_keyframes"),
    }
    rep["ok"] = True
    out = paths.draft_run_dir(draft.get("id"))
    draftio.dump_json(rep, os.path.join(out, "audit.json"))
    return rep
