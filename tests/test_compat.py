"""兼容层测试：只面对达芬奇 21.2 一版。

为什么还要造假 API 面
----------------------
21.2 自己就有两个坑，真机不一定会碰到，但代码必须扛住：

  · `hasattr` 对不存在的方法返回 True（`getattr` 是 None）—— 假面里放一个
    「撒谎的空壳」，断言 `alive()` 判它不存在；
  · LUT 导出类型只能传整数枚举 —— 断言 `lut_type()` 的兜底值是 2。

「缺能力时降级并说明，而不是抛异常」这条仍然要测：把某个方法从假面上删掉，
就是「这台机器装坏了」的样子。
"""
import types
import unittest
from unittest import mock

from colorpass import compat


def _fn(n):
    """造一个接受 n 个位置参数的方法，多给就 TypeError。"""
    params = ", ".join("a%d" % i for i in range(n))
    src = "lambda self%s: (%s)" % (
        ", " + params if params else "",
        ", ".join("a%d" % i for i in range(n)) or "()")
    return eval(src)


class Obj(object):
    """一个长得像达芬奇 API 对象的东西：有就是有，没有就是没有。"""

    def __init__(self, **methods):
        for k, v in methods.items():
            setattr(self, k, types.MethodType(v, self))


# 21.2 的 API 面：链路用到的每一个方法都在。
def surface(**over):
    base = {
        "app": dict(GetCurrentPage=_fn(0), OpenPage=_fn(1)),
        "project": dict(GetTimelineByIndex=_fn(1), GetTimelineCount=_fn(0)),
        "mp": dict(ImportMedia=_fn(1), CreateEmptyTimeline=_fn(1),
                   AppendToTimeline=_fn(1), GetRootFolder=_fn(0),
                   DeleteTimelines=_fn(1), CreateMulticamClip=_fn(2)),
        "tl": dict(AddMarker=_fn(6), SetTrackLock=_fn(3),
                   GetIsTrackLocked=_fn(2), SetStartTimecode=_fn(1),
                   CreateCompoundClip=_fn(2), GetName=_fn(0),
                   AddTrack=_fn(1), GetMarkers=_fn(0)),
        "item": dict(GetName=_fn(0), ExportLUT=_fn(2), AddMarker=_fn(6),
                     SetClipEnabled=_fn(1), GetClipEnabled=_fn(0),
                     GetMarkers=_fn(0), GetMediaPoolItem=_fn(0),
                     GetType=_fn(0), SetSpeed=_fn(1), SetName=_fn(1)),
    }
    for k, extra in (over or {}).items():
        base[k] = dict(base[k], **extra)
    return base


def make(version="21.2.0.6", surface_=None, **kw):
    """搭一条 app → pm → project → mediapool/timeline → item 的链。"""
    sf = surface_ or surface()
    item = Obj(**sf["item"])
    timeline = Obj(GetTrackCount=lambda self, t: 1,
                   GetItemListInTrack=lambda self, t, i: [item], **sf["tl"])
    mp = Obj(**sf["mp"])
    proj_methods = dict(GetTimelineByIndex=lambda self, i: timeline,
                        GetTimelineCount=lambda self: 1,
                        GetMediaPool=lambda self: mp,
                        GetCurrentTimeline=lambda self: timeline,
                        GetName=lambda self: "测试工程")
    proj_methods.update(sf["project"])      # 假面可以覆盖 / 追加
    proj = Obj(**proj_methods)
    pm = Obj(GetCurrentProject=lambda self: proj,
             GetVersionString=lambda self: version,
             GetProductName=lambda self: "DaVinci Resolve Studio")
    app = Obj(GetProjectManager=lambda self: pm,
              GetVersionString=lambda self: version,
              GetProductName=lambda self: "DaVinci Resolve Studio",
              **sf["app"])
    return app


