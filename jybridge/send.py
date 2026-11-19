"""剪映草稿 → 达芬奇时间线（严格帧对齐 + 全轨上锁 + 身份锚）

沿用已实测的结论：
  · 不用 FCPXML：Resolve 20.3.0.10 上 ImportTimelineFromFile 会卡死脚本接口
  · 起始时间码必须趁空时间线归零，之后再设会把片段整体平移到负帧
  · 落地时长 = floor(源帧数 × 30 / 素材帧率)；反解源帧数可达任意目标帧数，
    超出素材长度时达芬奇保持最后一帧；仍不可达的补 1 帧尾巴段
  · AppendToTimeline 的 endFrame 不含尾
  · 只送视频轨；文字/贴纸/转场/关键帧留在剪映，写回时原样保留

本轮新增的三件事（都基于实测，不是推测）：

1. **变速**：Resolve 没有设置片段速度的接口。
   实测：TimelineItem 的 GetProperty() 只返回 26 个键，没有 Speed；
   SetProperty("Speed", 2.0) → False；SetSpeed / ChangeSpeed / SetRetimeSpeed /
   Timeline.SetClipsSpeed 等 11 个候选方法全部是 None。
   所以变速只能二选一，由 speed_mode 决定：
     slot   （默认）时长按剪映 target 摆放，时间线严格对齐；该段在达芬奇里
            是 1x 播放，只看到源区间的前一段画面。
     source 按源帧数 1x 摆放，画面一帧不丢，但本轨后续片段整体后移。
   两种模式都会把每个变速段的 speed / 占位 / 实际落地写进报告，绝不静默处理。

2. **复合片段**：剪映的复合片段在母稿里是**一个段**，内部时间线嵌在
   materials.drafts[].draft 里（磁盘 subdraft/<uuid>/draft_content.json 是明文）。
   做法：把内部片段按原来的相对位置摆到相邻轨道上，立刻用
   Timeline.CreateCompoundClip 合成一个项（实测可跨轨道、音频自动带上、
   合成后能单独 ExportLUT）。于是它在达芬奇里仍然是「一项」，
   取回时一个 LUT 对应剪映里的一个段 —— 语义完全对齐。

3. **轨道顺序**：先摆复合片段、后摆普通片段。复合片段在合成的那一瞬间会把
   临时占用的轨道腾空，因此不会和后面的片段抢位置。
"""
import math
import os
import sys
import time
from pathlib import Path

from . import draftio, paths

MOD = r"C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
sys.path.insert(0, MOD)
import DaVinciResolveScript as dvr  # noqa: E402

FPS = paths.FPS
PAD = paths.PAD_SUFFIX


def say(*a):
    print(*a, flush=True)


def us2f(us):
    return int(round((us or 0) / 1e6 * FPS))


def tl_len(n, mf):
    return int(math.floor(n * FPS / mf + 1e-9))


def solve_exact(want, mf):
    """反解源帧数，让落地时长正好等于 want。返回 (n, tail, kind)。"""
    if want <= 0:
        return 0, 0, "empty"
    ideal = want * mf / FPS
    lo = max(1, int(math.floor(ideal)) - 3)
    hi = int(math.ceil(ideal)) + 6
    hit = [n for n in range(lo, hi + 1) if tl_len(n, mf) == want]
    if hit:
        return min(hit, key=lambda x: abs(x - ideal)), 0, "exact"
    under = [n for n in range(lo, hi + 1) if tl_len(n, mf) == want - 1]
    if under:
        n = min(under, key=lambda x: abs(x - ideal))
        return n, 1, "tail1"
    n = max(1, int(round(ideal)))
    return n, max(0, want - tl_len(n, mf)), "approx"


