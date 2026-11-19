"""剪映工程闭环：同步片段 → 调色 → 写回一条调色草稿。

数据通道（全部实测过，不靠猜）：
  读   剪映的草稿是密文，但「右键 → 保存为预设」会把选中片段写成明文，
       里面带素材路径、入出点、时间线位置。凌萌盒转 PR 用的就是这条路。
  写   剪映接受**明文** draft_content.json（实测：打开后它自己重写成密文）。
       但复合片段会按 `draft_file_path` 去加载原件，所以必须写**扁平**结构：
       每段视频一条 video 材质 + 一条 effects(LUT) 材质。
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import draftbuild as JD
from . import preset as JP
from . import probe as JY

TPL = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------- 素材参数
_FFMPEG = None


def _ffmpeg():
    global _FFMPEG
    if _FFMPEG is None:
        try:
            import imageio_ffmpeg
            _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            _FFMPEG = "ffmpeg"
    return _FFMPEG


def probe(path):
    """读素材的分辨率、帧率、时长。失败给默认值，不阻塞流程。"""
    info = {"width": 1920, "height": 1080, "fps": 25.0, "duration": 0.0}
    try:
        out = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True,
                             timeout=20,
                             creationflags=getattr(subprocess,
                                                   "CREATE_NO_WINDOW", 0)).stderr
        out = out.decode("gbk", errors="replace")
    except Exception:
        return info
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    v = re.search(r"Video:.*? (\d{2,5})x(\d{2,5})", out)
    if v:
        info["width"], info["height"] = int(v[1]), int(v[2])
    f = re.search(r"([\d.]+) fps", out)
    if f:
        info["fps"] = float(f[1])
    return info


# ---------------------------------------------------------------- 同步
def _origin_of(preset_file):
    """这份预设是从哪个工程里存出来的。

    复合片段材质上有 draft_file_path，形如
    `<工程目录>/subdraft/<uuid>/draft_content.json` —— 取 /subdraft/ 前面的部分。
    """
    d = JP._load(preset_file)
    if not d:
        return ""
    for x in (d.get("materials", {}).get("drafts") or []):
        p = str(x.get("draft_file_path") or "").replace("\\", "/")
        if "/subdraft/" in p:
            return p.split("/subdraft/")[0]
    return ""


def _normalize(raw_clips):
    clips, seen = [], 0
    for c in raw_clips:
        p = c["path"]
        if not p or "_presetpath_placeholder_" in p:
            continue
        if not Path(p).exists():
            continue
        seen += 1
        clips.append({
            "index": seen,
            "segment_id": c.get("segment_id"),
            "track_index": c.get("track_index", 0),
            "segment_index": c.get("segment_index", seen - 1),
            "path": p,
            "name": c["name"] or Path(p).stem,
            "src_start": c["src_start"],
            "src_dur": c["src_dur"],
            "tl_start": c["tl_start"],
            "tl_dur": c["tl_dur"],
        })
    clips.sort(key=lambda x: (x["tl_start"], x["index"]))
    for i, c in enumerate(clips, 1):
        c["index"] = i
    return clips


def current_draft_uuid():
    """剪映桥确认的当前唯一时间线 UUID；不根据文件时间推测。"""
    try:
        snap = JY.snapshot()
    except Exception:
        return None
    return (snap.get("draft") or {}).get("timeline_id")


def sync_mem(deadline=70, expected=None, target_bytes=None):
    """只读已识别窗口的进程，同步前后校验工程身份。

    target_bytes：磁盘上这份草稿的密文载荷字节数。内存里同一 ID 常有 2~3 个
    历史副本，传了它才能挑出「和磁盘同一版」的那份（见 jy_memscan 说明 5）。
    """
    from . import memscan as MS
    try:
        snap = JY.snapshot()
    except Exception:
        return None
    cur = snap.get("draft") or {}
    uuid_hint = cur.get("timeline_id")
    if (not snap.get("running") or not cur or not uuid_hint or not cur.get("pid")
            or (expected is not None and not JY.same_draft(expected, cur))):
        return None
    try:
        pid, draft = MS.extract_open_draft(pid=cur["pid"], uuid_hint=uuid_hint,
                                          deadline=deadline,
                                          target_bytes=target_bytes)
    except Exception:
        return None
    scan = dict(getattr(MS, "LAST_SCAN", {}) or {})
    if (pid != cur["pid"] or not draft
            or str(draft.get("id", "")).lower() != str(uuid_hint).lower()):
        return None
    try:
        after = JY.snapshot()
    except Exception:
        return None
    if not after.get("running") or not JY.same_draft(cur, after.get("draft")):
        return None
    raw = JP._clips_of(draft, "内存直读")
    clips = _normalize(raw)
    if not clips:
        return None
    return {"project": cur["name"], "identity": cur,
            "source": "内存直读", "clips": clips, "draft": draft,
            "mem_candidates": scan,
            "note": "内存直读（pid %s，%d 段，时长 %.1fs）"
                    % (pid, len(clips), (draft.get("duration") or 0) / 1e6)}


def sync(expected=None, target_bytes=None):
    """点击时重新识别当前工程；可校验调用方指定的完整工程身份。"""
    try:
        snap = JY.snapshot()
    except Exception as exc:
        snap = {"running": False, "draft": None,
                "draft_error": "读取剪映状态失败：%s" % exc}
    cur = snap.get("draft") or {}
    name = cur.get("name")
    if not snap.get("running"):
        error = "剪映未运行，请先启动剪映并打开目标草稿。"
    elif not cur:
        error = snap.get("draft_error") or \
            "无法唯一识别当前打开的剪映草稿，请确认草稿已打开且当前工程唯一。"
    elif expected is not None and not JY.same_draft(expected, cur):
        error = "匹配前当前工程发生变化，请重新匹配。"
    else:
        m = sync_mem(expected=cur, target_bytes=target_bytes)
        if m and JY.same_draft(cur, m.get("identity")) and \
                str((m.get("draft") or {}).get("id", "")).lower() == \
                str(cur.get("timeline_id", "")).lower():
            return m
        error = ("已识别剪映当前草稿「%s」，但无法在剪映进程内存中找到 ID 完全匹配的时间线，"
                 "或读取期间当前草稿发生变化。为避免同步到其他草稿，已停止匹配。" % name)
    return {"project": name, "source": "", "clips": [], "note": error,
            "error": error}


def sync_folder(folder, name=None, deadline=70):
    """兼容旧调用签名，但只同步剪映当前识别到的草稿；拒绝目录绕过。"""
    try:
        cur = (JY.snapshot().get("draft") or {})
    except Exception:
        cur = {}
    requested = os.path.normcase(os.path.normpath(str(folder)))
    current = os.path.normcase(os.path.normpath(str(cur.get("folder") or "")))
    if not cur or requested != current:
        disp = name or Path(folder).name
        error = ("拒绝同步草稿「%s」：该目录不是剪映当前唯一识别的打开工程。"
                 "请在剪映中打开目标草稿后使用同步。" % disp)
        return {"project": disp, "source": "", "clips": [], "note": error,
                "error": error}
    m = sync_mem(deadline=deadline, expected=cur)
    if m:
        return m
    error = ("已识别剪映当前草稿「%s」，但无法在剪映进程内存中找到 ID 完全匹配的时间线。"
             "为避免同步到其他草稿，已停止匹配。" % (name or cur.get("name")))
    return {"project": name or cur.get("name"), "source": "", "clips": [],
            "note": error, "error": error}


# ---------------------------------------------------------------- 写回
_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name, limit=64):
    """把用户输入的草稿名清成 Windows 目录名合法的形式。

    剪映的草稿名就是目录名，所以 `\\ / : * ? " < > |` 一个都不能留；
    结尾的点和空格会被资源管理器吞掉，也去掉；长度留 64 个字符
    （目录名上限 255，但加上 "_调色_N" 和中文路径就危险了）。
    """
    s = _BAD_NAME.sub(" ", str(name or "")).strip().strip(".")
    s = re.sub(r"\s{2,}", " ", s)
    return s[:limit].strip()


def _unique_name(base):
    root = JY.draft_root()
    if not (root / base).exists():
        return base
    for i in range(2, 40):
        n = "%s_%d" % (base, i)
        if not (root / n).exists():
            return n
    return "%s_%s" % (base, time.strftime("%H%M%S"))


def _key(p):
    return os.path.normcase(os.path.normpath(str(p)))


def write_back(clips, luts, project=None, cover_png=None, draft=None, *,
               segment_luts=None, name=None):
    """把调好的结果写回一条新的剪映草稿（不覆盖原工程）。

    clips : sync() 给的片段清单（只用于统计/兜底）
    luts  : {path: cube 路径}，旧接口，作用于该路径的所有片段。
    segment_luts : {源 segment_id: cube 路径}，命中时优先；未命中只回退到
                   显式传入的 luts。精确写回时传 luts={}，避免影响同源其他段。
    draft : sync() 带回的**完整明文草稿**。有它就走克隆：时间线、轨道、
            变速、裁切、画布、fps 全部照抄，只给对应片段补一条 LUT。
            没有它（比如直接选文件夹分析）才退回重建，那种情况本来也没有
            原工程可以对齐。
    """
    lut_map = {_key(k): v for k, v in (luts or {}).items()}
    want = safe_name(name)
    if want:
        # 用户指定了名字：就用它（同名自动加 _2），不再套 _调色_N 规则
        name = _unique_name(want)
    else:
        base = (project or "PB").strip()
        while base.endswith("_调色"):      # 别生成 xxx_调色_调色
            base = base[:-3]
        base = "%s_调色" % base
        name = _unique_name(base)

    if draft:
        r = JD.build_from_draft(draft, name, lut_map, cover_png=cover_png,
                                segment_luts=segment_luts)
        if r.get("ok"):
            r["name"] = name
            r["luts"] = JD.count_luts(draft, lut_map, segment_luts=segment_luts)
            r["clips"] = len(clips)
            tr = [t for t in (draft.get("tracks") or []) if isinstance(t, dict)]
            r["tracks"] = len(tr)
            r["segments"] = sum(len(t.get("segments") or []) for t in tr)
            r["mode"] = "clone"
        return r

    items = []
    for c in clips:
        path = c["path"]
        info = probe(path)
        dur = c["tl_dur"] or c["src_dur"] or info["duration"] or 5.0
        if dur <= 0:
            dur = 5.0
        items.append({
            "path": path,
            "name": c["name"],
            "dur": dur,
            "src_dur": info["duration"] or dur,
            "src_start": c["src_start"],
            "tl_start": c["tl_start"],
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "lut": (segment_luts or {}).get(c.get("segment_id"),
                                            lut_map.get(_key(path))),
        })

    r = JD.build_flat(TPL, name, items, cover_png=cover_png)
    if r.get("ok"):
        r["name"] = name
        r["luts"] = sum(1 for i in items if i.get("lut"))
    return r


def validate(folder):
    """写完自检：引用能否解析、素材和 LUT 是否都在盘上。

    以前这里有三个硬取键（sg["extra_material_refs"]、m["videos"]、
    m["effects"]）：剪映草稿里音频/文字段的 segment 根本没有
    extra_material_refs，一遇到就 KeyError。这个异常发生在写回线程里，
    直接把线程打死，界面永远停在"写回剪映…"—— 表现就是"写回很久都不成功"。
    现在全部改成容错读取，并且任何异常都返回结构化的失败，不再抛。
    """
    try:
        p = Path(folder) / "draft_content.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        m = d.get("materials") or {}
        ids = set()
        for v in m.values():
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict) and it.get("id"):
                        ids.add(it["id"])

        bad, n = [], 0
        for tr in d.get("tracks") or []:
            for sg in (tr.get("segments") or []):
                if not isinstance(sg, dict):
                    continue
                n += 1
                for r in (sg.get("extra_material_refs") or []):
                    if r not in ids:
                        bad.append(r)

        missing = []
        for v in (m.get("videos") or []):
            q = str(v.get("path") or "")
            if q and not Path(q).exists():
                missing.append(q)

        luts = []
        for e in (m.get("effects") or []):
            q = str(e.get("path") or "")
            if ".cube" in q.lower():
                luts.append(q.split(";")[0])
        lut_missing = [x for x in luts if not Path(x).exists()]

        return {"segments": n, "bad_refs": bad[:10], "missing_media": missing[:10],
                "missing_luts": lut_missing[:10],
                "ok": not (bad or missing or lut_missing)}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                "segments": 0, "bad_refs": [], "missing_media": [],
                "missing_luts": []}


if __name__ == "__main__":
    s = sync()
    print("工程:", s["project"], "|", s["note"])
    for c in s["clips"][:20]:
        print("  %02d %-22s 时间线 %6.1f–%6.1fs  源入点 %.1fs  %s"
              % (c["index"], c["name"][:20], c["tl_start"],
                 c["tl_start"] + c["tl_dur"], c["src_start"], c["path"][:60]))
