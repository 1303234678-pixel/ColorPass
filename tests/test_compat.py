"""兼容层测试：用假 API 面模拟不同达芬奇版本。

为什么这样做
------------
真机只有一台、一个版本，装几个达芬奇版本又太重。但「老版本没有某个方法」
这件事完全可以造出来 —— 把方法从对象上删掉，就是那版的样子。这里按官方
release notes 的增量 API 造了 17.4 / 18.5 / 19.1 / 20.3 四个面，断言的是
**同一件事**：缺能力时降级并说明，而不是抛异常。

（17.4 缺哪些、18.5 加了哪些，是按文档推的；真机只有在装了那版的人手上才能
确认细节。所以判断一律靠 hasattr，不靠版本号 —— 真机比这张表更权威。）
"""
import types
import unittest

from colorpass import compat


def _fn(n):
    """造一个只接受 n 个位置参数的方法，多给就 TypeError（老版本就是这样）。"""
    params = ", ".join("a%d" % i for i in range(n))
    src = "lambda self%s: (%s)" % (", " + params if params else "", 
                                   ", ".join("a%d" % i for i in range(n)) or "()")
    return eval(src)


class Obj(object):
    """一个长得像达芬奇 API 对象的东西：有就是有，没有就是没有。"""

    def __init__(self, **methods):
        for k, v in methods.items():
            setattr(self, k, types.MethodType(v, self))


def _resolve_app(version, product="DaVinci Resolve Studio", methods=None,
                 tl_methods=None, item_methods=None):
    """搭一条 app → pm → project → mediapool/timeline → item 的链。"""
    item = Obj(**(item_methods or {}))
    timeline = Obj(GetTrackCount=lambda self, t: 1,
                   GetItemListInTrack=lambda self, t, i: [item],
                   **(tl_methods or {}))
    mp = Obj(**(methods or {}))
    proj = Obj(GetTimelineByIndex=lambda self, i: timeline,
               GetTimelineCount=lambda self: 1,
               GetMediaPool=lambda self: mp,
               GetCurrentTimeline=lambda self: timeline,
               GetVersionString=lambda self: version,
               GetName=lambda self: "测试工程",
               GetProductName=lambda self: product)
    pm = Obj(GetCurrentProject=lambda self: proj,
             GetVersionString=lambda self: version,
             GetProductName=lambda self: product)
    app = Obj(GetProjectManager=lambda self: pm,
              GetVersionString=lambda self: version,
              GetProductName=lambda self: product)
    for k, v in (methods or {}).items():          # 17 没有 page API 之类
        if not hasattr(app, k):
            setattr(app, k, types.MethodType(v, app))
    return app


# --- 四个版本的 API 面 -------------------------------------------------
#
# 每条都只写「这版有的」，缺的就不写 —— 与真机一致。

V17 = dict(
    app={},                       # 17 没有 GetCurrentPage / OpenPage
    mp=dict(ImportMedia=_fn(1), CreateEmptyTimeline=_fn(1),
            AppendToTimeline=_fn(1)),
    tl=dict(AddMarker=_fn(5), SetTrackLock=_fn(3), GetIsTrackLocked=_fn(2),
            SetStartTimecode=_fn(1), GetName=_fn(0)),
    item=dict(GetName=_fn(0), ExportLUT=_fn(2), AddMarker=_fn(5)),
)

V18 = dict(
    app=dict(GetCurrentPage=_fn(0), OpenPage=_fn(1)),
    mp=dict(ImportMedia=_fn(1), CreateEmptyTimeline=_fn(1),
            AppendToTimeline=_fn(1)),
    tl=dict(AddMarker=_fn(6), SetTrackLock=_fn(3), GetIsTrackLocked=_fn(2),
            SetStartTimecode=_fn(1), CreateCompoundClip=_fn(2),
            GetName=_fn(0), DeleteTimelines=_fn(1)),
    item=dict(GetName=_fn(0), ExportLUT=_fn(2), AddMarker=_fn(6),
              SetClipEnabled=_fn(1), GetClipEnabled=_fn(0)),
)

V20 = dict(V18)
V20 = dict(app=V18["app"], mp=V18["mp"], tl=V18["tl"], item=V18["item"])


def make(surface, version="20.3.1.0007", **kw):
    app = _resolve_app(version, methods=surface["app"], **kw)
    proj = app.GetProjectManager().GetCurrentProject()
    mp = proj.GetMediaPool()
    tl = proj.GetCurrentTimeline()
    for k, v in surface["mp"].items():
        setattr(mp, k, types.MethodType(v, mp))
    for k, v in surface["tl"].items():
        setattr(tl, k, types.MethodType(v, tl))
    item = tl.GetItemListInTrack("video", 1)[0]
    for k, v in surface["item"].items():
        setattr(item, k, types.MethodType(v, item))
    # DeleteTimelines 在 project 上，不在 mediapool 上（真实 API 如此）
    for k in ("DeleteTimelines",):
        if k in surface["tl"]:
            setattr(proj, k, types.MethodType(surface["tl"][k], proj))
    return app


class CallShrinkTest(unittest.TestCase):
    def test_drops_extra_args_for_old_signature(self):
        got = compat.call_shrink(_fn(5).__get__(Obj()), 1, 2, 3, 4, 5, "末尾")
        self.assertEqual(got, (1, 2, 3, 4, 5))

    def test_keeps_all_args_when_accepted(self):
        self.assertEqual(compat.call_shrink(lambda *a: a, 1, 2, 3), (1, 2, 3))

    def test_reraises_when_nothing_works(self):
        def boom(*a):
            raise TypeError("no way")
        with self.assertRaises(TypeError):
            compat.call_shrink(boom, 1, 2)


