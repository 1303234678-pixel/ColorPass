"""真机验证：图片段与音频轨能不能按时长落进达芬奇（21.2）。

不依赖剪映草稿 —— 直接拼一份 plan，调 send 里真正在用的那几个函数：
place_video / place_photo / place_audio。跑完把实验时间线删掉。

用法（达芬奇开着、进了一个工程）：python tools/verify_photo_audio.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from colorpass import compat, resolve, send  # noqa: E402

MP4 = "C:/新建文件夹/C0006.MP4"
IMG = "C:/新建文件夹/01_四帧前后对比.jpg"
TL = "CP·PA"
OUT = Path(__file__).resolve().parent.parent / "build" / "verify_pa.json"


def drop(proj, mp, name):
    for i in range(1, (proj.GetTimelineCount() or 0) + 1):
        t = proj.GetTimelineByIndex(i)
        if t and t.GetName() == name:
            compat.delete_timelines(mp, [t])


def main():
    app = resolve.scriptapp("Resolve")
    rep = {"ok": False, "steps": {}}
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
        rep["error"] = "没有工程"
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 1
    mp = proj.GetMediaPool()
    rep["project"] = proj.GetName()

    drop(proj, mp, TL)
    tl = mp.CreateEmptyTimeline(TL)
    if tl is None:
        rep["error"] = "建不了时间线"
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 1
    proj.SetCurrentTimeline(tl)
    tl.SetStartTimecode("00:00:00:00")
    time.sleep(0.5)
    base = int(tl.GetStartFrame() or 0)
    rep["base"] = base
    rep["fps"] = tl.GetSetting("timelineFrameRate")
    rep["size"] = [proj.GetSetting("timelineResolutionWidth"),
                   proj.GetSetting("timelineResolutionHeight")]

    # 素材：视频 + 图片（图片的转码在 place_photo 里做）
    idx = send.media_index(mp)
    if send._key(MP4) not in idx:
        compat.import_media(mp, [MP4])
        idx = send.media_index(mp)
    rep["steps"]["import"] = {"video": send._key(MP4) in idx}

    plan = [
        {"segment_id": "V1", "lane": 1, "kind": "video", "path": MP4,
         "rec": 0, "want": 30, "place": 30, "src_us": 0, "speed": 1.0,
         "enabled": True},
        {"segment_id": "P1", "lane": 1, "kind": "photo", "path": IMG,
         "rec": 30, "want": 90, "place": 90, "src_us": 0, "speed": 1.0,
         "enabled": True, "photo_px": [1520, 1836], "photo_type": "photo"},
    ]
    done = []
    for c in plan:
        if c["kind"] == "photo":
            ok, items, detail = send.place_photo(
                mp, c, idx, draft_id="LABPA",
                fps=float(tl.GetSetting("timelineFrameRate") or 30),
                size=(int(rep["size"][0] or 1920), int(rep["size"][1] or 1080)))
        else:
            ok, items, detail = send.place_video(mp, c, idx)
        if ok:
            it = items[0]["item"]
            done.append({"segment_id": c["segment_id"], "kind": c["kind"],
                         "start": it.GetStart() - base, "dur": it.GetDuration(),
                         "want": c["want"], "clip": detail.get("clip")})
        else:
            done.append({"segment_id": c["segment_id"], "kind": c["kind"],
                         "error": detail.get("why") if isinstance(detail, dict)
                         else detail})
        time.sleep(0.3)
    rep["steps"]["video_photo"] = done

    # 音频：落在图片段下面（0..45），验证 mediaType=2 不会带画面进来
    audio_plan = [{"segment_id": "A1", "lane": 1, "rec": 0, "want": 45,
                   "path": MP4, "name": "音乐", "type": "music"}]
    a_ok, a_fail = send.place_audio(mp, tl, audio_plan, send.media_index(mp))
    rep["steps"]["audio"] = {
        "placed": [{k: r.get(k) for k in
                    ("segment_id", "lane_actual", "n", "got_start", "got_dur",
                     "fps", "total")} for r in a_ok],
        "failed": a_fail,
    }

    time.sleep(0.4)
    rep["tracks"] = {"video": tl.GetTrackCount("video"),
                     "audio": tl.GetTrackCount("audio")}
    rep["v1"] = [{"start": it.GetStart() - base, "dur": it.GetDuration()}
                 for it in (tl.GetItemListInTrack("video", 1) or [])]
    rep["a1"] = [{"start": it.GetStart() - base, "dur": it.GetDuration()}
                 for it in (tl.GetItemListInTrack("audio", 1) or [])]
    rep["end"] = tl.GetEndFrame() - base

    ok = (len(rep["v1"]) == 2
          and rep["v1"][0]["dur"] == 30
          and rep["v1"][1] == {"start": 30, "dur": 90}
          and len(rep["a1"]) == 1
          and rep["a1"][0]["start"] == 0
          and rep["a1"][0]["dur"] <= 45)
    rep["ok"] = bool(ok)

    drop(proj, mp, TL)
    rep["cleaned"] = True
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
