"""多机位 / 合并片段：能不能送、能不能回。

背景
----
剪映侧没有「多机位」这个概念（画中画只是多轨叠加，本工具按轨逐段送入，
达芬奇里就是多条 V 轨，不需要特殊处理）。真正会出事的是**回程**：

    用户在达芬奇里把送进去的几段合成了一个多机位片段（或复合片段），
    原来那几段上的段号 marker 随旧 item 一起消失了。

老逻辑看到「没有身份标记」就判定时间线被改过，整条拒绝导出 —— 用户只知道
「写回失败」，不知道只是因为他做了多机位。这里锁死新行为：

  · 一个 item 正好盖住连续几段（起点、终点、中间都严丝合缝）→ 接受，
    这几段共用同一份调色；
  · 只盖住一段、又不像多机位 → 仍然拒绝（可能是新插入的片段，挂 LUT 会错画面）；
  · 盖得不完整、或与已识别的片段重叠 → 拒绝，并说清原因。

最后一条测试跑的是 `pull.run` 真代码：确认多机位只 ExportLUT 一次，
其余段复制同一份 cube，写回时每段仍有自己的文件。
"""
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from colorpass import pull

# ---------------------------------------------------------------- 假对象

class _MediaItem(object):
    def __init__(self, ctype):
        self.ctype = ctype

    def GetClipProperty(self, key):
        return self.ctype if key == "Type" else ""


class Item(object):
    """时间线片段。media_type='Multicam' 就是 21.1 建出来的多机位。"""

    def __init__(self, start, dur, sid=None, name="clip",
                 media_type=None, native_type=None):
        self.start, self.dur, self.sid, self.name = start, dur, sid, name
        self.media_type, self.native_type = media_type, native_type
        self.exports = 0

    def GetStart(self):
        return self.start

    def GetDuration(self):
        return self.dur

    def GetName(self):
        return self.name

    def GetMarkers(self):
        return {0: {"name": "CP", "customData": self.sid}} if self.sid else {}

    def GetMediaPoolItem(self):
        return _MediaItem(self.media_type) if self.media_type else None

    def GetType(self):
        if self.native_type is None:
            raise AttributeError("GetType 是 21.1 才有的")
        return self.native_type

    def GetClipEnabled(self):
        return True

    def ExportLUT(self, kind, path):
        self.exports += 1
        write_identity(path)
        return True


class Timeline(object):
    def __init__(self, items, name="CP · 草稿"):
        self.items, self.name = items, name

    def GetName(self):
        return self.name

    def GetTrackCount(self, kind):
        return 1 if kind == "video" else 0

    def GetItemListInTrack(self, kind, lane):
        return list(self.items) if kind == "video" and lane == 1 else []


def plan_for(specs, timeline="CP · 草稿", start_frame=0):
    return {"timeline": timeline, "start_frame": start_frame,
            "segments": [{"segment_id": sid, "lane": lane, "rec": rec,
                          "dur": dur} for sid, lane, rec, dur in specs]}


def write_identity(path):
    """写一份合法的 33³ 恒等 cube（顺序与 pull.at 的索引一致）。"""
    lines = ["LUT_3D_SIZE 33"]
    for b in range(33):
        for g in range(33):
            for r in range(33):
                lines.append("%.6f %.6f %.6f" % (r / 32.0, g / 32.0, b / 32.0))
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ================================================================ 对齐匹配

