"""ColorPass 集成测试 —— 走真代码路径，只把两个宿主换成假的。

为什么要有这一层
----------------
`test_units.py` 测的是纯函数；而实际出过的两次事故都在**串起来之后**：
  · pull.json(14 段) 配 plan.json(13 段) → orphan，写回把 LUT 挂到不存在的段；
  · 恒等自检把手调过的段当成「数据坏了」，永久拒绝写回。

所以这里用 FakeResolve / FakeTimeline / FakeItem 顶替达芬奇脚本接口，
用临时目录顶替剪映草稿目录，让 `pull.verify_alignment`、`write.run`、
`preflight.daVinci` 这些**真函数**跑完整流程，断言它们的判定与报错。

不依赖达芬奇、不依赖剪映、不碰任何真实草稿（CURRENT/PENDING 指到临时目录）。

    <Python312> -m unittest discover -s tests -v
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import cli, draftio, paths, preflight, pull, snapshot, write  # noqa: E402

# ================================================================ 夹具

def identity_cube(path, n=33):
    """写一份真正的 33³ 恒等 cube —— verify_cubes 会逐行解析，糊弄不过去。"""
    lines = ["LUT_3D_SIZE %d" % n]
    for b in range(n):
        for g in range(n):
            for r in range(n):
                lines.append("%.6f %.6f %.6f"
                             % (r / (n - 1.0), g / (n - 1.0), b / (n - 1.0)))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return str(path)


class FakeItem(object):
    """时间线上的一个片段。marker 里放 segment_id，和 send.mark_item 一致。"""

    def __init__(self, start, dur, sid=None, name="clip"):
        self._start, self._dur, self._sid, self._name = start, dur, sid, name

    def GetStart(self):
        return self._start

    def GetDuration(self):
        return self._dur

    def GetName(self):
        return self._name

    def GetMarkers(self):
        return {0: {"name": "CP", "customData": self._sid}} if self._sid else {}

    def GetMediaPoolItem(self):
        return None


class FakeTimeline(object):
    def __init__(self, name="CP · 草稿", tracks=None):
        self._name = name
        self.tracks = tracks or {}

    def GetName(self):
        return self._name

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "video" else 0

    def GetItemListInTrack(self, kind, lane):
        return list(self.tracks.get(lane) or [])


class FakeProject(object):
    def __init__(self, name="空白工程", timelines=(), settings=None,
                 product="DaVinci Resolve Studio", pool=None):
        self._name = name
        self._timelines = list(timelines)
        self._settings = settings or {"timelineFrameRate": "30",
                                      "timelineResolutionWidth": "1920",
                                      "timelineResolutionHeight": "1080"}
        self._product = product
        self._pool = pool

    def GetName(self):
        return self._name

    def GetTimelineCount(self):
        return len(self._timelines)

    def GetTimelineByIndex(self, i):
        return FakeTimeline(self._timelines[i - 1])

    def GetSetting(self, key):
        return self._settings.get(key)

    def GetMediaPool(self):
        return self._pool


class FakePool(object):
    def __init__(self, clips=()):
        self._clips = list(clips)

    def GetRootFolder(self):
        outer = self

        class _Folder(object):
            def GetClipList(self):
                return outer._clips

            def GetSubFolderList(self):
                return []
        return _Folder()


class FakeResolve(object):
    def __init__(self, project=None, product="DaVinci Resolve Studio",
                 page="edit"):
        self._project = project
        self._product = product
        # 真达芬奇停在项目管理器时 GetCurrentPage() 返回 None。
        # daVinci 靠它区分「真的进了工程」和「幻影工程」。
        self._page = page
        self.created = []

    def GetProductName(self):
        return self._product

    def GetVersionString(self):
        return "20.3.0.10"

    def GetCurrentPage(self):
        return self._page

    def GetProjectManager(self):
        outer = self

        class _PM(object):
            def GetCurrentProject(self):
                return outer._project

            def GetProjectListInCurrentFolder(self):
                return ["空白工程", "我的片子"]

            def CreateProject(self, name):
                outer.created.append(name)
                outer._project = FakeProject(name=name, timelines=[],
                                              pool=FakePool())
                outer._page = "color"
                return outer._project
        return _PM()


def plan_for(specs, timeline="CP · 草稿", start_frame=0, draft_id="D1"):
    """按 [(segment_id, lane, rec, dur)] 造 plan.json 的结构。"""
    return {
        "timeline": timeline, "fps": 30, "start_frame": start_frame,
        "draft_id": draft_id, "draft_name": "草稿A", "speed_mode": "slot",
        "segments": [{"segment_id": s, "lane": lane, "kind": "video",
                      "rec": rec, "dur": dur, "want": dur, "speed": 1.0,
                      "items": []}
                     for (s, lane, rec, dur) in specs],
    }


def draft_with_effects(n=3, dur_us=4_000_000, sid_prefix="SEG"):
    """n 段首尾相接的视频草稿，素材用解释器本体（保证 path 存在）。"""
    path = sys.executable.replace("\\", "/")
    segs, vids = [], []
    for i in range(n):
        segs.append({
            "id": "%s-%02d" % (sid_prefix, i),
            "material_id": "VID-%02d" % i,
            "extra_material_refs": [],
            "common_keyframes": [],
            "source_timerange": {"start": 0, "duration": dur_us},
            "target_timerange": {"start": i * dur_us, "duration": dur_us},
        })
        vids.append({"id": "VID-%02d" % i, "path": path, "duration": dur_us})
    return {
        "id": "D1",
        "duration": n * dur_us,
        "materials": {"videos": vids, "speeds": [], "drafts": [],
                      "transitions": [], "effects": []},
        "tracks": [{"id": "T1", "type": "video", "segments": segs}],
    }


def with_masks(d, spec=((0, "线性", False), (2, "矩形", True))):
    """给部分段挂蒙版（剪映 11.x 的写法：materials.common_mask + 段的 refs）。

    用户 2026-11-14 真实加的就是这种：一段线性、一段矩形，矩形那段还带
    KFTypeCommonMaskPositionX/Y 关键帧。
    """
    masks = []
    for i, (idx, name, kf) in enumerate(spec):
        mid, cid = "MSK-%02d" % i, "CM-%02d" % i
        masks.append({"id": mid, "type": "mask", "name": name,
                      "constant_material_id": cid,
                      "resource_type": "line", "config": {"width": 0.28}})
        seg = d["tracks"][0]["segments"][idx]
        seg["extra_material_refs"] = list(seg.get("extra_material_refs") or []) + [mid]
        if kf:
            seg["common_keyframes"] = [{
                "id": "KF-%02d" % i, "material_id": cid,
                "property_type": "KFTypeCommonMaskPositionX",
                "keyframe_list": [{"id": "K-%02d" % i, "values": [0.0]}]}]
    d["materials"]["common_mask"] = masks
    return d


def draft_rich():
    """变速 + 转场 + 淡入淡出 + 蒙版 + 关键帧 + 画中画轨 + 独立特效轨 + 非 LUT 特效。

    照 CutConvert / Free Forever 公布的 carry-over 清单造的：这三家能带过去的
    那几项（速度、主轨转场、淡入淡出、位移关键帧），写回后必须一段不差。
    """
    d = draft_with_effects(2)
    segs = d["tracks"][0]["segments"]
    segs[0]["extra_material_refs"] = ["SP-1", "TR-1", "AF-1", "MSK-1"]
    segs[0]["common_keyframes"] = [{
        "id": "KF1", "property_type": "KFTypePositionX",
        "keyframe_list": [{"id": "K1", "values": [0.0]}]}]
    # 变速：source 比 target 长一倍
    segs[0]["source_timerange"] = {"start": 0, "duration": segs[0][
        "target_timerange"]["duration"] * 2}
    d["materials"].update({
        "speeds": [{"id": "SP-1", "speed": 2.0}],
        "transitions": [{"id": "TR-1", "name": "叠化", "duration": 500000}],
        "audio_fades": [{"id": "AF-1", "fade_in": 100000, "fade_out": 0}],
        "common_mask": [{"id": "MSK-1", "name": "线性", "resource_type": "line"}],
        "effects": [{"id": "EFF-N", "path": "%s;;pkg" % sys.executable.replace(
            "\\", "/"), "name": "滤镜"}],
    })
    d["tracks"].append({"id": "T2", "type": "video", "segments": [{
        "id": "PIP-1", "material_id": "VID-00",
        "target_timerange": {"start": 0, "duration": 1_000_000},
        "source_timerange": {"start": 0, "duration": 1_000_000},
        "extra_material_refs": [], "common_keyframes": []}]})
    d["tracks"].append({"id": "T3", "type": "effect", "segments": [{
        "id": "FX-1", "material_id": "EFF-N",
        "target_timerange": {"start": 0, "duration": 2_000_000},
        "source_timerange": {"start": 0, "duration": 2_000_000},
        "extra_material_refs": [], "common_keyframes": []}]})
    return d


# ================================================================ 对齐校验

class TestVerifyAlignment(unittest.TestCase):
    """verify_alignment 是写回前的唯一硬闸：它漏判 = LUT 挂错画面。"""

    def test_exact_match_passes(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 120, "A"),
                                      FakeItem(120, 120, "B")]})
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120)])
        issues, placed = pull.verify_alignment(tl, plan, 0)
        self.assertEqual(issues, [])
        self.assertEqual(len(placed), 2)

    def test_pad_attaches_to_main(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 119, "A"),
                                      FakeItem(119, 1, "A" + paths.PAD_SUFFIX)]})
        plan = plan_for([("A", 1, 0, 120)])
        issues, placed = pull.verify_alignment(tl, plan, 0)
        self.assertEqual(issues, [])
        ent = placed[(1, 0)]
        self.assertEqual(ent["total"], 120)          # 主段 119 + 尾巴 1
        self.assertEqual(len(ent["items"]), 2)

    def test_missing_segment_detected(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 120, "A")]})
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("不存在了" in i["why"] for i in issues))

    def test_shifted_segment_detected(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 120, "A"),
                                      FakeItem(140, 120, "B")]})
        plan = plan_for([("A", 1, 0, 120), ("B", 1, 120, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("位置与计划不符" in i["why"] for i in issues))

    def test_unmarked_item_detected(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 120, "A"),
                                      FakeItem(120, 120, None)]})
        plan = plan_for([("A", 1, 0, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("身份标记" in i["why"] for i in issues))

    def test_duration_changed_detected(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 118, "A")]})
        plan = plan_for([("A", 1, 0, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("时长变了" in i["why"] for i in issues))

    def test_wrong_timeline_detected(self):
        tl = FakeTimeline("别的工程", tracks={1: [FakeItem(0, 120, "A")]})
        plan = plan_for([("A", 1, 0, 120)])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("不是计划里的那条" in i["why"] for i in issues))

    def test_start_frame_offset_respected(self):
        # 达芬奇起始帧不是 0 时，比对必须减掉 base，否则整条时间线都「偏移」
        tl = FakeTimeline(tracks={1: [FakeItem(1040, 120, "A")]})
        plan = plan_for([("A", 1, 0, 120)], start_frame=1040)
        issues, _ = pull.verify_alignment(tl, plan, 1040)
        self.assertEqual(issues, [])

    def test_pad_without_main_detected(self):
        tl = FakeTimeline(tracks={1: [FakeItem(0, 1, "ZZ" + paths.PAD_SUFFIX)]})
        plan = plan_for([])
        issues, _ = pull.verify_alignment(tl, plan, 0)
        self.assertTrue(any("找不到对应主段" in i["why"] for i in issues))


# ================================================================ 写回全链路

class TestWriteChain(unittest.TestCase):
    """write.run 走真代码：快照 → 闸门 → 嵌入 cube → 逐段核对。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jyb_write_")
        self._cur = snapshot.CURRENT_PATH
        self._pend = snapshot.PENDING_PATH
        snapshot.CURRENT_PATH = os.path.join(self.tmp, "current.json")
        snapshot.PENDING_PATH = os.path.join(self.tmp, "pending.json")
        self.run_dir = os.path.join(self.tmp, "runs", "D1")
        os.makedirs(self.run_dir, exist_ok=True)
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src, exist_ok=True)
        self.lut_dir = Path(self.tmp) / "luts"
        self.lut_dir.mkdir()
        self.new_root = Path(self.tmp) / "newdrafts"
        self.new_root.mkdir()
        self.fail_write_back = False

        # 剪映侧不参与：drift 报告那一步直接当作「读不到内存」
        p1 = mock.patch.object(draftio, "read",
                               side_effect=draftio.NoDraft("测试里没有剪映"))
        p2 = mock.patch.object(write.WF, "write_back", self._fake_write_back)
        self.patches = [p1, p2]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        snapshot.CURRENT_PATH = self._cur
        snapshot.PENDING_PATH = self._pend
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 造一次「送入」 ----
    def seed(self, draft=None, committed=True, timeline="CP · 草稿", segments=None):
        draft = draft or draft_with_effects(3)
        meta = snapshot.save(draft, {"name": "草稿A", "folder": self.src},
                             self.run_dir)
        snapshot.claim(meta, self.run_dir, timeline, 0,
                       segments if segments is not None else 3)
        if committed:
            snapshot.commit(placed=3)
        return draft

    def cubes_for(self, draft, skip=(), bad=()):
        out = {}
        for tr in draft.get("tracks") or []:
            for sg in tr.get("segments") or []:
                sid = sg["id"]
                if sid in skip:
                    continue
                p = self.lut_dir / ("%s.cube" % sid)
                if sid in bad:
                    Path(p).write_text("LUT_3D_SIZE 33\n0 0 0\n", encoding="utf-8")
                else:
                    identity_cube(p)
                out[sid] = str(p)
        return out

    def canned_pull(self, luts, alignment=True):
        return {"ok": True, "count": len(luts), "segment_luts": luts,
                "alignment_ok": alignment, "identity_dev_max": 0.0,
                "identity_ok": True, "identity_graded": 0, "issues": [],
                "timeline": "CP · 草稿"}

    def _fake_write_back(self, clips, luts, draft=None, segment_luts=None,
                         project=None, name=None, **kw):
        """模拟 jy_draft.build_from_draft：克隆快照 + 给点名的段挂 LUT。

        命名规则走真函数 `WF.safe_name`（同一个实现，不另写一份），
        这样「用户填了非法字符」这条也能在这里被验到。
        """
        if self.fail_write_back:
            return {"ok": False, "error": "模拟克隆失败"}
        d = copy.deepcopy(draft)
        effs = d["materials"].setdefault("effects", [])
        for sid, cube in (segment_luts or {}).items():
            eid = "EFF-%s" % sid
            effs.append({"id": eid, "path": "%s;;effect_pkg" % cube,
                         "name": Path(cube).name})
            for tr in d.get("tracks") or []:
                for sg in tr.get("segments") or []:
                    if sg.get("id") == sid:
                        sg.setdefault("extra_material_refs", []).append(eid)
        base = write.WF.safe_name(name) or ("%s_调色" % (project or "PB"))
        out = self.new_root / base
        n = 2
        while out.exists():
            out = self.new_root / ("%s_%d" % (base, n))
            n += 1
        out.mkdir(parents=True, exist_ok=True)
        (out / "draft_content.json").write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "name": out.name, "path": str(out), "dir": str(out),
                "mode": "clone", "luts": len(segment_luts or {})}

    def test_disabled_segment_gets_no_lut_and_stays_disabled(self):
        # 停用段在 pull 里被跳过（达芬奇里是禁用片段，没人调它），所以没有
        # cube。写回不能因此判失败，new 草稿还得原样保持 visible:false。
        draft = draft_with_effects(3)
        draft["tracks"][0]["segments"][1]["visible"] = False
        draft = self.seed(draft)
        luts = self.cubes_for(draft, skip=("SEG-01",))
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(luts)):
            rep = write.run(new_name="停用段")
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["luts"], 2)
        self.assertEqual(rep["lut_ok"], 2)
        self.assertEqual(rep["problems"], [])
        self.assertEqual(rep["structure_problems"], [])
        nd = json.loads((Path(rep["new_draft"]) / "draft_content.json")
                        .read_text(encoding="utf-8"))
        sg = next(s for s in nd["tracks"][0]["segments"] if s["id"] == "SEG-01")
        self.assertIs(sg.get("visible"), False)

    # ---- 正常路径 ----
    def test_full_chain_ok(self):
        draft = self.seed()
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))):
            rep = write.run(new_name="我的成片")
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["luts"], 3)
        self.assertEqual(rep["paths_swapped"], 3)
        self.assertEqual(rep["segments"], 3)
        self.assertEqual(rep["lut_ok"], 3)
        self.assertEqual(rep["timeline_unchanged"], 3)
        self.assertEqual(rep["keyframes_unchanged"], 3)
        self.assertEqual(rep["duration_src"], rep["duration_new"])
        self.assertEqual(rep["draft_name"], "我的成片")
        # cube 必须复制进新草稿，而不是继续指向临时 LUT 目录
        emb = Path(rep["new_draft"]) / paths.EMBED_SUBDIR
        self.assertEqual(len(list(emb.glob("*.cube"))), 3)
        nd = json.loads((Path(rep["new_draft"]) / "draft_content.json")
                        .read_text(encoding="utf-8"))
        for e in nd["materials"]["effects"]:
            self.assertTrue(str(e["path"]).startswith(str(emb).replace("\\", "/")),
                            e["path"])

    def test_default_name_from_snapshot(self):
        draft = self.seed()
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))):
            rep = write.run()
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["draft_name"], "草稿A_调色")

    def test_illegal_name_sanitized(self):
        draft = self.seed()
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))):
            rep = write.run(new_name='a/b:c*?"<>|d')
        self.assertTrue(rep["requested_name"])          # 原样记录用户意图
        self.assertNotIn("/", rep["draft_name"])
        self.assertNotIn(":", rep["draft_name"])

    # ---- 各类拒绝 ----
    def test_no_pointer_refused(self):
        rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertIn("送入达芬奇", rep["error"])

    def test_incomplete_send_refused(self):
        self.seed(committed=False)
        rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertIn("没有走完", rep["error"])

    def test_alignment_false_refused(self):
        draft = self.seed()
        with mock.patch.object(
                pull, "run",
                return_value=self.canned_pull(self.cubes_for(draft), alignment=False)):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertIn("时间线与计划不一致", rep["error"])

    def test_orphan_segment_refused(self):
        draft = self.seed()
        luts = self.cubes_for(draft)
        luts["GHOST-99"] = next(iter(luts.values()))
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(luts)):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["orphan"], ["GHOST-99"])
        self.assertIn("不是同一次送入", rep["error"])

    def test_bad_cube_refused(self):
        draft = self.seed()
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(
                                   self.cubes_for(draft, bad=("SEG-01",)))):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["cube_problems"])
        self.assertIn("cube 不可用", rep["error"])

    def test_missing_cube_file_refused(self):
        draft = self.seed()
        luts = self.cubes_for(draft)
        luts["SEG-00"] = str(self.lut_dir / "gone.cube")
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(luts)):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["cube_problems"][0]["why"], "文件不存在")

    def test_clone_failure_refused(self):
        draft = self.seed()
        self.fail_write_back = True
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertIn("模拟克隆失败", rep["error"])

    def test_write_back_exception_is_reported(self):
        """写草稿时抛异常（权限/磁盘满）不能变成 traceback 甩给用户。"""
        draft = self.seed()
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))), \
                mock.patch.object(write.WF, "write_back",
                                  side_effect=OSError("磁盘已满")):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertIn("磁盘已满", rep["error"])

    def test_embed_failure_is_reported(self):
        draft = self.seed()
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))), \
                mock.patch.object(write, "embed_cubes",
                                  side_effect=OSError("目标目录只读")):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertIn("目标目录只读", rep["error"])

    # ---- --no-pull 排错模式 ----
    def test_no_pull_without_plan_refused(self):
        self.seed()
        rep = write.run(auto_pull=False)
        self.assertFalse(rep["ok"])
        self.assertIn("缺少 pull.json", rep["error"])

    def test_no_pull_plan_missing_gives_clear_error(self):
        """pull.json 在、plan.json 不在 —— 以前这里直接抛 FileNotFoundError。"""
        self.seed()
        (Path(self.run_dir) / "pull.json").write_text(
            json.dumps({"plan_sha": "abc", "alignment_ok": True}),
            encoding="utf-8")
        rep = write.run(auto_pull=False)
        self.assertFalse(rep["ok"])
        self.assertIn("plan.json", rep["error"])

    def test_no_pull_stale_pull_refused(self):
        draft = self.seed()
        (Path(self.run_dir) / "plan.json").write_text(
            json.dumps({"segments": []}), encoding="utf-8")
        (Path(self.run_dir) / "pull.json").write_text(json.dumps({
            "plan_sha": "deadbeefdeadbeef", "alignment_ok": True,
            "segment_luts": self.cubes_for(draft)}), encoding="utf-8")
        rep = write.run(auto_pull=False)
        self.assertFalse(rep["ok"])
        self.assertIn("上一版计划", rep["error"])

    # ---- 蒙版：写回必须原样带回 ----
    def test_masks_survive_write_back(self):
        """新草稿是从送入快照克隆的，蒙版/蒙版关键帧必须一段不差。

        2026-11-14 场景：用户在剪映里给两段加了蒙版（其中一段蒙版带关键帧），
        写回后这两段的蒙版不能少、也不能多。
        """
        draft = self.seed(with_masks(draft_with_effects(3)))
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))):
            rep = write.run()
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["segments"], 3)
        self.assertEqual(rep["mask_segments"], 2)
        self.assertEqual(rep["masks_unchanged"], 3, rep.get("problems"))
        self.assertEqual(rep["keyframes_unchanged"], 3)
        self.assertEqual(rep["problems"], [])

    # ---- 全轨结构：变速/转场/淡入淡出/画中画/特效轨 ----
    def test_rich_draft_survives_write_back(self):
        """照竞品 carry-over 清单逐项验：速度、主轨转场、淡入淡出、位置关键帧、
        画中画轨、独立特效轨。克隆模式下一项都不许丢。"""
        draft = self.seed(draft_rich())
        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))):
            rep = write.run()
        self.assertTrue(rep["ok"], rep.get("error"))
        self.assertEqual(rep["structure_problems"], [])
        kept = rep["kept"]
        self.assertEqual(kept["speed_segments"], 1)
        self.assertEqual(kept["fade_segments"], 1)
        self.assertEqual(kept["transitions"], 1)
        self.assertEqual(kept["masks"], 1)
        self.assertEqual(kept["keyframe_segments"], 1)
        self.assertEqual(kept["overlay_tracks"], 1)
        self.assertEqual(kept["effects"], 1)

    def test_silent_speed_loss_is_refused(self):
        """克隆如果悄悄丢了 speeds 引用：timerange 没变、旧断言全过，
        画面却回 1 倍速 —— LUT 就挂错了画面。必须判失败。"""
        draft = self.seed(draft_rich())
        orig = self._fake_write_back

        def losing(clips, luts, draft=None, segment_luts=None, project=None,
                   name=None, **kw):
            r = orig(clips, luts, draft=draft, segment_luts=segment_luts,
                     project=project, name=name, **kw)
            f = Path(r["path"]) / "draft_content.json"
            d = json.loads(f.read_text(encoding="utf-8"))
            for tr in d.get("tracks") or []:
                for sg in tr.get("segments") or []:
                    sg["extra_material_refs"] = [
                        x for x in (sg.get("extra_material_refs") or [])
                        if x != "SP-1"]
            f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            return r

        with mock.patch.object(pull, "run",
                               return_value=self.canned_pull(self.cubes_for(draft))), \
                mock.patch.object(write.WF, "write_back", losing):
            rep = write.run()
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["structure_problems"])
        self.assertIn("结构对不上", rep["error"])


