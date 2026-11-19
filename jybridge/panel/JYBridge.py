"""JYBridge · 剪映 ⇄ 达芬奇 往返（精简版）

只做一件事：**把剪映的剪辑送进达芬奇，调完色再写回一份新草稿。**

面板上只有三个动作：
  ① 送入达芬奇      —— 剪映当前草稿 → 达芬奇时间线（逐段按帧号摆放）
  ② 写回剪映        —— 达芬奇逐段 ExportLUT → 克隆出新的剪映草稿
  锁定 / 解锁轨道   —— 防止在达芬奇里误改剪辑（调色不受影响）

参考仿色、非常规偏色、一级调色三个功能已从本插件移除（2026-11-19）。
它们的代码移到了 `_stripped/`，面板不再加载，也不再依赖 `drx/PrimaryBalance.py`。

线程模型（沿用实测结论，别改）：
  Fusion 的 UIDispatcher 没有 AddTimer，RunLoop 还会抢 GIL，所以面板只能
  单线程 StepLoop 轮询；真正的活在子进程里干（`python -m jybridge.cli ...`），
  面板只负责起进程、读它写出来的结果文件。
  `cli status` 也放后台线程跑 —— 剪映内存读不出来时它会挂到超时，
  放主循环里就是整窗卡死（旧版实测过）。

安装：
    python -m jybridge.install
"""
import glob
import json
import os
import re
import subprocess
import threading
import time
import traceback

# ---------------------------------------------------------------- 装在哪、用哪个解释器
#
# 这两个值以前是写死的绝对路径 —— 只有作者本机能跑。开源后必须自动定位：
#
#   python    JYBRIDGE_PYTHON → config.json 的 python → 常见安装位置（取版本最高）
#             → PATH 上的 python
#   product   有 jybridge/cli.py 的目录：JYBRIDGE_HOME → config.json 的 product
#             → %LOCALAPPDATA%\JYBridge\product → 当前目录
#
# config.json 由 `python -m jybridge.install` 写，里面记着「装的时候用的是谁」。
# 这样 pip 安装、克隆源码、绿色解压三种情况都能对上，不用改代码。
BASE_DIR = os.path.join(os.environ.get("LOCALAPPDATA")
                        or os.path.expanduser("~"), "JYBridge")
CONF_PATH = os.path.join(BASE_DIR, "config.json")


def _conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _is_product(p):
    return bool(p) and os.path.isfile(os.path.join(p, "jybridge", "cli.py"))


def _find_product():
    conf = _conf()
    cands = [os.environ.get("JYBRIDGE_HOME"), conf.get("product"),
             os.path.join(BASE_DIR, "product"), os.getcwd()]
    for c in cands:
        if _is_product(c):
            return os.path.abspath(c)
    return ""


def _ver_key(path):
    m = re.search(r"[Pp]ython(\d)(\d+)", path.replace("\\", "/"))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _find_python():
    conf = _conf()
    for c in (os.environ.get("JYBRIDGE_PYTHON"), conf.get("python")):
        if c and os.path.isfile(c):
            return c
    pats = [
        os.path.join(os.environ.get("LOCALAPPDATA") or "",
                     "Programs", "Python", "Python3*", "python.exe"),
        os.path.join(os.environ.get("PROGRAMFILES") or "",
                     "Python3*", "python.exe"),
        "C:/Python3*/python.exe",
    ]
    hits = []
    for pat in pats:
        hits += [h for h in glob.glob(pat) if os.path.isfile(h)]
    if hits:
        return max(hits, key=_ver_key)
    return "python"          # 交给 PATH；找不到时子进程会报错，面板照原样显示


PY = _find_python()
PRODUCT = _find_product()
# 面板自己的日志与子进程输出放在 %LOCALAPPDATA%\JYBridge\panel\
# 不再借用 drx\generated\ —— 那个目录属于已移除的一级调色插件。
OUT = os.path.join(BASE_DIR, "panel")
LOG = os.path.join(OUT, "jybridge_panel.log")
NO_WIN = 0x08000000

# 找不到源码目录时，面板唯一的正确动作是「说清楚怎么修」，而不是弹一个
# 子进程报错让人去猜。三条路按从省事到麻烦排。
_NO_PRODUCT = (
    "找不到 JYBridge 源码目录（需要包含 jybridge/cli.py）。\n\n"
    "任选一种：\n"
    "  1) 在克隆下来的目录里运行  python -m jybridge.install\n"
    "  2) 设环境变量 JYBRIDGE_HOME 指向那个目录\n"
    "  3) 或用 pip 安装后重新运行一次 install\n\n"
    "装好后重启达芬奇。")

# 子进程一律按 utf-8 说话。不设的话 Windows 会按 cp936 写管道，面板再按
# utf-8 读 —— 草稿名就成了「10��30�� (5)」，报错里的中文也全是问号方块。
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

WIN_ID = "jybWin"
SLEEP_IDLE = 0.05
SLEEP_BUSY = 0.02
STATUS_TIMEOUT = 15          # 秒；旧版 25 秒在慢盘上会明显拖住面板


