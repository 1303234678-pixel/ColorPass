"""使用前闸门 —— 三个前置条件不满足就不许动。

为什么要有这个模块（用户明确要求，也是实测踩出来的）
----------------------------------------------------
这条链路的每一步都假设「两个程序都在、达芬奇里是一张白纸」：

  · 剪映不开：草稿明文只在剪映进程内存里，磁盘是 base64(AES)，读不到。
  · 剪映开着但没打开草稿：内存里没有可识别的明文对象，会把别的草稿读进来。
  · 达芬奇不开：脚本接口连不上，AppendToTimeline 全部落空。
  · 达芬奇不是空白工程：会有三条真实后果 ——
      1) 我们会改工程设置（timelineFrameRate/分辨率），
         这会把已有时间线的帧率解释一起改掉，用户原来的工程被改坏；
      2) 逐段 AppendToTimeline 用的是绝对帧号，落在已有片段上会把它们顶掉；
      3) 轨道数不够时会 AddTrack，已有轨道的编号/顺序全乱。

所以这里的判据是硬的：不满足就抛 Gate，调用方直接中止，一个字都不往
达芬奇里写。宁可让用户多点一次「新建工程」，也不动他已有的东西。
"""
import os
import sys
import time

from . import paths

MOD = (r"C:/ProgramData/Blackmagic Design/DaVinci Resolve/Support/Developer"
       r"/Scripting/Modules")
if MOD not in sys.path:
    sys.path.insert(0, MOD)

import DaVinciResolveScript as dvr  # noqa: E402


