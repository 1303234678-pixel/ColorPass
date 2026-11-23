"""规模压力测试：草稿里段数一多，哪里会慢、哪里会错。

两种模式：

    python tools/stress_scale.py --offline 50,200,500,1000
        纯 Python 侧，不碰达芬奇：合成 N 段草稿，逐阶段计时
        （计划 / 快照 / 体检 / 写回克隆 / 基础信息核对）。
        用来找「线性还是平方」——段数翻倍，耗时翻倍才对。

    python tools/stress_scale.py --resolve 60,180
        真机：用 ffmpeg 造几段真素材，合成 N 段草稿，走完整
        send.run → pull.run → write.run，逐阶段计时，并核对
        落位帧号、标记数、总长、LUT 数、回剪映后的帧数。
        跑完删掉实验时间线（不动剪映里的草稿）。

合成草稿刻意做成「脏」的：每 7 段一个转场、每 11 段一个变速、
每 13 段一组关键帧、每 17 段一张图片、每条轨都有停用段 ——
真实工程里让代码分支变多的东西都在里面。
"""
import argparse
import contextlib
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import audit, draftbuild, draftio, paths, send, snapshot, write  # noqa: E402

SEG_US = 1_000_000          # 每段 30 帧（30fps）
PHOTO_US = 3_000_000        # 图片段 3 秒 = 90 帧


# ---------------------------------------------------------------- 合成草稿