def note(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def human_bytes(n):
    """12345678 → 11.8 MB。

    面板跑在达芬奇的 Fusion 进程里，sys.path 上没有这个工程，所以不能
    `from jybridge import gc` —— 这份格式化函数就在本地留一份。
    """
    if n is None or n == "":
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return ("%d B" % n) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GB" % n


note("module loaded (pid=%d)" % os.getpid())


class Panel(object):
    """面板本体。构造顺序：状态 → 窗口 → 接线 → 首扫 → 进主循环。"""

    def __init__(self):
        # fusion / bmd 是达芬奇宿主注入的全局对象，脚本里没有 import 可写。
        self.ui = fusion.UIManager                     # noqa: F821
        self.disp = bmd.UIDispatcher(self.ui)          # noqa: F821

        self.closing = False
        self.job = None          # {"proc","out","label","action","t0","file"}
        self._lock_state = False
        self._has_draft = False
        # 缓存那一行的两个数：默认策略能删的、以及要额外一步才删的「可再生
        # 中间产物」。都来自 status 报的实测值，不是估算 —— 面板显示多少，
        # 按钮点下去就释放多少。两个都是 0 时按钮直接禁用。
        self._free_bytes = 0
        self._free_extra = 0
        # 「草稿打开之后又被编辑过」——来自 status 的两个文件时间，不扫内存。
        # 为真时点「送入」直接弹提示，连子进程都不启动（见 _on_send）。
        self._edited = False
        self._edit_text = ""
        self._open_text = ""
        # 结果弹窗（独立小窗，不是模态）：_dlg 是当前那个窗口对象，
        # _act 是弹窗上那个动作按钮（重试 / 清理）要执行的调用。
        self._dlg = None
        self._dlg_seq = 0
        self._act = None
        # 变速段落地方式固定 bake：达芬奇脚本接口不能设速度，只有把源区间
        # 按速度预渲染成等长素材，帧序/时长才和剪映一致。机器上没有 ffmpeg
        # 时 send 会自己退回 slot，并在报告里写明。
        self.speed_mode = "bake"

        # 后台状态查询（单飞：同时只有一个线程在问剪映）
        self._st = threading.Lock()
        self._st_running = False
        self._st_result = None
        self._st_err = None
        # 单飞查询进行中又有人要刷新：记下来，等这次结果贴完再补一次。
        # 不记的话，任务结束时的刷新会被「刚好还在跑」的旧查询吞掉，
        # 面板上的缓存/送去状态停在任务前。
        self._want_refresh = False

        self._close_existing()
        if not self._build_window():
            return
        self._wire()
        # 没扫到草稿时，① 和 ② 都不给点：这两个动作的前提就是「剪映里有一份
        # 打开的草稿」，让它点下去再报闸门失败是浪费一轮往返。
        self._sync_buttons()

        self.refresh_draft()
        self.win.Show()
        note("window shown")
        self.loop()

    # ---------- 构造分段 ----------
    def _close_existing(self):
        """同名旧窗口还在（重复点菜单）就先收掉，免得叠出两个。

        查窗要用 `disp.FindWindow`（UIDispatcher 上的方法），不是 `ui.FindWindow`
        —— fusion.UIManager 上没有这个方法，旧写法抛 AttributeError 被
        `except: return` 吞掉，等于这段自愈从来没执行过。两个面板同时跑同一个
        dispatcher 会把结果文件互相截断，还会抢事件循环。三个候选 ID 都要查：
        输入框不可用时会退回 jybWin_b / jybWin_c。
        """
        for wid in (WIN_ID, WIN_ID + "_b", WIN_ID + "_c"):
            existing = None
            for owner in (self.disp, self.ui):
                try:
                    existing = owner.FindWindow(wid)
                except Exception:
                    existing = None
                if existing:
                    break
            if not existing:
                continue
            for fn in ("Hide", "Close"):
                try:
                    getattr(existing, fn)()
                    break
                except Exception:
                    continue
            note("closed existing window %s" % wid)

    def _name_row(self):
        """新草稿名输入框。LineEdit 在部分 Fusion 版本上没有，拿不到就退化成
        一行提示（写回仍可用，只是走自动命名），绝不能让整个面板打不开。"""
        try:
            return self.ui.HGroup({"Weight": 0}, [
                self.ui.Label({"Text": "新草稿名", "Weight": 0}),
                self.ui.LineEdit({"ID": "txtName", "Text": "",
                                  "PlaceholderText": "留空＝自动命名（草稿名_调色）",
                                  "Weight": 1}),
            ])
        except Exception as exc:
            note("LineEdit unavailable: %r" % (exc,))
            try:
                return self.ui.Label(
                    {"Text": "新草稿名：本版本面板不支持输入，"
                             "用命令行 write --name 指定", "Weight": 0})
            except Exception:
                return None

    def _build_window(self):
        """画窗口。返回 False 表示三种尺寸都建不出来，只能放弃。"""
        name_row = self._name_row()
        rows = [
            self.ui.Label({"Text": "<b>剪映 ⇄ 达芬奇</b>（剪辑以剪映为准，达芬奇只调色）",
                           "Weight": 0}),
            self.ui.HGroup({"Weight": 0}, [
                self.ui.Label({"ID": "jybDraft", "Text": "当前草稿：—",
                               "Weight": 1}),
                self.ui.Button({"ID": "btnRefresh", "Text": "↻ 刷新",
                                "Weight": 0}),
            ]),
            self.ui.HGroup({"Weight": 0}, [
                self.ui.Button({"ID": "btnSend", "Text": "① 送入达芬奇", "Weight": 0.5}),
                self.ui.Button({"ID": "btnWrite",
                                "Text": "② 写回剪映（按送入那版）", "Weight": 0.5}),
            ]),
        ]
        if name_row is not None:
            rows.append(name_row)
        rows += [
            self.ui.HGroup({"Weight": 0}, [
                self.ui.Button({"ID": "btnLock", "Text": "锁定 / 解锁轨道",
                                "Weight": 1.0}),
            ]),
            # 缓存行长这样：`可清理：12.3 MB          [清理缓存]`
            # 数字＝按默认策略真会释放的量（删不掉的不算），0 时按钮禁用。
            self.ui.HGroup({"Weight": 0}, [
                self.ui.Label({"ID": "jybCache", "Text": "可清理：—", "Weight": 1}),
                self.ui.Button({"ID": "btnGc", "Text": "清理缓存", "Weight": 0}),
            ]),
            self.ui.Label({"ID": "jybInfo", "Text": "就绪", "Weight": 1,
                           "WordWrap": True}),
            # 任务结果单独占一行，不能塞进 jybInfo：任务结束后面板会立刻
            # refresh_draft()，状态回来会把 jybInfo 整段改写 —— 结果写在那里
            # 就等于「点了按钮什么都没发生」（2026-11-19 用户连点了 8 次）。
            self.ui.Label({"ID": "jybResult", "Text": "", "Weight": 0,
                           "WordWrap": True}),
        ]
        title = "JYBridge · 剪映 ↔ 达芬奇"
        self.win = self.disp.AddWindow(
            {"ID": WIN_ID, "Geometry": [180, 90, 580, 372],
             "WindowTitle": title}, self.ui.VGroup(rows))
        if self.win is None and name_row is not None:
            # LineEdit 在这个 Fusion 版本上不被接受时会整窗失败：去掉输入框重来，
            # 写回退化成自动命名（命令行 --name 仍可用），面板本身必须能开。
            note("AddWindow failed with LineEdit, retry without it")
            rest = [r for r in rows if r is not name_row]
            self.win = self.disp.AddWindow(
                {"ID": WIN_ID + "_b", "Geometry": [180, 90, 580, 342],
                 "WindowTitle": title}, self.ui.VGroup(rest))
        if self.win is None:
            note("AddWindow returned None, retry with fallback id")
            self.win = self.disp.AddWindow(
                {"ID": WIN_ID + "_c", "Geometry": [180, 90, 580, 342],
                 "WindowTitle": title}, self.ui.VGroup([]))
        if self.win is None:
            note("AddWindow failed twice, abort")
            return False
        self.items = self.win.GetItems()
        return True

    def _wire(self):
        """挂回调。窗口 ID 可能是 WIN_ID / _b / _c，谁在就挂谁。"""
        for wid in (WIN_ID, WIN_ID + "_b", WIN_ID + "_c"):
            try:
                getattr(self.win.On, wid).Close = self.on_close
                break
            except Exception:
                continue

        self.win.On.btnSend.Clicked = lambda _ev: self._on_send()
        self.win.On.btnWrite.Clicked = lambda _ev: self.start_job(
            "write", "② 写回剪映", extra=self._write_extra())
        self.win.On.btnLock.Clicked = lambda _ev: self.start_job(
            "lock", "切换轨道锁", extra=["off"] if self._lock_state else ["on"])
        self.win.On.btnRefresh.Clicked = lambda _ev: self.refresh_draft()
        self.win.On.btnGc.Clicked = lambda _ev: self.start_job(
            "gc", "清理缓存", extra=self._gc_extra())

    # ---------- 界面小工具 ----------
    def say_r(self, text):
        try:
            self.items["jybInfo"].Text = text
        except Exception:
            pass

    def say_result(self, text):
        """任务结果行。跟状态行分开，状态刷新不会把它冲掉。"""
        try:
            self.items["jybResult"].Text = text
        except Exception:
            pass

    def _new_name(self):
        """面板上填的新草稿名；没有输入框或留空就返回空串（走自动命名）。

        注意用的是 `items["txtName"]` 而不是 `.get()` —— Fusion 的 GetItems()
        返回的是它自己的 items 对象，不是 dict，没有 `.get` 方法。旧版这里
        一直静默失败，等于输入框从来没生效过。
        """
        try:
            w = self.items["txtName"]
        except Exception:
            return ""
        try:
            return (getattr(w, "Text", "") or "").strip()
        except Exception:
            return ""

    def _write_extra(self):
        n = self._new_name()
        return ["--name", n] if n else []

    # ---------- 按钮可用性 ----------
    def _set_enabled(self, wid, on):
        try:
            self.items[wid].Enabled = bool(on)
        except Exception:
            pass

    def _on_send(self):
        """点「送入」时先过一道本地闸门。

        草稿在打开之后被改过 → 直接弹窗说清楚并让人重开，不启动子进程。
        判据只有两个文件时间（draft_settings / .locked），几十毫秒就有结论；
        不这么做的话，用户点完要等 CLI 扫完内存（几秒到几十秒）才知道结果，
        中间还会先吃掉「达芬奇没开」之类的无关报错。
        """
        if self._edited:
            self._popup("草稿改过了，先重开", self._stale_body(),
                        act=self._go_send, act_label="我已重开，再试",
                        height=430)
            return
        self._go_send()

    def _go_send(self):
        self.start_job("send", "① 送入达芬奇", extra=["--speed", self.speed_mode])

    def _stale_body(self):
        """「打开后又被编辑过」的说明。三步处理必须给全 —— 只说不能送，
        用户下一步还是不知道干什么。"""
        when = ""
        if self._edit_text:
            when = ("（最后编辑 %s，打开时刻 %s）"
                    % (self._edit_text, self._open_text or "?"))
        return ("剪映里的这份草稿在打开之后又被改过%s。\n\n"
                "剪映只在「打开草稿」那一刻把内容读进内存，之后的编辑只往磁盘写。"
                "现在送进去的会是打开那一刻的版本，你后来的改动会被丢掉。\n\n"
                "处理：\n"
                "1  回到剪映首页（把这份草稿关掉）\n"
                "2  再双击重新打开它\n"
                "3  再点一次「① 送入达芬奇」\n\n"
                "只按 Ctrl+S 不够 —— 保存只是写盘，剪映不会因此重读内存。" % when)

    def _gc_extra(self):
        """清理按钮这次该带什么参数。

        默认策略还有东西可收就按默认收。若默认已是 0 项、面板上却写着
        「可再生 X」，就一次带到 --intermediate —— 用户看着这个数字按的
        按钮，再弹一层窗问他「要不要清可再生的」是多余的一步。
        runs（报告 + 底稿快照）不在 --intermediate 范围内，怎么按都动不到。
        """
        if self._free_bytes <= 0 and self._free_extra > 0:
            return ["--apply", "--intermediate"]
        return ["--apply"]

    def _sync_buttons(self):
        """按「有没有草稿 / 有没有任务在跑 / 有没有东西可清」决定按钮能不能点。

        规则：
        · 没有扫到草稿时 ① 和 ② 一律禁用 —— 它们的共同前提就是「剪映里有
          一份打开的草稿」，让用户点下去再吃闸门报错是白跑一趟。
        · 缓存确实一项都收不掉时，「清理缓存」也禁用。面板已经把数字报成
          0 了，再让它能点只会换来一次「点了没反应」。
        """
        busy = self.job is not None
        can_free = (self._free_bytes + self._free_extra) > 0
        self._set_enabled("btnSend", self._has_draft and not busy)
        self._set_enabled("btnWrite", self._has_draft and not busy)
        self._set_enabled("btnRefresh", not busy)
        self._set_enabled("btnLock", not busy)
        self._set_enabled("btnGc", not busy and can_free)

    # ---------- 状态（后台线程，绝不阻塞 StepLoop） ----------
    def _status_worker(self):
        rep, err = {}, None
        if not PRODUCT:
            err = _NO_PRODUCT
        else:
            try:
                r = subprocess.run(
                    [PY, "-m", "jybridge.cli", "status"], cwd=PRODUCT,
                    capture_output=True, text=True, timeout=STATUS_TIMEOUT,
                    creationflags=NO_WIN, encoding="utf-8", errors="replace",
                    env=ENV)
                txt = r.stdout or ""
                cut = txt.rfind("}")
                rep = json.loads(txt[:cut + 1]) if cut >= 0 else {}
            except Exception as exc:
                err = "%s: %s" % (type(exc).__name__, exc)
        with self._st:
            self._st_result = rep
            self._st_err = err
            self._st_running = False

    def refresh_draft(self):
        """触发一次后台状态查询（面板上的 ↻ 刷新也走这里）。

        已有查询在跑时不并发起第二个，但会记一个「待刷新」—— 否则任务结束时
        这次刷新就丢了，面板一直显示任务前的缓存数字和上次送入时间。
        """
        with self._st:
            if self._st_running:
                self._want_refresh = True
                return
            self._st_running = True
            self._st_result = None
            self._st_err = None
        self._set_enabled("btnRefresh", False)
        try:
            t = threading.Thread(target=self._status_worker, daemon=True)
            t.start()
        except Exception as exc:
            note("status thread failed: %r" % (exc,))
            with self._st:
                self._st_running = False
            self._sync_buttons()

    def apply_status(self):
        """主循环里把后台结果贴到界面上（没有新结果就什么都不做）。"""
        with self._st:
            if self._st_running:
                return
            rep, err = self._st_result, self._st_err
            self._st_result = self._st_err = None
        if rep is None and err is None:
            return
        try:
            self._apply(rep, err)
        finally:
            self._sync_buttons()
        with self._st:
            again, self._want_refresh = self._want_refresh, False
        if again:
            self.refresh_draft()

    def _cache_line(self, rep):
        """缓存行文案：只报「真能删掉的量」，不报目录总占用。

        这里踩过坑。原来显示的是整个 %LOCALAPPDATA%\\JYBridge 的大小，而里面
        大头是**按设计就不能删**的东西：runs 里写回要用的底稿快照、当前草稿
        的 cube、最近几份草稿的痕迹。于是面板写着「缓存 253 MB」，点清理却
        0 项、数字纹丝不动，用户只能认定按钮坏了。现在显示的每一笔都是按钮
        点下去真会消失的量，删不掉的干脆不提。

        两笔分开报：
          可清理       默认策略（剪映里已删的 / 超 30 天 / 每池留 5 份）会收的
          可再生       变速预渲染与 cube 工作副本，清掉下次送入自动重生成，
                       要额外走一次 --intermediate，所以单独标出来
        """
        free = rep.get("cache_free_bytes")
        extra = rep.get("cache_extra_bytes")
        if free is None and extra is None:
            self._free_bytes = self._free_extra = 0
            return "可清理：—"
        free, extra = int(free or 0), int(extra or 0)
        self._free_bytes, self._free_extra = free, extra
        if not free and not extra:
            return "可清理：无"
        if not free:
            return "可清理：可再生 %s" % human_bytes(extra)
        if not extra:
            return "可清理：%s" % human_bytes(free)
        return "可清理：%s ＋ 可再生 %s" % (human_bytes(free), human_bytes(extra))

    def _apply(self, rep, err):
        if not rep:
            # 配置类错误（找不到源码目录）要原样显示 —— 它本身就是修复步骤，
            # 塞进「详见日志」等于让用户去翻一个他不知道为什么存在的文件。
            if err and err.startswith("找不到 JYBridge"):
                self.say_r(err)
            else:
                self.say_r("读取草稿状态失败（详见 %s）" % LOG)
            if err:
                note("status failed: %s" % err)
            self._has_draft = False
            self._free_bytes = self._free_extra = 0
            self._edited = False
            try:
                self.items["jybDraft"].Text = "当前草稿：—"
                self.items["jybCache"].Text = "可清理：—"
            except Exception:
                pass
            return
        name = rep.get("draft") or ""
        jy = rep.get("jianying") or {}
        alive = bool(jy.get("processes"))
        opened = bool(rep.get("opened"))
        # 「扫到草稿」= 剪映进程里认出了当前工程。没打开（没有新鲜 .locked）
        # 也算扫到 —— 这种情况要让人看见名字，再用下面的提示催他重新打开。
        self._has_draft = bool(name)
        self._edited = bool(rep.get("draft_edited"))
        self._edit_text = rep.get("draft_edit_text") or ""
        self._open_text = rep.get("draft_opened_text") or ""
        try:
            self.items["jybDraft"].Text = (
                "当前草稿：%s%s" % (name, " · 已改" if self._edited else "")
                if name else "当前草稿：—")
        except Exception:
            pass
        try:
            self.items["jybCache"].Text = self._cache_line(rep)
        except Exception:
            pass
        if not alive:
            msg = "剪映没在运行。送入前请先启动剪映并打开目标草稿。"
        elif not name:
            msg = "剪映在运行，但没认出当前草稿：请在剪映里打开一份草稿，再点 ↻ 刷新。"
        elif not opened:
            msg = ("草稿「%s」：没检测到 .locked（可能已回到首页）。"
                   "送入前请在剪映里重新打开它。" % name)
        elif self._edited:
            msg = ("草稿「%s」：打开之后又被编辑过（%s）。送入前请关掉重开一次 ——"
                   "剪映内存里还是打开那一刻的版本。" % (name, self._edit_text or "时间未知"))
        else:
            msg = "草稿「%s」：已打开 · 就绪" % name

        last = rep.get("last_send") or {}
        if last:
            state = "已提交" if last.get("committed") else "未走完"
            msg += ("\n上次送入：%s（%s，%s 段，%s）"
                    % (last.get("draft") or "?", last.get("at") or "?",
                       last.get("segments") or 0, state))
        self.say_r(msg)
        note("status: draft=%s alive=%s opened=%s last=%s"
             % (name or "-", alive, opened, last.get("at")))

    # ---------- 子进程任务 ----------
    def start_job(self, action, label, extra=None):
        if not PRODUCT:
            self.say_r(_NO_PRODUCT)
            return
        if self.job and self.job["proc"].poll() is None:
            self.say_r("正在执行「%s」，等它结束或关窗口。"
                       % self.job.get("label"))
            return
        os.makedirs(OUT, exist_ok=True)
        out = os.path.join(OUT, "_jyb_%s.txt" % action)
        try:
            f = open(out, "w", encoding="utf-8")
        except Exception as exc:
            self.say_r("无法写结果文件：%s" % exc)
            return
        cmd = [PY, "-m", "jybridge.cli", action] + list(extra or [])
        try:
            proc = subprocess.Popen(cmd, cwd=PRODUCT, stdout=f,
                                    stderr=subprocess.STDOUT,
                                    creationflags=NO_WIN, env=ENV)
        except Exception as exc:
            f.close()
            self.say_r("启动失败：%s" % exc)
            return
        self.job = {"proc": proc, "out": out, "label": label,
                    "action": action, "file": f, "t0": time.time(),
                    "extra": list(extra or []),
                    "next_lock": (not self._lock_state) if action == "lock" else None}
        self.say_result("%s：执行中…" % label)
        self._sync_buttons()
        note("job start %s pid=%s" % (action, proc.pid))

    def poll_job(self):
        j = self.job
        if not j:
            return
        if j["proc"].poll() is None:
            el = time.time() - j["t0"]
            if el > 1.0:
                self.say_result("%s：执行中… 已用 %.0f 秒（可关窗口中止）"
                                % (j["label"], el))
            return
        try:
            j["file"].close()
        except Exception:
            pass
        txt = ""
        try:
            with open(j["out"], encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except Exception:
            pass
        code = j["proc"].returncode
        rep = self._parse(txt)
        # CLI 用退出码 + ok 双判：闸门失败时退出码 4，校验不过时退出码 1，
        # 两种情况 ok 都是 false。只有两边都过才算成功。
        ok = (code == 0) and bool(rep.get("ok", True))
        self.job = None
        note("job end %s code=%s ok=%s" % (j["action"], code, ok))
        # 只有真的切成功才翻本地镜像。原来无条件翻：锁定失败后按
        # 「已锁定」发下一次，用户得点两下才解锁，而且面板显示的是反的。
        if ok and j["action"] == "lock" and j.get("next_lock") is not None:
            self._lock_state = j["next_lock"]
        if ok:
            msg = self._summary(j["action"], rep)
            notes = self._notes(txt)
            if notes:
                msg += "\n" + "\n".join(notes)
            self.say_result(msg)
            if j["action"] == "gc" and not rep.get("count"):
                # 默认策略没东西可收时，给一条「那就清可再生的」的出路，
                # 否则用户只会看到「0 项」，以为按钮坏了。
                self._popup_gc_empty(rep)
        else:
            self.say_result("%s：没有完成（详见弹窗）" % j["label"])
            self._popup_fail(j, rep, txt, code)
        self._sync_buttons()
        self.refresh_draft()

    # ---------- 结果解析 ----------
    @staticmethod
    def _parse(txt):
        """取子进程输出里最后一个完整 JSON 对象。

        CLI 会先打几行进度（「闸门通过：…」「· 不送：文本 1」），成功时最后
        才是完整报告，失败时是一段 `{ok:false, gate, error}`。用 raw_decode
        逐个试比按行切稳 —— error 字段里本身就带换行和引号。
        """
        dec = json.JSONDecoder()
        best, i = {}, 0
        txt = txt or ""
        while True:
            i = txt.find("{", i)
            if i < 0:
                break
            try:
                obj, end = dec.raw_decode(txt[i:])
            except ValueError:
                i += 1
                continue
            if isinstance(obj, dict):
                best = obj
            i += max(1, end)
        return best

    @staticmethod
    def _summary(action, rep):
        """成功时面板上那一行。只挑用户真正关心的数字。"""
        if action == "send":
            n = rep.get("placed") or 0
            msg = "送入完成：%d 段" % n
            ex = rep.get("exact")
            if ex is not None:
                msg += "（精确对齐 %d 段）" % ex
            if rep.get("timeline"):
                msg += " · 时间线「%s」" % rep["timeline"]
            sm = rep.get("speed_mode")
            if sm == "bake":
                msg += "\n变速段已按速度预渲染"
            elif rep.get("speed_fallback"):
                msg += "\n变速段退回近似摆放（缺 ffmpeg）"
            if rep.get("disabled"):
                msg += "\n停用段 %d 段已按停用送入（占位保留、不出画面）" % rep["disabled"]
            return msg
        if action == "write":
            name = rep.get("draft_name") or "新草稿"
            n = rep.get("luts") or rep.get("segments") or 0
            msg = "写回完成：新草稿「%s」，%d 段 LUT 已嵌入" % (name, n)
            kept = rep.get("kept") or {}
            bits = []
            for key, label, unit in (("speed_segments", "变速", "段"),
                                     ("transitions", "转场", "个"),
                                     ("fade_segments", "淡入淡出", "段"),
                                     ("masks", "蒙版", "个"),
                                     ("keyframe_segments", "关键帧", "段"),
                                     ("overlay_tracks", "画中画", "轨"),
                                     ("compounds", "复合片段", "个"),
                                     ("effects", "特效", "个")):
                v = kept.get(key) or 0
                if v:
                    bits.append("%s %d %s" % (label, v, unit))
            if bits:
                msg += "\n原样保留：" + " · ".join(bits)
            if rep.get("alignment_ok") is False:
                msg += "\n注意：对齐校验未通过，建议在剪映里抽查"
            return msg
        if action == "lock":
            if str(rep.get("state")) == "on":
                return "轨道已锁定：%d 条（调色不受影响）" % (rep.get("locked") or 0)
            return "轨道已解锁：现在可以改剪辑，改完要重新送入"
        if action == "gc":
            n = rep.get("count") or 0
            if not n:
                # 别只说「0 项」：用户看不出这个词的意思是「确实没东西可清」
                # 还是「按钮坏了」。说清楚还剩多少在正常使用。
                kept = rep.get("kept_bytes") or rep.get("before_bytes")
                msg = "没有可回收的缓存（%s 是写回要用的快照和当前草稿）" % human_bytes(kept)
                alt = rep.get("intermediate_bytes") or 0
                if alt:
                    msg += "\n可再生中间产物还能清 %s" % human_bytes(alt)
                return msg
            msg = "缓存清理完成：%d 项，释放 %s" % (n, human_bytes(rep.get("freed_bytes")))
            kept = rep.get("kept_bytes")
            if kept:
                msg += "，剩 %s 仍在使用" % human_bytes(kept)
            if rep.get("intermediate"):
                msg += "\n（只清了变速预渲染与 cube 工作副本，报告和快照保留）"
            return msg
        return "完成"

    @staticmethod
    def _notes(txt, limit=2):
        """成功摘要下面最多附两行 —— 「达芬奇里比剪映少了什么」。

        CLI 会打「蒙版：2 段不送」「不送：文本 1、贴纸 1」「画面变换：…」。
        以前这些和整份 JSON 糊在一起，现在既然不显示报告了，也不能把它们
        一起丢掉：用户就是靠这两行知道这次送进去的片子缺了哪部分。
        """
        keys = ("不送：", "蒙版：", "画面变换：", "!! ")
        out = []
        for line in (txt or "").splitlines():
            s = line.strip()
            if s.startswith(keys):
                out.append(s if len(s) <= 90 else s[:88] + "…")
        return out[:limit]

    @staticmethod
    def _fail_text(action, rep, txt, code):
        """弹窗正文。stale_draft 单独写，因为它的处理办法和别的不一样。"""
        gate = str(rep.get("gate") or "")
        err = str(rep.get("error") or "").strip()
        if gate == "stale_draft":
            # 后端已经在 error 里写了「最后编辑 23:47:41 / 打开 23:38:25」这种
            # 具体时间，别丢掉 —— 用户没法反驳一个说不出所以然的提示。三步
            # 处理固定跟在后面，因为不管哪种原因，解法都是重开草稿。
            head = ("剪映里的这份草稿在打开之后被改过。\n\n"
                    "内存里留着的是「打开那一刻」的版本，磁盘上已经是新的了。"
                    "照旧版送进去会把你刚做的改动悄悄丢掉，所以这里停住了。\n\n")
            if err:
                head = err + "\n\n"
            return (head +
                    "处理：\n"
                    "1  把这份草稿关掉（回到剪映首页）\n"
                    "2  再双击重新打开它\n"
                    "3  点下面的「重试」\n\n"
                    "只按 Ctrl+S 不够 —— 保存只是写盘，剪映不会因此重读内存。")
        if gate == "no_plaintext":
            # 草稿认得出来，但明文拿不到。最常见的原因是「开着放了太久」：
            # 剪映会把自己留在内存里的明文回收掉，跟用户改没改过毫无关系。
            # 旧提示写「内存与磁盘对不上」，等于暗示用户改了草稿 —— 他明明
            # 什么都没动。这里只讲机制和下一步。
            return ("剪映内存里已经没有这份草稿的完整明文了。\n\n"
                    "它只在刚打开草稿那段时间里把明文留在内存，放久了会自己回收"
                    "（这次是打开很久之后才点的送入）。这跟你有没有改过草稿无关，"
                    "内存里通常只剩半截残片，所以读不出时间线。\n\n"
                    "处理：\n"
                    "1  回到剪映首页，把这份草稿关掉\n"
                    "2  再双击重新打开它\n"
                    "3  重开后尽快点一次「① 送入达芬奇」，再按下面的「重试」\n\n"
                    "成功送一次之后工具会存一份本地副本；只要这份草稿的磁盘内容"
                    "没再变，以后放多久都能直接送。")
        if err:
            return err[:900]
        probs = rep.get("problems") or []
        if probs:
            return "校验没通过：\n" + "\n".join("· %s" % p for p in probs[:8])
        tail = [s for s in (txt or "").strip().splitlines() if s.strip()][-3:]
        if tail:
            return "\n".join(tail)[:900]
        return ("命令没有成功（退出码 %s），而且没有留下可读原因。\n"
                "完整日志：%s" % (code, LOG))

    # ---------- 失败弹窗 ----------
    def _close_popup(self):
        w = self._dlg
        self._dlg = None
        if not w:
            return
        for fn in ("Hide", "Close"):
            try:
                getattr(w, fn)()
                return
            except Exception:
                continue

    def _run_act(self):
        """弹窗上那个动作按钮：先关窗再执行，免得回调里又弹一个叠上来。"""
        fn = self._act
        self._act = None
        self._close_popup()
        if callable(fn):
            fn()

    def _popup(self, title, body, act=None, act_label="重试", height=330):
        """结果弹窗（失败 / 需要用户再决定一步）。

        不用模态：Fusion 的 UIDispatcher 没有模态 API，在这里再起一层
        StepLoop 会和主循环抢事件。开个独立小窗就够 —— 主循环照常
        StepLoop，窗口自己响应点击。

        act 是一个无参可调用对象；给了才亮那个按钮，否则按钮禁用。
        """
        self._close_popup()
        self._dlg_seq += 1
        wid = "jybDlg%d" % self._dlg_seq
        self._act = act
        try:
            dlg = self.disp.AddWindow(
                {"ID": wid, "Geometry": [280, 190, 620, height],
                 "WindowTitle": "JYBridge · " + title},
                self.ui.VGroup([
                    self.ui.Label({"ID": "dMsg", "Text": body, "WordWrap": True,
                                   "Weight": 1}),
                    self.ui.HGroup({"Weight": 0}, [
                        self.ui.Label({"ID": "dHint",
                                       "Text": "完整日志：%s" % LOG,
                                       "Weight": 1, "WordWrap": True}),
                        self.ui.Button({"ID": "dAct", "Text": act_label,
                                        "Weight": 0}),
                        self.ui.Button({"ID": "dOk", "Text": "知道了", "Weight": 0}),
                    ]),
                ]))
        except Exception as exc:
            note("popup failed: %r" % (exc,))
            return False
        if dlg is None:
            note("popup AddWindow returned None")
            return False
        try:
            dlg.On.dOk.Clicked = lambda _ev: self._close_popup()
            dlg.On.dAct.Clicked = lambda _ev: self._run_act()
            getattr(dlg.On, wid).Close = lambda _ev: self._close_popup()
        except Exception:
            note("popup wiring failed:\n" + traceback.format_exc())
        if not callable(act):
            try:
                dlg.GetItems()["dAct"].Enabled = False
            except Exception:
                pass
        self._dlg = dlg
        try:
            dlg.Show()
        except Exception as exc:
            note("popup show failed: %r" % (exc,))
            return False
        return True

    def _popup_fail(self, j, rep, txt, code):
        titles = {"send": "送入失败", "write": "写回失败",
                  "lock": "锁定失败", "gc": "清理失败"}
        title = titles.get(j["action"], "操作失败")
        body = self._fail_text(j["action"], rep, txt, code)
        action, label, extra = j["action"], j["label"], j.get("extra") or []
        ok = self._popup(title, body, act_label="重试",
                         act=lambda: self.start_job(action, label, extra=extra))
        if not ok:
            # 弹窗建不出来也不能什么都不说
            self.say_result("%s失败：%s" % (j["label"], body.splitlines()[0][:80]))

    def _popup_gc_empty(self, rep):
        """缓存清理没东西可收 —— 说明为什么，并给「清可再生的」这条出路。

        正常情况下走不到这里：面板上写着 0 时按钮已经禁用，写着「可再生 X」
        时按钮直接带 --intermediate。只剩一种情形 —— 面板上的数字过期了
        （比如刚自动回收过），点下去才发现已经没得清。
        """
        size = human_bytes(rep.get("kept_bytes") or rep.get("before_bytes"))
        alt = rep.get("intermediate_bytes") or 0
        body = ("%s\n\n普通清理只收「剪映里已经删掉的草稿 / 超过 30 天 / "
                "每个池只留最近 5 份」的缓存。剩下这 %s 全是还在用的："
                "写回要用的底稿快照、当前草稿的 cube、最近几份草稿的痕迹，"
                "所以一项都没动。"
                % (rep.get("note") or "没有可回收的缓存。", size))
        if alt:
            body += ("\n\n要现在就腾空间，可以清掉「可再生中间产物」：\n"
                     "· 变速段预渲染的 .mov（retime\\<草稿ID>\\）\n"
                     "· 写回时导出的 cube 工作副本（luts\\<草稿ID>\\）\n"
                     "约 %s。报告和底稿快照一律保留（写回还要用），"
                     "这些文件下次送入时会自动重新生成，变速段会多渲染一次。"
                     % human_bytes(alt))
            self._popup("缓存没有可回收项", body, act_label="清理可再生缓存",
                        act=lambda: self.start_job(
                            "gc", "清理可再生缓存",
                            extra=["--apply", "--intermediate"]))
        else:
            self._popup("缓存没有可回收项", body)

    # ---------- 主循环 ----------
    def loop(self):
        pump_ok, pump_fail = True, 0
        while not self.closing:
            if pump_ok:
                try:
                    self.disp.StepLoop()
                    pump_fail = 0
                except Exception as exc:
                    pump_fail += 1
                    if pump_fail in (1, 10, 50):
                        note("StepLoop failed x%d: %r" % (pump_fail, exc))
                    if pump_fail >= 50:
                        # 事件循环连续失败 50 次（约 1 秒）说明面板已经收不到
                        # 任何事件了。继续 while 只是 20Hz 空转，窗口既不能用
                        # 也关不掉（on_close 永远等不到），用户只能重启达芬奇。
                        # 明确退出循环并留一句话在状态行。
                        note("StepLoop dead after %d failures" % pump_fail)
                        try:
                            self.say_r("面板已失去响应，请关掉这个窗口重新打开面板。")
                        except Exception:
                            pass
                        self.closing = True
                        pump_ok = False
                        break
            try:
                self.apply_status()
                self.poll_job()
            except Exception:
                note("step error:\n" + traceback.format_exc())
                self.say_r("处理出错，详见 %s" % LOG)
            time.sleep(SLEEP_BUSY if self.job else SLEEP_IDLE)

    # ---------- 生命周期 ----------
    def on_close(self, _ev):
        self.closing = True
        self._close_popup()
        try:
            if self.job:
                subprocess.run(["taskkill", "/F", "/T", "/PID",
                                str(self.job["proc"].pid)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=NO_WIN)
        except Exception:
            pass
        try:
            self.win.Hide()
        except Exception:
            pass


def main():
    return Panel()


try:
    main()
except SystemExit:
    pass
except Exception:
    note("panel error:\n" + traceback.format_exc())
    raise
