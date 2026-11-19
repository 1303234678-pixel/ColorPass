"""草稿明文的两个「不靠内存」来源：剪映的载入备份 + 我们自己的缓存。

为什么需要
----------
剪映把解密后的草稿 JSON 放在进程内存里，但**不长期保留**。2026-11-20 实测：
一份 23:38 打开、一直没动过的草稿，到 00:38 再扫内存，只剩 167 KB 的残片
（有 `"tracks":[`，却没有 `"materials"`，括号根本不闭合），完整对象已被释放，
同期 `draft.extra` 在 23:58 被写过一次。也就是说——**用户什么都没改，只是把
草稿挂在那儿一小时，就又读不到了**。原来那句「内存与磁盘对不上」的解释完全
对不上这种情况。

两个替代来源（都带新鲜度校验：宁可报错，也不送错版本）：

1. 载入备份 `<草稿>/.backup/<时间戳>_<hash>.load.bak`
   剪映「打开草稿」时会把载入前的 draft_content.json 原样备份一份。普通草稿
   那份是密文（读出来是二进制，直接跳过）；**我们写回的草稿是明文写进去的**，
   所以那份备份就是明文，内容正是「打开那一刻」的版本（实测与写回时用的快照
   结构完全一致：9 轨 / 17 段 / 13 个 video 材质）。
   校验：id 必须等于当前主时间线；草稿的最后编辑时间不得晚于备份时间。

2. 缓存 `%LOCALAPPDATA%\\JYBridge\\plain\\<草稿ID>.json`
   每次成功读出明文就存一份，附磁盘指纹（密文载荷字节数 + 最后编辑时间）。
   指纹一致才用 —— 磁盘没变，内容就还是那份，不再依赖剪映留着内存副本。
   这是**唯一能长期生效**的一条：载入备份只在「我们写回后第一次打开」是明文，
   之后剪映自己保存成密文，备份也跟着变回密文。
"""
import glob
import json
import os
import time

from . import paths

CACHE_ROOT = os.path.join(paths.BASE, "plain")
KEEP_CACHE = 12          # 只留最近这么多个草稿的缓存，合计通常一两 MB
SLACK_SEC = 3.0          # 备份时间与编辑时间的允许误差（文件写入先后）
MIN_BACKUP = 32          # 再小的不可能是草稿正文（真正的校验在 _usable）

_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyz"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ-_")


def _safe_id(draft_id):
    """草稿 ID 当文件名用：只留字母数字和 `-`/`_`。

    它来自剪映的 JSON，不能直接拼路径 —— 一个带 `..` 或 `\\` 的 id 会把
    缓存写到别处去（和 retime.clean 同一个坑）。
    """
    s = "".join(ch for ch in str(draft_id or "") if ch in _CHARS)
    return s[:64]


def cache_file(draft_id):
    return os.path.join(CACHE_ROOT, _safe_id(draft_id) + ".json")