class Gate(Exception):
    """前置条件不满足。code 供面板/CLI 分类，msg 直接给人看。"""

    def __init__(self, code, msg, detail=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.detail = detail or {}

    def as_dict(self):
        return {"ok": False, "gate": self.code, "error": self.msg,
                "gate_detail": self.detail}


def _tasklist_pids(image):
    """某进程的 PID 集合。一次 tasklist 约 0.3 秒，结果只用来做「在不在」判断。"""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % image, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="gbk", errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return set()
    pids = set()
    for ln in (out or "").splitlines():
        parts = [x.strip('"') for x in ln.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def resolve_app(tries=8, delay=1.0):
    """连达芬奇。它启动慢/正在切工程时会短暂返回 None，所以要重试。

    先花 0.3 秒看进程在不在：不在就直接判死。老写法无条件重试 12×2 秒
    （=24 秒），而「达芬奇根本没开」是最常见的失败，用户等半分钟才看到
    「没有响应脚本接口」，还以为是插件卡住了。
    """
    if not _tasklist_pids("Resolve.exe"):
        raise Gate("no_resolve",
                   "达芬奇没有在运行（找不到 Resolve.exe）。请先启动达芬奇，"
                   "打开一个空白工程，然后再点一次。",
                   {"why": "process not found"})
    last = ""
    for _ in range(max(1, tries)):
        try:
            r = dvr.scriptapp("Resolve")
        except Exception as e:                      # 接口异常也当未就绪
            r, last = None, "%s: %s" % (type(e).__name__, e)
        if r:
            return r
        time.sleep(delay)
    raise Gate("no_resolve",
               "达芬奇进程在，但脚本接口没有响应。请确认它已经进到某个工程里"
               "（不是启动画面），然后重试。",
               {"last_error": last})


def _walk_folders(folder, depth=0):
    out = [folder]
    if depth >= 6:
        return out
    try:
        subs = folder.GetSubFolderList() or []
    except Exception:
        subs = []
    for s in subs:
        out += _walk_folders(s, depth + 1)
    return out


def pool_paths(mp):
    paths_out = []
    for f in _walk_folders(mp.GetRootFolder()):
        for it in (f.GetClipList() or []):
            p = it.GetClipProperty("File Path") or ""
            if p:
                paths_out.append(p)
    return paths_out


def _unique_project_name(pm, base):
    try:
        names = set(pm.GetProjectListInCurrentFolder() or [])
    except Exception:
        names = set()
    if base not in names:
        return base
    for i in range(2, 100):
        n = "%s %d" % (base, i)
        if n not in names:
            return n
    return "%s %d" % (base, int(time.time()))


def open_blank_project(r, base="JY Bridge"):
    """在达芬奇里新建一个空白工程并进入，返回 (proj, name)。

    只在「没进任何工程」时用：此刻用户没在用达芬奇，新建工程不会打断工作。
    停在项目管理器时 GetCurrentProject() 仍会返回一个幻影工程
    （名字 Untitled Project、0 条时间线），闸门会把它当成空白工程放行，
    随后 CreateEmptyTimeline 静默返回 None —— 实测踩过，报错还误导成
    「同名时间线可能还在」。
    """
    pm = r.GetProjectManager()
    if not pm:
        raise Gate("no_project_manager", "达芬奇的工程管理器没有响应脚本接口。")
    name = _unique_project_name(pm, base)
    proj = pm.CreateProject(name)
    if not proj:
        raise Gate("create_project_failed",
                   "在达芬奇里新建空白工程「%s」失败。请手动「文件 → 新建工程」"
                   "（别用正在剪的那个），再重试。" % name,
                   {"tried_name": name})
    for _ in range(15):
        if r.GetCurrentPage():
            break
        time.sleep(1.0)
    return pm.GetCurrentProject() or proj, name


def daVinci(tag=None, require_blank=True, auto_project=True):
    """达芬奇侧前置条件。require_blank=True 时要求工程里没有别人的时间线。

    auto_project：停在项目管理器（GetCurrentPage() 为 None）时自动新建一个
    空白工程。只在这一种情况下动手 —— 已经进了工程就绝不新建，免得打断
    用户手上的工作。
    """
    tag = paths.TIMELINE_TAG if tag is None else tag
    r = resolve_app()
    product = r.GetProductName() or ""
    ver = r.GetVersionString() or ""
    if "Studio" not in product:
        raise Gate("needs_studio",
                   "当前是达芬奇免费版（%s）。免费版不开外部脚本接口，"
                   "送不进也导不出 LUT。请用 Studio 版。" % (product or "未知"),
                   {"product": product, "version": ver})

    pm = r.GetProjectManager()
    if not pm:
        raise Gate("no_project_manager", "达芬奇的工程管理器没有响应脚本接口。",
                   {"product": product, "version": ver})

    # 停在项目管理器时先等两秒：可能只是工程正在加载。
    page = r.GetCurrentPage()
    for _ in range(3):
        if page:
            break
        time.sleep(1.0)
        page = r.GetCurrentPage()

    created = ""
    if not page:
        if not auto_project:
            raise Gate("project_manager",
                       "达芬奇停在「项目管理器」，脚本接口还没进入任何工程。\n"
                       "请在达芬奇里打开一个**空白**工程（或「文件 → 新建工程」），"
                       "然后重试；也可以运行 `jybridge newproj` 让工具自己建。",
                       {"product": product, "version": ver, "page": None})
        _, created = open_blank_project(r, "JY Bridge")
        page = r.GetCurrentPage()

    proj = pm.GetCurrentProject() if pm else None
    if not proj:
        raise Gate("no_project",
                   "达芬奇里没打开工程。请先新建一个空白工程（别用正在剪的"
                   "那个），再重试。", {"product": product, "version": ver})

    names, foreign = [], []
    n_tl = proj.GetTimelineCount() or 0
    for i in range(1, n_tl + 1):
        tl = proj.GetTimelineByIndex(i)
        nm = (tl.GetName() or "") if tl else ""
        names.append(nm)
        if not nm.startswith(tag):
            foreign.append(nm)

    info = {
        "product": product, "version": ver, "project": proj.GetName(),
        "timelines": names, "timeline_count": n_tl,
        "foreign_timelines": foreign,
        "own_timelines": [nm for nm in names if nm.startswith(tag)],
        "fps": proj.GetSetting("timelineFrameRate"),
        "width": proj.GetSetting("timelineResolutionWidth"),
        "height": proj.GetSetting("timelineResolutionHeight"),
        "page": page,
        "created_project": created,
    }

    if require_blank and foreign:
        # 只拦「别人的时间线」。上一轮本工具建的 `JY · …` 不再拦 ——
        # send 建时间线时会先找同名：空的删掉复用，有内容的自动加序号，
        # 不会覆盖已调色的成果（这两条都有用例钉住）。原来一刀切要求
        # 工程全空，导致第二轮送入必然失败，用户每次都得手动新建工程。
        raise Gate(
            "not_blank",
            "当前工程「%s」里还有 %d 条不是本工具建的时间线（%s）。"
            "接着做会改动这个工程的帧率、并用绝对帧号往时间线上放片段，"
            "可能弄乱你正在剪的东西。\n"
            "请「文件 → 新建工程」建一个空工程再送入（或运行 "
            "`jybridge newproj`，工具会自己建一个干净的）。"
            % (proj.GetName(), len(foreign),
               "、".join(foreign[:3]) + ("…" if len(foreign) > 3 else "")),
            info)

    # 帧率：只有空工程才允许改。已有本工具的时间线时，改工程帧率会让
    # 已经调过色的时间线整体重算，所以只做一致性检查。
    if require_blank and n_tl and not foreign:
        try:
            cur_fps = float(proj.GetSetting("timelineFrameRate") or 0)
        except Exception:
            cur_fps = 0.0
        if cur_fps and abs(cur_fps - 30.0) > 0.01:
            raise Gate(
                "fps_mismatch",
                "工程「%s」现在是 %g fps，里面已经有本工具建的时间线。"
                "改成 30 fps 会让那些时间线整体重算（已调好的片段位置会变）。\n"
                "请新建一个空白工程再送入：`jybridge newproj`。"
                % (proj.GetName(), cur_fps), info)

    try:
        info["pool_clips"] = len(pool_paths(proj.GetMediaPool()))
    except Exception:
        info["pool_clips"] = -1
    return r, proj, info


def _jy_windows():
    """剪映所有可见顶层窗口 (hwnd, 标题, 面积)，按面积降序。"""
    import ctypes
    import ctypes.wintypes as wt
    import subprocess
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    u = ctypes.windll.user32
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe",
                              "/FO", "CSV"], capture_output=True, text=True,
                             encoding="gbk", errors="ignore").stdout
    except Exception:
        return []
    pids = set()
    for ln in out.splitlines()[1:]:
        parts = [x.strip('"') for x in ln.split('","')]
        if len(parts) > 1:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    found = []
    EP = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _l):
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        b = ctypes.create_unicode_buffer(300)
        u.GetWindowTextW(h, b, 300)
        if not b.value or not u.IsWindowVisible(h):
            return True
        r = wt.RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        found.append(((r.right - r.left) * (r.bottom - r.top), h, b.value))
        return True

    u.EnumWindows(EP(cb), 0)
    found.sort(reverse=True)
    return found


