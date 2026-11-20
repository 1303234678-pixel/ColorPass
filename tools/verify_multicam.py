"""真机验证：多机位 / 合并片段的取回链路。

它做什么
--------
在一个**新建的临时工程**里：

1. 导入两段测试素材，摆一条 3 段的时间线，每段写段号 marker；
2. 用 `Timeline.CreateCompoundClip` 把 3 段合成 1 个片段 —— 在时间线上，
   它与多机位片段是同一种形态（一个 item 盖住原来几段，marker 随旧 item 消失）；
3. 跑真的 `pull.verify_alignment` 与 `pull.run`，确认能靠「完全覆盖」认出来、
   只导出一次 LUT、其余段复制同一份；
4. 若这版有 `MediaPool.CreateMulticamClip`（21.1+），再建一个真的多机位，
   验证识别、标记、ExportLUT 三项能力；没有就如实记为 skipped。

为什么不用 CreateMulticamClip 造覆盖片段：21.1 之前的版本没有这个接口
（本机 20.3 就没有），而「覆盖」是写回真正要处理的时间线形态。复合片段在
18.0 起就有，能在所有目标版本上复现同一形态。

不动用户已有工程：全程在 `ColorPass MC Test <时间>` 这个新工程里，测完保留，
方便进达芬奇肉眼看。删不删由你。

用法
----
    python tools/verify_multicam.py --media a.mp4 b.mp4
    python tools/verify_multicam.py --media a.mp4 b.mp4 --data D:\\cp-test

`--data` 指定一个隔离的数据目录（COLORPASS_DATA），不碰你真实的
current.json / runs / luts。
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FPS = 30
SEG = 120                      # 每段 4 秒
PLAN = [("SEG-00", 0), ("SEG-01", 120), ("SEG-02", 240)]


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--media", nargs="+", required=True,
                   help="至少两段视频；多机位测试会用前两段")
    p.add_argument("--data", default="",
                   help="隔离的数据目录（默认临时目录）；不碰真实的 ColorPass 数据")
    p.add_argument("--wait", type=int, default=90,
                   help="等达芬奇脚本接口就绪的秒数（默认 90）")
    return p.parse_args()


def _connect(timeout):
    from colorpass import resolve
    dvr = resolve.lazy()
    last = ""
    for _ in range(max(1, timeout // 2)):
        try:
            app = dvr.scriptapp("Resolve")
        except Exception as exc:
            app, last = None, str(exc)[:120]
        if app:
            return app
        time.sleep(2)
    raise SystemExit("连不上达芬奇脚本接口（等了 %d 秒）。%s\n"
                     "确认达芬奇已打开、偏好设置里允许外部脚本。"
                     % (timeout, last))


def _append(mp, mi, src, n, rec, lane=1):
    return mp.AppendToTimeline([{
        "mediaPoolItem": mi, "startFrame": src, "endFrame": src + n,
        "recordFrame": rec, "trackIndex": lane}]) or []


def _hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main():
    a = _args()
    media = [str(Path(x).resolve()) for x in a.media]
    for m in media:
        if not os.path.isfile(m):
            raise SystemExit("素材不存在：%s" % m)
    data = a.data or tempfile.mkdtemp(prefix="cp_mc_real_")
    os.environ["COLORPASS_DATA"] = data

    # 必须在设置 COLORPASS_DATA 之后导入：paths.BASE 是模块级常量。
    from colorpass import compat, pull, send, snapshot

    rep = {"ok": False, "data": data, "media": media,
           "version": "", "product": "", "steps": {}}

    app = _connect(a.wait)
    rep["version"] = compat.version_text(app)
    rep["product"] = compat.product_text(app)
    pm = app.GetProjectManager()
    proj = pm.GetCurrentProject()
    if getattr(proj, "GetName", None) and proj.GetName():
        rep["previous_project"] = proj.GetName()

    name = "ColorPass MC Test " + time.strftime("%m%d-%H%M%S")
    if not pm.CreateProject(name):
        raise SystemExit("建不了工程「%s」（可能同名）。" % name)
    proj = pm.GetCurrentProject()
    for key, val in (("timelineFrameRate", str(FPS)),
                     ("timelineResolutionWidth", "1920"),
                     ("timelineResolutionHeight", "1080")):
        try:
            proj.SetSetting(key, val)
        except Exception:
            pass
    mp = proj.GetMediaPool()
    clips, mode = compat.import_media(mp, media)
    if len(clips) < 2:
        raise SystemExit("导入素材失败：%s" % media)
    rep["steps"]["import"] = {"count": len(clips), "mode": mode}

    tl = mp.CreateEmptyTimeline("MC Cover")
    if not tl:
        raise SystemExit("建不了空时间线")
    proj.SetCurrentTimeline(tl)
    base = 0
    try:
        if tl.SetStartTimecode("00:00:00:00"):
            time.sleep(0.5)
            base = int(tl.GetStartFrame() or 0)
    except Exception:
        pass

    # 摆 3 段：用前两段素材交叉，保证是不同的画面
    placed = []
    for i, (_sid, rec) in enumerate(PLAN):
        got = _append(mp, clips[i % len(clips)], 0, SEG, rec + base)
        if not got:
            raise SystemExit("第 %d 段摆不上时间线" % i)
        placed.append(got[0])
    rep["steps"]["place"] = {"count": len(placed), "base": base,
                             "starts": [it.GetStart() - base for it in placed]}

    inked = [send.mark_item(it, sid) for it, (sid, _r) in zip(placed, PLAN)]
    rep["steps"]["mark"] = {"ok": sum(1 for x in inked if x), "of": len(inked)}

    verify = pull.verify_alignment(tl, _plan(tl.GetName(), base), base)
    rep["steps"]["before_merge"] = {"issues": verify[0], "matched": len(verify[1])}

    # 合成 1 个片段盖住 3 段 —— 多机位在时间线上的形态
    cc = tl.CreateCompoundClip(placed, {"name": "MC 覆盖测试"})
    if cc is None:
        raise SystemExit("CreateCompoundClip 返回空（这版可能不支持）")
    time.sleep(0.4)
    rep["steps"]["compound"] = {
        "start": cc.GetStart() - base, "dur": cc.GetDuration(),
        "name": cc.GetName(),
        "covered": cc.GetDuration() == SEG * len(PLAN),
    }

    plan = _plan(tl.GetName(), base)
    issues, matched = pull.verify_alignment(tl, plan, base)
    groups = pull.coverage_groups(matched)
    rep["steps"]["after_merge"] = {
        "ok": not issues, "issues": issues,
        "aligned": len(matched),
        "coverage": groups,
        "multicam_recognised": bool(groups and groups[0]["multicam"]),
    }

    # 写 plan.json + current.json，跑真的 pull
    run_dir = os.path.join(data, "runs", "MCTEST")
    os.makedirs(run_dir, exist_ok=True)
    Path(run_dir, "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    snapshot.CURRENT_PATH = os.path.join(data, "current.json")
    Path(snapshot.CURRENT_PATH).write_text(json.dumps({
        "run_dir": run_dir, "draft_id": "MCTEST", "draft_name": "多机位测试",
        "timeline": tl.GetName(), "start_frame": base, "segments": 3,
        "committed": True, "snapshot_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_dir": "",
    }, ensure_ascii=False), encoding="utf-8")

    os.environ["COLORPASS_DATA"] = data
    pull_rep = pull.run()
    rows = pull_rep.get("items") or []
    rep["steps"]["pull"] = {
        "ok": pull_rep.get("ok"), "error": pull_rep.get("error"),
        "count": pull_rep.get("count"),
        "coverage": pull_rep.get("coverage"),
        "coverage_segments": pull_rep.get("coverage_segments"),
        "shared_rows": len([r for r in rows if r.get("shared_with")]),
        "cube_hashes": [_hash(r["cube"]) for r in rows if r.get("cube")],
        "problems": pull_rep.get("problems"),
    }

    # 21.1+ 才能脚本建多机位：单独验证识别 / 标记 / 导出
    mc = {"supported": callable(getattr(mp, "CreateMulticamClip", None))}
    if mc["supported"]:
        try:
            mc_clips = mp.CreateMulticamClip(clips[:2], {"name": "MC API 测试"})
            mc["created"] = len(mc_clips or [])
            if mc_clips:
                mci = mc_clips[0]
                mc["media_type"] = (mci.GetClipProperty("Type") or "")
                tl2 = mp.CreateTimelineFromClips("MC API", [mci])
                mc["timeline"] = bool(tl2)
                if tl2:
                    proj.SetCurrentTimeline(tl2)
                    items = tl2.GetItemListInTrack("video", 1) or []
                    mc["items"] = len(items)
                    if items:
                        it = items[0]
                        mc["kind"] = compat.item_kind(it)
                        mc["mark"] = bool(send.mark_item(it, "MC-00"))
                        dst = os.path.join(data, "mc-api.cube")
                        mc["export_lut"] = bool(
                            it.ExportLUT(compat.lut_type(app=app), dst))
                        mc["cube_ok"] = bool(
                            mc["export_lut"] and pull.measure_cube(dst))
                    proj.SetCurrentTimeline(tl)
        except Exception as exc:
            mc["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    else:
        mc["note"] = "这版没有 CreateMulticamClip（21.1 起才有）；" \
                     "覆盖形态已用复合片段等价验证"
    rep["steps"]["multicam"] = mc

    s = rep["steps"]
    rep["ok"] = bool(
        s["place"]["starts"] == [r for _s, r in PLAN]
        and s["mark"]["ok"] == len(PLAN)
        and s["after_merge"]["ok"]
        and s["pull"].get("ok")
        and s["pull"].get("shared_rows") == len(PLAN) - 1
        and len(set(s["pull"].get("cube_hashes") or [])) == 1
    )
    rep["project"] = name
    rep["timeline"] = tl.GetName()
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    print("\n工程「%s」已建好，时间线「%s」留着给你看；数据目录 %s"
          % (name, tl.GetName(), data), file=sys.stderr)
    return 0 if rep["ok"] else 1


def _plan(timeline, base):
    return {
        "timeline": timeline, "fps": FPS, "start_frame": base,
        "draft_id": "MCTEST", "draft_name": "多机位测试",
        "segments": [{"segment_id": sid, "lane": 1, "kind": "video",
                      "rec": rec, "dur": SEG, "want": SEG, "speed": 1.0,
                      "items": []} for sid, rec in PLAN],
    }


if __name__ == "__main__":
    sys.exit(main())
