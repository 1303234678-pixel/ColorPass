"""JYBridge 离线单元测试 —— 不依赖达芬奇、不依赖剪映、不动任何真实草稿。

覆盖：
    paths / draftio / snapshot / send.build_plan / audit.scan_draft
    retime（真跑一次 ffmpeg 编码）/ write 的四个纯函数 / jy_workflow 命名

运行：
    <Python312> -m unittest discover -s tests -p "test_units.py" -v
"""
import copy
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

from jybridge import audit, draftio, paths, plain, pull, resolve, retime, send, snapshot, write  # noqa: E402

# ---------------------------------------------------------------- 夹具

def load_real_draft():
    """真实快照（13 段 / 2 变速 / 1 复合 / 1 文本 / 1 贴纸）。没有就返回 None。"""
    try:
        ptr = snapshot.current()
        if not ptr or ptr.get("_incomplete"):
            return None
        d, _meta = snapshot.snapshot_of(ptr)
        return d
    except Exception:
        return None


REAL = load_real_draft()


def mini_draft(n=3, dur_us=4_000_000):
    """合成一份最小草稿：n 段首尾相接的普通视频，素材是当前解释器本体。

    用 sys.executable 当素材路径是为了让 Path(...).exists() 为真 ——
    build_plan 会跳过不存在的素材。真正的编码/帧率无关，因为这里只测计划。
    """
    path = sys.executable.replace("\\", "/")
    segs = []
    for i in range(n):
        segs.append({
            "id": "SEG-%02d" % i,
            "material_id": "VID-%02d" % i,
            "extra_material_refs": [],
            "source_timerange": {"start": 0, "duration": dur_us},
            "target_timerange": {"start": i * dur_us, "duration": dur_us},
            "common_keyframes": [],
        })
    return {
        "id": "DRAFT-MINI",
        "duration": n * dur_us,
        "materials": {
            "videos": [{"id": "VID-%02d" % i, "path": path, "duration": dur_us}
                       for i in range(n)],
            "speeds": [], "drafts": [], "transitions": [],
        },
        "tracks": [{"id": "T1", "type": "video", "segments": segs}],
    }


def add_speed(draft, seg_index, speed):
    """给第 seg_index 段挂一个变速材质（并且 source 时长 = target × speed）。"""
    seg = draft["tracks"][0]["segments"][seg_index]
    sid = "SPD-%02d" % seg_index
    draft["materials"].setdefault("speeds", []).append(
        {"id": sid, "speed": speed})
    seg["extra_material_refs"] = list(seg.get("extra_material_refs") or []) + [sid]
    tt = seg["target_timerange"]
    seg["source_timerange"] = {"start": 0,
                               "duration": int(round(tt["duration"] * speed))}
    return draft


# ---------------------------------------------------------------- paths

class TestPaths(unittest.TestCase):
    def test_ensure_idempotent(self):
        d = tempfile.mkdtemp()
        try:
            a = paths.ensure(d, "x", "y")
            b = paths.ensure(d, "x", "y")
            self.assertEqual(a, b)
            self.assertTrue(os.path.isdir(a))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_note_does_not_raise(self):
        paths.note("unit test line")          # 写不进去也不许抛


# ---------------------------------------------------------------- draftio

