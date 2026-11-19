"""JYBridge · 缓存回收

为什么会越用越大
----------------
每送一次草稿都会在 `%LOCALAPPDATA%\\JYBridge\\` 下留一份痕迹，而且**按草稿 ID
分目录**，剪映里删掉旧草稿也不会有人来收：

  luts/<草稿ID>/     取回时导出的 cube 工作副本，一份草稿约 12 MB
  retime/<草稿ID>/   变速段 ffmpeg 预渲染的 ProRes，一份草稿 30~60 MB
  runs/<草稿ID>/     报告 + 写回用的底稿快照 —— **不参与回收**，见 POOLS

一条片子来回三四版，几百 MB 是很正常的事。luts 与 retime 都是**可再生的
中间产物**：cube 在写回时已经复制进新草稿的 `Resources/JYBridge/`，retime
素材随时能重编。runs 里的快照删了就真的没了（写回只认它），所以单独排除。

回收策略（四道闸，从硬到软）
----------------------------
1. **活动草稿永不自动动** —— current.json / pending.json 指着的 draft_id 全程保护。
   写回还要用它的快照和 cube。
2. **孤儿草稿** —— runs/<id> 里记的源草稿名在剪映草稿目录里已经不存在了，
   这份缓存就没有再来一次的可能，直接收。
3. **太旧 / 太多** —— 超过 keep_days 没动过，或同池只留最近 keep_last 份。
4. **总量兜底** —— 前三条过完还超过 max_mb，就从最老的开始继续收。

默认 `run()` 只算不删（apply=False），`--apply` 才真动手。send/write 成功后
会调 `auto()` 静默跑一次，用的是更宽松的阈值（只在明显堆积时才收），
失败也不影响往返 —— 清不掉缓存不该让一次成功的送入变成失败。
"""
import json
import os
import shutil
import time
from pathlib import Path

from . import paths

# 可以**整目录回收**的缓存池。
#
# runs 故意不在这里。它的目录结构是：
#     runs/<草稿ID>/snap-YYYYmmdd-HHMMSS/draft_content.json + subdraft/
# 也就是写回唯一的底稿（连同复合片段的子草稿副本）。删掉它，那份草稿就再也
# 写不回去 —— 只能在剪映里重新送入一次，而原草稿可能已经删了。它不是「可再
# 生的中间产物」，之前把它列进来是因为文档只写了「报告」，看漏了快照。
# 体积上也没必要：同一草稿的旧快照每次送入都会被 _cleanup 清掉，一个草稿只
# 占几百 KB；真正的大头一直是 retime（30~60 MB/份）和 luts（约 12 MB/份）。
POOLS = ("luts", "retime")
# 已移除功能留下的老目录，整目录回收（不属于任何草稿）
LEGACY = ("castfix", "refmatch")

DEFAULT_KEEP_LAST = 5
DEFAULT_KEEP_DAYS = 30
DEFAULT_MAX_MB = 600

# 往返成功后自动回收用的阈值：比手动宽松，避免刚做完就被清掉要重算的东西
AUTO_KEEP_LAST = 8
AUTO_KEEP_DAYS = 60
AUTO_MAX_MB = 1200


def _dir_size(p):
    """目录（或单文件）占用字节。算不出来就当 0 —— 统计不该让回收失败。"""
    p = Path(p)
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for base, _dirs, names in os.walk(p):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total


def _mtime(p):
    """目录里最新文件的时间。目录自身的 mtime 只在增删文件时变，
    往里覆盖写不变，拿它判「多久没动过」会误判成很旧。"""
    latest = 0.0
    for base, _dirs, names in os.walk(p):
        for n in names:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(base, n)))
            except OSError:
                pass
    if not latest:
        try:
            latest = os.path.getmtime(p)
        except OSError:
            latest = 0.0
    return latest


