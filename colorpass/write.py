"""达芬奇 LUT → 写回剪映（克隆模式，生成新草稿，不动原工程）

本轮改的两件事
--------------
1. **cube 不再依赖临时目录**：LUT 复制进新草稿自己的
   `<新草稿>/Resources/ColorPass/`，effect 的 path 指向这份副本。
   草稿被复制/迁移/换机器，LUT 都跟着走。
2. **转场与关键帧逐条核对**：写回前记录源草稿每段的转场/关键帧，
   写完后逐段比对。用户新加的转场、关键帧必须一字节不差。

写回只认「送入时的快照」（2026-11-14）
--------------------------------------
送入那一刻把明文草稿整份存进 `<RUN_ROOT>/<草稿ID>/snap-<时间戳>/`（见 snapshot.py）。
写回拿它当底稿，**不再读剪映当前的内存草稿**：

  · 你在剪映里之后继续剪（删段、加复合片段、改变速）都不影响这次往返 ——
    写出来的永远是「送入那版剪辑 + 达芬奇里的调色」；
  · LUT 一定对得上自己的画面：段 ID、位置、时长都来自同一份快照；
  · 新草稿是另存的一份，不会覆盖你后来的改动。

旧做法（拿当前草稿当底）实测出过两种错：段被删 → 拒绝写回；段 ID 还在但
位置变了 → LUT 挂到别的画面上，还没有任何报错。

留下的闸：时间线对齐（alignment_ok）、cube 完整、LUT 的段 ID 都在快照里。
"""
import json
import os
import shutil
from pathlib import Path

from . import draftio, paths, pull
from . import workflow as WF


def _trans_sig(rows):
    """转场的语义签名（名字 + 时长，多重集）。只比数量会漏掉「叠化→闪白」。"""
    return tuple(sorted((str((r or {}).get("name") or ""),
                         str((r or {}).get("duration")))
                        for r in rows or []))


def verify_cubes(seg_luts):
    """逐个校验 cube 真能被剪映用：存在、非空、33³ 可解析。"""
    bad = []
    for sid, p in seg_luts.items():
        f = Path(str(p).split(";")[0])
        if not f.exists():
            bad.append({"segment_id": sid, "file": str(f), "why": "文件不存在"})
            continue
        if f.stat().st_size < 1024:
            bad.append({"segment_id": sid, "file": str(f), "why": "文件过小/空"})
            continue
        try:
            size, data = pull.load_cube(f)
        except Exception as e:
            bad.append({"segment_id": sid, "file": str(f), "why": "解析失败 %s" % e})
            continue
        if size != 33 or len(data) != 33 ** 3:
            bad.append({"segment_id": sid, "file": str(f),
                        "why": "不是完整 33³（size=%s n=%d）" % (size, len(data))})
    return bad


def embed_cubes(new_dir, seg_luts):
    """把 cube 复制进新草稿目录，并把 effect.path 改指向副本。

    返回 {segment_id: 嵌入后的绝对路径}。剪映的 path 是分号分隔的多字段，
    第二段是它自己的效果包路径，只能替换第一段。
    """
    emb = paths.embed_dir(str(new_dir))
    mapping, copied = {}, []
    for sid, src in seg_luts.items():
        s = Path(str(src).split(";")[0])
        dst = Path(emb) / ("%s.cube" % sid)
        shutil.copy2(s, dst)
        mapping[sid] = str(dst).replace("\\", "/")
        copied.append({"segment_id": sid, "src": str(s), "dst": str(dst),
                       "bytes": dst.stat().st_size})
    return mapping, copied