def build_plan(draft, speed_mode="slot"):
    """草稿 → 摆放计划。

    转场会让相邻两段的目标区间重叠（剪映用两段的余量做交叉淡化），而达芬奇
    一条轨不能放重叠片段。slot 模式下保留每段**起始帧**（这是和剪映对齐的
    关键），把前一段的落地时长收到后一段的起点。
    source 模式不消解重叠，而是整轨按实际时长顺排（会推移后续片段）。
    """
    vids = {}
    for v in (draft.get("materials") or {}).get("videos") or []:
        if isinstance(v, dict) and v.get("id"):
            vids[v["id"]] = v
    photos = {}
    for ph in (draft.get("materials") or {}).get("photos") or []:
        if isinstance(ph, dict) and ph.get("id"):
            photos[ph["id"]] = ph
    mats = draft.get("materials") or {}
    trans_ids = {m.get("id") for m in mats.get("transitions") or []
                 if isinstance(m, dict)}
    cindex = draftio.compound_index(draft)
    spd = draftio.speeds_by_ref(draft)
    mmask = draftio.mask_map(draft)
    # 曲线变速：speed 只是平均值，真正的节奏在 curve_speed.speed_points 里。
    curved = {m.get("id") for m in (mats.get("speeds") or [])
              if isinstance(m, dict) and m.get("curve_speed")}
    kf_segs = set()
    for tr in (draft.get("tracks") or []):
        for sg in (tr.get("segments") or []):
            if isinstance(sg, dict) and (sg.get("common_keyframes") or []):
                kf_segs.add(sg.get("id"))

    v_tracks = [tr for tr in (draft.get("tracks") or [])
                if isinstance(tr, dict) and tr.get("type") == "video"]

    plan, skipped = [], []
    for lane, tr in enumerate(v_tracks):
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            tt = sg.get("target_timerange") or {}
            st = sg.get("source_timerange") or {}
            refs = sg.get("extra_material_refs") or []
            rec, want = us2f(tt.get("start")), us2f(tt.get("duration"))
            speed = draftio.segment_speed(sg, spd)
            # 蒙版只做标记，不影响任何摆放参数：这条链路对蒙版的策略是
            # 「完全不送」（见 draftio.UNSENT_SPEC）。标记留给报告和写回核对。
            mask_names = [str((mmask.get(r) or {}).get("name") or "蒙版")
                          for r in refs if r in mmask]
            base = {
                "segment_id": sg.get("id"),
                "lane": lane + 1,
                "rec": rec, "want": want,
                "src_us": int(st.get("start") or 0),
                "range_us": int(st.get("duration") or 0),
                "speed": speed,
                "has_transition": any(r in trans_ids for r in refs),
                "has_keyframes": sg.get("id") in kf_segs,
                "has_mask": bool(mask_names) or bool(sg.get("enable_adjust_mask")),
                "mask_names": mask_names,
                "curve_speed": any(r in curved for r in refs),
                # 剪映的「停用片段」= 段上 visible:false。它仍然占着时间线
                # 的位置（取消停用后时间轴不会动），只是不渲染。所以不能跳过
                # —— 跳过会让达芬奇里凭空多出一段黑场，也会让后续每段的
                # 位置核对全部错位。正确做法是照样落轨，落完 SetClipEnabled
                # 关掉它（见 mark_disabled）。
                "enabled": sg.get("visible") is not False,
            }

            comp = draftio.compound_of(sg, cindex)
            if comp is not None:
                inner, src = draftio.compound_inner(comp)
                parts = draftio.expand_compound(inner) if inner else []
                parts = [p for p in parts if p.get("path")]
                if not parts:
                    skipped.append({"segment_id": sg.get("id"),
                                    "reason": "复合片段内部没有可摆放的视频"
                                              "（来源 %s）" % src})
                    continue
                miss = [p["path"] for p in parts
                        if not Path(p["path"]).exists()]
                if miss:
                    skipped.append({"segment_id": sg.get("id"),
                                    "reason": "复合片段内部素材不在盘上：%s"
                                              % "、".join(Path(m).name for m in miss[:3])})
                    continue
                base.update({"kind": "compound", "inner": parts,
                             "compound_name": str(comp.get("name") or ""),
                             "inner_disabled": sum(
                                 1 for p in parts
                                 if p.get("enabled") is False),
                             "inner_source": src, "path": ""})
                plan.append(base)
                continue

            v = vids.get(sg.get("material_id")) or {}
            p = str(v.get("path") or "").replace("\\", "/")
            if not p:
                # 图片段（materials.photos）没有 path 在 videos 里。达芬奇能导
                # 静帧，但这条链路只走视频素材，图片会被整段跳过 —— 报
                # 「素材不在盘上」会让人以为文件丢了，必须说清是「不送」。
                ph = photos.get(sg.get("material_id"))
                if ph:
                    skipped.append({"segment_id": sg.get("id"),
                                    "path": str(ph.get("path") or ""),
                                    "reason": "图片素材当前不送，这一段会是空的"})
                    continue
                skipped.append({"segment_id": sg.get("id"), "path": p,
                                "reason": "素材不在盘上"})
                continue
            if not Path(p).exists():
                skipped.append({"segment_id": sg.get("id"), "path": p,
                                "reason": "素材不在盘上"})
                continue
            base.update({"kind": "video", "path": p})
            plan.append(base)

    for c in plan:
        if c["kind"] == "video":
            c["place"] = c["want"]
        else:
            span = 0
            for p in c["inner"]:
                span = max(span, us2f(p["rec_us"] + p["dur_us"]))
            c["place"] = span or c["want"]

    # 变速段：source 模式按源帧数落地，并把整轨顺排（会推移后续片段）
    speed_rows = [c for c in plan if abs(c["speed"] - 1.0) > 1e-6]
    if speed_mode == "bake":
        # 占位与 slot 完全相同，只是把素材换成预渲染好的等长文件。
        # 复合片段内部的变速由 CreateCompoundClip 保留，不另行烘焙。
        for c in plan:
            # 停用段不渲染，预渲染它纯属浪费一次 ffmpeg 转码；占位照旧，
            # 落轨时用原素材即可（反正看不见）。
            if (c["kind"] == "video" and c.get("enabled", True)
                    and abs(c["speed"] - 1.0) > 1e-6):
                c["bake"] = True
    if speed_mode == "source":
        for c in speed_rows:
            if c["kind"] == "video":
                # 1x 播放「源区间」那么多内容，占位随之变长/变短（单位都是帧）
                c["place"] = max(1, int(round(c["want"] * c["speed"])))
        by_lane = {}
        for c in plan:
            by_lane.setdefault(c["lane"], []).append(c)
        shifted = []
        for lane, items in by_lane.items():
            items.sort(key=lambda x: x["rec"])
            cursor = None
            for c in items:
                old = c["rec"]
                if cursor is None:
                    cursor = old
                c["rec"] = cursor
                if c["rec"] != old:
                    shifted.append({"lane": lane, "segment_id": c["segment_id"],
                                    "from": old, "to": c["rec"]})
                cursor = c["rec"] + c["place"]
        plan.sort(key=lambda x: (x["lane"], x["rec"]))
    else:
        shifted = []

    # 同轨重叠：转场造成，用「后段起点收前段时长」消解（slot / bake）
    overlaps, trimmed = [], []
    if speed_mode in ("slot", "bake"):
        by_lane = {}
        for c in plan:
            by_lane.setdefault(c["lane"], []).append(c)
        for lane, items in by_lane.items():
            items.sort(key=lambda x: x["rec"])
            for a, b in zip(items, items[1:]):
                if b["rec"] < a["rec"] + a["place"]:
                    ov = a["rec"] + a["place"] - b["rec"]
                    overlaps.append({"lane": lane, "prev": a["segment_id"],
                                     "segment_id": b["segment_id"],
                                     "overlap_frames": ov,
                                     "transition": bool(a["has_transition"] or
                                                        b["has_transition"])})
                    new_place = b["rec"] - a["rec"]
                    if new_place <= 0:
                        overlaps[-1]["fatal"] = True
                        continue
                    a["place"] = new_place
                    trimmed.append({"lane": lane, "segment_id": a["segment_id"],
                                    "from": a["want"], "to": new_place,
                                    "cut_frames": a["want"] - new_place})
    plan.sort(key=lambda x: (x["lane"], x["rec"]))
    return plan, skipped, overlaps, trimmed, shifted