def _read_ptr(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def protected_ids(base=None):
    """当前「还在用」的草稿 ID：current.json 和 pending.json 指着的。

    pending 也要保护 —— 送入中途失败时 current 还指着上一次，pending 里那份
    cube 是新导出的，清掉就得重新取回一次。
    """
    base = Path(base or paths.BASE)
    ids = set()
    for name in ("current.json", "pending.json"):
        p = _read_ptr(base / name)
        did = p.get("draft_id")
        if did:
            ids.add(str(did))
    return ids


def _draft_root():
    """剪映草稿根目录。拿不到返回 None（这时不做孤儿判定）。"""
    try:
        from . import draftio
        return Path(draftio.WF.JY.draft_root())
    except Exception:
        return None


def _draft_names(base, draft_id):
    """runs/<id> 的报告里记下的源草稿名（send.json 的 draft 等）。"""
    names = set()
    d = Path(base) / "runs" / str(draft_id)
    if not d.is_dir():
        return names
    for f in d.glob("*.json"):
        o = _read_ptr(f)
        for k in ("draft", "draft_name", "name"):
            v = o.get(k)
            if isinstance(v, str) and v.strip():
                names.add(v.strip())
    return names


def _is_orphan(base, draft_id, root):
    """源草稿在剪映里还在不在。

    读不到草稿名（runs 被手动清过、或是很老的目录）时返回 False —— 不能因为
    「没证据」就当成孤儿删掉，那属于猜测。
    """
    if root is None or not Path(root).is_dir():
        return False
    names = _draft_names(base, draft_id)
    if not names:
        return False
    return not any((Path(root) / n).exists() for n in names)


def total_bytes(base=None):
    """整个缓存目录现在多大。只 stat 不读内容，面板每次刷新都能调。"""
    base = Path(base or paths.BASE)
    return _dir_size(base) if base.is_dir() else 0


def reclaimable(base=None, draft_root=None):
    """面板那一行「可清理：X」的唯一来源。

    这里的数字**必须等于按默认策略真删会释放的量**。以前面板显示的是整个
    缓存目录占用（含 runs 里写回要用的底稿快照、当前草稿的 cube），而默认
    策略一项都收不掉 —— 用户点完按钮看到同样的数字，只能认为按钮坏了。

    分两笔：
      free_bytes  默认策略（几份/几天/总量上限）会收掉的
      extra_bytes 这些之外、还能清的「可再生中间产物」（retime/luts），
                  要显式加 --intermediate 才动，runs 永远不算在里面
    """
    base = Path(base or paths.BASE)
    empty = {"ok": True, "base": str(base), "total_bytes": 0,
             "free_bytes": 0, "free_count": 0,
             "extra_bytes": 0, "extra_count": 0, "pools": {}}
    if not base.is_dir():
        return empty
    root = draft_root if draft_root is not None else _draft_root()
    total = _dir_size(base)
    items = plan(base=base, draft_root=root, total=total)
    inter = plan(base=base, draft_root=root, intermediate=True, total=total)
    seen = {(i["pool"], i["name"]) for i in items}
    extra = [i for i in inter if (i["pool"], i["name"]) not in seen]
    pools = {}
    for it in items + extra:
        p = pools.setdefault(it["pool"], {"bytes": 0, "count": 0})
        p["bytes"] += it["bytes"]
        p["count"] += 1
    return {"ok": True, "base": str(base), "total_bytes": total,
            "free_bytes": sum(i["bytes"] for i in items),
            "free_count": len(items),
            "extra_bytes": sum(i["bytes"] for i in extra),
            "extra_count": len(extra),
            "pools": pools}


def scan(base=None, draft_root=None):
    """只读盘点：每个池里有哪些草稿目录、多大、多久没动。"""
    base = Path(base or paths.BASE)
    root = draft_root if draft_root is not None else _draft_root()
    prot = protected_ids(base)
    pools = {}
    for pool in POOLS + LEGACY:
        p = base / pool
        if not p.is_dir():
            continue
        rows = []
        for d in sorted(p.iterdir()):
            if not d.is_dir():
                continue
            mt = _mtime(d)
            rows.append({
                "name": d.name,
                "bytes": _dir_size(d),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))
                if mt else "",
                "age_days": round((time.time() - mt) / 86400, 1) if mt else None,
                "protected": d.name in prot,
                "orphan": _is_orphan(base, d.name, root) if pool in POOLS else False,
            })
        rows.sort(key=lambda r: r["age_days"] if r["age_days"] is not None else 1e9)
        pools[pool] = {"bytes": sum(r["bytes"] for r in rows),
                       "count": len(rows), "drafts": rows}
    return {"ok": True, "base": str(base), "bytes": _dir_size(base),
            "protected": sorted(prot), "pools": pools}