def clone_subdrafts(src_dir, new_dir, nd):
    """把复合片段的子草稿（subdraft/<uuid>/）复制进新草稿，并改写路径。

    为什么必须复制（实测结构，2026-11-14）：
    母稿里每个复合片段是一条 materials.drafts 记录，内部时间线存在
    `subdraft/<uuid>/draft_content.json`，`draft_file_path` 是**绝对路径**。
    克隆草稿时这段 JSON 是原样搬过去的，于是两份草稿指向同一个子草稿文件：
      · 删掉原草稿 → 新草稿里的复合片段直接坏掉；
      · 在新草稿里改复合片段 → 剪映写的是同一个文件，原草稿被一起改。
    所以克隆时必须把子草稿目录复制一份，并把路径改指副本。
    """
    src_sub = Path(src_dir) / "subdraft"
    out = []
    if not src_sub.is_dir():
        return out
    for e in (nd.get("materials") or {}).get("drafts") or []:
        if not isinstance(e, dict):
            continue
        p = str(e.get("draft_file_path") or "")
        if not p:
            continue
        sp = Path(p)
        try:
            rel = sp.parent.relative_to(src_sub)
        except ValueError:
            out.append({"compound_id": e.get("id"), "skipped": True,
                        "why": "子草稿不在源草稿的 subdraft 下", "from": p})
            continue
        dst_dir = Path(new_dir) / "subdraft" / rel
        try:
            shutil.copytree(sp.parent, dst_dir, dirs_exist_ok=True)
        except Exception as ex:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "from": p, "why": str(ex)})
            continue
        newp = dst_dir / sp.name
        e["draft_file_path"] = str(newp).replace("\\", "/")
        out.append({"compound_id": e.get("id"), "ok": newp.exists(),
                    "from": p, "to": e["draft_file_path"]})
    return out


def compound_intact(src_draft, new_draft, problems):
    """复合片段内部时间线必须一字节没变。

    为什么单独查这一项：复合片段在剪映里是「一个段」，但内部有自己的时间线。
    LUT 只挂在外层段上，照理不会碰到内部；可一旦哪天克隆逻辑动到了
    materials.drafts[].draft，用户在复合片段里的剪辑就会被打乱 ——
    这种问题肉眼在面板上看不出来，必须逐字节比。
    """
    a = draftio.compound_index(src_draft)
    b = draftio.compound_index(new_draft)
    ok = 0
    for cid, ea in a.items():
        eb = b.get(cid)
        if eb is None:
            problems.append({"compound_id": cid, "why": "复合片段在新草稿里没了"})
            continue
        ia, _ha = draftio.compound_inner(ea)
        ib, _hb = draftio.compound_inner(eb)
        sa = json.dumps(ia, ensure_ascii=False, sort_keys=True) if ia else ""
        sb = json.dumps(ib, ensure_ascii=False, sort_keys=True) if ib else ""
        if sa != sb:
            problems.append({"compound_id": cid, "why": "复合片段内部时间线变了",
                             "len_src": len(sa), "len_new": len(sb)})
            continue
        ok += 1
    return ok


# ---------- 全轨结构指纹（2026-11-20） ----------
# 旧核对只遍历 type == "video" 的轨，且转场只比数量：
#   · 独立特效轨 / 滤镜轨 / 文字轨完全不在断言范围；
#   · 转场被换成别的类型（叠化→闪白）查不出来；
#   · 段的 speed 引用被丢掉时，timerange 不变、断言全过，画面却回到 1 倍速，
#     LUT 就挂在了错误的画面上。
# 现在改成「逐轨逐段语义指纹」：轨道类型、段顺序、入出点、关键帧、
# 转场/蒙版/变速/淡入淡出/复合片段的引用全部入账。只比语义不比 id ——
# 克隆会给每段新增一条 LUT effect，id 变化不该算错。

_REF_KINDS = ("transitions", "common_mask", "masks", "speeds",
              "audio_fades", "drafts")


def _ref_kinds(draft):
    """{材质ID: (材质类别, 材质本体)}。只收会影响画面的那几类。"""
    mats = draft.get("materials") or {}
    out = {}
    for key in _REF_KINDS:
        for m in mats.get(key) or []:
            if isinstance(m, dict) and m.get("id"):
                out[str(m["id"])] = (key, m)
    return out


def _ref_sig(kind, m):
    """把一条材质引用压成语义签名：留着会改画面的字段，扔掉 id/时间戳。"""
    if kind == "transitions":
        # 名字 + 时长：换成「闪白」或时长被改，都必须报出来
        return (kind, str(m.get("name") or ""), str(m.get("duration")))
    if kind in ("common_mask", "masks"):
        return (kind, str(m.get("name") or ""), str(m.get("resource_type") or ""))
    if kind == "speeds":
        try:
            return (kind, "%.4f" % float(m.get("speed") or 1.0))
        except (TypeError, ValueError):
            return (kind, "1.0000")
    if kind == "audio_fades":
        return (kind, str(m.get("fade_in")), str(m.get("fade_out")))
    if kind == "drafts":
        # 复合片段：认它的内容文件（<uuid>/draft_content.json），路径会被改写
        parts = str(m.get("draft_file_path") or "").replace("\\", "/").split("/")
        return (kind, "/".join(parts[-2:]))
    return (kind,)