class TestDraftio(unittest.TestCase):
    def test_layers_counts_by_type(self):
        d = {"tracks": [
            {"type": "video", "segments": [{}, {}]},
            {"type": "text", "segments": [{}]},
            {"type": "audio", "segments": [{}]},
            {"type": "weird", "segments": [{}, {}, {}]},
        ]}
        out = draftio.layers(d)
        self.assertEqual(out["video"], 2)
        self.assertEqual(out["text"], 1)
        self.assertEqual(out["audio"], 1)
        self.assertEqual(out["weird"], 3)

    def test_layers_tolerates_garbage(self):
        for d in ({}, {"tracks": None}, {"tracks": [None, 3, {"type": "video"}]}):
            out = draftio.layers(d)
            self.assertIsInstance(out, dict)

    def test_segment_speed_from_ref_and_field(self):
        spd = {"A": 2.0, "B": 1.0}
        self.assertEqual(draftio.segment_speed({"extra_material_refs": ["A"]}, spd), 2.0)
        # 材质是 1.0 等于没变速
        self.assertEqual(draftio.segment_speed({"extra_material_refs": ["B"]}, spd), 1.0)
        # 段自带 speed 字段优先
        self.assertEqual(draftio.segment_speed(
            {"speed": 0.5, "extra_material_refs": ["A"]}, spd), 0.5)
        # 段自带的 1.0 不算变速，继续看材质
        self.assertEqual(draftio.segment_speed(
            {"speed": 1.0, "extra_material_refs": ["A"]}, spd), 2.0)
        self.assertEqual(draftio.segment_speed({}, {}), 1.0)

    def test_speeds_by_ref_bad_values(self):
        d = {"materials": {"speeds": [
            {"id": "A", "speed": 2.0}, {"id": "B", "speed": "x"},
            {"id": None, "speed": 3}, "junk", {"id": "C"}]}}
        out = draftio.speeds_by_ref(d)
        self.assertEqual(out["A"], 2.0)
        self.assertEqual(out["B"], 1.0)      # 坏值退回 1.0，不抛
        self.assertEqual(out["C"], 1.0)
        self.assertNotIn(None, out)

    def test_compound_inner_prefers_embedded(self):
        entry = {"draft": {"tracks": [{"type": "video", "segments": [{}]}]},
                 "draft_file_path": "Z:/nope.json"}
        inner, how = draftio.compound_inner(entry)
        self.assertEqual(how, "embedded")
        self.assertTrue(inner["tracks"])

    def test_compound_inner_falls_back_to_file(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "sub.json")
            Path(p).write_text(json.dumps({"tracks": [{"type": "video",
                                                       "segments": [{}]}]}),
                               encoding="utf-8")
            inner, how = draftio.compound_inner({"draft_file_path": p})
            self.assertEqual(how, "file")
            self.assertTrue(inner["tracks"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_compound_inner_missing(self):
        inner, how = draftio.compound_inner({"draft_file_path": "Z:/nope.json"})
        self.assertIsNone(inner)
        self.assertEqual(how, "missing")

    def test_compound_inner_broken_json(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "sub.json")
            Path(p).write_text("{not json", encoding="utf-8")
            inner, how = draftio.compound_inner({"draft_file_path": p})
            self.assertIsNone(inner)
            self.assertEqual(how, "missing")     # 不抛
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_expand_compound_renumbers_lanes(self):
        # 内部只有 V2 有内容 → 归一化后必须是 1
        inner = {
            "materials": {"videos": [{"id": "V", "path": "a.mp4"}], "speeds": []},
            "tracks": [
                {"type": "video", "segments": []},
                {"type": "video", "segments": [{
                    "id": "s1", "material_id": "V",
                    "target_timerange": {"start": 0, "duration": 1_000_000},
                    "source_timerange": {"start": 0, "duration": 1_000_000}}]},
            ]}
        parts = draftio.expand_compound(inner)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["lane"], 1)

    def test_expand_compound_nested_offsets(self):
        leaf = {"materials": {"videos": [{"id": "V", "path": "a.mp4"}], "speeds": []},
                "tracks": [{"type": "video", "segments": [{
                    "id": "leaf", "material_id": "V",
                    "target_timerange": {"start": 0, "duration": 1_000_000},
                    "source_timerange": {"start": 0, "duration": 1_000_000}}]}]}
        outer = {"materials": {"videos": [], "speeds": [],
                               "drafts": [{"id": "D1", "draft": leaf}]},
                 "tracks": [{"type": "video", "segments": [{
                     "id": "s1", "extra_material_refs": ["D1"],
                     "target_timerange": {"start": 2_000_000, "duration": 1_000_000},
                     "source_timerange": {"start": 0, "duration": 1_000_000}}]}]}
        parts = draftio.expand_compound(outer)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["rec_us"], 2_000_000)   # 内层偏移叠加
        self.assertTrue(parts[0].get("nested"))

    def test_expand_compound_carries_disabled_flag(self):
        # 内部段上的 visible:false 必须带出来，否则 send 无法提示「复合里有停用段」
        inner = {
            "materials": {"videos": [{"id": "V", "path": "a.mp4"},
                                     {"id": "W", "path": "b.mp4"}], "speeds": []},
            "tracks": [{"type": "video", "segments": [
                {"id": "s1", "material_id": "V", "visible": False,
                 "target_timerange": {"start": 0, "duration": 1_000_000},
                 "source_timerange": {"start": 0, "duration": 1_000_000}},
                {"id": "s2", "material_id": "W",
                 "target_timerange": {"start": 1_000_000, "duration": 1_000_000},
                 "source_timerange": {"start": 0, "duration": 1_000_000}},
            ]}]}
        parts = draftio.expand_compound(inner)
        self.assertEqual([p["enabled"] for p in parts], [False, True])

    def test_expand_compound_depth_limit(self):
        self.assertEqual(draftio.expand_compound({"tracks": []}, depth=9), [])

    def test_expand_compound_handles_broken(self):
        self.assertEqual(draftio.expand_compound(None), [])
        self.assertEqual(draftio.expand_compound({"tracks": [None]}), [])

    def test_transition_records_shape(self):
        d = {"materials": {"transitions": [{"id": "TR", "name": "叠化",
                                            "duration": 500000}]},
             "tracks": [{"type": "video", "segments": [{
                 "id": "s1", "extra_material_refs": ["TR"],
                 "common_keyframes": [{"k": "v"}],
                 "target_timerange": {"start": 5, "duration": 9},
                 "source_timerange": {"start": 1, "duration": 7}}]}]}
        rec = draftio.transition_records(d)["s1"]
        self.assertEqual(len(rec["transitions"]), 1)
        self.assertEqual(rec["transitions"][0]["name"], "叠化")
        self.assertEqual(rec["target"], 5)
        self.assertEqual(rec["dur"], 9)
        self.assertEqual(rec["src"], 1)
        self.assertIn("k", rec["common_keyframes"])

    def test_plaintext_size_matches_compact_json(self):
        obj = {"a": 1, "b": "中文"}
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertEqual(draftio.plaintext_size(obj), len(raw))

    def test_freshness_plaintext_draft_is_not_stale(self):
        """磁盘是明文（不是 base64）时 payload=0 → 不能判 stale。"""
        d = tempfile.mkdtemp()
        try:
            Path(d, "draft_content.json").write_text('{"a":1}', encoding="utf-8")
            f = draftio.freshness({"folder": d}, {"a": 1})
            self.assertEqual(f["disk_payload_bytes"], 0)
            self.assertFalse(f["stale"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_draft_edit_time_reads_plain_settings(self):
        """剪映把最后编辑时间放在明文 draft_settings 里，不是 draft_content。"""
        d = tempfile.mkdtemp()
        try:
            Path(d, "draft_settings").write_text(
                "[General]\ndraft_create_time=100\ndraft_last_edit_time=1795102693\n",
                encoding="utf-8")
            self.assertEqual(draftio.draft_edit_time(d), 1795102693.0)
            self.assertEqual(draftio.draft_edit_time(d + "_没有"), 0.0)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_edited_since_open_beats_small_change(self):
        """改得很少（体积落在容差里）也要拦住 —— 时间先后能抓到。

        真实样本：「11月19日」打开于 03:44:28、last_edit 03:47:41。
        """
        d = tempfile.mkdtemp()
        try:
            import base64
            import os
            body = {"k": "v" * 100}
            raw = json.dumps(body, ensure_ascii=False,
                             separators=(",", ":")).encode()
            # 密文载荷与明文只差 12 B —— 体积对账会放行
            Path(d, "draft_content.json").write_bytes(
                base64.b64encode(raw + b"\x00" * 12))
            Path(d, "draft_settings").write_text(
                "[General]\ndraft_last_edit_time=%d\n" % 1795102693,
                encoding="utf-8")
            lock = Path(d, ".locked")
            lock.write_bytes(b"")
            os.utime(lock, (1795102685, 1795102685))   # 打开于编辑之前
            fresh = draftio.freshness({"folder": d}, body)
            self.assertTrue(fresh["same_version"], "体积本来就对得上")
            self.assertTrue(fresh["edited_since_open"])
            self.assertTrue(fresh["stale"])
            self.assertEqual(fresh["stale_reason"], "edited_since_open")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_edited_since_open_false_after_reopen(self):
        """重开草稿后 .locked 比 last_edit 新 —— 不该再报改过。"""
        d = tempfile.mkdtemp()
        try:
            import os
            Path(d, "draft_settings").write_text(
                "[General]\ndraft_last_edit_time=%d\n" % 1795102693,
                encoding="utf-8")
            lock = Path(d, ".locked")
            lock.write_bytes(b"")
            os.utime(lock, (1795102900, 1795102900))
            self.assertFalse(draftio.edited_since_open(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_stale_message_says_reopen_not_ctrl_s(self):
        """提示必须给出「关掉再打开」，且不要说 Ctrl+S 能解决。"""
        msg = draftio.stale_message({
            "edited_since_open": True, "draft_edit_text": "11-19 23:47:41",
            "opened_text": "11-19 23:38:25"})
        self.assertIn("11-19 23:47:41", msg)
        self.assertIn("重新打开", msg)
        self.assertIn("Ctrl+S", msg)          # 只出现在「不够」那句里
        self.assertIn("不够", msg)

    def test_freshness_detects_real_drift(self):
        d = tempfile.mkdtemp()
        try:
            import base64
            payload = b"x" * 200000
            Path(d, "draft_content.json").write_bytes(base64.b64encode(payload))
            fresh = draftio.freshness({"folder": d}, {"k": "v" * 100})
            self.assertTrue(fresh["stale"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(REAL, "没有真实快照")
    def test_real_features(self):
        """在真实草稿上验证统计口径，而不是记住某一天的段数。

        快照每送一次新版就换一份，钉死「7 段动画」这种数字只会制造假红
        （2026-11-19 用户在剪映里新加了两段动画，就成了 9）。这里改成独立
        手算一遍再和 feature_counts 对账：数据怎么变都不会误报，口径写错才会。
        """
        mats = REAL.get("materials") or {}
        anim = {m.get("id") for m in (mats.get("material_animations") or [])
                if isinstance(m, dict)}
        trans = {m.get("id") for m in (mats.get("transitions") or [])
                 if isinstance(m, dict)}
        want_anim = want_trans = video_segs = 0
        for tr in REAL.get("tracks") or []:
            for sg in (tr.get("segments") or []):
                if not isinstance(sg, dict):
                    continue
                if tr.get("type") == "video":
                    video_segs += 1
                refs = sg.get("extra_material_refs") or []
                want_anim += int(any(r in anim for r in refs))
                want_trans += int(any(r in trans for r in refs))

        f = draftio.feature_counts(REAL)
        self.assertEqual(f["segments_with_animation"], want_anim)
        self.assertEqual(f["segments_with_transition"], want_trans)
        self.assertEqual(draftio.layers(REAL)["video"], video_segs)
        self.assertEqual(len(f["compound_names"]), f["segments_with_compound"])


# ---------------------------------------------------------------- snapshot

class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jyb_snap_")
        self._cur = snapshot.CURRENT_PATH
        self._pend = snapshot.PENDING_PATH
        snapshot.CURRENT_PATH = os.path.join(self.tmp, "current.json")
        snapshot.PENDING_PATH = os.path.join(self.tmp, "pending.json")
        self.run_dir = os.path.join(self.tmp, "runs", "D1")
        os.makedirs(self.run_dir, exist_ok=True)
        self.src = os.path.join(self.tmp, "src_draft")
        os.makedirs(self.src, exist_ok=True)

    def tearDown(self):
        snapshot.CURRENT_PATH = self._cur
        snapshot.PENDING_PATH = self._pend
        shutil.rmtree(self.tmp, ignore_errors=True)

    def draft(self, n=2):
        d = mini_draft(n)
        d["id"] = "D1"
        return d

    def test_save_creates_version_dir(self):
        m = snapshot.save(self.draft(), {"name": "草稿A", "folder": self.src},
                          self.run_dir)
        self.assertTrue(os.path.isdir(m["dir"]))
        self.assertTrue(m["dir"].replace("\\", "/").endswith(m["dir"].split("/")[-1]))
        self.assertTrue(os.path.exists(os.path.join(m["dir"], "meta.json")))
        self.assertEqual(m["video_segments"], 2)
        self.assertEqual(len(m["sha"]), 16)

    def test_second_save_same_second_is_distinct(self):
        a = snapshot.save(self.draft(), {"name": "A", "folder": self.src}, self.run_dir)
        b = snapshot.save(self.draft(), {"name": "A", "folder": self.src}, self.run_dir)
        self.assertNotEqual(a["dir"], b["dir"])
        self.assertTrue(os.path.isdir(a["dir"]) and os.path.isdir(b["dir"]))

    def test_claim_incomplete_then_commit(self):
        m = snapshot.save(self.draft(), {"name": "A", "folder": self.src}, self.run_dir)
        snapshot.claim(m, self.run_dir, "TL", 0, 2)
        cur = snapshot.current()
        self.assertTrue(cur["_incomplete"])
        self.assertFalse(cur["committed"])
        snapshot.commit(placed=2)
        cur = snapshot.current()
        self.assertFalse(cur.get("_incomplete"))
        self.assertTrue(cur["committed"])
        self.assertEqual(cur["timeline"], "TL")

    def test_cleanup_keeps_previous_until_commit(self):
        a = snapshot.save(self.draft(), {"name": "A", "folder": self.src}, self.run_dir)
        snapshot.claim(a, self.run_dir, "TL", 0, 2)
        snapshot.commit()
        b = snapshot.save(self.draft(), {"name": "A", "folder": self.src}, self.run_dir)
        self.assertTrue(os.path.isdir(a["dir"]))
        self.assertTrue(os.path.isdir(b["dir"]))
        snapshot.claim(b, self.run_dir, "TL2", 0, 2)
        # claim 只是 pending：上一次成功的那份必须留着（写回还要用）
        self.assertTrue(os.path.isdir(a["dir"]))
        self.assertTrue(os.path.isdir(b["dir"]))
        snapshot.commit()
        # 走完了才清旧的
        self.assertFalse(os.path.isdir(a["dir"]))
        self.assertTrue(os.path.isdir(b["dir"]))

    def test_snapshot_of_raises_without_file(self):
        snapshot.claim({"dir": os.path.join(self.tmp, "nope"), "draft_id": "D1"},
                       self.run_dir, "TL", 0, 0)
        with self.assertRaises(FileNotFoundError):
            snapshot.snapshot_of(snapshot.current())

    def test_diff(self):
        a = mini_draft(3)
        b = copy.deepcopy(a)
        b["tracks"][0]["segments"].pop(1)                      # 删一段
        b["tracks"][0]["segments"][0]["target_timerange"]["duration"] += 100000
        d = snapshot.diff(a, b)
        self.assertEqual(d["removed"], 1)
        self.assertEqual(d["changed"], 1)
        self.assertEqual(d["added"], 0)
        self.assertFalse(d["same"])
        self.assertTrue(snapshot.diff(a, a)["same"])

    def test_subdraft_copied_and_rewritten(self):
        sub = os.path.join(self.src, "subdraft", "UUID1")
        os.makedirs(sub, exist_ok=True)
        inner = os.path.join(sub, "draft_content.json")
        Path(inner).write_text('{"tracks": []}', encoding="utf-8")
        d = self.draft()
        d["materials"]["drafts"] = [{"id": "C1", "draft_file_path": inner}]
        m = snapshot.save(d, {"name": "A", "folder": self.src}, self.run_dir)
        self.assertEqual(len(m["subdrafts"]), 1)
        self.assertTrue(m["subdrafts"][0]["ok"])
        snap, _ = snapshot.load(m["dir"])
        newp = snap["materials"]["drafts"][0]["draft_file_path"]
        self.assertNotEqual(newp, inner)
        self.assertTrue(os.path.exists(newp))
        self.assertIn("snap-", newp)

    def test_current_none_when_missing(self):
        self.assertIsNone(snapshot.current())

    def test_current_none_when_corrupt(self):
        Path(snapshot.CURRENT_PATH).write_text("{oops", encoding="utf-8")
        self.assertIsNone(snapshot.current())

    def test_pointer_write_is_atomic(self):
        """指针必须是「写临时文件 + 原子替换」。

        面板关窗口时会 taskkill 正在跑的 cli，正好落在写指针的几毫秒里；
        直写会留下半截 JSON，下次读不出来就当成「没有未完成的送入」，
        写回于是拿上一次的旧快照当底。
        """
        target = os.path.join(self.tmp, "p.json")
        Path(target).write_text('{"old": 1}', encoding="utf-8")
        snapshot._write_ptr(target, {"draft_id": "D1", "committed": True})
        self.assertEqual(json.loads(Path(target).read_text(encoding="utf-8")),
                         {"draft_id": "D1", "committed": True})
        self.assertFalse(os.path.exists(target + ".tmp"), "不该留下临时文件")


# ---------------------------------------------------------------- build_plan

class TestBuildPlan(unittest.TestCase):
    def test_mini_plan_slot(self):
        d = mini_draft(3)
        plan, skip, ov, tr, sh = send.build_plan(d, "slot")
        self.assertEqual(len(plan), 3)
        self.assertEqual([c["rec"] for c in plan], [0, 120, 240])
        self.assertTrue(all(c["place"] == c["want"] for c in plan))
        self.assertEqual(skip + ov + tr + sh, [])

    def test_lane_is_one_based_in_file_order(self):
        d = mini_draft(2)
        d["tracks"].append({"id": "T2", "type": "video", "segments": [
            dict(copy.deepcopy(d["tracks"][0]["segments"][0]), id="B0",
                 target_timerange={"start": 0, "duration": 1_000_000})]})
        plan, *_ = send.build_plan(d, "slot")
        self.assertEqual(sorted({c["lane"] for c in plan}), [1, 2])

    def test_missing_media_skipped(self):
        d = mini_draft(2)
        d["materials"]["videos"][1]["path"] = "Z:/definitely/missing.mp4"
        plan, skip, *_ = send.build_plan(d, "slot")
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(skip), 1)
        self.assertIn("素材不在盘上", skip[0]["reason"])

    def test_source_mode_scales_speed_segments(self):
        d = add_speed(mini_draft(3), 1, 2.0)
        plan, _s, _o, _t, sh = send.build_plan(d, "source")
        sp = [c for c in plan if abs(c["speed"] - 2.0) < 1e-6][0]
        self.assertEqual(sp["place"], 240)      # want 120 × 2
        self.assertTrue(sh)                     # 后段被推移并报出来
        self.assertEqual(plan[-1]["rec"] + plan[-1]["place"], 480)

    def test_bake_mode_keeps_placeholder(self):
        d = add_speed(mini_draft(3), 1, 2.0)
        plan, _s, _o, _t, sh = send.build_plan(d, "bake")
        sp = [c for c in plan if abs(c["speed"] - 2.0) < 1e-6][0]
        self.assertTrue(sp.get("bake"))
        self.assertEqual(sp["place"], 120)
        self.assertEqual(sh, [])
        self.assertFalse(any(c.get("bake") for c in plan
                             if abs(c["speed"] - 1.0) < 1e-6))

    def test_transition_overlap_is_trimmed(self):
        d = mini_draft(2)
        d["materials"]["transitions"] = [{"id": "TR", "name": "叠化"}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["TR"]
        # 制造 30 帧重叠（剪映草稿里的时间全是微秒：90 帧 = 3_000_000 µs）
        d["tracks"][0]["segments"][1]["target_timerange"]["start"] = 3_000_000
        plan, _s, ov, tr, _sh = send.build_plan(d, "slot")
        self.assertEqual(len(ov), 1)
        self.assertTrue(ov[0]["transition"])
        self.assertEqual(tr[0]["cut_frames"], 30)
        self.assertEqual(plan[0]["place"], 90)          # 收到后段起点
        self.assertEqual(plan[0]["rec"], 0)             # 起点不动
        self.assertEqual(plan[1]["rec"], 90)

    def test_fatal_overlap_flagged(self):
        d = mini_draft(2)
        d["tracks"][0]["segments"][1]["target_timerange"]["start"] = 0
        _plan, _s, ov, _t, _sh = send.build_plan(d, "slot")
        self.assertTrue(ov and ov[0].get("fatal"))

    @unittest.skipUnless(REAL, "没有真实快照")
    def test_real_snapshot_plan_invariants(self):
        for mode in ("slot", "bake"):
            plan, skip, ov, tr, sh = send.build_plan(REAL, mode)
            self.assertEqual(len(plan), 13, mode)
            self.assertEqual(skip, [], mode)
            self.assertEqual(sh, [], mode)
            self.assertFalse([o for o in ov if o.get("fatal")], mode)
            # 同轨不重叠
            by = {}
            for c in plan:
                by.setdefault(c["lane"], []).append(c)
            for lane, items in by.items():
                items.sort(key=lambda x: x["rec"])
                for a, b in zip(items, items[1:]):
                    self.assertLessEqual(a["rec"] + a["place"], b["rec"],
                                         "%s 轨 %d 重叠" % (mode, lane))
            end = max(c["rec"] + c["place"] for c in plan)
            self.assertEqual(end, 352, mode)     # 与 13:01 的真实送入一致

    @unittest.skipUnless(REAL, "没有真实快照")
    def test_real_compound_carries_inner_timeline(self):
        plan, *_ = send.build_plan(REAL, "slot")
        comp = [c for c in plan if c["kind"] == "compound"]
        self.assertEqual(len(comp), 1)
        self.assertTrue(comp[0]["inner"])
        self.assertTrue(all(p["path"] for p in comp[0]["inner"]))


class TestSolveExact(unittest.TestCase):
    def test_exact_or_tail(self):
        for mf in (23.976, 24, 25, 29.97, 30, 50, 59.94, 60):
            for want in (1, 2, 7, 19, 20, 65, 120, 352):
                n, tail, kind = send.solve_exact(want, mf)
                self.assertGreaterEqual(n, 1)
                got = send.tl_len(n, mf) + tail
                self.assertEqual(got, want, "mf=%s want=%s → n=%s tail=%s"
                                 % (mf, want, n, tail))
                self.assertIn(kind, ("exact", "tail1", "approx"))

    def test_zero(self):
        self.assertEqual(send.solve_exact(0, 30)[0], 0)


# ---------------------------------------------------------------- audit

class TestAudit(unittest.TestCase):
    def codes(self, rep):
        return {x["code"] for x in rep}

    def test_mini_clean(self):
        b, w, f = audit.scan_draft(mini_draft(2))
        self.assertEqual(b, [])
        self.assertEqual(f["video_segments"], 2)

    def test_zero_duration_and_negative(self):
        d = mini_draft(2)
        d["tracks"][0]["segments"][0]["target_timerange"] = {
            "start": -1_000_000, "duration": 0}
        b, _w, _f = audit.scan_draft(d)
        self.assertIn("zero_duration", self.codes(b))
        self.assertIn("negative_rec", self.codes(b))

    def test_no_path(self):
        d = mini_draft(1)
        d["materials"]["videos"][0]["path"] = ""
        b, _w, _f = audit.scan_draft(d)
        self.assertIn("no_path", self.codes(b))

    def test_missing_media(self):
        d = mini_draft(1)
        d["materials"]["videos"][0]["path"] = "Z:/nope.mp4"
        b, _w, _f = audit.scan_draft(d)
        self.assertIn("missing_media", self.codes(b))

    def test_over_source_warning(self):
        d = mini_draft(1)
        d["materials"]["videos"][0]["duration"] = 1_000_000   # 素材只有 1s
        _b, w, _f = audit.scan_draft(d)
        self.assertIn("over_source", self.codes(w))

    def test_speed_overrun_blocks_only_without_bake(self):
        """慢放段越界：bake 模式只是提醒（会被预渲染吃掉），slot/source 才是硬问题。"""
        d = add_speed(mini_draft(1), 0, 0.5)
        d["tracks"][0]["segments"][0]["target_timerange"]["duration"] = 4_000_000
        d["tracks"][0]["segments"][0]["source_timerange"]["duration"] = 1_000_000
        b_bake, w_bake, _f = audit.scan_draft(d, speed_mode="bake")
        self.assertIn("speed_overrun", self.codes(w_bake))
        self.assertNotIn("speed_overrun", self.codes(b_bake))
        b_slot, w_slot, _f = audit.scan_draft(d, speed_mode="slot")
        self.assertIn("speed_overrun", self.codes(b_slot))
        self.assertNotIn("speed_overrun", self.codes(w_slot))

    def test_speed_inconsistent(self):
        d = add_speed(mini_draft(1), 0, 2.0)
        d["tracks"][0]["segments"][0]["source_timerange"]["duration"] = 1_000_000
        _b, w, _f = audit.scan_draft(d)
        self.assertIn("speed_inconsistent", self.codes(w))

    def test_hard_overlap_blocks(self):
        d = mini_draft(2)
        d["tracks"][0]["segments"][1]["target_timerange"]["start"] = 0
        b, _w, _f = audit.scan_draft(d)
        self.assertIn("hard_overlap", self.codes(b))

    def test_compound_empty_blocks(self):
        d = mini_draft(1)
        d["materials"]["drafts"] = [{"id": "C1", "name": "复合"}]
        d["materials"]["videos"] = []
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["C1"]
        b, _w, _f = audit.scan_draft(d)
        self.assertIn("compound_unreadable", self.codes(b))

    @unittest.skipUnless(REAL, "没有真实快照")
    def test_real_no_blocking(self):
        b, w, _f = audit.scan_draft(REAL)
        self.assertEqual(b, [], [x["code"] for x in b])

    @unittest.skipUnless(REAL, "没有真实快照")
    def test_real_no_false_drift(self):
        """文本轨到 368、视频到 352 —— 这不叫漂移（草稿总长由文本轨定义）。"""
        _b, w, _f = audit.scan_draft(REAL)
        self.assertNotIn("duration_drift", self.codes(w),
                         "视频轨短于草稿但其它轨已到末尾，不该报漂移")


# ---------------------------------------------------------------- retime

class TestRetime(unittest.TestCase):
    def test_cache_path_stable_and_distinct(self):
        a = retime.cache_path("D1", "SEGAAAAAA-1", 2.0, 1_000_000, 20)
        b = retime.cache_path("D1", "SEGAAAAAA-1", 2.0, 1_000_000, 20)
        c = retime.cache_path("D1", "SEGAAAAAA-1", 2.0, 1_000_000, 21)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotIn(".", os.path.basename(a)[:-4])   # 文件名里不能带点

    def test_atempo_chaining(self):
        self.assertEqual(retime._atempo(2.0), "atempo=2.000000")
        self.assertEqual(retime._atempo(4.0), "atempo=2.000000,atempo=2.000000")
        self.assertIn("atempo=0.500000", retime._atempo(0.25))

    @unittest.skipUnless(retime.available(), "没有 ffmpeg")
    def test_bake_real_encode_and_cache(self):
        d = tempfile.mkdtemp(prefix="jyb_rt_")
        old = paths.RETIME_ROOT
        paths.RETIME_ROOT = os.path.join(d, "retime")
        try:
            src = os.path.join(d, "src.mp4")
            import subprocess
            exe = retime._ffmpeg()
            subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=size=160x90:rate=30:duration=2",
                            "-pix_fmt", "yuv420p", src], check=True)
            out = retime.cache_path("D1", "SEG1", 2.0, 0, 15)
            ok, det = retime.bake(src, 0, 2_000_000, 2.0, 15, out)
            self.assertTrue(ok, det.get("why"))
            self.assertTrue(os.path.getsize(out) > 4096)
            ok2, det2 = retime.bake(src, 0, 2_000_000, 2.0, 15, out)
            self.assertTrue(ok2 and det2.get("cached"))
            import av
            with av.open(out) as c:
                v = c.streams.video[0]
                self.assertEqual(v.frames, 15)
                self.assertAlmostEqual(float(v.average_rate), 30.0, places=2)
                self.assertEqual(v.codec_context.format.name, "yuv422p10le")
            rep = retime.clean(None)
            self.assertTrue(rep["ok"])
            self.assertFalse(os.path.isdir(paths.RETIME_ROOT))
        finally:
            paths.RETIME_ROOT = old
            shutil.rmtree(d, ignore_errors=True)

    def test_clean_rejects_path_traversal(self):
        """--draft-id .. 不能把缓存根（甚至 %LOCALAPPDATA%）删掉。

        clean 会对 paths.retime_dir(id) 做 rmtree，而 paths.ensure 会先
        os.path.join + makedirs —— 传 .. 时目标就是 JYBridge 根，
        current.json、全部 runs 快照、日志都在里面。
        """
        old = paths.RETIME_ROOT
        d = tempfile.mkdtemp()
        try:
            paths.RETIME_ROOT = os.path.join(d, "retime")
            paths.ensure(paths.RETIME_ROOT)
            keep = Path(d, "current.json")
            keep.write_text("{}", encoding="utf-8")
            for bad in ("..", "../..", "a/b", "..\\x", "x/../.."):
                rep = retime.clean(bad)
                self.assertFalse(rep["ok"], "应该拒绝 %r" % bad)
                self.assertIn("不合法", rep.get("error") or "")
            self.assertTrue(os.path.isdir(paths.RETIME_ROOT))
            self.assertTrue(keep.exists(), "缓存根之外的东西一个都不能动")
        finally:
            paths.RETIME_ROOT = old
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(retime.available(), "没有 ffmpeg")
    def test_bake_missing_source(self):
        d = tempfile.mkdtemp()
        try:
            ok, det = retime.bake("Z:/nope.mp4", 0, 1_000_000, 2.0, 5,
                                  os.path.join(d, "o.mov"))
            self.assertFalse(ok)
            self.assertIn("不在盘上", det["why"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- write

class TestWriteHelpers(unittest.TestCase):
    def cube(self, path, n=33, val=0.0):
        lines = ['TITLE "t"', "LUT_3D_SIZE %d" % n]
        for _ in range(n ** 3):
            lines.append("%.6f %.6f %.6f" % (val, val, val))
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def test_verify_cubes_ok_and_bad(self):
        d = tempfile.mkdtemp()
        try:
            good = self.cube(os.path.join(d, "a.cube"))
            self.assertEqual(write.verify_cubes({"s1": good}), [])
            # 分号后的附加字段要忽略：文件本身在盘上就算有效
            self.assertEqual(write.verify_cubes({"s1": good + ";tail"}), [])
            self.assertEqual(
                write.verify_cubes({"s1": good + ".nope;tail"})[0]["why"],
                "文件不存在")
            short = os.path.join(d, "b.cube")
            Path(short).write_text("x", encoding="utf-8")
            self.assertEqual(write.verify_cubes({"s": short})[0]["why"],
                             "文件过小/空")
            half = self.cube(os.path.join(d, "c.cube"), n=17)
            self.assertIn("不是完整", write.verify_cubes({"s": half})[0]["why"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_embed_cubes_rewrites_and_copies(self):
        d = tempfile.mkdtemp()
        try:
            good = self.cube(os.path.join(d, "a.cube"))
            new_draft = os.path.join(d, "new")
            os.makedirs(new_draft)
            mapping, copied = write.embed_cubes(new_draft, {"s1": good})
            dst = mapping["s1"]
            self.assertTrue(os.path.exists(dst))
            self.assertIn("Resources/JYBridge", dst.replace("\\", "/"))
            self.assertTrue(dst.endswith("s1.cube"))
            self.assertEqual(copied[0]["bytes"], os.path.getsize(dst))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_clone_subdrafts(self):
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(d, "src")
            sub = os.path.join(src, "subdraft", "U1")
            os.makedirs(sub)
            inner = os.path.join(sub, "draft_content.json")
            Path(inner).write_text("{}", encoding="utf-8")
            nd = {"materials": {"drafts": [{"id": "C1",
                                           "draft_file_path": inner}]}}
            new = os.path.join(d, "new")
            os.makedirs(new)
            out = write.clone_subdrafts(src, new, nd)
            self.assertTrue(out[0]["ok"])
            p = nd["materials"]["drafts"][0]["draft_file_path"]
            self.assertIn("new", p.replace("\\", "/"))
            self.assertTrue(os.path.exists(p))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_clone_subdrafts_outside_source_is_skipped(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "subdraft"), exist_ok=True)
            nd = {"materials": {"drafts": [{"id": "C", "draft_file_path":
                                            "Z:/other/sub/x.json"}]}}
            out = write.clone_subdrafts(d, os.path.join(d, "new"), nd)
            self.assertEqual(out[0].get("skipped"), True)
            # 源目录里连 subdraft 都没有时，直接返回空，不报错
            d2 = tempfile.mkdtemp()
            self.assertEqual(write.clone_subdrafts(d2, d2, nd), [])
            shutil.rmtree(d2, ignore_errors=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_compound_intact_detects_change(self):
        inner = {"tracks": [{"type": "video", "segments": [{"id": "x"}]}]}
        a = {"materials": {"drafts": [{"id": "C", "draft": inner}]}}
        b = copy.deepcopy(a)
        b["materials"]["drafts"][0]["draft"]["tracks"][0]["segments"][0]["id"] = "y"
        problems = []
        self.assertEqual(write.compound_intact(a, b, problems), 0)
        self.assertTrue(problems)
        self.assertIn("内部时间线变了", problems[0]["why"])
        problems2 = []
        self.assertEqual(write.compound_intact(a, a, problems2), 1)
        self.assertEqual(problems2, [])

    def test_compound_intact_missing(self):
        a = {"materials": {"drafts": [{"id": "C", "draft":
                                       {"tracks": [{"segments": []}]}}]}}
        problems = []
        self.assertEqual(write.compound_intact(a, {"materials": {}}, problems), 0)
        self.assertIn("没了", problems[0]["why"])


def rich_draft():
    """一份「什么都有」的草稿：变速 + 转场 + 淡入淡出 + 蒙版 + 关键帧
    + 画中画轨 + 独立特效轨 + 非 LUT 特效。用来钉「克隆必须一样不差」。"""
    path = sys.executable.replace("\\", "/")
    base = mini_draft(2)
    segs = base["tracks"][0]["segments"]
    segs[0]["extra_material_refs"] = ["SP-1", "TR-1", "AF-1", "MSK-1"]
    segs[0]["common_keyframes"] = [{"id": "KF1", "property_type": "KFTypePositionX",
                                    "keyframe_list": [{"values": [0.0]}]}]
    base["materials"].update({
        "speeds": [{"id": "SP-1", "speed": 2.0}],
        "transitions": [{"id": "TR-1", "name": "叠化", "duration": 500000}],
        "audio_fades": [{"id": "AF-1", "fade_in": 100000, "fade_out": 0}],
        "common_mask": [{"id": "MSK-1", "name": "线性", "resource_type": "line"}],
        "effects": [{"id": "EFF-N", "path": "%s;;pkg" % path, "name": "滤镜"}],
    })
    base["tracks"].append({"id": "T2", "type": "video", "segments": [{
        "id": "PIP-1", "material_id": "VID-00",
        "target_timerange": {"start": 0, "duration": 1000000},
        "source_timerange": {"start": 0, "duration": 1000000},
        "extra_material_refs": [], "common_keyframes": []}]})
    base["tracks"].append({"id": "T3", "type": "effect", "segments": [{
        "id": "FX-1", "material_id": "EFF-N",
        "target_timerange": {"start": 0, "duration": 2000000},
        "source_timerange": {"start": 0, "duration": 2000000},
        "extra_material_refs": [], "common_keyframes": []}]})
    return base


class TestPullDisabled(unittest.TestCase):
    """达芬奇里的禁用片段（= 剪映的停用段）不该被取色。

    给它导一版恒等 LUT 再写回，等于往停用段里塞一层处理：用户在剪映里
    一取消停用，画面会莫名其妙多一层。
    """

    class Clip(object):
        def __init__(self, v):
            self.v = v

        def GetClipEnabled(self):
            return self.v

    def test_disabled_is_detected(self):
        self.assertTrue(pull.is_disabled(self.Clip(False)))
        self.assertFalse(pull.is_disabled(self.Clip(True)))

    def test_missing_api_means_enabled(self):
        # Resolve 17 没有 GetClipEnabled：宁可多导一个 LUT，也不能漏段
        self.assertFalse(pull.is_disabled(object()))

    def test_error_means_enabled(self):
        class Boom(object):
            def GetClipEnabled(self):
                raise RuntimeError("nope")

        self.assertFalse(pull.is_disabled(Boom()))


class TestStructureDiff(unittest.TestCase):
    """克隆之后「剪映原生结构还在不在」必须能被断言出来。

    这三种漏判都真实存在过：
      · 转场只比数量 → 叠化被换成闪白也通过；
      · 只遍历 video 轨 → 独立特效轨/滤镜轨/文字轨是盲区；
      · speed 引用丢了但 timerange 没变 → 回 1 倍速，LUT 挂错画面。
    """

    def setUp(self):
        self.d = rich_draft()

    def test_clean_clone_has_no_problem(self):
        self.assertEqual(write.structure_diff(self.d, copy.deepcopy(self.d)), [])

    def test_lost_disabled_flag_is_caught(self):
        # 克隆把停用段变回启用：用户关掉的画面会「复活」进成片，而其它断言全过
        self.d["tracks"][0]["segments"][0]["visible"] = False
        b = copy.deepcopy(self.d)
        b["tracks"][0]["segments"][0].pop("visible")
        probs = write.structure_diff(self.d, b)
        self.assertTrue(probs)
        self.assertIn("停用", probs[0]["why"])

    def test_disabled_flag_survives_clone(self):
        self.d["tracks"][0]["segments"][0]["visible"] = False
        self.assertEqual(write.structure_diff(self.d, copy.deepcopy(self.d)), [])

    def test_transition_swapped_same_count_is_caught(self):
        # 数量不变，类型换了 —— 旧逻辑（比数量）会放过
        b = copy.deepcopy(self.d)
        b["materials"]["transitions"][0]["name"] = "闪白"
        probs = write.structure_diff(self.d, b)
        self.assertTrue(probs)
        self.assertIn("转场", probs[0]["why"])

    def test_transition_duration_change_is_caught(self):
        b = copy.deepcopy(self.d)
        b["materials"]["transitions"][0]["duration"] = 250000
        self.assertTrue(write.structure_diff(self.d, b))

    def test_speed_ref_loss_is_caught(self):
        # timerange 一动没动，只少了 speeds 引用：旧断言全过，画面却回 1 倍速
        b = copy.deepcopy(self.d)
        b["tracks"][0]["segments"][0]["extra_material_refs"] = [
            r for r in b["tracks"][0]["segments"][0]["extra_material_refs"]
            if r != "SP-1"]
        probs = write.structure_diff(self.d, b)
        self.assertTrue(any("转场/蒙版/变速" in p["why"] for p in probs), probs)

    def test_segment_speed_field_change_is_caught(self):
        a = copy.deepcopy(self.d)
        a["tracks"][0]["segments"][1]["speed"] = 1.5
        b = copy.deepcopy(a)
        b["tracks"][0]["segments"][1]["speed"] = 1.0
        self.assertTrue(any("变速倍率" in p["why"]
                            for p in write.structure_diff(a, b)))

    def test_effect_track_dropped_is_caught(self):
        # 独立特效轨不在旧核对的遍历范围里
        b = copy.deepcopy(self.d)
        b["tracks"] = [t for t in b["tracks"] if t["type"] != "effect"]
        probs = write.structure_diff(self.d, b)
        self.assertTrue(any(p["why"] == "轨道数变了" for p in probs), probs)

    def test_mask_loss_is_caught(self):
        b = copy.deepcopy(self.d)
        b["tracks"][0]["segments"][0]["extra_material_refs"] = [
            r for r in b["tracks"][0]["segments"][0]["extra_material_refs"]
            if r != "MSK-1"]
        self.assertTrue(write.structure_diff(self.d, b))

    def test_segment_order_change_is_caught(self):
        b = copy.deepcopy(self.d)
        b["tracks"][0]["segments"].reverse()
        self.assertTrue(any("段顺序" in p["why"]
                            for p in write.structure_diff(self.d, b)))

    def test_kept_summary_counts_what_survived(self):
        k = write.kept_summary(self.d)
        self.assertEqual(k["speed_segments"], 1)
        self.assertEqual(k["fade_segments"], 1)
        self.assertEqual(k["transitions"], 1)
        self.assertEqual(k["masks"], 1)
        self.assertEqual(k["keyframe_segments"], 1)
        self.assertEqual(k["overlay_tracks"], 1)
        self.assertEqual(k["compounds"], 0)
        # 只数非 LUT 特效：cube 是工具自己加的，不算「保住的原生效果」
        self.assertEqual(k["effects"], 1)


# ---------------------------------------------------------------- 命名
class TestSafeName(unittest.TestCase):
    def setUp(self):
        from jybridge import workflow
        self.WF = workflow

    def test_illegal_chars(self):
        self.assertEqual(self.WF.safe_name('a\\b/c:d*e?f"g<h>i|j'),
                         "a b c d e f g h i j")

    def test_trim_and_limit(self):
        self.assertEqual(self.WF.safe_name("  名字...  "), "名字")
        self.assertEqual(len(self.WF.safe_name("x" * 200)), 64)

    def test_empty(self):
        self.assertEqual(self.WF.safe_name(""), "")
        self.assertEqual(self.WF.safe_name(None), "")


# ---------------------------------------------------------------- 回归

class TestRegressions(unittest.TestCase):
    """每一个用例对应一个真实改过的缺陷，防止改回去。"""

    def test_speed_counted_per_segment_not_per_ref(self):
        """一段挂两个 speed 材质，只能算「1 段变速」。"""
        d = mini_draft(2)
        seg = d["tracks"][0]["segments"][0]
        d["materials"]["speeds"] = [{"id": "S1", "speed": 2.0},
                                    {"id": "S2", "speed": 2.0}]
        seg["extra_material_refs"] = ["S1", "S2"]
        f = draftio.feature_counts(d)
        self.assertEqual(f["segments_with_speed"], 1)

    def test_compound_counted_once(self):
        d = mini_draft(1)
        d["materials"]["drafts"] = [{"id": "C1", "name": "复合A",
                                     "draft": {"tracks": [{"type": "video",
                                                           "segments": []}]}}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["C1"]
        f = draftio.feature_counts(d)
        self.assertEqual(f["segments_with_compound"], 1)
        self.assertEqual(f["compound_names"], ["复合A"])

    def test_segment_own_speed_field_counted(self):
        """剪映有时把速度直接写在段上（没有 speeds 材质），也得算。"""
        d = mini_draft(1)
        d["tracks"][0]["segments"][0]["speed"] = 0.5
        self.assertEqual(draftio.feature_counts(d)["segments_with_speed"], 1)

    def test_disabled_segments_counted(self):
        """visible:false 才是停用；visible:true / 没有这个键都不算。"""
        d = mini_draft(3)
        segs = d["tracks"][0]["segments"]
        segs[0]["visible"] = False
        segs[1]["visible"] = True
        self.assertEqual(draftio.feature_counts(d)["segments_disabled"], 1)

    def test_cache_path_changes_with_source_range(self):
        """只拖出点（入点/速度/占位都不变）也必须换缓存文件，否则复用旧画面。"""
        a = retime.cache_path("D1", "SEG1", 2.0, 0, 30, range_us=2_000_000)
        b = retime.cache_path("D1", "SEG1", 2.0, 0, 30, range_us=1_000_000)
        self.assertNotEqual(a, b)
        self.assertEqual(a, retime.cache_path("D1", "SEG1", 2.0, 0, 30,
                                              range_us=2_000_000))

    def test_cache_path_backward_compatible(self):
        """老调用（不传 range_us）仍然生成同一个名字。"""
        self.assertEqual(retime.cache_path("D1", "SEG1", 2.0, 0, 30),
                         retime.cache_path("D1", "SEG1", 2.0, 0, 30))

    def test_read_with_deadline_raises_nodraft(self):
        """限时读取扫不出来时给 NoDraft（可读），而不是 None 传下去崩掉。"""
        from unittest import mock
        with mock.patch.object(draftio, "identify",
                               return_value={"folder": "", "timeline_id": "X"}):
            with mock.patch.object(draftio, "_scan_open_draft", return_value=None):
                with self.assertRaises(draftio.NoDraft):
                    draftio.read(deadline=1)

    def test_read_with_deadline_passes_it_through(self):
        from unittest import mock
        seen = {}

        def fake_sync_mem(deadline=70, expected=None, target_bytes=None):
            seen["d"] = deadline
            seen["t"] = target_bytes
            return {"draft": {"id": "X", "duration": 0}}

        with mock.patch.object(draftio, "identify",
                               return_value={"folder": "", "timeline_id": "X"}), \
                mock.patch.object(draftio, "_scan_open_draft", side_effect=fake_sync_mem):
            draftio.read(allow_stale=True, deadline=6)
        self.assertEqual(seen["d"], 6)
        # 磁盘载荷也要传下去：内存里常有 2~3 份同 ID 副本，只有拿载荷体量
        # 才能挑出「与磁盘同一版」的那份（folder 为空时这里是 0）。
        self.assertIn("t", seen)
        self.assertEqual(seen["t"], 0)

    def test_compound_part_without_path_flagged(self):
        """内部段没有素材路径：Path("") 会退化成 "."，老写法漏报。"""
        d = mini_draft(1)
        inner = {"materials": {"videos": [{"id": "V0", "path": ""}]},
                 "tracks": [{"type": "video", "segments": [{
                     "id": "IN0", "material_id": "V0",
                     "target_timerange": {"start": 0, "duration": 4_000_000},
                     "source_timerange": {"start": 0, "duration": 4_000_000}}]}]}
        d["materials"]["drafts"] = [{"id": "C1", "draft": inner}]
        d["tracks"][0]["segments"][0]["extra_material_refs"] = ["C1"]
        b, w, _f = audit.scan_draft(d)
        codes = {x["code"] for x in (b + w)}
        self.assertIn("compound_empty", codes)

    def test_load_cube_with_bom(self):
        """厂商 cube 常带 BOM：不去掉会把 LUT_3D_SIZE 行当成数据行。"""
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "bom.cube")
            body = ["LUT_3D_SIZE 2",
                    "0 0 0", "1 0 0", "0 1 0", "1 1 0",
                    "0 0 1", "1 0 1", "0 1 1", "1 1 1"]
            with open(p, "w", encoding="utf-8-sig") as f:      # 写 BOM
                f.write("\n".join(body))
            size, data = pull.load_cube(p)
            self.assertEqual(size, 2)
            self.assertEqual(len(data), 8)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_has_audio_unknown_without_pyav(self):
        """没有 PyAV 时返回 None（不确定），不能当成「没有音轨」偷偷丢声音。"""
        with mock.patch.dict(sys.modules, {"av": None}):
            self.assertIsNone(retime._has_audio(sys.executable))

    def test_unique_name_appends_suffix(self):
        from jybridge import workflow
        d = tempfile.mkdtemp()
        try:
            with mock.patch.object(workflow.JY, "draft_root",
                                   return_value=Path(d)):
                self.assertEqual(workflow._unique_name("成片"), "成片")
                (Path(d) / "成片").mkdir()
                self.assertEqual(workflow._unique_name("成片"), "成片_2")
                (Path(d) / "成片_2").mkdir()
                self.assertEqual(workflow._unique_name("成片"), "成片_3")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestMaskAndCopies(unittest.TestCase):
    """蒙版识别 + 内存多副本挑最新 —— 2026-11-14 用户加蒙版后暴露的两个问题。"""

    FOLDER = "C:/fake-draft"

    @staticmethod
    def with_masks(d, seg_index=0, name="线性", keyframed=False):
        m = {"id": "MSK-01", "type": "mask", "name": name,
             "constant_material_id": "CM-01", "resource_type": "line",
             "config": {"width": 0.28}}
        d["materials"].setdefault("common_mask", []).append(m)
        seg = d["tracks"][0]["segments"][seg_index]
        seg["extra_material_refs"] = list(seg.get("extra_material_refs") or []) + ["MSK-01"]
        if keyframed:
            seg["common_keyframes"] = [{
                "id": "KF-1", "material_id": "CM-01",
                "property_type": "KFTypeCommonMaskPositionX",
                "keyframe_list": [{"id": "K1", "values": [0.0]}]}]
        return d

    def test_mask_counted_from_common_mask(self):
        """剪映 11.x 的蒙版在 materials.common_mask；只认 enable_adjust_mask 会数出 0。"""
        d = self.with_masks(mini_draft(2), 0)
        f = draftio.feature_counts(d)
        self.assertEqual(f["segments_with_mask"], 1)
        self.assertEqual(f["mask_materials"], 1)
        self.assertEqual(f["mask_names"], ["线性"])

    def test_mask_keyframes_detected(self):
        d = self.with_masks(mini_draft(2), 1, name="矩形", keyframed=True)
        f = draftio.feature_counts(d)
        self.assertEqual(f["segments_with_mask"], 1)
        self.assertEqual(f["segments_with_mask_keyframes"], 1)

    def test_records_carry_masks(self):
        """写回核对要靠这份记录，蒙版必须进去。"""
        d = self.with_masks(mini_draft(2), 0)
        r = draftio.transition_records(d)
        self.assertEqual(r["SEG-00"]["masks"], ["MSK-01"])
        self.assertEqual(r["SEG-00"]["mask_names"], ["线性"])
        self.assertEqual(r["SEG-01"]["masks"], [])

    # ---------- 内存多副本 ----------

    def _fake_scan(self, drafts, target):
        """把 extract_open_draft 的内部依赖全部打桩，只测「挑哪一份」。"""
        from jybridge import memscan as MS
        blob = b'{"id":"UUID-1","version":1,' + b" " * 400

        def fake_regions(h):
            return [(0x1000, 65536)]

        def fake_read(h, addr, size):
            return blob * 3

        for d in drafts:
            d["id"] = "UUID-1"
        it = iter(drafts)
        with mock.patch.object(MS, "regions", side_effect=fake_regions), \
                mock.patch.object(MS, "read", side_effect=fake_read), \
                mock.patch.object(MS, "_try_root", side_effect=lambda h, s, a: next(it, None)), \
                mock.patch.object(MS.k32, "OpenProcess", return_value=1), \
                mock.patch.object(MS.k32, "CloseHandle", return_value=1), \
                mock.patch.object(MS, "GRACE_SEC", 0.0):
            pid, d = MS.extract_open_draft(pid=1, uuid_hint="UUID-1",
                                           target_bytes=target)
        return pid, d, dict(MS.LAST_SCAN)

    def test_picks_copy_closest_to_disk_payload(self):
        """三份同 ID 副本：老的（无蒙版）在两份里，新的（带蒙版）只有一份。

        老实现「找到第一份就 break」，读到的是 60109 那份 → 送进去的时间线
        没有蒙版，写回还会把蒙版抹掉。现在要挑体量最接近磁盘载荷的那份。
        """
        small = mini_draft(2)
        big = self.with_masks(mini_draft(2), 0)
        # 蒙版材质带一条真实长度的贴图路径，把体量差拉过 GOOD_TOL
        big["materials"]["common_mask"][0]["path"] = "C:/Users/test/" + "a" * 500
        target = len(json.dumps(big, ensure_ascii=False,
                                separators=(",", ":")).encode("utf-8"))
        self.assertGreater(target - len(json.dumps(small, ensure_ascii=False,
                                                   separators=(",", ":")).encode("utf-8")),
                           320 + 50)
        pid, d, meta = self._fake_scan([small, big, small], target)
        self.assertEqual(pid, 1)
        self.assertIn("common_mask", d["materials"], "挑到了没有蒙版的那份")
        self.assertEqual(meta["count"], 3)
        self.assertTrue(meta["structure_consistent"])

    def test_single_copy_still_refused_when_size_off(self):
        """只有一份副本时，「结构一致」是自己跟自己比，不能当通行证。"""
        d = mini_draft(2)
        fresh = draftio.freshness({"folder": self.FOLDER}, d,
                                  {"count": 1, "structure_consistent": True,
                                   "picked_size": 100, "picked_delta": -900})
        self.assertFalse(fresh["copy_ok"])

    def test_multiple_copies_same_structure_still_stale(self):
        """三份副本结构一致也要判 stale：老副本结构当然一致，缺的是蒙版。"""
        d = mini_draft(2)
        with mock.patch.object(draftio, "disk_payload_size", return_value=100000), \
                mock.patch.object(draftio, "disk_saved_at", return_value=0):
            fresh = draftio.freshness({"folder": self.FOLDER}, d,
                                      {"count": 3, "structure_consistent": True,
                                       "picked_size": 98000, "picked_delta": -2000})
        self.assertTrue(fresh["stale"])
        self.assertTrue(fresh["copy_ok"], "这一项只用于提示，不能当通行证")

    def test_multiple_copies_diff_structure_is_stale(self):
        d = mini_draft(2)
        with mock.patch.object(draftio, "disk_payload_size", return_value=100000), \
                mock.patch.object(draftio, "disk_saved_at", return_value=0):
            fresh = draftio.freshness({"folder": self.FOLDER}, d,
                                      {"count": 3, "structure_consistent": False,
                                       "picked_size": 98000, "picked_delta": -2000})
        self.assertTrue(fresh["stale"])
        self.assertFalse(fresh["copy_ok"], "副本之间结构不一致时必须停下")

    def test_read_refuses_and_says_reopen(self):
        """差 1742 B 时不能放行，且必须告诉用户「关掉草稿再打开」。"""
        d = mini_draft(2)
        scan = {"count": 3, "structure_consistent": True,
                "picked_size": 100, "picked_delta": -1742, "elapsed": 1.0}
        with mock.patch.object(draftio, "identify",
                               return_value={"folder": self.FOLDER, "timeline_id": "DRAFT-MINI"}), \
                mock.patch.object(draftio, "_scan_open_draft",
                                  return_value={"draft": d, "mem_candidates": scan}), \
                mock.patch.object(draftio, "disk_payload_size", return_value=999999), \
                mock.patch.object(draftio, "disk_saved_at", return_value=0):
            with self.assertRaises(draftio.StaleDraft) as cm:
                draftio.read(deadline=5)
        msg = str(cm.exception)
        self.assertIn("3 份同 ID 副本", msg)
        self.assertIn("关掉", msg)
        self.assertIn("Ctrl+S 不够", msg)

    def test_read_refuses_when_copies_disagree(self):
        d = mini_draft(2)
        scan = {"count": 3, "structure_consistent": False,
                "picked_size": 100, "picked_delta": -900, "elapsed": 1.0}
        with mock.patch.object(draftio, "identify",
                               return_value={"folder": self.FOLDER, "timeline_id": "DRAFT-MINI"}), \
                mock.patch.object(draftio, "_scan_open_draft",
                                  return_value={"draft": d, "mem_candidates": scan}), \
                mock.patch.object(draftio, "disk_payload_size", return_value=999999), \
                mock.patch.object(draftio, "disk_saved_at", return_value=0):
            with self.assertRaises(draftio.StaleDraft):
                draftio.read(deadline=5)

    def test_structure_sig_ignores_decorations(self):
        """结构指纹只比段结构：加蒙版/关键帧不该改变它，剪掉一段必须改变。"""
        from jybridge import memscan as MS
        a = mini_draft(2)
        b = self.with_masks(mini_draft(2), 0, keyframed=True)
        self.assertEqual(MS._structure(a), MS._structure(b))
        c = mini_draft(2)
        c["tracks"][0]["segments"][1]["target_timerange"]["start"] += 1000
        self.assertNotEqual(MS._structure(a), MS._structure(c))

    def test_compact_size_counts_bytes_not_chars(self):
        """中文名一段就差几百字节，必须按 UTF-8 字节跟磁盘载荷对账。"""
        from jybridge import memscan as MS
        d = mini_draft(1)
        d["materials"]["videos"][0]["name"] = "中文名字"
        chars = len(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(MS._compact_size(d), chars + 8)


class TestUnsentFeatures(unittest.TestCase):
    """「草稿里有、但送不进达芬奇」的清单。

    这份清单是用户判断「达芬奇里能调到什么」的唯一依据，所以每一项都要
    数得准：数错（比如把蒙版数成 0）等于让人以为已经送进去了。
    """

    @staticmethod
    def codes(rows):
        return {r["feature"]: r["count"] for r in rows}

    def test_text_sticker_filter_audio_counted(self):
        d = mini_draft(2)
        d["tracks"] += [
            {"id": "T2", "type": "text", "segments": [{"id": "TX1"}, {"id": "TX2"}]},
            {"id": "T3", "type": "sticker", "segments": [{"id": "ST1"}]},
            {"id": "T4", "type": "filter", "segments": [{"id": "FL1"}]},
            {"id": "T5", "type": "audio", "segments": [{"id": "AU1"}]},
        ]
        d["materials"]["audio_fades"] = [{"id": "AF1"}]
        rows = draftio.unsupported(d)
        c = self.codes(rows)
        self.assertEqual(c["text"], 2)
        self.assertEqual(c["sticker"], 1)
        self.assertEqual(c["filter"], 1)
        self.assertEqual(c["audio"], 1)
        self.assertEqual(c["audio_fade"], 1)
        for r in rows:
            self.assertFalse(r["sent"], "这些元素本来就不该被当成已送入")
            self.assertTrue(r["writeback"], "写回从快照克隆，装饰必须原样带回")

    def test_mask_detail_reported(self):
        d = mini_draft(3)
        d["materials"]["common_mask"] = [
            {"id": "MSK-1", "type": "mask", "name": "矩形",
             "constant_material_id": "CM-1"}]
        seg = d["tracks"][0]["segments"][1]
        seg["extra_material_refs"] = ["MSK-1"]
        seg["common_keyframes"] = [{
            "id": "KF-1", "material_id": "CM-1",
            "property_type": "KFTypeCommonMaskPositionX",
            "keyframe_list": [{"id": "K1", "values": [0.0]}]}]
        rows = draftio.mask_segments(d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_id"], "SEG-01")
        self.assertEqual(rows[0]["lane"], 1)
        self.assertEqual(rows[0]["start_sec"], 4.0)
        self.assertEqual(rows[0]["mask_names"], ["矩形"])
        self.assertEqual(rows[0]["mask_keyframes"], 1)

    def test_transform_only_when_not_identity(self):
        d = mini_draft(2)
        d["tracks"][0]["segments"][0]["clip"] = {
            "scale": {"x": 1.49, "y": 1.49}, "transform": {"x": 0, "y": 0}}
        d["tracks"][0]["segments"][1]["clip"] = {
            "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0, "y": 0},
            "rotation": 0, "flip": {"horizontal": False, "vertical": False}}
        rows = draftio.transform_segments(d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["segment_id"], "SEG-00")
        self.assertEqual(rows[0]["transform"]["scale"], [1.49, 1.49])
        self.assertIn("transform", self.codes(draftio.unsupported(d)))

    def test_rotation_and_flip_detected(self):
        d = mini_draft(2)
        d["tracks"][0]["segments"][0]["clip"] = {"rotation": 90}
        d["tracks"][0]["segments"][1]["clip"] = {
            "flip": {"horizontal": True, "vertical": False}}
        rows = draftio.transform_segments(d)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["transform"]["rotation"], 90)
        self.assertEqual(rows[1]["transform"]["flip"], [True, False])

    def test_curve_speed_counted(self):
        d = add_speed(mini_draft(2), 0, 1.4667)
        d["materials"]["speeds"][0]["curve_speed"] = {
            "name": "蒙太奇",
            "speed_points": [{"x": 0.0, "y": 0.9}, {"x": 0.5, "y": 6.8},
                             {"x": 1.0, "y": 1.0}]}
        self.assertEqual(draftio.curve_speed_segments(d), 1)
        self.assertEqual(self.codes(draftio.unsupported(d))["curve_speed"], 1)

    def test_photo_segment_skipped_with_real_reason(self):
        """图片段没有 videos.path，老逻辑报「素材不在盘上」，像文件丢了。"""
        d = mini_draft(1)
        d["materials"]["photos"] = [{"id": "PH1", "path": "C:/tmp/a.png",
                                     "duration": 3_000_000}]
        d["tracks"][0]["segments"].append({
            "id": "SEG-PH", "material_id": "PH1", "extra_material_refs": [],
            "source_timerange": {"start": 0, "duration": 3_000_000},
            "target_timerange": {"start": 4_000_000, "duration": 3_000_000}})
        plan, skipped, _o, _t, _s = send.build_plan(d)
        self.assertEqual(len(plan), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("图片素材当前不送", skipped[0]["reason"])
        self.assertEqual(self.codes(draftio.unsupported(d))["photo"], 1)

    def test_compound_inner_decoration_reported(self):
        """复合片段内部的特效/文本：外观看只有 1 段，里面丢的东西要报出来。"""
        inner = {"materials": {"effects": [{"id": "E1"}], "hsl": [{"id": "H1"}]},
                 "tracks": [
                     {"type": "video", "segments": [
                         {"id": "IS1", "extra_material_refs": ["E1"]}]},
                     {"type": "text", "segments": [{"id": "IT1"}]}]}
        d = mini_draft(1)
        with mock.patch.object(draftio, "compound_index",
                               return_value={"C1": {"name": "复合"}}), \
                mock.patch.object(draftio, "compound_inner",
                                  return_value=(inner, "embedded")):
            rows = draftio.compound_inner_unsent(d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["compound"], "C1")
            self.assertEqual(rows[0]["decorated_segments"], 1)
            self.assertEqual(rows[0]["extra_tracks"], 1)
            self.assertEqual(self.codes(draftio.unsupported(d))["compound_inner"], 2)

    def test_plain_draft_reports_nothing(self):
        """没有装饰的草稿不该冒出一堆「不送」行。"""
        self.assertEqual(draftio.unsupported(mini_draft(2)), [])


class TestPlainSources(unittest.TestCase):
    """草稿明文的两个离线来源（缓存 / 载入备份）。

    背景：剪映把明文留在进程内存里只有一段时间，打开久了就回收 —— 2026-11-20
    实测一份放了 1 小时、没动过的草稿，内存里只剩 167 KB 残片，用户点送入
    直接失败。这两条来源就是为了让这种情况还能读到，而且**不能读错版本**。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old_cache = plain.CACHE_ROOT
        plain.CACHE_ROOT = os.path.join(self.tmp, "plain")
        self.addCleanup(setattr, plain, "CACHE_ROOT", self.old_cache)
        self.folder = os.path.join(self.tmp, "草稿A")
        os.makedirs(os.path.join(self.folder, ".backup"))
        self._write_disk(b'{"id":"x","tracks":[]}')
        # .locked 比编辑时间新 —— 「打开之后没改过」
        Path(self.folder, ".locked").write_bytes(b"")
        os.utime(Path(self.folder, ".locked"),
                 (1795102800, 1795102800))

    def _write_disk(self, plaintext, edit_at=1795102693):
        import base64
        Path(self.folder, "draft_content.json").write_bytes(
            base64.b64encode(plaintext))
        Path(self.folder, "draft_settings").write_text(
            "[General]\ndraft_last_edit_time=%d\n" % edit_at,
            encoding="utf-8")
        os.utime(Path(self.folder, "draft_content.json"),
                 (1795102700, 1795102700))

    def _cur(self, tid="T1"):
        return {"timeline_id": tid, "name": "草稿A", "folder": self.folder}

    def _draft(self, tid="T1", n=1):
        return {"id": tid, "materials": {"videos": []},
                "tracks": [{"type": "video",
                            "segments": [{"id": "S%d" % i} for i in range(n)]}]}

    def _payload(self):
        import base64
        raw = Path(self.folder, "draft_content.json").read_bytes()
        return len(base64.b64decode(raw + b"=" * (-len(raw) % 4)))

    # ---- 缓存 ----
    def test_cache_roundtrip(self):
        cur = self._cur()
        path = plain.save(self._draft(), cur)
        self.assertTrue(os.path.isfile(path))
        hit = plain.from_cache(cur, self._payload())
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "缓存")
        self.assertEqual(len(hit["draft"]["tracks"][0]["segments"]), 1)

    def test_cache_invalidated_when_disk_saved_again(self):
        """磁盘又存过一次（载荷变了）→ 缓存必须作废，不能拿旧内容送入。"""
        cur = self._cur()
        plain.save(self._draft(n=1), cur)
        self._write_disk(b'{"id":"x","tracks":[],"new":"a much longer body"}')
        self.assertIsNone(plain.from_cache(cur, self._payload()))

    def test_cache_invalidated_when_edited_again(self):
        cur = self._cur()
        plain.save(self._draft(), cur)
        self._write_disk(b'{"id":"x","tracks":[]}', edit_at=1795102999)
        os.utime(Path(self.folder, "draft_content.json"),
                 (1795102700, 1795102700))
        self.assertIsNone(plain.from_cache(cur, self._payload()))

    def test_cache_rejects_other_draft_id(self):
        cur = self._cur("T1")
        plain.save(self._draft("T2"), cur)
        self.assertIsNone(plain.from_cache(cur, self._payload()))

    def test_cache_id_cannot_escape_root(self):
        """草稿 ID 来自 JSON，拼路径前必须过滤。"""
        for bad in ("../evil", "..\\evil", "a/b"):
            p = plain.cache_file(bad)
            self.assertEqual(os.path.dirname(os.path.abspath(p)),
                             os.path.abspath(plain.CACHE_ROOT), bad)

    # ---- 载入备份 ----
    def _backup(self, draft, name="20261119233823_abc.load.bak", mtime=None):
        p = Path(self.folder, ".backup", name)
        p.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def test_backup_reads_plaintext(self):
        self._backup(self._draft(), mtime=1795102690)
        hit = plain.from_backup(self._cur())
        self.assertIsNotNone(hit)
        self.assertEqual(hit["source"], "载入备份")
        self.assertEqual(hit["draft"]["id"], "T1")

    def test_backup_skips_ciphertext(self):
        """普通草稿的载入备份是密文 —— 读不出来就该跳过，不能当明文解析。"""
        Path(self.folder, ".backup", "20261119233823_abc.load.bak").write_bytes(
            b"\x8a\x02\x00\x00\xff\xfe" * 400)
        self.assertIsNone(plain.from_backup(self._cur()))

    def test_backup_rejects_other_draft(self):
        self._backup(self._draft("T9"), mtime=1795102690)
        self.assertIsNone(plain.from_backup(self._cur()))

    def test_backup_rejected_when_edited_after(self):
        """备份之后草稿又存过盘（最后编辑更晚）→ 那是编辑前的版本，不能用。"""
        self._backup(self._draft("T1", n=1), mtime=1795102690)
        self._write_disk(b'{"id":"x","tracks":[]}', edit_at=1795102800)
        os.utime(Path(self.folder, "draft_content.json"),
                 (1795102700, 1795102700))
        self.assertIsNone(plain.from_backup(self._cur()))

    def test_best_prefers_cache_over_backup(self):
        cur = self._cur()
        plain.save(self._draft(n=2), cur)
        self._backup(self._draft(n=5), mtime=1795102690)
        hit = plain.best(cur, self._payload())
        self.assertEqual(hit["source"], "缓存")
        self.assertEqual(len(hit["draft"]["tracks"][0]["segments"]), 2)


class TestReadFallsBack(unittest.TestCase):
    """draftio.read 在「内存里没有明文」时的行为。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old_cache = plain.CACHE_ROOT
        plain.CACHE_ROOT = os.path.join(self.tmp, "plain")
        self.addCleanup(setattr, plain, "CACHE_ROOT", self.old_cache)
        self.folder = os.path.join(self.tmp, "草稿B")
        os.makedirs(os.path.join(self.folder, ".backup"))
        import base64
        body = b'{"id":"T1","materials":{"videos":[]},"tracks":[]}'
        Path(self.folder, "draft_content.json").write_bytes(
            base64.b64encode(body))
        Path(self.folder, "draft_settings").write_text(
            "[General]\ndraft_last_edit_time=1795102693\n", encoding="utf-8")
        lock = Path(self.folder, ".locked")
        lock.write_bytes(b"")
        os.utime(lock, (1795102900, 1795102900))
        self.cur = {"timeline_id": "T1", "name": "草稿B",
                    "folder": self.folder, "pid": 1}
        self.draft = {"id": "T1", "duration": 1000000,
                      "materials": {"videos": []},
                      "tracks": [{"type": "video", "segments": [{"id": "S1"}]}]}

    def test_read_uses_backup_when_memory_empty(self):
        """内存扫不出来时用载入备份顶上，且不拿体积去卡它。

        剪映的载入备份是加载器序列化，同一份内容能比落盘版长 14%
        （实测 73683 vs 63143）—— 拿内存那套容差去卡会误拦。
        """
        bak = Path(self.folder, ".backup", "20261119233823_a.load.bak")
        bak.write_text(json.dumps(self.draft, ensure_ascii=False),
                       encoding="utf-8")
        os.utime(bak, (1795102695, 1795102695))
        with mock.patch.object(draftio, "identify", return_value=self.cur), \
                mock.patch.object(draftio, "_scan_open_draft", return_value={}):
            draft, cur, fresh = draftio.read()
        self.assertEqual(draft["id"], "T1")
        self.assertEqual(fresh["plain_source"], "载入备份")
        self.assertTrue(fresh["same_version"])
        self.assertFalse(fresh["stale"])

    def test_read_caches_what_it_read(self):
        bak = Path(self.folder, ".backup", "20261119233823_a.load.bak")
        bak.write_text(json.dumps(self.draft, ensure_ascii=False),
                       encoding="utf-8")
        os.utime(bak, (1795102695, 1795102695))
        with mock.patch.object(draftio, "identify", return_value=self.cur), \
                mock.patch.object(draftio, "_scan_open_draft", return_value={}):
            draftio.read()
            self.assertTrue(os.path.isfile(plain.cache_file("T1")))
            # 第二次：备份删了也能读（走缓存）
            os.remove(bak)
            draft2, _c, fresh2 = draftio.read()
        self.assertEqual(draft2["id"], "T1")
        self.assertEqual(fresh2["plain_source"], "缓存")

    def test_read_raises_no_plaintext(self):
        """内存没有、备份没有、缓存没有 —— 报专门的 no_plaintext，不要含糊。"""
        with mock.patch.object(draftio, "identify", return_value=self.cur), \
                mock.patch.object(draftio, "_scan_open_draft", return_value={}):
            with self.assertRaises(draftio.NoPlaintext) as cm:
                draftio.read()
        msg = str(cm.exception)
        self.assertIn("完整明文", msg)
        self.assertIn("关掉", msg)
        self.assertIn("重新打开", msg)
        self.assertIsInstance(cm.exception, draftio.NoDraft)

    def test_no_plaintext_message_blames_edit_when_edited(self):
        """打开后改过 + 明文没了 —— 先说是改过，别让人以为是「放久了」。"""
        Path(self.folder, "draft_settings").write_text(
            "[General]\ndraft_last_edit_time=1795103600\n", encoding="utf-8")
        msg = draftio.no_plaintext_message(self.cur)
        self.assertIn("又被编辑过", msg)
        self.assertIn("重新打开", msg)


# ---------------------------------------------------------------- 达芬奇 API 延迟加载
#
# 2026-11-20 的 CI 教训：顶层 `import DaVinciResolveScript` 会让「没装达芬奇」的机器
# 连 `import jybridge.send` 都失败。这三条钉住「不用 API 的功能必须在裸机上可用」。

class TestResolveLazy(unittest.TestCase):
    def test_all_modules_import_without_resolve(self):
        """子进程里把搜索路径清空，模拟没装达芬奇的机器。"""
        import subprocess
        code = (
            "import jybridge.resolve as r; r.MOD_DIRS = () ;"
            "import jybridge.send, jybridge.pull, jybridge.preflight, jybridge.lock,"
            " jybridge.audit, jybridge.workflow, jybridge.draftbuild;"
            "assert r.available() is False;"
            "print('ok')"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, (out.stderr or "")[-800:])
        self.assertIn("ok", out.stdout)

    def test_loud_error_when_api_missing(self):
        saved_dirs, saved = resolve.MOD_DIRS, resolve._loaded
        resolve.MOD_DIRS, resolve._loaded = (), None
        try:
            with self.assertRaises(resolve.ResolveUnavailable) as ctx:
                resolve.load()
            self.assertIn("check", str(ctx.exception))     # 告诉人哪些命令不受影响
        finally:
            resolve.MOD_DIRS, resolve._loaded = saved_dirs, saved

    def test_proxy_is_patchable_without_resolve(self):
        """测试里 patch `send.dvr.scriptapp` 不能反过来要求先装达芬奇。"""
        sentinel = mock.Mock()
        with mock.patch.object(send.dvr, "scriptapp", return_value=sentinel) as m:
            self.assertIs(send.dvr.scriptapp("Resolve"), sentinel)
            self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)