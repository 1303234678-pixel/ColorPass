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
import time

from . import paths, platforms, procs, resolve

# 延迟加载：没装达芬奇的机器照样能 import 本模块（check / plan 不需要 API）。
dvr = resolve.lazy()

# 达芬奇主进程的可执行文件路径（macOS）。装法有两种：/Applications 下直接一个
# 目录，或 /Applications/DaVinci Resolve/DaVinci Resolve.app —— 路径尾部相同，
# 按尾部匹配两种都能命中，又不会命中 Helper 子进程。
M_RESOLVE_BIN = "/DaVinci Resolve.app/Contents/MacOS/Resolve"


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


def _resolve_pids():
    """达芬奇主进程 PID 集合（跨平台）。

    Windows 靠 tasklist 找 Resolve.exe；macOS 上没有 .exe，进程身份只能看
    可执行文件全路径 `/Applications/DaVinci Resolve/DaVinci Resolve.app/
    Contents/MacOS/Resolve` —— 只认 Resolve.exe 会让「达芬奇明明开着」被判
    成没启动。

    macOS 先走 libproc（procs 模块，不 exec 任何东西）。这一步是有实据的：
    面板是从达芬奇内部 Popen 出来的，那里 `pgrep` 拿不到结果，于是送出一
    秒就退出、报「达芬奇没有在运行」。libproc 不受子进程环境影响，pgrep
    只作为兜底。
    """
    if platforms.is_mac():
        found = procs.find(suffix=M_RESOLVE_BIN)
        if found is not None:
            return found
        import subprocess
        try:
            out = subprocess.run(
                ["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"],
                capture_output=True, text=True, timeout=6).stdout
        except Exception:
            return set()
        return {int(x) for x in (out or "").split() if x.isdigit()}
    return _tasklist_pids("Resolve.exe")


def _connect():
    """问一次脚本接口。连上返回对象，连不上返回 (None, 原因)。"""
    try:
        return dvr.scriptapp("Resolve"), ""
    except Exception as exc:                      # 接口异常也当未就绪
        return None, "%s: %s" % (type(exc).__name__, exc)


def _probe_note():
    """进程探测失败时的现场记录（写进日志，供事后定位）。

    只在「要判死」这一刻调用，平时零开销。记录的是「探测本身怎么了」，
    而不是「达芬奇在不在」—— 前者才是这类假故障的真正原因。
    """
    info = {"platform": platforms.name()}
    try:
        pids = procs.all_pids()
        info["libproc"] = "unavailable" if pids is None else "ok"
        info["pids"] = 0 if not pids else len(pids)
        info["videofusion"] = sorted(procs.find(name="VideoFusion-macOS") or [])
    except Exception as exc:
        info["libproc"] = "error: %s" % exc
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-f", "DaVinci Resolve"],
                           capture_output=True, text=True, timeout=6)
        info["pgrep"] = {"rc": r.returncode, "out": (r.stdout or "")[:80],
                         "err": (r.stderr or "")[:120]}
    except Exception as exc:
        info["pgrep"] = "error: %s" % exc
    try:
        paths.note("resolve probe: %s" % info)
    except Exception:
        pass
    return info


def resolve_app(tries=8, delay=1.0):
    """连达芬奇。

    判据的顺序很关键：**脚本接口是权威，进程枚举只用来决定「要不要等」**。

    反过来（先要求进程在）会有一个致命的假故障：进程枚举在达芬奇自己的
    子进程环境里可能拿不到结果 —— 面板就是这么踩的，`pgrep` 明明在终端能
    用，从面板里返回空，于是「达芬奇开着」被判成「没运行」，send 一秒就
    退出码 4。进程枚举本来只是个「省掉 8 秒重试」的加速手段，不该有否决权。

    所以：先直接连；连不上再看进程在不在，不在时才判死（这就是最常见的
    「达芬奇根本没开」，0.3 秒出结论，不用让用户等半分钟）。
    """
    r, last = _connect()
    if r:
        return r

    pids = _resolve_pids()
    if pids:
        # 进程在，只是还没准备好（正在切工程、还在启动画面、正在载入素材）
        for _ in range(max(1, tries) - 1):
            time.sleep(delay)
            r, last = _connect()
            if r:
                return r
        raise Gate("no_resolve",
                   "达芬奇进程在，但脚本接口没有响应。请确认它已经进到某个工程里"
                   "（不是启动画面），然后重试。",
                   {"last_error": last, "pids": sorted(pids)})

    # 进程也看不到。可能是真没开，也可能这台机器上进程枚举被环境挡了 ——
    # 再连一次确认，两次都连不上才判死，别把能用的环境判成没开。
    time.sleep(0.4)
    r, last = _connect()
    if r:
        return r
    raise Gate("no_resolve",
               "达芬奇没有在运行（找不到%s）。请先启动达芬奇，"
               "打开一个空白工程，然后再点一次。"
               % ("Resolve 进程" if platforms.is_mac() else " Resolve.exe"),
               {"why": "process not found", "last_error": last,
                "probe": _probe_note()})


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


