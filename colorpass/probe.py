"""剪映桥：识别正在编辑的工程，并把它的草稿路径解析出来。

为什么是这个形状
----------------
剪映 6.0 起把 draft_content.json 加密了（本机 11.7.0，实测密文熵 7.95、
长度非 16 字节对齐）。所以**直接读工程文件里的素材列表**做不到，除非去调
videoeditor.dll 的解密导出（开源项目 jy-draftc / jy-draft-port 走的就是
这条路，但只测到 10.3~10.6，11.x 结构又变过）。

所以本模块只做一件事：**认工程**。从剪映正在运行的进程和窗口标题认出
"现在开的是哪个工程"，返回它的目录、主时间线 ID、锁状态，再顺着
`Projects/com.lveditor.draft/<工程>/` 把能拿到的明文线索（代理转码清单、
素材路径）捞出来，交给 `jy_memscan` 去进程内存里取明文草稿。

装 LUT / 装预设那两条链已经删掉了：那是"把调色塞进剪映自己的库"，
入口（顶栏「装成 LUT」「存成预设」）随瘦身一起没了，留着只是死代码。
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"


# ---------------------------------------------------------------- 路径探测
def user_data_dir():
    """剪映的用户数据根目录。允许用环境变量覆盖，方便测试。"""
    env = os.environ.get("JY_USERDATA")
    if env:
        return Path(env)
    if MACOS:
        return Path(os.path.expanduser("~/Movies/JianyingPro/User Data"))
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "User Data"
    return base


def install_dir():
    """返回 (版本号, 版本目录)。版本目录里应该有 videoeditor.dll。

    这套 `Apps/<版本>/videoeditor.dll` 是 Windows 安装器的布局；macOS 是
    .app bundle 里的固定结构，没有对应的「版本目录」。返回 (None, None)
    只影响「装没装/什么版本」这条展示信息，不影响读草稿。
    """
    best = (None, None)
    if not WINDOWS:
        return best
    apps = Path(os.environ.get("LOCALAPPDATA", "")) / "JianyingPro" / "Apps"
    if not apps.is_dir():
        return best
    for d in apps.iterdir():
        if not d.is_dir():
            continue
        if not (d / "videoeditor.dll").exists():
            continue
        if best[0] is None or d.name > best[0]:
            best = (d.name, d)
    return best


def is_running():
    if not WINDOWS:
        # mac/Linux 用 ps；进程名两边都叫 JianyingPro（CapCut 是 CapCut）
        try:
            out = subprocess.run(["ps", "-axo", "comm="], capture_output=True,
                                 timeout=8).stdout.decode("utf-8", errors="replace")
        except Exception:
            return False
        low = out.lower()
        return any(n in low for n in ("jianyingpro", "jianying", "capcut"))
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/NH"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "JianyingPro.exe" in out.decode("gbk", errors="replace")
    except Exception:
        return False


def draft_root():
    return user_data_dir() / "Projects" / "com.lveditor.draft"


# ---------------------------------------------------------------- 认工程
def _meta_index():
    """root_meta_info.json 是明文的：工程名、目录、修改时间都在里面。"""
    p = draft_root() / "root_meta_info.json"
    idx = {}
    if not p.exists():
        return idx
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return idx
    for e in d.get("all_draft_store", []):
        folder = str(e.get("draft_fold_path") or "").replace("\\", "/")
        if folder:
            idx[folder.lower()] = {
                "name": e.get("draft_name") or Path(folder).name,
                "folder": folder,
                "modified": e.get("tm_draft_modified") or 0,
            }
    return idx


def process_windows():
    """实时枚举剪映可见窗口及所属 PID；只查询，不发送界面操作。"""
    if not WINDOWS:
        # Win32 窗口枚举在 mac/Linux 上不存在。返回同一份结构，只是没有窗口
        # 证据 —— 上层照原样走「认不出当前草稿」，不会抛异常。
        return {"running": is_running(), "windows": [], "unsupported": True}
    import csv
    import ctypes
    from ctypes import wintypes as W

    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/FO", "CSV", "/NH"],
        capture_output=True, timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    pids = {int(row[1]) for row in csv.reader(
        out.decode("gbk", errors="replace").splitlines())
        if len(row) > 1 and row[0].lower() == "jianyingpro.exe"
        and row[1].isdigit()}
    u = ctypes.windll.user32
    callback = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    u.EnumWindows.argtypes = [callback, W.LPARAM]
    u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    u.IsWindowVisible.argtypes = [W.HWND]
    u.GetWindowTextLengthW.argtypes = [W.HWND]
    u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
    rows = []

    @callback
    def visit(hwnd, _):
        pid = W.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd):
            text = ctypes.create_unicode_buffer(u.GetWindowTextLengthW(hwnd) + 1)
            u.GetWindowTextW(hwnd, text, len(text))
            if text.value:
                rows.append({"pid": pid.value, "hwnd": int(hwnd),
                             "title": text.value})
        return True

    u.EnumWindows(visit, 0)
    return {"running": bool(pids), "windows": rows}


def process_draft_folders(pid):
    """标题不含工程名时，只读查询该窗口进程仍持有的草稿文件句柄。"""
    if not WINDOWS:
        return []
    import ctypes as C
    from ctypes import wintypes as W

    class Handle(C.Structure):
        _fields_ = [("object", C.c_void_p), ("pid", C.c_size_t),
                    ("value", C.c_size_t), ("access", W.ULONG),
                    ("backtrace", W.USHORT), ("type", W.USHORT),
                    ("attributes", W.ULONG), ("reserved", W.ULONG)]

    class Header(C.Structure):
        _fields_ = [("count", C.c_size_t), ("reserved", C.c_size_t)]

    k = C.WinDLL("kernel32", use_last_error=True)
    nt = C.WinDLL("ntdll")
    nt.NtQuerySystemInformation.argtypes = [W.ULONG, C.c_void_p, W.ULONG,
                                           C.POINTER(W.ULONG)]
    nt.NtQuerySystemInformation.restype = W.LONG
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.GetCurrentProcess.restype = W.HANDLE
    k.DuplicateHandle.argtypes = [W.HANDLE, W.HANDLE, W.HANDLE,
                                   C.POINTER(W.HANDLE), W.DWORD, W.BOOL, W.DWORD]
    k.GetFileType.argtypes = [W.HANDLE]
    k.GetFinalPathNameByHandleW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD]
    k.CloseHandle.argtypes = [W.HANDLE]
    size = 1024 * 1024
    while size <= 128 * 1024 * 1024:
        buf = C.create_string_buffer(size)
        needed = W.ULONG()
        status = nt.NtQuerySystemInformation(64, buf, size, C.byref(needed))
        if status == 0:
            break
        if status != -1073741820:  # STATUS_INFO_LENGTH_MISMATCH
            return []
        size = max(size * 2, needed.value + 65536)
    else:
        return []
    source = k.OpenProcess(0x0040, False, pid)  # PROCESS_DUP_HANDLE，仅复制本地查询句柄
    if not source:
        return []
    result = set()
    root = draft_root().resolve()
    try:
        count = Header.from_buffer(buf).count
        for i in range(count):
            entry = Handle.from_buffer(buf, C.sizeof(Header) + i * C.sizeof(Handle))
            if entry.pid != pid:
                continue
            duplicate = W.HANDLE()
            if not k.DuplicateHandle(source, entry.value, k.GetCurrentProcess(),
                                     C.byref(duplicate), 0, False, 2):
                continue
            try:
                if k.GetFileType(duplicate) != 1:  # 跳过管道等可能阻塞的句柄
                    continue
                text = C.create_unicode_buffer(32768)
                n = k.GetFinalPathNameByHandleW(duplicate, text, len(text), 0)
                if not 0 < n < len(text):
                    continue
                path = Path(text.value.removeprefix("\\\\?\\"))
                if path.name not in (".locked", "draft_content.json", "project.json"):
                    continue
                try:
                    parts = path.relative_to(root).parts
                except ValueError:
                    continue
                if (len(parts) == 2 or
                        (len(parts) == 3 and parts[1:] == ("Timelines", "project.json"))):
                    result.add(str(root / parts[0]))
            finally:
                k.CloseHandle(duplicate)
    finally:
        k.CloseHandle(source)
    return sorted(result)


def _title_names(title):
    """只接受完整工程名或带已知应用前后缀的完整标题。"""
    title = str(title).strip()
    apps = ("剪映专业版", "剪映", "JianyingPro")
    if not title or title in apps:
        return set()
    names = {title}
    for app in apps:
        for sep in (" - ", " — ", " | "):
            if title.endswith(sep + app):
                names.add(title[:-len(sep + app)].strip())
            if title.startswith(app + sep):
                names.add(title[len(app + sep):].strip())
    return names


def resolve_open_draft(windows, *, live_handles=False):
    """由实时窗口证据精确定位目录及主时间线；锁文件不参与决策。"""
    root = draft_root()
    if not root.is_dir():
        return None, "找不到剪映草稿目录：%s" % root
    if not WINDOWS and not windows:
        # 目录找到了，但「当前打开的是哪一个」这一步依赖 Win32 窗口证据。
        # 与其猜一个「最近修改的草稿」，不如明确说清楚，别让用户拿错草稿去调色。
        return None, (
            "已找到剪映草稿目录（%s），但 macOS 上暂时无法自动识别「当前打开的是哪一份」"
            "（这一步依赖 Windows 的窗口接口）。请在 Windows 上操作，"
            "或把 `colorpass doctor` 的输出贴到 issue 里帮助补齐 mac 支持。" % root)
    idx = _meta_index()
    folders = []
    for d in root.iterdir():
        if d.is_dir() and (d / "draft_content.json").is_file():
            info = idx.get(str(d).replace("\\", "/").lower(), {})
            folders.append((d, info.get("name") or d.name))
    matches = {}
    for window in windows:
        names = _title_names(window.get("title"))
        for folder, name in folders:
            if name in names:
                key = (window["pid"], str(folder))
                matches[key] = (folder, name, window)
    if not matches and live_handles:
        # 11.7 把工程名画在自绘标题栏，Win32 标题只有应用名。
        # 此时必须由该可见窗口 PID 的实时打开句柄证明目录，不能看锁文件是否存在。
        for pid in {w["pid"] for w in windows}:
            held = process_draft_folders(pid)
            for folder, name in folders:
                if str(folder) in held:
                    window = next(w for w in windows if w["pid"] == pid)
                    matches[(pid, str(folder))] = (folder, name,
                        dict(window, evidence="process_file_handle"))
    if len(matches) != 1:
        error = ("窗口工程名或实时文件句柄对应多个进程或草稿目录，无法精确匹配。" if matches else
                 "剪映窗口未提供可精确匹配目录的工程名；点击匹配可重新检测。")
        return None, error
    folder, name, window = next(iter(matches.values()))
    try:
        project = json.loads((folder / "Timelines" / "project.json").read_text(
            encoding="utf-8-sig"))
        if not isinstance(project, dict) or not isinstance(project.get("timelines"), list):
            raise ValueError("工程索引必须包含时间线列表")
        timeline_id = project["main_timeline_id"]
        if not isinstance(timeline_id, str) or str(uuid.UUID(timeline_id)).lower() != timeline_id.lower():
            raise ValueError("主时间线 ID 不是标准 UUID")
        entries = [t for t in project.get("timelines", [])
                   if isinstance(t, dict) and str(t.get("id", "")).lower() == timeline_id.lower()
                   and not t.get("is_marked_delete")]
        dirs = [p for p in (folder / "Timelines").iterdir()
                if p.is_dir() and p.name.lower() == timeline_id.lower()]
        if len(entries) != 1 or len(dirs) != 1:
            raise ValueError("主时间线记录或对应目录缺失、重复或已删除")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, "工程「%s」的主时间线信息无效：%s" % (name, exc)
    return {"name": name, "folder": str(folder),
            "locked": (folder / ".locked").is_file(), "timeline_id": timeline_id,
            "pid": window["pid"], "hwnd": window["hwnd"],
            "window_title": window["title"],
            "evidence": window.get("evidence", "window_title")}, ""


def open_draft():
    """识别实时窗口对应的工程，不使用锁数量或文件修改时间。"""
    state = process_windows()
    return resolve_open_draft(state["windows"], live_handles=True)[0] if state["running"] else None


def same_draft(a, b):
    """同步前后必须仍是同一进程、目录及主时间线。"""
    if not a or not b:
        return False
    return (bool(a.get("pid")) and a.get("pid") == b.get("pid")
            and os.path.normcase(os.path.normpath(a.get("folder") or "")) ==
            os.path.normcase(os.path.normpath(b.get("folder") or ""))
            and bool(a.get("timeline_id"))
            and str(a["timeline_id"]).lower() == str(b.get("timeline_id", "")).lower())


def recent_drafts(n=8):
    root = draft_root()
    items = []
    if not root.is_dir():
        return items
    for d in root.iterdir():
        if not d.is_dir() or not (d / "draft_content.json").exists():
            continue
        try:
            items.append((d.stat().st_mtime, d))
        except Exception:
            pass
    items.sort(reverse=True)
    idx = _meta_index()
    out = []
    for m, d in items[:n]:
        info = idx.get(str(d).replace("\\", "/").lower(), {})
        out.append({"name": info.get("name") or d.name, "folder": str(d),
                    "mtime": m})
    return out


def draft_is_encrypted(draft_folder):
    """判断草稿正文是否密文（这决定我们能不能读素材列表）。"""
    p = Path(draft_folder) / "draft_content.json"
    if not p.exists():
        return None
    try:
        head = p.read_bytes()[:8]
    except Exception:
        return None
    return not head.lstrip().startswith(b"{")


def draft_material_hints(draft_folder):
    """工程里能拿到的明文线索（不完整，只作提示）。

    剪映为"播放优化"生成的代理转码清单 draft_agency_info.json 是明文的，
    里面带 source_path。有代理的素材能捞到，没代理的捞不到 —— 所以这只是
    提示，不能当素材列表用。
    """
    p = Path(draft_folder) / "draft_agency_info.json"
    out = []
    if not p.exists():
        return out
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return out
    for m in d.get("marterial") or []:
        s = str(m.get("source_path") or "").replace("\\", "/")
        if s and s not in out:
            out.append(s)
    return out


def snapshot():
    """一次读取剪映状态；只提供当前工程，不暴露最近草稿候选。"""
    ver, path = install_dir()
    try:
        state = process_windows()
        running = state["running"]
        d, error = (resolve_open_draft(state["windows"], live_handles=True) if running else
                    (None, "剪映未运行，请先启动剪映并打开目标草稿。"))
    except Exception as exc:
        state = {"windows": []}
        running, d = False, None
        error = "读取剪映实时窗口失败：%s" % exc
    enc = draft_is_encrypted(d["folder"]) if d else None
    return {
        "installed": ver is not None,
        "version": ver,
        "userdata": str(user_data_dir()),
        "draft_root": str(draft_root()),
        "running": running,
        "draft": d,
        "draft_error": error,
        "windows": state["windows"],
        "draft_encrypted": enc,
        "hints": draft_material_hints(d["folder"]) if d else [],
        "recent": [],
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False, indent=1)[:1200])