def plan(keep_last=DEFAULT_KEEP_LAST, keep_days=DEFAULT_KEEP_DAYS,
         max_mb=DEFAULT_MAX_MB, now=None, base=None, draft_root=None,
         intermediate=False, total=None):
    """算出这一次要回收哪些目录。不碰磁盘。

    intermediate=True：不按「几份/几天」算，直接把**可再生的中间产物**
    （retime 变速预渲染、luts 里的 cube 工作副本）全部收掉，跳过活动草稿。
    runs 一律不动 —— 里面是报告和写回要用的底稿快照，删了那份草稿就再也
    写不回去（只能重新送入一次）。这个是给「我现在就要腾空间」用的。
    """
    now = time.time() if now is None else now
    base = Path(base or paths.BASE)
    root = draft_root if draft_root is not None else _draft_root()
    prot = protected_ids(base)

    items, seen = [], set()
    spare = []          # 没被前三条选中、但配额不够时可以继续收的候选

    def add(pool, d, reason, size, mt):
        key = (pool, Path(d).name)
        if key in seen:
            return
        seen.add(key)
        items.append({"pool": pool, "name": Path(d).name, "path": str(d),
                      "bytes": size, "reason": reason,
                      "mtime": time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(mt)) if mt else ""})

    for name in LEGACY:
        p = base / name
        if p.is_dir():
            add("legacy", p, "已移除功能的残留目录", _dir_size(p), _mtime(p))

    if intermediate:
        for pool in ("retime", "luts"):
            p = base / pool
            if not p.is_dir():
                continue
            for d in p.iterdir():
                if not d.is_dir() or d.name in prot:
                    continue
                add(pool, d, "可再生中间产物（下次送入自动重生成）",
                    _dir_size(d), _mtime(d))
        return items

    for pool in POOLS:
        p = base / pool
        if not p.is_dir():
            continue
        entries = []
        for d in p.iterdir():
            if not d.is_dir() or d.name in prot:
                continue
            entries.append((d, _mtime(d), _dir_size(d)))
        entries.sort(key=lambda t: t[1])          # 老 → 新
        n = len(entries)
        for i, (d, mt, sz) in enumerate(entries):
            age = (now - mt) / 86400 if mt else 0
            if _is_orphan(base, d.name, root):
                add(pool, d, "剪映里已经没有这份草稿", sz, mt)
            elif age > keep_days:
                add(pool, d, "超过 %d 天没动过" % keep_days, sz, mt)
            elif n - i > keep_last:
                add(pool, d, "同池只保留最近 %d 份" % keep_last, sz, mt)
            else:
                spare.append((pool, d, mt, sz))

    # 总量兜底：前三条过完仍然超过上限，从最老的继续收
    # total 由调用方传入时不重复遍历（status 一次刷新要算两遍 plan）。
    left = (total if total is not None else _dir_size(base)) \
        - sum(it["bytes"] for it in items)
    limit = int(max_mb) * 1024 * 1024
    if left > limit:
        for pool, d, mt, sz in sorted(spare, key=lambda t: t[2]):
            if left <= limit:
                break
            add(pool, d, "缓存总量超过 %d MB，回收最老的" % max_mb, sz, mt)
            left -= sz

    return items


def run(apply=False, keep_last=DEFAULT_KEEP_LAST, keep_days=DEFAULT_KEEP_DAYS,
        max_mb=DEFAULT_MAX_MB, base=None, draft_root=None, intermediate=False):
    """执行回收。apply=False 只算账（默认），True 才真删。

    删目录失败（文件被达芬奇/剪映占着）不中断，记在 errors 里接着删下一个。
    """
    base = Path(base or paths.BASE)
    before = _dir_size(base) if base.is_dir() else 0
    items = plan(keep_last=keep_last, keep_days=keep_days, max_mb=max_mb,
                 base=base, draft_root=draft_root, intermediate=intermediate)
    freed, errors = 0, []
    if apply:
        for it in items:
            p = Path(it["path"])
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                elif p.exists():
                    p.unlink()
                freed += it["bytes"]
            except Exception as e:
                errors.append({"path": str(p), "error": str(e)})
    after = _dir_size(base) if base.is_dir() else 0
    # 「这次到底能腾出多少」和「腾完之后还剩多少还在用」是两件事，面板和
    # 文字报告都要分开说 —— 否则用户看到 81.9 MB 会以为按钮没生效。
    planned = sum(it["bytes"] for it in items)
    # 部分成功（几百 MB 删掉了、个别文件被达芬奇占着）不能判成「整条命令失败」：
    # 面板会据此弹「清理失败 + 重试」，用户再点一次只会得到「没有可回收的」。
    rep = {"ok": (not errors) or freed > 0, "partial": bool(errors),
           "applied": bool(apply), "base": str(base),
           "before_bytes": before, "after_bytes": after,
           "freed_bytes": freed, "count": len(items), "items": items,
           "free_bytes": planned,
           "kept_bytes": max(0, before - (freed if apply else planned)),
           "intermediate": bool(intermediate)}
    if errors:
        rep["error"] = ("%d 个目录没删掉（可能正被达芬奇/播放器占用），其余已清理"
                        % len(errors))
        rep["errors"] = errors
    if not items:
        # 没有可回收的必须说清楚为什么 —— 面板上「点了没反应」和「确实没东西可清」
        # 是两回事，用户分不出来就会一直点。
        rep["note"] = ("没有可自动回收的缓存：%s 都还在用（当前草稿的 cube、"
                       "写回要用的底稿快照、最近 %d 份仍在剪映里的草稿）"
                       % (human(before), keep_last))
        if not intermediate:
            alt = plan(base=base, draft_root=draft_root, intermediate=True)
            if alt:
                rep["intermediate_bytes"] = sum(i["bytes"] for i in alt)
                rep["intermediate_count"] = len(alt)
    return rep


def auto(base=None, draft_root=None):
    """往返成功后静默回收一次。任何异常都吞掉，只写日志。"""
    try:
        rep = run(apply=True, keep_last=AUTO_KEEP_LAST,
                  keep_days=AUTO_KEEP_DAYS, max_mb=AUTO_MAX_MB,
                  base=base, draft_root=draft_root)
        if rep.get("count"):
            paths.note("gc auto: 回收 %d 项，%d → %d B"
                       % (rep["count"], rep["before_bytes"], rep["after_bytes"]))
        return rep
    except Exception as e:
        paths.note("gc auto failed: %r" % (e,))
        return {"ok": False, "error": str(e), "count": 0}


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024
    return "%.1f GB" % n
