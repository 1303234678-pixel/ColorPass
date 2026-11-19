"""送入那一刻的草稿快照 —— 写回只认这一版

为什么要有它（用户要求，也是实测后果）
--------------------------------------
「① 送入达芬奇」之后，用户在剪映里继续剪是很正常的：删一段、加个复合片段、
再改改变速。这时候内存里的草稿是新版本，达芬奇里的时间线还是旧版本 ——
两边已经不是同一份剪辑了。

旧做法写回时拿「当前草稿」当底稿，于是必然错配：
  · 新草稿里少了旧段 → 2 个 segment_id 不存在，拒绝写回（实测）；
  · 更糟的情况：段 ID 还在但位置变了 → LUT 挂到别的画面上，还没人发现。

所以送入那一刻把明文草稿**整份**存下来（连同复合片段的 subdraft 目录一起
复制进快照），写回只认这份快照。剪映后来的改动不影响这次往返，也不会被
我们覆盖 —— 写回永远是「另存一份新草稿」。

快照结构
--------
<RUN_ROOT>/<草稿ID>/snap-<YYYYmmdd-HHMMSS>/
    draft_content.json     送入时的完整明文草稿（复合片段路径已改指快照内副本）
    meta.json              草稿名、目录、段数、时长、指纹、复制了哪些 subdraft
    subdraft/<uuid>/…      复合片段内部时间线的副本

每次送入新建一个版本目录（不覆盖）。指针分两步走：

  claim()  写 pending.json —— 这一次送入正在进行，**不动** current.json
  commit() 走完全程，才把 pending 提升为 current.json

这样送入中途失败时，current.json 仍是上一次成功的那一版（快照也还在盘上），
但 current() 会优先看到 pending.json 并标 `_incomplete`，写回直接拒绝 ——
既不拿旧版本冒充新送入，也不会因为一次失败把上一版的好数据删掉。

%LOCALAPPDATA%/ColorPass/current.json
    最近一次「完整走完」的送入：哪个草稿、哪条达芬奇时间线、快照在哪。
    取回与写回都从这里找上下文，不再去读剪映的内存。
%LOCALAPPDATA%/ColorPass/pending.json
    正在进行的送入。存在即表示「上一次没走完」。
"""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import paths

SNAP_DIRNAME = "snapshot"
CURRENT_PATH = os.path.join(paths.BASE, "current.json")
PENDING_PATH = os.path.join(paths.BASE, "pending.json")


def dir_of(run_dir):
    return os.path.join(str(run_dir), SNAP_DIRNAME)


def _fingerprint(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _video_segments(draft):
    n = 0
    for tr in draft.get("tracks") or []:
        if isinstance(tr, dict) and tr.get("type") == "video":
            n += len(tr.get("segments") or [])
    return n


def _copy_subdrafts(snap, folder, dest):
    """把复合片段的子草稿复制进快照，并把 draft_file_path 改指副本。

    必须复制：那个 json 是**绝对路径**，不复制就还是指向原草稿的
    `subdraft/<uuid>/`。原草稿一被删/一被改，快照里的复合片段就跟着坏。
    """
    src = Path(str(folder or "")) / "subdraft"
    out = []
    if not src.is_dir():
        return out
    for e in (snap.get("materials") or {}).get("drafts") or []:
        if not isinstance(e, dict):
            continue
        p = str(e.get("draft_file_path") or "")
        if not p:
            continue
        sp = Path(p)
        try:
            rel = sp.parent.relative_to(src)
        except ValueError:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "why": "子草稿不在源草稿的 subdraft 下", "from": p})
            continue
        dst_dir = Path(dest) / "subdraft" / rel
        try:
            shutil.copytree(sp.parent, dst_dir, dirs_exist_ok=True)
        except Exception as exc:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "from": p, "why": "%s: %s" % (type(exc).__name__, exc)})
            continue
        e["draft_file_path"] = str(dst_dir / sp.name).replace("\\", "/")
        out.append({"compound_id": e.get("id"), "ok": True,
                    "from": p, "to": e["draft_file_path"]})
    return out