class AliveTest(unittest.TestCase):
    class _Liar(object):
        """21.2 的绑定：方法不存在也存在，取到 None。"""

        def DeleteTimelines(self):          # noqa: D401 - 只是占位
            return None

    def test_rejects_none_from_binding(self):
        class Lying(object):
            pass

        lie = Lying()
        lie.DeleteTimelines = None          # hasattr 为真，取出来是 None
        self.assertTrue(hasattr(lie, "DeleteTimelines"))
        self.assertFalse(compat.alive(lie, "DeleteTimelines"))

    def test_accepts_real_method(self):
        self.assertTrue(compat.alive(Obj(DeleteTimelines=_fn(1)),
                                     "DeleteTimelines"))

    def test_none_object_is_not_alive(self):
        self.assertFalse(compat.alive(None, "Anything"))


class ReportTest(unittest.TestCase):
    def test_ok_on_212(self):
        rep = compat.report(make())
        self.assertEqual(rep["state"], "ok", rep["missing"])
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["version_ok"])

    def test_old_version_is_flagged(self):
        rep = compat.report(make(version="20.3.1.0007"))
        self.assertIs(rep["version_ok"], False)
        self.assertIn("21.2", rep["version_note"])
        # 版本不对是版本问题，不是「这台机器接口缺失」。
        self.assertEqual(rep["state"], "ok")

    def test_missing_fatal_feature_breaks_pipeline(self):
        sf = surface()
        del sf["mp"]["AppendToTimeline"]
        rep = compat.report(make(surface_=sf))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["state"], "broken")
        self.assertIn("append", rep["missing"])

    def test_missing_optional_feature_degrades(self):
        sf = surface()
        del sf["mp"]["CreateMulticamClip"]
        rep = compat.report(make(surface_=sf))
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["state"], "degraded")
        self.assertIn("multicam_create", rep["missing"])

    def test_unknown_without_resolve(self):
        with mock.patch.object(compat.resolve, "scriptapp",
                               side_effect=RuntimeError("no resolve")):
            rep = compat.report(app=None)
        self.assertEqual(rep["state"], "unknown")
        self.assertTrue(all(f["state"] == "unknown"
                            for f in rep["features"].values()))

    def test_missing_text_is_user_facing(self):
        sf = surface()
        del sf["item"]["ExportLUT"]
        rep = compat.report(make(surface_=sf))
        why = rep["missing"]["lut_export"]
        self.assertIn("LUT", why)
        self.assertNotIn("ExportLUT", why)     # 不给用户看方法名


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(compat.parse_version("21.2.0.6"), (21, 2, 0))
        self.assertEqual(compat.parse_version("DaVinci Resolve Studio 21.2.0.6"),
                         (21, 2, 0))
        self.assertEqual(compat.parse_version(""), ())

    def test_requires_21_2(self):
        ok, why = compat.version_ok(make(version="21.2.0.6"))
        self.assertTrue(ok)
        self.assertIn("21.2", why)
        ok, why = compat.version_ok(make(version="21.1.0.4"))
        self.assertFalse(ok)
        self.assertIn("21.2", why)


class LutTypeTest(unittest.TestCase):
    def test_defaults_to_33_point_enum(self):
        # 21.2 的绑定取不到 EXPORT_LUT_33PTCUBE；字符串会静默返回 False。
        self.assertEqual(compat.lut_type(app=Obj()), 2)

    def test_prefers_constant_when_present(self):
        app = Obj()
        app.EXPORT_LUT_33PTCUBE = 2
        self.assertEqual(compat.lut_type(app=app), 2)


class EnsurePageTest(unittest.TestCase):
    def test_skips_when_already_there(self):
        app = Obj(GetCurrentPage=lambda self: "color",
                  OpenPage=lambda self, p: True)
        self.assertTrue(compat.ensure_page("color", app=app))

    def test_switches_from_edit(self):
        calls = []

        class App(object):
            page = "edit"

            def GetCurrentPage(self):
                return self.page

            def OpenPage(self, p):
                calls.append(p)
                self.page = p
                return True

        self.assertTrue(compat.ensure_page("color", app=App(), wait=1.0))
        self.assertEqual(calls, ["color"])

    def test_returns_false_when_page_never_ready(self):
        app = Obj(GetCurrentPage=lambda self: "edit",
                  OpenPage=lambda self, p: True)
        self.assertFalse(compat.ensure_page("color", app=app, wait=0.05))


