"""从正在运行的剪映进程内存里找解密后的时间线 JSON（只读，不动任何文件）。

为什么：剪映把草稿加密落盘，但正在编辑时，明文草稿一定存在于进程内存里。
找到 `"videos"` / `"tracks"` 这类 JSON 键，就能把时间线捞出来 ——
这样连「右键保存为预设」都不用了。

只读本机用户自己的剪映进程，读取后把命中片段 dump 成文件供检查。

平台
----
这条路全是 Win32 调用（OpenProcess / VirtualQueryEx / ReadProcessMemory），
只有 Windows 能用。但模块本身必须在 mac / Linux 上照样 import —— 顶层写
`ctypes.windll` 的话，`import colorpass.draftio` 会直接 AttributeError，
连 `colorpass check` 都跑不起来。所以 Windows 相关的声明整段收进分支里：

  AVAILABLE  True 才真的能扫
  mac/Linux  扫描入口一律返回空，草稿读取自动退到「本地缓存 → 载入备份」两级

想彻底关掉扫描（包括 Windows）用 `COLORPASS_NO_MEMSCAN=1`，那是 draftio 的事，
这里只负责「这台机器有没有这个能力」。
"""
import os
import subprocess
import sys

WINDOWS = sys.platform.startswith("win")
AVAILABLE = WINDOWS

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT = 0x1000
    READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}  # R / RW / WC / XR / XRW / XWC

    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                   ctypes.POINTER(MBI), ctypes.c_size_t]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
else:
    ctypes = None
    wintypes = None
    k32 = None
    MBI = None
    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT = 0x1000
    READABLE = set()


def pids():
    if not AVAILABLE:
        return []
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    res = []
    for ln in out.decode("gbk", errors="replace").splitlines():
        parts = [p.strip('"') for p in ln.split('","')]
        if len(parts) >= 2 and any(n in parts[0].lower()
                                   for n in ("jianyingpro", "jianying", "capcut")):
            try:
                res.append(int(parts[1]))
            except ValueError:
                pass
    return res


def pids_by_mem():
    """按内存占用从大到小给进程排序。

    剪映有十来个进程（渲染、解码、崩溃上报……），草稿只在占内存最大的
    主进程里。先扫大的能少读几 GB —— 实测从 20 秒降到 4 秒。
    """
    if not AVAILABLE:
        return []
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    mem = {}
    for ln in out.decode("gbk", errors="replace").splitlines():
        parts = [p.strip('"') for p in ln.split('","')]
        if len(parts) >= 5 and any(n in parts[0].lower()
                                   for n in ("jianyingpro", "jianying", "capcut")):
            try:
                mem[int(parts[1])] = int(parts[4].replace(",", "").replace(" K", ""))
            except ValueError:
                pass
    return sorted(mem, key=lambda p: -mem[p])


def read(h, addr, size):
    if not AVAILABLE:
        return b""
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, addr, buf, size, ctypes.byref(n)):
        return b""
    return buf.raw[:n.value]


def _balanced(blob, start=0, limit=None):
    """从 start 处的 '{' 开始找配对结束位置（考虑字符串与转义）。"""
    depth = 0
    instr = False
    esc = False
    end = -1
    n = len(blob) if limit is None else min(len(blob), limit)
    for j in range(start, n):
        ch = blob[j]
        if instr:
            if esc:
                esc = False
            elif ch == 0x5C:
                esc = True
            elif ch == 0x22:
                instr = False
            continue
        if ch == 0x22:
            instr = True
        elif ch == 0x7B:
            depth += 1
        elif ch == 0x7D:
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    return end


