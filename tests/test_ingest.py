"""ingest：达芬奇时间线 → 剪映草稿。

用的都是假对象（不连达芬奇、不碰剪映），验证的是**换算**与**落盘结构**：
  · 源帧数 / 时间线帧数 → 倍速（拿不到 Speed 属性也能算）
  · 停用片段 → visible:false，但仍占时间
  · 离线素材 / 生成器 → 跳过并写进报告，不静默丢
  · 多轨：达芬奇 V1 落主轨，V2 起是画中画
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from colorpass import draftbuild, ingest


class FakeMP:
    def __init__(self, path, width=1920, height=1080, frames=300, audio="2"):
        self.path = path
        self.props = {"File Path": path, "Resolution": "%dx%d" % (width, height),
                      "Frames": str(frames), "Audio Ch": audio}

    def GetClipProperty(self, key):
        return self.props.get(key, "")


class FakeItem:
    def __init__(self, name, start, dur, src_start=0, src_dur=None, mp=None,
                 enabled=True):
        self.name, self.start, self.dur = name, start, dur
        self.src_start = src_start
        self.src_dur = dur if src_dur is None else src_dur
        self.mp = mp
        self.enabled = enabled

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.start + self.dur

    def GetDuration(self):
        return self.dur

    def GetSourceStartFrame(self):
        return self.src_start

    def GetSourceEndFrame(self):
        return self.src_start + self.src_dur

    def GetClipEnabled(self):
        return self.enabled

    def GetMediaPoolItem(self):
        return self.mp


class FakeTimeline:
    def __init__(self, tracks, name="我的片子", fps=25, res=(1920, 1080)):
        self.tracks = tracks          # {"video": {1: [items], 2: [...]}}
        self.name = name
        self.fps = fps
        self.res = res

    def GetName(self):
        return self.name

    def GetSetting(self, key):
        return {"timelineFrameRate": str(self.fps),
                "timelineResolutionWidth": str(self.res[0]),
                "timelineResolutionHeight": str(self.res[1])}.get(key, "")

    def GetTrackCount(self, kind):
        return len(self.tracks.get(kind) or {})

    def GetItemListInTrack(self, kind, idx):
        return (self.tracks.get(kind) or {}).get(idx) or []


def media(path):
    Path(path).write_bytes(b"x" * 8)
    return path


class IngestReadCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.a = media(os.path.join(self.dir.name, "a.mov"))
        self.b = media(os.path.join(self.dir.name, "b.mov"))

    def tearDown(self):
        self.dir.cleanup()

    def test_speed_from_frames_and_disabled_kept(self):
        tl = FakeTimeline({"video": {1: [
            FakeItem("A", 0, 50, src_start=0, src_dur=100,
                     mp=FakeMP(self.a)),                 # 2 倍速
            FakeItem("B", 50, 50, src_start=0, src_dur=50,
                     mp=FakeMP(self.b), enabled=False),  # 停用，仍占 50 帧
        ]}})
        info = ingest.read_timeline(timeline=tl)
        tr = info["tracks"][0]
        self.assertEqual(tr["type"], "video")
        self.assertAlmostEqual(tr["items"][0]["speed"], 2.0, places=6)
        self.assertAlmostEqual(tr["items"][1]["speed"], 1.0, places=6)
        self.assertFalse(tr["items"][1]["enabled"])
        self.assertEqual(info["stats"], {"clips": 2, "disabled": 1, "retimed": 1,
                                         "no_media": 0, "no_source": 0})

    def test_v1_lands_on_main_track(self):
        """达芬奇轨道号 1 是底层；映射到剪映后必须是主轨（顺序第一位）。"""
        tl = FakeTimeline({"video": {
            1: [FakeItem("底", 0, 25, mp=FakeMP(self.a))],
            2: [FakeItem("画中画", 0, 25, mp=FakeMP(self.b))],
        }})
        info = ingest.read_timeline(timeline=tl)
        self.assertEqual(len(info["tracks"]), 2)
        self.assertEqual(info["tracks"][0]["items"][0]["name"], "底")
        self.assertEqual(info["tracks"][1]["items"][0]["name"], "画中画")

    def test_offline_media_is_reported_not_dropped(self):
        tl = FakeTimeline({"video": {1: [
            FakeItem("在线", 0, 25, mp=FakeMP(self.a)),
            FakeItem("离线", 25, 25, mp=FakeMP(os.path.join(self.dir.name, "no.mov"))),
        ]}})
        info = ingest.read_timeline(timeline=tl)
        self.assertEqual(info["stats"]["no_media"], 1)
        self.assertTrue(any("离线" in n for n in info["notes"]))
        self.assertEqual(len(info["tracks"][0]["items"]), 1)

    def test_no_usable_clip_raises(self):
        tl = FakeTimeline({"video": {1: []}})
        with self.assertRaises(RuntimeError):
            ingest.read_timeline(timeline=tl)

    def test_plan_duration_and_default_name(self):
        tl = FakeTimeline({"video": {1: [
            FakeItem("A", 0, 250, mp=FakeMP(self.a))]}}, fps=25)
        p = ingest.plan(ingest.read_timeline(timeline=tl))
        self.assertEqual(p["duration_s"], 10.0)
        self.assertEqual(p["name"], "我的片子_达芬奇")
        self.assertEqual(p["segments"][0][0]["dur_f"], 250)


class IngestBuildCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "drafts"
        self.root.mkdir()
        self.src = media(os.path.join(self.dir.name, "clip.mov"))
        self.tpl = os.path.join(os.path.dirname(draftbuild.__file__), "templates")

    def tearDown(self):
        self.dir.cleanup()

    def items(self):
        return [{
            "path": self.src, "name": "clip", "tl_start_f": 0, "dur_f": 100,
            "src_start_f": 50, "src_dur_f": 200, "src_total_f": 500,
            "speed": 2.0, "enabled": False, "width": 1920, "height": 1080,
            "has_audio": True,
        }]

    def test_draft_structure(self):
        rep = draftbuild.build_timeline(self.tpl, "测试_达芬奇", [
            {"type": "video", "items": self.items()},
        ], fps=25, width=1080, height=1920, register=False, root=str(self.root))
        self.assertTrue(rep["ok"], rep)

        d = json.loads((Path(rep["folder"]) / "draft_content.json")
                       .read_text(encoding="utf-8"))
        self.assertEqual(d["fps"], 25.0)
        self.assertEqual(d["canvas_config"], {"background": None, "width": 1080,
                                              "height": 1920, "ratio": "9:16"})
        self.assertEqual(len(d["tracks"]), 1)
        seg = d["tracks"][0]["segments"][0]
        # 25 fps：100 帧 = 4 s = 4,000,000 µs；源入点 50 帧 = 2 s
        self.assertEqual(seg["target_timerange"], {"start": 0, "duration": 4000000})
        self.assertEqual(seg["source_timerange"], {"start": 2000000,
                                                   "duration": 8000000})
        self.assertIs(seg["visible"], False)
        self.assertAlmostEqual(seg["speed"], 2.0)
        mats = d["materials"]
        self.assertEqual(len(mats["videos"]), 1)
        self.assertEqual(mats["videos"][0]["duration"], 20000000)   # 500 帧
        self.assertAlmostEqual(mats["speeds"][0]["speed"], 2.0)
        self.assertIn(mats["speeds"][0]["id"], seg["extra_material_refs"])
        self.assertEqual(d["duration"], 4000000)

    def test_two_tracks_attribute(self):
        rep = draftbuild.build_timeline(self.tpl, "双轨_达芬奇", [
            {"type": "video", "items": self.items()},
            {"type": "video", "items": [dict(self.items()[0], dur_f=50,
                                             src_dur_f=50, speed=1.0)]},
        ], fps=25, register=False, root=str(self.root))
        self.assertTrue(rep["ok"], rep)
        d = json.loads((Path(rep["folder"]) / "draft_content.json")
                       .read_text(encoding="utf-8"))
        self.assertEqual([t["attribute"] for t in d["tracks"]], [0, 1])
        self.assertEqual(rep["clips"], 2)


class IngestRunCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.a = media(os.path.join(self.dir.name, "a.mov"))
        self.tl = FakeTimeline({"video": {1: [
            FakeItem("A", 0, 50, src_start=0, src_dur=50, mp=FakeMP(self.a))]}})

    def tearDown(self):
        self.dir.cleanup()

    def test_dry_run_through_patch(self):
        from unittest import mock
        with mock.patch.object(ingest, "read_timeline",
                               return_value=ingest.read_timeline(timeline=self.tl)):
            rep = ingest.run(dry_run=True)
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["clips"], 1)
        self.assertNotIn("draft", rep)

    def test_run_writes_draft(self):
        """整条链：读时间线 → 落草稿。登记那步打桩，别碰真实剪映目录。"""
        from unittest import mock
        info = ingest.read_timeline(timeline=self.tl)
        drafts = self.root / "drafts"
        drafts.mkdir()
        with mock.patch.object(ingest, "read_timeline", return_value=info), \
                mock.patch.object(ingest.probe, "user_data_dir",
                                  return_value=self.root), \
                mock.patch.object(draftbuild, "register_in_root_meta",
                                  return_value={"ok": True}):
            rep = ingest.run(name="测试_达芬奇", root=str(drafts))
        self.assertTrue(rep["ok"], rep)
        folder = Path(rep["draft"]["folder"])
        self.assertTrue((folder / "draft_content.json").is_file())
        self.assertEqual(rep["draft"]["name"], "测试_达芬奇")


if __name__ == "__main__":
    unittest.main()
