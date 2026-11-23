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
import time
from pathlib import Path

from . import compat, draftio, paths, resolve

# 延迟加载：没装达芬奇的机器照样能 import 本模块（check / plan 不需要 API）。
dvr = resolve.lazy()

FPS = paths.FPS
PAD = paths.PAD_SUFFIX

# timings 里的键 → 给用户看的名字（面板/命令行都直接念这一行）
_T_LABEL = {"read": "读草稿", "snapshot": "存快照", "bake": "预渲染",
            "media": "导素材", "place": "落轨", "finish": "标记与上锁"}


def say(*a):
    print(*a, flush=True)


def _mb(n):
    n = float(n or 0)
    return "%.0f MB" % (n / 1e6) if n >= 1e6 else "%.0f KB" % (n / 1e3)


def _prune_retime(draft, speed_mode, say=None):
    """送入成功后，清掉这份草稿上一版留下的变速中转素材。

    这些文件在 `retime/<草稿ID>/` 里按「段 + 速度 + 源区间」命名：改一次倍速
    就多一个文件、旧的不删。而 gc 是按目录整体回收的，正在用的这份草稿整个
    目录受保护（写回要读它的快照）——不在这里收，就永远收不到。1080p ProRes
    约 18~22 MB/s，一分钟的段就是 1.2 GB，攒十版十几个 GB。

    只删本次没用到的，时间线上摆的那几个文件原样留着。失败只记日志：清缓存
    出错不该让一次成功的送入变成失败。
    """
    if speed_mode != "bake":
        return {"files": 0, "bytes": 0}
    try:
        from . import retime
        files, freed = retime.prune_unused(draft.get("id"))
    except Exception as e:
        paths.note("prune retime failed: %r" % (e,))
        return {"files": 0, "bytes": 0}
    if files and say:
        say("· 清掉上一版的中转素材 %d 个（%s，只留这条时间线正在用的）"
            % (files, _mb(freed)))
    if files:
        paths.note("prune retime: %d 个 / %d B" % (files, freed))
    return {"files": files, "bytes": freed}


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
                # 图片段（materials.photos）。达芬奇能收静帧，但静帧的时长
                # 摆不进剪映的时长（见 retime.photo_clip 的实测结论），所以
                # 落轨前先转成等长视频 —— 计划里仍按图片记录，落轨时再转。
                ph = photos.get(sg.get("material_id"))
                if ph:
                    pp = str(ph.get("path") or "").replace("\\", "/")
                    if not pp:
                        skipped.append({"segment_id": sg.get("id"),
                                        "reason": "图片段没有文件路径"})
                        continue
                    if not Path(pp).exists():
                        skipped.append({"segment_id": sg.get("id"), "path": pp,
                                        "reason": "图片不在盘上"})
                        continue
                    base.update({
                        "kind": "photo", "path": pp,
                        "photo_type": str(ph.get("type") or ""),
                        # 剪映里图片段也可能有缩放/位移，apply_transform 一样适用
                        "photo_px": [ph.get("width"), ph.get("height")],
                    })
                    plan.append(base)
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
        if c["kind"] in ("video", "photo"):
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
    """规范化路径 → 媒体池片段。

    根目录和当前目录都要看：ImportMedia 导进的是**当前目录**，只扫根目录
    会在「素材分了文件夹」的工程里把已导入的片段判成「不在池里」。
    """
    return {k: c for k, c in compat.pool_clips(mp) if k}


def _key(p):
    return os.path.normcase(os.path.normpath(str(p)))


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
    成果，宁可换个名字（`CP · 草稿 (2)`）。
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
                if compat.delete_timelines(mp, [old]):
                    notes.append("清理了空时间线「%s」并复用这个名字" % name)
                else:
                    notes.append("空的同名时间线「%s」删不掉，换下一个名字" % name)
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
    """在片段上写段号：6 参数，段号放在 customData 里（21.2 的签名）。

    段号必须落在 customData，不能塞进 note —— note 是给用户看的，
    `pull.seg_marker()` 只认 customData，塞错位置等于没写。

    同一帧已经有标记时 `AddMarker` 返回 False（不覆盖）。这时改用
    `UpdateMarkerCustomData` 把原来的改掉 —— 否则「这一段叫什么」取决于谁先
    写，重送一次就可能残留上一轮的名字。
    """
    try:
        if it.AddMarker(0, "Blue", paths.MARKER_NAME, "", 1, sid):
            return True
    except Exception:
        pass
    # 这一帧已经有标记（AddMarker 拒绝覆盖）：改成直接改那一枚的 customData。
    upd = getattr(it, "UpdateMarkerCustomData", None)
    if not callable(upd):
        return False
    try:
        return bool(upd(0, sid))
    except Exception:
        return False


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
            if not compat.alive(obj, "SetClipEnabled"):
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