def lock_state(folder, max_age_hours=12):
    """草稿是否正被剪映开着：看草稿目录里的 .locked。

    为什么以它为准（2026-11-14 实测两条）：
      · 剪映打开草稿时在草稿目录建 .locked，关闭时删掉 —— 目标草稿被关掉后
        目录里确实没有 .locked，而新开的「11月14日」有。
      · 窗口标题不能用来判断：11.7 时代标题就是草稿名，但 11.8 里即使草稿开着，
        OS 窗口标题仍然是「剪映专业版」。用标题判定会误拦。
    存在很久的 .locked 是崩溃残留，所以再加一条修改时间上限。
    """
    p = os.path.join(folder, ".locked")
    if not os.path.exists(p):
        return False, 0.0, p
    age = max(0.0, time.time() - os.path.getmtime(p))
    return (age <= max_age_hours * 3600), age, p


def jianying_alive():
    """剪映进程在不在（**不要求**打开了某份草稿）。

    为什么写回只查这一条：写回不读剪映的内存了 —— 它按「送入时的快照」重建
    一份新草稿，只往草稿目录里新建文件夹、并登记进 root_meta_info。所以你在
    剪映里切到别的草稿、甚至把原草稿删了，都不影响写回。
    反过来，剪映必须开着：它在首页会重读索引，新草稿才会出现在列表里；
    否则一退出就拿内存里的旧索引盖掉我们刚写进去的登记。
    """
    n = len(_tasklist_pids("JianyingPro.exe"))
    if not n:
        raise Gate("no_jianying",
                   "剪映没有在运行。写回要在剪映的草稿目录里新建一份草稿并登记，"
                   "剪映开着新草稿才会出现在列表里。请先打开剪映（不用打开草稿）。")
    return {"processes": n}