class _Clip(object):
    def __init__(self, path):
        self.path = path

    def GetClipProperty(self, key):
        return self.path if key == "File Path" else ""


class _Folder(object):
    def __init__(self, pool):
        self.pool = pool

    def GetClipList(self):
        return list(self.pool.clips)


class _Pool(object):
    """能模拟「哪种入参形态真的会导入」的媒体池。

    21.2.0.6 的实测行为：`[{"FilePath": p}]` 返回 None、什么都不导；
    裸路径列表才真的导入。`accept` 用来说明这台假机器认哪几种形态。
    """

    def __init__(self, accept=("paths",), clips=(), raise_on=()):
        self.accept = tuple(accept)
        self.clips = [_Clip(p) for p in clips]
        self.seen = []
        self.raise_on = tuple(raise_on)

    def GetRootFolder(self):
        return _Folder(self)

    def GetCurrentFolder(self):
        return _Folder(self)

    def ImportMedia(self, arg):
        self.seen.append(arg)
        if isinstance(arg, list) and arg and isinstance(arg[0], dict):
            form, paths = "clipinfo", [d.get("FilePath") for d in arg]
        elif isinstance(arg, list):
            form, paths = "paths", list(arg)
        else:
            form, paths = "one", [arg]
        if form in self.raise_on:
            raise RuntimeError("boom")
        if form not in self.accept:
            return None                     # 静默空操作：不抛、不导、返回 None
        new = [_Clip(p) for p in paths if p]
        self.clips += new
        return new


class ImportMediaTest(unittest.TestCase):
    def test_clipinfo_silently_does_nothing_so_fall_back_to_paths(self):
        """21.2.0.6 的真实现象：clipinfo 不报错、不导入。

        只看「没抛异常」就会以为成功，接着每一段都摆不上、报成「素材不在
        媒体池」—— 用户会去查素材路径，其实路径没问题。
        """
        mp = _Pool(accept=("paths",))
        got = compat.import_media(mp, ["/a.mov", "/b.mov"])
        self.assertEqual(len(got), 2)
        self.assertEqual(got.mode, "paths")
        self.assertEqual(len(mp.clips), 2)
        # 两种形态都试过，报告里要能看出是哪一种生效的
        self.assertEqual(mp.seen[0], [{"FilePath": "/a.mov"},
                                      {"FilePath": "/b.mov"}])
        self.assertEqual(mp.seen[1], ["/a.mov", "/b.mov"])
        self.assertEqual(len(got.tried), 2)

    def test_clipinfo_wins_when_it_actually_works(self):
        mp = _Pool(accept=("clipinfo", "paths"))
        got = compat.import_media(mp, ["/a.mov"])
        self.assertEqual(got.mode, "clipinfo")
        self.assertEqual(len(got), 1)

    def test_skips_files_already_in_pool(self):
        mp = _Pool(clips=["/a.mov"])
        got = compat.import_media(mp, ["/a.mov"])
        self.assertEqual(got.mode, "already")
        self.assertEqual(mp.seen, [])

    def test_returns_nothing_when_every_form_fails(self):
        mp = _Pool(accept=())
        got = compat.import_media(mp, ["/a.mov"])
        self.assertEqual(got, [])
        self.assertEqual(got.mode, "")
        self.assertTrue(got.tried)          # 试过什么要留痕

    def test_empty_list_returns_empty(self):
        self.assertEqual(compat.import_media(Obj(), []), [])

    def test_swallows_error(self):
        mp = _Pool(accept=("paths",), raise_on=("clipinfo", "paths", "one"))
        self.assertEqual(compat.import_media(mp, ["/a.mov"]), [])