def media_index(mp):
    idx = {}
    for it in (mp.GetRootFolder().GetClipList() or []):
        p = it.GetClipProperty("File Path") or ""
        if p:
            idx[os.path.normcase(os.path.normpath(p))] = it
    return idx


def _key(p):
    return os.path.normcase(os.path.normpath(str(p)))


def purge_old(proj, mp, tag):
    """已废弃：不再自动删时间线。

    闸门现在要求工程必须是空白（一条时间线都没有），所以正常情况下没有
    「旧时间线」可删。而一旦用户绕过闸门（--no-gate）或工程里有上一轮
    调好色的 `JY · …`，自动删就等于删掉他的工作。同名冲突交给
    CreateEmptyTimeline 报错，让人自己决定。
    """
    return []


def timeline_clip_count(tl):
    n = 0
    for kind in ("video", "audio", "subtitle"):
        try:
            for lane in range(1, (tl.GetTrackCount(kind) or 0) + 1):
                n += len(tl.GetItemListInTrack(kind, lane) or [])
        except Exception:
            pass
    return n


def find_timeline(proj, name):
    for i in range(1, (proj.GetTimelineCount() or 0) + 1):
        tl = proj.GetTimelineByIndex(i)
        if tl and (tl.GetName() or "") == name:
            return tl
    return None


def create_timeline(mp, proj, base_name, max_try=6):
    """建一条空时间线：空的同名先删掉复用，有内容的重名就加序号。

    老写法直接 CreateEmptyTimeline，同名时只返回 None，报告里只能写
    「同名时间线可能还在」——用户看到的是「送入失败」，却分不清是重名、
    还是达芬奇根本没进工程。现在把每一步都记进 notes。

    有内容的同名时间线**绝不复用、绝不覆盖**：那可能是上一轮已经调过色的
    成果，宁可换个名字（`JY · 草稿 (2)`）。
    返回 (tl, used_name, tried, notes)。
    """
    tried, notes = [], []
    for i in range(max_try):
        name = base_name if i == 0 else "%s (%d)" % (base_name, i + 1)
        tried.append(name)
        old = find_timeline(proj, name)
        if old is not None:
            n = timeline_clip_count(old)
            if n == 0:
                ok = False
                try:
                    ok = bool(mp.DeleteTimelines([old]))
                except Exception as exc:
                    notes.append("空的同名时间线「%s」删不掉：%s" % (name, exc))
                if ok:
                    notes.append("清理了空时间线「%s」并复用这个名字" % name)
                else:
                    continue
            else:
                notes.append("「%s」已有 %d 个片段，换下一个名字" % (name, n))
                continue
        tl = mp.CreateEmptyTimeline(name)
        if tl:
            return tl, name, tried, notes
        notes.append("CreateEmptyTimeline(%s) 返回空" % name)
    return None, None, tried, notes


def mark_item(it, sid):
    try:
        return bool(it.AddMarker(0, "Blue", "JY", "", 1, sid))
    except TypeError:
        return bool(it.AddMarker(0, "Blue", "JY", sid))


def mark_disabled(placed):
    """剪映里停用的段 → 达芬奇里禁用的片段（占位、不出画面）。

    剪映的「停用」语义是留着位置不渲染，取消停用时时间轴不会动。达芬奇的
    TimelineItem.SetClipEnabled(False) 是同一件事。所以停用段不能跳过：跳过
    的话达芬奇时间线上那块变黑场，还会让后面每一段的位置核对全部错位。

    返回 (关成功的段数, 失败清单)。
    """
    done, failed = 0, []
    for p in placed:
        if p.get("enabled", True):
            continue
        hit, no_api = False, False
        for it in p["items"]:
            obj = it["item"]
            if not hasattr(obj, "SetClipEnabled"):
                no_api = True
                failed.append({"segment_id": p["segment_id"], "no_api": True,
                               "why": "这版达芬奇没有 SetClipEnabled，"
                                      "停用段在时间线上会出画面"})
                break
            try:
                hit = bool(obj.SetClipEnabled(False)) or hit
            except Exception as exc:
                failed.append({"segment_id": p["segment_id"],
                               "why": ("SetClipEnabled 调用失败：%s"
                                       % str(exc)[:80])})
        if no_api:
            continue
        if hit:
            done += 1
        elif not any(f["segment_id"] == p["segment_id"] for f in failed):
            failed.append({"segment_id": p["segment_id"],
                           "why": "SetClipEnabled 返回 False（这段在达芬奇里"
                                  "仍会出画面）"})
    return done, failed