def structure_index(draft):
    """{轨道序号: {type, order, segments{段ID: 指纹}}}，覆盖所有轨道类型。"""
    kinds = _ref_kinds(draft)
    out = {}
    for i, tr in enumerate(draft.get("tracks") or []):
        if not isinstance(tr, dict):
            continue
        order, segs = [], {}
        for sg in tr.get("segments") or []:
            if not isinstance(sg, dict) or not sg.get("id"):
                continue
            sid = str(sg["id"])
            order.append(sid)
            refs = sorted(_ref_sig(*(kinds[str(r)]))
                          for r in (sg.get("extra_material_refs") or [])
                          if str(r) in kinds)
            tt = sg.get("target_timerange") or {}
            st = sg.get("source_timerange") or {}
            segs[sid] = {
                "material": str(sg.get("material_id") or ""),
                "target": (tt.get("start"), tt.get("duration")),
                "source": (st.get("start"), st.get("duration")),
                "keyframes": json.dumps(sg.get("common_keyframes") or [],
                                        ensure_ascii=False, sort_keys=True),
                # 段自带的 speed 字段（曲线变速/旧结构会写在这里）
                "speed": str(sg.get("speed")),
                # 停用状态必须一字节不差地带过去：克隆时把 visible:false 丢了，
                # 这段被用户关掉的画面会「复活」在成片里，而所有其它断言全过。
                "visible": sg.get("visible") is not False,
                "refs": refs,
            }
        out[i] = {"type": str(tr.get("type") or ""),
                  "order": order, "segments": segs}
    return out


def structure_diff(src, new):
    """写回前后逐轨逐段比结构，返回问题清单（空 = 剪映原生结构一段不差）。"""
    a, b = structure_index(src), structure_index(new)
    probs = []
    if len(a) != len(b):
        probs.append({"why": "轨道数变了", "src": len(a), "new": len(b)})
    for i in sorted(a):
        ea, eb = a[i], b.get(i)
        if eb is None:
            probs.append({"track": i, "type": ea["type"], "why": "轨道没了"})
            continue
        if ea["type"] != eb["type"]:
            probs.append({"track": i, "why": "轨道类型变了",
                          "src": ea["type"], "new": eb["type"]})
            continue
        if ea["order"] != eb["order"]:
            probs.append({"track": i, "type": ea["type"],
                          "why": "段顺序/数量变了",
                          "src": len(ea["order"]), "new": len(eb["order"])})
        for sid in ea["order"]:
            sa, sb = ea["segments"].get(sid), eb["segments"].get(sid)
            if sb is None:
                probs.append({"track": i, "type": ea["type"],
                              "segment_id": sid, "why": "段不见了"})
                continue
            for k, label in (("target", "时间线位置/时长"),
                             ("source", "素材入出点"),
                             ("material", "素材引用"),
                             ("speed", "变速倍率"),
                             ("visible", "启用/停用状态"),
                             ("keyframes", "关键帧"),
                             ("refs", "转场/蒙版/变速/淡入淡出/复合片段")):
                if sa[k] != sb[k]:
                    probs.append({"track": i, "type": ea["type"],
                                  "segment_id": sid, "why": label + "变了",
                                  "src": sa[k], "new": sb[k]})
    return probs