class RealItemsTest(unittest.TestCase):
    """AppendToTimeline 摆不上时返回 `[None]`，不是空列表。"""

    def test_drops_none(self):
        self.assertEqual(compat.real_items([None]), [])
        self.assertEqual(compat.real_items([None, "a", None, "b"]), ["a", "b"])

    def test_empty_and_none_are_empty(self):
        self.assertEqual(compat.real_items([]), [])
        self.assertEqual(compat.real_items(None), [])


class ProjectIdentityTest(unittest.TestCase):
    def test_reads_unique_id(self):
        self.assertEqual(compat.project_id(Obj(GetUniqueId=lambda s: "u1")), "u1")

    def test_missing_api_is_empty_not_error(self):
        self.assertEqual(compat.project_id(Obj()), "")
        self.assertEqual(compat.project_id(None), "")


class RenderingTest(unittest.TestCase):
    def test_true_while_rendering(self):
        self.assertTrue(compat.rendering(Obj(IsRenderingInProgress=lambda s: True)))

    def test_missing_api_does_not_block(self):
        """读不到状态就当没在渲染 —— 不能因为读不到就拒绝干活。"""
        self.assertFalse(compat.rendering(Obj()))
        self.assertFalse(compat.rendering(None))


class DeleteTimelinesTest(unittest.TestCase):
    def test_uses_mediapool(self):
        seen = []

        class MP(object):
            def DeleteTimelines(self, tls):
                seen.extend(tls)
                return True

        self.assertTrue(compat.delete_timelines(MP(), ["tl1"]))
        self.assertEqual(seen, ["tl1"])

    def test_missing_api_returns_false(self):
        self.assertFalse(compat.delete_timelines(Obj(), ["tl1"]))

    def test_empty_list_is_noop_success(self):
        self.assertTrue(compat.delete_timelines(Obj(), []))

    def test_lying_stub_counts_as_absent(self):
        class MP(object):
            DeleteTimelines = None

        self.assertFalse(compat.delete_timelines(MP(), ["tl1"]))


class MulticamDetectTest(unittest.TestCase):
    class _Clip(object):
        def __init__(self, ctype="", name="", native=""):
            self._ctype, self._name, self._native = ctype, name, native

        def GetMediaPoolItem(self):
            outer = self

            class MI(object):
                def GetClipProperty(self, key):
                    return outer._ctype if key == "Type" else ""

            return MI()

        def GetName(self):
            return self._name

        def GetType(self):
            return self._native

    def test_media_pool_type(self):
        ok, why = compat.is_multicam(self._Clip(ctype="Multicam"))
        self.assertTrue(ok)
        self.assertIn("Multicam", why)

    def test_localized_media_pool_type(self):
        ok, why = compat.is_multicam(self._Clip(ctype="多机位"))
        self.assertTrue(ok)
        self.assertIn("多机位", why)

    def test_angle_name(self):
        ok, _why = compat.is_multicam(self._Clip(name="A001 - Angle 2"))
        self.assertTrue(ok)

    def test_native_type(self):
        ok, _why = compat.is_multicam(self._Clip(native="multicam"))
        self.assertTrue(ok)

    def test_normal_clip(self):
        ok, why = compat.is_multicam(self._Clip(ctype="Video", name="A001"))
        self.assertFalse(ok)
        self.assertEqual(why, "")

    def test_kind_never_raises(self):
        kind = compat.item_kind(object())
        self.assertFalse(kind["multicam"])
        self.assertIn("media_type", kind)


class TextReportTest(unittest.TestCase):
    def test_says_what_is_supported(self):
        text = compat.text_report(make())
        self.assertIn("21.2", text)
        self.assertIn("接口齐全", text)

    def test_works_without_resolve(self):
        with mock.patch.object(compat.resolve, "scriptapp",
                               side_effect=RuntimeError("no resolve")):
            text = compat.text_report(app=None)
        self.assertIn("ColorPass", text)
        self.assertIn("21.2", text)


if __name__ == "__main__":
    unittest.main()