def set_locks(tl, on=True):
    out = []
    for t in ("video", "audio"):
        for i in range(1, (tl.GetTrackCount(t) or 0) + 1):
            name = "%s%d" % (t[0].upper(), i)
            try:
                tl.SetTrackLock(t, i, bool(on))
                out.append({"track": name,
                            "locked": bool(tl.GetIsTrackLocked(t, i))})
            except Exception:
                out.append({"track": name, "locked": False, "error": True})
    return out


def _media_fps_frames(mi):
    try:
        mf = float(mi.GetClipProperty("FPS") or FPS)
    except Exception:
        mf = float(FPS)
    try:
        total = int(mi.GetClipProperty("Frames") or 0)
    except Exception:
        total = 0
    return mf, total


def _append(mp, mi, src, n, rec, lane):
    if n <= 0:
        return []
    return mp.AppendToTimeline([{"mediaPoolItem": mi, "startFrame": src,
                                 "endFrame": src + n,
                                 "recordFrame": rec,
                                 "trackIndex": lane}]) or []


def place_compound(mp, tl, c, idx, base, draft_id="", bake=False):
    """复合片段：内部片段摆到相邻轨道 → 立刻合成一项。

    bake=True 时，内部带变速的小段也先预渲染成等长素材（合成后再变速就
    没有办法了 —— 达芬奇不能给合成项设速度）。

    返回 (ok, items, actual_lane, detail)。
    """
    from . import retime as _retime
    items, parts_detail, fail = [], [], None
    lo, hi = c["rec"], c["rec"] + c["place"]
    for part in c["inner"]:
        mpath = part["path"]
        pspeed = float(part.get("speed") or 1.0)
        psrc = int(part.get("src_us") or 0)
        pspan = int(part.get("dur_us") or 0)
        baked_here = False
        if bake and abs(pspeed - 1.0) > 1e-6:
            want_f = max(1, us2f(pspan))
            out = _retime.cache_path(draft_id, part.get("segment_id") or
                                     Path(mpath).stem, pspeed, psrc, want_f,
                                     pspan)
            ok, d = _retime.bake(mpath, psrc, max(1, int(pspan * pspeed)),
                                 pspeed, want_f, out)
            if not ok:
                fail = "复合片段内部变速段预渲染失败：%s" % d.get("why")
                break
            if _key(out) not in idx:
                for im in (mp.ImportMedia([out]) or []):
                    p2 = im.GetClipProperty("File Path") or ""
                    if p2:
                        idx[_key(p2)] = im
            mpath, psrc, baked_here = out, 0, True
        mi = idx.get(_key(mpath))
        if mi is None:
            fail = "内部素材不在媒体池：%s" % Path(mpath).name
            break
        mf, total = _media_fps_frames(mi)
        p_rec = c["rec"] + us2f(part["rec_us"])
        p_place = us2f(part["dur_us"])
        p_rec = max(p_rec, lo)
        p_place = max(1, min(p_place, hi - p_rec))
        lane = c["lane"] + (part.get("lane") or 1) - 1
        src = int(round(psrc / 1e6 * mf))
        if total and src >= total:
            src = max(0, total - 1)
        n, tail, kind = solve_exact(p_place, mf)
        got = _append(mp, mi, src, n, p_rec, lane)
        if not got:
            fail = "内部片段摆放失败：%s" % Path(mpath).name
            break
        items.append(got[0])
        parts_detail.append({"path": mpath, "lane": lane, "rec": p_rec,
                             "dur": got[0].GetDuration(), "n": n, "tail": tail,
                             "kind": kind, "fps": mf, "src": src,
                             "speed": round(pspeed, 4), "baked": baked_here})
        if tail > 0:
            t_src = min(src + n, max(total - 1, 0)) if total else src + n
            tg = _append(mp, mi, t_src, tail,
                         p_rec + got[0].GetDuration(), lane)
            if tg:
                items.append(tg[0])
    if fail:
        return False, parts_detail, None, fail

    # 内部没铺满复合片段的占位：用首尾帧补齐，保证合成后长度 = 剪映占位
    if items:
        starts = [it.GetStart() for it in items]
        ends = [it.GetStart() + it.GetDuration() for it in items]
        if min(starts) > lo and parts_detail:
            gap = min(starts) - lo
            mi = idx.get(_key(parts_detail[0]["path"]))
            if mi is not None:
                tg = _append(mp, mi, parts_detail[0]["src"], gap, lo, c["lane"])
                if tg:
                    items.append(tg[0])
        if max(ends) < hi and parts_detail:
            gap = hi - max(ends)
            last = parts_detail[-1]
            mi = idx.get(_key(last["path"]))
            if mi is not None and gap > 0:
                t_src = last["src"] + max(0, last["n"] - 1)
                tg = _append(mp, mi, t_src, gap, max(ends), last["lane"])
                if tg:
                    items.append(tg[0])

    name = c.get("compound_name") or ("复合 %s" % str(c["segment_id"])[:6])
    cc = None
    try:
        cc = tl.CreateCompoundClip(items, {"name": name})
    except Exception as e:
        return False, parts_detail, None, "CreateCompoundClip 异常：%s" % e
    if cc is None:
        return False, parts_detail, None, "CreateCompoundClip 返回空"

    actual_lane = None
    for lane in range(1, (tl.GetTrackCount("video") or 0) + 1):
        for it in (tl.GetItemListInTrack("video", lane) or []):
            if it.GetStart() == cc.GetStart() and it.GetDuration() == cc.GetDuration():
                if it.GetName() == cc.GetName():
                    actual_lane = lane
                    break
        if actual_lane:
            break
    ok = (cc.GetStart() - base == c["rec"] and
          cc.GetDuration() == c["place"])
    return ok, [{"kind": "main", "start": cc.GetStart(),
                 "dur": cc.GetDuration(), "item": cc}], actual_lane, parts_detail