def kept_summary(draft):
    """这一版到底保住了多少剪映原生结构 —— 写回报告与面板都用它。"""
    mats = draft.get("materials") or {}
    kinds = _ref_kinds(draft)
    tracks = [t for t in (draft.get("tracks") or []) if isinstance(t, dict)]
    video_tracks = [t for t in tracks if t.get("type") == "video"]
    n_seg = n_speed = n_fade = n_kf = 0
    for tr in tracks:
        for sg in tr.get("segments") or []:
            if not isinstance(sg, dict):
                continue
            n_seg += 1
            if sg.get("common_keyframes"):
                n_kf += 1
            refs = [str(r) for r in (sg.get("extra_material_refs") or [])]
            if any((kinds.get(r) or ("", {}))[0] == "speeds" for r in refs):
                n_speed += 1
            elif isinstance(sg.get("speed"), (int, float)) and \
                    abs(float(sg["speed"]) - 1.0) > 1e-6:
                n_speed += 1
            if any((kinds.get(r) or ("", {}))[0] == "audio_fades" for r in refs):
                n_fade += 1
    other_effects = [e for e in mats.get("effects") or []
                     if isinstance(e, dict)
                     and ".cube" not in str(e.get("path") or "").lower()]
    return {
        "tracks": len(tracks),
        "overlay_tracks": max(0, len(video_tracks) - 1),
        "segments": n_seg,
        "speed_segments": n_speed,
        "fade_segments": n_fade,
        "keyframe_segments": n_kf,
        "transitions": len(mats.get("transitions") or []),
        "masks": len(mats.get("common_mask") or []) + len(mats.get("masks") or []),
        "effects": len(other_effects),
        "compounds": len(mats.get("drafts") or []),
    }