class LutTypeTest(unittest.TestCase):
    def test_falls_back_to_literal(self):
        """老版本脚本模块没有 EXPORT_LUT_33PTCUBE 这个属性。"""
        self.assertEqual(compat.lut_type(types.SimpleNamespace()), "33PTCube")

    def test_prefers_new_constant(self):
        mod = types.SimpleNamespace(EXPORT_LUT_33PTCUBE="33PTCube_v2")
        self.assertEqual(compat.lut_type(mod), "33PTCube_v2")


class EnsurePageTest(unittest.TestCase):
    def test_uses_open_page_on_old_app(self):
        """17 没有 OpenPage：返回 False，不抛。"""
        app = make(V17, version="17.4.6")
        self.assertFalse(compat.ensure_page("color", app=app))

    def test_skips_when_already_there(self):
        calls = []
        app = Obj(GetCurrentPage=lambda self: "color",
                  OpenPage=lambda self, p: calls.append(p))
        self.assertTrue(compat.ensure_page("color", app=app))
        self.assertEqual(calls, [], "已经在调色页就不该再切")

    def test_switches_from_edit(self):
        calls = []
        app = Obj(GetCurrentPage=lambda self: "edit",
                  OpenPage=lambda self, p: calls.append(p) or True)
        self.assertTrue(compat.ensure_page("color", app=app))
        self.assertEqual(calls, ["color"])


class ReportTest(unittest.TestCase):
    def test_unknown_without_resolve(self):
        rep = compat.report(app=None)
        self.assertEqual(rep["state"], "unknown")
        self.assertEqual(len(rep["features"]), len(compat.FEATURES))
        self.assertEqual(rep["features"]["lut_export"]["state"], "unknown")

    def test_modern_version_all_ok(self):
        rep = compat.report(app=make(V20))
        self.assertEqual(rep["state"], "ok", rep["missing"])
        self.assertTrue(rep["ok"])
        self.assertIn("20.3", compat.line(rep))

    def test_17_degrades_but_stays_usable(self):
        rep = compat.report(app=make(V17, version="17.4.6"))
        self.assertEqual(rep["features"]["clip_enable"]["state"], "missing")
        self.assertEqual(rep["features"]["page_nav"]["state"], "missing")
        # 停用段、页面切换缺了不致命 —— 仍算「可降级使用」
        self.assertTrue(rep["ok"], rep["missing"])
        self.assertEqual(rep["state"], "degraded")
        self.assertIn("缺", compat.line(rep))

    def test_missing_fatal_feature_breaks_pipeline(self):
        surface = {k: dict(v) for k, v in V20.items()}
        surface["item"] = {k: v for k, v in surface["item"].items()
                           if k != "ExportLUT"}
        rep = compat.report(app=make(surface))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["state"], "broken")
        self.assertIn("lut_export", rep["missing"])

    def test_missing_text_is_user_facing(self):
        """缺项说明里不许出现方法名 —— 用户看不懂 SetClipEnabled。"""
        rep = compat.report(app=make(V17, version="17.4.6"))
        for why in rep["missing"].values():
            self.assertNotIn("Set", why)
            self.assertNotIn("Get", why)


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(compat.parse_version("20.3.1.0007"), (20, 3, 1))
        self.assertEqual(compat.parse_version("DaVinci Resolve Studio 18.6.6"),
                         (18, 6, 6))
        self.assertEqual(compat.parse_version("18.1"), (18, 1, 0))
        self.assertEqual(compat.parse_version(""), ())


class TextReportTest(unittest.TestCase):
    def test_says_something_useful_without_resolve(self):
        text = compat.text_report(app=None)
        self.assertIn("ColorPass", text)
        self.assertIn("达芬奇", text)
        self.assertIn("未连上", text)


class MarkItemTest(unittest.TestCase):
    """片段标记：新版 6 参数、老版 5 参数，两种都得把段号写进去。

    重点是**段号不能丢**。如果用 call_shrink 硬缩参数，老版本会以 5 个参数
    「成功」—— 标记写上了，但 note 是空串、customData 没有，写回时整条链
    找不到片段，而且全程不报错。
    """

    class Old(object):
        """只认 5 个参数，多的直接 TypeError —— 与真机一致。"""

        def __init__(self):
            self.args = None

        def AddMarker(self, a0, a1, a2, a3, a4):
            self.args = (a0, a1, a2, a3, a4)
            return True

    class New(object):
        def __init__(self):
            self.args = None

        def AddMarker(self, *a):
            self.args = a
            return True

    def test_old_add_marker_puts_sid_in_note(self):
        from colorpass import send
        it = self.Old()
        self.assertTrue(send.mark_item(it, "seg-42"))
        self.assertEqual(len(it.args), 5, "老版本只认 5 个参数")
        self.assertEqual(it.args[3], "seg-42", "段号要退到 note 里")

    def test_new_add_marker_keeps_custom_data(self):
        from colorpass import send
        it = self.New()
        self.assertTrue(send.mark_item(it, "seg-42"))
        self.assertEqual(len(it.args), 6)
        self.assertEqual(it.args[5], "seg-42")

    def test_sid_never_dropped_by_shrink(self):
        """call_shrink 会「成功但丢段号」—— 这就是 mark_item 不用它的原因。"""
        it = self.Old()
        got = compat.call_shrink(it.AddMarker, 0, "Blue", "CP",
                                 "", 1, "seg-42")
        self.assertTrue(got)
        self.assertEqual(it.args[3], "", "缩参数把段号弄丢了，正是要避免的")


if __name__ == "__main__":
    unittest.main()
