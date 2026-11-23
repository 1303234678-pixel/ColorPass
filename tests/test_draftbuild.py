"""draftbuild 的落盘路径 —— 之前只有 5% 被自动化覆盖，全靠手工跑真工程。

这里不碰剪映、不碰达芬奇，只用临时目录和模板，覆盖三件要紧的事：

  1. 新草稿的文件是否齐全（缺 meta 的那条在剪映列表里是点不开的）
  2. 中途失败之后，半成品目录有没有被清掉
  3. 同名草稿绝不被覆盖

运行：
    <Python312> -m unittest tests.test_draftbuild -v
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colorpass import draftbuild as JD

TEMPLATES = Path(__file__).resolve().parents[1] / "colorpass" / "templates"


def mini_draft():
    """一份最小的明文草稿：一条视频轨、一段视频、没有 LUT。"""
    return {
        "id": "OLD-ID",
        "name": "原草稿",
        "duration": 1_000_000,
        "fps": 30.0,
        "canvas_config": {"width": 1920, "height": 1080, "ratio": "16:9"},
        "materials": {
            "videos": [{"id": "V1", "path": "C:/media/a.mp4", "type": "video"}],
            "effects": [],
            "speeds": [],
        },
        "tracks": [{
            "id": "T1", "type": "video", "segments": [{
                "id": "S1", "material_id": "V1", "extra_material_refs": [],
                "target_timerange": {"start": 0, "duration": 1_000_000},
                "source_timerange": {"start": 0, "duration": 1_000_000},
            }],
        }],
    }


class TestInstallFromTemplate(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cp_db_root_"))
        self.tpl = Path(tempfile.mkdtemp(prefix="cp_db_tpl_"))
        (self.tpl / "draft_content.json").write_text(
            json.dumps({"id": "T", "tracks": []}), encoding="utf-8")
        (self.tpl / "draft_meta_info.json").write_text(
            json.dumps({"draft_name": "模板"}), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.tpl, ignore_errors=True)

    def test_all_files_are_written(self):
        r = JD.install_from_template(self.tpl, "新草稿", root=self.root)
        self.assertTrue(r["ok"], r.get("error"))
        names = set(r["files"])
        self.assertLessEqual(
            {"draft_content.json", "draft_meta_info.json",
             "draft_biz_config.json", "draft_settings"}, names)
        meta = json.loads((self.root / "新草稿" / "draft_meta_info.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(meta["draft_name"], "新草稿")
        self.assertEqual(meta["draft_fold_path"],
                         str(self.root / "新草稿").replace("\\", "/"))

    def test_failure_leaves_no_half_draft(self):
        """写到一半炸了，目录必须被清掉 —— 否则剪映列表里多一条点不开的草稿。"""
        def boom(_draft):
            raise RuntimeError("模拟磁盘满")

        with self.assertRaises(RuntimeError):
            JD.install_from_template(self.tpl, "坏的", mutate=boom, root=self.root)
        self.assertFalse((self.root / "坏的").exists(),
                         "失败后残留了半成品目录")

    def test_existing_name_is_never_overwritten(self):
        keep = self.root / "同名"
        keep.mkdir()
        (keep / "draft_content.json").write_text('{"id":"KEEP"}', encoding="utf-8")

        r = JD.install_from_template(self.tpl, "同名", root=self.root)
        self.assertFalse(r["ok"])
        self.assertIn("同名草稿已存在", r["error"])
        # 原来那份一个字节都不能动
        self.assertEqual((keep / "draft_content.json").read_text(encoding="utf-8"),
                         '{"id":"KEEP"}')

    def test_encrypted_template_is_rejected(self):
        enc = Path(tempfile.mkdtemp(prefix="cp_db_enc_"))
        try:
            (enc / "draft_content.json").write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
            r = JD.install_from_template(enc, "x", root=self.root)
            self.assertFalse(r["ok"])
            self.assertIn("密文", r["error"])
            self.assertFalse((self.root / "x").exists())
        finally:
            shutil.rmtree(enc, ignore_errors=True)

    def test_missing_content_json_is_rejected(self):
        empty = Path(tempfile.mkdtemp(prefix="cp_db_empty_"))
        try:
            r = JD.install_from_template(empty, "y", root=self.root)
            self.assertFalse(r["ok"])
            self.assertIn("draft_content.json", r["error"])
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class TestBuildFromDraft(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cp_db_root_"))
        self.cube = self.root / "S1.cube"
        self.cube.write_text("LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n"
                             "0 0 1\n1 0 1\n0 1 1\n1 1 1\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _build(self, seg_luts=None, draft=None):
        return JD.build_from_draft(draft or mini_draft(), "调色稿",
                                   {}, root=self.root,
                                   template_dir=TEMPLATES,
                                   segment_luts=seg_luts if seg_luts is not None
                                   else {"S1": str(self.cube)})

    def test_timeline_is_copied_verbatim(self):
        src = mini_draft()
        r = self._build(draft=src)
        self.assertTrue(r["ok"], r.get("error"))
        new = json.loads((self.root / "调色稿" / "draft_content.json")
                         .read_text(encoding="utf-8"))
        self.assertNotEqual(new["id"], src["id"], "id 必须换新")
        self.assertEqual(new["name"], "调色稿")
        seg = new["tracks"][0]["segments"][0]
        self.assertEqual(seg["target_timerange"], src["tracks"][0]["segments"][0]["target_timerange"])
        self.assertEqual(seg["source_timerange"], src["tracks"][0]["segments"][0]["source_timerange"])
        self.assertEqual(seg["material_id"], "V1")
        self.assertEqual(new["duration"], src["duration"])
        self.assertEqual(new["canvas_config"]["width"], 1920)

    def test_lut_is_attached_to_the_segment(self):
        r = self._build()
        self.assertTrue(r["ok"], r.get("error"))
        new = json.loads((self.root / "调色稿" / "draft_content.json")
                         .read_text(encoding="utf-8"))
        eff = {e["id"]: e for e in new["materials"]["effects"]}
        seg = new["tracks"][0]["segments"][0]
        refs = [x for x in seg["extra_material_refs"] if x in eff]
        self.assertEqual(len(refs), 1, "这一段应该正好挂一条 LUT")
        self.assertEqual(Path(eff[refs[0]]["path"].split(";")[0]).name, "S1.cube")
        self.assertTrue(seg.get("enable_lut"))

    def test_missing_cube_reference_is_dropped(self):
        """cube 不在盘上时不能留引用 —— 剪映会显示「LUT 丢失」，画面回退成没调过。"""
        gone = str(self.root / "not-there.cube")
        r = self._build(seg_luts={"S1": gone})
        self.assertTrue(r["ok"], r.get("error"))
        new = json.loads((self.root / "调色稿" / "draft_content.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(new["materials"]["effects"], [])
        self.assertEqual(new["tracks"][0]["segments"][0]["extra_material_refs"], [])

    def test_no_lut_at_all_is_fine(self):
        r = self._build(seg_luts={})
        self.assertTrue(r["ok"], r.get("error"))
        new = json.loads((self.root / "调色稿" / "draft_content.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(new["materials"]["effects"], [])


class TestRootMetaBackup(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cp_db_meta_"))
        (self.root / "root_meta_info.json").write_text(
            json.dumps({"all_draft_store": []}), encoding="utf-8")
        for ts in ("20260101_000000", "20260102_000000", "20260103_000000"):
            (self.root / ("root_meta_info.json.pb_%s.bak" % ts)).write_text(
                "{}", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_only_one_backup_is_kept(self):
        draft = self.root / "某草稿"
        draft.mkdir()
        with mock.patch.object(JD.JY, "draft_root", return_value=self.root):
            r = JD.register_in_root_meta(str(draft), "某草稿")
        self.assertTrue(r.get("ok"), r.get("error"))
        baks = list(self.root.glob("root_meta_info.json.pb_*.bak"))
        self.assertEqual(len(baks), 1, "备份没有收敛，剪映根目录会被 .bak 塞满")
        data = json.loads((self.root / "root_meta_info.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["all_draft_store"]), 1)
        self.assertEqual(data["all_draft_store"][0]["draft_name"], "某草稿")

    def test_concurrent_registration_keeps_every_entry(self):
        """两个窗口同时登记：不能丢条目，也不能把索引写坏。

        以前用固定的 `root_meta_info.json.tmp`，并发时一方 os.replace 之后
        另一方直接 ENOENT 抛出去；而且「读—改—写」没有锁，后写的盖掉先写的
        （实测 8 并发只剩 1~2 条）。
        """
        import threading
        drafts = []
        for i in range(6):
            d = self.root / ("并发_%d" % i)
            d.mkdir()
            drafts.append(d)
        errs = []

        def work(d):
            try:
                with mock.patch.object(JD.JY, "draft_root", return_value=self.root):
                    r = JD.register_in_root_meta(str(d), d.name)
                if not r.get("ok"):
                    errs.append(r.get("error"))
            except Exception as exc:            # noqa: BLE001 - 这里就是要抓全部
                errs.append("%s: %s" % (type(exc).__name__, exc))

        ts = [threading.Thread(target=work, args=(d,)) for d in drafts]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [], "并发登记抛异常了")
        data = json.loads((self.root / "root_meta_info.json").read_text(encoding="utf-8"))
        names = {e.get("draft_name") for e in data["all_draft_store"]}
        for d in drafts:
            self.assertIn(d.name, names, "并发登记丢了一条：%s" % d.name)
        self.assertEqual(list(self.root.glob("root_meta_info.json.*.tmp")), [],
                         "临时文件没清干净")


def mac_source(root, tid="TID-1", with_subdraft=True, with_meta=True):
    """一份最小的 macOS 草稿目录（明文正文 + Timelines/project.json）。"""
    src = root / "源草稿"
    (src / "Timelines" / tid).mkdir(parents=True)
    (src / "Timelines" / "project.json").write_text(json.dumps({
        "id": "P1", "main_timeline_id": tid,
        "timelines": [{"id": tid, "name": "时间线01"}],
        "create_time": 1, "update_time": 1, "version": 0, "config": {},
    }), encoding="utf-8")
    body = json.dumps(mini_draft(), ensure_ascii=False)
    for rel in ("draft_info.json", "template-2.tmp",
                "Timelines/%s/draft_info.json" % tid):
        (src / rel).write_text(body, encoding="utf-8")
    (src / ".locked").write_text("", encoding="utf-8")
    if with_meta:
        (src / "draft_meta_info.json").write_text(
            json.dumps({"draft_name": "源草稿", "draft_materials": []}),
            encoding="utf-8")
    if with_subdraft:
        sd = src / "subdraft" / "SUB-1"
        sd.mkdir(parents=True)
        (sd / "draft_content.json").write_text(json.dumps({"tracks": []}),
                                              encoding="utf-8")
    return src


class TestMacInstallFromSource(unittest.TestCase):
    """macOS 写回建稿：整目录克隆 + 明文正文 + 必须登记成功。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cp_db_mac_"))
        self.drafts = self.root / "drafts"
        self.drafts.mkdir()
        (self.drafts / "root_meta_info.json").write_text(
            json.dumps({"all_draft_store": [
                {"draft_name": "别人的稿", "draft_fold_path": "/x/别人的稿",
                 "draft_materials": []}]}), encoding="utf-8")
        self.src = mac_source(self.root)
        self._ov = JD.platforms.OVERRIDE
        JD.platforms.OVERRIDE = "darwin"

    def tearDown(self):
        JD.platforms.OVERRIDE = self._ov
        shutil.rmtree(self.root, ignore_errors=True)

    def _install(self, name="新稿"):
        with mock.patch.object(JD.JY, "draft_root", return_value=self.drafts), \
                mock.patch.object(JD, "_mac_meta", return_value={"draft_id": "X"}):
            return JD.install_mac_from_source(self.src, mini_draft(), name)

    def test_body_is_written_to_all_copies_and_registered(self):
        r = self._install()
        self.assertTrue(r.get("ok"), r.get("error"))
        d = Path(r["folder"])
        for rel in ("draft_info.json", "template-2.tmp",
                    "Timelines/TID-1/draft_info.json",
                    "Timelines/TID-1/template-2.tmp"):
            self.assertTrue((d / rel).is_file(), "少了 %s" % rel)
            self.assertEqual((d / rel).read_text(encoding="utf-8")[:1], "{",
                             "%s 不是明文" % rel)
        self.assertFalse((d / ".locked").exists(), "把剪映的占用标记复制过来了")
        self.assertTrue((d / "subdraft" / "SUB-1" / "draft_content.json").is_file(),
                        "复合片段的子草稿没跟过来")
        idx = json.loads((self.drafts / "root_meta_info.json").read_text(encoding="utf-8"))
        names = [e.get("draft_name") for e in idx["all_draft_store"]]
        self.assertIn("新稿", names)
        self.assertIn("别人的稿", names, "登记时把别人的条目弄丢了")

    def test_register_failure_is_not_silent(self):
        """索引不在/读不了时，不能返回 ok —— 否则用户看到「写回完成」
        却在剪映列表里找不到草稿。"""
        (self.drafts / "root_meta_info.json").unlink()
        r = self._install("无索引")
        self.assertFalse(r.get("ok"))
        self.assertIn("登记", r.get("error") or "")
        self.assertTrue(Path(r["folder"]).is_dir(), "目录路径要报出来，用户才知道去哪找")

    def test_same_name_never_overwrites(self):
        (self.drafts / "重名").mkdir()
        r = self._install("重名")
        self.assertFalse(r.get("ok"))
        self.assertIn("同名草稿已存在", r.get("error") or "")

    def test_missing_source_says_so(self):
        gone = str(self.root / "已经删掉的草稿")
        r = JD.install_mac_from_source(gone, mini_draft(), "x", root=self.drafts)
        self.assertFalse(r.get("ok"))
        self.assertIn("不在原处", r.get("error") or "")

    def test_failure_leaves_no_half_built_draft(self):
        """中途失败（磁盘满/崩溃）不能在剪映草稿根里留半成品目录。

        半成品在剪映下次启动时会被列成一条点不开的草稿，用户不知道那是什么。
        所以先建隐藏暂存目录，全部写完再 rename。
        """
        def boom(_d):
            raise RuntimeError("模拟中途失败")
        with mock.patch.object(JD.JY, "draft_root", return_value=self.drafts), \
                mock.patch.object(JD, "_mac_meta", return_value={}):
            with self.assertRaises(RuntimeError):
                JD.install_mac_from_source(self.src, mini_draft(), "坏的",
                                           mutate=boom, root=self.drafts)
        self.assertFalse((self.drafts / "坏的").exists())
        self.assertEqual(list(self.drafts.glob(".cp-staging-*")), [],
                         "暂存目录没清掉")

    def test_competing_same_name_directory_is_never_deleted(self):
        """发布瞬间另一个窗口抢先完成同名写回，失败方不能删对方的目录。"""
        real_replace = JD.os.replace

        def competing_replace(src, dst):
            if Path(src).name.startswith(".cp-staging-"):
                target = Path(dst)
                target.mkdir()
                (target / "属于先完成的写回.txt").write_text("keep", encoding="utf-8")
                raise OSError("模拟同名目标已被另一个进程创建")
            return real_replace(src, dst)

        with mock.patch.object(JD.JY, "draft_root", return_value=self.drafts), \
                mock.patch.object(JD, "_mac_meta", return_value={}), \
                mock.patch.object(JD.os, "replace", side_effect=competing_replace):
            r = JD.install_mac_from_source(self.src, mini_draft(), "同名竞态",
                                           root=self.drafts)
        self.assertFalse(r.get("ok"))
        self.assertIn("同名草稿已存在", r.get("error") or "")
        marker = self.drafts / "同名竞态" / "属于先完成的写回.txt"
        self.assertTrue(marker.is_file(), "失败方把另一个窗口发布的草稿删了")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.drafts.glob(".cp-staging-*")), [])

    def test_success_leaves_no_staging_dir(self):
        r = self._install("干净的")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertEqual(list(self.drafts.glob(".cp-staging-*")), [])
        self.assertTrue((self.drafts / "干净的" / "draft_info.json").is_file())


if __name__ == "__main__":
    unittest.main()