def run(new_name=None, auto_pull=True):
    """按「送入那一刻的快照」生成新草稿。

    不再读剪映当前的内存草稿：送入之后你在剪映里怎么改都不影响这次写回 ——
    写出来的永远是送入时那一版剪辑 + 达芬奇里的调色。这样 LUT 一定对得上自己的
    画面（段 ID、位置、时长都来自同一份快照），也不会覆盖你后来的改动（新草稿
    是另存的一份）。
    """
    rep = {"ok": False, "action": "write"}
    from . import snapshot
    ptr = snapshot.current()
    if ptr is None:
        rep["error"] = ("还没有完成过一次「送入达芬奇」。顺序是："
                        "送入达芬奇 → 在达芬奇里调色 → 写回剪映。")
        return rep
    if ptr.get("_incomplete"):
        rep["error"] = ("最近一次「送入达芬奇」没有走完（%s 开始，时间线只摆了"
                        "一部分），写回会把残缺的剪辑带回去。请重新送入；"
                        "要按上一次成功的版本写回，先执行 `colorpass reset`。"
                        % (ptr.get("pending_at") or "上一轮"))
        return rep
    did = ptr.get("draft_id")
    run_dir = ptr.get("run_dir") or paths.draft_run_dir(did)
    rep["draft"] = ptr.get("draft_name")
    rep["draft_id"] = did
    rep["snapshot_at"] = ptr.get("snapshot_at")
    rep["snapshot_sha"] = ptr.get("snapshot_sha")
    try:
        draft, snap_meta = snapshot.snapshot_of(ptr)
    except FileNotFoundError as exc:
        rep["error"] = "%s —— 这一版没留下快照，请重新送入" % exc
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    cur = {"name": snap_meta.get("draft_name") or ptr.get("draft_name"),
           "folder": snap_meta.get("folder") or ""}
    snap_dir = ptr.get("snapshot_dir") or snapshot.dir_of(run_dir)

    # 剪映里之后又改过？如实报出来（写回仍按送入那版走，不掺新改动）
    # deadline=6：这一步只产出「你后来又改了几段」的提示，扫不到就算了。
    # 不设上限时它走 sync() 的 70 秒内存扫描，实测能把 15 秒的写回拖到 85 秒，
    # 面板上就是「点完没反应」。
    try:
        live, _lc, _lf = draftio.read(allow_stale=True, deadline=6)
        if not isinstance(live, dict):
            pass
        elif str(live.get("id")) != str(draft.get("id")):
            # 剪映里换成别的草稿了：跟这次往返无关，别拿它算差异
            rep["live_note"] = ("剪映当前打开的是另一份草稿（%s），"
                                "与送入的不是同一份；写回不受影响"
                                % (_lc or {}).get("name"))
        else:
            rep["drift"] = snapshot.diff(draft, live)
            d = rep["drift"]
            if d["same"]:
                rep["drift_note"] = ("剪映草稿与送入时一致（%d 段）"
                                     % d["segments_sent"])
            else:
                rep["drift_note"] = (
                    "剪映草稿在送入之后又改过：新增 %d / 删除 %d / 改动 %d 段。"
                    "本次写回按送入那一版（%d 段）生成，不含这些新改动"
                    % (d["added"], d["removed"], d["changed"], d["segments_sent"]))
    except Exception as exc:
        rep["live_error"] = "%s: %s" % (type(exc).__name__, exc)

    # 先取回：面板已经不再提供「取回调色」按钮，写回必须是「一次点击做完」
    if auto_pull:
        pl = pull.run()
        rep["pull"] = {k: pl.get(k) for k in
                       ("ok", "error", "count", "timeline", "alignment_ok",
                        "identity_dev_max", "identity_graded")
                       if k in pl}
        rep["pull_issues"] = pl.get("issues") or []
        if not pl.get("ok"):
            rep["error"] = "取回调色没成功，已中止写回：%s" % (
                pl.get("error") or "未知原因")
            rep["alignment_ok"] = pl.get("alignment_ok")
            rep["identity_dev_max"] = pl.get("identity_dev_max")
            draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
            return rep
    else:
        pull_path = os.path.join(run_dir, "pull.json")
        if not os.path.exists(pull_path):
            rep["error"] = "缺少 pull.json（并且关掉了自动取回），无法写回"
            draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
            return rep
        pl = json.loads(Path(pull_path).read_text(encoding="utf-8"))
        # 这份 pull.json 是不是「另一次送入」留下的？对不上就直接中止，
        # 否则 LUT 会按旧段 ID 往新草稿上挂（实测踩过：13 段的新计划配 14 段的旧 pull）。
        plan_path = Path(os.path.join(run_dir, "plan.json"))
        if not plan_path.exists():
            # 以前这里直接 read_bytes()，plan.json 不在时抛的是
            # FileNotFoundError —— 面板只看到「未完成（退出码 1）」，不知道原因。
            rep["error"] = "缺少 plan.json（并且关掉了自动取回），无法校验收回结果"
            draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
            return rep
        cur_sha = __import__("hashlib").sha1(plan_path.read_bytes()).hexdigest()[:16]
        if pl.get("plan_sha") and pl["plan_sha"] != cur_sha:
            rep["error"] = ("pull.json 是上一版计划留下的（%s ≠ %s），"
                            "请去掉 --no-pull 重新取回" % (pl["plan_sha"], cur_sha))
            draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
            return rep
    rep["alignment_ok"] = pl.get("alignment_ok")
    rep["identity_ok"] = pl.get("identity_ok")
    if pl.get("alignment_ok") is not True:
        rep["error"] = "时间线与计划不一致，拒绝写回（LUT 会挂错段）"
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    # 恒等偏差只作参考，不再是闸门。
    # 旧逻辑：没调过的段导出 LUT 应该 ≈ 恒等，否则拒绝写回。可这个工具的全部
    # 用途就是「在达芬奇里调色」，用户手调过的段导出 LUT 必然偏离恒等
    # （实测一份 14 段的手调草稿：dev 92.68 码）—— 于是永远写不回去。
    # 真正能保证「LUT 不挂错段」的是对齐校验 + 每段的身份标记。
    rep["identity_dev_max"] = pl.get("identity_dev_max")
    rep["identity_graded"] = pl.get("identity_graded")
    # 多机位/合并片段：一个片段盖住多段时，这些段共用同一份 LUT。
    # 写回本身照常（每段仍有自己的 cube 文件），但要在结果里说一句 ——
    # 用户看到「3 段同一个调色」才知道这不是插件搞错了。
    rep["coverage"] = pl.get("coverage") or []
    rep["coverage_segments"] = pl.get("coverage_segments") or 0
    rep["coverage_note"] = pl.get("coverage_note") or ""

    seg_luts = pl.get("segment_luts") or {}
    rep["luts"] = len(seg_luts)
    bad_cubes = verify_cubes(seg_luts)
    rep["cube_problems"] = bad_cubes
    if bad_cubes:
        rep["error"] = "%d 个 cube 不可用，拒绝写回（会让剪映显示 LUT 丢失）" % len(bad_cubes)
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep

    src_recs = draftio.transition_records(draft)
    src_seg = set(src_recs)
    orphan = [s for s in seg_luts if s not in src_seg]
    rep["orphan"] = orphan
    if orphan:
        # 段 ID 只可能来自同一次送入，对不上就是快照/计划被覆盖过（比如重新送入
        # 前没删旧 run 目录）。写回会把 LUT 挂到不存在的段上，只能中止。
        rep["error"] = ("%d 个 segment_id 在送入时的快照里不存在，"
                        "快照与达芬奇时间线不是同一次送入的，拒绝写回"
                        % len(orphan))
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep

    rep["requested_name"] = new_name or ""
    try:
        r = WF.write_back([], {}, draft=draft, segment_luts=seg_luts,
                          project=cur.get("name"), name=new_name)
    except Exception as exc:
        # 写草稿落在剪映目录里，可能撞上权限/磁盘满/索引被占用。以前这些异常
        # 直接冒到 CLI 顶层，用户只看到一段 traceback。
        rep["error"] = "生成新草稿时出错：%s: %s" % (type(exc).__name__, exc)
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    rep["write_back"] = {k: v for k, v in r.items()
                         if k in ("ok", "name", "error", "luts", "mode",
                                  "tracks", "segments")}
    if not r.get("ok"):
        rep["error"] = r.get("error") or "write_back 失败"
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep

    new_dir = None
    for cand in (r.get("path"), r.get("dir"), r.get("folder")):
        if cand and Path(str(cand)).is_dir():
            new_dir = Path(str(cand))
            break
    if new_dir is None:
        c = Path(WF.JY.draft_root()) / r["name"]
        new_dir = c if c.is_dir() else None
    if new_dir is None:
        rep["error"] = "找不到新草稿目录（无法嵌入 LUT）"
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    rep["new_draft"] = str(new_dir)
    rep["draft_name"] = Path(new_dir).name
    rep["compound_src"] = len(draftio.compound_index(draft))

    # 嵌入 + 改指向
    try:
        mapping, copied = embed_cubes(new_dir, seg_luts)
    except Exception as exc:
        rep["error"] = "把 cube 复制进新草稿失败：%s: %s" % (
            type(exc).__name__, exc)
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    rep["embedded"] = copied
    f = new_dir / "draft_content.json"
    try:
        nd = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        rep["error"] = "新草稿的 draft_content.json 读不出来：%s" % exc
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    eff = {e.get("id"): e for e in (nd.get("materials") or {}).get("effects") or []
           if isinstance(e, dict)}
    swapped = 0
    for tr in nd.get("tracks") or []:
        for sg in tr.get("segments") or []:
            sid = sg.get("id")
            if sid not in mapping:
                continue
            for ref in sg.get("extra_material_refs") or []:
                e = eff.get(ref) or {}
                p = str(e.get("path") or "")
                if ".cube" not in p.lower():
                    continue
                parts = p.split(";")
                parts[0] = mapping[sid]
                e["path"] = ";".join(parts)
                e["name"] = Path(mapping[sid]).name
                swapped += 1
    # 复合片段：把快照里的 subdraft 复制进新草稿，别和任何一份草稿共用同一个文件
    rep["subdrafts"] = clone_subdrafts(snap_dir, new_dir, nd)
    try:
        f.write_text(json.dumps(nd, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        rep["error"] = "改写新草稿的 LUT 路径时写盘失败：%s: %s" % (
            type(exc).__name__, exc)
        draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
        return rep
    rep["paths_swapped"] = swapped
    # ---- 核对 ----
    effects = {e.get("id"): e for e in (nd.get("materials") or {}).get("effects") or []
               if isinstance(e, dict)}
    trans_mat = {m.get("id"): m
                 for m in (nd.get("materials") or {}).get("transitions") or []
                 if isinstance(m, dict)}
    trans_ids = set(trans_mat)
    mask_ids = set()
    for _k in ("common_mask", "masks"):
        for m in (nd.get("materials") or {}).get(_k) or []:
            if isinstance(m, dict) and m.get("id"):
                mask_ids.add(m["id"])
    rows, problems = [], []
    seen = 0
    for tr in nd.get("tracks") or []:
        if not isinstance(tr, dict) or tr.get("type") != "video":
            continue
        for sg in tr.get("segments") or []:
            sid = sg.get("id")
            seen += 1
            want = Path(str(seg_luts.get(sid, ""))).name or None
            got, got_path = None, None
            for ref in sg.get("extra_material_refs") or []:
                e = effects.get(ref) or {}
                p = str(e.get("path") or "")
                if ".cube" in p.lower():
                    got_path = p.split(";")[0]
                    got = Path(got_path).name
            base = src_recs.get(sid) or {}
            same_tl = (base.get("target") ==
                       (sg.get("target_timerange") or {}).get("start")
                       and base.get("dur") ==
                       (sg.get("target_timerange") or {}).get("duration")
                       and base.get("src") ==
                       (sg.get("source_timerange") or {}).get("start"))
            ck_same = (base.get("common_keyframes") ==
                       json.dumps(sg.get("common_keyframes") or [],
                                  ensure_ascii=False, sort_keys=True))
            trans_src = base.get("transitions") or []
            refs = sg.get("extra_material_refs") or []
            trans_got_ids = [r for r in refs if r in trans_ids]
            # 比名字+时长，不再只比数量：叠化被换成闪白、或转场时长被改，
            # 以前都能混过去。
            trans_same = (_trans_sig(trans_src) ==
                          _trans_sig([trans_mat.get(r) for r in trans_got_ids]))
            # 蒙版：快照里这段挂了哪些蒙版，新草稿必须一模一样。用户这次真的
            # 加了两个蒙版（其中一个带关键帧），所以这一项不是摆设。
            masks_src = base.get("masks") or []
            masks_got = sorted(r for r in refs if r in mask_ids)
            masks_same = (masks_src == masks_got)
            file_ok = bool(got_path and Path(got_path).exists())
            # 这一段本来就不该有 LUT（pull 跳过了停用段 / 这段没有调色）：
            # 「文件不存在」不是问题，两边都空才是对的。
            no_lut = want is None and got is None
            ok_row = (got == want) and same_tl and ck_same and trans_same \
                and masks_same and (file_ok or no_lut)
            if not ok_row:
                problems.append({"segment_id": sid, "want_lut": want, "got_lut": got,
                                 "lut_file_exists": file_ok,
                                 "timeline_unchanged": same_tl,
                                 "keyframes_unchanged": ck_same,
                                 "transitions_unchanged": trans_same,
                                 "masks_unchanged": masks_same,
                                 "mask_names": base.get("mask_names") or []})
            rows.append({"segment_id": sid, "want": want, "got": got,
                         "lut_path": got_path, "file_ok": file_ok,
                         "timeline_unchanged": same_tl,
                         "keyframes_unchanged": ck_same,
                         "transitions_unchanged": trans_same,
                         "masks_unchanged": masks_same,
                         "mask_names": base.get("mask_names") or [],
                         "ok": ok_row})

    # 全轨结构比对：这条以前只覆盖 video 轨，独立特效轨/滤镜轨/文字轨是盲区
    struct_probs = structure_diff(draft, nd)
    rep.update({
        "segments": seen,
        "lut_ok": sum(1 for x in rows if x["want"] == x["got"] and x["want"]),
        "timeline_unchanged": sum(1 for x in rows if x["timeline_unchanged"]),
        "keyframes_unchanged": sum(1 for x in rows if x["keyframes_unchanged"]),
        "transitions_unchanged": sum(1 for x in rows if x["transitions_unchanged"]),
        "masks_unchanged": sum(1 for x in rows if x["masks_unchanged"]),
        "mask_segments": sum(1 for x in rows if x.get("mask_names")),
        "duration_src": draft.get("duration"), "duration_new": nd.get("duration"),
        "tracks_src": len(draft.get("tracks") or []),
        "tracks_new": len(nd.get("tracks") or []),
        "problems": problems, "rows": rows,
        "compound_ok": compound_intact(draft, nd, problems),
        "structure_problems": struct_probs,
        # 这一版保住了什么 —— 面板直接拿来显示，别再让用户猜
        "kept": kept_summary(draft),
    })
    rep["ok"] = (not problems and not struct_probs and seen == len(rows)
                 and rep["duration_src"] == rep["duration_new"]
                 and rep["tracks_src"] == rep["tracks_new"]
                 and rep["compound_ok"] == rep["compound_src"])
    if struct_probs:
        rep["error"] = ("写回后结构对不上（%d 处）：%s —— 新草稿可能缺转场/"
                        "变速/蒙版/特效，已按失败处理"
                        % (len(struct_probs), struct_probs[0].get("why")))
    draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
    return rep
