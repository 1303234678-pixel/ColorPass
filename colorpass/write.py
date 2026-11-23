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

from . import compat, draftio, paths, platforms, pull
from . import workflow as WF


def _trans_sig(rows):
    """转场的语义签名（名字 + 时长，多重集）。只比数量会漏掉「叠化→闪白」。"""
    return tuple(sorted((str((r or {}).get("name") or ""),
                         str((r or {}).get("duration")))
                        for r in rows or []))


def body_files(new_dir):
    """新草稿的正文文件清单。

    Windows 只有一份 `draft_content.json`；macOS 11.8 是「同一份正文的三个
    副本」：草稿根 `draft_info.json`（打开时那份镜像）、`Timelines/<id>/
    draft_info.json`（时间线本体）、以及两处 `template-2.tmp`（剪映落盘时
    留的第二份）。改 LUT 路径时三份必须同改，只改一份的话，剪映按另一份
    加载，LUT 就等于没挂上。
    """
    from . import platforms
    if not platforms.is_mac():
        return [Path(new_dir) / "draft_content.json"]
    out = [Path(new_dir) / "draft_info.json",
           Path(new_dir) / "template-2.tmp"]
    try:
        pj = json.loads((Path(new_dir) / "Timelines" / "project.json")
                        .read_text(encoding="utf-8"))
        tid = str(pj.get("main_timeline_id") or "")
    except Exception:
        tid = ""
    if tid:
        tl = Path(new_dir) / "Timelines" / tid
        out += [tl / "draft_info.json", tl / "template-2.tmp"]
    return out


def read_body(new_dir):
    """读回新草稿的明文正文（第一个以 `{` 开头的文件）。"""
    for f in body_files(new_dir):
        if not f.exists():
            continue
        if f.read_bytes()[:1] != b"{":
            continue
        return json.loads(f.read_text(encoding="utf-8"))
    raise FileNotFoundError("新草稿里没有可读的明文正文")


