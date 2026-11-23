"""真机端到端：一份「视频 + 图片 + 音乐」的草稿，走完整的 send.run。

不依赖剪映 —— 用 mock 把 draftio.read 换成构造好的明文草稿，其余全是真代码
（建时间线、归零时间码、导入、落轨、打标记、上锁、写报告）。这能覆盖
place_photo / place_audio 在 run 里的真实交互，而不只是单独调它们。

跑完删掉实验时间线，并清掉 pending 记录。
"""
import json
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from colorpass import compat, resolve, send, snapshot  # noqa: E402

MP4 = "C:/新建文件夹/C0006.MP4"          # 50fps / 72 帧 / 带声音
IMG = "C:/新建文件夹/01_四帧前后对比.jpg"  # 竖图 1520x1836
OUT = Path(__file__).resolve().parent.parent / "build" / "verify_pa_e2e.json"


def build_draft():
    """1 秒视频 + 3 秒图片 + 2 秒音乐，全部首尾相接。"""
    v = MP4.replace("\\", "/")
    i = IMG.replace("\\", "/")
    return {
        "id": "LABPA2", "name": "PA 测试", "duration": 6_000_000,
        "materials": {
            "videos": [{"id": "V1", "path": v, "duration": 1_440_000}],
            "photos": [{"id": "PH1", "path": i, "width": 1520, "height": 1836}],
            "audios": [{"id": "AU1", "path": v, "name": "音乐", "type": "music",
                        "duration": 1_440_000}],
            "speeds": [], "transitions": [], "drafts": [], "effects": [],
            "audio_fades": [],
        },
        "tracks": [
            {"id": "TV", "type": "video", "segments": [
                {"id": "S1", "material_id": "V1", "extra_material_refs": [],
                 "common_keyframes": [],
                 "source_timerange": {"start": 0, "duration": 1_000_000},
                 "target_timerange": {"start": 0, "duration": 1_000_000}},
                {"id": "S2", "material_id": "PH1", "extra_material_refs": [],
                 "common_keyframes": [],
                 "source_timerange": {"start": 0, "duration": 3_000_000},
                 "target_timerange": {"start": 1_000_000, "duration": 3_000_000}},
            ]},
            {"id": "TA", "type": "audio", "segments": [
                {"id": "S3", "material_id": "AU1", "extra_material_refs": [],
                 "target_timerange": {"start": 1_000_000, "duration": 2_000_000}},
            ]},
        ],
    }


def main():
    app = resolve.scriptapp("Resolve")
    rep = {"ok": False}
    if app is None:
        rep["error"] = "连不上达芬奇"
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 2
    pm = app.GetProjectManager()
    proj = pm.GetCurrentProject()
    if proj is None or app.GetCurrentPage() is None:
        names = list(pm.GetProjectListInCurrentFolder() or [])
        try:
            pm.LoadProject(names[0]) if names else pm.CreateProject("CP PA")
        except Exception as exc:
            rep["error"] = "打不开工程：%s" % exc
            print(json.dumps(rep, ensure_ascii=False, indent=1))
            return 1
        time.sleep(4.0)
        proj = pm.GetCurrentProject()
    if proj is None:
        print(json.dumps({"ok": False, "error": "没有工程"}, ensure_ascii=False))
        return 1

    draft = build_draft()
    cur = {"name": "PA 测试", "folder": str(Path.home()), "timeline_id": "LABPA2"}
    fresh = {"stale": False, "plaintext_bytes": 1, "disk_payload_bytes": 1,
             "delta_bytes": 0, "source": "lab"}
    try:
        with mock.patch.object(send.draftio, "read",
                               return_value=(draft, cur, fresh)):
            rep = send.run(speed_mode="slot")
    finally:
        pass
    rep = {k: v for k, v in rep.items()
           if k in ("ok", "error", "timeline", "placed", "exact", "failed",
                    "photos", "audio", "skipped", "timeline_end",
                    "expected_end", "a_tracks", "note_marker")}

    # 读回真实时间线
    mp = proj.GetMediaPool()
    tl = None
    for i in range(1, (proj.GetTimelineCount() or 0) + 1):
        t = proj.GetTimelineByIndex(i)
        if t and t.GetName() == rep.get("timeline"):
            tl = t
            break
    if tl is not None:
        base = tl.GetStartFrame() or 0
        rep["v1"] = [{"start": it.GetStart() - base, "dur": it.GetDuration()}
                     for it in (tl.GetItemListInTrack("video", 1) or [])]
        rep["audio_tracks"] = {}
        for lane in range(1, (tl.GetTrackCount("audio") or 0) + 1):
            rep["audio_tracks"][lane] = [
                {"start": it.GetStart() - base, "dur": it.GetDuration()}
                for it in (tl.GetItemListInTrack("audio", lane) or [])]
        # 清理：删实验时间线
        compat.delete_timelines(mp, [tl])
        rep["cleaned"] = True
    try:
        snapshot.clear_pending()
    except Exception:
        pass

    v1 = rep.get("v1") or []
    has_photo = any(d["dur"] == 90 for d in v1)
    has_music = any(d["start"] == 30 for items in
                    (rep.get("audio_tracks") or {}).values() for d in items)
    rep["ok"] = bool(rep.get("ok") and has_photo and has_music)
    rep["checks"] = {"photo_90": has_photo, "music_at_30": has_music}
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