# ================================================================ 闸门

class TestPreflightGate(unittest.TestCase):
    """闸门判错 = 要么写坏用户工程，要么永远进不去。两种都发生过。"""

    def gate(self, project, product="DaVinci Resolve Studio", blank=True):
        r = FakeResolve(project, product)
        with mock.patch.object(preflight, "resolve_app", return_value=r):
            return preflight.daVinci(require_blank=blank)

    def test_blank_project_passes(self):
        proj = FakeProject(timelines=[], pool=FakePool())
        r, p, info = self.gate(proj)
        self.assertEqual(info["timeline_count"], 0)
        self.assertEqual(info["project"], "空白工程")

    def test_non_blank_refused(self):
        proj = FakeProject(timelines=["我的片子"], pool=FakePool())
        with self.assertRaises(preflight.Gate) as cm:
            self.gate(proj)
        self.assertEqual(cm.exception.code, "not_blank")
        self.assertIn("我的片子", cm.exception.msg)

    def test_own_timeline_allowed_now(self):
        """上一轮自己建的 `CP · …` 不再拦：send 会换名，不会覆盖旧成果。

        原来一刀切要求工程全空，导致第二轮送入必然失败 —— 用户每次都得
        手动新建工程，这就是「送入不顺」的主因。
        """
        proj = FakeProject(timelines=["CP · 草稿A"], pool=FakePool())
        r, p, info = self.gate(proj)
        self.assertEqual(info["own_timelines"], ["CP · 草稿A"])
        self.assertEqual(info["foreign_timelines"], [])

    def test_legacy_jy_timeline_still_owned(self):
        """改名前建的 `JY · …` 也算自己的，升级后老工程不该被闸门拦下。"""
        proj = FakeProject(timelines=["JY · 草稿A", "CP · 草稿B"], pool=FakePool())
        r, p, info = self.gate(proj)
        self.assertEqual(info["own_timelines"], ["JY · 草稿A", "CP · 草稿B"])
        self.assertEqual(info["foreign_timelines"], [])

    def test_fps_mismatch_refused_when_own_timeline_exists(self):
        """工程里有自己的时间线、帧率却被人改成 24：不许改回去。"""
        proj = FakeProject(timelines=["CP · 草稿A"], pool=FakePool(),
                           settings={"timelineFrameRate": "24",
                                     "timelineResolutionWidth": "1920",
                                     "timelineResolutionHeight": "1080"})
        with self.assertRaises(preflight.Gate) as cm:
            self.gate(proj)
        self.assertEqual(cm.exception.code, "fps_mismatch")

    def test_project_manager_auto_creates_blank_project(self):
        """停在项目管理器（page=None）时自动新建空白工程，别让用户卡在这。"""
        r = FakeResolve(None, page=None)
        with mock.patch.object(preflight, "resolve_app", return_value=r), \
                mock.patch.object(preflight.time, "sleep", return_value=None):
            _, proj, info = preflight.daVinci(require_blank=True)
        self.assertEqual(info["page"], "color")
        self.assertEqual(info["project"], "ColorPass")
        self.assertTrue(r.created)

    def test_project_manager_without_auto_is_a_gate(self):
        r = FakeResolve(None, page=None)
        with mock.patch.object(preflight, "resolve_app", return_value=r), \
                mock.patch.object(preflight.time, "sleep", return_value=None):
            with self.assertRaises(preflight.Gate) as cm:
                preflight.daVinci(require_blank=True, auto_project=False)
        self.assertEqual(cm.exception.code, "project_manager")
        self.assertIn("newproj", cm.exception.msg)

    def test_non_blank_allowed_when_not_required(self):
        proj = FakeProject(timelines=["我的片子"], pool=FakePool())
        r, p, info = self.gate(proj, blank=False)
        self.assertEqual(info["timeline_count"], 1)

    def test_free_version_refused(self):
        proj = FakeProject(timelines=[], pool=FakePool())
        with self.assertRaises(preflight.Gate) as cm:
            self.gate(proj, product="DaVinci Resolve")
        self.assertEqual(cm.exception.code, "needs_studio")

    def test_no_project_refused(self):
        with self.assertRaises(preflight.Gate) as cm:
            self.gate(None)
        self.assertEqual(cm.exception.code, "no_project")

    def test_no_resolve_process_fails_fast(self):
        """达芬奇没开时要立刻失败，不能先重试 24 秒。"""
        with mock.patch.object(preflight, "_tasklist_pids", return_value=set()):
            t0 = time.time()
            with self.assertRaises(preflight.Gate) as cm:
                preflight.resolve_app()
            self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(cm.exception.code, "no_resolve")
        self.assertIn("没有在运行", cm.exception.msg)

    # ---- 草稿「打开后又被编辑过」的直接闸门（不扫内存） ----
    def _jy_folder(self, edit_offset):
        """造一个最小草稿目录：.locked 是打开时刻，draft_settings 记编辑时刻。"""
        d = tempfile.mkdtemp(prefix="jyb_jy_")
        self.addCleanup(shutil.rmtree, d, True)
        lock = Path(d, ".locked")
        lock.write_bytes(b"")
        now = time.time()
        Path(d, "draft_settings").write_text(
            "[General]\ndraft_last_edit_time=%d\n" % int(now + edit_offset),
            encoding="utf-8")
        return d

    def test_gate_blocks_edited_since_open(self):
        d = self._jy_folder(edit_offset=+60)
        cur = {"name": "测试草稿", "timeline_id": "T1", "folder": d}
        with mock.patch("colorpass.draftio.identify", return_value=cur):
            with self.assertRaises(preflight.Gate) as cm:
                preflight.jianying(require_fresh=True)
        self.assertEqual(cm.exception.code, "stale_draft")
        self.assertIn("打开之后又被编辑过", cm.exception.msg)
        self.assertEqual(cm.exception.detail["folder"], d)

    def test_gate_allows_clean_draft(self):
        d = self._jy_folder(edit_offset=-60)     # 编辑在打开之前
        cur = {"name": "测试草稿", "timeline_id": "T1", "folder": d}
        with mock.patch("colorpass.draftio.identify", return_value=cur):
            info = preflight.jianying(require_fresh=True)
        self.assertEqual(info["draft"], "测试草稿")

    def test_gate_fresh_check_off_for_writeback(self):
        """写回按快照重建，不受「打开后改过」影响，默认不该被拦。"""
        d = self._jy_folder(edit_offset=+60)
        cur = {"name": "测试草稿", "timeline_id": "T1", "folder": d}
        with mock.patch("colorpass.draftio.identify", return_value=cur):
            info = preflight.jianying()          # require_fresh 默认 False
        self.assertTrue(info["locked"])

    def test_cache_nonexistent_folder(self):
        # lock_state 只看 .locked：目录不存在时不许抛
        ok, age, p = preflight.lock_state(os.path.join(self._tmp(), "没有这个目录"))
        self.assertFalse(ok)
        self.assertEqual(age, 0.0)

    def _tmp(self):
        d = tempfile.mkdtemp(prefix="jyb_lock_")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_lock_state_fresh_vs_stale(self):
        d = self._tmp()
        p = os.path.join(d, ".locked")
        Path(p).write_text("", encoding="utf-8")
        ok, age, _ = preflight.lock_state(d)
        self.assertTrue(ok)
        old = time.time() - 13 * 3600
        os.utime(p, (old, old))
        ok, age, _ = preflight.lock_state(d)
        self.assertFalse(ok, "超过 12 小时的 .locked 是崩溃残留，不能当成「开着」")


