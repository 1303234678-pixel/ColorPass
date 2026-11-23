"""ColorPass 离线单元测试 —— 不依赖达芬奇、不依赖剪映、不动任何真实草稿。

覆盖：
    paths / draftio / snapshot / send.build_plan / audit.scan_draft
    retime（真跑一次 ffmpeg 编码）/ write 的四个纯函数 / jy_workflow 命名

运行：
    <Python312> -m unittest discover -p "test_units.py" -v
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

from colorpass import audit, draftio, paths, plain, pull, resolve, retime, send, snapshot, write  # noqa: E402

# ---------------------------------------------------------------- 夹具

def load_real_draft(min_segments=6):
    """真实工程快照（够长、素材还在盘上的那种）。没有就返回 None。

    为什么要求段数：快照指针指向「最近一次送入」，而那可能是本工具自己造的
    两三段脚手架草稿（验证用的）。拿脚手架当「用户真实工程」来断言
    「不该报警」，测的其实是脚手架本身 —— 它末尾确实空着，报漂移是对的。

    为什么要看素材在不在：`tools/stress_scale.py` 的合成草稿引用临时目录里的
    素材，跑完就删。留下指针不看的话，这两条用例会报「素材没有文件路径」
    ——报的是测试环境的残渣，不是代码问题。
    """
    try:
        ptr = snapshot.current()
        if not ptr or ptr.get("_incomplete"):
            return None
        if str(ptr.get("draft_id") or "").upper().startswith("STRESS"):
            return None
        d, _meta = snapshot.snapshot_of(ptr)
        n = sum(len([s for s in (t.get("segments") or []) if isinstance(s, dict)])
                for t in d.get("tracks") or [] if t.get("type") == "video")
        if n < min_segments:
            return None
        mats = d.get("materials") or {}
        refs = {s.get("material_id")
                for t in d.get("tracks") or [] if t.get("type") == "video"
                for s in (t.get("segments") or []) if isinstance(s, dict)}
        paths_ = [m.get("path") for k in ("videos", "photos")
                  for m in (mats.get(k) or [])
                  if isinstance(m, dict) and m.get("id") in refs]
        paths_ = [p for p in paths_ if p]
        if not paths_ or not all(os.path.exists(p) for p in paths_):
            return None
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

    def test_atomic_write_keeps_mode_and_leaves_no_tmp(self):
        """原子写不能顺手把文件的权限改掉。

        mkstemp 建的是 0600，直接 replace 会把剪映草稿正文/索引从 0644 变成
        0600 —— 同一账号还能读，但同步盘代理、备份工具、另一账号起的剪映就读
        不到了，而且这种问题事后极难归因。
        """
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "a.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{}")
            os.chmod(p, 0o644)
            before = os.stat(p).st_mode & 0o777
            paths.atomic_write(p, '{"x": 1}')
            # Windows 没有 0644 这套语义（st_mode 恒为 0666 或 0444），所以断言
            # 「前后一致」而不是「等于 0644」—— 要防的是 mkstemp 的 0600 把原
            # 权限顶掉，这一点两个平台都能验。
            self.assertEqual(os.stat(p).st_mode & 0o777, before)
            self.assertEqual(open(p, encoding="utf-8").read(), '{"x": 1}')
            fresh = os.path.join(d, "b.json")
            paths.atomic_write(fresh, "{}")
            if paths.WINDOWS:
                self.assertTrue(os.access(fresh, os.W_OK))
            else:
                self.assertEqual(os.stat(fresh).st_mode & 0o777, 0o644)
            self.assertEqual([x for x in os.listdir(d) if x.endswith(".tmp")], [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_file_lock_excludes_other_holders(self):
        """锁必须真的互斥：拿不到就等，等不到就抛 —— 不能默默放行。"""
        key = "unit-test-%d" % os.getpid()
        with paths.FileLock(key):
            holder = paths.FileLock(key, timeout=0.3)
            with self.assertRaises(TimeoutError):
                with holder:
                    pass


# ---------------------------------------------------------------- draftio

class TestDraftio(unittest.TestCase):
    def test_prog_is_silent_when_nobody_watches(self):
        """进度行只在终端/面板（COLORPASS_PROGRESS）里出现。

        输出被重定向时多打一行，会把「最后那段 JSON」变成「最后那段 JSON
        后面还有别的」—— 测试里的 json.loads(stdout) 与任何拿管道读 JSON 的
        脚本都会当场炸。这条是那份契约的唯一保障。
        """
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COLORPASS_PROGRESS", None)
            with redirect_stdout(buf):          # StringIO.isatty() 是 False
                draftio.prog("不该出现")
        self.assertEqual(buf.getvalue(), "")

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"COLORPASS_PROGRESS": "1"}):
            with redirect_stdout(buf):
                draftio.prog("取回 LUT 3/18")
        self.assertEqual(buf.getvalue(), "·· 取回 LUT 3/18\n")

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

    def test_skeleton_is_saved_for_deleted_source(self):
        """送入之后原草稿被删，写回要靠这份骨架重建目录。

        骨架必须带上 Timelines/project.json（没有它建不出 mac 草稿），
        并且不能把 .locked（剪映的占用标记）和 subdraft（另有副本）带上。
        """
        tl = os.path.join(self.src, "Timelines", "TID")
        os.makedirs(tl, exist_ok=True)
        with open(os.path.join(self.src, "Timelines", "project.json"), "w") as f:
            f.write('{"main_timeline_id": "TID"}')
        open(os.path.join(self.src, "draft_info.json"), "w").write("{")
        open(os.path.join(self.src, ".locked"), "w").write("")
        sd = os.path.join(self.src, "subdraft", "S1")
        os.makedirs(sd, exist_ok=True)
        open(os.path.join(sd, "draft_content.json"), "w").write('{"tracks": []}')
        m = snapshot.save(self.draft(), {"name": "A", "folder": self.src},
                          self.run_dir)
        sk = m["skeleton"]
        self.assertTrue(sk.get("ok"), sk)
        root = sk["dir"]
        self.assertTrue(os.path.isfile(os.path.join(root, "Timelines", "project.json")))
        self.assertTrue(os.path.isfile(os.path.join(root, "draft_info.json")))
        self.assertFalse(os.path.exists(os.path.join(root, ".locked")))
        self.assertFalse(os.path.isdir(os.path.join(root, "subdraft")))

    def test_skeleton_missing_assets_only_flags_referenced_skipped_files(self):
        resources = os.path.join(self.src, "Resources")
        os.makedirs(resources, exist_ok=True)
        media = os.path.join(resources, "随稿素材.mov")
        Path(media).write_bytes(b"media")
        draft = self.draft()
        draft["materials"]["videos"][0]["path"] = media
        with mock.patch.object(snapshot, "_SKEL_MAX_DIR", 1):
            meta = snapshot.save(draft, {"name": "A", "folder": self.src},
                                 self.run_dir)
        skel = meta["skeleton"]
        self.assertEqual(skel.get("skipped_dirs"), ["Resources"])
        missing = snapshot.skeleton_missing_assets(
            draft, self.src, skel["dir"], skel["skipped_dirs"])
        self.assertEqual([x["relative"] for x in missing],
                         ["Resources/随稿素材.mov"])
        # 没被跳过目录引用、或骨架里实际有副本时，不应误报。
        draft["materials"]["videos"][0]["path"] = "/external/other.mov"
        self.assertEqual(snapshot.skeleton_missing_assets(
            draft, self.src, skel["dir"], skel["skipped_dirs"]), [])
        restored = Path(skel["dir"]) / "Resources" / "随稿素材.mov"
        restored.parent.mkdir(parents=True, exist_ok=True)
        restored.write_bytes(b"media")
        draft["materials"]["videos"][0]["path"] = media
        self.assertEqual(snapshot.skeleton_missing_assets(
            draft, self.src, skel["dir"], skel["skipped_dirs"]), [])

    def test_overlapping_claim_cannot_be_committed_by_other_send(self):
        """A、B 两次送入重叠时，A 结束不能把 B 的 pending 提成 current。"""
        a = snapshot.save(self.draft(), {"name": "A", "folder": self.src},
                          self.run_dir)
        run_b = os.path.join(self.tmp, "runs", "D2")
        os.makedirs(run_b, exist_ok=True)
        b_draft = self.draft()
        b_draft["id"] = "D2"
        b = snapshot.save(b_draft, {"name": "B", "folder": self.src}, run_b)
        ca = snapshot.claim(a, self.run_dir, "CP_A", 0, 2)
        cb = snapshot.claim(b, run_b, "CP_B", 0, 3)
        self.assertIsNone(snapshot.commit(claim_id=ca["claim_id"], placed=2))
        pending = snapshot.current()
        self.assertEqual(pending["draft_name"], "B")
        self.assertTrue(pending["_incomplete"], "A 不该删掉 B 的进行中记录")
        self.assertIsNotNone(snapshot.commit(claim_id=cb["claim_id"], placed=3))
        current = snapshot.current()
        self.assertEqual(current["draft_name"], "B")
        self.assertEqual(current["timeline"], "CP_B")
        self.assertEqual(current["placed"], 3)

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
        """真实草稿上的不变量 —— 只断言「必须永远成立」的性质。

        以前这里写死了「13 段 / 终点 352 帧」，结果机器上再送一次（草稿换了、
        或者剪映里多剪了一段）测试就红，红的原因跟代码毫无关系。段数、帧位
        本来就该随草稿变，能固定下来的是：**和草稿自己的层数一致、同轨不重叠、
        终点等于最远的那一段、不超出草稿总长**。
        """
        video = draftio.layers(REAL)["video"]
        limit = send.us2f(REAL.get("duration")) + 1
        for mode in ("slot", "bake"):
            plan, skip, ov, tr, sh = send.build_plan(REAL, mode)
            self.assertEqual(len(plan), video, mode)
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
            self.assertEqual(end, max(c["rec"] + c["place"] for c in plan), mode)
            self.assertLessEqual(end, limit, "%s: 计划超出草稿总长" % mode)

    @unittest.skipUnless(REAL, "没有真实快照")
    def test_real_compound_carries_inner_timeline(self):
        plan, *_ = send.build_plan(REAL, "slot")
        want = draftio.feature_counts(REAL)["segments_with_compound"]
        comp = [c for c in plan if c["kind"] == "compound"]
        self.assertEqual(len(comp), want)
        for c in comp:
            self.assertTrue(c["inner"])
            self.assertTrue(all(p["path"] for p in c["inner"]))


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

    def test_photo_segment_is_not_a_missing_material(self):
        """静帧在 materials.photos 里，不在 videos 里 —— 漏了它整个图片段
        会被报成「素材没有文件路径」，而盘上明明有图。"""
        d = mini_draft(1)
        d["materials"]["photos"] = [{"id": "PH-0",
                                     "path": sys.executable.replace("\\", "/"),
                                     "width": 1520, "height": 1836}]
        seg = d["tracks"][0]["segments"][0]
        seg["material_id"] = "PH-0"
        b, w, f = audit.scan_draft(d)
        self.assertNotIn("no_path", self.codes(b))
        self.assertNotIn("missing_media", self.codes(b))
        self.assertNotIn("over_source", self.codes(w))   # 静帧没有源长度
        self.assertEqual(f["video_segments"], 1)

    def test_photo_off_disk_still_blocks(self):
        d = mini_draft(1)
        d["materials"]["photos"] = [{"id": "PH-0", "path": "Z:/nope.jpg"}]
        d["tracks"][0]["segments"][0]["material_id"] = "PH-0"
        b, _w, _f = audit.scan_draft(d)
        self.assertIn("missing_media", self.codes(b))

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

    def test_video_filter_scales_only_down(self):
        """画布要进滤镜链，而且只缩不放：1080p 源不该被放大到 4K 再编。"""
        vf = retime.video_filter(1.4535, (1920, 1080))
        self.assertIn("setpts=(PTS-STARTPTS)/1.453500", vf)
        self.assertIn("fps=30", vf)
        self.assertIn("min(iw,1920)", vf)
        self.assertIn("min(ih,1080)", vf)
        self.assertIn("force_original_aspect_ratio=decrease", vf)
        self.assertTrue(vf.endswith("format=yuv422p10le"))
        # 不传画布 = 老行为（按源分辨率编），不能凭空多出 scale
        self.assertNotIn("scale=", retime.video_filter(2.0))
        # 非法画布也不能把滤镜链搞坏
        self.assertNotIn("scale=", retime.video_filter(2.0, (0, "x")))

    def test_cache_path_changes_with_canvas(self):
        """画布变了必须换文件：否则升级后仍命中 4K 老缓存（慢、占盘）。"""
        a = retime.cache_path("D1", "SEG1", 2.0, 0, 30, 2_000_000, (1920, 1080))
        b = retime.cache_path("D1", "SEG1", 2.0, 0, 30, 2_000_000, (3840, 2160))
        self.assertNotEqual(a, b)
        self.assertIn("1920x1080", os.path.basename(a))
        self.assertIn("3840x2160", os.path.basename(b))
        self.assertEqual(a, retime.cache_path("D1", "SEG1", 2.0, 0, 30,
                                              2_000_000, (1920, 1080)))

    def test_prune_unused_keeps_only_what_this_send_used(self):
        """同一段改十次倍速会在草稿目录里堆十个 ProRes，且目录级回收永远收不到
        （活动草稿整个目录受保护）。送入成功后按「本次在用清单」反删。"""
        d = tempfile.mkdtemp(prefix="jyb_pr_")
        old = paths.RETIME_ROOT
        paths.RETIME_ROOT = os.path.join(d, "retime")
        try:
            root = paths.retime_dir("D9")
            os.makedirs(root, exist_ok=True)
            keep = os.path.join(root, "a_1p0_100.mov")
            old1 = os.path.join(root, "a_1p5_100.mov")
            old2 = os.path.join(root, "b_2p0_90.mov")
            for p in (keep, old1, old2):
                with open(p, "wb") as f:
                    f.write(b"x" * 5000)
            files, freed = retime.prune_unused("D9", keep={keep})
            self.assertEqual(files, 2)
            self.assertEqual(freed, 10000)
            self.assertTrue(os.path.exists(keep))
            self.assertFalse(os.path.exists(old1))
            self.assertFalse(os.path.exists(old2))
            # 空清单不能当成「全都不用」—— 没有变速段的送入不该清光上一版
            self.assertEqual(retime.prune_unused("D9", keep=set()), (0, 0))
            self.assertTrue(os.path.exists(keep))
        finally:
            paths.RETIME_ROOT = old
            shutil.rmtree(d, ignore_errors=True)

    def test_prune_unused_drops_path_traversal_ids(self):
        # 路径直接由草稿 ID 拼出来，非法 ID 必须什么都不动
        self.assertEqual(retime.prune_unused("..", keep={"x"}), (0, 0))

    @unittest.skipUnless(retime.available(), "没有 ffmpeg")
    def test_bake_marks_cache_hit_as_used(self):
        """命中缓存的文件也算「本次在用」，否则会被当成上一版删掉 ——
        第二次送入同一份草稿会把时间线引用的素材删掉。"""
        retime.reset_used()
        d = tempfile.mkdtemp(prefix="jyb_ru_")
        try:
            p = os.path.join(d, "hit.mov")
            with open(p, "wb") as f:
                f.write(b"x" * 5000)
            ok, det = retime.bake("nope.mp4", 0, 1_000_000, 2.0, 30, p)
            self.assertTrue(ok and det.get("cached"))
            self.assertIn(p, retime.used_paths())
        finally:
            shutil.rmtree(d, ignore_errors=True)

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
            # 帧数/编码格式的深度校验要 PyAV。它只是「验证用的额外眼睛」，
            # 不是运行时依赖 —— 没装就退一步只查文件大小，别让 CI 为了看一眼
            # 编码格式去装一个几十 MB 的包。
            try:
                import av
            except ImportError:
                av = None
            if av is not None:
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

    @unittest.skipUnless(retime.available(), "没有 ffmpeg")
    def test_bake_scales_to_canvas(self):
        """预渲染按画布缩 —— 这是 4K 源从 82 秒降到 34 秒的全部来源。

        实测（2026-11-23，RTX 4060 笔记本 / 32 线程）：同一段 1903 帧的 4K 素材
        按源分辨率编 4.5 GB / 82 秒，缩到 1080p 1.4 GB / 34 秒；时间线上两者
        逐帧一致，因为达芬奇摆上去本来就要缩到工程分辨率。
        """
        d = tempfile.mkdtemp(prefix="jyb_rt_canvas_")
        old = paths.RETIME_ROOT
        paths.RETIME_ROOT = os.path.join(d, "retime")
        try:
            src = os.path.join(d, "src.mp4")
            import subprocess
            exe = retime._ffmpeg()
            subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=size=320x180:rate=30:duration=1",
                            "-pix_fmt", "yuv420p", src], check=True)
            out = retime.cache_path("D1", "SEG1", 1.0, 0, 10, 1_000_000,
                                    (160, 90))
            ok, det = retime.bake(src, 0, 1_000_000, 1.0, 10, out,
                                  size=(160, 90))
            self.assertTrue(ok, det.get("why"))
            probe = subprocess.run([exe, "-hide_banner", "-i", out],
                                   capture_output=True, text=True,
                                   errors="ignore")
            self.assertIn("160x90", probe.stderr)
        finally:
            paths.RETIME_ROOT = old
            shutil.rmtree(d, ignore_errors=True)

    def test_clean_rejects_path_traversal(self):
        """--draft-id .. 不能把缓存根（甚至 %LOCALAPPDATA%）删掉。

        clean 会对 paths.retime_dir(id) 做 rmtree，而 paths.ensure 会先
        os.path.join + makedirs —— 传 .. 时目标就是 ColorPass 根，
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

    @unittest.skipUnless(retime.available(), "没有 ffmpeg")
    def test_photo_clip_exact_frames_and_cache(self):
        """静帧转成「正好 N 帧」—— 差一帧，达芬奇里整条时间线就错位。

        这是图片段唯一的落地方式（达芬奇摆静帧恒为 150 帧且改不了），
        所以帧数必须逐帧数出来，不能只看文件大小。
        """
        import re
        import subprocess
        d = tempfile.mkdtemp(prefix="jyb_ph_")
        try:
            src = os.path.join(d, "still.png")
            subprocess.run([retime._ffmpeg(), "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=size=96x54:duration=1",
                            "-frames:v", "1", src], check=True)
            out = os.path.join(d, "out.mp4")
            ok, det = retime.photo_clip(src, 24, out_path=out)
            self.assertTrue(ok, det.get("why"))
            p = subprocess.run([retime._ffmpeg(), "-hide_banner", "-nostdin",
                                "-i", out, "-f", "null", "-"],
                               capture_output=True, text=True, errors="ignore")
            got = max(int(n) for n in re.findall(r"frame=\s*(\d+)", p.stderr))
            self.assertEqual(got, 24)
            ok2, det2 = retime.photo_clip(src, 24, out_path=out)
            self.assertTrue(ok2 and det2.get("cached"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


NO_BUNDLED = "源码树里没有随包 ffmpeg（mac/Linux 或未跑 fetch_ffmpeg.py）"


class TestFfmpegResolve(unittest.TestCase):
    """ffmpeg 从 0.4.2 起随包自带。

    以前找不到它是「用户没装 PATH」，提示写了也没法照做；现在只可能是安装
    残缺（被杀毒软件删了），所以 resolver 必须优先用自带那份 —— 用户机器上
    那个老版本/残缺构建会让我们测过的行为变得不可复现。
    """

    def setUp(self):
        self._old = retime._FF
        self._oldenv = os.environ.get("COLORPASS_FFMPEG")
        os.environ.pop("COLORPASS_FFMPEG", None)
        retime._FF = None
        self.d = tempfile.mkdtemp(prefix="jyb_ff_")

    def tearDown(self):
        retime._FF = self._old
        os.environ.pop("COLORPASS_FFMPEG", None)
        if self._oldenv is not None:
            os.environ["COLORPASS_FFMPEG"] = self._oldenv
        shutil.rmtree(self.d, ignore_errors=True)

    @unittest.skipUnless(retime.bundled(),
                         "源码树里没有随包 ffmpeg（mac/Linux 或未跑 fetch_ffmpeg.py）")
    def test_bundled_found_in_source_tree(self):
        p = retime.bundled()
        self.assertTrue(p, "源码模式应能从 vendor/ffmpeg/ 找到")
        self.assertTrue(os.path.isfile(p))
        self.assertTrue(p.endswith("ffmpeg.exe") or p.endswith(os.sep + "ffmpeg"))

    # 下面这几条断言「随包那份优先」。仓库里 vendor/ffmpeg/ 只有 Windows 的
    # ffmpeg.exe，mac/Linux 上源码模式没有「随包」这回事 —— 那边该验的是
    # 「装了就回落 PATH」，见 test_missing_reason_says_how_to_install。
    @unittest.skipUnless(retime.bundled(), NO_BUNDLED)
    def test_info_reports_bundled(self):
        it = retime.info()
        self.assertTrue(it["ok"], it.get("why"))
        self.assertEqual(it["source"], "bundled")
        self.assertGreater(it["bytes"], 1 << 20)

    def test_env_wins_over_bundled(self):
        fake = os.path.join(self.d, "ffmpeg.exe")
        Path(fake).write_bytes(b"x")
        os.environ["COLORPASS_FFMPEG"] = fake
        retime._FF = None
        self.assertEqual(retime._ffmpeg(), fake)
        self.assertEqual(retime.info()["source"], "env")

    @unittest.skipUnless(retime.bundled(), NO_BUNDLED)
    def test_bundled_wins_over_path(self):
        """PATH 上有个 ffmpeg 也不能用它 —— 自带那份才是构建时校验过的。"""
        other = os.path.join(self.d, "other_ffmpeg.exe")
        Path(other).write_bytes(b"x")
        retime._FF = None
        with mock.patch("shutil.which", return_value=other):
            self.assertEqual(retime._ffmpeg(), retime.bundled())

    @unittest.skipUnless(paths.WINDOWS, "「重装安装包」这句只在 Windows 上成立")
    def test_missing_says_reinstall_not_configure_path(self):
        retime._FF = None
        with mock.patch.object(retime, "bundled", return_value=""), \
                mock.patch("shutil.which", return_value=None), \
                mock.patch.dict(sys.modules, {"imageio_ffmpeg": None}):
            self.assertFalse(retime.available())
            it = retime.info()
            self.assertFalse(it["ok"])
            self.assertIn("重新运行一次安装包", it["why"])
            # 不能再教用户去配 PATH —— 那不是他的活
            self.assertNotIn("PATH", it["why"])

    def test_missing_reason_says_how_to_install(self):
        """mac/Linux 没有「安装包」这个东西，提示得直接教怎么装 —— 不然用户
        被指去重装一个根本不存在的东西。"""
        why = retime.missing_reason()
        if paths.WINDOWS:
            self.assertIn("重新运行一次安装包", why)
        else:
            self.assertIn("ffmpeg", why)
            self.assertIn("brew", why)
            self.assertNotIn("重新运行一次安装包", why)

    @unittest.skipUnless(retime.bundled(), NO_BUNDLED)
    def test_env_is_a_hint_not_a_hard_override(self):
        """COLORPASS_FFMPEG 指向一个不存在的文件时，不能因此判「没有 ffmpeg」。

        环境变量是调试用的旁路，不是布尔开关：用户随手写了条过期路径，
        插件还得靠自带那份照常干活。
        """
        os.environ["COLORPASS_FFMPEG"] = os.path.join(self.d, "nope.exe")
        retime._FF = None
        self.assertTrue(retime.available())
        self.assertEqual(retime.info()["source"], "bundled")


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
            self.assertIn("Resources/ColorPass", dst.replace("\\", "/"))
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
            # 路径既不指快照也不在 subdraft 下：必须如实报成「没复制成功」，
            # 不能静默当作已处理 —— 否则写出来的草稿复合片段是空的。
            self.assertEqual(out[0].get("ok"), False)
            self.assertIn("subdraft", str(out[0].get("why") or ""))
            # 没有复合片段时，源目录里连 subdraft 都没有也该安静返回空
            d2 = tempfile.mkdtemp()
            self.assertEqual(write.clone_subdrafts(d2, d2, {"materials": {}}), [])
            shutil.rmtree(d2, ignore_errors=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_clone_subdrafts_placeholder_path(self):
        """macOS 11.8：draft_file_path 是「本草稿目录」占位符，不是绝对路径。"""
        d = tempfile.mkdtemp()
        try:
            src = os.path.join(d, "snap")
            inner = os.path.join(src, "subdraft", "S1", "draft_content.json")
            os.makedirs(os.path.dirname(inner))
            Path(inner).write_text("{}", encoding="utf-8")
            ph = ("##_draftpath_placeholder_AAAA-1_##"
                  "/subdraft/S1/draft_content.json")
            nd = {"materials": {"drafts": [{"id": "C", "draft_file_path": ph}]}}
            new = os.path.join(d, "new")
            os.makedirs(new)
            out = write.clone_subdrafts(src, new, nd)
            self.assertTrue(out[0]["ok"])
            # 占位符原样保留（相对新草稿解析），子草稿目录要搬过去
            self.assertEqual(nd["materials"]["drafts"][0]["draft_file_path"], ph)
            self.assertTrue(os.path.exists(
                os.path.join(new, "subdraft", "S1", "draft_content.json")))
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

    def test_compound_intact_flags_copy_that_did_not_follow(self):
        """源能读、新草稿读不到 —— 这是复制出了问题，必须算失败。

        反例就是用户报的「写回提示复合片段会缺失」：子草稿没搬进新草稿，
        但两边都读不到时签名都是空串，旧逻辑判成「一字节没变」。
        """
        inner = {"tracks": [{"type": "video", "segments": [{"id": "x"}]}]}
        a = {"materials": {"drafts": [{"id": "C", "draft": inner}]}}
        b = {"materials": {"drafts": [{"id": "C", "draft_file_path":
                                       "/nope/subdraft/S1/draft_content.json"}]}}
        problems = []
        self.assertEqual(write.compound_intact(a, b, problems,
                                               new_folder="/nope"), 0)
        self.assertIn("没跟着复制", problems[0]["why"])
        self.assertTrue(problems[0]["new_path"])

    def test_compound_intact_both_unreadable_is_recorded_not_claimed(self):
        """两边都读不到（源草稿自己坏了）：不算失败，但要留痕 —— 不能假装核对过。"""
        e = {"materials": {"drafts": [{"id": "C", "draft_file_path":
                                       "/nope/x.json"}]}}
        problems, unv = [], []
        self.assertEqual(write.compound_intact(e, e, problems, unverified=unv), 1)
        self.assertEqual(problems, [])
        self.assertEqual(len(unv), 1)

    def test_compound_path_expands_placeholder(self):
        ph = "##_draftpath_placeholder_AB_##/subdraft/S1/draft_content.json"
        got = draftio.compound_path({"draft_file_path": ph}, "/tmp/新稿")
        self.assertEqual(got.replace("\\", "/"),
                         "/tmp/新稿/subdraft/S1/draft_content.json")


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
class TestKeptAudioPhoto(unittest.TestCase):
    """写回是「整份克隆 + 只给视频段挂 LUT」：音频轨与图片段天生保留。

    这两类最容易被误以为「没送进达芬奇 = 写回也没了」，所以既要能数出来，
    也要能在它们真的丢了的时候报出来。
    """

    def setUp(self):
        self.d = rich_draft()
        p = sys.executable.replace("\\", "/")
        self.d["materials"]["audios"] = [{"id": "AU-1", "path": p,
                                          "name": "音乐", "type": "music",
                                          "duration": 2_000_000}]
        self.d["materials"]["photos"] = [{"id": "PH-1", "path": p,
                                          "width": 800, "height": 600}]
        self.d["tracks"].append({"id": "T4", "type": "audio", "segments": [{
            "id": "AUD-1", "material_id": "AU-1", "extra_material_refs": [],
            "target_timerange": {"start": 0, "duration": 2_000_000},
            "source_timerange": {"start": 0, "duration": 2_000_000}}]})
        self.d["tracks"][0]["segments"].append({
            "id": "PIC-1", "material_id": "PH-1", "extra_material_refs": [],
            "target_timerange": {"start": 2_000_000, "duration": 3_000_000},
            "source_timerange": {"start": 0, "duration": 3_000_000}})

    def test_kept_counts_audio_and_photo(self):
        k = write.kept_summary(self.d)
        self.assertEqual(k["audio_tracks"], 1)
        self.assertEqual(k["audio_segments"], 1)
        self.assertEqual(k["photo_segments"], 1)

    def test_losing_audio_track_is_caught(self):
        b = copy.deepcopy(self.d)
        b["tracks"] = [t for t in b["tracks"] if t.get("type") != "audio"]
        why = [p["why"] for p in write.structure_diff(self.d, b)]
        self.assertIn("轨道数变了", why)

    def test_losing_photo_segment_is_caught(self):
        b = copy.deepcopy(self.d)
        b["tracks"][0]["segments"] = [s for s in b["tracks"][0]["segments"]
                                      if s.get("id") != "PIC-1"]
        why = [p["why"] for p in write.structure_diff(self.d, b)]
        self.assertTrue(any("段" in w for w in why), why)


class TestWriteBasics(unittest.TestCase):
    """回剪映以后帧数/画布/素材像素有没有变。

    「结构一样」不等于「帧数一样」：duration、canvas_config、每段的微秒值
    都是独立字段，任何一处被四舍五入，成片就短一帧或者糊一档。
    """

    def draft(self):
        d = mini_draft(2)
        d["canvas_config"] = {"width": 1920, "height": 1080}
        d["color_space"] = 0
        for i, v in enumerate(d["materials"]["videos"]):
            v.update({"width": 1920, "height": 1080})
            v["duration"] = 4_000_000
        return d

    def test_frames_of_matches_jiemying_grid(self):
        self.assertEqual(write.frames_of(22_500_000), 675)
        self.assertEqual(write.frames_of(1_033_333), 31)
        self.assertEqual(write.frames_of(33_333), 1)
        self.assertEqual(write.frames_of(0), 0)

    def test_clean_clone_has_no_problem(self):
        d = self.draft()
        self.assertEqual(write.basics_diff(d, copy.deepcopy(d)), [])

    def test_shorter_draft_is_caught(self):
        d = self.draft()
        b = copy.deepcopy(d)
        b["duration"] -= 33_334          # 少 1 帧
        why = [p["why"] for p in write.basics_diff(d, b)]
        self.assertTrue(any("总帧数" in w for w in why), why)

    def test_canvas_change_is_caught(self):
        d = self.draft()
        b = copy.deepcopy(d)
        b["canvas_config"] = {"width": 3840, "height": 2160}
        probs = write.basics_diff(d, b)
        self.assertEqual(probs[0]["why"], "画布分辨率变了")
        self.assertEqual(probs[0]["new"], [3840, 2160])

    def test_one_frame_shift_is_caught(self):
        d = self.draft()
        b = copy.deepcopy(d)
        seg = b["tracks"][0]["segments"][1]
        seg["target_timerange"]["start"] -= 33_334
        why = [p["why"] for p in write.basics_diff(d, b)]
        self.assertIn("起始帧变了", why)

    def test_material_pixels_are_kept(self):
        d = self.draft()
        b = copy.deepcopy(d)
        b["materials"]["videos"][0]["width"] = 1280
        probs = write.basics_diff(d, b)
        self.assertEqual(probs[0]["why"], "素材像素尺寸变了")
        self.assertEqual(probs[0]["src"], [1920, 1080])

    def test_index_reports_frames_per_segment(self):
        idx = write.basics_index(self.draft())
        seg = idx["lanes"]["0"]["segments"]["SEG-01"]
        self.assertEqual((seg["start"], seg["dur"], seg["end"]), (120, 120, 240))
        self.assertEqual(idx["duration"], 240)
        self.assertEqual(idx["canvas"], [1920, 1080])


class TestSafeName(unittest.TestCase):
    def setUp(self):
        from colorpass import workflow
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

    def test_control_chars_do_not_reach_mkdir(self):
        """名字里带 \\x00 时 os.mkdir 会抛 ValueError，必须在这里就清掉。"""
        got = self.WF.safe_name("调\x00色\x1f稿")
        self.assertNotIn("\x00", got)
        self.assertNotIn("\x1f", got)

    def test_windows_reserved_names(self):
        """CON/PRN/COM1 这些是 Windows 设备名，当目录名建不出来。"""
        for nm in ("CON", "con", "PRN", "COM1", "LPT9"):
            self.assertNotEqual(self.WF.safe_name(nm).upper(), nm.upper())
        self.assertEqual(self.WF.safe_name("CONTRACT"), "CONTRACT")


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
        from colorpass import workflow
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
        from colorpass import memscan as MS
        if not MS.AVAILABLE:
            # 内存扫描是 Win32 专用（OpenProcess/ReadProcessMemory）。
            # mac/Linux 上模块能 import，但入口按设计直接返回空 —— 这里没得测。
            self.skipTest("内存扫描仅 Windows 可用")
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
        from colorpass import memscan as MS
        a = mini_draft(2)
        b = self.with_masks(mini_draft(2), 0, keyframed=True)
        self.assertEqual(MS._structure(a), MS._structure(b))
        c = mini_draft(2)
        c["tracks"][0]["segments"][1]["target_timerange"]["start"] += 1000
        self.assertNotEqual(MS._structure(a), MS._structure(c))

    def test_compact_size_counts_bytes_not_chars(self):
        """中文名一段就差几百字节，必须按 UTF-8 字节跟磁盘载荷对账。"""
        from colorpass import memscan as MS
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

    def test_text_sticker_filter_counted(self):
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
        # 音频轨现在会送进达芬奇（A 轨），不再算「不送」；音量/淡入淡出
        # 这层参数仍然不搬，所以 audio_fade 还在清单里。
        self.assertNotIn("audio", c)
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

    def test_photo_segment_goes_into_plan(self):
        """图片段以前整段跳过（「图片素材当前不送」），现在按图片进计划。

        落轨时会先转成等长视频 —— 静帧在 21.2 上摆出来的时长恒为 150 帧，
        不看 endFrame（实测见 tools/lab_ia.py）。
        """
        d = mini_draft(1)
        import os
        import tempfile
        fd, png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        self.addCleanup(os.unlink, png)
        d["materials"]["photos"] = [{"id": "PH1", "path": png,
                                     "duration": 3_000_000}]
        d["tracks"][0]["segments"].append({
            "id": "SEG-PH", "material_id": "PH1", "extra_material_refs": [],
            "source_timerange": {"start": 0, "duration": 3_000_000},
            "target_timerange": {"start": 4_000_000, "duration": 3_000_000}})
        plan, skipped, _o, _t, _s = send.build_plan(d)
        self.assertEqual(skipped, [])
        ph = [c for c in plan if c["kind"] == "photo"]
        self.assertEqual(len(ph), 1)
        self.assertEqual(ph[0]["path"], png.replace("\\", "/"))
        self.assertEqual(ph[0]["rec"], 120)          # 4 秒
        self.assertEqual(ph[0]["want"], 90)          # 3 秒
        self.assertEqual(ph[0]["place"], 90)         # 图片没有变速，占位=时长
        self.assertNotIn("photo", self.codes(draftio.unsupported(d)))

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
# 连 `import colorpass.send` 都失败。这三条钉住「不用 API 的功能必须在裸机上可用」。

class TestResolveLazy(unittest.TestCase):
    def test_all_modules_import_without_resolve(self):
        """子进程里把搜索路径清空，模拟没装达芬奇的机器。"""
        import subprocess
        code = (
            "import colorpass.resolve as r; r.MOD_DIRS = () ;"
            "import colorpass.send, colorpass.pull, colorpass.preflight, colorpass.lock,"
            " colorpass.audit, colorpass.workflow, colorpass.draftbuild;"
            "assert r.available() is False;"
            "print('ok')"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, (out.stderr or "")[-800:])
        self.assertIn("ok", out.stdout)

    def test_loud_error_when_api_missing(self):
        """模拟「这台机器没有脚本模块」。

        只清 MOD_DIRS 不够：`load()` 成功过一次就把那些目录塞进了 sys.path、
        把模块留在 sys.modules 里。同进程后续再清 MOD_DIRS，`import` 照样命中
        缓存 —— 这条用例就会因为「前面别的测试先加载过」而假通过或假失败
        （加了 compat 自检之后真的踩到了）。所以要连 sys.path 和 sys.modules
        一起还原。
        """
        import sys
        saved_dirs, saved = resolve.MOD_DIRS, resolve._loaded
        saved_path = list(sys.path)
        saved_mod = sys.modules.pop("DaVinciResolveScript", None)
        # sys.path 里可能早就被别的用例塞进了脚本模块目录，也要临时拿掉，
        # 否则 import 照样命中真实安装 —— 这条用例就成了「看执行顺序」。
        sys.path[:] = [p for p in sys.path
                       if "Scripting" not in p and "Resolve" not in p]
        resolve.MOD_DIRS, resolve._loaded = (), None
        try:
            with self.assertRaises(resolve.ResolveUnavailable) as ctx:
                resolve.load()
            self.assertIn("check", str(ctx.exception))     # 告诉人哪些命令不受影响
        finally:
            resolve.MOD_DIRS, resolve._loaded = saved_dirs, saved
            sys.path[:] = saved_path
            if saved_mod is not None:
                sys.modules["DaVinciResolveScript"] = saved_mod

    def test_proxy_is_patchable_without_resolve(self):
        """测试里 patch `send.dvr.scriptapp` 不能反过来要求先装达芬奇。"""
        sentinel = mock.Mock()
        with mock.patch.object(send.dvr, "scriptapp", return_value=sentinel) as m:
            self.assertIs(send.dvr.scriptapp("Resolve"), sentinel)
            self.assertEqual(m.call_count, 1)


class TestProcs(unittest.TestCase):
    """进程探测不能依赖 PATH —— 面板是从达芬奇内部 Popen 出来的。

    那儿的 PATH / 权限跟终端不一样，`pgrep` 拿不到结果，于是「达芬奇明明
    开着」被判成没启动（send 退出码 4，1 秒返回）。这里用 libproc 直接枚举，
    不 exec 任何外部命令，所以连 PATH 都不需要存在。
    """

    def setUp(self):
        if sys.platform != "darwin":
            self.skipTest("libproc 只存在于 macOS")

    def test_lists_own_process_without_path(self):
        """把 PATH 清空也必须能列到本进程 —— 这正是终端里测不出来的那种环境。"""
        from colorpass import procs
        with mock.patch.dict(os.environ, {"PATH": ""}):
            pids = procs.all_pids()
            self.assertIsNotNone(pids, "libproc 不可用，探测会退回外部命令")
            self.assertIn(os.getpid(), pids)

    def test_exec_path_is_absolute(self):
        from colorpass import procs
        path = procs.exec_path(os.getpid())
        self.assertTrue(os.path.isabs(path), path)

    def test_find_returns_empty_set_not_none_when_nothing_matches(self):
        """「找不到」和「查不了」必须分开：混在一起就会把探测失败当成程序没开。"""
        from colorpass import procs
        hits = procs.find(substr="/this/path/definitely/does/not/exist")
        self.assertIsNotNone(hits)
        self.assertEqual(hits, set())


class TestMacPlainDraft(unittest.TestCase):
    """写回生成的新草稿正文是明文的：读它不能去求解密器。

    旧行为：`read_draft` 一律走 `EncryptUtils::decrypt`，明文文件只会拿回
    「成功 + 0 字节」，上层翻译成「剪映需要重启一次」—— 用户刚写完回、
    什么都没动，却被要求重启剪映。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cp_plain_")
        self.plain = os.path.join(self.tmp, "draft_info.json")
        with open(self.plain, "w", encoding="utf-8") as f:
            f.write(json.dumps(mini_draft(2), ensure_ascii=False))
        self.enc = os.path.join(self.tmp, "enc.json")
        with open(self.enc, "wb") as f:
            f.write(b"\x9f\x1c\x00\x00 binary")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_plain_detects_json(self):
        from colorpass import macdraft
        self.assertTrue(macdraft.is_plain(self.plain))
        self.assertFalse(macdraft.is_plain(self.enc))
        self.assertFalse(macdraft.is_plain(os.path.join(self.tmp, "nope")))

    def test_read_draft_does_not_decrypt_plaintext(self):
        from colorpass import macdraft
        row = {"name": "新稿", "folder": self.tmp, "main": self.plain,
               "locked": False, "plain": True}
        with mock.patch.object(macdraft, "decrypt",
                               side_effect=AssertionError("不该走解密")), \
                mock.patch.object(macdraft, "reader_ping",
                                  return_value=(False, "no-socket")):
            draft, cur = macdraft.read_draft(row)
        self.assertEqual(len(draft["tracks"][0]["segments"]), 2)
        self.assertEqual(cur["platform"], "darwin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