class MulticamMatchTest(unittest.TestCase):
    def test_multicam_covering_three_segments_accepted(self):
        tl = Timeline([Item(0, 360, name="机位合集 - Angle 1",
                            media_type="Multicam")])
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120),
                         ("C", 1, 240, 120)])
        issues, placed = pull.verify_alignment(tl, plan, 0)
        self.assertEqual(issues, [], issues)
        self.assertEqual(sorted(placed), [(1, 0), (1, 120), (1, 240)])
        groups = pull.coverage_groups(placed)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)
        self.assertTrue(groups[0]["multicam"])
        self.assertIn("Multicam", groups[0]["evidence"])

    def test_merged_clip_without_multicam_type_still_accepted(self):
        """复合片段转出来的多机位，媒体池类型未必写着 Multicam。
        只要它完整盖住 ≥2 段，就按合并片段处理并标注。"""
        tl = Timeline([Item(0, 240, name="合并片段")])
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120)])
        issues, placed = pull.verify_alignment(tl, plan, 0)
        self.assertEqual(issues, [])
        cov = pull.coverage_groups(placed)[0]
        self.assertFalse(cov["multicam"])
        self.assertEqual(cov["count"], 2)

    def test_single_segment_multicam_allowed(self):
        tl = Timeline([Item(0, 120, name="一段多机位", media_type="Multicam")])
        plan = plan_for([("A", 1, 0, 120)])
        issues, placed = pull.verify_alignment(tl, plan, 0)
        self.assertEqual(issues, [])
        self.assertEqual(len(pull.coverage_groups(placed)), 1)

    def test_single_unmarked_normal_clip_rejected(self):
        """单段、不像多机位：仍按新插入片段处理，不能挂 LUT。"""
        tl = Timeline([Item(0, 120, name="随手拖进来的")])
        plan = plan_for([("A", 1, 0, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("身份标记" in i["why"] for i in issues), issues)

    def test_partial_cover_rejected(self):
        tl = Timeline([Item(0, 300, name="差 60 帧", media_type="Multicam")])
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120),
                         ("C", 1, 240, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("对不上" in i["why"] for i in issues), issues)

    def test_cover_overlapping_marked_segment_rejected(self):
        # 多机位从 0 开始盖住 A+B，但 A 上还留着段号：说明它不是替换，
        # 而是叠加上来的，这时分不清该听谁的 —— 拒绝，不赌。
        tl = Timeline([Item(0, 120, sid="A"),
                       Item(0, 240, name="盖住 A+B", media_type="Multicam")])
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120),
                         ("C", 1, 240, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("重叠" in i["why"] for i in issues), issues)

    def test_mixed_marked_and_covered(self):
        tl = Timeline([Item(0, 120, sid="A"),
                       Item(120, 240, name="机位 - Angle 2",
                            media_type="Multicam")])
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120),
                         ("C", 1, 240, 120)])
        issues, placed = pull.verify_alignment(tl, plan, 0)
        self.assertEqual(issues, [])
        self.assertEqual(len(placed), 3)
        self.assertEqual(len(pull.coverage_groups(placed)), 1)


# ================================================================ 取回真链路

class _MP(object):
    """只带多机位能力的媒体池，够 coverage_capable 探测用。"""

    def CreateMulticamClip(self, name, clips):
        return clips[0] if clips else None


class _Proj(object):
    def __init__(self, tl):
        self.tl = tl

    def GetCurrentTimeline(self):
        return self.tl

    def GetMediaPool(self):
        return _MP()

    def GetSetting(self, key):
        return {"colorScience": "DaVinci YRGB"}.get(key)


class _App(object):
    def __init__(self, proj):
        self.proj = proj

    def GetCurrentPage(self):
        return "color"

    def GetProjectManager(self):
        return types.SimpleNamespace(GetCurrentProject=lambda: self.proj)