# ================================================================ 命令行

class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def test_clean_scope_default_all(self):
        seen = {}
        with mock.patch("colorpass.retime.clean",
                        side_effect=lambda d=None: seen.update(d=d) or {"ok": True}):
            self.run_cli(["clean"])
        self.assertIsNone(seen["d"])

    def test_plan_reports_placement_without_davinci(self):
        """plan 是 dry-run：只读草稿、算出落轨位置，全程不碰达芬奇。

        开源后它是「没装达芬奇也能验证」的那一屏，所以必须真能跑。
        """
        d = draft_with_effects(2)
        with mock.patch("colorpass.draftio.read",
                        return_value=(d, {"name": "草稿A"}, {"stale": False})):
            code, out = self.run_cli(["plan"])
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["segments"], len(rep["plan"]))
        self.assertGreater(rep["timeline_end"], 0)
        self.assertIn("video", rep["kinds"])

    def test_no_memscan_env_short_circuits_scan(self):
        """COLORPASS_NO_MEMSCAN=1 时连进程快照都不该拍。"""
        with mock.patch.dict(os.environ, {"COLORPASS_NO_MEMSCAN": "1"}), \
                mock.patch("colorpass.draftio.probe.snapshot",
                           side_effect=AssertionError("不该碰剪映进程")):
            self.assertIsNone(draftio._scan_open_draft(deadline=1))

    def test_draft_out_maps_no_plaintext(self):
        """「草稿在但明文没了」要和「没打开草稿」分开报。

        面板按 gate 换说明：no_draft 让人去剪映打开草稿，no_plaintext 让人
        关掉重开（重开才会让剪映重新留一份明文）。
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli._draft_out("send", draftio.NoPlaintext("剪映内存里已经没有明文了"))
        self.assertEqual(code, 3)
        rep = json.loads(buf.getvalue())
        self.assertEqual(rep["gate"], "no_plaintext")
        self.assertIn("明文", rep["error"])

    def test_gc_rejects_zero_or_negative_thresholds(self):
        """--keep-days 0 看着像「不限制」，在 plan 里却是「全都超期」。

        那会把除当前草稿外的缓存一次删光（包括写回快照，如果它在池里）。
        宁可当场报错。
        """
        from contextlib import redirect_stderr
        for args in (["gc", "--keep-days", "0"],
                     ["gc", "--keep-last", "0"],
                     ["gc", "--max-mb", "-5"]):
            with self.assertRaises(SystemExit) as cm:
                with redirect_stderr(io.StringIO()):
                    cli.main(args)
            self.assertEqual(cm.exception.code, 2, args)

    def test_clean_scope_draft_id(self):
        seen = {}
        with mock.patch("colorpass.retime.clean",
                        side_effect=lambda d=None: seen.update(d=d) or {"ok": True}):
            self.run_cli(["clean", "--draft-id", "D9"])
        self.assertEqual(seen["d"], "D9")

    def test_clean_rejects_conflicting_scope(self):
        """--all 与 --draft-id 同时给是矛盾指令，必须报错。

        旧行为是 --all 静默胜出、连别人的预渲染一起清掉；用户以为只清自己
        那份，下次送入还得全部重编。
        """
        seen = {}
        with mock.patch("colorpass.retime.clean",
                        side_effect=lambda d=None: seen.update(d=d) or {"ok": True}):
            code, out = self.run_cli(["clean", "--all", "--draft-id", "D9"])
        self.assertNotIn("d", seen, "冲突时不该真的去删")
        self.assertEqual(code, 1)
        self.assertIn("不能同时给", out)

    def test_audit_speed_flag_parsed(self):
        got = {}

        def fake_audit(speed_mode="bake"):
            got["m"] = speed_mode
            return {"ok": True, "action": "audit", "blocking": [],
                    "warnings": [], "facts": {}}

        with mock.patch("colorpass.audit.run", side_effect=fake_audit):
            self.run_cli(["audit", "--speed", "slot"])
        self.assertEqual(got["m"], "slot")

    def test_no_gate_flag_both_positions(self):
        got = []

        def fake_send(**kw):
            got.append(kw.get("speed_mode"))
            return {"ok": True, "action": "send"}

        with mock.patch("colorpass.send.run", side_effect=fake_send):
            self.run_cli(["--no-gate", "send"])
            self.run_cli(["send", "--no-gate"])
        self.assertEqual(len(got), 2, "两个位置都该被认成 --no-gate")

    def test_unknown_command_prints_help(self):
        code, out = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("status", out)

    def test_print_filters_and_reports_error(self):
        rep = {"ok": False, "action": "send", "error": "闸门没过",
               "secret": "x" * 100, "placed": 3}
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli._print(rep)
        txt = buf.getvalue()
        self.assertIn("闸门没过", txt)
        self.assertIn("placed", txt)
        self.assertNotIn("secret", txt, "报告里无关字段不该刷屏")


if __name__ == "__main__":
    unittest.main()
