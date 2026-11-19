"""colorpass doctor —— 一条命令说清「这台机器能跑到哪一步」。

为什么要有它
------------
兼容性靠一台机器是试不出来的：Windows / macOS、达芬奇 17~20、剪映 10~11，
组合太多，开发者手里也不可能有每一台。所以换个办法 —— **让代码自己报病历**：
用户跑一次 `colorpass doctor`，把这段 JSON 贴进 issue，就能知道那个版本上
每个 API 在不在、路径对不对、卡在哪一步。

输出里 `resolve.api.missing` 是重点：它是「这个版本没有这些方法」的清单。
比版本号可靠得多 —— 达芬奇的 API 是增量加的，同一个大版本内部也会有增删，
所以判断一律用 `hasattr`，版本号只用于展示。

这个命令**不修改任何东西**：不写草稿、不动时间线、不改配置，只读和试调。
没装达芬奇、没开达芬奇、没装剪映，都能跑，缺什么就报什么。
"""
import json
import os
import platform
import sys
import tempfile

from . import __version__, install, memscan, paths, probe, resolve, retime


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


def _resolve_part(app=None):
    out = {"module": _try(resolve.module_path), "searched": _try(resolve.existing_dirs, []),
           "loaded": False, "app": False}
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

    pm = _try(app.GetProjectManager)
    proj = _try(pm.GetCurrentProject) if pm else None
    out["project"] = _try(proj.GetName) if proj else None

    tl = _try(proj.GetCurrentTimeline) if proj else None
    item = None
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

    groups, missing = _collect_api({"app": app, "project_manager": pm,
                                    "project": proj, "timeline": tl, "item": item})
    out["api"] = groups
    out["api"]["missing"] = missing
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
    return {
        "memscan": bool(getattr(memscan, "AVAILABLE", False)),
        "ffmpeg": bool(_try(retime.available, False)),
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


def run(app=None):
    """收集一份诊断报告（纯 dict，可直接 json.dumps）。"""
    rep = {
        "colorpass": __version__,
        "platform": _platform(),
        "data": _data_dir(),
        "resolve": _resolve_part(app),
        "jianying": _jianying_part(),
        "capabilities": _capabilities(),
        "panel": _panel_part(),
    }
    r = rep["resolve"]
    rep["ok"] = bool(r.get("loaded") and r.get("app") and not r.get("api", {}).get("missing"))
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