class MulticamPullTest(unittest.TestCase):
    """多机位只导一次 LUT，其余段复制同一份；manifest 一段一条。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp_mc_")
        self.run_dir = os.path.join(self.tmp, "run")
        self.lut_dir = os.path.join(self.tmp, "luts")
        os.makedirs(self.run_dir)
        os.makedirs(self.lut_dir)
        self.plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120),
                              ("C", 1, 240, 120)])
        self.item = Item(0, 360, name="机位合集 - Angle 1",
                         media_type="Multicam")
        self.tl = Timeline([self.item])
        Path(self.run_dir, "plan.json").write_text(
            json.dumps({"draft_id": "D1", "start_frame": 0, **self.plan}),
            encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_export_three_cubes(self):
        ptr = {"draft_id": "D1", "draft_name": "草稿A", "run_dir": self.run_dir,
               "timeline": "CP · 草稿", "snapshot_at": "2026-11-21 00:00"}
        from colorpass import snapshot as snap_mod
        app = _App(_Proj(self.tl))
        with mock.patch.object(pull, "dvr",
                               types.SimpleNamespace(scriptapp=lambda n: app)), \
             mock.patch.object(snap_mod, "current", return_value=ptr), \
             mock.patch.object(pull.paths, "draft_run_dir",
                               return_value=self.run_dir), \
             mock.patch.object(pull.paths, "draft_lut_dir",
                               return_value=self.lut_dir), \
             mock.patch.object(pull.compat, "ensure_page", return_value=True), \
             mock.patch.object(pull.compat, "lut_type", return_value="33PTCube"), \
             mock.patch.object(pull.time, "sleep"):
            rep = pull.run()

        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["count"], 3)
        self.assertEqual(self.item.exports, 1, "多机位只该导出一次 LUT")
        self.assertEqual(rep["coverage"][0]["count"], 3)
        self.assertTrue(rep["coverage"][0]["multicam"])
        self.assertIn("共用同一份调色", rep.get("coverage_note", ""))

        # 三段各有自己的 cube，内容一致
        files = [Path(r["cube"]) for r in rep["items"]]
        self.assertEqual(len(files), 3)
        self.assertTrue(all(f.exists() for f in files))
        self.assertEqual(len({f.stat().st_size for f in files}), 1)
        self.assertEqual(sorted(rep["segment_luts"]), ["A", "B", "C"])
        shared = [r for r in rep["items"] if r.get("shared_with")]
        self.assertEqual(len(shared), 2)

        man = json.loads(Path(self.run_dir, "manifest.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(len(man["cubes"]), 3)


# ================================================================ 自检命令

class MulticamCommandTest(unittest.TestCase):
    def setUp(self):
        from colorpass import multicam, snapshot
        self.mc = multicam
        self.snap = snapshot

    def _run(self, items=(), current=None, export=False, no_timeline=False):
        tl = None if no_timeline else Timeline(list(items))
        app = _App(_Proj(tl))
        with mock.patch.object(self.mc, "dvr",
                               types.SimpleNamespace(scriptapp=lambda n: app)), \
             mock.patch.object(self.snap, "current",
                               return_value=current), \
             mock.patch.object(self.mc.compat, "ensure_page", return_value=True), \
             mock.patch.object(self.mc.compat, "lut_type", return_value="33PTCube"), \
             mock.patch.object(self.mc.time, "sleep"):
            return self.mc.run(export=export)

    def test_reports_multicam_item(self):
        rep = self._run([Item(0, 360, name="机位 - Angle 1",
                              media_type="Multicam"),
                         Item(360, 120, sid="D", name="普通段")])
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["count"], 2)
        self.assertEqual(rep["multicam"], 1)
        self.assertEqual(rep["plan_note"], "还没有完成过一次「送入达芬奇」，没有可对照的计划")
        txt = self.mc.text(rep)
        self.assertIn("多机位 1 个", txt)
        self.assertIn("没有段号", txt)

    def test_export_measures_cube(self):
        rep = self._run([Item(0, 240, name="合并", media_type="Multicam")],
                        export=True)
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(len(rep["exports"]), 1)
        self.assertTrue(rep["exports"][0]["ok"], rep["exports"])
        self.assertEqual(rep["exports"][0]["identity_dev_max"], 0.0)

    def test_no_timeline_is_an_error(self):
        rep = self._run(no_timeline=True)
        self.assertFalse(rep["ok"])
        self.assertIn("没有时间线", rep["error"])

    def test_plan_match_accepts_coverage(self):
        tmp = tempfile.mkdtemp(prefix="cp_mc_cmd_")
        try:
            run_dir = os.path.join(tmp, "run")
            os.makedirs(run_dir)
            plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120)])
            Path(run_dir, "plan.json").write_text(
                json.dumps({"draft_id": "D1", "start_frame": 0, **plan}),
                encoding="utf-8")
            rep = self._run(
                [Item(0, 240, name="机位 - Angle 1", media_type="Multicam")],
                current={"draft_id": "D1", "draft_name": "草稿A",
                         "run_dir": run_dir, "timeline": "CP · 草稿"})
            self.assertTrue(rep["ok"], rep.get("error"))
            self.assertTrue(rep["plan"]["ok"], rep["plan"]["issues"])
            self.assertEqual(rep["plan"]["coverage"][0]["count"], 2)
            self.assertIn("可写回", self.mc.text(rep))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