def read_span(h, spans, addr, limit=64 * 1024 * 1024):
    """跨内存区块连续读取。

    为什么必须跨区块：实测草稿 JSON 有 20 秒量级、几十 KB，剪映把它放在
    连续虚拟地址上，但 VirtualQueryEx 报出来的是**若干个相邻区块**。
    只在单个区块里括号配对，会因为在半截处遇到 '}' 而提前收尾，
    解析出来的是残缺对象 —— 这正是「工程里明明有素材，却读不到片段」的原因。
    """
    out = bytearray()
    i = 0
    n = len(spans)
    while i < n and spans[i][0] + spans[i][1] <= addr:
        i += 1
    if i >= n or not (spans[i][0] <= addr < spans[i][0] + spans[i][1]):
        return b""
    pos = addr
    while i < n and len(out) < limit:
        base, size = spans[i]
        if pos < base:
            gap = base - pos
            got = read(h, pos, gap)
            if len(got) < gap:      # 中间有未映射的空洞，放弃
                break
            out += got
            pos = base
        take = min(base + size - pos, limit - len(out))
        got = read(h, pos, take)
        if not got:
            break
        out += got
        pos += len(got)
        if len(got) < take:
            break
        i += 1
    return bytes(out)


_ROOT = b'{"id":"'
_VERSION = b'"version":'