def open_blank_project(r, base="ColorPass"):
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
                       "然后重试；也可以运行 `colorpass newproj` 让工具自己建。",
                       {"product": product, "version": ver, "page": None})
        _, created = open_blank_project(r, "ColorPass")
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
        if not paths.is_own_timeline(nm, tag):
            foreign.append(nm)

    info = {
        "product": product, "version": ver, "project": proj.GetName(),
        "timelines": names, "timeline_count": n_tl,
        "foreign_timelines": foreign,
        "own_timelines": [nm for nm in names if paths.is_own_timeline(nm, tag)],
        "fps": proj.GetSetting("timelineFrameRate"),
        "width": proj.GetSetting("timelineResolutionWidth"),
        "height": proj.GetSetting("timelineResolutionHeight"),
        "page": page,
        "created_project": created,
    }

    if require_blank and foreign:
        # 只拦「别人的时间线」。上一轮本工具建的 `CP · …` 不再拦 ——
        # send 建时间线时会先找同名：空的删掉复用，有内容的自动加序号，
        # 不会覆盖已调色的成果（这两条都有用例钉住）。原来一刀切要求
        # 工程全空，导致第二轮送入必然失败，用户每次都得手动新建工程。
        raise Gate(
            "not_blank",
            "当前工程「%s」里还有 %d 条不是本工具建的时间线（%s）。"
            "接着做会改动这个工程的帧率、并用绝对帧号往时间线上放片段，"
            "可能弄乱你正在剪的东西。\n"
            "请「文件 → 新建工程」建一个空工程再送入（或运行 "
            "`colorpass newproj`，工具会自己建一个干净的）。"
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
                "请新建一个空白工程再送入：`colorpass newproj`。"
                % (proj.GetName(), cur_fps), info)

    try:
        info["pool_clips"] = len(pool_paths(proj.GetMediaPool()))
    except Exception:
        info["pool_clips"] = -1
    return r, proj, info


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

    macOS 上「剪映」不是 JianyingPro.exe，而是 `/Applications/VideoFusion-macOS.app`
    （bundle com.lemon.lvpro）。上一版这里仍然只查 `JianyingPro.exe`，于是在 Mac
    上恒为 0 —— 写回一点就退，报「剪映没有在运行」，可剪映明明开着（面板同一
    时刻显示 alive=True）。进程名和判定口径必须跟平台走。

    判据顺序也是「有证据就算活着」，不是「枚举不到就判死」：读取器 dylib 就住在
    剪映进程里，能 ping 通就等于剪映在跑 —— 这比进程枚举更硬，不受 PATH /
    entitlement 影响。
    """
    if platforms.is_mac():
        found = procs.find(name="VideoFusion-macOS")
        if found:
            return {"processes": len(found)}
        from . import macdraft
        ok, info = macdraft.reader_ping()
        if ok:
            return {"processes": 1, "inferred": True, "reader": info}
        raise Gate("no_jianying",
                   "剪映没有在运行（也没连上读取器：%s）。写回要在剪映的草稿目录里"
                   "新建一份草稿并登记，剪映开着新草稿才会出现在列表里。\n"
                   "处理：退出剪映，回到 ColorPass 点「读取草稿」由它启动剪映；"
                   "新草稿会出现在剪映首页的草稿列表里。" % (info or "无响应"),
                   {"platform": platforms.name(), "found": sorted(found or [])})
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

    if platforms.is_mac():
        # macOS：草稿正文来自**磁盘上最新的密文**（由剪映进程内的读取器解密），
        # 所以「剪映必须正开着这份草稿」这个 Windows 前提在这里不成立 ——
        # 不开草稿也能读到最新版本，也就不存在「拿旧版覆盖新改动」的风险。
        # 唯一必须守住的是读取器在位，否则一个字节都读不到。
        from . import macdraft
        ok, info = macdraft.reader_ping()
        if not ok:
            raise Gate("reader_down",
                       "剪映没有带着读取器运行（%s）。\n"
                       "macOS 上剪映 11.7+ 的草稿密钥只在它自己的进程里有效，"
                       "ColorPass 必须在启动剪映时注入读取器才能读草稿。\n"
                       "处理：退出剪映，回到 ColorPass 点「读取草稿」由它启动剪映，"
                       "之后剪映照常使用。"
                       % info,
                       {"draft": cur.get("name"), "folder": folder,
                        "reader": info})
        return {"draft": cur.get("name"), "draft_id": cur.get("timeline_id"),
                "folder": folder, "locked": bool(opened),
                "lock_age_sec": round(age), "reader": True,
                "draft_content": False}

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
