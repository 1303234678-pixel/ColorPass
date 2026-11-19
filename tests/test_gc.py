"""缓存回收（colorpass/gc.py）的行为测试。

全部在临时目录里跑，不碰真实的 %LOCALAPPDATA%\\ColorPass。
覆盖四道闸：活动保护 / 孤儿 / 超期超量 / 总量兜底，以及 dry-run 与真删的区别。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from colorpass import gc


class GcBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base = root / "ColorPass"
        self.base.mkdir()
        self.drafts = root / "drafts"
        self.drafts.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def mk(self, pool, name, size=1024, age_days=0.0):
        d = self.base / pool / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "f.bin"
        f.write_bytes(b"x" * size)
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(f, (t, t))
        return d

    def runs_json(self, draft_id, name):
        d = self.base / "runs" / draft_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "send.json").write_text(json.dumps({"draft": name}),
                                     encoding="utf-8")

    def current(self, draft_id):
        (self.base / "current.json").write_text(
            json.dumps({"draft_id": draft_id}), encoding="utf-8")

    def names(self, items):
        return {(i["pool"], i["name"]) for i in items}


class TestPlan(GcBase):
    def test_legacy_dirs_are_collected(self):
        self.mk("castfix", "demo", 2048)
        items = gc.plan(base=self.base, draft_root=self.drafts)
        self.assertEqual(self.names(items), {("legacy", "castfix")})

    def test_current_draft_is_protected_even_if_ancient(self):
        self.current("AAA")
        self.mk("luts", "AAA", age_days=999)
        self.mk("retime", "AAA", age_days=999)
        items = gc.plan(base=self.base, draft_root=self.drafts, keep_days=1)
        self.assertEqual(items, [])

    def test_pending_draft_is_protected(self):
        (self.base / "pending.json").write_text(
            json.dumps({"draft_id": "PPP"}), encoding="utf-8")
        self.mk("luts", "PPP", age_days=999)
        self.assertEqual(gc.plan(base=self.base, draft_root=self.drafts), [])

    def test_orphan_is_collected(self):
        # runs 里记的源草稿名在剪映里已经没有了
        self.runs_json("OLD", "已删掉的片子")
        self.mk("luts", "OLD")
        items = gc.plan(base=self.base, draft_root=self.drafts)
        reasons = {i["pool"]: i["reason"] for i in items}
        self.assertEqual(reasons.get("luts"), "剪映里已经没有这份草稿")

    def test_live_draft_is_not_orphan(self):
        self.runs_json("NEW", "还在的片子")
        (self.drafts / "还在的片子").mkdir()
        self.mk("luts", "NEW")
        items = gc.plan(base=self.base, draft_root=self.drafts, keep_last=5)
        self.assertEqual(items, [])

    def test_no_draft_name_means_keep(self):
        # 读不到草稿名时不能靠猜：宁可留着
        self.mk("luts", "UNKNOWN", age_days=1)
        self.assertEqual(gc.plan(base=self.base, draft_root=self.drafts,
                                 keep_days=30, keep_last=5), [])

    def test_missing_draft_root_disables_orphan_check(self):
        self.runs_json("OLD", "已删掉的片子")
        self.mk("luts", "OLD")
        items = gc.plan(base=self.base, draft_root=Path(self.tmp.name) / "nope")
        self.assertEqual(items, [])

    def test_expired_is_collected(self):
        self.mk("luts", "X", age_days=40)
        items = gc.plan(base=self.base, draft_root=self.drafts, keep_days=30)
        self.assertEqual(len(items), 1)
        self.assertIn("没动过", items[0]["reason"])

    def test_keep_last_keeps_newest(self):
        for i, days in enumerate([50, 40, 30, 20, 10, 1]):
            self.mk("luts", "D%d" % i, age_days=days)
        items = gc.plan(base=self.base, draft_root=self.drafts,
                        keep_days=365, keep_last=2)
        self.assertEqual(self.names(items),
                         {("luts", "D0"), ("luts", "D1"), ("luts", "D2"), ("luts", "D3")})

    def test_quota_collects_oldest_beyond_limit(self):
        # 每份 1 MB，6 份 = 6 MB，上限 3 MB
        for i in range(6):
            self.mk("luts", "Q%d" % i, size=1024 * 1024, age_days=10 - i)
        items = gc.plan(base=self.base, draft_root=self.drafts, keep_days=365,
                        keep_last=99, max_mb=3)
        self.assertEqual(len(items), 3)
        self.assertTrue(all("总量" in i["reason"] for i in items))
        # 收的是最老的三份
        self.assertEqual(self.names(items),
                         {("luts", "Q0"), ("luts", "Q1"), ("luts", "Q2")})

    def test_protected_counts_toward_quota_limit(self):
        # 活动草稿占 5 MB，上限 5 MB：保护它，其余照收
        self.current("BIG")
        self.mk("luts", "BIG", size=5 * 1024 * 1024, age_days=99)
        self.mk("luts", "OTHER", size=2 * 1024 * 1024, age_days=5)
        items = gc.plan(base=self.base, draft_root=self.drafts, keep_days=365,
                        keep_last=99, max_mb=5)
        self.assertEqual(self.names(items), {("luts", "OTHER")})


class TestRun(GcBase):
    def test_dry_run_touches_nothing(self):
        self.mk("castfix", "demo", 4096)
        rep = gc.run(apply=False, base=self.base, draft_root=self.drafts)
        self.assertFalse(rep["applied"])
        self.assertEqual(rep["freed_bytes"], 0)
        self.assertTrue((self.base / "castfix" / "demo").is_dir())
        self.assertGreater(rep["before_bytes"], 0)

    def test_apply_removes_and_reports(self):
        self.mk("castfix", "demo", 4096)
        self.mk("luts", "GONE", 2048)
        self.runs_json("GONE", "删掉的")
        rep = gc.run(apply=True, base=self.base, draft_root=self.drafts)
        self.assertTrue(rep["ok"])
        # runs 不在回收范围内（里面是写回唯一的底稿快照），所以是 2 项
        self.assertEqual(rep["count"], 2)
        self.assertFalse((self.base / "castfix" / "demo").exists())
        self.assertFalse((self.base / "luts" / "GONE").exists())
        self.assertTrue((self.base / "runs" / "GONE").exists(),
                        "写回底稿不能被缓存回收删掉")
        self.assertLess(rep["after_bytes"], rep["before_bytes"])
        self.assertGreaterEqual(rep["freed_bytes"], 6144)

    def test_partial_failure_is_not_total_failure(self):
        """删掉一部分 + 个别被占用，不能判成「整条命令失败」。

        否则面板会弹「清理失败 + 重试」，用户再点只会得到「没有可回收的」。
        """
        import shutil as _sh
        from unittest import mock
        self.mk("luts", "A", 2048, age_days=999)
        self.mk("luts", "B", 2048, age_days=999)
        real = _sh.rmtree
        calls = {"n": 0}

        def boom(p, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("正被占用")
            return real(p, *a, **k)

        with mock.patch.object(gc.shutil, "rmtree", side_effect=boom):
            rep = gc.run(apply=True, base=self.base, draft_root=self.drafts)
        self.assertTrue(rep["ok"], "删掉了一部分就是部分成功")
        self.assertTrue(rep["partial"])
        self.assertTrue(rep["freed_bytes"] > 0)
        self.assertIn("没删掉", rep["error"])

    def test_apply_keeps_protected_draft(self):
        self.current("KEEP")
        self.mk("luts", "KEEP", age_days=500)
        gc.run(apply=True, base=self.base, draft_root=self.drafts, keep_days=1)
        self.assertTrue((self.base / "luts" / "KEEP").is_dir())

    def test_logs_file_is_never_touched(self):
        (self.base / "colorpass.log").write_text("log", encoding="utf-8")
        gc.run(apply=True, base=self.base, draft_root=self.drafts)
        self.assertTrue((self.base / "colorpass.log").exists())


class TestScan(unittest.TestCase):
    def test_scan_reports_pools_and_protection(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t) / "ColorPass"
            (base / "luts" / "A").mkdir(parents=True)
            (base / "luts" / "A" / "f").write_bytes(b"x" * 100)
            (base / "current.json").write_text(json.dumps({"draft_id": "A"}),
                                               encoding="utf-8")
            rep = gc.scan(base=base, draft_root=Path(t) / "none")
            self.assertEqual(rep["protected"], ["A"])
            self.assertEqual(rep["pools"]["luts"]["count"], 1)
            self.assertTrue(rep["pools"]["luts"]["drafts"][0]["protected"])

    def test_human_units(self):
        self.assertEqual(gc.human(0), "0 B")
        self.assertEqual(gc.human(2048), "2.0 KB")
        self.assertEqual(gc.human(5 * 1024 * 1024), "5.0 MB")


class TestTidy(unittest.TestCase):
    """自动回收的触发点必须在命令行层。

    踩过的坑：一开始放在 `send.run` / `write.run` 末尾，结果跑一轮 unittest
    就把真实的 %LOCALAPPDATA%\\ColorPass 删了 252 MB —— 测试只隔离了 run 目录，
    gc.auto() 用的仍是默认的真实缓存根。
    """

    def test_skips_failed_report(self):
        from colorpass import cli, gc
        calls = []
        orig = gc.auto
        gc.auto = lambda *a, **kw: calls.append(1) or {"count": 0}
        try:
            cli._tidy({"ok": False})
        finally:
            gc.auto = orig
        self.assertEqual(calls, [], "失败的报告不该触发回收")

    def test_attaches_summary_when_ok(self):
        from colorpass import cli, gc
        orig = gc.auto
        gc.auto = lambda *a, **kw: {"count": 2, "freed_bytes": 4096,
                                    "before_bytes": 8192, "after_bytes": 4096}
        try:
            rep = cli._tidy({"ok": True})
        finally:
            gc.auto = orig
        self.assertEqual(rep["gc"], {"count": 2, "freed_bytes": 4096})

    def test_auto_failure_does_not_break_report(self):
        from colorpass import cli, gc
        orig = gc.auto

        def boom(*a, **kw):
            raise RuntimeError("disk gone")
        gc.auto = boom
        try:
            rep = cli._tidy({"ok": True})
        finally:
            gc.auto = orig
        self.assertTrue(rep["ok"])
        self.assertNotIn("gc", rep)

    def test_library_modules_do_not_gc(self):
        from pathlib import Path
        for name in ("write.py", "send.py"):
            src = (Path(gc.paths.PKG_DIR) / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "gc.auto(", src,
                "%s 里不能直接调 gc.auto —— 测试会连真实缓存一起删" % name)


class TestIntermediate(GcBase):
    """「清可再生中间产物」模式：retime/luts 全收，runs 一律不动。

    这条路径是给「默认策略没东西可清、但我想立刻腾空间」用的，所以边界必须
    钉死：底稿快照还在，那份草稿就还能写回；删了就只能重新送入一次。
    """

    def test_collects_retime_and_luts_regardless_of_age(self):
        self.mk("retime", "AAA", 4096)
        self.mk("luts", "AAA", 2048)
        items = gc.plan(base=self.base, draft_root=self.drafts,
                        intermediate=True)
        self.assertEqual(self.names(items),
                         {("retime", "AAA"), ("luts", "AAA")})

    def test_never_touches_runs(self):
        self.mk("runs", "AAA", 1024)
        self.runs_json("AAA", "旧草稿")
        items = gc.plan(base=self.base, draft_root=self.drafts,
                        intermediate=True)
        self.assertEqual(items, [])

    def test_active_draft_still_protected(self):
        self.current("AAA")
        self.mk("retime", "AAA", 4096)
        self.mk("retime", "BBB", 4096)
        items = gc.plan(base=self.base, draft_root=self.drafts,
                        intermediate=True)
        self.assertEqual(self.names(items), {("retime", "BBB")})

    def test_apply_frees_space(self):
        self.mk("retime", "AAA", 4096)
        rep = gc.run(apply=True, base=self.base, draft_root=self.drafts,
                     intermediate=True)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["count"], 1)
        self.assertFalse((self.base / "retime" / "AAA").exists())
        self.assertGreaterEqual(rep["freed_bytes"], 4096)

    def test_empty_run_explains_why(self):
        """没东西可清必须给出原因 —— 面板上要原样显示这句话。"""
        self.mk("luts", "AAA", 2048)          # 在保留期内，默认策略不动
        rep = gc.run(apply=True, base=self.base, draft_root=self.drafts)
        self.assertEqual(rep["count"], 0)
        self.assertIn("没有可自动回收", rep["note"])
        # 同时报出「可再生的那部分还有多少」，面板用它决定给不给按钮
        self.assertGreaterEqual(rep.get("intermediate_bytes") or 0, 2048)
        self.assertEqual(rep.get("intermediate_count"), 1)

    def test_auto_mode_does_not_report_intermediate(self):
        """intermediate 模式自己不该再去算「中间产物有多少」（会绕回来）。"""
        rep = gc.run(apply=True, base=self.base, draft_root=self.drafts,
                     intermediate=True)
        self.assertEqual(rep["count"], 0)
        self.assertNotIn("intermediate_bytes", rep)

    def test_run_reports_what_stays(self):
        """「释放」和「还剩多少在用」要分开报，否则用户以为按钮没生效。"""
        self.current("AAA")
        self.mk("luts", "AAA", 100000)
        self.mk("luts", "OLD", 2048, age_days=999)
        rep = gc.run(apply=True, base=self.base, draft_root=self.drafts)
        self.assertEqual(rep["count"], 1)
        self.assertEqual(rep["freed_bytes"], 2048)
        self.assertGreaterEqual(rep["kept_bytes"], 100000)


class TestReclaimable(GcBase):
    """面板「可清理：X」那一行的口径。

    这里的数字必须等于按钮点下去真会释放的量。历史 bug：面板报的是整个
    缓存目录占用（含 runs 底稿快照、当前草稿 cube），默认策略却一项都收不
    掉 —— 用户点完看到同样的数字，只能认为按钮坏了。
    """

    def test_numbers_match_plan(self):
        self.current("AAA")
        self.mk("luts", "AAA", 4096)              # 活动草稿，保护
        self.mk("luts", "OLD", 2048, age_days=999)
        self.mk("retime", "NEW", 8192)            # 近的，默认不收
        self.mk("runs", "NEW", 512)
        rc = gc.reclaimable(base=self.base, draft_root=self.drafts)
        items = gc.plan(base=self.base, draft_root=self.drafts)
        self.assertEqual(rc["free_bytes"], sum(i["bytes"] for i in items))
        self.assertEqual(rc["free_count"], len(items))
        self.assertEqual(rc["extra_bytes"], 8192)
        self.assertEqual(rc["extra_count"], 1)
        self.assertEqual(rc["total_bytes"], gc.total_bytes(self.base))

    def test_total_is_not_reported_as_freeable(self):
        """写回要用的快照让总占用很大，但可回收是 0 —— 不能混成一个数。"""
        self.current("AAA")
        self.mk("runs", "AAA", 100000)
        self.mk("luts", "AAA", 50000)
        rc = gc.reclaimable(base=self.base, draft_root=self.drafts)
        self.assertEqual(rc["free_bytes"], 0)
        self.assertEqual(rc["extra_bytes"], 0)
        self.assertGreater(rc["total_bytes"], 150000)

    def test_runs_never_recycled(self):
        """runs 是报告 + 底稿快照，删了那份草稿就写不回去。

        它既不算 free（默认策略会删的）也不算 extra（--intermediate 会删的）。
        以前它被放进 POOLS，超期/超量时连快照一起收掉，正好把「原草稿删了也能
        写回」这条能力静默取消。
        """
        self.mk("runs", "OLD", 4096, age_days=999)
        rc = gc.reclaimable(base=self.base, draft_root=self.drafts)
        self.assertEqual(rc["extra_bytes"], 0)
        self.assertEqual(rc["free_bytes"], 0)
        rep = gc.run(apply=True, base=self.base, draft_root=self.drafts)
        self.assertEqual(rep["count"], 0)
        self.assertTrue((self.base / "runs" / "OLD").is_dir())

    def test_dry_does_not_touch_disk(self):
        d = self.mk("luts", "OLD", 2048, age_days=999)
        gc.reclaimable(base=self.base, draft_root=self.drafts)
        self.assertTrue(d.is_dir())

    def test_missing_base_is_zero(self):
        rc = gc.reclaimable(base=self.base / "nope", draft_root=self.drafts)
        self.assertEqual((rc["free_bytes"], rc["extra_bytes"], rc["total_bytes"]),
                         (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
