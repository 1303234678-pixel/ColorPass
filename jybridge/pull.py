"""达芬奇 → 剪映：逐段 ExportLUT，按 marker 里的 segment_id 归位。

与上一版的差别（都是为了「cube 不丢」）：
  1. cube 落到 %LOCALAPPDATA%\\JYBridge\\luts\\<草稿ID>\\，不再是会话工作区
  2. 每个 cube 导出后立即校验：文件存在、33³、非空、对角线可读
  3. 写 manifest.json（段号→文件→字节数→白点），写回时按它对账
  4. 对齐校验不通过就不导（沿用：宁可不写，也不挂错段）
"""
import json
import os
import time
from pathlib import Path

from . import draftio, paths

MOD = r"C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer/Scripting/Modules"
import sys

sys.path.insert(0, MOD)
import DaVinciResolveScript as dvr  # noqa: E402

PAD = paths.PAD_SUFFIX
# 以前这里内置了几个「已知被调过色」的测试段 ID，用来把恒等校验放过去。
# 那是排错期的临时补丁：换一份草稿就全错位，而且会把真问题藏起来。
# 现在恒等偏差只作参考量（谁都别拿它当闸门），不再需要白名单。
GRADED_DEV = 1.0          # 对角偏离超过 1 码就认为这一段确实被调过色


def load_cube(path):
    size, data = None, []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        # BOM / 全角空格：真实 cube 里有厂商会写 BOM，不去掉的话第一行
        # 「\ufeffLUT_3D_SIZE 33」解析不出 size，整份 cube 被判成不完整。
        s = line.strip().lstrip("\ufeff").strip()
        if not s or s.startswith("#"):
            continue
        u = s.upper()
        if u.startswith("LUT_3D_SIZE"):
            size = int(s.split()[-1])
        elif u.startswith(("TITLE", "DOMAIN_")):
            continue
        else:
            try:
                data.append(tuple(float(x) for x in s.split()[:3]))
            except ValueError:
                pass
    return size, data


def at(data, n, r, g, b):
    return data[r + g * n + b * n * n]


def warm_timeline(proj, tries=15):
    tl, last = None, 0
    for _ in range(tries):
        tl = proj.GetCurrentTimeline()
        if tl:
            last = tl.GetTrackCount("video") or 0
            if last > 0 and (tl.GetItemListInTrack("video", 1) or []):
                return tl
        time.sleep(0.4)
    print("!! 预热失败，最后一次 V 轨数 =", last)
    return tl


def seg_marker(it):
    sid = None
    for _, v in (it.GetMarkers() or {}).items():
        c = (v or {}).get("customData")
        if c:
            sid = c
    return sid


def is_disabled(it):
    """这段在达芬奇里是不是被禁用了（对应剪映的「停用片段」）。

    老版本 Resolve 没有 GetClipEnabled：探测失败一律当启用，不能因为一句
    hasattr 没过去就把片段从取色里漏掉。
    """
    try:
        if not hasattr(it, "GetClipEnabled"):
            return False
        return it.GetClipEnabled() is False
    except Exception:
        return False


def verify_alignment(tl, plan, base):
    issues, placed = [], {}
    want = {(s["lane"], s["rec"]): s for s in plan["segments"]}
    for lane in range(1, (tl.GetTrackCount("video") or 0) + 1):
        for it in sorted((tl.GetItemListInTrack("video", lane) or []),
                         key=lambda x: x.GetStart()):
            sid = seg_marker(it)
            st = it.GetStart() - base
            dur = it.GetDuration()
            if not sid:
                issues.append({"lane": lane, "start": st,
                               "why": "片段上没有身份标记（可能是新插入的片段）"})
                continue
            if sid.endswith(PAD):
                main_id = sid[:-len(PAD)]
                target = next((k for k, v in placed.items()
                               if k[0] == lane and v["segment_id"] == main_id), None)
                if target is None:
                    issues.append({"lane": lane, "start": st, "segment_id": sid,
                                   "why": "尾巴段找不到对应主段"})
                else:
                    placed[target]["items"].append(
                        {"start": st, "dur": dur, "kind": "pad"})
                    placed[target]["total"] += dur
                continue
            s = want.get((lane, st))
            if s is None or s["segment_id"] != sid:
                issues.append({"lane": lane, "start": st, "segment_id": sid,
                               "why": "位置与计划不符（片段被移动/替换）"})
                continue
            ent = placed.setdefault((lane, st), {"segment_id": sid,
                                                 "items": [], "total": 0})
            ent["items"].append({"start": st, "dur": dur, "kind": "main"})
            ent["total"] += dur
    for (lane, rec), s in sorted(want.items()):
        got = placed.get((lane, rec))
        if got is None:
            issues.append({"lane": lane, "rec": rec, "segment_id": s["segment_id"],
                           "why": "计划中的片段不存在了（被删除或移位）"})
        elif got["total"] != s["dur"]:
            issues.append({"lane": lane, "rec": rec, "segment_id": s["segment_id"],
                           "want": s["dur"], "got": got["total"],
                           "why": "该段占用时长变了"})
    if plan.get("timeline") and tl.GetName() != plan["timeline"]:
        issues.append({"why": "当前时间线不是计划里的那条",
                       "want": plan["timeline"], "got": tl.GetName()})
    return issues, placed