def _try_root(h, spans, addr):
    """把 addr 当成草稿根对象试解析一次。"""
    import json
    blob = read_span(h, spans, addr, 64 * 1024 * 1024)
    if not blob.startswith(_ROOT):
        return None
    end = _balanced(blob, 0)
    if end <= 0:
        return None
    try:
        d = json.loads(blob[:end].decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(d, dict) or "materials" not in d or "tracks" not in d:
        return None
    return d


def _score(d, uuid_hint=None):
    """候选草稿的优先级：段数多 > 素材能对上磁盘 > uuid 命中当前工程。"""
    import os
    m = d.get("materials") or {}
    vids = [v for v in (m.get("videos") or []) if isinstance(v, dict)]
    imgs = [v for v in (m.get("images") or []) if isinstance(v, dict)]
    segs = sum(len(t.get("segments") or []) for t in (d.get("tracks") or [])
               if isinstance(t, dict))
    exist = 0
    for v in vids + imgs:
        p = v.get("path")
        if p and os.path.exists(str(p)):
            exist += 1
    s = segs * 10 + exist * 40 + len(vids) + len(imgs)
    if uuid_hint and str(d.get("id", "")).lower() == str(uuid_hint).lower():
        s += 500
    return s


GOOD_TOL = 320        # 与 draftio.TOL_FIXED 同源：体积差在这以内就是同一版
GRACE_SEC = 6.0       # 最后一次命中后再多扫这么久，防止更靠后的副本被漏掉
LAST_SCAN = {}        # 上一次扫描的候选清单（调用方据此汇报「取的是哪一份」）


def _compact_size(d):
    """按剪映的紧凑序列化量明文**字节数**（要和磁盘密文载荷对账，不能用字符数：
    中文名一段就能差出几百字节）。"""
    import json
    return len(json.dumps(d, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def _structure(d):
    """段的「结构指纹」：哪条轨的哪一段、落在时间线哪里、用了源素材的哪一段。

    只比结构，不比蒙版 / 关键帧 / LUT 引用 —— 后者恰恰是「必须挑最新那份」
    的原因。结构完全一致时，几份副本只差装饰，挑错也不会丢片段。
    """
    rows = []
    for t in d.get("tracks") or []:
        if not isinstance(t, dict):
            continue
        typ = str(t.get("type") or "")
        for s in t.get("segments") or []:
            if not isinstance(s, dict):
                continue
            tg = s.get("target_timerange") or {}
            sc = s.get("source_timerange") or {}
            rows.append((typ, str(s.get("id") or ""), tg.get("start"),
                         tg.get("duration"), sc.get("start"), sc.get("duration")))
    rows.sort(key=lambda r: (r[0], r[2] or 0, r[1]))
    return rows


def extract_open_draft(pid=None, uuid_hint=None, deadline=None, target_bytes=None):
    """从正在编辑的剪映进程内存里抽出当前草稿。返回 (pid, draft_dict)。

    draft_dict 含 materials / tracks（明文），与「保存为预设」得到的是同一份。

    实现要点（都是踩过的坑）：
      1. 草稿 JSON **不保证在单个内存区块内** → read_span 跨区块连续读。
      2. 根对象特征是 `{"id":"<uuid>","version":<n>,...,"canvas_config"`，
         不能拿 canvas_config 前面最近的 `{` —— 那是 `"config":{}` 的空对象，
         解析出来 keys=0、videos=0，表现就是「读到了但没片段」。
      3. 剪映有 10 个进程，草稿只在一两个里面；全部扫一遍取分数最高的。
      4. 每一处都限时，避免窗口卡死。
      5. 同一份草稿在内存里常有 2~3 个 id 相同的副本（打开时那版 + 之后每次
         保存/回写留下的那些），分数也一样。老实现「找到第一份就 break」，
         于是经常拿到最旧的那份 —— 2026-11-14 实测：用户新加的两个蒙版
         （materials.common_mask）和一个关键帧段全在第二份副本里，第一份
         根本没有；读错副本 = 送进去的是加蒙版之前的时间线，写回还会把
         蒙版一起抹掉。现在把候选全收下来，按 target_bytes（磁盘密文载荷）
         挑「体量最接近」的那份，并把「几份结构是否一致」一并报出去。
    """
    import time
    if not uuid_hint or not AVAILABLE:
        return None, None
    t0 = time.monotonic()
    targets = [pid] if pid else pids_by_mem()
    hits = []                       # [{score, pid, draft, size, structure}]
    last_hit = 0.0

    def _enough():
        """能不能收工：有一份与磁盘体量吻合，或已经静默超过 GRACE_SEC。"""
        if not hits:
            return False
        if not target_bytes:
            # 没给目标体量（老调用路径）：沿用「找到就用」，别拖慢 station。
            return True
        for r in hits:
            if abs(r["size"] - target_bytes) <= GOOD_TOL:
                return True
        return (time.monotonic() - last_hit) > GRACE_SEC

    for p in targets:
        if deadline and time.monotonic() - t0 > deadline:
            break
        h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
                            False, p)
        if not h:
            continue
        try:
            spans = sorted(regions(h))
            for base, size in spans:
                if deadline and time.monotonic() - t0 > deadline:
                    break
                if size < 8192:
                    continue
                off = 0
                while off < size:
                    if deadline and time.monotonic() - t0 > deadline:
                        break
                    chunk = min(size - off, 8 * 1024 * 1024)
                    d = read(h, base + off, chunk)
                    if not d:
                        off += chunk
                        continue
                    i = d.find(_ROOT)
                    while i != -1:
                        head = d[i:i + 600]
                        if _VERSION in head:
                            dd = _try_root(h, spans, base + off + i)
                            # 有目标 ID 时只认 id 对得上的：宁可失败并让用户
                            # 看到提示，也不能把别的工程/复合片段当成当前工程。
                            if dd and str(dd.get("id", "")).lower() == \
                                    str(uuid_hint).lower():
                                hits.append({
                                    "score": _score(dd, uuid_hint),
                                    "pid": p,
                                    "draft": dd,
                                    "size": _compact_size(dd),
                                    "structure": _structure(dd),
                                })
                                last_hit = time.monotonic()
                        i = d.find(_ROOT, i + 1)
                    if _enough():
                        break
                    off += len(d)
                if _enough():
                    break
        finally:
            k32.CloseHandle(h)
        if _enough():
            break

    LAST_SCAN.clear()
    if not hits:
        LAST_SCAN.update({"count": 0, "candidates": [],
                          "structure_consistent": None,
                          "elapsed": round(time.monotonic() - t0, 1)})
        return None, None

    if target_bytes:
        picked = min(hits, key=lambda r: (abs(r["size"] - target_bytes),
                                          -r["score"]))
    else:
        picked = max(hits, key=lambda r: r["score"])
    st = picked["structure"]
    LAST_SCAN.update({
        "count": len(hits),
        "target_bytes": target_bytes or 0,
        "picked_size": picked["size"],
        "picked_delta": (picked["size"] - target_bytes) if target_bytes else 0,
        "elapsed": round(time.monotonic() - t0, 1),
        "structure_consistent": all(r["structure"] == st for r in hits),
        "candidates": [{
            "size": r["size"],
            "segments": len(r["structure"]),
            "delta": (r["size"] - target_bytes) if target_bytes else 0,
            "picked": r is picked,
            "same_structure": r["structure"] == st,
        } for r in hits],
    })
    return picked["pid"], picked["draft"]


def regions(h):
    out = []
    if not AVAILABLE:
        return out
    addr = 0
    mbi = MBI()
    while True:
        r = k32.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi))
        if r == 0:
            break
        base = int(mbi.BaseAddress) if mbi.BaseAddress is not None else 0
        size = int(mbi.RegionSize)
        if mbi.State == MEM_COMMIT and (mbi.Protect & 0xff) in READABLE and size > 0:
            out.append((base, size))
        addr = base + size
        if addr > 0x00007FFFFFFFFFFF or size == 0:
            break
    return out


