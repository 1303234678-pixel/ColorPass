"""doctor 的离线测试。

这份报告是收兼容性数据的唯一入口（mac 用户、旧版达芬奇用户都靠它回帖），
所以要求很硬：**没有任何一样东西（达芬奇、剪映、ffmpeg）也要能出完整报告**，
不能因为某一项缺失就抛异常或者少一个字段。
"""
import io
import json
import os
import sys
import unittest
from unittest import mock

from colorpass import doctor, install, memscan, paths, resolve


class _Partial:
    """只有部分方法的对象 —— 模拟某个达芬奇版本少几个 API。"""

    def __init__(self, **methods):
        for name, value in methods.items():
            setattr(self, name, value)


class _Full:
    """任何方法都有，返回 None：模拟「API 齐全」。"""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class TestSurface(unittest.TestCase):
    def test_missing_methods_are_reported_by_name(self):
        obj = _Partial(GetName=lambda: "V1", GetTrackCount=lambda _t: 2)
        surf = resolve.surface(obj, "timeline")
        self.assertTrue(surf["GetName"])
        self.assertTrue(surf["GetTrackCount"])
        self.assertFalse(surf["AppendToTimeline"])
        self.assertNotIn("ExportLUT", surf)         # 不属于 timeline 组，不该出现

    def test_none_object_means_not_verified(self):
        """拿不到对象（比如没有时间线）是「没验到」，不是「全都不支持」。"""
        self.assertIsNone(resolve.surface(None, "timeline"))

    def test_groups_cover_the_apis_we_call(self):
        """表里漏掉一个，doctor 就会漏报那个版本的缺项。"""
        for group, names in resolve.API_GROUPS.items():
            self.assertTrue(names, group)
        self.assertIn("SetClipEnabled", resolve.API_GROUPS["item"])
        self.assertIn("ExportLUT", resolve.API_GROUPS["item"])
        self.assertIn("AppendToTimeline", resolve.API_GROUPS["timeline"])


class TestDoctorReport(unittest.TestCase):
    def setUp(self):
        # 剪映快照要枚举窗口，跟 doctor 的逻辑无关；只测报告结构就别去碰它
        patcher = mock.patch.object(doctor.probe, "snapshot", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, app=None):
        with mock.patch.object(doctor.resolve, "load", side_effect=resolve.ResolveUnavailable("x")), \
                mock.patch.object(doctor.resolve, "scriptapp", return_value=app):
            return doctor.run()

    def test_works_without_anything_installed(self):
        rep = self._run()
        for key in ("colorpass", "platform", "data", "resolve", "jianying",
                    "capabilities", "panel", "ok"):
            self.assertIn(key, rep)
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["resolve"]["error"])
        json.dumps(rep, ensure_ascii=False)          # 必须能直接序列化贴出去

    def test_full_api_reports_ok(self):
        tl = _Full(GetName=lambda: "Timeline 1", GetTrackCount=lambda _t: 2)
        proj = _Full(GetName=lambda: "未命名项目", GetCurrentTimeline=lambda: tl)
        app = _Full(GetVersionString=lambda: "20.0.1",
                    GetProductName=lambda: "DaVinci Resolve Studio",
                    GetCurrentPage=lambda: "edit",
                    GetProjectManager=lambda: _Full(GetCurrentProject=lambda: proj))
        with mock.patch.object(doctor.resolve, "load", return_value=object()), \
                mock.patch.object(doctor.resolve, "module_path", return_value="/x/DaVinciResolveScript.py"):
            rep = doctor.run(app=app)
        self.assertEqual(rep["resolve"]["version"], "20.0.1")
        self.assertEqual(rep["resolve"]["project"], "未命名项目")
        self.assertEqual(rep["resolve"]["timeline"]["video_tracks"], 2)
        self.assertEqual(rep["resolve"]["api"]["missing"], [])
        self.assertTrue(rep["ok"])

    def test_missing_api_blocks_ok_and_is_listed(self):
        """旧版本缺方法时要能一眼看出缺哪几个 —— 这就是兼容性数据本身。"""
        item = _Partial(GetName=lambda: "clip", ExportLUT=lambda *a: True)
        tl = _Full(GetName=lambda: "T", GetTrackCount=lambda _t: 1,
                   GetItemListInTrack=lambda *a: [item])
        proj = _Full(GetName=lambda: "P", GetCurrentTimeline=lambda: tl)
        app = _Full(GetVersionString=lambda: "17.4.6", GetProductName=lambda: "DaVinci Resolve",
                    GetProjectManager=lambda: _Full(GetCurrentProject=lambda: proj))
        with mock.patch.object(doctor.resolve, "load", return_value=object()):
            rep = doctor.run(app=app)
        self.assertFalse(rep["ok"])
        self.assertIn("item.SetClipEnabled", rep["resolve"]["api"]["missing"])
        self.assertTrue(rep["resolve"]["api"]["item"]["ExportLUT"])

    def test_compact_output_is_one_line_json(self):
        buf = io.StringIO()
        with mock.patch.object(doctor.resolve, "load", side_effect=resolve.ResolveUnavailable("x")), \
                mock.patch("sys.stdout", buf), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(doctor.main(["--compact"]), 0)
        text = buf.getvalue().strip()
        self.assertEqual(len(text.splitlines()), 1)
        self.assertTrue(json.loads(text)["colorpass"])


class TestPlatformGuards(unittest.TestCase):
    def test_memscan_reports_its_own_availability(self):
        self.assertEqual(memscan.AVAILABLE, sys.platform.startswith("win"))

    @unittest.skipIf(memscan.AVAILABLE, "只在非 Windows 上验降级路径")
    def test_scan_entry_points_are_safe_off_windows(self):
        """模块在 mac/Linux 上必须能 import 且不炸 —— 否则 check 都跑不了。"""
        self.assertIsInstance(memscan.pids(), list)
        self.assertIsInstance(memscan.regions(None), list)
        pid, draft = memscan.extract_open_draft(uuid_hint="UUID-1")
        self.assertIsNone(pid)
        self.assertIsNone(draft)

    def test_data_dir_can_be_overridden(self):
        with mock.patch.dict(os.environ, {"COLORPASS_DATA": "/tmp/colorpass-test"}):
            self.assertEqual(paths.data_root(), os.path.abspath("/tmp/colorpass-test"))

    def test_panel_targets_exist_for_this_platform(self):
        """Windows / macOS 有面板目录，其它平台明确为空（面板只在达芬奇桌面版用）。"""
        targets = install._targets()
        if paths.WINDOWS or paths.MACOS:
            self.assertTrue(targets, "当前平台应至少有一个面板安装位置")
        else:
            self.assertEqual(targets, [])
        for d in targets:
            self.assertTrue(os.path.isabs(d), d)


if __name__ == "__main__":
    unittest.main()