def run(draft_id=None):
    """导出 LUT。

    上下文来自「最近一次完整送入」（current.json + snapshot），**不再读剪映
    的内存**：送入之后你在剪映里怎么改都不影响这一步，导出的永远是达芬奇里
    那条时间线（= 送入时的剪辑版本）的调色。
    """
    from . import snapshot
    rep = {"ok": False, "action": "pull"}
    ptr = snapshot.current()
    if ptr is None:
        rep["error"] = ("还没有完成过一次「送入达芬奇」。正常流程是："
                        "① 送入达芬奇 → 在达芬奇里调色 → ② 写回剪映。")
        return rep
    if ptr.get("_incomplete"):
        rep["error"] = ("最近一次「送入达芬奇」没有走完（%s 开始，时间线可能只摆了"
                        "一半），取回会得到不完整的 LUT 表。请重新送入；"
                        "要按上一次成功的版本取回，先执行 `jybridge reset`。"
                        % (ptr.get("pending_at") or "上一轮"))
        return rep
    did = ptr.get("draft_id")
    run_dir = ptr.get("run_dir") or paths.draft_run_dir(did)
    lut_dir = Path(paths.draft_lut_dir(did))
    rep["draft"] = ptr.get("draft_name")
    rep["draft_id"] = did
    rep["snapshot_at"] = ptr.get("snapshot_at")
    rep["sent_timeline"] = ptr.get("timeline")

    plan_path = os.path.join(run_dir, "plan.json")
    if not os.path.exists(plan_path):
        rep["error"] = "缺少 plan.json，先执行「送入达芬奇」"
        draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
        return rep
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    # 记下这份 pull 对的是哪一版计划。写回默认会自动重新取回，但在 -no-pull
    # 排错模式下靠它判断「这份 pull.json 是不是上一版留下的」。
    import hashlib
    rep["plan_sha"] = hashlib.sha1(
        Path(plan_path).read_bytes()).hexdigest()[:16]
    if str(plan.get("draft_id") or "") != str(did):
        rep["error"] = ("plan.json 属于另一份草稿（%s），当前是 %s。"
                        "请先重新送入。" % (plan.get("draft_id"), did))
        draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
        return rep

    resolve = dvr.scriptapp("Resolve")
    page = None
    try:
        page = resolve.GetCurrentPage()
    except Exception:
        pass
    if not page:
        rep["error"] = ("达芬奇停在项目管理器，还没进入工程。请在达芬奇里打开"
                        "送入时用的那个工程（里面有时间线「%s」），再取回。"
                        % (ptr.get("timeline") or "?"))
        draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
        return rep
    proj = resolve.GetProjectManager().GetCurrentProject()
    tl = warm_timeline(proj)
    if tl is None:
        # 写回会自动走这里：用户可能还没送入，或者达芬奇停在一个空工程上。
        rep["error"] = ("达芬奇里没有当前时间线。请先点「① 送入达芬奇」把剪映"
                        "的剪辑送进去，再到面板里写回。")
        draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
        return rep
    rep["timeline"] = tl.GetName() if tl else None
    want_tl = ptr.get("timeline")
    if want_tl and rep["timeline"] != want_tl:
        rep["error"] = ("达芬奇当前时间线是「%s」，不是最近一次送入的「%s」。"
                        "LUT 只对得上那条，请切回去（或重新送入）。"
                        % (rep["timeline"], want_tl))
        draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
        return rep
    rep["color_science"] = proj.GetSetting("colorScience")
    rep["timeline_space"] = proj.GetSetting("colorSpaceTimeline")
    rep["timeline_gamma"] = proj.GetSetting("timelineGamma")
    base = int(plan.get("start_frame") or 0)

    issues, placed = verify_alignment(tl, plan, base)
    rep["alignment_ok"] = not issues
    rep["issues"] = issues
    rep["aligned_segments"] = len(placed)
    if issues:
        rep["error"] = "时间线与计划不一致，已中止，未导出任何 cube"
        draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
        return rep

    try:
        resolve.OpenPage("color")
        time.sleep(0.8)
    except Exception:
        pass

    rows, seg_luts, problems, skipped_disabled = [], {}, [], []
    for (lane, rec), s in sorted(placed.items()):
        if not [x for x in s["items"] if x["kind"] == "main"]:
            problems.append({"lane": lane, "rec": rec, "why": "该段没有主片段"})
            continue
        it = None
        for cand in (tl.GetItemListInTrack("video", lane) or []):
            if cand.GetStart() - base == rec:
                it = cand
                break
        if it is None:
            problems.append({"lane": lane, "rec": rec, "why": "取不到片段对象"})
            continue
        sid = s["segment_id"]
        # 剪映里停用的段，在达芬奇里也是禁用片段：用户看不见它，就不会去调它。
        # 给它导一版 LUT 再写回，等于往停用段里塞一个恒等 cube —— 一旦用户
        # 在剪映里取消停用，画面会莫名其妙多一层处理。所以直接跳过。
        try:
            if is_disabled(it):
                skipped_disabled.append({"lane": lane, "rec": rec,
                                         "segment_id": sid,
                                         "name": it.GetName()})
                continue
        except Exception:
            pass
        dst = lut_dir / ("%s.cube" % sid)
        ok = it.ExportLUT(resolve.EXPORT_LUT_33PTCUBE, str(dst))
        if not ok or not dst.exists() or dst.stat().st_size == 0:
            time.sleep(0.3)
            ok = it.ExportLUT(resolve.EXPORT_LUT_33PTCUBE, str(dst))
        if not ok or not dst.exists() or dst.stat().st_size == 0:
            problems.append({"lane": lane, "rec": rec, "segment_id": sid,
                             "why": "ExportLUT 失败或产出空文件"})
            continue
        size, data = load_cube(dst)
        if size != 33 or len(data) != 33 ** 3:
            problems.append({"segment_id": sid,
                             "why": "cube 不完整 size=%s n=%d" % (size, len(data))})
            continue
        devs = []
        for v in (0, 16, 128, 235, 255):
            gi = int(round(v / 255.0 * 32))
            o = at(data, 33, gi, gi, gi)
            exp = gi / 32.0
            devs += [abs(o[0] - exp) * 255, abs(o[1] - exp) * 255,
                     abs(o[2] - exp) * 255]
        corner = at(data, 33, 32, 32, 32)
        dev_max = round(max(devs), 2)
        rows.append({
            "track": lane, "start": rec, "name": it.GetName(),
            "segment_id": sid, "cube": str(dst),
            "bytes": dst.stat().st_size,
            # 「调过色」由导出结果自己说话：偏离恒等就是调过。
            "graded": dev_max > GRADED_DEV,
            "identity_dev_max": dev_max,
            "white_rgb": [round(x * 255, 1) for x in corner],
        })
        seg_luts[sid] = str(dst)

    worst = max((r["identity_dev_max"] for r in rows), default=0.0)
    graded_n = len([r for r in rows if r["graded"]])
    rep.update({
        "count": len(rows),
        "items": rows,
        "problems": problems,
        "identity_dev_max": round(worst, 2),
        # identity_* 全是参考量，不是闸门：
        #   identity_dev_max 全片最大的对角偏离（越大说明调得越狠）
        #   identity_ok      整条时间线都没动过色（worst ≤ 1 码）
        #   identity_graded  确实被调过色的段数
        # 决定能不能写回的是对齐校验（alignment_ok）：轨道/起点/身份标记都对得上，
        # LUT 就不可能挂错段。拿「有没有调色」当失败条件，等于让工具拒绝干活。
        "identity_ok": worst <= GRADED_DEV,
        "identity_graded": graded_n,
        "lut_dir": str(lut_dir),
        "segment_luts": seg_luts,
        "disabled_skipped": skipped_disabled,
    })
    draftio.dump_json({
        "draft_id": did, "draft_name": ptr.get("draft_name"),
        "timeline": tl.GetName(), "lut_dir": str(lut_dir),
        "count": len(rows),
        "cubes": [{"segment_id": r["segment_id"], "file": r["cube"],
                   "bytes": r["bytes"], "white_rgb": r["white_rgb"]}
                  for r in rows],
    }, os.path.join(run_dir, "manifest.json"))
    rep["ok"] = bool(not problems and (rows or skipped_disabled))
    draftio.dump_json(rep, os.path.join(run_dir, "pull.json"))
    return rep