def scan_one(pid, needles, dump_dir):
    if not AVAILABLE:
        return []
    h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return []
    hits = []
    buf = ctypes.create_string_buffer(4 * 1024 * 1024)
    total = 0
    try:
        for base, size in regions(h):
            if total > 4 * 1024 * 1024 * 1024:
                break
            off = 0
            while off < size:
                chunk = min(size - off, len(buf))
                n = ctypes.c_size_t(0)
                ok = k32.ReadProcessMemory(h, base + off, buf, chunk,
                                           ctypes.byref(n))
                if not ok or n.value == 0:
                    off += max(chunk, 4096)
                    continue
                data = buf.raw[:n.value]
                for tag, pat in needles:
                    i = data.find(pat)
                    while i != -1:
                        lo = max(0, i - 512)
                        hi = min(len(data), i + 512)
                        hits.append((pid, base + off + lo, tag, data[lo:hi]))
                        i = data.find(pat, i + 1)
                        if len(hits) >= 200:
                            return hits
                off += n.value
                total += n.value
    finally:
        k32.CloseHandle(h)
    return hits


def main():
    """开发用：把命中片段 dump 到磁盘，人工翻看。

    这不是给用户跑的入口，产物只在 %LOCALAPPDATA%\\ColorPass\\_mem 下，
    不会写进仓库。needles 也保持通用 —— 别把个人目录名写进这里。
    """
    import time

    from . import paths

    if not AVAILABLE:
        print("内存扫描只支持 Windows（Win32 进程 API）。当前平台：", sys.platform)
        return 2

    t0 = time.time()
    needles = []
    for tag, pat in (
        ("videos_utf8", b'"videos"'),
        ("canvas_utf8", b'canvas_config'),
        ("tracks_utf8", b'"tracks"'),
        ("draft_name_utf8", b'"draft_name"'),
    ):
        needles.append((tag, pat))
    for s in ("draft_content", "extra_material_refs"):
        needles.append(("utf8_" + s, s.encode("utf-8")))
        needles.append(("utf16_" + s, s.encode("utf-16-le")))

    plist = pids()
    print("剪映进程 PID:", plist)
    dump_dir = os.path.join(paths.BASE, "_mem")
    os.makedirs(dump_dir, exist_ok=True)

    allhits = []
    for pid in plist:
        try:
            hits = scan_one(pid, needles, dump_dir)
        except Exception as e:
            print("  pid", pid, "scan fail", repr(e))
            continue
        print("  pid", pid, "命中", len(hits))
        allhits += hits

    # 去重，按 pid 分文件
    print("总命中", len(allhits), "用时 %.1fs" % (time.time() - t0))
    for i, (pid, addr, tag, data) in enumerate(allhits[:400]):
        fn = os.path.join(dump_dir, "pid%d_%s_%04d.bin" % (pid, tag, i))
        with open(fn, "wb") as f:
            f.write(data)
    print("dump 到", dump_dir, "共", min(len(allhits), 400), "份")
    # 统计命中位置最多的地址段（最可能是草稿）
    from collections import Counter
    cnt = Counter((pid, tag) for pid, a, tag, d in allhits)
    print("命中分布:", cnt.most_common(12))


if __name__ == "__main__":
    main()