def jianying(require_fresh=False):
    """剪映侧前置条件：进程在、能认出当前草稿、而且这份草稿确实被打开着。

    require_fresh=True 时再多查一条**不扫内存**的硬信号：草稿是不是在打开
    之后又被编辑过（`draft_settings` 的 last_edit 晚于 `.locked` 的建立时刻）。
    只有送入需要它 —— 送入读的是剪映内存里「打开那一刻」的明文，编辑过就
    一定是旧版；写回用的是送入快照，不受影响，所以默认不开，免得误拦写回。

    放在这里而不是只放在 draftio.read 里，是因为 read 要先扫内存（几秒到
    几十秒），用户点完按钮得等半分钟才看到「请重新打开」；这两条都是磁盘
    上的文件时间，几十毫秒就能给出同样的结论。
    """
    from . import draftio
    try:
        cur = draftio.identify()
    except draftio.NoDraft as e:
        raise Gate("no_draft", str(e))
    folder = str(cur.get("folder") or "")
    if not folder or not os.path.isdir(folder):
        raise Gate("no_folder",
                   "认到的草稿目录不存在：%s。请在剪映里重新打开一次草稿。"
                   % (folder or "（空）"))

    opened, age, lock_path = lock_state(folder)
    # 不再调 window_title()：它要再跑一次 tasklist + 枚举全部顶层窗口
    # （约 0.6 秒），而上面已经证明 11.8 的标题恒为「剪映专业版」，
    # 判不了任何事。诊断信息用 lock_path / lock_age_sec 就够。
    if not opened:
        raise Gate("draft_not_open",
                   "剪映现在没有打开草稿「%s」（草稿目录里没有有效的 .locked）。"
                   "剪映只在「打开草稿」那一刻把最新明文留在内存，回到首页后"
                   "留在里面的是旧版本，照它写回会覆盖你之后的修改。\n"
                   "请在剪映里双击打开这份草稿，然后再重试。"
                   % (cur.get("name") or ""),
                   {"draft": cur.get("name"), "folder": folder,
                    "lock_age_sec": round(age), "lock_path": lock_path})

    if require_fresh and draftio.edited_since_open(folder):
        edit = draftio.draft_edit_time(folder)
        op = draftio.open_mtime(folder)
        raise Gate("stale_draft",
                   draftio.stale_message({
                       "edited_since_open": True,
                       "draft_edit_text": draftio.stamp(edit),
                       "opened_text": draftio.stamp(op),
                   }),
                   {"draft": cur.get("name"), "folder": folder,
                    "draft_edit_at": edit, "opened_at": op,
                    "lock_age_sec": round(age)})

    return {"draft": cur.get("name"), "draft_id": cur.get("timeline_id"),
            "folder": folder, "locked": True,
            "lock_age_sec": round(age),
            "draft_content": os.path.exists(os.path.join(folder,
                                                         "draft_content.json"))}


def both(tag=None, require_blank=True, auto_project=True, require_fresh=False):
    """送入/取回/写回之前都该过这一关。返回两侧信息，不满足就抛 Gate。

    auto_project 只该在「送入」时打开：取回/写回要读已有时间线，如果这时
    达芬奇停在项目管理器还替用户新建一个空工程，反而找不到那条时间线。

    require_fresh 只给送入用（见 jianying）。
    """
    jy = jianying(require_fresh=require_fresh)
    r, proj, dv = daVinci(tag=tag, require_blank=require_blank,
                          auto_project=auto_project)
    return {"jianying": jy, "davinci": dv, "resolve": r, "project": proj}


def describe(info):
    dv = info.get("davinci") or {}
    jy = info.get("jianying") or {}
    return ("剪映「%s」· 达芬奇「%s」（%s %s）· 已有时间线 %d 条"
            % (jy.get("draft"), dv.get("project"), dv.get("product"),
               dv.get("version"), dv.get("timeline_count") or 0))
