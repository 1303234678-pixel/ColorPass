"""验一次「达芬奇里调的色，真的传回剪映了」。

为什么需要：不调色时导出的 cube 是恒等的（dev=0），只能证明「文件生成了」，
证明不了「调色被正确读出来了」。这里给第一段挂一个可验证的 CDL（把 R 压到
0.5、G 抬 0.1），再跑 pull，看导出的 cube 是否跟着变。

为什么不挂 LUT：`TimelineItem.SetLUT()` 在 21.2.0.6 上返回 False，节点数不变
（它要的是已注册的 LUT 名，不是文件路径）。`SetCDL` 是实测能生效的那个。

用法：
    python tools/lut_probe.py apply    # 挂 CDL（会改时间线！只在测试工程上用）
    colorpass pull                     # 取色
    python tools/lut_probe.py check    # 看导出的 cube 偏离恒等多少
    python tools/lut_probe.py clear    # 把 CDL 复位成中性
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from colorpass import pull, resolve  # noqa: E402

NEUTRAL = {"NodeIndex": "1", "Slope": "1 1 1", "Offset": "0 0 0",
           "Power": "1 1 1", "Saturation": "1"}
GRADED = {"NodeIndex": "1", "Slope": "0.5 1 1", "Offset": "0.1 0 0",
          "Power": "1 1 1", "Saturation": "1"}


def ctx():
    app = resolve.scriptapp("Resolve")
    proj = app.GetProjectManager().GetCurrentProject()
    for i in range(1, proj.GetTimelineCount() + 1):
        tl = proj.GetTimelineByIndex(i)
        if tl.GetName().startswith("CP"):
            return app, proj, tl
    raise SystemExit("找不到送入建的时间线（CP · …）")


def main(argv):
    mode = argv[0] if argv else "apply"
    app, proj, tl = ctx()
    items = tl.GetItemListInTrack("video", 1) or []
    if not items:
        raise SystemExit("时间线 V1 上没有片段")
    it = items[0]
    print("时间线 %s / V1 %d 个 / 目标 %r" % (tl.GetName(), len(items),
                                            it.GetName()))

    if mode in ("apply", "clear"):
        cdl = GRADED if mode == "apply" else NEUTRAL
        print("切调色页:", app.OpenPage("color"))
        i = 0
        while i < 40 and app.GetCurrentPage() != "color":
            import time
            time.sleep(0.25)
            i += 1
        print("当前页:", app.GetCurrentPage(), "| 节点数:", it.GetNumNodes())
        ok = it.SetCDL(cdl)
        print("SetCDL(%s) -> %s" % ("调过" if mode == "apply" else "中性", ok))
        if not ok:
            # 不带 NodeIndex 的写法在某些版本上返回 False，试一次裸参数
            print("  重试（去掉 NodeIndex）->", it.SetCDL(
                {k: v for k, v in cdl.items() if k != "NodeIndex"}))
        return 0

    # check：把这次 pull 导出的所有 cube 扫一遍，挑偏离恒等最大的那段
    import hashlib
    import json

    from colorpass import paths, snapshot
    ptr = snapshot.current()
    if not ptr:
        raise SystemExit("没有 current.json，先送入一次")
    run = Path(ptr["run_dir"])
    lut_dir = Path(paths.draft_lut_dir(ptr["draft_id"]))
    man = run / "manifest.json"
    if not man.exists():
        raise SystemExit("还没导出，先跑 colorpass pull")
    cubes = json.loads(man.read_text(encoding="utf-8"))["cubes"]
    print("这次导出 %d 份 cube，目录 %s" % (len(cubes), lut_dir))

    worst = None
    for row in cubes:
        p = Path(row["file"])
        if not p.exists():
            continue
        m = pull.measure_cube(p)
        if m is None:
            print("  %s 不完整" % p.name)
            continue
        if worst is None or m["dev_max"] > worst[1]["dev_max"]:
            worst = (row["segment_id"], m, p)
    if worst is None:
        raise SystemExit("没有可读的 cube")
    sid, m, p = worst
    print("偏离最大的一段 %s（%s）" % (sid, p.name))
    print("  dev_max %.2f / 白点 %s" % (m["dev_max"], m["white_rgb"]))
    print("  sha %s" % hashlib.sha1(p.read_bytes()).hexdigest()[:12])
    print("结论：%s" % ("读到调色了（dev>1 码）" if m["dev_max"] > 1
                       else "整条时间线都是恒等 LUT —— 没调色，或调色没读到"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
