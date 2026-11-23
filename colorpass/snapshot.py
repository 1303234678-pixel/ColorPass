"""送入那一刻的草稿快照 —— 写回只认这一版

为什么要有它（用户要求，也是实测后果）
--------------------------------------
「送入达芬奇」之后，用户在剪映里继续剪是很正常的：删一段、加个复合片段、
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
import uuid
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

    必须复制：那个 json 在 Windows 上是**绝对路径**，不复制就还是指向原草稿的
    `subdraft/<uuid>/`。原草稿一被删/一被改，快照里的复合片段就跟着坏。

    macOS 11.8 里它换成了 `##_draftpath_placeholder_<uuid>_##/subdraft/...`
    这种「本草稿目录」占位符 —— 相对路径，不解析前缀就永远判成「不在
    subdraft 下」，一个子草稿都复制不进来。所以路径归一走
    `draftio.subdraft_rel()`，两种形式都能认。

    占位符形式保留原样（相对新草稿解析，复制过去的子草稿原地可用）；
    绝对路径照旧改写成快照里的副本。
    """
    from . import draftio
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
        rel, placeholder = draftio.subdraft_rel(p, folder)
        if rel is None:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "why": "子草稿不在源草稿的 subdraft 下", "from": p})
            continue
        src_dir = Path(str(folder or "")) / rel.parent
        dst_dir = Path(dest) / rel.parent
        try:
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        except Exception as exc:
            out.append({"compound_id": e.get("id"), "ok": False,
                        "from": p, "why": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if placeholder:
            # 占位符是相对草稿目录的：快照和新草稿都在自己的根下留了
            # subdraft/，原样保留即可，改成绝对路径反而把它写死。
            out.append({"compound_id": e.get("id"), "ok": True,
                        "from": p, "to": p, "placeholder": True})
            continue
        e["draft_file_path"] = str(dst_dir / sp.name).replace("\\", "/")
        out.append({"compound_id": e.get("id"), "ok": True,
                    "from": p, "to": e["draft_file_path"]})
    return out


def _dir_bytes(p):
    total = 0
    for root, _dirs, files in os.walk(str(p)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# 骨架复制时永远跳过的目录：subdraft 已经由 _copy_subdrafts 单独搬过了，
# 其余是剪映自己的回滚点 / 回收站。
_SKEL_SKIP_DIRS = ("subdraft", ".backup", ".recycle_bin")
_SKEL_MAX_FILE = 6 << 20        # 单个文件超过 6MB 不像骨架的一部分
_SKEL_MAX_DIR = 24 << 20        # 单个子目录超过 24MB（素材/代理）就不搬


def _copy_skeleton(folder, dest):
    """把源草稿的**目录骨架**复制进快照（不含大素材）。

    为什么需要这份东西：写回是「整目录克隆源草稿 + 换正文」。用户在送入之后
    把原草稿删了（测试草稿、清理旧工程都很常见），写回就完全做不了了 ——
    而 `preflight.jianying_alive()` 里写的却是「甚至把原草稿删了都不影响写回」。
    骨架只有几十个 JSON 和几个空文件（实测整份 1.7MB，其中大头是封面和
    ledger），存一份很便宜，换来的是「原稿没了照样能写回」。
    """
    src = Path(str(folder or ""))
    if not src.is_dir():
        return {"ok": False, "why": "源草稿目录不存在"}
    out = Path(dest) / "skeleton"
    heavy, skipped = set(), []
    for n in sorted(os.listdir(str(src))):
        full = src / n
        if not full.is_dir() or n in _SKEL_SKIP_DIRS:
            continue
        if _dir_bytes(full) > _SKEL_MAX_DIR:
            heavy.add(n)
            skipped.append(n)

    def ignore(dirpath, names):
        out_names = []
        for n in names:
            full = Path(dirpath) / n
            if n in _SKEL_SKIP_DIRS or n in heavy:
                out_names.append(n)
                continue
            if full.is_file():
                if n in (".locked", "root_meta_info.json") or n.endswith(".bak"):
                    out_names.append(n)
                    continue
                try:
                    if full.stat().st_size > _SKEL_MAX_FILE:
                        out_names.append(n)
                        continue
                except OSError:
                    out_names.append(n)
        return out_names

    try:
        shutil.copytree(str(src), str(out), ignore=ignore, symlinks=True,
                        dirs_exist_ok=True)
    except Exception as exc:
        return {"ok": False, "why": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "dir": str(out).replace("\\", "/"),
            "bytes": _dir_bytes(out), "skipped_dirs": skipped}


def skeleton_missing_assets(draft, source_folder, skeleton_dir, skipped_dirs):
    """原草稿被删后，精简骨架里缺了哪些**仍被草稿引用**的随稿文件。

    骨架为控制体积会跳过超过上限的 Resources 等目录；跳过本身不等于不能写回
    （多数素材本来就是磁盘上的绝对路径）。真正危险的是草稿引用了
    `<原草稿>/Resources/...`，而骨架没有把它带过来。以前这种情况照样建稿并
    报成功，用户打开才发现素材离线。
    """
    src = Path(str(source_folder or ""))
    skel = Path(str(skeleton_dir or ""))
    skipped = {str(x) for x in (skipped_dirs or [])}
    found, seen = [], set()
    if not skipped or not source_folder:
        return found

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, str) and (key == "path" or
                                               str(key).endswith("_path")):
                    raw = value.split(";", 1)[0]
                    if not raw or raw.startswith("##_draftpath_placeholder_"):
                        continue
                    try:
                        rel = Path(raw).relative_to(src)
                    except (ValueError, OSError):
                        continue
                    if not rel.parts or rel.parts[0] not in skipped:
                        continue
                    target = skel / rel
                    marker = str(rel).replace("\\", "/")
                    if marker not in seen and not target.exists():
                        seen.add(marker)
                        found.append({"path": raw, "relative": marker})
                else:
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(draft)
    return found


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
    # 骨架也留一份：送入之后原草稿被删/被改名，写回还能按这一份重建目录。
    skel = _copy_skeleton(cur.get("folder"), d)

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
        "skeleton": skel,
    }
    if extra:
        meta.update(extra)
    paths.atomic_write(d / "draft_content.json",
                       json.dumps(snap, ensure_ascii=False))
    paths.atomic_write(d / "meta.json",
                       json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def _write_ptr(path, payload):
    """指针文件原子写，临时文件名按进程随机，避免两个窗口互相搬走。"""
    paths.atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1))


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
        # 每次送入都有独立编号。commit 必须拿同一个编号来认领 pending，
        # 否则两个窗口重叠送入时，先结束的会把后一次的上下文提交成自己的。
        "claim_id": uuid.uuid4().hex,
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
    # claim 与 commit 共用一把锁：提交时的「核对编号 → 写 current → 删 pending」
    # 必须是一个整体，否则另一次 claim 能插进中间，pending 会被误删。
    with paths.FileLock("snapshot-pointers"):
        _write_ptr(PENDING_PATH, payload)
    _cleanup(run_dir, keep=payload["snapshot_dir"])
    return payload


def commit(claim_id=None, **extra):
    """送入走完最后一步才调用：把**属于这次送入**的 pending 提升成 current。

    claim_id 不匹配时返回 None，且绝不动 pending/current。旧调用不传编号时
    保持原行为；正常 send 流程必须传，以防两个窗口的上下文串号。
    """
    paths.ensure(paths.BASE)
    with paths.FileLock("snapshot-pointers"):
        payload = _read_ptr(PENDING_PATH)
        if payload is None:
            return None
        if claim_id and str(payload.get("claim_id") or "") != str(claim_id):
            paths.note("snapshot: 拒绝提交另一次送入的 pending（%s ≠ %s）"
                       % (claim_id, payload.get("claim_id")))
            return None
        payload.update(extra)
        payload["committed"] = True
        payload["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
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