def write_body(new_dir, nd):
    """把正文写回上述全部副本。

    每份都走原子写：剪映可能正开着另一份草稿在读草稿根下的文件，先截断再写
    会留下一个「空文件」窗口（实测剪映遇到空正文直接把草稿判成打不开）。
    """
    from . import paths
    txt = json.dumps(nd, ensure_ascii=False)
    got = []
    for f in body_files(new_dir):
        paths.atomic_write(f, txt)
        got.append(str(f))
    return got


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

    macOS 11.8 的 `draft_file_path` 是
    `##_draftpath_placeholder_<uuid>_##/subdraft/<uuid>/draft_content.json`：
    占位符 = 当前草稿目录。这种形式**不能**改写成绝对路径（等于把它写死），
    只需把子草稿目录搬到新草稿里，路径原样留着就对了。
    """
    from . import draftio
    out = []
    for e in (nd.get("materials") or {}).get("drafts") or []:
        if not isinstance(e, dict):
            continue
        p = str(e.get("draft_file_path") or "")
        if not p:
            continue
        rel, placeholder = draftio.subdraft_rel(p, src_dir)
        if rel is None:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "why": "子草稿路径既不指向快照也不在 subdraft 下",
                        "from": p})
            continue
        src_d = Path(src_dir) / rel.parent
        dst_d = Path(new_dir) / rel.parent
        if not src_d.is_dir():
            # 快照里没有子草稿（老快照 / 源草稿已被删）：新草稿若已经带着
            # 同名目录（整目录克隆时搬过来的）就算过，否则如实报出来。
            out.append({"compound_id": e.get("id"), "ok": dst_d.is_dir(),
                        "from": p, "to": str(dst_d).replace("\\", "/"),
                        "why": None if dst_d.is_dir() else "快照里没有这份子草稿"})
            continue
        try:
            shutil.copytree(src_d, dst_d, dirs_exist_ok=True)
        except Exception as ex:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "from": p, "why": str(ex)})
            continue
        if not placeholder:
            e["draft_file_path"] = str(dst_d / rel.name).replace("\\", "/")
        out.append({"compound_id": e.get("id"), "ok": True,
                    "from": p, "to": e["draft_file_path"],
                    "placeholder": placeholder})
    return out


def compound_intact(src_draft, new_draft, problems, src_folder=None,
                    new_folder=None, unverified=None):
    """复合片段内部时间线必须一字节没变。

    为什么单独查这一项：复合片段在剪映里是「一个段」，但内部有自己的时间线。
    LUT 只挂在外层段上，照理不会碰到内部；可一旦哪天克隆逻辑动到了
    materials.drafts[].draft，用户在复合片段里的剪辑就会被打乱 ——
    这种问题肉眼在面板上看不出来，必须逐字节比。

    2026-11-23 修：以前两边都读不到内部时间线时（`compound_inner` 返回
    "missing"），签名都是空串，于是被判成「一字节没变」并计入 ok ——
    子草稿没复制进新草稿（实测：快照里缺 subdraft）时，报告照样是
    compound_ok == compound_src，用户拿到的却是一份复合片段空掉的新草稿。

    现在的判据分三种：
      · 源能读、新读不到 —— 是我们的复制出了问题，算**问题**（写回失败）；
      · 两边都能读 —— 逐字节比，不同就算问题；
      · 两边都读不到 —— 源草稿自己就是坏的，没法核对，记进 unverified
        （仍然算过，但报告里留痕，不假装核对过）。
    """
    a = draftio.compound_index(src_draft)
    b = draftio.compound_index(new_draft)
    ok = 0
    for cid, ea in a.items():
        eb = b.get(cid)
        if eb is None:
            problems.append({"compound_id": cid, "why": "复合片段在新草稿里没了"})
            continue
        ia, ha = draftio.compound_inner(ea, src_folder)
        ib, hb = draftio.compound_inner(eb, new_folder)
        if ib is None and ia is not None:
            problems.append({
                "compound_id": cid,
                "why": "复合片段的内容没跟着复制进新草稿（新草稿里会是空的）",
                "src": ha, "new": hb,
                "new_path": draftio.compound_path(eb, new_folder),
            })
            continue
        if ia is None and ib is None:
            # 源草稿自己就读不到（子草稿文件早就不在了）。不敢说「没变」，
            # 但也不该把写回判死 —— 这是源草稿的问题，不是这次往返弄坏的。
            if unverified is not None:
                unverified.append({"compound_id": cid,
                                   "why": "源草稿里的复合片段内容读不到（%s）" % ha,
                                   "path": draftio.compound_path(eb, new_folder)})
            ok += 1
            continue
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


def frames_of(us):
    """微秒 → 剪映时间线上的帧号（索引从 0 起，和「第几帧」一致）。"""
    return int(round((int(us or 0)) / 1e6 * paths.FPS))


def basics_index(draft):
    """回剪映最要紧的那几样：画布、总长（微秒与帧）、每段起止帧、素材像素尺寸。

    剪映草稿用**微秒**存时间，30 帧/秒换算成帧。所以「帧数变没变」不是再算一遍
    剪辑，而是比微秒值：微秒一致 → 帧数必然一致；微秒差 1 也可能跨过帧的边界
    （例如 1_033_333 与 1_033_000 都是第 31 帧，但 33_333 与 33_000 就未必）。
    两边都记下来，报告里能直接看到「第几帧到第几帧」。
    """
    fps = paths.FPS
    cc = draft.get("canvas_config") or {}
    lanes = {}
    for i, tr in enumerate(draft.get("tracks") or []):
        if not isinstance(tr, dict):
            continue
        segs = {}
        for sg in tr.get("segments") or []:
            if not isinstance(sg, dict) or not sg.get("id"):
                continue
            tt = sg.get("target_timerange") or {}
            st = sg.get("source_timerange") or {}
            start, dur = int(tt.get("start") or 0), int(tt.get("duration") or 0)
            segs[str(sg["id"])] = {
                "material": str(sg.get("material_id") or ""),
                "start_us": start, "dur_us": dur,
                "start": frames_of(start), "dur": frames_of(dur),
                "end": frames_of(start) + frames_of(dur),
                "src_us": (None if not st else int(st.get("start") or 0)),
                "src_dur_us": (None if not st else int(st.get("duration") or 0)),
            }
        lanes[str(i)] = {"type": str(tr.get("type") or ""), "segments": segs}
    mats = {}
    for kind in ("videos", "photos", "audios"):
        for m in (draft.get("materials") or {}).get(kind) or []:
            if isinstance(m, dict) and m.get("id"):
                mats[str(m["id"])] = {
                    "kind": kind,
                    "w": m.get("width"), "h": m.get("height"),
                    "dur_us": m.get("duration"),
                    "path": str(m.get("path") or ""),
                }
    dur = int(draft.get("duration") or 0)
    return {
        "fps": fps,
        "canvas": [cc.get("width"), cc.get("height")],
        "color_space": draft.get("color_space"),
        "duration_us": dur,
        "duration": frames_of(dur),
        "lanes": lanes,
        "materials": mats,
    }


def basics_diff(src, new):
    """写回前后比基础信息，返回问题清单（空 = 帧数与分辨率一帧一像素都没动）。"""
    a, b = basics_index(src), basics_index(new)
    probs = []
    if a["canvas"] != b["canvas"]:
        probs.append({"why": "画布分辨率变了", "src": a["canvas"], "new": b["canvas"]})
    if a["duration_us"] != b["duration_us"]:
        probs.append({"why": "草稿总长变了（微秒）", "src": a["duration_us"],
                      "new": b["duration_us"], "src_frames": a["duration"],
                      "new_frames": b["duration"]})
    if a["duration"] != b["duration"]:
        probs.append({"why": "总帧数变了", "src": a["duration"], "new": b["duration"]})
    for lane, ea in a["lanes"].items():
        eb = b["lanes"].get(lane)
        if eb is None:
            probs.append({"why": "轨道没了", "lane": lane, "type": ea["type"]})
            continue
        for sid, sa in ea["segments"].items():
            sb = eb["segments"].get(sid)
            if sb is None:
                probs.append({"why": "段没了", "lane": lane, "segment_id": sid})
                continue
            for k, label in (("start", "起始帧"), ("dur", "时长帧"),
                             ("start_us", "起始时间"), ("dur_us", "时长"),
                             ("src_us", "素材入点"), ("src_dur_us", "素材区间")):
                if sa[k] != sb[k]:
                    probs.append({"why": label + "变了", "lane": lane,
                                  "segment_id": sid, "field": k,
                                  "src": sa[k], "new": sb[k]})
    for mid, ma in a["materials"].items():
        mb = b["materials"].get(mid)
        if mb is None:
            probs.append({"why": "素材没了", "material_id": mid,
                          "path": ma["path"]})
            continue
        if (ma["w"], ma["h"]) != (mb["w"], mb["h"]):
            probs.append({"why": "素材像素尺寸变了", "material_id": mid,
                          "path": ma["path"],
                          "src": [ma["w"], ma["h"]], "new": [mb["w"], mb["h"]]})
        if ma["dur_us"] != mb["dur_us"]:
            probs.append({"why": "素材时长变了", "material_id": mid,
                          "path": ma["path"], "src": ma["dur_us"],
                          "new": mb["dur_us"]})
    return probs


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
    # 音频轨与图片段：写回是「整份克隆 + 只挂 LUT」，这两类天生就在。
    # 单列出来是因为它们最容易被误以为「没送进达芬奇 = 写回也没了」。
    photo_ids = {m.get("id") for m in mats.get("photos") or []
                 if isinstance(m, dict) and m.get("id")}
    n_photo = 0
    for tr in tracks:
        for sg in tr.get("segments") or []:
            if isinstance(sg, dict) and sg.get("material_id") in photo_ids:
                n_photo += 1
    n_audio_seg = sum(len(t.get("segments") or []) for t in tracks
                      if t.get("type") == "audio")
    return {
        "tracks": len(tracks),
        "overlay_tracks": max(0, len(video_tracks) - 1),
        "audio_tracks": len([t for t in tracks if t.get("type") == "audio"]),
        "audio_segments": n_audio_seg,
        "photo_segments": n_photo,
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
    ok, why = compat.version_ok()
    if ok is False:
        rep["error"] = why
        return rep
    from . import snapshot
    ptr = snapshot.current()
    if ptr is None:
        rep["error"] = ("还没有完成过一次「送入达芬奇」。顺序是："
                        "送入达芬奇 → 在达芬奇里调色 → 写回剪映。")
        return rep
    if ptr.get("_incomplete"):
        # 同 pull：面板里没有终端，别让用户去敲 `colorpass reset`。
        rep["need_reset"] = True
        rep["error"] = ("最近一次「送入达芬奇」没有走完（%s 开始，时间线只摆了"
                        "一部分），写回会把残缺的剪辑带回去。\n\n"
                        "两条路：重新送一次；或者丢掉这次没走完的记录，"
                        "按上一次成功的版本写回。"
                        % (ptr.get("pending_at") or "上一轮"))
        return rep
    did = ptr.get("draft_id")
    run_dir = ptr.get("run_dir") or paths.draft_run_dir(did)

    def _fail(why, **extra):
        return draftio.fail(rep, os.path.join(run_dir, "write.json"), why, **extra)

    rep["draft"] = ptr.get("draft_name")
    rep["draft_id"] = did
    rep["snapshot_at"] = ptr.get("snapshot_at")
    rep["snapshot_sha"] = ptr.get("snapshot_sha")
    try:
        draft, snap_meta = snapshot.snapshot_of(ptr)
    except FileNotFoundError as exc:
        return _fail("%s —— 这一版没留下快照，请重新送入" % exc)
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
        draftio.prog("正在取回调色…")
        pl = pull.run()
        rep["pull"] = {k: pl.get(k) for k in
                       ("ok", "error", "count", "timeline", "alignment_ok",
                        "identity_dev_max", "identity_graded")
                       if k in pl}
        rep["pull_issues"] = pl.get("issues") or []
        if not pl.get("ok"):
            return _fail("取回调色没成功，已中止写回：%s"
                         % (pl.get("error") or "未知原因"),
                         alignment_ok=pl.get("alignment_ok"),
                         identity_dev_max=pl.get("identity_dev_max"))
    else:
        pull_path = os.path.join(run_dir, "pull.json")
        if not os.path.exists(pull_path):
            return _fail("缺少 pull.json（并且关掉了自动取回），无法写回")
        pl = json.loads(Path(pull_path).read_text(encoding="utf-8"))
        # 这份 pull.json 是不是「另一次送入」留下的？对不上就直接中止，
        # 否则 LUT 会按旧段 ID 往新草稿上挂（实测踩过：13 段的新计划配 14 段的旧 pull）。
        plan_path = Path(os.path.join(run_dir, "plan.json"))
        if not plan_path.exists():
            # 以前这里直接 read_bytes()，plan.json 不在时抛的是
            # FileNotFoundError —— 面板只看到「未完成（退出码 1）」，不知道原因。
            return _fail("缺少 plan.json（并且关掉了自动取回），无法校验收回结果")
        cur_sha = __import__("hashlib").sha1(plan_path.read_bytes()).hexdigest()[:16]
        if pl.get("plan_sha") and pl["plan_sha"] != cur_sha:
            return _fail("pull.json 是上一版计划留下的（%s ≠ %s），"
                         "请去掉 --no-pull 重新取回"
                         % (pl["plan_sha"], cur_sha))
    rep["alignment_ok"] = pl.get("alignment_ok")
    rep["identity_ok"] = pl.get("identity_ok")
    if pl.get("alignment_ok") is not True:
        return _fail("时间线与计划不一致，拒绝写回（LUT 会挂错段）")
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
    if not seg_luts:
        # 取回「成功」但一段 LUT 都没有：要么整条时间线的段都被停用了，要么
        # 达芬奇里那条线根本不是这次送入的。照原样往下走会生成一份**没调过色**
        # 的新草稿 —— 用户看到「写回完成」和一条新草稿，会以为调色丢了。
        skipped = pl.get("disabled_skipped") or []
        why = ("时间线上的 %d 段全是停用状态，没有可导出的片段" % len(skipped)
               if skipped else "达芬奇时间线上没有可导出的片段")
        return _fail("%s，写回会得到一份没调过色的草稿，所以停住了。"
                     "请确认调色的是「送入达芬奇」建的那条时间线（名字以 CP · 开头）。"
                     % why)
    bad_cubes = verify_cubes(seg_luts)
    rep["cube_problems"] = bad_cubes
    if bad_cubes:
        return _fail("%d 个 cube 不可用，拒绝写回（会让剪映显示 LUT 丢失）" % len(bad_cubes))

    src_recs = draftio.transition_records(draft)
    src_seg = set(src_recs)
    orphan = [s for s in seg_luts if s not in src_seg]
    rep["orphan"] = orphan
    if orphan:
        # 段 ID 只可能来自同一次送入，对不上就是快照/计划被覆盖过（比如重新送入
        # 前没删旧 run 目录）。写回会把 LUT 挂到不存在的段上，只能中止。
        return _fail(("%d 个 segment_id 在送入时的快照里不存在，" "快照与达芬奇时间线不是同一次送入的，拒绝写回" % len(orphan)))

    rep["requested_name"] = new_name or ""
    draftio.prog("正在生成新草稿…")
    # 克隆骨架从哪来：优先源草稿目录（最完整）。它要是被删了/改名了，
    # 退回「送入时存下的那份骨架」—— 快照里存了一小份，足以重建目录结构。
    src_folder = cur.get("folder") or ""
    if not (Path(src_folder) / "Timelines" / "project.json").is_file():
        skel = (snap_meta.get("skeleton") or {})
        skel_dir = skel.get("dir") or str(Path(snap_dir) / "skeleton")
        if (Path(skel_dir) / "Timelines" / "project.json").is_file():
            if platforms.is_mac():
                missing = snapshot.skeleton_missing_assets(
                    draft, cur.get("folder"), skel_dir, skel.get("skipped_dirs"))
                if missing:
                    rep["skeleton_missing"] = missing
                    return _fail(
                        "原草稿已不在原处，而它引用的 %d 个随稿文件没有进入快照骨架"
                        "（第一个是 %s）。继续写回会得到素材离线的新草稿，所以停住了。\n"
                        "请先从剪映回收站恢复原草稿并重新送入；若只是目录改名，"
                        "把名字改回去后再写回。"
                        % (len(missing), missing[0].get("relative") or "未知文件"))
            src_folder = skel_dir
            rep["src_rebuilt"] = True
            rep["src_rebuilt_note"] = (
                "送入时的那份剪映草稿已经不在原处，已用送入时的目录骨架重建"
                "（素材仍指向原文件，复合片段用的是快照里的副本）。")
            draftio.prog("!! 原草稿已不在原处，已按快照骨架重建")
    try:
        r = WF.write_back([], {}, draft=draft, segment_luts=seg_luts,
                          project=cur.get("name"), name=new_name,
                          src_folder=src_folder or None)
    except Exception as exc:
        # 写草稿落在剪映目录里，可能撞上权限/磁盘满/索引被占用。以前这些异常
        # 直接冒到 CLI 顶层，用户只看到一段 traceback。
        return _fail("生成新草稿时出错：%s: %s" % (type(exc).__name__, exc))
    rep["write_back"] = {k: v for k, v in r.items()
                         if k in ("ok", "name", "error", "luts", "mode",
                                  "tracks", "segments")}
    if not r.get("ok"):
        return _fail(r.get("error") or "write_back 失败")

    new_dir = None
    for cand in (r.get("path"), r.get("dir"), r.get("folder")):
        if cand and Path(str(cand)).is_dir():
            new_dir = Path(str(cand))
            break
    if new_dir is None:
        c = Path(WF.JY.draft_root()) / r["name"]
        new_dir = c if c.is_dir() else None
    if new_dir is None:
        return _fail("找不到新草稿目录（无法嵌入 LUT）")
    rep["new_draft"] = str(new_dir)
    rep["draft_name"] = Path(new_dir).name
    rep["compound_src"] = len(draftio.compound_index(draft))

    # 嵌入 + 改指向
    draftio.prog("正在把 %d 段 LUT 嵌进新草稿…" % len(seg_luts))
    try:
        mapping, copied = embed_cubes(new_dir, seg_luts)
    except Exception as exc:
        return _fail("把 cube 复制进新草稿失败：%s: %s" % ( type(exc).__name__, exc))
    rep["embedded"] = copied
    try:
        nd = read_body(new_dir)
    except Exception as exc:
        return _fail("新草稿的正文读不出来：%s" % exc)
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
        rep["body_written"] = write_body(new_dir, nd)
    except Exception as exc:
        return _fail("改写新草稿的 LUT 路径时写盘失败：%s: %s" % ( type(exc).__name__, exc))
    rep["paths_swapped"] = swapped
    # 子草稿没搬过去 = 新草稿里的复合片段会是空的。这件事必须在写回结果里
    # 直接失败，不能等用户打开剪映才发现（以前 ok 只由 problems 决定，
    # subdrafts 一个都没成功也照样报「写回完成」）。
    bad_subs = [x for x in (rep["subdrafts"] or []) if not x.get("ok")]
    if bad_subs:
        rep["new_draft"] = str(new_dir)
        rep["draft_name"] = Path(new_dir).name
        return _fail("有 %d 个复合片段的内容没能复制进新草稿（%s）：新草稿里这些"
                     "片段会是空的。\n新草稿「%s」已生成（%s），确认不用可以删掉；"
                     "建议先重新「送入」一次再写回。"
                     % (len(bad_subs), bad_subs[0].get("why") or "未知原因",
                        Path(new_dir).name, new_dir))
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
    draftio.prog("正在逐段核对…")
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
    compound_unverified = []
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
        "compound_ok": compound_intact(draft, nd, problems,
                                       src_folder=snap_dir, new_folder=str(new_dir),
                                       unverified=compound_unverified),
        "compound_unverified": compound_unverified,
        "structure_problems": struct_probs,
        # 这一版保住了什么 —— 面板直接拿来显示，别再让用户猜
        "kept": kept_summary(draft),
    })
    # 基础信息（帧数/画布/素材像素）逐项核对：结构对不对是一回事，
    # 「回去以后是不是还那 675 帧、还是 1920×1080」是另一回事。
    basics_probs = basics_diff(draft, nd)
    bi_src, bi_new = basics_index(draft), basics_index(nd)
    rep["basics"] = {
        "fps": bi_new["fps"],
        "canvas_src": bi_src["canvas"], "canvas_new": bi_new["canvas"],
        "duration_us_src": bi_src["duration_us"],
        "duration_us_new": bi_new["duration_us"],
        "frames_src": bi_src["duration"], "frames_new": bi_new["duration"],
        "segments_checked": sum(len(v["segments"]) for v in bi_src["lanes"].values()),
        "materials_checked": len(bi_src["materials"]),
    }
    rep["basics_problems"] = basics_probs
    rep["basic_ok"] = not basics_probs
    # 三处对账：剪映原稿（快照）→ 达芬奇时间线（plan.json 的帧号）→ 写回稿。
    # 只比快照和写回稿是「抄得对不对」，加上 plan 才是「摆过去又拿回来」整圈。
    plan_frames = {}
    try:
        pf = json.loads((Path(run_dir) / "plan.json").read_text(encoding="utf-8"))
        for row in pf.get("segments") or []:
            if isinstance(row, dict) and row.get("segment_id"):
                plan_frames[str(row["segment_id"])] = (row.get("rec"), row.get("dur"))
    except Exception:
        plan_frames = {}
    moved = []
    checked_plan = 0
    if plan_frames:
        for lane in bi_new["lanes"].values():
            for sid, sg in lane["segments"].items():
                if sid not in plan_frames:
                    continue
                checked_plan += 1
                want = plan_frames[sid]
                got = (sg["start"], sg["dur"])
                if want != got:
                    moved.append({"segment_id": sid, "davinci": list(want),
                                  "jiemying": list(got)})
    rep["frame_check"] = {
        "plan_segments": len(plan_frames),
        "checked": checked_plan,
        "mismatched": moved,
        "timeline_end_frames": bi_new["duration"],
    }
    rep["ok"] = (not problems and not struct_probs and not basics_probs
                 and not moved
                 and seen == len(rows)
                 and rep["duration_src"] == rep["duration_new"]
                 and rep["tracks_src"] == rep["tracks_new"]
                 and rep["compound_ok"] == rep["compound_src"])
    if struct_probs:
        rep["error"] = ("写回后结构对不上（%d 处）：%s —— 新草稿可能缺转场/"
                        "变速/蒙版/特效，已按失败处理"
                        % (len(struct_probs), struct_probs[0].get("why")))
    elif basics_probs:
        rep["error"] = ("写回后帧数/分辨率对不上（%d 处）：%s —— 已按失败处理"
                        % (len(basics_probs), basics_probs[0].get("why")))
    elif moved:
        rep["error"] = ("有 %d 段在达芬奇里摆的帧号与写回剪映的帧号不一致"
                        "（第一段 %s：达芬奇 %s / 剪映 %s），已按失败处理"
                        % (len(moved), moved[0]["segment_id"],
                           moved[0]["davinci"], moved[0]["jiemying"]))
    elif rep["compound_ok"] != rep["compound_src"]:
        # 复合片段的数量/内容对不上时，前面三处都不会命中，用户只会看到
        # 「写回失败」而没有任何原因 —— 补一句指明是复合片段。
        first = next((p for p in problems if p.get("compound_id")), {})
        rep["error"] = ("复合片段核对没过（%d/%d 个一致）：%s —— 已按失败处理"
                        % (rep["compound_ok"], rep["compound_src"],
                           first.get("why") or "内部时间线对不上"))
    if not rep["ok"] and rep.get("new_draft"):
        # 核对没过不等于草稿没生成：目录已经在剪映列表里了。不把这句话写出来，
        # 用户看到的是「写回失败」＋剪映里多出一条草稿，只会以为工具搞坏了东西。
        # 只能放在三个 if 之后 —— 前面那几处会覆盖 rep["error"]。
        tail = ("新草稿「%s」已生成并留在剪映的草稿列表里（%s）。"
                "如果确认不用，直接删掉这条草稿即可。"
                % (rep.get("draft_name") or Path(rep["new_draft"]).name,
                   rep["new_draft"]))
        rep["error"] = ("%s\n%s" % (rep["error"], tail)) if rep.get("error") else tail
    draftio.dump_json(rep, os.path.join(run_dir, "write.json"))
    return rep
