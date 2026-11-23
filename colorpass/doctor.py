"""colorpass doctor —— 一条命令说清「这台机器能跑到哪一步」。

为什么要有它
------------
兼容性靠一台机器是试不出来的：Windows / macOS、达芬奇 17~20、剪映 10~11，
组合太多，开发者手里也不可能有每一台。所以换个办法 —— **让代码自己报病历**：
用户跑一次 `colorpass doctor`，把这段 JSON 贴进 issue，就能知道那个版本上
每个 API 在不在、路径对不对、卡在哪一步。

输出里 `resolve.api` 是每个对象身上「哪些方法真的能调」的表面清单，
`compat` 才是**结论**（哪一项缺了会影响什么、算不算致命）。结论只认 compat：
接口表是给人翻的，`compat` 是按后果分级的。

这个命令**不修改任何东西**：不写草稿、不动时间线、不改配置，只读和试调。
没装达芬奇、没开达芬奇、没装剪映，都能跑，缺什么就报什么。
"""
import json
import os
import platform
import sys
import tempfile

from . import __version__, compat, install, memscan, paths, probe, resolve, retime


def _try(fn, default=None):
    """取一个值，失败就返回默认值 —— doctor 不能因为某一项挂了就什么都不输出。"""
    try:
        return fn()
    except Exception:
        return default