def _write_atomic(path, payload):
    """先写 .tmp 再 os.replace —— 面板关窗会 taskkill 正在跑的 cli。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fingerprint(folder):
    """磁盘侧指纹：密文载荷字节数 + 草稿最后编辑时间。

    两个都不随「换一份内存副本」而变，只随「真的存了一次盘」而变。
    """
    from . import draftio
    return {"payload": int(draftio.disk_payload_size(str(folder or "")) or 0),
            "edit_at": int(draftio.draft_edit_time(str(folder or "")) or 0)}


def save(draft, cur):
    """把刚读出来的明文存一份。返回写好的路径，失败返回空串（不影响主流程）。"""
    try:
        did = str(draft.get("id") or (cur or {}).get("timeline_id") or "")
        if not did or not isinstance(draft, dict):
            return ""
        fp = fingerprint((cur or {}).get("folder"))
        if not fp["payload"]:
            return ""
        blob = {"id": did, "fingerprint": fp,
                "saved_at": int(time.time()),
                "name": (cur or {}).get("name") or draft.get("name") or "",
                "draft": draft}
        path = cache_file(did)
        _write_atomic(path, blob)
        _prune()
        return path
    except Exception:
        return ""


def _prune(keep=KEEP_CACHE):
    try:
        files = [p for p in glob.glob(os.path.join(CACHE_ROOT, "*.json"))
                 if os.path.isfile(p)]
    except OSError:
        return
    if len(files) <= keep:
        return
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for p in files[keep:]:
        try:
            os.remove(p)
        except OSError:
            pass


def from_cache(cur, payload):
    """磁盘没变就返回上次读到的明文，否则 None。"""
    did = str((cur or {}).get("timeline_id") or "")
    if not did or not payload:
        return None
    path = cache_file(did)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    from . import draftio
    fp = blob.get("fingerprint") or {}
    if str(blob.get("id") or "").lower() != did.lower():
        return None
    if int(fp.get("payload") or 0) != int(payload):
        return None                     # 磁盘存过一次，缓存作废
    # 只取编辑时间，别再算一遍载荷（调用方刚算过，那是整份文件的 base64 解码）
    now_edit = draftio.draft_edit_time(str((cur or {}).get("folder") or ""))
    if fp.get("edit_at") and now_edit and \
            int(fp["edit_at"]) != int(now_edit):
        return None                     # 又编辑过
    draft = blob.get("draft")
    if not _usable(draft, did):
        return None
    return {"draft": draft, "source": "缓存",
            "note": "明文来自本地缓存（%s 读出的那一版，磁盘此后没变）"
                    % _stamp(blob.get("saved_at")),
            "at": blob.get("saved_at")}


def _candidates(folder, draft_id):
    out = []
    base = os.path.join(str(folder or ""), ".backup")
    if not os.path.isdir(base):
        return out
    out += glob.glob(os.path.join(base, "*.load.bak"))
    if draft_id:
        out += glob.glob(os.path.join(base, str(draft_id), "*.load.bak"))
    return [p for p in out if _size(p) >= MIN_BACKUP]


def from_backup(cur):
    """从剪映的载入备份里读明文。只有「我们写回的草稿」那份是明文。"""
    did = str((cur or {}).get("timeline_id") or "")
    folder = (cur or {}).get("folder")
    if not did or not folder:
        return None
    from . import draftio
    edit_at = draftio.draft_edit_time(str(folder))
    files = _candidates(folder, did)
    files.sort(key=_mtime, reverse=True)
    for path in files[:4]:
        mtime = _mtime(path)
        if edit_at and mtime and edit_at > mtime + SLACK_SEC:
            # 备份之后草稿又存过盘 —— 这份是编辑前的，不能用
            continue
        draft = _load(path)
        if not _usable(draft, did):
            continue
        return {"draft": draft, "source": "载入备份",
                "note": "明文来自剪映的载入备份（%s，打开草稿那一刻的版本）"
                        % _stamp(mtime),
                "at": mtime}
    return None


def best(cur, payload):
    """先缓存（指纹完全一致）再载入备份；都不成立返回 None。

    两个都是「秒级」的读文件操作，拿不到就交给内存扫描（20 秒起）——
    顺序不能反：内存扫描既慢，又恰恰是打开久了会失效的那条路。
    """
    hit = from_cache(cur, payload)
    if hit:
        return hit
    return from_backup(cur)


def _load(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw.lstrip()[:1] == b"{":
        return None                     # 密文备份，跳过
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _usable(draft, draft_id):
    """结构够不够我们用的最低标准。"""
    if not isinstance(draft, dict):
        return False
    if draft_id and str(draft.get("id") or "").lower() != str(draft_id).lower():
        return False
    if "materials" not in draft or "tracks" not in draft:
        return False
    for t in draft.get("tracks") or []:
        if isinstance(t, dict) and t.get("segments"):
            return True
    return False


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _stamp(ts):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"
