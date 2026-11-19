"""送入达芬奇的端到端测试 —— 用一个「假达芬奇」跑 send.run 的全部真代码。

为什么值得单独一个文件
----------------------
`solve_exact / build_plan` 是纯函数，单测能覆盖；但真正决定「送进去对不对」的
是 send.run 里那串动作：建时间线、归零时间码、按绝对帧号 AppendToTimeline、
打身份标记、上锁、写 plan.json、commit 快照。这些只有连着跑才会暴露问题
（历史事故：先摆后摆顺序、复合片段把外层片段顶开、时间线末尾差一帧）。

这里不 import 达芬奇，也不碰真实草稿：
  · FakeResolve/FakeMediaPool/FakeTimeline 顶替脚本接口
  · draftio.read 直接返回构造好的明文草稿
  · CURRENT/PENDING 指到临时目录
  · 变速只用 slot 模式（bake 要真跑 ffmpeg，另在 test_units 里测过单段）
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jybridge import paths, send, snapshot  # noqa: E402

FPS = paths.FPS


# ================================================================ 假达芬奇

class FakeClip(object):
    """时间线上的一段。AppendToTimeline 的 endFrame 不含尾，所以 dur=end-start。"""

    def __init__(self, start, dur, media=None, name="clip"):
        self.start, self.dur = int(start), int(dur)
        self.media = media
        self.name = name
        self.markers = {}
        self.enabled = True

    def SetClipEnabled(self, on):
        self.enabled = bool(on)
        return True

    def GetClipEnabled(self):
        return self.enabled

    def GetStart(self):
        return self.start

    def GetDuration(self):
        return self.dur

    def GetName(self):
        return self.name

    def AddMarker(self, frame, color, name, note="", dur=1, custom=None):
        self.markers[int(frame)] = {"name": name, "note": note,
                                    "customData": custom or note}
        return True

    def GetMarkers(self):
        return self.markers

    def GetProperty(self):
        return {}

    def GetMediaPoolItem(self):
        return self.media


class FakeMediaItem(object):
    default_fps = 30.0

    def __init__(self, path, fps=None, frames=100000):
        self.path = str(path)
        self.fps = float(fps if fps is not None else self.default_fps)
        self.frames = int(frames)

    def GetClipProperty(self, key):
        return {"File Path": self.path, "FPS": self.fps,
                "Frames": self.frames}.get(key)

    def GetName(self):
        return Path(self.path).name


class FakeTimeline(object):
    def __init__(self, name="JY · 测试"):
        self.name = name
        self.tracks = {"video": {}, "audio": {}}
        self.locks = {}
        self.markers = {}
        self.start_frame = 0
        self.start_tc = "00:00:00:00"

    # ---- 基本信息 ----
    def GetName(self):
        return self.name

    def SetStartTimecode(self, tc):
        self.start_tc = tc
        self.start_frame = 0
        return True

    def GetStartFrame(self):
        return self.start_frame

    def GetEndFrame(self):
        ends = [c.start + c.dur for kind in self.tracks.values()
                for items in kind.values() for c in items]
        return max(ends) if ends else 0

    def GetTrackCount(self, kind):
        return len(self.tracks.get(kind) or {})

    def AddTrack(self, kind):
        d = self.tracks.setdefault(kind, {})
        d[len(d) + 1] = []
        return True

    def GetItemListInTrack(self, kind, lane):
        return list((self.tracks.get(kind) or {}).get(lane) or [])

    def SetTrackLock(self, kind, lane, on):
        self.locks[(kind, lane)] = bool(on)
        return True

    def GetIsTrackLocked(self, kind, lane):
        return self.locks.get((kind, lane), False)

    def AddMarker(self, frame, color, name, note="", dur=1, custom=None):
        self.markers[int(frame)] = {"name": name, "color": color, "note": note}
        return True

    def GetMarkers(self):
        return self.markers

    def DeleteMarkerAtFrame(self, frame):
        self.markers.pop(int(frame), None)

    # ---- 核心：摆放 ----
    def AppendToTimeline(self, specs):
        out = []
        for s in (specs or []):
            kind = "video"
            lane = int(s.get("trackIndex") or 1)
            start, end = int(s["startFrame"]), int(s["endFrame"])
            rec = int(s["recordFrame"])
            media = s.get("mediaPoolItem")
            mf = float(getattr(media, "fps", FPS) or FPS)
            # 真实达芬奇的规则：源帧数按素材帧率折算成时间线帧数
            dur = int(round((end - start) * FPS / mf))
            it = FakeClip(rec, dur, media)
            self.tracks.setdefault(kind, {}).setdefault(lane, []).append(it)
            out.append(it)
        return out

    def CreateCompoundClip(self, items, info=None):
        """把若干片段合成一项：取最小起点、最大终点，落在最小号轨道上。"""
        if not items:
            return None
        lo = min(i.GetStart() for i in items)
        hi = max(i.GetStart() + i.GetDuration() for i in items)
        lanes = []
        for lane, lst in (self.tracks.get("video") or {}).items():
            for i in items:
                if i in lst:
                    lst.remove(i)
                    lanes.append(lane)
        lane = min(lanes) if lanes else 1
        cc = FakeClip(lo, hi - lo, name=(info or {}).get("name") or "复合")
        self.tracks.setdefault("video", {}).setdefault(lane, []).append(cc)
        return cc


class FakePool(object):
    def __init__(self, project):
        self.project = project
        self.imported = []

    def _all(self):
        return self.imported

    def GetRootFolder(self):
        pool = self

        class _F(object):
            def GetClipList(self):
                return list(pool.imported)

            def GetSubFolderList(self):
                return []
        return _F()

    def ImportMedia(self, paths_):
        out = []
        for p in (paths_ or []):
            if not Path(p).exists():
                continue
            if any(m.path == str(p) for m in self.imported):
                continue
            m = FakeMediaItem(p)
            self.imported.append(m)
            out.append(m)
        return out

    def CreateEmptyTimeline(self, name):
        tl = FakeTimeline(name)
        self.project.timelines.append(tl)
        self.project.current = tl
        return tl

    def DeleteTimelines(self, tls):
        for t in (tls or []):
            if t in self.project.timelines:
                self.project.timelines.remove(t)
                if self.project.current is t:
                    self.project.current = None
        return True

    def AppendToTimeline(self, specs):
        """真实接口在 MediaPool 上，落进**当前**时间线。"""
        tl = self.project.current
        return tl.AppendToTimeline(specs) if tl else []


class FakeProject(object):
    def __init__(self):
        self.timelines = []
        self.current = None
        self.settings = {}
        self.pool = FakePool(self)

    def GetName(self):
        return "假工程"

    def SetSetting(self, k, v):
        self.settings[k] = v
        return True

    def GetSetting(self, k):
        return self.settings.get(k)

    def GetMediaPool(self):
        return self.pool

    def SetCurrentTimeline(self, tl):
        self.current = tl
        return True

    def GetCurrentTimeline(self):
        return self.current

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, i):
        return self.timelines[i - 1]


class FakeResolve(object):
    def __init__(self):
        self.project = FakeProject()

    def GetProjectManager(self):
        proj = self.project

        class _PM(object):
            def GetCurrentProject(self):
                return proj
        return _PM()

    def GetCurrentPage(self):
        # 真达芬奇停在项目管理器时返回 None；进了工程才返回 "edit"/"color"。
        # send 现在靠这个判断「有没有真的进工程」，假件必须给一个页面。
        return "edit"

    def OpenPage(self, name):
        return True


# ================================================================ 夹具

def draft_video(n=3, dur_us=4_000_000, start_us=0):
    """n 段首尾相接的普通视频。素材用解释器本体（保证路径存在）。"""
    p = sys.executable.replace("\\", "/")
    segs, vids = [], []
    for i in range(n):
        segs.append({
            "id": "SEG-%02d" % i, "material_id": "VID-%02d" % i,
            "extra_material_refs": [], "common_keyframes": [],
            "source_timerange": {"start": 0, "duration": dur_us},
            "target_timerange": {"start": start_us + i * dur_us,
                                 "duration": dur_us},
        })
        vids.append({"id": "VID-%02d" % i, "path": p, "duration": 60_000_000})
    return {"id": "D1", "duration": start_us + n * dur_us,
            "materials": {"videos": vids, "speeds": [], "drafts": [],
                          "transitions": [], "effects": []},
            "tracks": [{"id": "T1", "type": "video", "segments": segs}]}


def draft_compound():
    """一段普通视频 + 一个复合片段（内部 2 段，正好铺满占位）。"""
    d = draft_video(1, 4_000_000, start_us=4_000_000)
    p = sys.executable.replace("\\", "/")
    inner = {
        "materials": {"videos": [{"id": "IV0", "path": p},
                                 {"id": "IV1", "path": p}]},
        "tracks": [{"type": "video", "segments": [
            {"id": "IN0", "material_id": "IV0", "extra_material_refs": [],
             "source_timerange": {"start": 0, "duration": 2_000_000},
             "target_timerange": {"start": 0, "duration": 2_000_000}},
            {"id": "IN1", "material_id": "IV1", "extra_material_refs": [],
             "source_timerange": {"start": 0, "duration": 2_000_000},
             "target_timerange": {"start": 2_000_000, "duration": 2_000_000}},
        ]}],
    }
    d["materials"]["drafts"] = [{"id": "C1", "name": "复合段", "draft": inner}]
    d["tracks"][0]["segments"].insert(0, {
        "id": "CMP-00", "material_id": "",
        "extra_material_refs": ["C1"], "common_keyframes": [],
        "source_timerange": {"start": 0, "duration": 4_000_000},
        "target_timerange": {"start": 0, "duration": 4_000_000},
    })
    return d


class SendBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jyb_send_")
        self._cur = snapshot.CURRENT_PATH
        self._pend = snapshot.PENDING_PATH
        snapshot.CURRENT_PATH = os.path.join(self.tmp, "current.json")
        snapshot.PENDING_PATH = os.path.join(self.tmp, "pending.json")
        self.run_dir = os.path.join(self.tmp, "runs", "D1")
        os.makedirs(self.run_dir, exist_ok=True)
        # paths.draft_run_dir 指到临时目录，别污染真实 %LOCALAPPDATA%
        self._prd = paths.draft_run_dir
        paths.draft_run_dir = lambda did: (os.makedirs(
            os.path.join(self.tmp, "runs", str(did)), exist_ok=True)
            or os.path.join(self.tmp, "runs", str(did)))
        self.resolve = FakeResolve()
        # 真代码里有几处「等达芬奇缓过来」的 sleep（各 0.4~0.5 秒），
        # 测试里没必要真等，否则 18 个用例要跑 15 秒。
        self._sleep = mock.patch("jybridge.send.time.sleep", return_value=None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        snapshot.CURRENT_PATH = self._cur
        snapshot.PENDING_PATH = self._pend
        paths.draft_run_dir = self._prd
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_send(self, draft, speed="slot", fresh=None):
        cur = {"name": "草稿A", "folder": self.tmp, "timeline_id": "D1"}
        fresh = fresh or {"stale": False, "plaintext_bytes": 1,
                          "disk_payload_bytes": 1, "delta_bytes": 0}
        with mock.patch.object(send.draftio, "read",
                               return_value=(draft, cur, fresh)), \
                mock.patch.object(send.dvr, "scriptapp",
                                  return_value=self.resolve):
            return send.run(speed_mode=speed)

    def plan(self):
        p = os.path.join(paths.draft_run_dir("D1"), "plan.json")
        return json.loads(Path(p).read_text(encoding="utf-8"))


# ================================================================ 用例

class TestSendVideo(SendBase):
    def test_three_segments_exact(self):
        rep = self.run_send(draft_video(3))
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["placed"], 3)
        self.assertEqual(rep["exact"], 3)
        self.assertEqual(rep["timeline_end"], 360)        # 3 × 4s × 30fps
        self.assertEqual(rep["expected_end"], 360)
        self.assertEqual(rep["markers"], 3)
        self.assertEqual(rep["failed"], [])
        # 时间线真的摆了三段，且首尾相接
        tl = self.resolve.project.current
        items = tl.GetItemListInTrack("video", 1)
        self.assertEqual([i.GetStart() for i in items], [0, 120, 240])
        self.assertEqual([i.GetDuration() for i in items], [120, 120, 120])
        # 每段都带身份标记（取回时靠它认段）
        self.assertTrue(all("customData" in m for i in items
                            for m in i.GetMarkers().values()))

    def test_all_tracks_locked(self):
        rep = self.run_send(draft_video(2))
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertTrue(rep["locks"])
        self.assertTrue(all(x["locked"] for x in rep["locks"]),
                        rep["locks"])

    def test_plan_and_snapshot_committed(self):
        self.run_send(draft_video(3))
        plan = self.plan()
        self.assertEqual(len(plan["segments"]), 3)
        self.assertEqual(plan["draft_id"], "D1")
        self.assertEqual(plan["start_frame"], 0)
        self.assertEqual([s["rec"] for s in plan["segments"]], [0, 120, 240])
        self.assertEqual(plan["timeline"], self.resolve.project.current.GetName())
        ptr = snapshot.current()
        self.assertTrue(ptr.get("committed"))
        self.assertFalse(ptr.get("_incomplete"))
        self.assertEqual(ptr["segments"], 3)

    def test_bridge_marker_written(self):
        self.run_send(draft_video(1))
        marks = self.resolve.project.current.GetMarkers()
        self.assertIn(0, marks)
        self.assertEqual(marks[0]["name"], "BRIDGE")

    def test_project_settings_forced(self):
        self.run_send(draft_video(1))
        s = self.resolve.project.settings
        self.assertEqual(s.get("timelineFrameRate"), "30")
        self.assertEqual(s.get("timelineResolutionWidth"), "1920")

    def test_transition_overlap_trimmed(self):
        """两段有转场 → 目标区间重叠 → 前一段收短，起点不动。"""
        d = draft_video(2)
        d["materials"]["transitions"] = [{"id": "TR1", "name": "叠化",
                                          "duration": 1_000_000}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["TR1"]
        d["tracks"][0]["segments"][1]["target_timerange"]["start"] = 3_500_000
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(len(rep["overlaps"]), 1)
        self.assertTrue(rep["overlaps"][0]["transition"])
        self.assertEqual(rep["trimmed"][0]["cut_frames"], 15)   # 0.5s
        items = self.resolve.project.current.GetItemListInTrack("video", 1)
        self.assertEqual(items[0].GetDuration(), 105)           # 120-15
        self.assertEqual(items[1].GetStart(), 105)

    def test_fatal_overlap_aborts_without_timeline(self):
        d = draft_video(2)
        d["tracks"][0]["segments"][1]["target_timerange"]["start"] = 0
        rep = self.run_send(d)
        self.assertFalse(rep["ok"])
        self.assertIn("整段被覆盖", rep["error"])
        self.assertEqual(self.resolve.project.timelines, [], "不许建时间线")
        self.assertIsNone(snapshot.current(), "不许留下可写回的指针")

    def test_missing_media_skipped(self):
        d = draft_video(2)
        d["materials"]["videos"][0]["path"] = "Z:/nope/缺失.mp4"
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["placed"], 1)
        self.assertEqual(len(rep["skipped"]), 1)
        self.assertIn("素材不在盘上", rep["skipped"][0]["reason"])

    def test_no_placeable_segments(self):
        d = draft_video(1)
        d["materials"]["videos"][0]["path"] = "Z:/nope.mp4"
        rep = self.run_send(d)
        self.assertFalse(rep["ok"])
        self.assertIn("没有可摆放的片段", rep["error"])

    def test_offset_start_is_preserved(self):
        """剪映里从 2 秒处开始有画面（前面是空白）→ 达芬奇里也要空出来。"""
        rep = self.run_send(draft_video(2, start_us=2_000_000))
        self.assertTrue(rep["ok"], rep.get("error"))
        items = self.resolve.project.current.GetItemListInTrack("video", 1)
        self.assertEqual([i.GetStart() for i in items], [60, 180])

    def test_no_project_guard(self):
        """--no-gate 排错模式下工程为空：给结构化报错，不要 AttributeError。"""
        self.resolve.project = None

        class _PM(object):
            def GetCurrentProject(self_):
                return None

        with mock.patch.object(self.resolve, "GetProjectManager",
                               return_value=_PM()):
            rep = self.run_send(draft_video(1))
        self.assertFalse(rep["ok"])
        self.assertIn("没有打开工程", rep["error"])

    def test_high_fps_media_solved_exactly(self):
        """素材 60fps、时间线 30fps：落地帧数要反解，不能直接按源帧数摆。"""
        d = draft_video(1)
        old = FakeMediaItem.default_fps
        FakeMediaItem.default_fps = 60.0
        try:
            rep = self.run_send(d)
        finally:
            FakeMediaItem.default_fps = old
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["exact"], 1)
        self.assertEqual(self.resolve.project.current
                         .GetItemListInTrack("video", 1)[0].GetDuration(), 120)


class TestSendDisabled(SendBase):
    """剪映的停用段（段上 visible:false）→ 达芬奇的禁用片段。

    剪映的停用 = 留着位置、不渲染。跳过它会让达芬奇里多出一段黑场，还会把
    后面每一段的位置核对全打乱，所以必须照样落轨、落完再关掉。
    """

    def clips(self, lane=1):
        return sorted(self.resolve.project.current.GetItemListInTrack("video", lane),
                      key=lambda c: c.GetStart())

    def test_disabled_segment_keeps_slot_but_turns_off(self):
        d = draft_video(3)
        d["tracks"][0]["segments"][1]["visible"] = False
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["placed"], 3)              # 一段都没少
        self.assertEqual(rep["disabled"], 1)
        self.assertEqual(rep["disabled_failed"], [])
        self.assertEqual(rep["timeline_end"], 360)      # 位置没动，后面没被前移
        got = self.clips()
        self.assertEqual([c.enabled for c in got], [True, False, True])
        self.assertEqual([c.GetStart() for c in got], [0, 120, 240])

    def test_enabled_draft_turns_nothing_off(self):
        rep = self.run_send(draft_video(2))
        self.assertEqual(rep["disabled"], 0)
        self.assertTrue(all(c.enabled for c in self.clips()))

    def test_mark_disabled_reports_missing_api(self):
        class Bare(object):
            pass

        placed = [{"segment_id": "S1", "enabled": False,
                   "items": [{"item": Bare()}]}]
        done, fail = send.mark_disabled(placed)
        self.assertEqual(done, 0)
        self.assertTrue(fail and fail[0]["no_api"])
        self.assertIn("SetClipEnabled", fail[0]["why"])

    def test_mark_disabled_leaves_enabled_segments_alone(self):
        touched = []

        class Clip(object):
            def SetClipEnabled(self, on):
                touched.append(on)
                return True

        placed = [{"segment_id": "S1", "enabled": True,
                   "items": [{"item": Clip()}]},
                  {"segment_id": "S2", "enabled": False,
                   "items": [{"item": Clip()}, {"item": Clip()}]}]
        done, fail = send.mark_disabled(placed)
        self.assertEqual(done, 1)
        self.assertEqual(fail, [])
        # 只给停用那段（两段 main+pad）关，启用那段一次都不碰
        self.assertEqual(touched, [False, False])


class TestSendCompound(SendBase):
    def test_compound_and_video(self):
        rep = self.run_send(draft_compound())
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["kinds"], {"compound": 1, "video": 1})
        self.assertEqual(rep["placed"], 2)
        self.assertEqual(rep["timeline_end"], 240)
        plan = self.plan()
        comp = [s for s in plan["segments"] if s["kind"] == "compound"][0]
        self.assertEqual((comp["rec"], comp["dur"]), (0, 120))
        # 合成项在时间线上确实是一项，且长度等于占位
        items = self.resolve.project.current.GetItemListInTrack("video", 1)
        self.assertEqual(len([i for i in items if i.GetName() == "复合段"]), 1)

    def test_compound_inner_disabled_is_reported(self):
        # 复合片段内部的停用段：CreateCompoundClip 会不会跟着变灰没实测过，
        # 所以不贸然处理，但必须报出来 —— 不能静默当成正常段。
        d = draft_compound()
        inner = d["materials"]["drafts"][0]["draft"]
        inner["tracks"][0]["segments"][0]["visible"] = False
        plan = send.build_plan(d)[0]
        comp = [c for c in plan if c["kind"] == "compound"][0]
        self.assertEqual(comp["inner_disabled"], 1)
        rep = self.run_send(d)
        self.assertEqual(rep.get("compound_disabled_warning"), 1)

    def test_compound_with_missing_inner_media_skipped(self):
        d = draft_compound()
        d["materials"]["drafts"][0]["draft"]["materials"]["videos"][0]["path"] = \
            "Z:/nope.mp4"
        rep = self.run_send(d)
        self.assertEqual(len(rep["skipped"]), 1)
        self.assertIn("内部素材不在盘上", rep["skipped"][0]["reason"])

    def test_nested_compound_depth(self):
        """复合里再套复合：要递归展开，且层级归一化后不越界。"""
        d = draft_compound()
        inner = d["materials"]["drafts"][0]["draft"]
        inner["materials"]["drafts"] = [{
            "id": "C2", "draft": {
                "materials": {"videos": [{"id": "DV0",
                                          "path": sys.executable}]},
                "tracks": [{"type": "video", "segments": [{
                    "id": "DN0", "material_id": "DV0",
                    "source_timerange": {"start": 0, "duration": 2_000_000},
                    "target_timerange": {"start": 0, "duration": 2_000_000},
                }]}],
            }}]
        inner["tracks"][0]["segments"][1] = {
            "id": "IN1", "material_id": "", "extra_material_refs": ["C2"],
            "source_timerange": {"start": 0, "duration": 2_000_000},
            "target_timerange": {"start": 2_000_000, "duration": 2_000_000},
        }
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["placed"], 2)


class TestSendSpeed(SendBase):
    def test_speed_slot_reports_truncation(self):
        d = draft_video(1)
        d["materials"]["speeds"] = [{"id": "SP1", "speed": 2.0}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["SP1"]
        d["tracks"][0]["segments"][0]["source_timerange"] = {
            "start": 0, "duration": 8_000_000}
        rep = self.run_send(d, speed="slot")
        self.assertTrue(rep["ok"], rep.get("error"))
        row = rep["speed_segments"][0]
        self.assertAlmostEqual(row["speed"], 2.0, places=3)
        self.assertEqual(row["truncated_frames"], 120)   # 源 240 帧只播 120
        self.assertIn("尾部", row["presentation"])

    def test_speed_slow_reports_overrun(self):
        d = draft_video(1)
        d["materials"]["speeds"] = [{"id": "SP1", "speed": 0.5}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["SP1"]
        d["tracks"][0]["segments"][0]["source_timerange"] = {
            "start": 0, "duration": 2_000_000}
        rep = self.run_send(d, speed="slot")
        row = rep["speed_segments"][0]
        self.assertEqual(row["overrun_frames"], 60)      # 要 120 帧，只有 60
        self.assertIn("越界", row["presentation"])

    def test_speed_bake_without_ffmpeg_falls_back(self):
        d = draft_video(1)
        with mock.patch("jybridge.retime.available", return_value=False):
            rep = self.run_send(d, speed="bake")
        self.assertEqual(rep["speed_mode"], "slot")
        self.assertIn("ffmpeg", rep["speed_fallback"])

    def test_speed_bake_marks_baked(self):
        d = draft_video(1)
        d["materials"]["speeds"] = [{"id": "SP1", "speed": 2.0}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["SP1"]
        d["tracks"][0]["segments"][0]["source_timerange"] = {
            "start": 0, "duration": 8_000_000}
        baked = [{"segment_id": "SEG-00", "speed": 2.0, "want": 120,
                  "out": sys.executable, "cached": True, "audio": True}]

        def fake_bake_plan(plan, draft_id):
            for c in plan:
                if c.get("bake"):
                    c["path"] = sys.executable
                    c["src_us"] = 0
            return baked, []

        # 显式声明「这台机器有 ffmpeg」：否则没装 ffmpeg 的环境（CI）会先退回
        # slot 模式，根本走不到预渲染，这条用例就成了环境探测器。
        with mock.patch("jybridge.retime.available", return_value=True), \
                mock.patch("jybridge.retime.bake_plan",
                           side_effect=fake_bake_plan):
            rep = self.run_send(d, speed="bake")
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["baked"], baked)
        self.assertIn("已预渲染", rep["speed_segments"][0]["presentation"])


class TestSendMasked(SendBase):
    """带蒙版送入 —— 2026-11-14 用户新加两个蒙版后的真机场景。

    蒙版在达芬奇里建不出来（TimelineItem 没有蒙版接口），所以这一步能证明的
    是三件事：
      1) 蒙版不干扰摆放：帧位、段数、末尾帧与没蒙版时完全一致；
      2) 蒙版被**数出来**并写进报告，用户看得见它没被吞掉；
      3) 送入快照里带着蒙版，写回才有东西可克隆。
    """

    def test_masked_draft_places_identically(self):
        plain = self.run_send(draft_video(3))
        self.assertTrue(plain["ok"], plain.get("error"))
        plain_items = [(i.GetStart(), i.GetDuration())
                       for i in self.resolve.project.current.GetItemListInTrack("video", 1)]

        d = draft_video(3)
        d["materials"]["common_mask"] = [
            {"id": "MSK-1", "type": "mask", "name": "线性",
             "constant_material_id": "CM-1", "resource_type": "line",
             "config": {"width": 0.28}},
            {"id": "MSK-2", "type": "mask", "name": "矩形",
             "constant_material_id": "CM-2", "resource_type": "rectangle",
             "config": {"width": 0.28, "height": 0.5}}]
        s0 = d["tracks"][0]["segments"][0]
        s0["extra_material_refs"] = ["MSK-1"]
        s2 = d["tracks"][0]["segments"][2]
        s2["extra_material_refs"] = ["MSK-2"]
        s2["common_keyframes"] = [
            {"id": "KF-1", "material_id": "CM-2",
             "property_type": "KFTypeCommonMaskPositionX",
             "keyframe_list": [{"id": "K1", "values": [0.0]}]},
            {"id": "KF-2", "material_id": "CM-2",
             "property_type": "KFTypeCommonMaskPositionY",
             "keyframe_list": [{"id": "K2", "values": [0.0]}]}]

        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["placed"], 3)
        self.assertEqual(rep["exact"], 3)
        self.assertEqual(rep["timeline_end"], 360)
        self.assertEqual(rep["failed"], [])
        got = [(i.GetStart(), i.GetDuration())
               for i in self.resolve.project.current.GetItemListInTrack("video", 1)]
        self.assertEqual(got, plain_items, "蒙版影响了摆放")

        f = rep["features"]
        self.assertEqual(f["segments_with_mask"], 2)
        self.assertEqual(f["segments_with_mask_keyframes"], 1)
        self.assertEqual(f["mask_names"], ["线性", "矩形"])
        # 蒙版是「完全不送」的固定策略：报告要写清段数、明细和写回是否保留。
        m = rep["masks"]
        self.assertEqual(m["policy"], "not_sent")
        self.assertEqual(m["count"], 2)
        self.assertEqual(len(m["segments"]), 2)
        self.assertEqual(sorted(m["segments"][0]["mask_names"]), ["线性"])
        self.assertEqual(m["segments"][1]["mask_keyframes"], 2)
        self.assertIn("不会丢", m["note"])
        self.assertNotIn("mask_note", rep, "旧字段已由 masks 取代")
        feats = {r["feature"] for r in rep["not_sent"]}
        self.assertIn("mask", feats)
        self.assertIn("mask_keyframes", feats)

    def test_mask_reaches_snapshot(self):
        """快照必须带着蒙版：写回是从它克隆的，丢在这里就等于丢在剪映里。"""
        d = draft_video(2)
        d["materials"]["common_mask"] = [
            {"id": "MSK-1", "type": "mask", "name": "线性",
             "constant_material_id": "CM-1", "config": {"width": 0.28}}]
        d["tracks"][0]["segments"][1]["extra_material_refs"] = ["MSK-1"]
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        snap_dir = rep["snapshot_dir"] if "snapshot_dir" in rep else None
        if snap_dir is None:
            ptr = snapshot.current()
            snap_dir = ptr["snapshot_dir"]
        saved = json.loads((Path(snap_dir) / "draft_content.json")
                           .read_text(encoding="utf-8"))
        masks = (saved.get("materials") or {}).get("common_mask") or []
        self.assertEqual(len(masks), 1)
        self.assertEqual(masks[0]["name"], "线性")


class TestSendUnsent(SendBase):
    """送入报告里的「不送清单」：用户靠它判断达芬奇里能调到什么。"""

    def test_transform_reported_but_placement_unchanged(self):
        """剪映里放大过的段：达芬奇不接变换，但帧位一个都不能变。"""
        d = draft_video(2)
        d["tracks"][0]["segments"][0]["clip"] = {
            "scale": {"x": 1.49, "y": 1.49}, "transform": {"x": 0, "y": 0}}
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["exact"], 2)
        self.assertEqual(len(rep["transform_segments"]), 1)
        self.assertEqual(rep["transform_segments"][0]["transform"]["scale"],
                         [1.49, 1.49])
        got = [(i.GetStart(), i.GetDuration())
               for i in self.resolve.project.current.GetItemListInTrack("video", 1)]
        self.assertEqual(got, [(0, 120), (120, 120)])

    def test_unsent_list_has_text_and_sticker(self):
        d = draft_video(1)
        d["tracks"].append({"id": "T2", "type": "text",
                            "segments": [{"id": "TX1"}]})
        d["tracks"].append({"id": "T3", "type": "sticker",
                            "segments": [{"id": "ST1"}]})
        rep = self.run_send(d)
        self.assertTrue(rep["ok"], rep.get("error"))
        feats = {r["feature"]: r["count"] for r in rep["not_sent"]}
        self.assertEqual(feats.get("text"), 1)
        self.assertEqual(feats.get("sticker"), 1)
        # 文字/贴纸不进 plan，但视频段照常摆
        self.assertEqual(rep["placed"], 1)
        self.assertEqual(self.resolve.project.current.GetTrackCount("video"), 1)

    def test_masks_policy_is_never(self):
        """蒙版策略要明确是「完全不送」，不是「尝试失败」。"""
        d = draft_video(2)
        d["materials"]["common_mask"] = [
            {"id": "MSK-1", "name": "线性", "constant_material_id": "CM-1"}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["MSK-1"]
        rep = self.run_send(d)
        self.assertEqual(rep["masks"]["policy"], "not_sent")
        self.assertEqual(rep["masks"]["count"], 1)
        self.assertNotIn("mask_note", rep)


class TestTimelineNaming(unittest.TestCase):
    """建时间线的名字冲突处理 —— 第二轮送入必须还能过。

    真实场景：同一个工程里「送入 → 调色 → 写回」跑完，用户把片子又剪了一版
    再送一次。老实现直接 CreateEmptyTimeline，同名返回 None，报告只说
    「同名时间线可能还在」，第二条就再也送不进去。
    """

    def setUp(self):
        self.proj = FakeProject()
        self.mp = self.proj.pool

    def test_fresh_name(self):
        tl, used, tried, _notes = send.create_timeline(self.mp, self.proj, "JY · A")
        self.assertIsNotNone(tl)
        self.assertEqual(used, "JY · A")
        self.assertEqual(tried, ["JY · A"])

    def test_empty_same_name_reused(self):
        self.mp.CreateEmptyTimeline("JY · A")
        self.proj.current = None
        tl, used, _tried, notes = send.create_timeline(self.mp, self.proj, "JY · A")
        self.assertEqual(used, "JY · A")
        self.assertEqual(self.proj.GetTimelineCount(), 1,
                         "空的重名应该删掉复用，不该堆两条")
        self.assertTrue(any("清理了空时间线" in n for n in notes))

    def test_nonempty_same_name_gets_suffix(self):
        old = self.mp.CreateEmptyTimeline("JY · A")
        old.tracks["video"] = {1: [FakeClip(0, 10, None)]}
        tl, used, _tried, notes = send.create_timeline(self.mp, self.proj, "JY · A")
        self.assertEqual(used, "JY · A (2)")
        self.assertEqual(self.proj.GetTimelineCount(), 2, "旧时间线必须留着")
        self.assertTrue(any("已有 1 个片段" in n for n in notes))

    def test_never_overwrites_colored_timeline(self):
        """同名有内容时，宁可换名也不能动旧时间线 —— 那可能是已调色的成果。"""
        old = self.mp.CreateEmptyTimeline("JY · A")
        old.tracks["video"] = {1: [FakeClip(0, 10, None)]}
        send.create_timeline(self.mp, self.proj, "JY · A")
        self.assertIn(old, self.proj.timelines)
        self.assertEqual(old.GetName(), "JY · A")
        self.assertEqual(old.tracks["video"][1][0].dur, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
