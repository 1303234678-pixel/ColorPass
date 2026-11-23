"""回剪映的基础信息核对：帧数、画布、素材像素尺寸到底变没变。

用法：
    python tools/verify_write_basics.py                 # 最近一次带快照的送入
    python tools/verify_write_basics.py --run 738B06E6… # 指定 run 目录/草稿 ID
    python tools/verify_write_basics.py --root real --keep   # 真写进剪映草稿目录

它走的是**同一条代码路径**（`draftbuild.build_from_draft` → `install_from_template`），
默认把新草稿写进临时目录，比完就删 —— 不碰剪映的工程列表。要让剪映里真的
多出一份草稿，用 `--root real --keep`，脚本不会替你删。

三处对账：
    剪映原稿（送入时的快照） → 达芬奇时间线（plan.json 的帧号） → 写回稿
只要有一处对不上，退出码就是 1。
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import draftbuild, paths, snapshot, write  # noqa: E402

RUNS = Path(paths.data_root()) / "runs"


def _load_run(run_id=None):
    if run_id:
        d = Path(run_id)
        if not d.is_dir():
            d = RUNS / str(run_id)
        if not d.is_dir():
            raise SystemExit("找不到 run 目录：%s" % run_id)
        return d
    cands = [p for p in RUNS.iterdir() if p.is_dir()] if RUNS.is_dir() else []
    cands = [p for p in cands if any(p.glob("snap-*/draft_content.json"))]
    if not cands:
        raise SystemExit("没有找到任何带快照的 run（先做一次「送入达芬奇」）")
    return max(cands, key=lambda p: p.stat().st_mtime)


def _snap_dir(run_dir):
    p = run_dir / "snapshot_dir"
    if p.exists():
        s = p.read_text(encoding="utf-8").strip()
        if s and Path(s).is_dir():
            return Path(s)
    snaps = sorted(run_dir.glob("snap-*"))
    if not snaps:
        raise SystemExit("run 目录里没有快照：%s" % run_dir)
    return snaps[-1]


def _free_name(root, name):
    cand, n = name, 1
    while (root / cand).exists():
        n += 1
        cand = "%s_%d" % (name, n)
    return cand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None)
    ap.add_argument("--root", default="temp", choices=("temp", "real"))
    ap.add_argument("--keep", action="store_true", help="留下新草稿（real 模式必带）")
    ap.add_argument("--name", default="CP 帧校验")
    args = ap.parse_args()

    run_dir = _load_run(args.run)
    snap = _snap_dir(run_dir)
    src, meta = snapshot.load(str(snap))
    pull_p = run_dir / "pull.json"
    pull = json.loads(pull_p.read_text(encoding="utf-8")) if pull_p.exists() else {}
    seg_luts = pull.get("segment_luts") or {}
    plan_p = run_dir / "plan.json"
    plan = json.loads(plan_p.read_text(encoding="utf-8")) if plan_p.exists() else {}
    plan_frames = {str(r["segment_id"]): (r.get("rec"), r.get("dur"))
                   for r in plan.get("segments") or [] if r.get("segment_id")}

    tmp = Path(tempfile.mkdtemp(prefix="cpbasics_"))
    root = tmp if args.root == "temp" else Path(draftbuild.JY.draft_root())
    if args.root == "real" and not args.keep:
        raise SystemExit("--root real 会真的在剪映里建一份草稿，请加 --keep "
                         "（脚本不会替你删剪映目录里的东西）")
    name = _free_name(root, args.name)
    print("旧稿  %s（%s）" % (meta.get("draft_name") or pull.get("timeline"),
                             src.get("id")))
    print("run   %s" % run_dir)
    print("写回  %s" % (str(root / name)))
    print("LUT   %d 份（%s）" % (len(seg_luts), pull.get("lut_type_used") or "-"))
    print()

    try:
        r = draftbuild.build_from_draft(src, name, {}, segment_luts=seg_luts,
                                        root=root)
        if not r.get("ok"):
            print("生成新草稿失败：%s" % r.get("error"))
            return 2
        new_dir = Path(r.get("folder") or root / name)
        nd = json.loads((new_dir / "draft_content.json").read_text(encoding="utf-8"))

        a, b = write.basics_index(src), write.basics_index(nd)
        probs = write.basics_diff(src, nd)
        a["segments_checked"] = sum(len(v["segments"]) for v in a["lanes"].values())
        b["segments_checked"] = sum(len(v["segments"]) for v in b["lanes"].values())

        print("基础信息")
        print("  帧率      %s 帧/秒（剪映草稿以微秒存时间，按这个换帧）" % b["fps"])
        print("  画布      %s → %s" % (a["canvas"], b["canvas"]))
        print("  总长      %.3f s → %.3f s" % (a["duration_us"] / 1e6,
                                              b["duration_us"] / 1e6))
        print("  总帧数    %d → %d" % (a["duration"], b["duration"]))
        print("  段数      %d → %d" % (a["segments_checked"], b["segments_checked"]))
        print("  素材      %d 条（视频/图片/音频）" % len(a["materials"]))
        print()

        print("逐段帧号（剪映原稿 → 达芬奇 → 写回稿）")
        print("  %-5s %-9s %5s %5s %5s %9s %-13s %-10s %s"
              % ("轨", "段", "开始", "时长", "结束", "微秒",
                 "素材入/出(帧)", "像素", "达芬奇"))
        bad = []
        for lane, e in sorted(a["lanes"].items(), key=lambda kv: int(kv[0])):
            for sid, sg in e["segments"].items():
                want = plan_frames.get(sid)
                mat = a["materials"].get(sg["material"]) or {}
                nb = b["lanes"].get(lane, {}).get("segments", {}).get(sid)
                same = nb is not None and (nb["start"], nb["dur"]) == \
                    (sg["start"], sg["dur"])
                if not same:
                    bad.append({"segment_id": sid, "src": (sg["start"], sg["dur"]),
                                "new": None if nb is None else
                                (nb["start"], nb["dur"])})
                src_rng = "-" if sg["src_us"] is None else "%d/%d" % (
                    write.frames_of(sg["src_us"]),
                    write.frames_of((sg["src_us"] or 0) + (sg["src_dur_us"] or 0)))
                if mat and mat.get("w") and mat.get("h"):
                    px = "%sx%s" % (mat["w"], mat["h"])
                elif mat and mat.get("kind") == "audios":
                    px = "（音频）"
                else:
                    px = "-"
                print("  %-5s %-9s %5d %5d %5d %9d %-13s %-10s %s%s"
                      % (e["type"][:5], sid[:8], sg["start"], sg["dur"],
                         sg["end"], sg["dur_us"], src_rng, px,
                         ("%s/%s" % want) if want else "（未送入）",
                         "" if same else "  ← 写回稿对不上"))
        print()

        landed = [k for k in plan_frames
                  if any(k in v["segments"] for v in b["lanes"].values())]
        moved = []
        for lane in b["lanes"].values():
            for sid, sg in lane["segments"].items():
                w = plan_frames.get(sid)
                if w and tuple(w) != (sg["start"], sg["dur"]):
                    moved.append({"segment_id": sid, "davinci": list(w),
                                  "jiemying": [sg["start"], sg["dur"]]})
        print("达芬奇时间线：plan 里 %d 段，从第 %s 帧开始"
              % (len(plan_frames), plan.get("start_frame", 0)))
        print("对账：快照 %d 段 ↔ 写回稿 %d 段；plan 命中 %d 段，错位 %d 段"
              % (a["segments_checked"], b["segments_checked"],
                 len(landed), len(moved)))
        if bad:
            print("！同一段的帧号在写回稿里变了：%s" % bad[:3])
        if moved:
            print("！达芬奇摆的帧号与写回稿不一致：%s" % moved[:3])
        if probs:
            print("！基础信息有 %d 处不一致：" % len(probs))
            for p in probs[:10]:
                print("   %s  %s → %s" % (p.get("why"), p.get("src"), p.get("new")))
        print()
        ok = not probs and not bad and not moved
        print("结论：%s" % ("帧数与画面信息完全一致" if ok else "有不一致，见上"))
        if args.keep:
            print("新草稿留在：%s" % new_dir)
        return 0 if ok else 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