def save(draft, cur, run_dir, extra=None):
    """存一份送入时的草稿，目录按时间戳新建：`snap-YYYYmmdd-HHMMSS`。

    为什么不覆盖同一个目录：送入有可能中途失败（摆到一半、预渲染失败）。
    如果失败也把旧快照盖了，current.json 还指着「上一次成功的那条时间线」，
    两边就不是同一版了 —— 写回会拿新草稿配旧时间线。所以每次都新建目录，
    指针等到 commit() 才挪；失败的那份留着也不影响任何东西，下次送入时顺手清掉。
    """
    snap = json.loads(json.dumps(draft))          # 深拷贝，别动调用方的对象
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = Path(run_dir) / ("snap-%s" % stamp)
    n = 2
    while d.exists():                             # 同一秒内重复送入
        d = Path(run_dir) / ("snap-%s_%d" % (stamp, n))
        n += 1
    d.mkdir(parents=True, exist_ok=True)
    subs = _copy_subdrafts(snap, cur.get("folder"), d)

    meta = {
        "draft_id": snap.get("id"),
        "draft_name": cur.get("name"),
        "folder": cur.get("folder"),
        "saved_at": time.time(),
        "saved_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_us": snap.get("duration"),
        "video_segments": _video_segments(snap),
        "tracks": len(snap.get("tracks") or []),
        "compounds": len((snap.get("materials") or {}).get("drafts") or []),
        "sha": _fingerprint(snap),
        "dir": str(d).replace("\\", "/"),
        "subdrafts": subs,
    }
    if extra:
        meta.update(extra)
    (d / "draft_content.json").write_text(
        json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def _write_ptr(path, payload):
    """指针文件原子写：先写 .tmp 再 os.replace。

    为什么不能直接 write_text：面板关窗口时会 taskkill 正在跑的 cli，
    正好落在写指针的那几毫秒就会留下半截 JSON；下次 _read_ptr 读不出来，
    等于「没有未完成的送入」—— 写回会拿**上一次**的旧快照当底，还理直气壮。
    os.replace 在 NTFS 上是原子的，替换前旧文件始终完整。
    """
    path = str(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_ptr(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        # 读到坏文件不能静默 —— 它是「指针损坏」，不是「没有指针」。
        # 修不了就当没有（调用方按无指针处理），但必须在日志里留下证据，
        # 否则下次出现「写回用了旧版本」根本查不到原因。
        try:
            paths.note("snapshot: 指针文件读不出来 %s（%r）" % (p, exc))
        except Exception:
            pass
        return None
    return payload if isinstance(payload, dict) else None


def _cleanup(run_dir, keep):
    """删掉这个草稿目录下用不上的快照版本。

    保留：刚建的这一份（keep）+ 指针还在用的那一份（current/pending）。
    两处都读文件、不调 current()，否则 claim 之后 current() 会返回 pending，
    把「上一次成功」的那份误删掉。
    """
    refs = set()
    for payload in (_read_ptr(CURRENT_PATH), _read_ptr(PENDING_PATH)):
        if not payload:
            continue
        if str(payload.get("draft_id")) != Path(run_dir).name:
            continue
        d = str(payload.get("snapshot_dir") or "")
        if d:
            refs.add(d.replace("\\", "/").lower())
    keep = str(keep).replace("\\", "/").lower()
    try:
        for e in Path(run_dir).iterdir():
            if not e.is_dir():
                continue
            if not e.name.startswith("snap-"):
                continue
            p = str(e).replace("\\", "/").lower()
            if p == keep or p in refs:
                continue
            shutil.rmtree(e, ignore_errors=True)
    except Exception:
        pass


def load(path):
    """读回 (快照草稿, meta)。path 是快照目录（current.json 的 snapshot_dir）。"""
    d = Path(str(path))
    f = d / "draft_content.json"
    if not f.exists():
        raise FileNotFoundError("没有送入时的快照：%s" % f)
    draft = json.loads(f.read_text(encoding="utf-8"))
    meta = {}
    mf = d / "meta.json"
    if mf.exists():
        try:
            meta = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    return draft, meta


def claim(meta, run_dir, timeline, start_frame, segments):
    """送入过程中占位：记下这一次的上下文，写 pending.json，**不动** current.json。

    为什么分两步：pull/write 都是按指针找上下文的。如果送入中途失败（比如摆到
    一半达芬奇报错），一条没搭完的时间线被当成「最近一次送入」会让写回导出一份
    残缺的 LUT 表。所以 claim 只写 pending，走完全程才 commit 提升为 current。

    另一个原因：原来 claim 直接覆盖 current.json，一旦之后失败，上一次成功的
    那份上下文就没了（连带快照被 cleanup 删掉）。现在失败只留下 pending，
    current 与它的快照原地不动 —— 写回会明确拒绝，但历史数据不丢。
    """
    payload = {
        "run_dir": str(run_dir),
        "snapshot_dir": meta.get("dir") or dir_of(run_dir),
        "draft_id": meta.get("draft_id"),
        "draft_name": meta.get("draft_name"),
        "snapshot_sha": meta.get("sha"),
        "snapshot_at": meta.get("saved_at_text"),
        "video_segments": meta.get("video_segments"),
        "timeline": timeline,
        "start_frame": start_frame,
        "segments": segments,
        "committed": False,
        "claimed_at": time.time(),
    }
    paths.ensure(paths.BASE)
    _write_ptr(PENDING_PATH, payload)
    _cleanup(run_dir, keep=payload["snapshot_dir"])
    return payload


def commit(**extra):
    """送入走完最后一步才调用：把 pending 提升成 current。"""
    payload = _read_ptr(PENDING_PATH)
    if payload is None:
        return None
    payload.update(extra)
    payload["committed"] = True
    payload["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    paths.ensure(paths.BASE)
    _write_ptr(CURRENT_PATH, payload)
    try:
        os.remove(PENDING_PATH)
    except OSError:
        pass
    run_dir = payload.get("run_dir")
    if run_dir and payload.get("snapshot_dir"):
        _cleanup(run_dir, keep=payload["snapshot_dir"])
    return payload


def current():
    """最近一次「完整走完」的送入。

    pending 优先：它代表「用户刚做了一次送入但没成功」。这时候不能退回上一次
    成功的那份 —— 用户以为送的是新版本，写回却给出旧版本，比拒绝更糟。
    """
    pending = _read_ptr(PENDING_PATH)
    if pending:
        pending["_incomplete"] = True
        ts = pending.get("claimed_at")
        if ts:
            pending["pending_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(float(ts)))
        return pending
    payload = _read_ptr(CURRENT_PATH)
    if payload is None:
        return None
    if not payload.get("committed"):
        payload["_incomplete"] = True
    return payload


def clear_pending():
    """丢掉「没走完的那次送入」记录，让写回退回到上一次成功的版本。

    为什么需要这个：送入中途被打断（关达芬奇、强杀进程）会留下 pending。
    按设计写回会一直拒绝 —— 这是对的，但不该变成死角：用户明确知道自己在
    做什么时，要能自己回到上一版。
    """
    p = Path(PENDING_PATH)
    if not p.exists():
        return {"ok": True, "cleared": False}
    try:
        os.remove(p)
        return {"ok": True, "cleared": True}
    except OSError as exc:
        return {"ok": False, "cleared": False, "error": str(exc)}


def snapshot_of(payload):
    """把 current.json 变成 (快照草稿, 快照 meta)。缺文件就抛。"""
    run_dir = payload.get("run_dir")
    d = payload.get("snapshot_dir") or (dir_of(run_dir) if run_dir else "")
    if not d:
        raise FileNotFoundError("current.json 里没有 snapshot_dir / run_dir")
    return load(d)


def diff(a, b):
    """比两份草稿的剪辑差异，只报「影响 LUT 挂位」的那几类。

    写回以快照为准，但仍要把「你之后在剪映里改了什么」如实报出来 ——
    否则用户会以为写回的草稿包含他最新的剪辑。
    """
    def index(d):
        out = {}
        for tr in d.get("tracks") or []:
            if not isinstance(tr, dict) or tr.get("type") != "video":
                continue
            for sg in tr.get("segments") or []:
                if isinstance(sg, dict) and sg.get("id"):
                    out[sg["id"]] = {
                        "target": (sg.get("target_timerange") or {}).get("start"),
                        "dur": (sg.get("target_timerange") or {}).get("duration"),
                        "src": (sg.get("source_timerange") or {}).get("start"),
                        "refs": sorted(sg.get("extra_material_refs") or []),
                    }
        return out

    ia, ib = index(a), index(b)
    added = [k for k in ib if k not in ia]
    removed = [k for k in ia if k not in ib]
    changed = [k for k in ia if k in ib and ia[k] != ib[k]]
    return {
        "segments_sent": len(ia),
        "segments_now": len(ib),
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
        "same": not (added or removed or changed),
        "added_ids": added[:5], "removed_ids": removed[:5],
        "changed_ids": changed[:5],
    }