def _platform():
    return {
        "sys": sys.platform,
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _data_dir():
    """数据目录能不能写 —— 权限问题在 mac 上比 Windows 常见。"""
    out = {"base": paths.BASE, "writable": False}
    try:
        os.makedirs(paths.BASE, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=paths.BASE, delete=True):
            out["writable"] = True
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def _collect_api(objs):
    """{组名: {方法: bool}}，外加一份合并后的 missing 清单。"""
    groups, missing = {}, []
    for group, obj in objs.items():
        surf = resolve.surface(obj, group)
        if surf is None:                      # 对象拿不到（比如没有时间线）
            continue
        groups[group] = surf
        missing += ["%s.%s" % (group, name) for name, ok in surf.items() if not ok]
    return groups, missing


def _blank_api():
    """没验到的时候也要把键写全。

    报告结构随状态变是最容易埋雷的一种小问题：命令自己用 `.get()` 容错，
    谁按固定结构读 `resolve.api.missing` 就会 KeyError —— 而这只会发生在
    「达芬奇没开」的时候，平时跑测试根本碰不到。值用 None 表示「没验到」，
    不是「全都不支持」。
    """
    out = {group: None for group in resolve.API_GROUPS}
    out["missing"] = []
    out["verified"] = False
    return out


def _resolve_part(app=None):
    out = {"module": _try(resolve.module_path), "searched": _try(resolve.existing_dirs, []),
           "loaded": False, "app": False, "timeline": None, "api": _blank_api()}
    try:
        resolve.load()
        out["loaded"] = True
    except resolve.ResolveUnavailable as exc:
        out["error"] = str(exc).strip().splitlines()[0]
        return out

    app = app if app is not None else _try(lambda: resolve.scriptapp("Resolve"))
    if app is None:
        out["error"] = ("脚本模块在，但拿不到 Resolve 对象：达芬奇没开，"
                        "或装的是不支持脚本的版本。打开达芬奇后重跑一次。")
        return out
    out["app"] = True
    out["version"] = _try(app.GetVersionString)
    out["product"] = _try(app.GetProductName)
    out["page"] = _try(app.GetCurrentPage)      # 18 起才有，17 会是 None

    pm = _try(lambda: app.GetProjectManager())
    proj = _try(lambda: pm.GetCurrentProject()) if pm else None
    out["project"] = _try(proj.GetName) if proj else None

    tl = _try(lambda: proj.GetCurrentTimeline()) if proj else None
    # 一律用 lambda 取：缺方法的对象上 `obj.GetX` 这**一次属性访问**就抛
    # AttributeError，写在 _try(fn) 里已经晚了一步（那时参数已经求值）。
    mp = _try(lambda: proj.GetMediaPool()) if proj else None
    root = _try(lambda: mp.GetRootFolder()) if mp else None
    item = None
    media_item = None
    if tl:
        n = _try(lambda: tl.GetTrackCount("video"), 0) or 0
        items = _try(lambda: tl.GetItemListInTrack("video", 1), []) or []
        item = items[0] if items else None
        out["timeline"] = {
            "name": _try(tl.GetName),
            "video_tracks": n,
            "items_v1": len(items),
            "fps": _try(lambda: tl.GetSetting("timelineFrameRate")),
        }
    else:
        out["timeline"] = None
    if item is not None:
        media_item = _try(lambda: item.GetMediaPoolItem())
    if media_item is None:
        clips = (_try(lambda: root.GetClipList(), []) or []) if root else []
        media_item = clips[0] if clips else None

    groups, missing = _collect_api({"app": app, "project_manager": pm,
                                    "project": proj, "mediapool": mp,
                                    "folder": root, "timeline": tl,
                                    "item": item, "mediaitem": media_item})
    out["api"] = groups
    out["api"]["missing"] = missing
    out["api"]["verified"] = True
    return out


def _jianying_part():
    ver, path = (None, None)
    try:
        ver, path = probe.install_dir()
    except Exception:
        pass
    out = {
        "running": _try(probe.is_running, False),
        "installed_version": ver,
        "userdata": str(_try(probe.user_data_dir, "")),
        "draft_root": str(_try(probe.draft_root, "")),
        "draft_root_exists": _try(lambda: probe.draft_root().is_dir(), False),
    }
    snap = _try(probe.snapshot)
    if snap:
        out["draft"] = (snap.get("draft") or {}).get("name")
        out["draft_encrypted"] = snap.get("draft_encrypted")
        out["error"] = snap.get("draft_error") or ""
    return out


def _capabilities():
    """能不能用某条可选能力。缺了不是错误，是这台机器的条件。"""
    ff = _try(retime.info, {}) or {}
    return {
        "memscan": bool(getattr(memscan, "AVAILABLE", False)),
        # ffmpeg 从 0.4.2 起随包自带，正常必为 True。false 只有一种含义：
        # 文件被删了（杀毒软件/手工清理），修法是重装 —— 所以把路径和原因
        # 一起带上，用户报问题时不用再猜。
        "ffmpeg": bool(ff.get("ok")),
        "ffmpeg_exe": ff.get("exe") or "",
        "ffmpeg_why": ff.get("why") or "",
    }


def _panel_part():
    files = [os.path.join(d, install.PANEL_NAME) for d in install.TARGETS]
    installed = [p for p in files if os.path.isfile(p)]
    conf = {}
    try:
        with open(install.CONF_PATH, encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        pass
    return {"targets": list(install.TARGETS), "installed": installed, "config": conf}


def _cache_part():
    """缓存占了多少、都是什么、能收多少。

    用户会问「长期用会不会越来越占地方」，所以这三个数要能一眼查到，而不是
    让人翻 `%LOCALAPPDATA%` 自己数。retime 里是变速段的中转素材（ProRes，
    1080p 约 18~22 MB/s），也是唯一会长到 GB 级的池。
    """
    from . import gc
    sc = _try(gc.scan, {}) or {}
    rec = _try(gc.reclaimable, {}) or {}
    pools = {}
    for k, v in (sc.get("pools") or {}).items():
        if v.get("count"):
            pools[k] = {"count": v["count"], "bytes": v.get("bytes"),
                        "human": gc.human(v.get("bytes") or 0)}
    total = sc.get("bytes") or 0
    return {"base": sc.get("base") or "",
            "bytes": total, "human": gc.human(total),
            "pools": pools,
            "free_bytes": rec.get("free_bytes") or 0,
            "free_human": gc.human(rec.get("free_bytes") or 0),
            "note": ("变速段中转素材被达芬奇时间线直接引用：清掉它，那条旧时间线"
                     "会显示媒体离线（重新送入即可再生成）")}


def run(app=None):
    """收集一份诊断报告（纯 dict，可直接 json.dumps）。"""
    # compat 必须拿到**同一个** app：不然它自己再去连一次，测试/离线场景下
    # 连不上就全记成 unknown，结论会莫名其妙变成「不 ok」。
    compat_rep = _try(lambda: compat.report(app), {}) or {}
    rep = {
        "colorpass": __version__,
        "platform": _platform(),
        "data": _data_dir(),
        "resolve": _resolve_part(app),
        # 同一份能力清单，换成「缺了会怎样」的说法 —— doctor 给人看，
        # compat 给面板和命令行用，数据源只有一个。
        "compat": compat_rep,
        "jianying": _jianying_part(),
        "capabilities": _capabilities(),
        "panel": _panel_part(),
        "cache": _cache_part(),
    }
    r = rep["resolve"]
    # 结论只看 compat：它按「缺了会怎样」分级（致命/可降级），而 API 表面的
    # 那张表是给人翻的。以前拿 `api.missing` 直接当结论，表里有一处写错位
    # （方法挂到了别的对象下）就会在健康机器上报「有缺项」—— 诊断工具自己
    # 说谎，比不给诊断更浪费时间。
    ok = bool(r.get("loaded") and r.get("app"))
    if ok and compat_rep.get("features"):
        ok = bool(compat_rep.get("ok"))
    if ok and compat_rep.get("version_ok") is False:
        ok = False
    rep["ok"] = ok
    if r.get("api", {}).get("missing") and not compat_rep.get("missing"):
        # 表面表报了缺项、但能力表说没事：写进报告，但不当成故障。
        rep["note"] = ("接口表里有几项不在预期位置（%s），能力自检未受影响："
                       "以 compat 为准" % "、".join(r["api"]["missing"]))
    return rep


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    rep = run()
    text = json.dumps(rep, ensure_ascii=False, indent=1)
    if "--compact" in argv:
        text = json.dumps(rep, ensure_ascii=False)
    print(text)
    if not rep["ok"]:
        # 提示写在 stderr，stdout 保持纯 JSON，方便直接重定向成文件贴出去
        sys.stderr.write("\n报告显示有缺项。把上面这段 JSON 贴到 issue 里即可，"
                         "里面不含素材内容、工程名以外的隐私信息。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
