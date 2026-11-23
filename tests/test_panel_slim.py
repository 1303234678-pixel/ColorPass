"""面板回归测试：不靠达芬奇，用假 Fusion/UI 真跑一遍 Panel。

为什么要这份测试
----------------
面板是整个插件唯一「用户直接摸得到」的一层，但它以前一条测试都没有 ——
因为 `ColorPass.py` 末尾会直接 `main()`，一 import 就弹窗。这里把源码读进来、
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
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PANEL = Path(__file__).resolve().parent.parent / "colorpass" / "panel" / "ColorPass.py"


# ---------------------------------------------------------------- 假 UI
class _Elem(object):
    def __init__(self, kind, props, children=None):
        self.kind = kind
        self.children = list(children or [])
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
            e = _Elem(kind, props, children)
            if isinstance(props, dict) and props.get("ID"):
                self.items[props["ID"]] = e
            return e
        return f

    def __getattr__(self, name):
        if name in ("HGroup", "VGroup", "Label", "Button", "LineEdit",
                    "VGap", "TextEdit"):
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


class _FlakyDisp(_Disp):
    """前 n 次 AddWindow 返回 None —— 模拟「这版 Fusion 不认 StyleSheet」。"""

    def __init__(self, ui, fail_times=1):
        _Disp.__init__(self, ui)
        self.fail_left = fail_times
        self.calls = []

    def AddWindow(self, definition, layout):
        self.calls.append(dict(definition))
        if self.fail_left > 0:
            self.fail_left -= 1
            return None
        return _Disp.AddWindow(self, definition, layout)


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
    # 面板的数据目录三个平台取值不同：Windows 看 LOCALAPPDATA，mac/Linux 看
    # COLORPASS_DATA（没有就落到 ~/Library 或 ~/.local）。两个都指到临时目录，
    # 才不会在 mac 上写进真实的家目录。
    saved = {k: os.environ.get(k) for k in ("LOCALAPPDATA", "COLORPASS_DATA")}
    os.environ["LOCALAPPDATA"] = tmp
    os.environ["COLORPASS_DATA"] = tmp
    try:
        # 面板是独立脚本（达芬奇 Fusion 环境），平台判断直接用 sys.platform，
        # 不经过 colorpass.platforms 的 OVERRIDE 开关。exec 时顶层已经按真实
        # sys 求值过 NO_WIN / BASE_DIR 等；exec 之后把 ns 里的 sys 换成只有
        # platform 属性的假对象，让按钮回调（_on_send 等运行时读 sys.platform）
        # 固定走 Windows 语义 —— 与 conftest 里「mac 上跑单测强制 Windows 语义」
        # 是同一件事，否则 mac 真机上会走到 mac 读取器分支去连本机 socket。
        ns = {"__name__": "jyb_panel_test", "__file__": str(path)}
        exec(compile(src, str(path), "exec"), ns)
        ns["sys"] = type("_FakeSys", (), {"platform": "win32"})()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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

# 送入过一次之后的 status：last_send 指的就是当前这份草稿。
SENT_OK = dict(STATUS_OK, last_send={
    "draft": "测试草稿", "at": "2026-11-19 23:47:41",
    "timeline": "CP · 测试草稿", "segments": 13, "committed": True})



class PanelSlimTest(unittest.TestCase):
    # ---- 装在哪、用哪个解释器（开源后不能再写死绝对路径） ----
    def test_product_resolves_from_env(self):
        root = str(Path(__file__).resolve().parents[1])
        with mock.patch.dict(os.environ, {"COLORPASS_HOME": root}):
            ns = load_panel()
        self.assertEqual(ns["PRODUCT"], root)
        self.assertTrue(os.path.isfile(os.path.join(root, "colorpass", "cli.py")))

    def test_missing_product_tells_how_to_fix(self):
        with mock.patch.dict(os.environ, {"COLORPASS_HOME": "",
                                          "COLORPASS_PYTHON": ""}), \
                mock.patch("os.getcwd", return_value="C:/definitely/not/here"):
            ns = load_panel()
        self.assertEqual(ns["PRODUCT"], "")
        self.assertIn("python -m colorpass.install", ns["_NO_PRODUCT"])

    def test_python_always_resolves_to_something(self):
        ns = load_panel()
        self.assertTrue(ns["PY"])

    def test_job_refuses_without_product(self):
        """找不到源码目录时，子进程一个都不许起。"""
        with mock.patch.dict(os.environ, {"COLORPASS_HOME": ""}), \
                mock.patch("os.getcwd", return_value="C:/definitely/not/here"):
            ns, p = make_panel(payload=STATUS_OK)
            p.start_job("send", "送入")
        self.assertIsNone(p.job)

    def test_note_rotates_when_log_grows(self):
        """状态每 10 秒记一行，日志只追加不清理就会涨到几十 MB。

        轮转要保留「最近一份」—— 出问题时那份才是要看的东西。
        """
        ns = load_panel()
        tmp = tempfile.mkdtemp(prefix="jyb_log_")
        ns["LOG"] = os.path.join(tmp, "panel.log")
        ns["LOG_MAX"] = 120
        for i in range(20):
            ns["note"]("第 %d 行，凑点长度让日志超过阈值" % i)
        self.assertTrue(os.path.exists(ns["LOG"] + ".1"), "该轮转出 .1")
        self.assertLess(os.path.getsize(ns["LOG"]), 300, "轮转后主文件要变小")
        with open(ns["LOG"], encoding="utf-8") as f:
            self.assertIn("第 19 行", f.read(), "最新几行要留在主文件里")
        with open(ns["LOG"] + ".1", encoding="utf-8") as f:
            old = f.read()
        self.assertTrue(old.strip(), "上一份不能被清空")
        self.assertNotIn("第 19 行", old, "轮转出去的是旧的，最新的要留在主文件")

    def test_no_drx_dependency(self):
        ns = load_panel()
        self.assertNotIn("load_pb", ns)
        self.assertNotIn("PB_SRC", ns)
        self.assertNotIn("DRX", ns)

    # ---- 面板只剩「送入 / 写回 / 诊断」 ----
    def test_buttons_wired(self):
        ns, p = make_panel(payload=STATUS_OK)
        on = p.win.On
        for b in ("btnSend", "btnWrite", "btnDiag"):
            self.assertTrue(callable(getattr(getattr(on, b), "Clicked", None)),
                            "%s 没有接上回调" % b)

    def test_only_three_actions_on_panel(self):
        """锁定轨道、清理缓存、刷新、缓存行都不该再出现在面板上。

        挤在同一行的次要按钮会把主按钮的份量稀释掉 —— 这一屏只留「送入」
        「写回」，其余是命令行能力（colorpass lock / gc）。
        """
        ns, p = make_panel(payload=STATUS_OK)
        for wid in ("btnLock", "btnGc", "btnRefresh", "jybCache"):
            self.assertNotIn(wid, p.items, "%s 应该已经从面板上删掉" % wid)
        # 「新草稿名」和「写回剪映」必须是挨着的两个控件，中间不隔别的东西
        self.assertIn("txtName", p.items)
        self.assertIn("btnWrite", p.items)

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
        # 草稿名单独一行（不再带「当前草稿：」前缀），状态交给右上角那两个字
        self.assertEqual(p.items["jybDraft"].Text, "测试草稿")
        self.assertEqual(p.items["jybState"].Text, "● 待重开")
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
        self.assertEqual(p.items["jybState"].Text, "● 就绪")
        got = []
        p.start_job = lambda action, label, extra=None: got.append(action)
        p.win.On.btnSend.Clicked(None)
        self.assertEqual(got, ["send"])
        self.assertIsNone(p._dlg)

    # ---- 草稿显示 / 可用性 ----
    def test_draft_label_shows_current_draft(self):
        """草稿名单独一行大字。上面那行小字已经是「当前草稿」这个标签了。"""
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertEqual(p.items["jybDraft"].Text, "测试草稿")

    def test_everything_disabled_without_draft(self):
        ns, p = make_panel(payload={"draft": None, "jianying": {"processes": [1]},
                                    "opened": False})
        p.apply_status()
        self.assertFalse(p.items["btnSend"].Enabled)
        self.assertFalse(p.items["btnWrite"].Enabled)
        self.assertFalse(p.items["txtName"].Enabled)
        # 没草稿时说一句话，不再显示孤零零的「—」（那根横线在真实面板里
        # 看着像一条断掉的分隔线）
        self.assertEqual(p.items["jybDraft"].Text, "未检测到草稿")
        # 第 1 步自己说「等待剪映」，不让人从灰按钮反推
        self.assertEqual(p.items["step1State"].Text, "等待剪映")
        self.assertEqual(p.items["step2State"].Text, "等待前一步")

    def test_write_locked_before_first_send(self):
        """有草稿、但还没送入过：能送，不能写回，也不能填名字。

        写回读的是「送入那一刻的快照」。没送过就没有东西可写回，这时候让
        按钮亮着，点下去只能弹一句「还没有完成过一次送入」—— 拿报错当提示。
        """
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertTrue(p.items["btnSend"].Enabled)
        self.assertFalse(p.items["btnWrite"].Enabled)
        self.assertFalse(p.items["txtName"].Enabled)
        self.assertIn("先送入", p.items["txtName"].PlaceholderText)

    def test_write_unlocked_after_send(self):
        ns, p = make_panel(payload=SENT_OK)
        p.apply_status()
        self.assertTrue(p.items["btnWrite"].Enabled)
        self.assertTrue(p.items["txtName"].Enabled)
        self.assertEqual(p.items["jybState"].Text, "● 已送入")
        self.assertIn("自动命名", p.items["txtName"].PlaceholderText)

    def test_write_locked_when_last_send_is_another_draft(self):
        """上次送入的是别的草稿 —— 那份快照跟眼前这份没关系，不能写回。"""
        payload = dict(STATUS_OK, last_send={
            "draft": "上一份草稿", "at": "2026-11-19 20:00:00",
            "segments": 8, "committed": True})
        ns, p = make_panel(payload=payload)
        p.apply_status()
        self.assertFalse(p.items["btnWrite"].Enabled)
        self.assertFalse(p.items["txtName"].Enabled)

    def test_write_locked_when_send_not_committed(self):
        """送入没走完（快照不完整）就不算数 —— 写回会把残缺的剪辑带回去。"""
        payload = dict(STATUS_OK, last_send={
            "draft": "测试草稿", "at": "2026-11-19 23:00:00",
            "segments": 5, "committed": False})
        ns, p = make_panel(payload=payload)
        p.apply_status()
        self.assertFalse(p.items["btnWrite"].Enabled)

    def test_send_success_unlocks_write_without_waiting_for_status(self):
        """送入成功的瞬间就解锁写回，不等下一次 status 回来。"""
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertFalse(p.items["btnWrite"].Enabled)
        p.job = self._job(ns, "send", 0, '{"ok": true, "placed": 13}')
        p.poll_job()
        self.assertTrue(p.items["btnWrite"].Enabled)
        self.assertTrue(p.items["txtName"].Enabled)

    def test_disabled_before_first_scan(self):
        """构造完、还没拿到状态之前不能让按钮亮着。"""
        ns, p = make_panel(payload=STATUS_OK)
        p.items["btnSend"].Enabled = True          # 故意打乱
        p._has_draft = False
        p._can_write = False
        p._sync_buttons()
        self.assertFalse(p.items["btnSend"].Enabled)
        self.assertFalse(p.items["btnWrite"].Enabled)

    def test_buttons_disabled_while_job_running(self):
        ns, p = make_panel(payload=SENT_OK)
        p.apply_status()
        p.job = {"proc": _Proc(0), "out": "", "label": "x", "action": "send",
                 "file": None, "t0": 0}
        p._sync_buttons()
        self.assertFalse(p.items["btnSend"].Enabled)
        self.assertFalse(p.items["btnWrite"].Enabled)
        self.assertFalse(p.items["txtName"].Enabled)
        p.job = None
        p._sync_buttons()
        self.assertTrue(p.items["btnSend"].Enabled)
        self.assertTrue(p.items["btnWrite"].Enabled)

    # ---- 自动刷新（面板上没有「刷新」按钮了） ----
    def test_refresh_rescans_on_its_own(self):
        """空闲时面板自己看一眼当前草稿，10 秒一次。

        剪映里换了草稿、面板还挂着上一份的名字，用户就会照着旧名字点送入。
        """
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
        p.maybe_refresh()
        self.assertEqual(len(calls), 1, "刚扫过，不该马上再扫")
        p._last_scan -= 60               # 假装十秒过去了
        p.maybe_refresh()
        self.assertEqual(len(calls), 2, "到点了必须真的重新扫")

    def test_refresh_skipped_while_busy(self):
        """任务在跑时不轮询 —— 那一次结果必然被覆盖，白起一个进程。"""
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
        p = Panel()
        p.job = {"proc": _Proc(0), "out": "", "label": "x", "action": "send",
                 "file": None, "t0": 0}
        p._last_scan -= 60
        p.maybe_refresh()
        self.assertEqual(len(calls), 1)

    def test_slow_scan_backs_off(self):
        """一次查询超过 2.5 秒的机器，轮询间隔退到 30 秒。

        剪映内存读不出来时 status 会一直等到超时；这台机器再被 10 秒一次的
        轮询追着，等于永远有一个子进程在跑。
        """
        import time as _t
        ns, p = make_panel(payload=STATUS_OK)
        self.assertEqual(p._scan_interval, ns["AUTO_REFRESH"])

        def slow(*a, **kw):
            _t.sleep(ns["SLOW_SCAN"] + 0.05)
            return _RunResult(json.dumps(STATUS_OK, ensure_ascii=False))

        ns["subprocess"].run = slow
        p._status_worker()
        self.assertEqual(p._scan_interval, ns["AUTO_REFRESH_SLOW"])

    def test_refresh_mid_scan_keeps_single_flight(self):
        ns, p = make_panel(payload=STATUS_OK)
        p._st_running = True
        p.refresh_draft()                      # 不许起第二个
        self.assertTrue(p._st_running)
        self.assertTrue(p._want_refresh, "被拒的刷新要记下来")

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

    # ---- 页脚的「联系作者」 ----
    def test_footer_has_about_button(self):
        """页脚正中间那个入口。它是唯一与调色流程无关的按钮，
        位置和文案都别被后来的改动挤掉。"""
        ns, p = make_panel(payload=STATUS_OK)
        self.assertEqual(p.items["btnAbout"].Text, "联系作者")

    def test_about_button_opens_window(self):
        ns, p = make_panel(payload=STATUS_OK)
        got = []

        class FakePopen(object):
            def __init__(self, cmd, **kw):
                got.append((cmd, kw))

        ns["subprocess"].Popen = FakePopen
        p.win.On.btnAbout.Clicked(None)
        self.assertEqual(len(got), 1)
        cmd, kw = got[0]
        self.assertIn("about", cmd)
        # 弹窗不该顺带闪一个黑框：面板起的子进程一律带 CREATE_NO_WINDOW
        self.assertEqual(kw.get("creationflags"), ns["NO_WIN"])

    def test_about_button_survives_missing_backend(self):
        """找不到运行入口时说人话，而不是抛异常。"""
        ns, p = make_panel(payload=STATUS_OK)
        ns["_no_backend"] = lambda: True
        p.win.On.btnAbout.Clicked(None)     # 不抛就算过

    # ---- 版面：密度与层级 ----
    def test_window_height_matches_content(self):
        """窗口高度要和内容对得上。

        460×578 的时候，量出来内容只有 328px、页脚底边在 466 —— 净剩一百多
        像素是组间距和弹簧，用户看到的就是「页面利用率很低」。这里用上界
        卡住，防止有人把窗口又拉高、或者往 _body 里再塞一层 VGroup。
        """
        src = PANEL.read_text(encoding="utf-8")
        m = re.search(r"^WIN_W, WIN_H = (\d+), (\d+)$", src, re.M)
        self.assertIsNotNone(m, "找不到窗口尺寸常量")
        self.assertEqual(int(m.group(1)), 460)
        self.assertEqual(int(m.group(2)), 465)
        # 顶层只许一个 VGroup（外面一个 HGroup 负责左右留白）
        ns, p = make_panel(payload=STATUS_OK)
        self.assertEqual(len(p._body()), 1)

    def test_draft_block_is_one_group_of_two_labels(self):
        """草稿名 + 说明只套一层 VGroup。

        每多套一层，Fusion 就再加一次默认 spacing —— 行距失控正是这么来的。
        所以这里卡死「恰好一层、恰好两个 Label」。
        """
        ns, p = make_panel(payload=STATUS_OK)
        block = p._draft_block()
        self.assertEqual(block.kind, "VGroup")
        self.assertEqual([e.kind for e in block.children], ["Label", "Label"])
        self.assertEqual([e.ID for e in block.children], ["jybDraft", "jybInfo"])

    def test_buttons_share_one_size_token(self):
        """两颗按钮必须同宽同高。

        宽度靠 min-width/max-width 锁死 120（文字长短不同，靠内容撑会 118/114）；
        高度靠同一套 font-size + padding + 1px 边框 —— 主按钮也要那圈同色边框，
        少了它就是 32 对 34，截图上一眼能看出来。

        字体本身也得一模一样：主按钮曾经加过 font-weight:600，21.2 换成粗体
        字面后行高从 18 变 25，实测「送入达芬奇」41 高、「写回剪映」34 高，
        而且粗体看着比旁边大一号 —— 用户原话是「框不一样大小、字也不一样大」。
        """
        ns, p = make_panel(payload=STATUS_OK)
        ctrl_w, pad_v, f_btn = ns["CTRL_W"], ns["CTRL_PAD_V"], ns["F_BTN"]
        for kind in ("primary", "secondary"):
            for on in (True, False):
                qss = ns["_btn_qss"](kind, on)
                self.assertIn("min-width:%dpx;" % ctrl_w, qss, (kind, on))
                self.assertIn("max-width:%dpx;" % ctrl_w, qss, (kind, on))
                self.assertIn("padding:%dpx 0;" % pad_v, qss, (kind, on))
                self.assertIn("border:1px solid", qss, (kind, on))
                self.assertIn("border-radius:8px;", qss, (kind, on))
                self.assertIn("font-size:%dpx;" % f_btn, qss, (kind, on))
                self.assertNotIn("font-weight", qss, (kind, on))

    def test_buttons_capped_to_same_height(self):
        """两颗按钮的垂直上限必须一致，且是设计高度。

        它们分别和「草稿名 + 两行说明」（约 60 高）和「单行输入框」（34 高）
        同处一个 HGroup，而 Fusion 会把同排子项拉满行高 —— macOS 真机量到
        主按钮 60 高、次按钮 34 高，两颗按钮一高一矮。QSS 的 padding 管不了
        这件事（它决定内容高度，不决定控件被拉伸到多高），只能在控件上用
        MaximumSize 卡上限。不卡就会退回到 60 vs 34。
        """
        ns, p = make_panel(payload=STATUS_OK)
        send = list(p.items["btnSend"].MaximumSize)
        write = list(p.items["btnWrite"].MaximumSize)
        self.assertEqual(send, write)
        self.assertEqual(send[1], ns["CTRL_H"])
        self.assertEqual(send[0], ns["CTRL_W"] + 2)

    def test_input_matches_button_height(self):
        """输入框和按钮同一套尺寸令牌，并排时才对得齐。"""
        ns, p = make_panel(payload=STATUS_OK)
        pad_v, f_btn = ns["CTRL_PAD_V"], ns["F_BTN"]
        for on in (True, False):
            qss = ns["_input_qss"](on)
            self.assertIn("padding:%dpx 10px;" % pad_v, qss)
            self.assertIn("border:1px solid", qss)
            self.assertIn("border-radius:8px;", qss)
            self.assertIn("font-size:%dpx;" % f_btn, qss)

    def test_text_boxes_are_tall_enough_for_their_lines(self):
        """说明区 / 结果区是「自己折行 + 固定高度」，两个数字必须对得上。

        0.6.0 的真机症状：说明区开了 WordWrap，但高度只有 16px（一行），
        「没认出当前草稿：…」折成两行后第二行被齐腰切掉，截图里像一行乱码。
        现在行数由 wrap_text 决定，高度 = 行数 × LH_INFO —— 这里把两组数字
        钉在一起，谁改都会红。
        """
        ns, p = make_panel(payload=STATUS_OK)
        lh = ns["LH_INFO"]
        self.assertEqual(p.items["jybInfo"].MaximumSize[1], lh * 2)
        self.assertEqual(p.items["jybInfo"].MinimumSize[1], lh * 2)
        self.assertEqual(p.items["jybResult"].MaximumSize[1],
                         lh * ns["RESULT_LINES"])
        self.assertEqual(p.items["jybDraft"].MaximumSize[1], ns["LH_NAME"])

    def test_wrap_text_fits_the_budget(self):
        """折出来的每一行都得放得下（按显示宽度算，不是字符数）。"""
        ns = load_panel()
        w = ns["wrap_text"]
        cases = [
            "剪映在运行，但没认出当前草稿。在剪映里打开一份草稿，面板会自己跟上。",
            "没认出草稿：开了多个剪映窗口。\n先关掉多余的，只留一份。",
            "读取草稿状态失败，详见「诊断」。",
            "已送入（11-19 23:47）。调完色就能写回。",
            "",
        ]
        for text in cases:
            out = w(text, 18, 2)
            self.assertLessEqual(len(out.splitlines()), 2, text)
            for line in out.splitlines():
                width = sum(1.0 if ord(ch) > 0x2E7F else 0.55 for ch in line)
                self.assertLessEqual(width, 18.001, line)

    def test_wrap_text_keeps_the_action_line(self):
        """作者自己分段的那句「怎么办」优先保留，不许被省略号吃掉。"""
        ns = load_panel()
        out = ns["wrap_text"]("没认出草稿：开了多个剪映窗口。\n先关掉多余的，"
                              "只留一份。", 18, 2)
        self.assertIn("先关掉多余的", out)
        # 超长时才加省略号，且只加在末行
        long = ns["wrap_text"]("很长" * 80, 18, 2)
        self.assertTrue(long.splitlines()[-1].endswith("…"))

    def test_footer_links_are_bare_text(self):
        """页脚三个入口都是裸字，不许再套胶囊底。

        0.6.0 给它们加了 C_PILL 圆角底，用户的原话是「诊断和联系作者为什么
        变成了在框里」—— 底部四个灰方块和输入框撞脸，整页看着像一排控件。
        左右 padding 也必须归零：页脚两端要和上面的正文对齐在同一条竖轴上。
        """
        ns, p = make_panel(payload=STATUS_OK)
        for kind in ("ghost", "link"):
            qss = ns["_btn_qss"](kind, True)
            self.assertIn("background-color:transparent;", qss)
            self.assertIn("border:0;", qss)
            self.assertIn("padding:4px 0;", qss)
            self.assertNotIn(ns["C_PILL"], qss)

    def test_step_state_sits_next_to_title(self):
        """状态字要挨着标题，不能靠右对齐到下一行按钮的正上方。
        原来它顶在最右端，截图里看着像压住了按钮，而且离标题 300px，
        读起来不像这一步的属性。
        """
        ns, p = make_panel(payload=STATUS_OK)
        head = p._step_head(1, "读取草稿")
        self.assertEqual(head.children[-1].Weight, 1)       # 弹簧在最后
        self.assertEqual(head.children[-2].ID, "step1State")  # 状态紧贴标题一侧
        self.assertEqual(head.children[0].kind, "Label")     # 序号
        self.assertEqual(head.children[0].Text, "1")

    def test_type_scale_is_three_steps(self):
        """字号只有 11 / 13 / 15 三档。

        18/15/13/12/11 五档的时候，1px 分不出层级，18 的草稿名反而压过
        15 的产品名；12 那一档只用于步骤名，和 13 摆在一起仍然看不出差别，
        于是整页同时有三种几乎一样大的字。现在步骤名回到 13，靠 600 字重
        区分，字号只剩三档。
        """
        ns, p = make_panel(payload=STATUS_OK)
        sizes = sorted({ns[k] for k in ("F_TITLE", "F_NAME", "F_BTN",
                                       "F_BODY", "F_STEP", "F_NOTE")})
        self.assertEqual(sizes, [11, 13, 15])

    def test_step_heads_tell_their_own_state(self):
        """两步各自带一句状态，不靠按钮灰度让人反推。"""
        ns, p = make_panel(payload=STATUS_OK)
        self.assertTrue(p.items["step1State"].Text)
        self.assertTrue(p.items["step2State"].Text)
        # 有草稿但没送入过：第 1 步就绪、第 2 步等前一步
        p.apply_status()
        self.assertEqual(p.items["step1State"].Text, "就绪")
        self.assertEqual(p.items["step2State"].Text, "等待前一步")

    def test_no_em_dash_placeholder(self):
        """没草稿时不许再摆一个「—」：那根横线看着像断掉的分隔线。"""
        ns, p = make_panel(payload={"draft": None, "jianying": {"processes": []},
                                    "opened": False})
        p.apply_status()
        self.assertNotEqual(p.items["jybDraft"].Text.strip(), "—")


        """版本号退回成一行小字，旁边才是「检查更新」按钮 —— 点按钮才知道
        会发生什么，版本号只是给人看的。"""
        ns, p = make_panel(payload=STATUS_OK)
        self.assertRegex(p.items["jybVer"].Text, r"^v\d+\.\d+\.\d+$")
        self.assertEqual(p.items["btnUpdate"].Text, "检查更新")

    def test_opening_panel_does_not_check(self):
        """开窗不再自动查更新：查一次要起子进程、走网络，而用户开面板是
        为了送入/写回。只留按钮这一条路径。"""
        src = PANEL.read_text(encoding="utf-8")
        self.assertNotIn("Timer(4.0", src)
        self.assertEqual(src.count("self._check_update_async()"), 1)

    def test_check_button_checks_once(self):
        ns, p = make_panel(payload=STATUS_OK)
        calls = []
        p._check_update_async = lambda: calls.append(1)
        p.win.On.btnUpdate.Clicked(None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(p.items["btnUpdate"].Text, "检查中…")
        # 真实实现里第一次点击就把 _upd_checking 置位了；这里手动置位，
        # 连点第二下不该再起一次查询。
        p._upd_checking = True
        p.win.On.btnUpdate.Clicked(None)
        self.assertEqual(len(calls), 1)

    def test_update_available_asks_before_installing(self):
        """发现新版只弹窗问一句，不自己下载。"""
        ns, p = make_panel(payload=STATUS_OK)
        got = []

        class FakePopen(object):
            def __init__(self, cmd, **kw):
                got.append((cmd, kw))

        ns["subprocess"].Popen = FakePopen
        p._apply_update({"ok": True, "has_new": True, "latest": "9.9.9",
                         "size": 56000000, "notes": "· 修了 A"})
        self.assertTrue(p._upd_ready)
        self.assertEqual(got, [])                       # 一个进程都没起
        self.assertEqual(p.items["btnUpdate"].Text, "检查更新")   # 按钮不改名
        self.assertIsNotNone(p._dlg)
        box = p._dlg.GetItems()
        body = box["dMsg"].Text
        self.assertIn("9.9.9", body)
        self.assertIn("56.0 MB", body)
        self.assertIn("修了 A", body)                    # 带更新说明
        self.assertEqual(box["dOk"].Text, "以后再说")
        self.assertEqual(box["dHint"].Text, "")          # 不是失败，不贴日志
        self.assertIn("9.9.9", p.items["jybResult"].Text)

    def test_install_only_after_confirm(self):
        """弹窗里点「下载并安装」才真的起安装器。"""
        ns, p = make_panel(payload=STATUS_OK)
        got = []

        class FakePopen(object):
            def __init__(self, cmd, **kw):
                got.append((cmd, kw))

        ns["subprocess"].Popen = FakePopen
        p._apply_update({"ok": True, "has_new": True, "latest": "9.9.9",
                         "size": 56000000})
        self.assertEqual(got, [])
        p._run_act()                                    # 等价于点那颗按钮
        self.assertEqual(len(got), 1)
        self.assertIn("--install", got[0][0])
        self.assertEqual(got[0][1].get("creationflags"), ns["NO_WIN"])
        self.assertTrue(p._upd_busy)
        self.assertEqual(p.items["btnUpdate"].Text, "更新中…")

    def test_dismiss_update_runs_nothing(self):
        """「以后再说」= 什么也不做。"""
        ns, p = make_panel(payload=STATUS_OK)
        got = []

        class FakePopen(object):
            def __init__(self, cmd, **kw):
                got.append((cmd, kw))

        ns["subprocess"].Popen = FakePopen
        p._apply_update({"ok": True, "has_new": True, "latest": "9.9.9"})
        p._dlg.On.dOk.Clicked(None)
        self.assertEqual(got, [])
        self.assertFalse(p._upd_busy)
        self.assertIsNone(p._dlg)
        self.assertEqual(p.items["btnUpdate"].Text, "检查更新")

    def test_update_check_failure_is_explained(self):
        """点了才查，所以失败必须说清楚 —— 而且要给一条手动下载的路。"""
        ns, p = make_panel(payload=STATUS_OK)
        p._apply_update({"ok": False, "error": "URLError"})
        self.assertIn("检查更新失败", p.items["jybResult"].Text)
        self.assertIn("releases", p.items["jybResult"].Text)
        self.assertEqual(p.items["btnUpdate"].Text, "检查更新")
        self.assertFalse(p._upd_ready)

    def test_no_new_version_keeps_plain_button(self):
        ns, p = make_panel(payload=STATUS_OK)
        p._apply_update({"ok": True, "has_new": False, "latest": "0.0.1"})
        self.assertFalse(p._upd_ready)
        self.assertEqual(p.items["btnUpdate"].Text, "检查更新")

    def test_install_progress_then_done(self):
        """安装期间读的是 update.json 和 installed.json，不是子进程的 stdout ——
        那个进程会被它自己启动的安装器杀掉。"""
        ns, p = make_panel(payload=STATUS_OK)
        d = tempfile.mkdtemp(prefix="cp_updui_")
        old = (ns["UPD_FILE"], ns["UPD_INSTALLED"])
        try:
            ns["UPD_FILE"] = os.path.join(d, "update.json")
            ns["UPD_INSTALLED"] = os.path.join(d, "installed.json")
            p._upd_busy = True
            p._upd_latest = "9.9.9"

            with open(ns["UPD_FILE"], "w", encoding="utf-8") as f:
                json.dump({"phase": "downloading", "pct": 42,
                           "got": 24000000, "target": "9.9.9"}, f)
            p.poll_update()
            self.assertIn("42%", p.items["jybResult"].Text)
            self.assertTrue(p._upd_busy)

            with open(ns["UPD_FILE"], "w", encoding="utf-8") as f:
                json.dump({"phase": "installing", "target": "9.9.9"}, f)
            p._upd_read_at = 0          # 进度读取有 0.4 秒节流
            p.poll_update()
            self.assertTrue(p._upd_busy)
            self.assertIn("正在安装", p.items["jybResult"].Text)

            with open(ns["UPD_INSTALLED"], "w", encoding="utf-8") as f:
                json.dump({"version": "9.9.9"}, f)
            p._upd_read_at = 0
            p.poll_update()
            self.assertFalse(p._upd_busy)
            self.assertEqual(p._upd_done, "9.9.9")
            self.assertIn("已更新到", p.items["jybResult"].Text)
            self.assertIn("已更新", p.items["btnUpdate"].Text)
        finally:
            ns["UPD_FILE"], ns["UPD_INSTALLED"] = old
            shutil.rmtree(d, ignore_errors=True)

    def test_install_error_is_reported(self):
        ns, p = make_panel(payload=STATUS_OK)
        d = tempfile.mkdtemp(prefix="cp_upderr_")
        old = ns["UPD_FILE"]
        try:
            ns["UPD_FILE"] = os.path.join(d, "update.json")
            p._upd_busy = True
            with open(ns["UPD_FILE"], "w", encoding="utf-8") as f:
                json.dump({"phase": "error", "msg": "下载失败：连不上"}, f)
            p._upd_read_at = 0
            p.poll_update()
            self.assertFalse(p._upd_busy)
            self.assertIn("下载失败", p.items["jybResult"].Text)
        finally:
            ns["UPD_FILE"] = old
            shutil.rmtree(d, ignore_errors=True)

    def test_write_passes_name(self):
        ns, p = make_panel(payload=SENT_OK)
        p.apply_status()
        p.items["txtName"].Text = "我的片子"
        got = []
        p.start_job = lambda action, label, extra=None: got.append((action, extra))
        p.win.On.btnWrite.Clicked(None)
        self.assertEqual(got, [("write", ["--name", "我的片子"])])

    def test_long_draft_name_is_elided(self):
        """面板只给草稿名一行。长名字截断加省略号 —— 剪到半个字更像坏了。"""
        long_name = "很长的草稿名" * 5
        ns, p = make_panel(payload=dict(STATUS_OK, draft=long_name))
        p.apply_status()
        shown = p.items["jybDraft"].Text
        self.assertTrue(shown.endswith("…"))
        self.assertLessEqual(len(shown), 26)
        # 显示可以截，逻辑用的名字必须是全的
        self.assertEqual(p._draft_name, long_name)

    def test_short_stamp(self):
        ss = load_panel()["short_stamp"]
        self.assertEqual(ss("2026-11-19 23:47:41"), "11-19 23:47")
        self.assertEqual(ss(""), "")
        self.assertEqual(ss(None), "")

    def test_apply_status_ready(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.apply_status()
        self.assertEqual(p.items["jybState"].Text, "● 就绪")
        self.assertIn("可以送入", p.items["jybInfo"].Text)
        self.assertIn("测试草稿", p.items["jybDraft"].Text)

    def test_apply_status_sent(self):
        ns, p = make_panel(payload=SENT_OK)
        p.apply_status()
        self.assertEqual(p.items["jybState"].Text, "● 已送入")
        self.assertIn("11-19 23:47", p.items["jybInfo"].Text)
        self.assertIn("写回", p.items["jybInfo"].Text)

    def test_apply_status_not_running(self):
        ns, p = make_panel(payload={"draft": None, "jianying": {"processes": []},
                                    "opened": False})
        p.apply_status()
        self.assertIn("剪映没在运行", p.items["jybInfo"].Text)
        self.assertEqual(p.items["jybState"].Text, "● 未运行")

    def test_apply_status_multi_window_tells_the_reason(self):
        """同时开两个剪映时，通用那句「打开一份草稿」会让用户干瞪眼 ——
        他明明已经打开了。状态区要换成具体原因 + 一句可操作的话。"""
        ns, p = make_panel(payload={
            "draft": None, "opened": False,
            "jianying": {"processes": [11, 22]},
            "draft_error": "窗口工程名或实时文件句柄对应多个进程或草稿目录，无法精确匹配。"})
        p.apply_status()
        info = p.items["jybInfo"].Text
        self.assertIn("没认出草稿", info)
        self.assertIn("多个", info)
        self.assertIn("关掉", info)
        # 两行封顶：后端原文 40+ 字，原样塞进来会被截成半句
        self.assertLessEqual(len(info.splitlines()), 2)

    def test_apply_status_timeout_is_not_fatal(self):
        def boom(*a, **kw):
            raise TimeoutError("timed out")
        ns, p = make_panel(payload=boom)
        p.apply_status()                      # 不许抛
        self.assertIn("读取草稿状态失败", p.items["jybInfo"].Text)
        self.assertFalse(p.items["btnWrite"].Enabled)

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
            "send", {"placed": 3, "exact": 3, "timeline": "CP · 11月18日",
                     "speed_mode": "bake"})
        self.assertIn("3 段", s)
        self.assertIn("CP · 11月18日", s)

    def test_summary_write(self):
        s = load_panel()["Panel"]._summary(
            "write", {"draft_name": "11月18日_调色", "luts": 3})
        self.assertIn("11月18日_调色", s)
        self.assertIn("3 段", s)

    def test_summary_send_says_speed_is_real(self):
        """预渲染过就要说「真变速」，别只说「已处理」——用户要知道慢放在
        达芬奇里能不能按剪映的快慢播放。"""
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "timeline": "CP · 草稿",
                     "speed_mode": "bake",
                     "baked": [{"segment_id": "a"}, {"segment_id": "b"}]})
        self.assertIn("变速段 2 段", s)
        self.assertIn("真变速", s)

    def test_summary_send_fallback_tells_reinstall(self):
        """ffmpeg 已随包自带。还走到兜底只可能是安装残缺，
        所以给的动作是「重装」，不是让他去装 ffmpeg。"""
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "speed_mode": "slot",
                     "speed_fallback": "这台机器上少了自带的 ffmpeg"})
        self.assertIn("重新运行一次安装包", s)
        self.assertNotIn("缺 ffmpeg", s)

    def test_summary_send_mentions_disabled(self):
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "timeline": "CP · 草稿",
                     "speed_mode": "bake", "disabled": 1})
        self.assertIn("停用段 1 段", s)

    def test_summary_send_silent_when_no_disabled(self):
        s = load_panel()["Panel"]._summary(
            "send", {"placed": 3, "exact": 3, "timeline": "CP · 草稿"})
        self.assertNotIn("停用", s)

    def test_summary_write_keeps_frames_and_warns_about_later_edits(self):
        """结果区只有 3 行。挤的时候要按「帧数 > 剪映改过 > 原样保留」让位：
        帧数是用户最怕变的，改过的提醒是唯一会让人白干活的坑。"""
        s = load_panel()["Panel"]._summary("write", {
            "draft_name": "X_调色", "luts": 3,
            "basics": {"frames_src": 675, "frames_new": 675,
                       "canvas_new": [1920, 1080], "segments_checked": 18},
            "kept": {"speed_segments": 2, "transitions": 5},
            "drift": {"same": False, "added": 1, "removed": 0, "changed": 2,
                      "segments_sent": 18}})
        lines = s.splitlines()
        self.assertLessEqual(len(lines), 3)
        self.assertIn("675", lines[1])          # 帧数紧跟标题，不会被挤掉
        self.assertIn("不含这些改动", s)
        self.assertNotIn("原样保留", s)          # 三行放不下，只能让位

    def test_summary_write_no_warning_when_draft_untouched(self):
        s = load_panel()["Panel"]._summary("write", {
            "draft_name": "X", "luts": 3,
            "basics": {"frames_src": 90, "frames_new": 90,
                       "canvas_new": [1080, 1920], "segments_checked": 2},
            "drift": {"same": True, "segments_sent": 2}})
        self.assertNotIn("改过", s)
        self.assertIn("1080x1920", s)

    def test_summary_reset_names_the_version_it_will_use(self):
        """重置完得说清「接下来按哪一版」，否则用户不知道该点写回还是重新送入。"""
        s = load_panel()["Panel"]._summary("reset", {
            "ok": True, "will_use": {"draft": "11月18日",
                                     "at": "2026-11-19 23:47:41",
                                     "timeline": "CP · 11月18日"}})
        self.assertIn("11月18日", s)
        self.assertIn("写回", s)

    def test_tail_phase_shows_last_progress_line(self):
        """长任务（几十秒到两分钟）不能只有一个跳动的秒数。"""
        Panel = load_panel()["Panel"]
        tmp = tempfile.mkdtemp(prefix="jyb_phase_")
        out = os.path.join(tmp, "out.txt")
        Path(out).write_text(
            "草稿 草稿A | 视频段 18\n"
            "·· 取回调色：逐段导出 LUT（18 段）\n"
            "·· 取回 LUT 18/18\n"
            "{\n \"ok\": true\n}\n", encoding="utf-8")
        ph = Panel._tail_phase({"out": out}, 1.0)
        self.assertIn("取回 LUT 18/18", ph)

    def test_tail_phase_ignores_json_report(self):
        """输出里只有最终 JSON 时不能把 `{\"ok\": true` 当成进度贴出来。"""
        Panel = load_panel()["Panel"]
        tmp = tempfile.mkdtemp(prefix="jyb_phase_")
        out = os.path.join(tmp, "out.txt")
        Path(out).write_text('{\n "ok": true,\n "placed": 3\n}\n',
                             encoding="utf-8")
        self.assertEqual(Panel._tail_phase({"out": out}, 1.0), "执行中 ")

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
                "action": action, "file": None, "t0": 0, "extra": []}

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

    def test_job_success_shows_missing_media_warning(self):
        """缺素材的段是静默不送的，唯一让它露面的通道就是这行 `!! `。"""
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "send", 0, "\n".join([
            '{"ok": true, "placed": 15, "exact": 15, "timeline": "CP · 草稿"}',
            "!! 3 段没送进去（素材不在盘上 ×3）",
        ]))
        p.poll_job()
        info = p.items["jybResult"].Text
        self.assertIn("15 段", info)
        self.assertIn("3 段没送进去", info)

    def test_job_failure_pops_dialog(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "send", 4,
                          '{"ok": false, "gate": "stale_draft", "error": "对不上"}')
        p.poll_job()
        self.assertIsNotNone(p._dlg, "失败必须弹窗")
        body = p._dlg.GetItems()["dMsg"].Text
        self.assertIn("重新打开", body)
        self.assertIn("没有完成", p.items["jybResult"].Text)

    def test_popup_offers_reset_when_send_incomplete(self):
        """「上次送入没走完」重试只会报同一句：按钮要换成「重置这次送入」。

        面板里没有终端，旧文案让人执行 `colorpass reset` —— 用户卡在这里只能
        重试到放弃。
        """
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "write", 1,
                          '{"ok": false, "need_reset": true,'
                          ' "error": "最近一次「送入达芬奇」没有走完（11-22 01:00）"}')
        p.poll_job()
        self.assertIsNotNone(p._dlg, "要弹窗")
        btn = p._dlg.GetItems()["dAct"]
        self.assertIn("重置", btn.Text)
        got = []
        p.start_job = lambda action, label, extra=None: got.append(action)
        p._run_act()
        self.assertEqual(got, ["reset"])

    def test_job_reset_success_says_which_version(self):
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "reset", 0,
                          '{"ok": true, "will_use": {"draft": "11月18日",'
                          ' "at": "2026-11-19 23:47:41"}}')
        p.poll_job()
        text = p.items["jybResult"].Text
        self.assertIn("11月18日", text)
        self.assertIn("写回", text)
        self.assertIsNone(p._dlg, "重置成功不该弹窗")

    def test_job_write_warns_about_edits_after_send(self):
        """整条链路走一遍：写回成功时，「帧数」和「剪映改过」两件事都要留在面板上。"""
        ns, p = make_panel(payload=STATUS_OK)
        p.job = self._job(ns, "write", 0, json.dumps({
            "ok": True, "draft_name": "X_调色", "luts": 18,
            "basics": {"frames_src": 675, "frames_new": 675,
                       "canvas_new": [1920, 1080], "segments_checked": 18},
            "drift": {"same": False, "added": 1, "removed": 0, "changed": 2,
                      "segments_sent": 18}}, ensure_ascii=False))
        p.poll_job()
        text = p.items["jybResult"].Text
        self.assertIn("675", text)
        self.assertIn("不含这些改动", text)
        self.assertLessEqual(len(text.splitlines()), 3)
        self.assertIsNone(p._dlg, "成功不弹窗")

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
        p.job = self._job(ns, "write", 0,
                          '{"ok": true, "draft_name": "新片_调色", "luts": 13}')
        p.poll_job()
        shown = p.items["jybResult"].Text
        self.assertIn("新片_调色", shown)
        p.apply_status()                      # 状态刷新回来
        self.assertEqual(p.items["jybResult"].Text, shown, "结果行不该被状态覆盖")
        self.assertIn("测试草稿", p.items["jybDraft"].Text)

    def test_refresh_requested_while_busy_is_queued(self):
        """查询在跑时来的刷新不能丢。

        任务结束时 poll_job 会 refresh_draft()；如果那一刻恰好有一次旧查询
        在跑，旧版直接 return —— 面板会永远停在任务前的草稿和送入状态。
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
        self.assertIn("补刷的草稿", p.items["jybDraft"].Text)

    # ---- 建窗降级 ----
    def test_falls_back_to_plain_when_styles_rejected(self):
        """带样式建不出来时，去掉样式再建一次 —— 面板不能因此打不开。

        `StyleSheet` / `MinimumSize` 不是每个 Fusion 版本都认；认不了的版本
        AddWindow 直接返回 None。整窗打不开是最坏的结果，比没有质感严重得多。
        """
        ns = load_panel(payload=STATUS_OK)
        ui = ns["fusion"].UIManager
        disp = _FlakyDisp(ui, fail_times=1)
        ns["bmd"] = _Bmd(disp)
        Panel = ns["Panel"]
        Panel.loop = lambda self: None
        p = Panel()
        self.assertFalse(p.styled, "第二次必须是「不带样式」的那一版")
        self.assertEqual(len(disp.calls), 2)
        self.assertIn("StyleSheet", disp.calls[0], "第一次应该带着样式试")
        self.assertNotIn("StyleSheet", disp.calls[1], "重试时样式必须摘掉")
        # 降级之后功能一个都不许少
        p.apply_status()
        self.assertIn("测试草稿", p.items["jybDraft"].Text)
        self.assertTrue(callable(getattr(p.win.On.btnSend, "Clicked", None)))

    def test_last_resort_minimal_window(self):
        """连朴素版都建不出来时，退到一个空壳，不许抛异常。"""
        ns = load_panel(payload=STATUS_OK)
        ns["bmd"] = _Bmd(_FlakyDisp(ns["fusion"].UIManager, fail_times=2))
        Panel = ns["Panel"]
        Panel.loop = lambda self: None
        p = Panel()
        self.assertIsNotNone(p.win)
        self.assertEqual(p.win.definition["ID"], "jybWin_c")

    # ---- 兼容性自检 ----
    def test_diag_shows_copyable_report(self):
        """「诊断」把自检结果摊在一个可全选复制的框里。

        这个工具是发给别人测的，出问题的人不一定会开终端。点一下 → 全选 →
        复制 → 发回来，这三步必须在面板里能完成。
        """
        ns, p = make_panel(payload=STATUS_OK)
        p.win.On.btnDiag.Clicked(None)          # 假线程是同步的，跑完即返回
        p.poll_diag()
        self.assertIsNotNone(p._dlg, "自检必须有结果窗口")
        text = p._dlg.GetItems()["dMsg"].Text
        self.assertTrue(text.strip(), "结果不能是空的")
        self.assertIn("测试草稿", text, "这里应带上 status 的输出")

    def test_diag_runs_once_at_a_time(self):
        ns, p = make_panel(payload=STATUS_OK)
        p._diag_running = True
        p.win.On.btnDiag.Clicked(None)
        self.assertEqual(p._diag_text, "", "正在跑就别再起一个")


class PanelVersionTest(unittest.TestCase):
    """面板是独立脚本，版本号只能手写一份 —— 这里防止它和包版本走散。"""

    def test_matches_package_version(self):
        import colorpass
        ns = load_panel()
        self.assertEqual(ns["PANEL_VERSION"], colorpass.__version__)


if __name__ == "__main__":
    unittest.main()