def _append(mp, mi, src, n, rec, lane, media_type=None):
    """往时间线上摆一段，返回**真的摆上去**的片段。

    21.2 摆不上时返回 `[None]`（长度 1），直接信返回值会在下一行
    `got[0].GetStart()` 上炸 AttributeError。统一过 compat.real_items()。

    media_type=2 表示「只取声音」：实测 21.2 认这个键，摆完之后视频轨不变、
    音频落进音轨；不传则视频进去的同时声音也自动进同号音轨（这就是视频段
    自带原声的来源）。
    """
    if n <= 0:
        return []
    spec = {"mediaPoolItem": mi, "startFrame": src, "endFrame": src + n,
            "recordFrame": rec, "trackIndex": lane}
    if media_type:
        spec["mediaType"] = int(media_type)
    return compat.real_items(mp.AppendToTimeline([spec]))


def place_compound(mp, tl, c, idx, base, draft_id="", bake=False,
                   size=(1920, 1080)):
    """复合片段：内部片段摆到相邻轨道 → 立刻合成一项。

    bake=True 时，内部带变速的小段也先预渲染成等长素材（合成后再变速就
    没有办法了 —— 达芬奇不能给合成项设速度）。size 是落画布用的，和
    主流程的 bake_plan 同一套缩放规则。

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
                                     pspan, size)
            ok, d = _retime.bake(mpath, psrc, max(1, int(pspan * pspeed)),
                                 pspeed, want_f, out, size=size)
            if not ok:
                fail = "复合片段内部变速段预渲染失败：%s" % d.get("why")
                break
            if _key(out) not in idx:
                # 刚预渲染出来的临时素材，导进来之后按媒体池实际内容重建索引
                # （21.2 的 ImportMedia 可能不返回片段，见 compat.import_media）
                compat.import_media(mp, [out])
                idx.update(media_index(mp))
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
        return False, [], {"why": ("主段摆不上（V%d 第 %d 帧起 %d 帧）："
                                  "轨道锁着、素材离线，或这个位置已被占用"
                                  % (c["lane"], c["rec"], n))}
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


def place_photo(mp, c, idx, draft_id="", fps=FPS, size=(1920, 1080),
                clip=None, cached=None):
    """图片段：先把静帧转成等长视频，再当普通素材摆上去。

    不能直接摆静帧 —— 21.2 会忽略 endFrame，恒定给 150 帧（5 秒），摆完
    也改不了（详见 retime.photo_clip 的实测记录）。转码结果按「源图 + 目标
    帧数 + 画布」缓存，同一张图重复送入不重复编码。

    clip 有值表示 run() 已经批量转好并导进媒体池了（大批图片段走这条）；
    这时这里只负责摆，不再转码、也不重建媒体池索引。
    """
    from . import retime as _retime
    if clip is None:
        out = _retime.photo_cache_path(draft_id, c["path"], c["place"], fps,
                                       size[0], size[1])
        ok, d = _retime.photo_clip(c["path"], c["place"], fps=fps,
                                   width=size[0], height=size[1], out_path=out)
        if not ok:
            return False, [], {"why": d.get("why") or "图片转码失败"}
        cached = d.get("cached")
        if _key(out) not in idx:
            compat.import_media(mp, [out])
            idx.update(media_index(mp))
    else:
        out = clip
    mi = idx.get(_key(out))
    if mi is None:
        return False, [], {"why": "转好的图片素材导不进媒体池"}
    mf, total = _media_fps_frames(mi)
    got = _append(mp, mi, 0, c["place"], c["rec"], c["lane"])
    if not got:
        return False, [], {"why": ("图片段摆不上（V%d 第 %d 帧起 %d 帧）："
                                  "轨道锁着或位置已被占用"
                                  % (c["lane"], c["rec"], c["place"]))}
    main = got[0]
    return True, [{"kind": "main", "start": main.GetStart(),
                   "dur": main.GetDuration(), "item": main}], {
        "clip": out, "cached": cached, "fps": mf, "total": total,
        "src_px": c.get("photo_px"), "photo_type": c.get("photo_type")}


def build_audio_plan(draft):
    """剪映音频轨 → 达芬奇 audio 轨的摆放计划，返回 (plan, skipped)。

    `video_original_sound` 跳过：那是视频自带的原声，AppendToTimeline 摆到
    视频轨时已经自动把声音落到 A1（21.2 实测），再单独送一遍会叠成两份。

    音量、淡入淡出、降噪这些**不搬** —— 脚本接口没有可靠入口，宁可在报告里
    写清楚，也不要搬一半。写回时这些设置还在原草稿的段上，不会丢。
    """
    mats = draft.get("materials") or {}
    auds = {}
    for a in mats.get("audios") or []:
        if isinstance(a, dict) and a.get("id"):
            auds[a["id"]] = a
    tracks = [t for t in (draft.get("tracks") or [])
              if isinstance(t, dict) and t.get("type") == "audio"]
    plan, skipped = [], []
    for lane, tr in enumerate(tracks, start=1):
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            au = auds.get(sg.get("material_id")) or {}
            tt = sg.get("target_timerange") or {}
            rec, want = us2f(tt.get("start")), us2f(tt.get("duration"))
            row = {"segment_id": sg.get("id"), "lane": lane, "rec": rec,
                   "want": want, "path": str(au.get("path") or "").replace("\\", "/"),
                   "name": str(au.get("name") or ""),
                   "type": str(au.get("type") or ""),
                   "volume": au.get("volume")}
            if want <= 0:
                skipped.append({**row, "why": "时长为 0"})
                continue
            if row["type"] == "video_original_sound":
                skipped.append({**row, "why": "视频原声（已随视频段自动进入 A1）"})
                continue
            if not row["path"]:
                skipped.append({**row, "why": "音频段没有文件路径"})
                continue
            if not Path(row["path"]).exists():
                skipped.append({**row, "why": "音频不在盘上"})
                continue
            plan.append(row)
    return plan, skipped


def _free_audio_lane(tl, rec, n, want_lane):
    """挑一条在这段区间里没被占用的音轨。

    视频摆到视频轨时原声会自动落在 A1，所以剪映的第 1 条音轨不能直接对应
    达芬奇的第 1 条 —— 直接按序号摆会和原声挤在一起，AppendToTimeline 会把
    已有片段顶开或干脆摆不上。
    """
    lane = max(1, int(want_lane or 1))
    for _ in range(16):
        if lane > (tl.GetTrackCount("audio") or 0):
            if not tl.AddTrack("audio"):
                return lane
        busy = False
        for it in (tl.GetItemListInTrack("audio", lane) or []):
            s, d = it.GetStart(), it.GetDuration()
            if s < rec + n and rec < s + d:
                busy = True
                break
        if not busy:
            return lane
        lane += 1
    return lane


def place_audio(mp, tl, rows, idx):
    """音频段：按剪映的位置和时长摆到达芬奇的音轨上。

    时长换算和视频段是同一套（源帧数 / 源 fps × 时间线 fps）。差别是**不能
    补尾帧**：21.2 对超长区间会「末帧保持」，视频段补一帧还能用，音频补出来
    是静音，听着像断了一截。所以这里把帧数 clamp 到素材实际长度，短一帧就
    短一帧，报告里照实写。
    """
    placed, failed = [], []
    for c in rows:
        mi = idx.get(_key(c["path"]))
        if mi is None:
            failed.append({**c, "why": "音频不在媒体池"})
            continue
        mf, total = _media_fps_frames(mi)
        n, tail, kind = solve_exact(c["want"], mf)
        if total:
            n = min(n, max(1, total))
        lane = _free_audio_lane(tl, c["rec"], n, c["lane"])
        # mediaType=2：只要声音。实测 21.2 认这个键 —— 摆完之后视频轨不变，
        # 音频落进音轨；不传的话视频也会落进 V 轨，把画面搞乱。
        got = _append(mp, mi, 0, n, c["rec"], lane, media_type=2)
        if not got:
            failed.append({**c, "why": "音轨 A%d 摆不上（第 %d 帧起 %d 帧）"
                                      % (lane, c["rec"], n)})
            continue
        it = got[0]
        actual = lane
        try:
            at = it.GetTrackTypeAndIndex()
            if isinstance(at, (list, tuple)) and len(at) > 1:
                actual = int(at[1])
        except Exception:
            pass
        placed.append({**c, "lane_actual": actual, "lane_planned": lane,
                       "n": n, "tail": tail, "kind": kind, "fps": mf,
                       "total": total, "got_start": it.GetStart(),
                       "got_dur": it.GetDuration()})
    return placed, failed


def apply_transform(placed, transforms):
    """把剪映的**缩放**搬到时间线片段上，返回 (成功的, 只报了没搬的)。

    为什么能做：21.2 的 `TimelineItem.SetProperty` 对 ZoomX / ZoomY / Pan /
    Tilt / RotationAngle 都返回 True，也能用 GetProperty 读回（实测件
    `GetProperties()` 32 个键）。`AppendToTimeline` 不收这些参数，但摆完之后
    单独设是有效的。

    为什么只搬缩放：缩放两边都是「倍数、绕画面中心」，语义一致，1.491 就是
    1.491x。位移（剪映的 x/y 是相对画布的比例，达芬奇的 Pan 是另一套单位）
    和旋转（正方向可能相反）没有肉眼校准过，宁可不动 —— 猜错的构图比不变的
    构图更难查。没有搬的照样在报告里列出来。
    """
    by_sid = {t.get("segment_id"): t for t in (transforms or [])}
    applied, skipped = [], []
    for p in placed:
        t = by_sid.get(p["segment_id"])
        if not t:
            continue
        tf = t.get("transform") or {}
        scale = list(tf.get("scale") or [])
        others = [k for k in ("pos", "rotation", "crop") if tf.get(k)]
        items = p.get("items") or []
        obj = items[0].get("item") if items else None
        if obj is None or len(scale) < 2:
            skipped.append({"segment_id": p["segment_id"], "why": "取不到片段"})
            continue
        if not compat.alive(obj, "SetProperty"):
            skipped.append({"segment_id": p["segment_id"],
                            "why": "这版没有 SetProperty"})
            continue
        x, y = float(scale[0]), float(scale[1])
        row = {"segment_id": p["segment_id"], "lane": p["lane"],
               "rec": p["rec"], "zoom": [round(x, 4), round(y, 4)],
               "untouched": others}
        try:
            okx = bool(obj.SetProperty("ZoomX", x))
            oky = bool(obj.SetProperty("ZoomY", y))
            row["set"] = [okx, oky]
            row["read_back"] = [obj.GetProperty("ZoomX"), obj.GetProperty("ZoomY")]
        except Exception as exc:
            row["why"] = "%s: %s" % (type(exc).__name__, str(exc)[:80])
            skipped.append(row)
            continue
        if okx and oky:
            applied.append(row)
        else:
            skipped.append(row)
    return applied, skipped


def run(tag=paths.TIMELINE_TAG, allow_stale=False, speed_mode="slot",
        with_audio=True):
    # baked 一开始就占位：没有 ffmpeg 时流程走不到预渲染，报告字段也要稳定存在，
    # 否则调用方（面板、上层脚本）得为「有没有这个键」分两种情况写。
    rep = {"ok": False, "action": "send", "speed_mode": speed_mode, "baked": []}
    ok, why = compat.version_ok()
    if ok is False:
        rep["error"] = why
        return rep
    if speed_mode == "bake":
        # 面板不再给变速开关，一律 bake（唯一能让达芬奇里的帧序与剪映一致的
        # 落地方式）。ffmpeg 是随包自带的，正常机器上这一段永远不会触发；
        # 真缺了（杀毒软件删了 / 安装残缺）也别直接失败 —— 退回 slot 把片子
        # 送进去，让用户至少能调色，同时在报告和面板上把原因说成人话。
        from . import retime as _rt
        if not _rt.available():
            speed_mode = "slot"
            rep["speed_mode"] = "slot"
            rep["speed_fallback"] = ("变速段只能按「占位对齐」摆放（加速段会缺"
                                     "尾部画面、慢放段会串帧）。"
                                     + _rt.missing_reason())
            say("!! " + rep["speed_fallback"])
    t_run = time.time()
    marks = {}

    def _lap(name, since):
        """记一段耗时。为什么要有它：用户看到的只有「一分多钟」，而这一分钟
        到底花在读草稿、预渲染、导素材还是落轨上，以前只能靠猜。写进报告的
        timings 之后，下一次说「还是慢」就能直接指出是哪一段。
        """
        marks[name] = round(time.time() - since, 1)
        return time.time()

    def _timings():
        out = dict(marks)
        out["total"] = round(time.time() - t_run, 1)
        return out

    t = time.time()
    draft, cur, fresh = draftio.read(allow_stale=allow_stale)
    _lap("read", t)
    rep["freshness"] = fresh
    rep["draft"] = cur.get("name")
    rep["draft_id"] = draft.get("id")
    run_dir = paths.draft_run_dir(draft.get("id"))

    def _fail(why, **extra):
        rep["timings"] = _timings()
        return draftio.fail(rep, os.path.join(run_dir, "send.json"), why, **extra)


    feats = draftio.feature_counts(draft)
    rep["features"] = feats
    lay0 = draftio.layers(draft)
    say("草稿", cur.get("name"), "| 视频段", lay0.get("video", 0),
        "| 转场段", feats["segments_with_transition"],
        "| 蒙版段", feats["segments_with_mask"],
        "| 关键帧段", feats["segments_with_keyframes"],
        "| 变速段", feats["segments_with_speed"],
        "| 复合片段", feats["segments_with_compound"])
    if not lay0.get("video"):
        # 只有音频/文字/贴纸的草稿（或者干脆是空草稿）送进去也摆不上一段，
        # 以前要等建完时间线、导完素材才在「一段都没能摆上」那里报「原因未知」。
        # 当场拦下，并说清是哪一种，用户才知道是不是开错了草稿。
        other = "、".join("%s %d 段" % (k, v) for k, v in
                         (("音频", lay0.get("audio", 0)),
                          ("文字", lay0.get("text", 0)),
                          ("贴纸", lay0.get("sticker", 0)),
                          ("特效", lay0.get("effect", 0))) if v)
        return _fail("这份草稿里没有视频片段，没有可送进达芬奇的内容。"
                     + ("（只有 %s）" % other if other else "（草稿是空的）")
                     + " 请在剪映里确认打开的是要调色的那份草稿。")
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
        say("画面变换：%d 段在剪映里缩放过/挪过位置（缩放按比例送入，位移与"
            "旋转不送，构图可能不同）（%s）"
            % (len(_tf), "、".join(parts) + ("…" if len(_tf) > 3 else "")))

    # 送入这一刻的草稿整份存成快照：之后你在剪映里怎么改，写回都按这一版走。
    # 不存的话，写回只能拿「当前草稿」当底 —— 而那时剪映里可能已经剪过一轮了。
    from . import snapshot as _snap
    t = time.time()
    snap_meta = _snap.save(draft, cur, run_dir,
                           extra={"speed_mode": speed_mode})
    _lap("snapshot", t)
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
        why = "；".join(sorted({str(s.get("reason") or "") for s in skipped
                               if s.get("reason")}))[:120]
        return _fail("这份草稿里的视频段一个都没能进入送入计划%s。"
                     "如果它们在剪映里都能正常播放，请把这份草稿反馈给我们。"
                     % (("：" + why) if why else ""), skipped=skipped)
    # 音频单独一份计划：它不影响视频段的落位和取色，但摆上去之后在达芬奇里
    # 能听到声音，调色时判断轻重更准。
    audio_plan, audio_skip = build_audio_plan(draft) if with_audio else ([], [])
    rep["audio_skipped"] = audio_skip
    rep["overlaps"] = overlaps
    rep["trimmed"] = trimmed
    rep["shifted"] = shifted
    rep["skipped"] = skipped
    if skipped:
        # 素材被移走/删掉、时长是 0、复合片段内部素材缺失 —— 这些段进不了
        # 达芬奇，写回后它们也不会有调色。以前只躺在报告的 skipped 数组里，
        # 面板上看不到：用户看到「送入完成：15 段」就以为是全部，直到回剪映
        # 才发现有 3 段没变。这里必须说一句，且要带原因。
        reasons = {}
        for s in skipped:
            k = str(s.get("reason") or "原因未知")
            reasons[k] = reasons.get(k, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])
        say("!! %d 段没送进去（%s）" % (
            len(skipped), "、".join("%s ×%d" % (k, v) for k, v in top[:2])))
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
        return _fail(("同轨有整段被覆盖的情况（重叠 ≥ 后段全长），无法摆放。" "已中止，未改动时间线。"))
    if trimmed:
        say("转场消解：%d 段收短 %d 帧（起始帧不变）"
            % (len(trimmed), sum(t["cut_frames"] for t in trimmed)))
    if shifted:
        say("变速 source 模式：%d 段位置后移（按源帧数落地）" % len(shifted))

    # 预渲染放在拿到工程画布之后做（见下面 tl_size 那一段）：按时间线分辨率
    # 编，4K 源不必产出 4.5 GB 的 4K 中转文件。
    baked, bfail = [], []

    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        return _fail("达芬奇没有响应脚本接口")
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
        return _fail(("达芬奇停在项目管理器，还没有进入任何工程，脚本接口" "不能建时间线。请在达芬奇里打开一个空白工程" "（或「文件 → 新建工程」），再重新送入。"))
    proj = resolve.GetProjectManager().GetCurrentProject()
    if not proj:
        # 闸门正常情况下已经拦住了；这是给 --no-gate 排错模式兜底，
        # 否则下一行 proj.GetName() 直接 AttributeError，看不出所以然。
        return _fail("达芬奇里没有打开工程（项目管理器返回空）")
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

    tl, _used, tried, notes = create_timeline(
        mp, proj, "%s · %s" % (tag, cur.get("name") or "draft"))
    rep["timeline_tried"] = tried
    rep["timeline_notes"] = notes
    if not tl:
        return _fail("建不出空时间线（试过：%s）。工程「%s」里已有 %d 条"
                     "时间线。请换一个空白工程再送入。"
                     % ("、".join(tried), proj.GetName(),
                        proj.GetTimelineCount() or 0))
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
    claimed = _snap.claim(snap_meta, run_dir, tl.GetName(), base, len(plan))

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

    # 这条时间线是刚刚建的空时间线，锁对它没有任何保护意义。先前有锁（同名
    # 时间线被复用、或用户手动锁过轨道）会让 AppendToTimeline 静默返回
    # [None] —— 表现为「每一段都摆不上」却看不出原因。先全部解锁。
    stuck = [r["track"] for r in set_locks(tl, False) if r.get("locked")]
    if stuck:
        rep["locks_stuck"] = stuck
        say("!! 这些轨道解不开锁：%s（摆不上的话先手动解锁）" % "、".join(stuck))

    tl_fps = FPS
    tl_size = (1920, 1080)
    try:
        tl_fps = float(proj.GetSetting("timelineFrameRate") or FPS)
        tl_size = (int(proj.GetSetting("timelineResolutionWidth") or 1920),
                   int(proj.GetSetting("timelineResolutionHeight") or 1080))
    except Exception:
        pass

    # 变速段预渲染。位置放在这里（而不是读草稿之后、连达芬奇之前）是为了
    # 拿到**工程画布**：中转素材按时间线分辨率编，4K 源不再产出 4K 中转件。
    # 实测同一段 63 秒的 4K 素材：按源分辨率 82 秒 / 4.5 GB；缩到 1080p
    # 34 秒 / 1.4 GB，时间线上逐帧一致（达芬奇摆上去本来就要缩）。
    if speed_mode == "bake":
        from . import retime
        todo = [c for c in plan if c.get("bake")]
        if todo:
            say("预渲染变速段 %d 段（ProRes 422 10bit / %dx%d / 30fps / 等长）…"
                % (len(todo), tl_size[0], tl_size[1]))
        t = time.time()
        baked, bfail = retime.bake_plan(
            plan, draft.get("id"), size=tl_size,
            progress=lambda i, n, c: draftio.prog(
                "预渲染变速段 %d/%d（%.2fx，%d 帧）…"
                % (i, n, c["speed"], c["place"])))
        _lap("bake", t)
        rep["baked"] = baked
        for b in baked:
            say("  · %.3fx %3d 帧 %s%s" % (
                b["speed"], b["want"],
                "复用" if b["cached"] else "已生成 %.1f 秒" % b["sec"],
                " · %s" % b.get("enc") if b.get("enc") else ""))
        if bfail:
            # 预渲染挪到建时间线之后了（要拿工程画布），所以失败时必须把刚
            # 建的空时间线删掉 —— 否则下一次送入只会在「CP · 测试 (2)」上
            # 继续攒，工程里留一串空时间线，用户完全不知道是哪来的。
            compat.delete_timelines(mp, [tl])
            _snap.clear_pending()
            return _fail("变速段预渲染失败（%d 段），已中止，未改动时间线"
                         "（刚建的空时间线已删掉）：%s"
                         % (len(bfail), bfail[0].get("why")),
                         bake_failed=bfail)

    # 图片段：先把所有静帧转成等长视频，**一次**导进媒体池。
    #
    # 为什么不放在落轨循环里逐张转：那条路上每转一张都要 `media_index(mp)`
    # 重建一次索引，而那是 O(池子大小) 的 API 调用。100 张图 = 上万次
    # GetClipProperty，媒体池还会随着每张新素材刷新一轮。图片多的工程
    # （照片混剪、产品图）到这里会从「几秒」变成「几分钟」。
    #
    # 转码本身不依赖达芬奇，但目标帧数/画布取自工程设置 —— 所以放在拿到
    # proj 之后。失败的不在这里报，落轨时 place_photo 会重试并按原样报错。
    photo_clips = {}
    photo_jobs = [c for c in plan if c["kind"] == "photo"]
    if photo_jobs:
        from . import retime as _retime
        say("图片段 %d 段：先转成等长视频（每段一次，转过的直接复用）…"
            % len(photo_jobs))
        for c in photo_jobs:
            out = _retime.photo_cache_path(draft.get("id"), c["path"],
                                           c["place"], tl_fps,
                                           tl_size[0], tl_size[1])
            ok, d = _retime.photo_clip(c["path"], c["place"], fps=tl_fps,
                                       width=tl_size[0], height=tl_size[1],
                                       out_path=out)
            if ok:
                photo_clips[c["segment_id"]] = {"clip": out,
                                                "cached": d.get("cached")}
        done = sum(1 for v in photo_clips.values() if v.get("cached"))
        say("  · 转好 %d/%d 段（复用缓存 %d 段）"
            % (len(photo_clips), len(photo_jobs), done))

    t = time.time()
    idx = media_index(mp)
    need = set()
    for c in plan:
        if c["kind"] == "video":
            need.add(c["path"])
        elif c["kind"] == "compound":
            need |= {p["path"] for p in c["inner"]}
        # photo 导的不是原图（原图导进来是 Still，摆不出正确时长），
        # 而是上面转码出来的等长视频。
    need |= {v["clip"] for v in photo_clips.values()}
    need |= {a["path"] for a in audio_plan}
    missing = [p for p in need if _key(p) not in idx]
    if missing:
        imported = compat.import_media(mp, missing)
        # 不信返回值，重新按媒体池的实际内容建索引：21.2 的导入有时不返回
        # 片段（或返回 None），但东西其实进来了。以池子为准。
        idx = media_index(mp)
        rep["import_mode"] = getattr(imported, "mode", "")
        rep["import_tried"] = getattr(imported, "tried", [])
        rep["imported"] = len(imported)
        still = [p for p in missing if _key(p) not in idx]
        if still:
            rep["import_missing"] = [{"path": p, "name": Path(p).name}
                                     for p in still]
            say("!! %d 个素材没能导进媒体池（%s）：这几段一定摆不上"
                % (len(still), "、".join(Path(p).name for p in still[:3])
                   + ("…" if len(still) > 3 else "")))
    _lap("media", t)

    # 先复合片段（合成瞬间腾空临时轨道），再普通片段，图片段最后
    order = ([c for c in plan if c["kind"] == "compound"]
             + [c for c in plan if c["kind"] == "video"]
             + [c for c in plan if c["kind"] == "photo"])
    placed, failed = [], []
    t = time.time()
    for c in order:
        lane_actual = None
        if c["kind"] == "compound":
            ok, items, lane_actual, detail = place_compound(
                mp, tl, c, idx, base, draft_id=draft.get("id"),
                bake=(speed_mode == "bake"), size=tl_size)
            why = (detail if isinstance(detail, str) else "复合片段摆放失败")
        elif c["kind"] == "photo":
            pc = photo_clips.get(c["segment_id"]) or {}
            ok, items, detail = place_photo(mp, c, idx, draft_id=draft.get("id"),
                                            fps=tl_fps, size=tl_size,
                                            clip=pc.get("clip"),
                                            cached=pc.get("cached"))
            why = detail.get("why")
        else:
            ok, items, detail = place_video(mp, c, idx)
            why = detail.get("why")
        if not ok:
            failed.append({**c, "why": why})
            continue
        main = items[0]["item"]
        row = {
            "segment_id": c["segment_id"], "kind": c["kind"],
            "lane": c["lane"], "lane_planned": c["lane"],
            "rec": c["rec"], "want": c["want"], "place": c["place"],
            "enabled": c.get("enabled", True), "items": items,
            "got_start": main.GetStart() - base,
            "got_dur": main.GetDuration(), "name": main.GetName(),
        }
        if c["kind"] == "compound":
            row.update({"lane": lane_actual or c["lane"], "speed": c["speed"],
                        "parts": detail,
                        "compound_name": c.get("compound_name")})
        elif c["kind"] == "photo":
            row.update({"speed": 1.0, "photo_clip": detail.get("clip"),
                        "photo_cached": detail.get("cached"),
                        "photo_src_px": detail.get("src_px")})
        else:
            row.update({
                "speed": c["speed"], "baked": bool(c.get("bake")),
                # 视频段可能被拆成 main + pad，长度按各段之和算
                "got_dur": sum(i["dur"] for i in items),
                **{k: detail.get(k) for k in ("n", "tail", "fps", "total", "src")},
                "solve": detail.get("kind")})
        placed.append(row)
        time.sleep(0.02)

    # 音频：等视频段全部摆完再摆 —— 视频自动带进来的原声先把 A1 占了，
    # 音频再去挑真正空着的那条（剪映的 A1 常常就是原声，不能直接按序号对应）。
    audio_placed, audio_failed = [], []
    if audio_plan:
        audio_placed, audio_failed = place_audio(mp, tl, audio_plan,
                                                 media_index(mp))
        if audio_placed:
            lanes_txt = "、".join("A%d" % n for n in
                                 sorted({a["lane_actual"] for a in audio_placed}))
            say("· 音频 %d 段已送进达芬奇（%s）"
                % (len(audio_placed), lanes_txt))
    _lap("place", t)

    # 一段都没摆上就不能往下走。原先这里直接对空序列 max()，21.2 上导入素材
    # 静默失效时就会一路走到这，用户看到的是
    # `ValueError: max() iterable argument is empty` 加一段 traceback，
    # 既不知道哪一段出了问题，也不知道工程被改成了什么样。
    # 现在：把每段的失败原因汇总成一句话，并且**把刚建的空时间线删掉** ——
    # 半截的时间线留着，下一次送入只会改名叫「… (2)」，越攒越多。
    if not placed:
        why = sorted({(f.get("why") or "原因未知") for f in failed})
        rep["failed"] = failed
        # 报告字段要齐：面板和命令行都按「键一定在」来读，少一个键就得为
        # 「有没有这个键」写第二种分支。
        rep["placed"] = 0
        rep["exact"] = 0
        rep["error"] = ("一段都没能摆上时间线：%s。"
                        "已删掉刚建的空时间线「%s」，工程没有留下改动。"
                        % ("；".join(why[:3]) or "原因未知", tl.GetName()))
        compat.delete_timelines(mp, [tl])
        _snap.clear_pending()
        rep["timings"] = _timings()
        draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
        return rep

    # 身份标记
    t = time.time()
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

    # 画面缩放：AppendToTimeline 不收，但 21.2 的 SetProperty 收（实测
    # ZoomX/ZoomY/Pan/Tilt/RotationAngle 都能写、都能读回）。只搬缩放：
    # 它是干净的比例倍数，两边语义一致；位移和旋转的单位没有实测依据，
    # 硬套会把构图改坏，照旧在报告里说明。
    tf_ok, tf_skip = apply_transform(placed, _tf)
    rep["transform_applied"] = tf_ok
    rep["transform_skipped"] = tf_skip
    if tf_ok:
        say("· 画面缩放 %d 段已按剪映的比例送入（ZoomX/ZoomY）" % len(tf_ok))

    lay = draftio.layers(draft)
    note = ("含视频轨、图片段（已转成等长视频）、音频轨；全部轨道已上锁"
            "（改前先解锁）。文字 %d 条、贴纸 %d 条、转场 %d 段、"
            "关键帧 %d 段未传入；写回剪映时从原草稿原样保留。"
            "复合片段 %d 个已按内部时间线展开后重新合成。"
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

    # 音频与图片单列成字段：面板和 CLI 都按「有没有这个键」直接读，
    # 也让「到底送没送」这件事有据可查，不用去翻 skipped 里有没有它。
    rep["photos"] = [{"segment_id": p["segment_id"], "rec": p["rec"],
                      "want": p["want"], "place": p["place"],
                      "got_dur": p["got_dur"],
                      "src_px": p.get("photo_src_px")}
                     for p in placed if p["kind"] == "photo"]
    rep["audio"] = {
        "enabled": bool(with_audio),
        "planned": len(audio_plan),
        "placed": len(audio_placed),
        "skipped": audio_skip,
        "failed": audio_failed,
        "lanes": sorted({a["lane_actual"] for a in audio_placed}),
        "note": ("音量/淡入淡出不搬（脚本接口无可靠入口），"
                 "写回时原样保留在剪映草稿里" if audio_plan else ""),
    }
    if audio_failed:
        say("!! %d 段音频没摆上（%s）"
            % (len(audio_failed),
               "；".join(f.get("why") or "原因未知" for f in audio_failed[:2])))

    rep["locks"] = set_locks(tl, True)
    try:
        compat.ensure_page("color")
        time.sleep(0.5)
    except Exception:
        pass
    _lap("finish", t)

    exact = [p for p in placed
             if p["got_dur"] == p["place"] and p["got_start"] == p["rec"]]
    # 用 id 集合比，而不是 `p not in exact`：后者是 dict 逐键比较，段数
    # 上千时退化成平方级（实测 2000 段 45 ms、4000 段 158 ms，纯属白花）。
    exact_ids = {id(p) for p in exact}
    end = max((p["rec"] + p["place"]) for p in placed)
    speed_rows = []
    plan_rng = {c["segment_id"]: us2f(c.get("range_us")) for c in plan}
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
        rng = plan_rng.get(p["segment_id"], 0)
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
        "mismatch": [p for p in placed if id(p) not in exact_ids],
        "failed": failed,
        "speed_segments": speed_rows,
        "speed_note": ("Resolve 脚本接口读得出速度、设不进去（21.2 实测："
                       "GetSpeed 返回 {'Percentage': …}，SetSpeed 返回 False、"
                       "SetProperty('Speed') 也返回 False）；"
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
        # 工程 id 用来在取色时确认「还是同一个工程」。只比时间线名字是不够的：
        # 另存一份同名工程、或同名时间线存在别的工程里，名字全对、内容是另一份。
        "project": proj.GetName(), "project_uid": compat.project_id(proj),
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
        # 只有整条时间线摆完、标记也全都写上了，才让这份快照成为「可写回的那版」。
        # 同时只能认领自己的 pending：另一个窗口若已经开始下一次送入，本次
        # 时间线仍然有效，但不能把对方的草稿身份/LUT 上下文抢过来。
        committed = _snap.commit(claim_id=claimed.get("claim_id"),
                                 timeline_end=rep["timeline_end"],
                                 placed=rep["placed"], speed_mode=speed_mode)
        rep["snapshot_committed"] = bool(committed)
        if not committed:
            rep["snapshot_conflict"] = True
            rep["ok"] = False
            rep["error"] = (
                "本次时间线已经建好，但另一个窗口已开始新的送入。为避免在旧窗口里"
                "把另一份草稿的调色写回，本次没有切换「最近一次送入」的上下文。\n"
                "请回到最后执行送入的窗口继续；若只需要本次时间线，请在另一个"
                "送入结束后重新送入一次。")
            say("!! 另一个窗口已开始新的送入；本次不作为可写回版本")
        _pruned = _prune_retime(draft, speed_mode, say)
        if _pruned.get("files"):
            rep["retime_pruned"] = _pruned
    else:
        rep["snapshot_committed"] = False
    rep["timings"] = _timings()
    # 耗时按「大头先说」列出来。用户看到「一分多钟」时，第一件事就是想知道
    # 时间花在哪，而不是被告知「已优化」。
    heavy = sorted(((v, k) for k, v in marks.items() if k != "total" and v >= 1),
                   reverse=True)
    if heavy:
        say("耗时 %.1f 秒（%s）" % (rep["timings"]["total"],
                                   " / ".join("%s %.1fs" % (_T_LABEL.get(k, k), v)
                                              for v, k in heavy[:3])))
    else:
        say("耗时 %.1f 秒" % rep["timings"]["total"])
    draftio.dump_json(rep, os.path.join(run_dir, "send.json"))
    return rep