def place_video(mp, c, idx):
    """普通片段：主段 + 必要的 1 帧尾巴段。"""
    mi = idx.get(_key(c["path"]))
    if mi is None:
        return False, [], {"why": "素材不在媒体池"}
    mf, total = _media_fps_frames(mi)
    src = int(round(c["src_us"] / 1e6 * mf))
    if total and src >= total:
        src = max(0, total - 1)
    n, tail, kind = solve_exact(c["place"], mf)
    got = _append(mp, mi, src, n, c["rec"], c["lane"])
    if not got:
        return False, [], {"why": "主段 append 失败"}
    main = got[0]
    items = [{"kind": "main", "start": main.GetStart(),
              "dur": main.GetDuration(), "item": main}]
    detail = {"n": n, "tail": tail, "kind": kind, "fps": mf,
              "total": total, "src": src}
    if tail > 0:
        t_src = min(src + n, max(total - 1, 0)) if total else src + n
        tg = _append(mp, mi, t_src, tail, c["rec"] + main.GetDuration(), c["lane"])
        if tg:
            items.append({"kind": "pad", "start": tg[0].GetStart(),
                          "dur": tg[0].GetDuration(), "item": tg[0]})
    return True, items, detail


def run(tag=paths.TIMELINE_TAG, allow_stale=False, speed_mode="slot"):
    rep = {"ok": False, "action": "send", "speed_mode": speed_mode}
    if speed_mode == "bake":
        # 面板上的变速开关已删掉，默认就用 bake（唯一能让达芬奇里的帧序与剪映
        # 一致的落地方式）。万一这台机器没有 ffmpeg，别直接失败，退回 slot 并
        # 在报告里写清楚 —— 用户至少还能把片子送进去调色。
        from . import retime as _rt
        if not _rt.available():
            speed_mode = "slot"
            rep["speed_mode"] = "slot"
            rep["speed_fallback"] = "找不到 ffmpeg，变速段退回「占位对齐」模式"
            say("!! " + rep["speed_fallback"])
    draft, cur, fresh = draftio.read(allow_stale=allow_stale)
    rep["freshness"] = fresh
    rep["draft"] = cur.get("name")
    rep["draft_id"] = draft.get("id")
    run_dir = paths.draft_run_dir(draft.get("id"))

    feats = draftio.feature_counts(draft)
    rep["features"] = feats
    lay0 = draftio.layers(draft)
    say("草稿", cur.get("name"), "| 视频段", lay0.get("video", 0),
        "| 转场段", feats["segments_with_transition"],
        "| 蒙版段", feats["segments_with_mask"],
        "| 关键帧段", feats["segments_with_keyframes"],
        "| 变速段", feats["segments_with_speed"],
        "| 复合片段", feats["segments_with_compound"])
    # 蒙版：**完全不送**。达芬奇脚本接口没有任何蒙版入口（TimelineItem 的
    # 26 个属性与候选方法已逐个试过），所以这不是「失败」也不是「降级」，
    # 而是一条固定策略：不尝试、不阻断，只在报告里列清哪几段有蒙版。
    # 写回是从送入快照克隆的，蒙版原样留在剪映草稿里。
    _masks = draftio.mask_segments(draft)
    rep["masks"] = {
        "policy": "not_sent",
        "count": len(_masks),
        "note": "蒙版按固定策略不进达芬奇（脚本接口无蒙版入口）；"
                "写回时从送入快照原样带回剪映，不会丢。",
        "segments": _masks,
    }
    if _masks:
        say("蒙版：%d 段不送（%s）" % (
            len(_masks), "、".join(
                "%s@%.2fs" % ("/".join(m["mask_names"]) or "调节蒙版",
                              m["start_sec"]) for m in _masks[:3])
            + ("…" if len(_masks) > 3 else "")))

    # 画面变换（缩放/位移/旋转）：AppendToTimeline 不收这些参数，达芬奇会
    # 按原素材铺满项目分辨率。构图可能和剪映里看到的不一样，逐段报出来。
    _tf = draftio.transform_segments(draft)
    rep["transform_segments"] = _tf
    if _tf:
        parts = []
        for t in _tf[:3]:
            tf = t["transform"]
            if tf.get("scale"):
                desc = "%.2fx" % tf["scale"][0]
            elif tf.get("rotation"):
                desc = "旋转"
            elif tf.get("pos"):
                desc = "位移"
            else:
                desc = "裁切"
            parts.append("%s@%.2fs" % (desc, t["start_sec"]))
        say("画面变换：%d 段在剪映里缩放过/挪过位置，达芬奇按原始素材铺满，"
            "构图会不同（%s）"
            % (len(_tf), "、".join(parts) + ("…" if len(_tf) > 3 else "")))

    # 送入这一刻的草稿整份存成快照：之后你在剪映里怎么改，写回都按这一版走。
    # 不存的话，写回只能拿「当前草稿」当底 —— 而那时剪映里可能已经剪过一轮了。
    from . import snapshot as _snap
    snap_meta = _snap.save(draft, cur, run_dir,
                           extra={"speed_mode": speed_mode})
    rep["snapshot"] = {"at": snap_meta["saved_at_text"],
                       "sha": snap_meta["sha"],
                       "video_segments": snap_meta["video_segments"],
                       "compounds": snap_meta["compounds"],
                       "subdrafts": snap_meta["subdrafts"]}
    say("已存快照：%s 段 / 复合 %d 个（写回以这一版为准）"
        % (snap_meta["video_segments"], snap_meta["compounds"]))
    # 复合片段的子草稿是绝对路径，必须复制进快照才算自包含。复制失败时
    # 快照里那条路径仍指向**原草稿**：原草稿一删/一改，写回出来的复合片段
    # 就是坏的。这条以前只记在 meta 里没人看，等于静默降级。
    bad_subs = [x for x in (snap_meta.get("subdrafts") or []) if not x.get("ok")]
    if bad_subs:
        rep["snapshot_warnings"] = bad_subs
        say("!! 快照里有 %d 个复合片段的子草稿没能复制（写回可能取不到内部时间线）"
            % len(bad_subs))

    plan, skipped, overlaps, trimmed, shifted = build_plan(draft, speed_mode)
    if not plan:
        rep["error"] = "没有可摆放的片段"
        rep["skipped"] = skipped
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep
    rep["overlaps"] = overlaps
    rep["trimmed"] = trimmed
    rep["shifted"] = shifted
    rep["skipped"] = skipped
    # 草稿里「有但不送」的元素，逐项列清。用户不用猜哪些东西没进来。
    rep["not_sent"] = draftio.unsupported(draft)
    if rep["not_sent"]:
        say("不送：" + "、".join("%s %d" % (r["label"], r["count"])
                               for r in rep["not_sent"]))
    # 复合片段内部的停用段：合成时会不会跟着变灰没实测过，先说清楚
    comp_off = sum(int(c.get("inner_disabled") or 0) for c in plan)
    if comp_off:
        rep["compound_disabled_warning"] = comp_off
        say("!! 复合片段内部有 %d 段是停用的：达芬奇合成后可能仍会显示，"
            "送完请看一眼" % comp_off)
    if any(o.get("fatal") for o in overlaps):
        rep["error"] = ("同轨有整段被覆盖的情况（重叠 ≥ 后段全长），无法摆放。"
                        "已中止，未改动时间线。")
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep
    if trimmed:
        say("转场消解：%d 段收短 %d 帧（起始帧不变）"
            % (len(trimmed), sum(t["cut_frames"] for t in trimmed)))
    if shifted:
        say("变速 source 模式：%d 段位置后移（按源帧数落地）" % len(shifted))

    baked, bfail = [], []
    if speed_mode == "bake":
        from . import retime
        todo = [c for c in plan if c.get("bake")]
        if todo:
            say("预渲染变速段 %d 段（ProRes 422 10bit / 30fps / 等长）…" % len(todo))
        baked, bfail = retime.bake_plan(plan, draft.get("id"))
        rep["baked"] = baked
        for b in baked:
            say("  · %.3fx %3d 帧 %s" % (b["speed"], b["want"],
                                        "复用" if b["cached"] else "已生成"))
        if bfail:
            rep["error"] = ("变速段预渲染失败（%d 段），已中止，未改动时间线：%s"
                            % (len(bfail), bfail[0].get("why")))
            rep["bake_failed"] = bfail
            draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
            return rep

    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        rep["error"] = "达芬奇没有响应脚本接口"
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep
    # 停在项目管理器时 GetCurrentProject() 会给一个幻影工程（Untitled
    # Project / 0 条时间线），后面 CreateEmptyTimeline 静默返回 None。
    # 先认页面，比事后猜「同名时间线还在」准得多。
    page = None
    try:
        page = resolve.GetCurrentPage()
    except Exception as exc:
        say("GetCurrentPage 失败：%s" % exc)
    rep["page"] = page
    if not page:
        rep["error"] = ("达芬奇停在项目管理器，还没有进入任何工程，脚本接口"
                        "不能建时间线。请在达芬奇里打开一个空白工程"
                        "（或「文件 → 新建工程」），再重新送入。")
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep
    proj = resolve.GetProjectManager().GetCurrentProject()
    if not proj:
        # 闸门正常情况下已经拦住了；这是给 --no-gate 排错模式兜底，
        # 否则下一行 proj.GetName() 直接 AttributeError，看不出所以然。
        rep["error"] = "达芬奇里没有打开工程（项目管理器返回空）"
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep
    rep["project"] = proj.GetName()
    # 工程设置只在空工程里改。已有本工具的时间线时改帧率/分辨率，会让那些
    # 时间线整体重算（已调好的片段位置会变），所以宁可不动。
    n_tl_before = proj.GetTimelineCount() or 0
    if not n_tl_before:
        for key, val in (("timelineFrameRate", str(FPS)),
                         ("timelineResolutionWidth", "1920"),
                         ("timelineResolutionHeight", "1080")):
            try:
                proj.SetSetting(key, val)
            except Exception as exc:
                say("SetSetting %s 失败：%s" % (key, exc))
    else:
        rep["settings_kept"] = ("工程里已有 %d 条时间线，未改工程帧率/分辨率"
                                "（免得已调色的时间线重算）" % n_tl_before)
        say("· " + rep["settings_kept"])

    mp = proj.GetMediaPool()
    rep["purged"] = purge_old(proj, mp, tag)

    tl, _used, tried, notes = create_timeline(
        mp, proj, "%s · %s" % (tag, cur.get("name") or "draft"))
    rep["timeline_tried"] = tried
    rep["timeline_notes"] = notes
    if not tl:
        rep["error"] = ("建不出空时间线（试过：%s）。工程「%s」里已有 %d 条"
                        "时间线。请换一个空白工程再送入。"
                        % ("、".join(tried), proj.GetName(),
                           proj.GetTimelineCount() or 0))
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep
    for n in notes:
        say("· " + n)
    proj.SetCurrentTimeline(tl)

    base = 0
    try:
        if tl.SetStartTimecode("00:00:00:00"):
            time.sleep(0.4)
            base = int(tl.GetStartFrame() or 0)
    except Exception as e:
        say("SetStartTimecode warn", e)
    rep["timeline_start_frame"] = base
    rep["timeline"] = tl.GetName()
    say("时间线", tl.GetName(), "| 起始帧", base)
    # 占位登记：从这里开始 pull/write 能找到「最近一次送入」的上下文。
    # 先标 committed=False，走完全程才 commit —— 半截失败的时间线不能被当成
    # 可写回的版本。
    _snap.claim(snap_meta, run_dir, tl.GetName(), base, len(plan))

    lanes = sorted({c["lane"] for c in plan})
    inner_depth = max([len({(p.get("lane") or 1) for p in (c.get("inner") or [])})
                       or 0 for c in plan] or [0])
    need_lanes = max(max(lanes), max(lanes) + inner_depth - 1)
    while (tl.GetTrackCount("video") or 0) < need_lanes:
        if not tl.AddTrack("video"):
            break
    while (tl.GetTrackCount("audio") or 0) < need_lanes:
        if not tl.AddTrack("audio"):
            break

    idx = media_index(mp)
    need = set()
    for c in plan:
        if c["kind"] == "video":
            need.add(c["path"])
        else:
            need |= {p["path"] for p in c["inner"]}
    missing = [p for p in need if _key(p) not in idx]
    if missing:
        for it in (mp.ImportMedia(missing) or []):
            p = it.GetClipProperty("File Path") or ""
            if p:
                idx[_key(p)] = it

    # 先复合片段（合成瞬间腾空临时轨道），再普通片段
    order = ([c for c in plan if c["kind"] == "compound"]
             + [c for c in plan if c["kind"] == "video"])
    placed, failed = [], []
    for c in order:
        if c["kind"] == "compound":
            ok, items, lane_actual, detail = place_compound(
                mp, tl, c, idx, base, draft_id=draft.get("id"),
                bake=(speed_mode == "bake"))
            if not ok:
                failed.append({**c, "why": detail if isinstance(detail, str)
                               else "复合片段摆放失败"})
                continue
            main = items[0]["item"]
            placed.append({
                "segment_id": c["segment_id"], "kind": "compound",
                "lane": lane_actual or c["lane"], "lane_planned": c["lane"],
                "rec": c["rec"], "want": c["want"], "place": c["place"],
                "speed": c["speed"], "parts": detail,
                "enabled": c.get("enabled", True),
                "items": items, "got_start": main.GetStart() - base,
                "got_dur": main.GetDuration(), "name": main.GetName(),
                "compound_name": c.get("compound_name"),
            })
        else:
            ok, items, detail = place_video(mp, c, idx)
            if not ok:
                failed.append({**c, "why": detail.get("why")})
                continue
            main = items[0]["item"]
            placed.append({
                "segment_id": c["segment_id"], "kind": "video",
                "lane": c["lane"], "lane_planned": c["lane"],
                "rec": c["rec"], "want": c["want"], "place": c["place"],
                "speed": c["speed"], "baked": bool(c.get("bake")),
                "enabled": c.get("enabled", True),
                "items": items,
                "got_start": main.GetStart() - base,
                "got_dur": sum(i["dur"] for i in items),
                "name": main.GetName(),
                **{k: detail.get(k) for k in ("n", "tail", "fps", "total", "src")},
                "solve": detail.get("kind"),
            })
        time.sleep(0.02)

    # 身份标记
    inked, mk_fail = 0, []
    for p in placed:
        for it in p["items"]:
            if it["start"] is None:
                mk_fail.append(p["segment_id"] + ":pad")
                continue
            tid = (p["segment_id"] if it["kind"] == "main"
                   else p["segment_id"] + PAD)
            try:
                ok = mark_item(it["item"], tid)
            except Exception:
                ok = False
            if ok and it["kind"] == "main":
                inked += 1
            elif not ok:
                mk_fail.append(tid)

    # 停用段：照样占了轨道，但要让达芬奇也别渲染它。
    dis_ok, dis_fail = mark_disabled(placed)
    if dis_ok:
        say("· 停用段 %d 段已按「禁用片段」送入（位置保留、不出画面）" % dis_ok)

    lay = draftio.layers(draft)
    note = ("只含视频轨与内嵌音频，全部轨道已上锁（改前先解锁）。"
            "文字 %d 条、贴纸 %d 条、转场 %d 段、关键帧 %d 段未传入；"
            "写回剪映时从原草稿原样保留。复合片段 %d 个已按内部时间线展开后"
            "重新合成。"
            % (lay.get("text", 0), lay.get("sticker", 0),
               feats["segments_with_transition"],
               feats["segments_with_keyframes"],
               feats["segments_with_compound"]))
    if dis_ok:
        note += ("剪映里停用的 %d 段已按「禁用片段」送入（位置保留、不出画面）。"
                 % dis_ok)
    try:
        tl.AddMarker(0, "Purple", "BRIDGE", note, 1)
    except Exception:
        pass
    rep["note_marker"] = note

    rep["locks"] = set_locks(tl, True)
    try:
        resolve.OpenPage("color")
        time.sleep(0.5)
    except Exception:
        pass

    exact = [p for p in placed
             if p["got_dur"] == p["place"] and p["got_start"] == p["rec"]]
    end = max((p["rec"] + p["place"]) for p in placed)
    speed_rows = []
    for p in placed:
        if abs(p["speed"] - 1.0) <= 1e-6:
            continue
        row = {"segment_id": p["segment_id"], "kind": p["kind"],
               "speed": round(p["speed"], 4), "want": p["want"],
               "place": p["place"], "got_dur": p["got_dur"],
               "curve_speed": bool(p.get("curve_speed"))}
        if p["kind"] == "compound":
            row["presentation"] = "复合片段整体速度，合成后按 1x"
            speed_rows.append(row)
            continue
        # 剪映这段实际吃掉的源帧数（时间线帧）
        rng = next((us2f(c.get("range_us")) for c in plan
                    if c["segment_id"] == p["segment_id"]), 0)
        row["range_frames"] = rng
        if p.get("baked") or any(b["segment_id"] == p["segment_id"]
                                 for b in baked):
            row["presentation"] = (
                "已按平均速度 %.3fx 预渲染；曲线节奏被拉平"
                "（剪映这段是变速曲线，帧位/总长对得上，观感会不一样）"
                % p["speed"] if row["curve_speed"] else
                "已预渲染（真变速，内容与剪映一致）")
        elif rng and p["place"] < rng:
            row["truncated_frames"] = rng - p["place"]
            row["presentation"] = ("1x 播放，尾部 %d 帧内容缺失（改用 --speed "
                                   "bake 可还原）" % (rng - p["place"]))
        elif rng and p["place"] > rng:
            row["overrun_frames"] = p["place"] - rng
            row["presentation"] = ("1x 播放，越界读出点之后 %d 帧画面（改用 "
                                   "--speed bake 可还原）" % (p["place"] - rng))
        else:
            row["presentation"] = "1x 播放（源区间恰好等于占位）"
        speed_rows.append(row)
    rep.update({
        "timeline_end": (tl.GetEndFrame() or 0) - base,
        "expected_end": end,
        "v_tracks": tl.GetTrackCount("video"),
        "a_tracks": tl.GetTrackCount("audio"),
        "placed": len(placed), "exact": len(exact),
        "mismatch": [p for p in placed if p not in exact],
        "failed": failed,
        "speed_segments": speed_rows,
        "speed_note": ("Resolve 脚本接口不能设置片段速度（实测 SetProperty("
                       "Speed) 返回 False、11 个候选方法全为 None）；"
                       + ("变速段已预渲染成等长 ProRes 素材，达芬奇里是真变速"
                          if speed_mode == "bake" else
                          "变速段在达芬奇里按 1x 呈现，时长按占位对齐"
                          if speed_mode == "slot" else
                          "变速段按源帧数 1x 摆放，本轨后续片段已整体后移")),
        "markers": inked, "marker_fail": mk_fail,
        "disabled": dis_ok,
        "disabled_failed": [f for f in dis_fail if not f.get("no_api")],
        "disabled_unsupported": any(f.get("no_api") for f in dis_fail),
        "kinds": {k: sum(1 for p in placed if p["kind"] == k)
                  for k in {p["kind"] for p in placed}},
    })
    rep["ok"] = (len(placed) == len(plan) and not failed
                 and inked == len(placed) and len(exact) == len(placed)
                 and rep["timeline_end"] == end
                 and not rep["disabled_failed"])

    draftio.dump_json({
        "timeline": tl.GetName(), "fps": FPS, "start_frame": base,
        "draft_id": draft.get("id"), "draft_name": cur.get("name"),
        "speed_mode": speed_mode,
        "segments": [{"segment_id": p["segment_id"], "lane": p["lane"],
                      "kind": p["kind"],
                      "rec": p["rec"], "dur": p["place"],
                      "want": p["want"], "speed": p["speed"],
                      "items": [{k: v for k, v in i.items() if k != "item"}
                                for i in p["items"]]}
                     for p in placed],
    }, os.path.join(run_dir, "plan.json"))
    if rep["ok"]:
        # 只有整条时间线摆完、标记也全都写上了，才让这份快照成为「可写回的那版」
        _snap.commit(timeline_end=rep["timeline_end"],
                     placed=rep["placed"], speed_mode=speed_mode)
    else:
        rep["snapshot_committed"] = False
    draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
    return rep