def make_draft(n, n_src=4, with_photos=True, with_audio=True,
               media_path=None, photo_path=None, photo_paths=None,
               photo_every=0, overlay_every=0):
    """N 段首尾相接的视频轨 + 音频轨 + 文字/贴纸轨，带转场/变速/关键帧/停用。

    media_path 必须真实存在（build_plan 会跳过不在盘上的素材）。默认拿当前
    解释器本体冒充素材 —— 这里只测计划，不解码。
    """
    media_path = (media_path or sys.executable).replace("\\", "/")
    photos = []
    if photo_paths:
        for k, p in enumerate(photo_paths):
            photos.append({"id": "PH%d" % k, "path": str(p).replace("\\", "/"),
                           "width": 1520, "height": 1836,
                           "duration": PHOTO_US, "type": "photo"})
    if with_photos or photo_paths:
        photos.append({"id": "PH0", "path": (photo_path or media_path),
                       "width": 1520, "height": 1836, "duration": PHOTO_US,
                       "type": "photo"})
    seen, uniq = set(), []
    for ph in photos:
        if ph["id"] in seen:
            continue
        seen.add(ph["id"])
        uniq.append(ph)
    photos = uniq
    photo_ids = [p["id"] for p in photos if p.get("path")]
    vids = [{"id": "V%d" % i, "path": media_path, "duration": 600 * SEG_US,
             "width": 1920, "height": 1080} for i in range(n_src)]
    speeds, transitions, masks = [], [], []
    segs, segs2 = [], []
    t = 0
    n_photo = 0
    for i in range(n):
        sid = "S%04d" % i
        refs = []
        if i and i % 7 == 0:
            tid = "TR%d" % i
            transitions.append({"id": tid, "name": "叠化", "duration": 500_000})
            refs.append(tid)
        if i and i % 11 == 0:
            spid = "SP%d" % i
            speeds.append({"id": spid, "speed": 1.5})
            refs.append(spid)
        if i and i % 13 == 0:
            mid = "MK%d" % i
            masks.append({"id": mid, "name": "线性"})
            refs.append(mid)
        # 图片段：photo_every 优先（真机压批量转码/导入用它），
        # 否则退回「每 17 段一张」的默认脏数据。
        if photo_every:
            is_photo = i % photo_every == 0 and bool(photo_ids)
        else:
            is_photo = with_photos and i and i % 17 == 0
        dur = PHOTO_US if is_photo else SEG_US
        seg = {
            "id": sid,
            "material_id": (photo_ids[n_photo % len(photo_ids)] if is_photo
                            else "V%d" % (i % n_src)),
            "extra_material_refs": refs,
            "target_timerange": {"start": t, "duration": dur},
            "common_keyframes": (
                [{"property_type": "KFTypePositionX", "keyframe_list": [
                    {"time_offset": 0, "values": [0.0]},
                    {"time_offset": dur // 2, "values": [0.2]}]}]
                if i and i % 13 == 0 else []),
            "visible": i % 19 != 0,
        }
        if is_photo:
            n_photo += 1
            seg["source_timerange"] = {"start": 0, "duration": dur}
        else:
            # 变速段的 source 比 target 长（剪映的真实形态）
            src_dur = int(round(dur * 1.5)) if "SP%d" % i in refs else dur
            seg["source_timerange"] = {"start": (i % 5) * SEG_US,
                                       "duration": src_dur}
        segs.append(seg)
        # 画中画：同一时间区间在 V2 上再叠一段（V1 底下不断），
        # 这才是剪映里「画中画」的真实形态，也才会逼出第二条视频轨。
        if overlay_every and i and i % overlay_every == 0:
            segs2.append({
                "id": "S%04d-o" % i,
                "material_id": "V%d" % ((i + 1) % n_src),
                "extra_material_refs": [],
                "source_timerange": {"start": 0, "duration": SEG_US},
                "target_timerange": {"start": t, "duration": SEG_US},
                "common_keyframes": [],
                "visible": True,
            })
        t += dur

    tracks = [{"id": "TV1", "type": "video", "segments": segs}]
    if segs2:
        tracks.append({"id": "TV2", "type": "video", "segments": segs2})
    if with_audio:
        a_segs = []
        at = 0
        for k in range(3):
            a_segs.append({
                "id": "A%d" % k, "material_id": "AU0",
                "extra_material_refs": [],
                "target_timerange": {"start": at, "duration": 4 * SEG_US},
            })
            at += 4 * SEG_US
        tracks.append({"id": "TA1", "type": "audio", "segments": a_segs})
    tracks.append({"id": "TT1", "type": "text", "segments": [{
        "id": "T0", "material_id": "TX0",
        "target_timerange": {"start": 0, "duration": 2 * SEG_US}}]})
    tracks.append({"id": "TS1", "type": "sticker", "segments": [{
        "id": "ST0", "material_id": "SK0",
        "target_timerange": {"start": SEG_US, "duration": SEG_US}}]})

    return {
        "id": "STRESS%05d" % n,
        "name": "压力 %d 段" % n,
        "duration": t,
        "canvas_config": {"width": 1920, "height": 1080, "ratio": "original"},
        "materials": {
            "videos": vids, "photos": photos,
            "audios": ([{"id": "AU0", "path": media_path, "name": "音乐",
                         "type": "music", "duration": 600 * SEG_US}]
                       if with_audio else []),
            "speeds": speeds, "transitions": transitions,
            "common_mask": masks, "drafts": [], "effects": [],
            "audio_fades": [],
            "texts": [{"id": "TX0"}], "stickers": [{"id": "SK0"}],
        },
        "tracks": tracks,
    }


def tiny_cube(path):
    """写一份 33 点恒等 cube（≈ 80 KB），给写回克隆用。"""
    lines = ['TITLE "identity"', "LUT_3D_SIZE 33"]
    n = 33
    for b in range(n):
        for g in range(n):
            for r in range(n):
                lines.append("%.6f %.6f %.6f" % (r / (n - 1), g / (n - 1),
                                                 b / (n - 1)))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- offline

def run_offline(n, luts=0, keep=False):
    tmp = Path(tempfile.mkdtemp(prefix="cpstress_"))
    d = make_draft(n, photo_path=sys.executable, media_path=sys.executable)
    cur = {"name": d["name"], "folder": str(ROOT), "timeline_id": d["id"]}
    run_dir = tmp / "run"
    run_dir.mkdir()
    out = {"n": n, "bytes": {}, "ms": {}, "facts": {}}

    def clock(label, fn):
        t0 = time.perf_counter()
        val = fn()
        out["ms"][label] = round((time.perf_counter() - t0) * 1000, 1)
        return val

    out["bytes"]["draft"] = len(json.dumps(d, ensure_ascii=False).encode())
    clock("features", lambda: draftio.feature_counts(d))
    clock("layers", lambda: draftio.layers(d))
    clock("masks", lambda: draftio.mask_segments(d))
    clock("transform", lambda: draftio.transform_segments(d))
    clock("unsupported", lambda: draftio.unsupported(d))
    plan, skipped, overlaps, trimmed, shifted = clock(
        "build_plan", lambda: send.build_plan(d, "bake"))
    aplan, askip = clock("build_audio_plan", lambda: send.build_audio_plan(d))
    meta = clock("snapshot_save", lambda: snapshot.save(d, cur, str(run_dir)))
    out["facts"].update({
        "plan": len(plan), "skipped": len(skipped),
        "overlaps": len(overlaps), "trimmed": len(trimmed),
        "audio": len(aplan), "kinds": {},
    })
    for c in plan:
        out["facts"]["kinds"][c["kind"]] = out["facts"]["kinds"].get(
            c["kind"], 0) + 1
    snap_dir = Path(meta["dir"])
    out["bytes"]["snapshot"] = (snap_dir / "draft_content.json").stat().st_size

    b, w, f = clock("audit", lambda: audit.scan_draft(d))
    out["facts"]["audit"] = {"block": len(b), "warn": len(w)}

    idx = clock("basics_index", lambda: write.basics_index(d))
    clock("structure_index", lambda: write.structure_index(d))
    clock("kept_summary", lambda: write.kept_summary(d))

    # 写回克隆：跟 write.run 同一条路径（draftbuild.build_from_draft → 落盘）
    seg_luts = {}
    lut_files = []
    if luts:
        for k, c in enumerate([x for x in plan if x["kind"] == "video"][:luts]):
            p = tmp / "luts" / ("%s.cube" % c["segment_id"])
            p.parent.mkdir(exist_ok=True)
            tiny_cube(p)
            seg_luts[c["segment_id"]] = str(p)
            lut_files.append(p)
    root = tmp / "drafts"
    root.mkdir()
    r = clock("write_build", lambda: draftbuild.build_from_draft(
        d, "压力写回", {}, segment_luts=seg_luts, root=str(root)))
    if r.get("ok"):
        new_dir = Path(r.get("folder") or root / "压力写回")
        content = new_dir / "draft_content.json"
        nd = json.loads(content.read_text(encoding="utf-8"))
        out["bytes"]["written"] = content.stat().st_size
        probs = clock("basics_diff", lambda: write.basics_diff(d, nd))
        sprobs = clock("structure_diff", lambda: write.structure_diff(d, nd))
        out["facts"]["basics_problems"] = len(probs)
        out["facts"]["structure_problems"] = len(sprobs)
        out["facts"]["same_duration"] = (idx["duration"] ==
                                         write.basics_index(nd)["duration"])
    else:
        out["facts"]["write_error"] = r.get("error")

    out["ms"]["total"] = round(sum(v for k, v in out["ms"].items()
                                   if k != "write_build"), 1)
    if keep:
        out["kept"] = str(tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ---------------------------------------------------------------- resolve

def make_media(dirpath, count, seconds=1.0, size="640x360"):
    """造 count 段真素材（H.264，无音轨，小尺寸，编码很快）。"""
    from colorpass import retime
    exe = retime._ffmpeg()
    out = []
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = dirpath / ("src%03d.mp4" % i)
        if p.exists():
            out.append(str(p))
            continue
        # 每段一个可区分的颜色 + 位置不同的白块，肉眼也能看出「摆错了没」
        r, g, b = (40 + (i * 53) % 200, 40 + (i * 97) % 200,
                   40 + (i * 31) % 200)
        box_x = (i * 37) % 560
        box_y = (i * 61) % 300
        cmd = [exe, "-y", "-v", "error", "-f", "lavfi",
               "-i", "color=c=0x%02x%02x%02x:s=%s:d=%.2f:r=30"
               % (r, g, b, size, seconds),
               "-vf", "drawbox=x=%d:y=%d:w=40:h=40:color=white:t=fill"
               % (box_x, box_y),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)]
        import subprocess
        subprocess.run(cmd, capture_output=True)
        if p.exists():
            out.append(str(p))
    return out


def make_pics(dirpath, count):
    """造 count 张不同颜色的静帧（竖图，模拟手机照片）。"""
    from colorpass import retime
    exe = retime._ffmpeg()
    out = []
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = dirpath / ("pic%03d.png" % i)
        if not p.exists():
            r, g, b = (40 + (i * 71) % 200, 40 + (i * 43) % 200,
                       40 + (i * 113) % 200)
            import subprocess
            subprocess.run([exe, "-y", "-v", "error", "-f", "lavfi",
                            "-i", "color=c=0x%02x%02x%02x:s=800x1000"
                            % (r, g, b), "-frames:v", "1", str(p)],
                           capture_output=True)
        if p.exists():
            out.append(p)
    return out


@contextlib.contextmanager
def _hermetic(tmp):
    """把这次压力测试的所有状态关进临时目录，跑完原样还回去。

    要盖住三样：快照指针（current/pending）、run 目录、LUT 池。
    不盖的话，跑完一轮「最近一次送入」就指向一条已经删掉的时间线 ——
    面板会说「上一轮没走完」，体检/取色也拿不到正确上下文，用户还得
    自己 reset 一次。压力测试不该留这种后遗症。
    """
    old = (snapshot.CURRENT_PATH, snapshot.PENDING_PATH,
           paths.draft_run_dir, paths.draft_lut_dir)
    snapshot.CURRENT_PATH = str(tmp / "current.json")
    snapshot.PENDING_PATH = str(tmp / "pending.json")

    def _dir(sub):
        def f(did=""):
            p = tmp / sub / str(did or "none")
            p.mkdir(parents=True, exist_ok=True)
            return str(p)
        return f

    paths.draft_run_dir = _dir("runs")
    paths.draft_lut_dir = _dir("luts")
    try:
        yield
    finally:
        (snapshot.CURRENT_PATH, snapshot.PENDING_PATH,
         paths.draft_run_dir, paths.draft_lut_dir) = old


def run_resolve(n, tag="CP-STRESS", keep=False, distinct=3, photos=0,
                overlay_every=0):
    from colorpass import resolve
    app = resolve.scriptapp("Resolve")
    if app is None:
        return {"ok": False, "error": "连不上达芬奇"}
    pm = app.GetProjectManager()
    proj = pm.GetCurrentProject()
    page = None
    try:
        page = app.GetCurrentPage()
    except Exception:
        pass
    if proj is None or not page:
        names = list(pm.GetProjectListInCurrentFolder() or [])
        try:
            (pm.LoadProject(names[0]) if names else pm.CreateProject("CP STRESS"))
        except Exception as exc:
            return {"ok": False, "error": "打不开工程：%s" % exc}
        time.sleep(4.0)
        proj = pm.GetCurrentProject()
        page = app.GetCurrentPage()
    if proj is None:
        return {"ok": False, "error": "没有工程"}

    tmp = Path(tempfile.mkdtemp(prefix="cpstress_media_"))
    n_src = max(1, min(int(distinct), n))
    t0 = time.perf_counter()
    srcs = make_media(tmp, n_src)
    pics = make_pics(tmp / "pics", int(photos)) if photos else []
    prep_ms = round((time.perf_counter() - t0) * 1000, 1)
    if len(srcs) < n_src:
        return {"ok": False, "error": "ffmpeg 造素材失败"}
    photo_every = max(1, n // len(pics)) if pics else 0
    d = make_draft(n, n_src=n_src, with_photos=bool(pics),
                   photo_paths=pics, photo_every=photo_every,
                   overlay_every=int(overlay_every),
                   with_audio=False, media_path=srcs[0])
    # 轮换真素材，别让所有段都指向同一个文件（媒体池命中路径不一样）。
    # 图片段的 material_id 是 PH*，不能在这里被覆盖。
    vids = d["materials"]["videos"]
    for i, v in enumerate(vids):
        v["path"] = srcs[i % len(srcs)].replace("\\", "/")
    for i, sg in enumerate(d["tracks"][0]["segments"]):
        if str(sg.get("material_id") or "").startswith("V"):
            sg["material_id"] = vids[i % len(vids)]["id"]

    cur = {"name": d["name"], "folder": str(tmp), "timeline_id": d["id"]}
    fresh = {"stale": False, "plaintext_bytes": 1, "disk_payload_bytes": 1,
             "delta_bytes": 0, "source": "stress"}
    rep = {"n": n, "distinct": n_src, "photos": len(pics),
           "overlay_every": int(overlay_every), "ok": False,
           "ms": {"prep": prep_ms}, "per_seg_ms": {}}

    with _hermetic(tmp):
        _pipeline(rep, d, cur, fresh, proj, tmp, n, tag, keep)
    shutil.rmtree(tmp, ignore_errors=True)
    return rep


def _pipeline(rep, d, cur, fresh, proj, tmp, n, tag, keep):
    """send → pull → write 全程。所有落盘状态都在 tmp 里（见 _hermetic）。"""
    from unittest import mock

    from colorpass import pull

    # send.run 会自己存快照、写 plan.json；这里只把「读剪映」换成构造草稿。
    t0 = time.perf_counter()
    with mock.patch.object(send.draftio, "read",
                           return_value=(d, cur, fresh)):
        srep = send.run(tag=tag, speed_mode="slot")
    total = (time.perf_counter() - t0) * 1000
    rep["ms"]["send_total"] = round(total, 1)
    rep["send"] = {k: srep.get(k) for k in
                   ("ok", "error", "timeline", "placed", "exact",
                    "failed", "timeline_end", "expected_end", "markers",
                    "marker_fail", "import_mode", "imported")}
    rep["send"]["failed"] = len(srep.get("failed") or [])
    rep["send"]["marker_fail"] = len(srep.get("marker_fail") or [])
    if not srep.get("ok"):
        rep["error"] = srep.get("error") or "送入失败"
        rep["send"]["first_fail"] = (srep.get("failed") or [{}])[0]
        rep["ms"]["total"] = rep["ms"]["send_total"]
        rep["per_seg_ms"]["send"] = round(total / n, 1)
        return rep

    tl = None
    for i in range(1, (proj.GetTimelineCount() or 0) + 1):
        t = proj.GetTimelineByIndex(i)
        if t and t.GetName() == srep.get("timeline"):
            tl = t
            break
    base = (tl.GetStartFrame() if tl else 0) or 0

    if tl is not None:
        t0 = time.perf_counter()
        preq = pull.run()
        rep["ms"]["pull_total"] = round((time.perf_counter() - t0) * 1000, 1)
        rep["pull"] = {k: preq.get(k) for k in
                       ("ok", "error", "count", "alignment_ok", "lut_type_used")}
        rep["pull"]["issues"] = len(preq.get("issues") or [])
        rep["pull"]["aligned"] = preq.get("aligned_segments")

        t0 = time.perf_counter()
        # 写回默认落在剪映的草稿目录里 —— 压力测试不该往用户的工程列表里
        # 塞一份 N 段假草稿。把 draft_root 指到临时目录，走的是同一条代码
        # 路径（克隆 → 嵌 cube → structure_diff / basics_diff）。
        from colorpass import probe as _probe
        drafts_tmp = tmp / "drafts"
        drafts_tmp.mkdir(exist_ok=True)
        with mock.patch.object(_probe, "draft_root",
                               return_value=drafts_tmp):
            wrep = write.run()
        rep["ms"]["write_total"] = round((time.perf_counter() - t0) * 1000, 1)
        rep["write"] = {k: wrep.get(k) for k in
                        ("ok", "error", "luts", "draft_name",
                         "duration_src", "duration_new")}
        rep["write"]["basics_problems"] = len(wrep.get("basics_problems") or [])
        rep["write"]["structure_problems"] = len(wrep.get("structure_problems") or [])
        rep["write"]["frame_checked"] = (wrep.get("frame_check") or {}).get(
            "segments")
        rep["write"]["frame_mismatch"] = len(
            (wrep.get("frame_check") or {}).get("mismatched") or [])
        ndir = Path(str(wrep.get("new_draft") or ""))
        if ndir.is_dir() and (ndir / "draft_content.json").exists():
            rep["write"]["draft_kb"] = round(
                (ndir / "draft_content.json").stat().st_size / 1024)

        # 读回真实时间线，确认落位与标记
        items = tl.GetItemListInTrack("video", 1) or []
        rep["facts"] = {
            "v1_items": len(items),
            "timeline_end": (tl.GetEndFrame() or 0) - base,
            "expected_end": srep.get("expected_end"),
            "markers": sum(len((it.GetMarkers() or {})) for it in items),
        }
        rep["ok"] = bool(srep.get("ok") and preq.get("ok") and wrep.get("ok")
                         and rep["facts"]["markers"] >= len(items))
        if not keep:
            compat_del(proj, tl)
    rep["ms"]["total"] = round(sum(v for k, v in rep["ms"].items()
                                   if k not in ("total", "prep")), 1)
    for stage, ms in rep["ms"].items():
        if stage not in ("total", "prep"):
            rep["per_seg_ms"][stage] = round(ms / n, 2)
    # 这次压力测试产出的 LUT 池也清掉：N 段就是 N 份 cube，
    # 攒着会占几十 MB（clean 命令也管这个池，这里直接删本轮的）。
    shutil.rmtree(paths.draft_lut_dir(d["id"]), ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return rep


def compat_del(proj, tl):
    from colorpass import compat
    try:
        compat.delete_timelines(proj.GetMediaPool(), [tl])
    except Exception:
        pass


# ---------------------------------------------------------------- 输出

def show_offline(rows):
    hdr = ("段数", "计划ms", "快照ms", "体检ms", "写回ms", "对账ms",
           "合计ms", "草稿KB", "写回KB", "问题")
    print("%6s %8s %8s %8s %8s %8s %8s %8s %8s  %s" % hdr)
    for r in rows:
        print("%6d %8.1f %8.1f %8.1f %8.1f %8.1f %8.1f %8.0f %8.0f  %s"
              % (r["n"], r["ms"].get("build_plan", 0),
                 r["ms"].get("snapshot_save", 0), r["ms"].get("audit", 0),
                 r["ms"].get("write_build", 0), r["ms"].get("basics_diff", 0),
                 r["ms"].get("total", 0),
                 (r["bytes"].get("draft") or 0) / 1024,
                 (r["bytes"].get("written") or 0) / 1024,
                 "ok" if not (r["facts"].get("basics_problems") or
                              r["facts"].get("structure_problems") or
                              r["facts"].get("write_error"))
                 else "BAD"))
    print()
    print("倍率（相对最小规模，>N 表示超线性）：")
    base = rows[0]
    for r in rows[1:]:
        k = r["n"] / base["n"]
        cells = []
        for stage in ("build_plan", "audit", "write_build"):
            a, b = base["ms"].get(stage, 0), r["ms"].get(stage, 0)
            cells.append("%s %.2fx / 段数 %.0fx" % (stage, (b / a) if a else 0, k))
        print("  %5d 段  %s" % (r["n"], " | ".join(cells)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", default=None,
                    help="逗号分隔的段数，例如 50,200,500,1000")
    ap.add_argument("--resolve", default=None,
                    help="真机：逗号分隔的段数，例如 60,180")
    ap.add_argument("--luts", default="0",
                    help="offline 模式给多少段挂 cube（0=不挂，seg=全挂，N=前 N 段）")
    ap.add_argument("--distinct", type=int, default=3,
                    help="resolve 模式用几个不同素材文件（默认 3；"
                         "调大可以单独压媒体池导入这一环）")
    ap.add_argument("--photos", type=int, default=0,
                    help="resolve 模式插入多少段图片（张张不同文件）")
    ap.add_argument("--overlay-every", type=int, default=0,
                    help="resolve 模式每隔几段在 V2 叠一段（画中画）")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", default=None, help="把结果写成 JSON")
    args = ap.parse_args()

    if not args.offline and not args.resolve:
        args.offline = "50,200,500,1000"

    alld = {}
    if args.offline:
        rows = []
        for n in [int(x) for x in args.offline.split(",") if x.strip()]:
            luts = n if args.luts == "seg" else int(args.luts or 0)
            try:
                r = run_offline(n, luts=luts, keep=args.keep)
            except Exception:
                traceback.print_exc()
                continue
            rows.append(r)
            print("· %d 段完成：计划 %d 段 / 跳过 %d / 合并 %d"
                  % (n, r["facts"].get("plan", 0), r["facts"].get("skipped", 0),
                     r["facts"].get("overlaps", 0)))
        if rows:
            show_offline(rows)
            alld["offline"] = rows

    if args.resolve:
        rows = []
        for n in [int(x) for x in args.resolve.split(",") if x.strip()]:
            print("\n=== 真机 %d 段（%d 个素材文件）===" % (n, min(args.distinct, n)))
            r = run_resolve(n, keep=args.keep, distinct=args.distinct,
                            photos=args.photos,
                            overlay_every=args.overlay_every)
            rows.append(r)
            print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        alld["resolve"] = rows

    if args.out:
        Path(args.out).write_text(json.dumps(alld, ensure_ascii=False, indent=1,
                                             default=str), encoding="utf-8")
        print("\n结果写入 %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
