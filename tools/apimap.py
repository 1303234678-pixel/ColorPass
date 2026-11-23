"""导出这台机器上达芬奇脚本 API 的真实全貌。

为什么要有这个：官方文档的接口表跟实际绑定对不上（`EXPORT_LUT_33PTCUBE`
取不到、`hasattr` 对不存在的方法也返回 True）。要做「这个版本上更好的写法」，
只能问这台机器本身：`dir()` 一遍，看有哪些方法真的能调。

用法：
    python tools/apimap.py            # 打印清单
    python tools/apimap.py --docs     # 连带每个方法自己的 docstring
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from colorpass import resolve  # noqa: E402


def surface(obj):
    """能调的方法名（含继承来的 base class 方法）。"""
    out = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            v = getattr(obj, name)
        except Exception:
            continue
        if callable(v):
            out[name] = v
    return out


def main(argv):
    docs = "--docs" in argv
    app = resolve.scriptapp("Resolve")
    if app is None:
        print("连不上达芬奇")
        return 1
    pm = app.GetProjectManager()
    proj = pm.GetCurrentProject() if pm else None
    mp = proj.GetMediaPool() if proj else None
    root = mp.GetRootFolder() if mp else None
    clips = root.GetClipList() if root else None
    tl = proj.GetCurrentTimeline() if proj else None
    items = tl.GetItemListInTrack("video", 1) if tl else None
    item = items[0] if items else None
    mi = item.GetMediaPoolItem() if item else (clips[0] if clips else None)

    objs = [("Resolve", app), ("ProjectManager", pm), ("Project", proj),
            ("MediaPool", mp), ("Folder", root), ("Timeline", tl),
            ("TimelineItem", item), ("MediaPoolItem", mi)]
    for label, obj in objs:
        print("=" * 60)
        if obj is None:
            print("%s: <拿不到>" % label)
            continue
        print("%s  (%s)" % (label, type(obj).__name__))
        for name, fn in sorted(surface(obj).items()):
            doc = (getattr(fn, "__doc__", "") or "").strip().replace("\n", " ")
            if docs and doc:
                print("  %-32s %s" % (name, doc[:160]))
            else:
                print("  " + name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
