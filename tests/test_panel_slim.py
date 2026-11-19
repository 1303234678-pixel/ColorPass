"""面板回归测试：不靠达芬奇，用假 Fusion/UI 真跑一遍 Panel。

为什么要这份测试
----------------
面板是整个插件唯一「用户直接摸得到」的一层，但它以前一条测试都没有 ——
因为 `JYBridge.py` 末尾会直接 `main()`，一 import 就弹窗。这里把源码读进来、
截掉末尾自启动，再用假的 `fusion` / `bmd` / `subprocess` 把 UI 与子进程顶掉，
于是构造、按钮回调、状态刷新都能在 CI 里跑。

覆盖三条实测过的坑：
  1. `GetItems()` 返回的不是 dict，没有 `.get` —— 旧版 `_new_name()` 用它，
     于是「新草稿名」输入框永远静默失效。
  2. `cli status` 曾经在主循环里同步跑，剪映读内存卡住就整窗卡死 ——
     现在必须走后台线程，主循环只贴结果。
  3. 面板不得再依赖 `drx/PrimaryBalance.py`（一级调色已移除）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PANEL = Path(__file__).resolve().parent.parent / "jybridge" / "panel" / "JYBridge.py"


# ---------------------------------------------------------------- 假 UI
class _Elem(object):
    def __init__(self, kind, props):
        self.kind = kind
        if isinstance(props, dict):
            for k, v in props.items():
                setattr(self, k, v)


class _Items(object):
    """故意不实现 .get() —— 模拟 Fusion 的 items 对象。"""

    def __init__(self, items):
        self._d = dict(items)

    def __getitem__(self, k):
        return self._d[k]

    def __setitem__(self, k, v):
        self._d[k] = v

    def __contains__(self, k):
        return k in self._d

    def keys(self):
        return self._d.keys()


class _Bag(object):
    """支持 `bag.btn.Clicked = fn` 又能 `bag.btn.Clicked(ev)`。"""

    def __init__(self):
        object.__setattr__(self, "_d", {})

    def __getattr__(self, k):
        d = object.__getattribute__(self, "_d")
        if k not in d:
            d[k] = _Bag()
        return d[k]

    def __setattr__(self, k, v):
        object.__getattribute__(self, "_d")[k] = v


class _FakeUI(object):
    def __init__(self):
        self.items = {}

    def _mk(self, kind):
        def f(props=None, children=None):
            e = _Elem(kind, props)
            if isinstance(props, dict) and props.get("ID"):
                self.items[props["ID"]] = e
            return e
        return f

    def __getattr__(self, name):
        if name in ("HGroup", "VGroup", "Label", "Button", "LineEdit", "VGap"):
            return self._mk(name)
        raise AttributeError(name)

    def FindWindow(self, wid):
        return None


class _Win(object):
    def __init__(self, items):
        self.On = _Bag()
        self._items = _Items(items)
        self.shown = False

    def GetItems(self):
        return self._items

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False


class _Disp(object):
    def __init__(self, ui):
        self.ui = ui
        self.win = None

    def AddWindow(self, definition, layout):
        self.win = _Win(self.ui.items)
        self.win.definition = definition
        return self.win

    def StepLoop(self):
        return True


class _Immediate(object):
    """把 Thread 变成同步执行，测试里就不用等线程。"""

    def __init__(self, target=None, daemon=None, **kw):
        self._t = target

    def start(self):
        self._t()


class _Bmd(object):
    """假的 bmd 模块：UIDispatcher 是普通方法，不会被绑成 bound method。"""

    def __init__(self, disp):
        self._disp = disp

    def UIDispatcher(self, ui):
        return self._disp


class _Proc(object):
    def __init__(self, code=0):
        self.returncode = code
        self.pid = 4242

    def poll(self):
        return self.returncode


class _RunResult(object):
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class _Sub(object):
    DEVNULL = None

    def __init__(self, payload):
        self.payload = payload
        self.spawned = []

    def run(self, *a, **kw):
        if callable(self.payload):
            return self.payload(*a, **kw)
        return _RunResult(json.dumps(self.payload, ensure_ascii=False))

    def Popen(self, *a, **kw):
        self.spawned.append((a, kw))
        return _Proc(0)


# ---------------------------------------------------------------- 载入面板
def load_panel(path=None, payload=None):
    """截掉末尾自启动后 exec，返回模块命名空间。

    path 必须在调用时解析（不能用默认参数）—— 默认参数在 def 时求值，
    测试里替换模块级 PANEL 就不生效了。
    """
    path = str(path or PANEL)
    src = Path(path).read_text(encoding="utf-8")
    cut = src.find("\ntry:\n    main()")
    if cut > 0:
        src = src[:cut]
    tmp = tempfile.mkdtemp(prefix="jyb_panel_")
    old = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = tmp
    try:
        ns = {"__name__": "jyb_panel_test", "__file__": str(path)}
        exec(compile(src, str(path), "exec"), ns)
    finally:
        if old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old
    ui = _FakeUI()
    ns["fusion"] = type("F", (), {"UIManager": ui})()
    ns["bmd"] = _Bmd(_Disp(ui))
    ns["subprocess"] = _Sub(payload if payload is not None else
                            {"draft": "测试草稿", "jianying": {"processes": [1]},
                             "opened": True})
    ns["threading"] = type("T", (), {"Thread": _Immediate, "Lock": __import__("threading").Lock})()
    return ns


def make_panel(path=None, payload=None):
    ns = load_panel(path, payload)
    Panel = ns["Panel"]
    Panel.loop = lambda self: None          # 不进主循环
    return ns, Panel()


STATUS_OK = {"draft": "测试草稿", "jianying": {"processes": [1]}, "opened": True,
             "last_send": None}


class PanelSlimTest(unittest.TestCase):
    # ---- 装在哪、用哪个解释器（开源后不能再写死绝对路径） ----
    def test_product_resolves_from_env(self):
        root = str(Path(__file__).resolve().parents[1])
        with mock.patch.dict(os.environ, {"JYBRIDGE_HOME": root}):
            ns = load_panel()
        self.assertEqual(ns["PRODUCT"], root)
        self.assertTrue(os.path.isfile(os.path.join(root, "jybridge", "cli.py")))

    def test_missing_product_tells_how_to_fix(self):
        with mock.patch.dict(os.environ, {"JYBRIDGE_HOME": "",
                                          "JYBRIDGE_PYTHON": ""}), \
                mock.patch("os.getcwd", return_value="C:/definitely/not/here"):
            ns = load_panel()
        self.assertEqual(ns["PRODUCT"], "")
        self.assertIn("python -m jybridge.install", ns["_NO_PRODUCT"])

    def test_python_always_resolves_to_something(self):
        ns = load_panel()
        self.assertTrue(ns["PY"])

    def test_job_refuses_without_product(self):
        """找不到源码目录时，子进程一个都不许起。"""
        with mock.patch.dict(os.environ, {"JYBRIDGE_HOME": ""}), \
                mock.patch("os.getcwd", return_value="C:/definitely/not/here"):
            ns, p = make_panel(payload=STATUS_OK)
            p.start_job("send", "送入")
        self.assertIsNone(p.job)

    def test_no_drx_dependency(self):
        ns = load_panel()
        self.assertNotIn("load_pb", ns)
        self.assertNotIn("PB_SRC", ns)
        self.assertNotIn("DRX", ns)

    def test_buttons_wired(self):
        ns, p = make_panel(payload=STATUS_OK)
        on = p.win.On
        for b in ("btnSend", "btnWrite", "btnLock", "btnRefresh", "btnGc"):
            self.assertTrue(callable(getattr(getattr(on, b), "Clicked", None)),
                            "%s 没有接上回调" % b)

    # ---- 缓存显示与回收 ----
    def test_cache_label_shows_only_deletable(self):
        """行上报的必须是「点下去真会释放」的量，不是目录总占用。

        旧版显示整个缓存目录大小（这里是 253 MB），而其中大头是写回要用的
        底稿快照和当前草稿的 cube —— 按设计删不得。用户点清理 0 项、数字
        不变，就会认定按钮坏了。
        """
        payload = dict(STATUS_OK, cache_bytes=265329868,
                       cache_free_bytes=12900000, cache_extra_bytes=0)
        ns, p = make_panel(payload=payload)
        p.apply_status()
        line = p.items["jybCache"].Text
        self.assertIn("12.3 MB", line)
        self.assertNotIn("253.0 MB", line, "总占用不能出现，那是幻觉")
        self.assertTrue(p.items["btnGc"].Enabled)

    def test_cache_label_separates_regenerable(self):
        payload = dict(STATUS_OK, cache_bytes=265329868,
                       cache_free_bytes=12900000, cache_extra_bytes=44000000)
        ns, p = make_panel(payload=payload)
        p.apply_status()
        line = p.items["jybCache"].Text
        self.assertIn("12.3 MB", line)
        self.assertIn("可再生", line)
        self.assertIn("42.0 MB", line)

    def test_cache_label_without_value(self):
        ns, p = make_panel(payload=dict(STATUS_OK, cache_bytes=None,
                                        cache_free_bytes=None,
                                        cache_extra_bytes=None))
        p.apply_status()
        self.assertEqual(p.items["jybCache"].Text, "可清理：—")

    # ---- 草稿被改过：送入前弹提示 ----
    def test_send_blocked_when_draft_edited(self):
        """打开之后被改过的草稿，点送入当场弹提示，不启动子进程。

        判据是磁盘上的两个时间（draft_settings / .locked），不用扫内存，
        所以这里连 job 都不该起 —— 起了就是让用户白等几十秒。
        """
        payload = dict(STATUS_OK, draft_edited=True,
                       draft_edit_text="11-19 23:47:41",
                       draft_opened_text="11-19 23:38:25")
        ns, p = make_panel(payload=payload)
        p.apply_status()
        self.assertIn("已改", p.items["jybDraft"].Text)
        self.assertIn("重开", p.items["jybInfo"].Text)
        got = []
        p.start_job = lambda action, label, extra=None: got.append(action)
        p.win.On.btnSend.Clicked(None)
        self.assertEqual(got, [], "被改过的草稿不该直接送入")
        self.assertIsNotNone(p._dlg, "要弹窗说明，不能静默")
        body = p._dlg.GetItems()["dMsg"].Text
        self.assertIn("11-19 23:47:41", body)
        self.assertIn("重新打开", body)
        self.assertIn("Ctrl+S", body)
        # 弹窗上的动作＝用户重开之后再送一次
        p._run_act()
        self.assertEqual(got, ["send"])

    def test_send_proceeds_when_draft_clean(self):
        payload = dict(STATUS_OK, draft_edited=False)
        ns, p = make_panel(payload=payload)
        p.apply_status()
        self.assertNotIn("已改", p.items["jybDraft"].Text)
        got = []
        p.start_job = lambda action, label, extra=None: got.append(action)
        p.win.On.btnSend.Clicked(None)
        self.assertEqual(got, ["send"])
        self.assertIsNone(p._dlg)

    def test_gc_disabled_when_nothing_deletable(self):
        payload = dict(STATUS_OK, cache_bytes=85917562,
                       cache_free_bytes=0, cache_extra_bytes=0)
        ns, p = make_panel(payload=payload)
        p.apply_status()
        self.assertEqual(p.items["jybCache"].Text, "可清理：无")
        self.assertFalse(p.items["btnGc"].Enabled,
                         "写着 0 还让点，就是又一次「点了没反应」")

    def test_gc_button_cleans_with_apply(self):
        payload = dict(STATUS_OK, cache_bytes=100, cache_free_bytes=2048,
                       cache_extra_bytes=0)
        ns, p = make_panel(payload=payload)
        p.apply_status()
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p.win.On.btnGc.Clicked(None)
        self.assertEqual(got, [("gc", ["--apply"])])

    def test_gc_button_goes_straight_to_intermediate(self):
        """只剩可再生中间产物时一步到位，别再弹一层窗问一遍。"""
        payload = dict(STATUS_OK, cache_bytes=85917562,
                       cache_free_bytes=0, cache_extra_bytes=44000000)
        ns, p = make_panel(payload=payload)
        p.apply_status()
        self.assertIn("可再生", p.items["jybCache"].Text)
        self.assertTrue(p.items["btnGc"].Enabled)
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p.win.On.btnGc.Clicked(None)
        self.assertEqual(got, [("gc", ["--apply", "--intermediate"])])

    def test_human_bytes(self):
        hb = load_panel()["human_bytes"]
        self.assertEqual(hb(0), "0 B")
        self.assertEqual(hb(2048), "2.0 KB")
        self.assertEqual(hb(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(hb(None), "—")

    # ---- 草稿显示 / 刷新 / 禁用 ----
    def test_draft_label_shows_current_draft(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertEqual(p.items["jybDraft"].Text, "当前草稿：测试草稿")

    def test_send_and_write_disabled_without_draft(self):
        ns, p = make_panel(payload={"draft": None, "jianying": {"processes": [1]},
                                    "opened": False})
        p.apply_status()
        self.assertFalse(p.items["btnSend"].Enabled)
        self.assertFalse(p.items["btnWrite"].Enabled)
        self.assertTrue(p.items["btnRefresh"].Enabled)
        self.assertEqual(p.items["jybDraft"].Text, "当前草稿：—")

    def test_send_and_write_enabled_with_draft(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertTrue(p.items["btnSend"].Enabled)
        self.assertTrue(p.items["btnWrite"].Enabled)

    def test_disabled_before_first_scan(self):
        """构造完、还没拿到状态之前不能让按钮亮着。"""
        ns, p = make_panel(payload=STATUS_OK)
        p.items["btnSend"].Enabled = True          # 故意打乱
        p._has_draft = False
        p._sync_buttons()
        self.assertFalse(p.items["btnSend"].Enabled)

    def test_refresh_rescans(self):
        sub = _Sub(STATUS_OK)
        calls = []
        orig = sub.run

        def counted(*a, **kw):
            calls.append(1)
            return orig(*a, **kw)

        sub.run = counted
        ns = load_panel(payload=STATUS_OK)
        ns["subprocess"] = sub
        Panel = ns["Panel"]
        Panel.loop = lambda self: None
        p = Panel()                      # 构造时扫一次
        self.assertEqual(len(calls), 1)
        p.win.On.btnRefresh.Clicked(None)
        self.assertEqual(len(calls), 2, "点刷新必须真的重新扫一次")

    def test_buttons_disabled_while_job_running(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        p.job = {"proc": _Proc(0), "out": "", "label": "x", "action": "send",
                 "file": None, "t0": 0, "next_lock": None}
        p._sync_buttons()
        self.assertFalse(p.items["btnSend"].Enabled)
        self.assertFalse(p.items["btnRefresh"].Enabled)
        p.job = None
        p._sync_buttons()
        self.assertTrue(p.items["btnSend"].Enabled)

    def test_refresh_mid_scan_keeps_single_flight(self):
        ns, p = make_panel(payload=STATUS_OK)
        p._st_running = True
        p.refresh_draft()                      # 不许起第二个
        self.assertTrue(p._st_running)

    def test_name_field_reads_items_without_get(self):
        """Fusion 的 items 没有 .get，旧写法读不到名字。"""
        ns, p = make_panel(payload=STATUS_OK)
        self.assertEqual(p._new_name(), "")
        self.assertEqual(p._write_extra(), [])
        p.items["txtName"].Text = "  成片A  "
        self.assertEqual(p._new_name(), "成片A")
        self.assertEqual(p._write_extra(), ["--name", "成片A"])

    def test_send_uses_bake(self):
        ns, p = make_panel(payload=STATUS_OK)
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p.win.On.btnSend.Clicked(None)
        self.assertEqual(got, [("send", ["--speed", "bake"])])

    def test_lock_toggles(self):
        ns, p = make_panel(payload=STATUS_OK)
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p.win.On.btnLock.Clicked(None)
        self.assertEqual(got[-1], ("lock", ["on"]))
        p._lock_state = True
        p.win.On.btnLock.Clicked(None)
        self.assertEqual(got[-1], ("lock", ["off"]))

    def test_write_passes_name(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.items["txtName"].Text = "我的片子"
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p.win.On.btnWrite.Clicked(None)
        self.assertEqual(got, [("write", ["--name", "我的片子"])])

    def test_apply_status_ready(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertIn("就绪", p.items["jybInfo"].Text)
        self.assertIn("测试草稿", p.items["jybDraft"].Text)

    def test_apply_status_not_running(self):
        ns, p = make_panel(payload={"draft": None, "jianying": {"processes": []},
                                    "opened": False})
        p.apply_status()
        self.assertIn("剪映没在运行", p.items["jybInfo"].Text)

    def test_apply_status_timeout_is_not_fatal(self):
        def boom(*a, **kw):
            raise TimeoutError("timed out")
        ns, p = make_panel(payload=boom)
        p.apply_status()                      # 不许抛
        self.assertIn("读取草稿状态失败", p.items["jybInfo"].Text)

    def test_apply_status_without_result_is_noop(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()                      # 消费掉启动时那次
        before = p.items["jybInfo"].Text
        p.apply_status()                      # 没有新结果
        self.assertEqual(before, p.items["jybInfo"].Text)

    # ---- 结果呈现：成功一行、失败弹窗 ----
    def test_parse_takes_last_json(self):
        txt = "\n".join(["闸门通过：剪映 11.7 / 达芬奇 20.3",
                         "· 不送：文本 1",
                         '{"ok": true, "placed": 3}',
                         "!! 内存里的草稿明文与磁盘对不上",
                         '{"ok": false, "gate": "stale_draft",',
                         ' "error": "差 1742 字节\\n处理：关掉再打开"}'])
        rep = load_panel()["Panel"]._parse(txt)
        self.assertEqual(rep["gate"], "stale_draft")
        self.assertIn("差 1742 字节", rep["error"])

    def test_summary_send(self):
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "timeline": "JY · 11月18日",
                     "speed_mode": "bake"})
        self.assertIn("3 段", s)
        self.assertIn("JY · 11月18日", s)

    def test_summary_write(self):
        s = load_panel()["Panel"]._summary(
            "write", {"draft_name": "11月18日_调色", "luts": 3})
        self.assertIn("11月18日_调色", s)
        self.assertIn("3 段", s)

    def test_summary_send_mentions_disabled(self):
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "timeline": "JY · 草稿",
                     "speed_mode": "bake", "disabled": 1})
        self.assertIn("停用段 1 段", s)

    def test_summary_send_silent_when_no_disabled(self):
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "timeline": "JY · 草稿"})
        self.assertNotIn("停用", s)

    def test_summary_write_lists_what_survived(self):
        """「回剪映后还能编辑」是这套方案唯一别人做不到的点，得写在脸上。"""
        s = load_panel()["Panel"]._summary("write", {
            "draft_name": "X", "luts": 3,
            "kept": {"speed_segments": 2, "transitions": 5, "masks": 0,
                     "overlay_tracks": 1, "effects": 0}})
        self.assertIn("原样保留", s)
        self.assertIn("变速 2 段", s)
        self.assertIn("转场 5 个", s)
        self.assertIn("画中画 1 轨", s)
        self.assertNotIn("蒙版", s)          # 0 项不显示，别占地
        self.assertNotIn("特效", s)

    def test_fail_text_stale_keeps_specific_time(self):
        """后端报的具体时间要留（说不出所以然的提示没法让人信服），
        同时固定三步处理必须在 —— 只说不能送，用户还是不知道干什么。"""
        t = load_panel()["Panel"]._fail_text(
            "send", {"gate": "stale_draft",
                     "error": "最后编辑 11-19 23:47:41，打开时刻 11-19 23:38:25"},
            "", 4)
        self.assertIn("11-19 23:47:41", t)
        self.assertIn("重新打开", t)
        self.assertIn("Ctrl+S", t)

    def test_fail_text_no_plaintext_says_reopen(self):
        """明文被剪映回收了 → 讲机制 + 三步处理，别让人以为是自己改了草稿。

        旧文案是「内存明文与磁盘密文对不上」，用户明明什么都没改，看到这句
        只能怀疑自己或者怀疑工具坏了。
        """
        t = load_panel()["Panel"]._fail_text(
            "send", {"gate": "no_plaintext",
                     "error": "剪映内存里已经没有「123_调色」的完整明文了。"},
            "", 3)
        self.assertIn("完整明文", t)
        self.assertIn("关掉", t)
        self.assertIn("重新打开", t)
        self.assertIn("送入达芬奇", t)
        self.assertNotIn("对不上", t)

    def test_fail_text_stale_without_error_still_explains(self):
        t = load_panel()["Panel"]._fail_text(
            "send", {"gate": "stale_draft"}, "", 4)
        self.assertIn("打开之后被改过", t)
        self.assertIn("重新打开", t)

    def test_fail_text_uses_error(self):
        t = load_panel()["Panel"]._fail_text(
            "write", {"ok": False, "error": "缺少 plan.json"}, "", 1)
        self.assertEqual(t, "缺少 plan.json")

    def _job(self, ns, action, code, stdout):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(stdout)
        return {"proc": _Proc(code), "out": path, "label": "测试任务",
                "action": action, "file": None, "t0": 0, "extra": [], "next_lock": None}

    def test_job_success_shows_one_line_and_no_popup(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "send", 0,
                          '{"ok": true, "placed": 3, "exact": 3, "timeline": "T"}')
        p.poll_job()
        self.assertIsNone(p._dlg, "成功后不该弹窗")
        self.assertIn("送入完成", p.items["jybResult"].Text)
        # 旧版会在这里塞一大段 JSON —— 现在面板上不该再有那种东西
        self.assertNotIn("timeline_end", p.items["jybResult"].Text)

    def test_job_success_keeps_unsent_note(self):
        """「不送：蒙版 2、文本 1」这类行必须留在面板上。

        它们告诉用户达芬奇里比剪映少了什么 —— 不能为了界面干净一起删掉。
        """
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "send", 0, "\n".join([
            '{"ok": true, "placed": 13}',
            "蒙版：2 段不送（线性@7.40s）",
            "不送：蒙版 2、文本/字幕 1",
            "画面变换：1 段在剪映里缩放过",
            "已存快照：13 段",
        ]))
        p.poll_job()
        info = p.items["jybResult"].Text
        self.assertIn("送入完成", info)
        self.assertIn("不送：蒙版 2", info)
        self.assertNotIn("已存快照", info, "内部进度行不该进面板")
        self.assertLessEqual(len(info.splitlines()), 3, "最多两行附注，别糊成一片")

    def test_job_failure_pops_dialog(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "send", 4,
                          '{"ok": false, "gate": "stale_draft", "error": "对不上"}')
        p.poll_job()
        self.assertIsNotNone(p._dlg, "失败必须弹窗")
        body = p._dlg.GetItems()["dMsg"].Text
        self.assertIn("重新打开", body)
        self.assertIn("没有完成", p.items["jybResult"].Text)

    def test_popup_retry_reruns_same_command(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "write", 1, '{"ok": false, "error": "boom"}')
        p.poll_job()
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p._run_act()
        self.assertEqual(got, [("write", [])])
        self.assertIsNone(p._dlg, "点重试后弹窗要关掉")

    def test_popup_replaces_previous(self):
        ns, p = make_panel(payload=STATUS_OK)
        p._popup("送入失败", "第一次")
        first = p._dlg
        p._popup("送入失败", "第二次")
        self.assertIsNot(p._dlg, first)
        self.assertFalse(first.shown, "旧弹窗要收掉，不能叠着")

    # ---- 结果行不能被状态刷新冲掉 ----
    def test_result_survives_status_refresh(self):
        """点完按钮的结果必须留着。

        旧版把结果写进 jybInfo，而 poll_job 结束会立刻 refresh_draft()，
        状态一回来就把那段整段改写 —— 用户看到的是「点了没反应」。
        """
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "gc", 0, '{"ok": true, "count": 2, "freed_bytes": 4096}')
        p.poll_job()
        shown = p.items["jybResult"].Text
        self.assertIn("释放 4.0 KB", shown)
        p.apply_status()                      # 状态刷新回来
        self.assertEqual(p.items["jybResult"].Text, shown, "结果行不该被状态覆盖")
        self.assertIn("测试草稿", p.items["jybInfo"].Text)

    def test_gc_empty_explains_and_offers_deep_clean(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "gc", 0, json.dumps({
            "ok": True, "count": 0, "applied": True,
            "before_bytes": 85917562, "freed_bytes": 0,
            "intermediate_bytes": 44000000, "intermediate_count": 2,
            "note": "没有可自动回收的缓存：81.9 MB 都属于当前草稿或最近 5 份仍在剪映里的草稿",
        }, ensure_ascii=False))
        p.poll_job()
        self.assertIn("没有可回收的缓存", p.items["jybResult"].Text)
        self.assertIsNotNone(p._dlg, "没东西可清时也要说清楚，不能静默")
        body = p._dlg.GetItems()["dMsg"].Text
        self.assertIn("可再生", body)
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p._run_act()
        self.assertEqual(got, [("gc", ["--apply", "--intermediate"])])

    # ---- 状态同步的两个边角 ----
    def test_lock_state_not_flipped_on_failure(self):
        """锁定失败不能把本地镜像翻成「已锁定」。

        旧版在算 ok 之前无条件翻：失败一次之后，下一次按「已锁定」发 on，
        用户得点两下才解锁，面板显示的也是反的。
        """
        ns, p = make_panel(payload=STATUS_OK)
        self.assertFalse(p._lock_state)
        job = self._job(ns, "lock", 1, '{"ok": false, "gate": "no_timeline"}')
        job["next_lock"] = True
        p.job = job
        p.poll_job()
        self.assertFalse(p._lock_state)

    def test_lock_state_flipped_on_success(self):
        ns, p = make_panel(payload=STATUS_OK)
        job = self._job(ns, "lock", 0, '{"ok": true, "state": "on", "locked": 3}')
        job["next_lock"] = True
        p.job = job
        p.poll_job()
        self.assertTrue(p._lock_state)

    def test_refresh_requested_while_busy_is_queued(self):
        """查询在跑时来的刷新不能丢。

        任务结束时 poll_job 会 refresh_draft()；如果那一刻恰好有一次旧查询
        在跑，旧版直接 return —— 面板永远停在任务前的缓存数字和上次送入时间。
        """
        ns, p = make_panel(payload=STATUS_OK)
        with p._st:
            p._st_running = True
        p.refresh_draft()
        self.assertTrue(p._want_refresh, "被拒的刷新要记下来")
        # 旧查询贴完结果 → 自动补一次
        with p._st:
            p._st_running = False
            p._st_result = dict(STATUS_OK, draft="补刷的草稿")
        p.apply_status()
        self.assertFalse(p._want_refresh)
        self.assertIn("补刷的草稿", p.items["jybInfo"].Text)

    def test_gc_empty_without_intermediate_has_no_action(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "gc", 0, json.dumps(
            {"ok": True, "count": 0, "before_bytes": 1024, "freed_bytes": 0},
            ensure_ascii=False))
        p.poll_job()
        self.assertFalse(p._dlg.GetItems()["dAct"].Enabled,
                         "没有可清的中间产物时不该给一个空按钮")


if __name__ == "__main__":
    unittest.main()
