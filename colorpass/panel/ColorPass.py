"""ColorPass · 剪映 ⇄ 达芬奇 往返（精简版）

只做一件事：**把剪映的剪辑送进达芬奇，调完色再写回一份新草稿。**

面板上只有两个动作：
  送入达芬奇   —— 剪映当前草稿 → 达芬奇时间线（逐段按帧号摆放）
  写回剪映     —— 达芬奇逐段 ExportLUT → 克隆出新的剪映草稿

写回读的是「上一次成功送入」的快照，所以没送入过时它和草稿名输入框都是灰的。
页脚只剩一个「诊断」：锁定轨道、清理缓存只是命令行能力（colorpass lock / gc），
手动刷新改由面板自己定时做（见 AUTO_REFRESH）。

参考仿色、非常规偏色、一级调色三个功能已从本插件移除（2026-11-19）。
它们的代码移到了 `_stripped/`，面板不再加载，也不再依赖 `drx/PrimaryBalance.py`。

线程模型（沿用实测结论，别改）：
  Fusion 的 UIDispatcher 没有 AddTimer，RunLoop 还会抢 GIL，所以面板只能
  单线程 StepLoop 轮询；真正的活在子进程里干（`python -m colorpass.cli ...`），
  面板只负责起进程、读它写出来的结果文件。
  `cli status` 也放后台线程跑 —— 剪映内存读不出来时它会挂到超时，
  放主循环里就是整窗卡死（旧版实测过）。

安装：
    python -m colorpass.install
"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------- 装在哪、用哪个解释器
#
# 这两个值以前是写死的绝对路径 —— 只有作者本机能跑。开源后必须自动定位：
#
#   python    COLORPASS_PYTHON → config.json 的 python → 常见安装位置（取版本最高）
#             → PATH 上的 python
#   product   有 colorpass/cli.py 的目录：COLORPASS_HOME → config.json 的 product
#             → <数据目录>/product → 当前目录
#
# config.json 由 `python -m colorpass.install` 写，里面记着「装的时候用的是谁」。
# 这样 pip 安装、克隆源码、绿色解压三种情况都能对上，不用改代码。
#
# 数据目录跟 CLI 的 paths.py 保持同一套规则（Windows %LOCALAPPDATA%，mac
# ~/Library/Application Support）—— 两边算出来不一样的话，面板就找不到
# install 写下的 config.json，表现是「装了却还说找不到源码目录」。


def _data_dir():
    env = os.environ.get("COLORPASS_DATA")
    if env:
        return env
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "ColorPass")
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("LOCALAPPDATA") or home, "ColorPass")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or
                        os.path.join(home, ".local", "share"), "ColorPass")


BASE_DIR = _data_dir()
CONF_PATH = os.path.join(BASE_DIR, "config.json")


def _conf():
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _is_product(p):
    return bool(p) and os.path.isfile(os.path.join(p, "colorpass", "cli.py"))


def _find_product():
    conf = _conf()
    cands = [os.environ.get("COLORPASS_HOME"), conf.get("product"),
             os.path.join(BASE_DIR, "product"), os.getcwd()]
    for c in cands:
        if _is_product(c):
            return os.path.abspath(c)
    return ""


def _find_cli():
    """打包安装时，install 会把 ColorPass.exe 的路径写进 config.json。

    有它就不需要本机装 Python：子进程直接调 exe。源码模式没有这一项，
    照旧走 `python -m colorpass.cli`。
    """
    conf = _conf()
    for c in (os.environ.get("COLORPASS_CLI"), conf.get("cli")):
        if c and os.path.isfile(c):
            return c
    return ""


def _ver_key(path):
    m = re.search(r"[Pp]ython(\d)(\d+)", path.replace("\\", "/"))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _find_python():
    conf = _conf()
    for c in (os.environ.get("COLORPASS_PYTHON"), conf.get("python")):
        if c and os.path.isfile(c):
            return c
    if sys.platform == "darwin":
        # Homebrew(Apple Silicon / Intel)、python.org 安装包、系统自带
        direct = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/usr/bin/python3"]
        found = [p for p in direct if os.path.isfile(p)]
        if found:
            return found[0]      # 顺序即优先级：homebrew > 官方包 > 系统
        return "python3"
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
CLI = _find_cli()


def _cmd(action, extra=None):
    """子进程该跑什么：(命令, 工作目录)。

    打包模式优先 —— 用户机器上没装 Python 也能用；源码模式给开发用。
    """
    if CLI:
        return [CLI, action] + list(extra or []), (PRODUCT or None)
    return [PY, "-m", "colorpass.cli", action] + list(extra or []), PRODUCT


def _no_backend():
    return not PRODUCT and not CLI
# 面板自己的日志与子进程输出放在 %LOCALAPPDATA%\ColorPass\panel\
# 不再借用 drx\generated\ —— 那个目录属于已移除的一级调色插件。
OUT = os.path.join(BASE_DIR, "panel")
LOG = os.path.join(OUT, "colorpass_panel.log")
# CREATE_NO_WINDOW 只在 Windows 上认；POSIX 传非零会 ValueError
NO_WIN = 0x08000000 if sys.platform.startswith("win") else 0

# 找不到源码目录时，面板唯一的正确动作是「说清楚怎么修」，而不是弹一个
# 子进程报错让人去猜。三条路按从省事到麻烦排。
_NO_PRODUCT = (
    "找不到 ColorPass 的运行入口（源码目录或打包好的 ColorPass.exe）。\n\n"
    "任选一种：\n"
    "  1) 双击安装程序 ColorPass-Setup.exe 重新安装\n"
    "  2) 在克隆下来的目录里运行  python -m colorpass.install\n"
    "  3) 设环境变量 COLORPASS_HOME 指向源码目录\n\n"
    "装好后重启达芬奇。")

# 子进程一律按 utf-8 说话。不设的话 Windows 会按 cp936 写管道，面板再按
# utf-8 读 —— 草稿名就成了「10��30�� (5)」，报错里的中文也全是问号方块。
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

WIN_ID = "jybWin"
WIN_W, WIN_H = 460, 456
SLEEP_IDLE = 0.05
SLEEP_BUSY = 0.02
STATUS_TIMEOUT = 15          # 秒；旧版 25 秒在慢盘上会明显拖住面板

# 没有「刷新」按钮了，面板自己看：空闲时每 10 秒问一次 status。为什么不是
# 更密 —— 每次都是一整个子进程（源码模式约 1 秒），追太紧等于后台常驻一个核。
# 一次查询超过 SLOW_SCAN 秒说明这台机器读剪映内存慢，退到 30 秒，别越拖越堆。
AUTO_REFRESH = 10.0
AUTO_REFRESH_SLOW = 30.0
SLOW_SCAN = 2.5


def note(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def elide(text, n):
    """长草稿名截断加省略号 —— 面板只有一行给它，剪到半个字更像坏了。"""
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def short_stamp(s):
    """「2026-11-19 23:47:41」→「11-19 23:47」。

    面板上只有一行位置放「上次送入」，年份和秒在这里都是噪音。
    """
    m = re.search(r"(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", str(s or ""))
    return "%s-%s %s:%s" % m.groups() if m else ""


note("module loaded (pid=%d)" % os.getpid())


# ---------------------------------------------------------------- 外观
#
# 面板跑在达芬奇的 Fusion 进程里，能用的只有 Qt 控件 + Qt 样式表。所以「质感」
# 只能靠三件事做出来：**字号层级、留白节奏、一个强调色**。阴影、毛玻璃、动效
# 这里都没有 —— 在这上面花力气只会换来各版本表现不一致。
#
# 定下来的规矩（改的时候别破坏）：
#   · 只有一个强调色，只给「送入达芬奇」和「已送入 / 执行中」。其余一律灰阶。
#   · 字号按角色定，一个数字只干一件事：
#       F_NAME  18  草稿名 —— 这一屏唯一的主角
#       F_TITLE 16  产品名 —— 比草稿名小一号，用户关心的不是产品叫什么
#       F_BTN   13  按钮
#       F_BODY  12  状态说明 / 结果 / 输入框
#       F_NOTE  11  小节标题 / 副标题 / 页脚
#   · 间距一律 4 的倍数，窗口左右各留 20。
#   · 分隔只用 1px 细线，不堆边框、不堆卡片底色。
#   · 字体用系统中文字体。指定拉丁字体会让中英混排一高一低。
#   · 一个动作只出现一次。常驻按钮越少越好：页脚只有「诊断」，锁定 / 清理 /
#     刷新都不在面板上 —— 挤在同一行的次要按钮会把主按钮的份量稀释掉。


def _font_family():
    """系统中文字体。Qt 的 font-family 只认第一个能命中的，所以给一个就够。"""
    if sys.platform == "darwin":
        return "PingFang SC"
    if sys.platform.startswith("win"):
        return "Microsoft YaHei UI"
    return "Noto Sans CJK SC"


FONT = _font_family()

C_ACCENT = "#0A84FF"        # 唯一强调色（系统蓝）
C_TEXT = "#F2F2F7"          # 主文字
C_SUB = "#9A9AA0"           # 次要文字
C_DIM = "#6E6E73"           # 说明 / 占位
C_LINE = "#3A3A3C"          # 分隔线
C_FIELD = "#2C2C2E"         # 次按钮底色 / 输入框
C_BG = "#1C1C1E"            # 面板底色（钉死深色，宿主换主题也不跑版）
C_OK = "#30D158"
C_WARN = "#FF9F0A"
C_ERR = "#FF453A"

GAP = 4                     # 所有间距都是它的倍数

F_NAME = 18                 # 草稿名
F_TITLE = 16                # 产品名
F_BTN = 13                  # 按钮
F_BODY = 12                 # 状态说明 / 结果 / 输入
F_NOTE = 11                 # 注释

# 输入框里那行灰字。它替面板解释了「为什么现在填不了」，比一句状态说明省地方。
PH_WAIT = "先送入达芬奇，才能写回"
PH_READY = "留空＝自动命名「草稿名_调色」"


def _qss(body):
    return "font-family:%s;%s" % (FONT, body)


def _text_qss(size, color, weight=0):
    body = "color:%s;font-size:%spx;background:transparent;" % (color, size)
    if weight:
        body += "font-weight:%d;" % weight
    return _qss(body)


def _btn_qss(kind, enabled=True):
    """按钮样式。禁用态直接换一套样式 —— 不靠 `:disabled` 选择器。

    为什么不用伪状态：Fusion 的元素不一定就是 QPushButton 类，类型选择器可能
    根本不命中；属性块一定能命中。代价是禁用时要自己换样式，见 _set_enabled。
    """
    if not enabled:
        return _qss("color:%s;background-color:transparent;border:1px solid %s;"
                    "border-radius:9px;font-size:%spx;"
                    % (C_DIM, C_LINE, F_BTN))
    if kind == "primary":
        return _qss("color:#FFFFFF;background-color:%s;border:0px;"
                    "border-radius:10px;font-size:%spx;font-weight:600;"
                    "padding:0 16px;" % (C_ACCENT, F_BTN))
    if kind == "secondary":
        return _qss("color:%s;background-color:%s;border:0px;border-radius:9px;"
                    "font-size:%spx;padding:0 16px;" % (C_TEXT, C_FIELD, F_BTN))
    # ghost：页脚那一个「诊断」，压到最小存在感
    return _qss("color:%s;background-color:transparent;border:0px;border-radius:7px;"
                "font-size:%spx;padding:0 8px;" % (C_SUB, F_NOTE))


def _input_qss(enabled=True):
    """输入框。不能填的时候底色退回面板色、字变暗 —— 不用读说明也知道点不动。"""
    if not enabled:
        return _qss("color:%s;background-color:%s;border:1px solid %s;"
                    "border-radius:8px;padding:0 10px;font-size:%spx;"
                    % (C_DIM, C_BG, C_LINE, F_BODY))
    return _qss("color:%s;background-color:%s;border:1px solid %s;"
                "border-radius:8px;padding:0 10px;font-size:%spx;"
                "selection-background-color:%s;"
                % (C_TEXT, C_FIELD, C_LINE, F_BODY, C_ACCENT))


class Panel(object):
    """面板本体。构造顺序：状态 → 窗口 → 接线 → 首扫 → 进主循环。"""

    def __init__(self):
        # fusion / bmd 是达芬奇宿主注入的全局对象，脚本里没有 import 可写。
        self.ui = fusion.UIManager                     # noqa: F821
        self.disp = bmd.UIDispatcher(self.ui)          # noqa: F821

        self.closing = False
        self.job = None          # {"proc","out","label","action","t0","file"}
        self._has_draft = False
        self._draft_name = ""
        # 能不能写回 / 能不能填新名字。判据只有一个：**这份草稿成功送入过**。
        # 写回读的是送入那一刻的快照，没送过就没有东西可写回 —— 这时候让按钮
        # 亮着、点下去再弹一个「还没有完成过一次送入」，是拿报错当提示。
        self._can_write = False
        self._sent_name = ""      # 本会话里送入成功的草稿名（status 的兜底）
        # 「草稿打开之后又被编辑过」——来自 status 的两个文件时间，不扫内存。
        # 为真时点「送入」直接弹提示，连子进程都不启动（见 _on_send）。
        self._edited = False
        self._edit_text = ""
        self._open_text = ""
        # 结果弹窗（独立小窗，不是模态）：_dlg 是当前那个窗口对象，
        # _act 是弹窗上那个动作按钮（重试）要执行的调用。
        self._dlg = None
        self._dlg_seq = 0
        self._act = None
        # 变速段落地方式固定 bake：达芬奇脚本接口不能设速度，只有把源区间
        # 按速度预渲染成等长素材，帧序/时长才和剪映一致。机器上没有 ffmpeg
        # 时 send 会自己退回 slot，并在报告里写明。
        self.speed_mode = "bake"
        # 自动查询的节拍：上次问的时刻、下次隔多久问。
        self._last_scan = time.monotonic()
        self._scan_interval = AUTO_REFRESH

        # 后台状态查询（单飞：同时只有一个线程在问剪映）
        self._st = threading.Lock()
        self._st_running = False
        self._st_result = None
        self._st_err = None
        # 单飞查询进行中又有人要刷新：记下来，等这次结果贴完再补一次。
        # 不记的话，任务结束时的刷新会被「刚好还在跑」的旧查询吞掉，
        # 面板上的草稿和送入状态停在任务前。
        self._want_refresh = False

        # 兼容性自检：后台线程跑 `cli compat`，结果贴回来再弹窗。
        # 单飞 + 结果暂存 —— 主循环不能等子进程（同 status 的理由）。
        self._diag_running = False
        self._diag_text = ""

        self._close_existing()
        if not self._build_window():
            return
        self._wire()
        # 第一次扫描回来之前，两个按钮都不给点：面板还不知道剪映里有没有草稿。
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

    # ---------- 画窗口 ----------
    #
    # 三级降级：带样式 → 不带样式 → 一个空壳。
    # `StyleSheet` / `MinimumSize` 不是每个 Fusion 版本都认，认不了的版本
    # AddWindow 直接返回 None（整窗打不开）。样式可以没有，面板不能没有。

    def _S(self, qss):
        """样式开关：plain 模式下所有样式属性都被丢掉。"""
        return {"StyleSheet": qss} if getattr(self, "styled", True) else {}

    def _box(self, h, w=9999):
        """高度钉死、宽度放开 —— 字体差一像素也不会把控件挤变形。"""
        if not getattr(self, "styled", True):
            return {}
        return {"MinimumSize": [0, h], "MaximumSize": [w, h]}

    def _grow(self, h):
        """只给下限不给上限：内容长了自己长高，把下面的弹簧顶掉。

        「送入完成 + 两行不送说明」最多三行，钉死 34 会把它剪掉半截 ——
        而这三行恰恰是用户最需要看的（达芬奇里比剪映少了什么）。
        """
        if not getattr(self, "styled", True):
            return {}
        return {"MinimumSize": [0, h]}

    def _pad(self, w):
        """左右留白。Fusion 的组没有 margin 属性，只能用定宽空标签撑。"""
        props = {"Text": "", "Weight": 0}
        if getattr(self, "styled", True):
            props.update({"MinimumSize": [w, 0], "MaximumSize": [w, 9999]})
            props["StyleSheet"] = "background:transparent;"
        return self.ui.Label(props)

    def _spacer(self, weight=1):
        """吃掉多余空间。空 Label 带 Weight 是最稳的弹簧（VGap 没有 Weight）。"""
        return self.ui.Label({"Text": "", "Weight": weight})

    def _rule(self):
        """1px 分隔线：拿 Label 的底色画，不引分割控件。"""
        props = {"Text": "", "Weight": 0}
        if getattr(self, "styled", True):
            props.update({"MinimumSize": [0, 1], "MaximumSize": [9999, 1],
                          "StyleSheet": "background-color:%s;" % C_LINE})
        return self.ui.Label(props)

    def _header(self):
        """产品名 + 一句话说明；右上角是状态。

        「ColorPass」比草稿名小一号：用户每次打开面板关心的是「现在哪份草稿」，
        不是产品叫什么。
        """
        ui, s = self.ui, self._S
        left = ui.VGroup({"Weight": 1}, [
            ui.Label({"ID": "jybTitle", "Text": "ColorPass", "Weight": 0,
                      **s(_text_qss(F_TITLE, C_TEXT, 600))}),
            ui.VGap(2),
            ui.Label({"ID": "jybSub", "Text": "剪映 → 达芬奇 · 往返调色",
                      "Weight": 0, **s(_text_qss(F_NOTE, C_SUB))}),
        ])
        # 状态做成一个标签（「● 就绪」），不拆点 + 文字两个控件：
        # 拆开就要靠间距对齐，而 Fusion 各版本对 Gap 的支持不一致。
        right = ui.Label({"ID": "jybState", "Text": "● 检测中", "Weight": 0,
                          **s(_text_qss(F_BODY, C_SUB))})
        return ui.HGroup({"Weight": 0}, [left, right])

    def _draft_block(self):
        """这一块只说一件事：剪映里现在是哪份草稿、能不能动手。"""
        ui, s = self.ui, self._S
        props = {"ID": "jybInfo", "Text": "正在读取剪映…", "Weight": 0,
                 "WordWrap": True}
        props.update(self._grow(34))
        props.update(s(_text_qss(F_BODY, C_SUB)))
        return ui.VGroup({"Weight": 0}, [
            ui.Label({"ID": "jybCap", "Text": "当前草稿", "Weight": 0,
                      **s(_text_qss(F_NOTE, C_DIM))}),
            ui.VGap(4),
            ui.Label({"ID": "jybDraft", "Text": "—", "Weight": 0,
                      **s(_text_qss(F_NAME, C_TEXT, 600))}),
            ui.VGap(6),
            ui.Label(props),
        ])

    def _action_block(self):
        """唯一的主按钮，整屏只有它是实心的。"""
        ui, s = self.ui, self._S
        send = {"ID": "btnSend", "Text": "送入达芬奇", "Weight": 0}
        send.update(self._box(42))
        send.update(s(_btn_qss("primary")))
        return ui.VGroup({"Weight": 0}, [ui.Button(send)])

    def _write_block(self):
        """写回：名字和按钮是一件事，所以放在同一个块里，中间不插别的。

        名字只在写回时用得上，放到别处就等于让用户先填一个还不知道干吗用的
        输入框。输入框平时是灰的（没送入过就没有可写回的调色），占位文字
        会说清楚还差哪一步。
        """
        ui, s = self.ui, self._S
        label = {"Text": "新草稿名", "Weight": 0,
                 **s(_text_qss(F_BODY, C_DIM))}
        write = {"ID": "btnWrite", "Text": "写回剪映", "Weight": 0}
        write.update(self._box(36))
        write.update(s(_btn_qss("secondary")))
        return ui.VGroup({"Weight": 0}, [
            ui.HGroup({"Weight": 0}, [ui.Label(label), self._pad(10),
                                      self._name_input()]),
            ui.VGap(8),
            ui.Button(write),
        ])

    def _name_input(self):
        """LineEdit 在部分 Fusion 版本上没有，拿不到就退化成一行说明。

        写回仍可用，只是走自动命名；面板本身必须能开。
        """
        ui = self.ui
        props = {"ID": "txtName", "Text": "", "Weight": 1,
                 "PlaceholderText": PH_WAIT}
        props.update(self._box(32))
        props.update(self._S(_input_qss(enabled=False)))
        try:
            return ui.LineEdit(props)
        except Exception as exc:
            note("LineEdit unavailable: %r" % (exc,))
            try:
                return ui.Label({"Text": "本版本面板不支持输入，"
                                         "用命令行 write --name 指定",
                                 "Weight": 1, **self._S(_text_qss(F_NOTE, C_DIM))})
            except Exception:
                return self._spacer(0)

    def _result_line(self):
        props = {"ID": "jybResult", "Text": "", "Weight": 0, "WordWrap": True}
        props.update(self._grow(34))
        props.update(self._S(_text_qss(F_BODY, C_SUB)))
        return self.ui.Label(props)

    def _footer(self):
        """页脚只剩「诊断」。它不是流程的一部分，所以最小号、最右边。"""
        ui, s = self.ui, self._S
        props = {"ID": "btnDiag", "Text": "诊断", "Weight": 0}
        props.update(self._box(22))
        props.update(s(_btn_qss("ghost")))
        return ui.HGroup({"Weight": 0}, [self._spacer(1), ui.Button(props)])

    def _body(self):
        """竖排内容 + 左右各 20 的留白。

        节奏：标题 →24 草稿 →18 主按钮 →20 │分隔│ 18 写回 →14 结果
        →弹簧→ │分隔│ 页脚。两个分隔线把「送进去」和「写回来」切成两段，
        剩下的靠留白，不靠卡片。

        窗口是固定的 460×448；多余的高度由中间那个空 Label 吃掉，所以窗口
        被拉高时只有中间变空，标题贴着顶、页脚贴着底 —— 不会散架。
        """
        ui = self.ui
        inner = ui.VGroup({"Weight": 1}, [
            self._header(),
            ui.VGap(24),
            self._draft_block(),
            ui.VGap(18),
            self._action_block(),
            ui.VGap(20),
            self._rule(),
            ui.VGap(18),
            self._write_block(),
            ui.VGap(14),
            self._result_line(),
            self._spacer(),
            self._rule(),
            ui.VGap(8),
            self._footer(),
            ui.VGap(16),
        ])
        return [ui.HGroup({"Weight": 1},
                          [self._pad(20), inner, self._pad(20)])]

    def _add_window(self, mode):
        """按 mode 建窗。styled 失败会被 _build_window 拿掉样式重试。"""
        self.styled = (mode == "styled")
        try:
            if mode == "minimal":
                return self.disp.AddWindow(
                    {"ID": WIN_ID + "_c", "Geometry": [180, 90, 420, 170],
                     "WindowTitle": "ColorPass"},
                    self.ui.VGroup([self.ui.Label({"Text": "ColorPass",
                                                   "Weight": 0})]))
            win_props = {"ID": WIN_ID, "Geometry": [180, 90, WIN_W, WIN_H],
                         "WindowTitle": "ColorPass"}
            if self.styled:
                win_props["StyleSheet"] = "background-color:%s;" % C_BG
            return self.disp.AddWindow(win_props, self.ui.VGroup(self._body()))
        except Exception:
            note("AddWindow(%s) raised:\n%s" % (mode, traceback.format_exc()))
            return None

    def _build_window(self):
        """三级降级建窗。返回 False 表示一个都建不出来。"""
        win = self._add_window("styled")
        if win is None:
            note("styled AddWindow failed, retry without styles")
            win = self._add_window("plain")
        if win is None:
            note("plain AddWindow failed, retry minimal")
            win = self._add_window("minimal")
        if win is None:
            note("AddWindow failed in all modes, abort")
            return False
        self.win = win
        self.items = self.win.GetItems()
        return True

    def _wire(self):
        """挂回调。窗口 ID 可能是 WIN_ID / _c，谁在就挂谁。"""
        for wid in (WIN_ID, WIN_ID + "_b", WIN_ID + "_c"):
            try:
                getattr(self.win.On, wid).Close = self.on_close
                break
            except Exception:
                continue

        # 逐个独包：退到「空壳窗口」那一级时，控件根本不存在，
        # 一次性赋值会在第一个缺失的按钮上抛异常，后面的全挂不上。
        def hook(wid, fn):
            try:
                getattr(self.win.On, wid).Clicked = fn
            except Exception:
                note("wire %s failed" % wid)

        hook("btnSend", lambda _ev: self._on_send())
        hook("btnWrite", lambda _ev: self.start_job(
            "write", "写回剪映", extra=self._write_extra()))
        hook("btnDiag", lambda _ev: self._on_diag())

    # ---------- 界面小工具 ----------
    def _paint(self, wid, qss):
        """换一个控件的样式。plain 模式下什么都不做。

        禁用态、执行中、成功/失败 —— 这几种状态都是换样式，不是换控件：
        面板里每多一个控件，就多一处各版本表现不一致的可能。
        """
        if not getattr(self, "styled", True):
            return
        try:
            self.items[wid].StyleSheet = qss
        except Exception:
            pass

    def set_state(self, word, color):
        """右上角状态：一个小圆点 + 两个字。颜色就是状态本身。"""
        try:
            self.items["jybState"].Text = "● " + word
        except Exception:
            pass
        self._paint("jybState", _text_qss(F_BODY, color))

    def say_r(self, text):
        try:
            self.items["jybInfo"].Text = text
        except Exception:
            pass

    def say_result(self, text, tone="sub"):
        """任务结果行。跟状态行分开，状态刷新不会把它冲掉。

        tone：sub 灰（默认）/ accent（执行中）/ ok / err。
        """
        try:
            self.items["jybResult"].Text = text
        except Exception:
            pass
        color = {"accent": C_ACCENT, "ok": C_TEXT,
                 "err": C_ERR}.get(tone, C_SUB)
        self._paint("jybResult",
                    _text_qss(F_BODY, color, 600 if tone == "accent" else 0))

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
    # 每个按钮的「身份」，禁用时按它重新上一套样式。用 :disabled 伪状态不保险：
    # Fusion 的元素不一定是 QPushButton 类，类型选择器可能根本不命中。
    KINDS = {"btnSend": "primary", "btnWrite": "secondary"}

    def _set_enabled(self, wid, on):
        try:
            self.items[wid].Enabled = bool(on)
        except Exception:
            pass
        kind = self.KINDS.get(wid)
        if kind:
            self._paint(wid, _btn_qss(kind, bool(on)))
        elif wid == "txtName":
            # 输入框不能填时，除了变色还要换占位文字 —— 用户第一眼看到的是
            # 那句灰字，它得回答「为什么填不了」，而不是「这里能填」。
            self._paint(wid, _input_qss(bool(on)))
            try:
                self.items[wid].PlaceholderText = PH_READY if on else PH_WAIT
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
        self.start_job("send", "送入达芬奇", extra=["--speed", self.speed_mode])

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
                "3  再点一次「送入达芬奇」\n\n"
                "只按 Ctrl+S 不够 —— 保存只是写盘，剪映不会因此重读内存。" % when)

    def _sync_buttons(self):
        """两个按钮、一个输入框，各由一条规则决定能不能动：

        · 剪映里有没有认得出的草稿 —— 没有就「送入」灰掉；
        · 这份草稿成功送入过没有 —— 没有就「写回」和输入框都灰掉；
          写回读的是送入那一刻的快照，没送过就是没有东西可写回；
        · 有没有任务在跑 —— 跑着全灰。
        """
        busy = self.job is not None
        self._set_enabled("btnSend", self._has_draft and not busy)
        self._set_enabled("btnWrite", self._can_write and not busy)
        self._set_enabled("txtName", self._can_write and not busy)

    # ---------- 状态（后台线程，绝不阻塞 StepLoop） ----------
    def _status_worker(self):
        t0 = time.monotonic()
        rep, err = {}, None
        if _no_backend():
            err = _NO_PRODUCT
        else:
            try:
                cmd, cwd = _cmd("status")
                r = subprocess.run(
                    cmd, cwd=cwd,
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
        # 这次问了多久，决定下次隔多久再问。慢机器（剪映内存读不出来要等
        # 超时）不该被 10 秒一次的轮询追着跑。
        spent = time.monotonic() - t0
        self._scan_interval = (AUTO_REFRESH if spent <= SLOW_SCAN
                               else AUTO_REFRESH_SLOW)

    def refresh_draft(self):
        """触发一次后台状态查询。

        没有人手动点它了：面板开窗时、每个任务结束时、以及空闲时的定时轮询
        （maybe_refresh）都会走到这里。

        已有查询在跑时不并发起第二个，但会记一个「待刷新」—— 否则任务结束时
        这次刷新就丢了，面板会一直显示任务前的草稿和送入状态。
        """
        self._last_scan = time.monotonic()
        with self._st:
            if self._st_running:
                self._want_refresh = True
                return
            self._st_running = True
            self._st_result = None
            self._st_err = None
        try:
            t = threading.Thread(target=self._status_worker, daemon=True)
            t.start()
        except Exception as exc:
            note("status thread failed: %r" % (exc,))
            with self._st:
                self._st_running = False
            self._sync_buttons()

    def maybe_refresh(self):
        """没有「刷新」按钮之后，面板自己看当前草稿。

        为什么不能干脆不刷：剪映里换了草稿，面板还挂着上一份的名字，用户会
        照着旧名字点「送入」。为什么不是每秒刷：每次 status 都是一整个子进程
        （源码模式约 1 秒），一直追等于后台常驻一个 CPU 核。所以空闲时 10 秒
        一次，单次查询超过 2.5 秒的机器退到 30 秒。

        任务在跑、或者上一次查询还没回来时不发起新的 —— 那两次的结果必然
        被覆盖，白起一个进程。
        """
        if self.job is not None or self._st_running or self._diag_running:
            return
        if time.monotonic() - self._last_scan < self._scan_interval:
            return
        self.refresh_draft()

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

    def _apply(self, rep, err):
        if not rep:
            # 配置类错误（找不到源码目录）要原样显示 —— 它本身就是修复步骤，
            # 塞进「详见日志」等于让用户去翻一个他不知道为什么存在的文件。
            if err and err.startswith("找不到 ColorPass"):
                self.say_r(err)
            else:
                self.say_r("读取草稿状态失败（详见 %s）" % LOG)
            if err:
                note("status failed: %s" % err)
            self._has_draft = self._can_write = False
            self._edited = False
            try:
                self.items["jybDraft"].Text = "—"
            except Exception:
                pass
            self.set_state("状态未知", C_ERR)
            return
        name = rep.get("draft") or ""
        jy = rep.get("jianying") or {}
        alive = bool(jy.get("processes"))
        opened = bool(rep.get("opened"))
        # 「扫到草稿」= 剪映进程里认出了当前工程。没打开（没有新鲜 .locked）
        # 也算扫到 —— 这种情况要让人看见名字，再用下面的提示催他重新打开。
        self._has_draft = bool(name)
        self._draft_name = name
        self._edited = bool(rep.get("draft_edited"))
        self._edit_text = rep.get("draft_edit_text") or ""
        self._open_text = rep.get("draft_opened_text") or ""
        last = rep.get("last_send") or {}
        # 能不能写回 = 这份草稿成功送入过。两个来源取或：snapshot 指针是权威
        # （跨会话有效），本会话送成功过的也认（指针文件读不出来时不至于卡住）。
        committed = (bool(last.get("committed"))
                     and (last.get("draft") or "") == name)
        self._can_write = bool(name) and (committed or self._sent_name == name)
        try:
            # 草稿名单独一行，前面不加「当前草稿：」—— 上面那行小字已经是标签了。
            self.items["jybDraft"].Text = elide(name, 26) or "—"
        except Exception:
            pass
        # 状态词只占一行位置，所以只挑「现在该干什么」说，不复述草稿名。
        if not alive:
            word, tone = "未运行", C_DIM
            msg = "剪映没在运行。先启动剪映，打开要处理的草稿。"
        elif not name:
            word, tone = "待识别", C_WARN
            msg = "剪映在运行，但没认出当前草稿。在剪映里打开一份草稿，面板会自己跟上。"
        elif not opened:
            word, tone = "待打开", C_WARN
            msg = "没检测到打开状态（可能已回到首页）。重新打开这份草稿再送入。"
        elif self._edited:
            word, tone = "待重开", C_WARN
            msg = ("打开后又改过（%s）。关掉重开一次再送入；只按 Ctrl+S 不够。"
                   % (self._edit_text or "时间未知"))
            if self._can_write:
                msg += "\n写回不受影响，用的是上次送入那版。"
        elif self._can_write:
            word, tone = "已送入", C_ACCENT
            at = short_stamp(last.get("at"))
            msg = "已送入达芬奇%s。调完色就能写回。" % (
                ("（%s）" % at) if at else "")
        else:
            word, tone = "就绪", C_OK
            msg = "已打开，可以送入。"
        self.set_state(word, tone)
        self.say_r(msg)
        note("status: draft=%s alive=%s opened=%s writable=%s"
             % (name or "-", alive, opened, self._can_write))

    # ---------- 兼容性自检 ----------
    def _on_diag(self):
        """收集「这版达芬奇缺什么」，显示成一段能直接复制的文字。

        为什么要放在面板里：这个工具是要发给别人测的，出问题的人未必会开
        终端跑命令。点一下、全选、复制、发回来 —— 就这些动作。

        同样走后台线程：`compat` 要连达芬奇的脚本接口，慢起来几秒。
        """
        if self._diag_running:
            self.say_result("正在收集兼容性信息…", tone="accent")
            return
        self._diag_running = True
        self.say_result("正在收集兼容性信息…", tone="accent")

        def work():
            text = ""
            try:
                cmd, cwd = _cmd("compat")
                r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                   timeout=20, creationflags=NO_WIN,
                                   encoding="utf-8", errors="replace", env=ENV)
                text = (r.stdout or "").strip() or (r.stderr or "").strip()
            except Exception as exc:
                text = ("自检没有跑起来：%s: %s\n\n"
                        "常见原因：安装目录被移动，或系统缺少运行库。"
                        % (type(exc).__name__, exc))
            self._diag_text = text or "自检没有输出。"
            self._diag_running = False

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._diag_running = False
            self.say_result("起不了后台线程：%r" % (exc,), tone="err")

    def poll_diag(self):
        """主循环里贴自检结果（没有新结果就什么都不做）。"""
        text = self._diag_text
        if not text:
            return
        self._diag_text = ""
        self.say_result("", tone="sub")
        if not self._popup_compat(text):
            self.say_result("自检结果：" + text.splitlines()[0][:60], tone="sub")

    def _popup_compat(self, text):
        """把自检结果放在一个可选中复制的框里。"""
        self._close_popup()
        self._dlg_seq += 1
        wid = "jybCmp%d" % self._dlg_seq
        ui, s_ = self.ui, self._S
        props = {"ID": wid, "Geometry": [300, 200, 620, 400],
                 "WindowTitle": "ColorPass · 兼容性自检"}
        if getattr(self, "styled", True):
            props["StyleSheet"] = "background-color:%s;" % C_BG
        box_qss = _qss("color:%s;background-color:%s;border:1px solid %s;"
                       "border-radius:8px;padding:6px;font-size:11px;"
                       % (C_TEXT, C_FIELD, C_LINE))
        ok = {"ID": "dOk", "Text": "关闭", "Weight": 0}
        ok.update(self._box(30))
        ok.update(s_(_btn_qss("secondary")))
        try:
            note_label = {"Text": "下面这一整段可以直接全选复制，发给开发者。",
                          "Weight": 0}
            note_label.update(s_(_text_qss(10, C_DIM)))
            try:
                box = ui.TextEdit({"ID": "dMsg", "Text": text, "ReadOnly": True,
                                   **s_(box_qss)})
            except Exception:
                # 没有 TextEdit 的版本退回只读标签：不能复制，但至少看得见
                box = ui.Label({"ID": "dMsg", "Text": text, "WordWrap": True,
                                "Weight": 1, **s_(_text_qss(11, C_TEXT))})
            dlg = self.disp.AddWindow(props, ui.VGroup([
                ui.Label(note_label), ui.VGap(GAP), box, ui.VGap(GAP * 2),
                ui.HGroup({"Weight": 0}, [ui.Label({"Text": "", "Weight": 1}),
                                          ui.Button(ok)]),
            ]))
        except Exception as exc:
            note("compat popup failed: %r" % (exc,))
            return False
        if dlg is None:
            return False
        try:
            dlg.On.dOk.Clicked = lambda _ev: self._close_popup()
            getattr(dlg.On, wid).Close = lambda _ev: self._close_popup()
        except Exception:
            note("compat popup wiring failed")
        self._dlg = dlg
        try:
            dlg.Show()
        except Exception:
            return False
        return True

    # ---------- 子进程任务 ----------
    def start_job(self, action, label, extra=None):
        if _no_backend():
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
        cmd, cwd = _cmd(action, extra)
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=f,
                                    stderr=subprocess.STDOUT,
                                    creationflags=NO_WIN, env=ENV)
        except Exception as exc:
            f.close()
            self.say_r("启动失败：%s" % exc)
            return
        self.job = {"proc": proc, "out": out, "label": label,
                    "action": action, "file": f, "t0": time.time(),
                    "extra": list(extra or [])}
        self.say_result("%s · 执行中…" % label, tone="accent")
        self.set_state("执行中", C_ACCENT)
        self._sync_buttons()
        note("job start %s pid=%s" % (action, proc.pid))

    def poll_job(self):
        j = self.job
        if not j:
            return
        if j["proc"].poll() is None:
            el = time.time() - j["t0"]
            if el > 1.0:
                self.say_result("%s · 执行中 %.0f 秒（可关窗口中止）"
                                % (j["label"], el), tone="accent")
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
        if ok:
            msg = self._summary(j["action"], rep)
            notes = self._notes(txt)
            if notes:
                msg += "\n" + "\n".join(notes)
            self.say_result(msg, tone="ok")
            # 送入成功 → 这份草稿马上就可以写回，不用等下一次 status 回来。
            if j["action"] == "send":
                self._sent_name = self._draft_name
                self._can_write = bool(self._draft_name)
        else:
            self.say_result("%s · 没有完成（详见弹窗）" % j["label"], tone="err")
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
            if rep.get("coverage_segments"):
                msg += ("\n多机位/合并片段：%d 段共用同一份调色"
                        % rep["coverage_segments"])
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
                    "3  重开后尽快点一次「送入达芬奇」，再按下面的「重试」\n\n"
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
        ui, s_ = self.ui, self._S
        props = {"ID": wid, "Geometry": [280, 190, 600, height],
                 "WindowTitle": "ColorPass · " + title}
        if getattr(self, "styled", True):
            props["StyleSheet"] = "background-color:%s;" % C_BG
        msg = {"ID": "dMsg", "Text": body, "WordWrap": True, "Weight": 1}
        msg.update(s_(_text_qss(12, C_TEXT)))
        hint = {"ID": "dHint", "Text": "完整日志：%s" % LOG, "Weight": 1,
                "WordWrap": True}
        hint.update(s_(_text_qss(10, C_DIM)))
        ok = {"ID": "dOk", "Text": "知道了", "Weight": 0}
        ok.update(self._box(30))
        ok.update(s_(_btn_qss("secondary")))
        act = {"ID": "dAct", "Text": act_label, "Weight": 0}
        act.update(self._box(30))
        act.update(s_(_btn_qss("primary")))
        try:
            dlg = self.disp.AddWindow(
                props, ui.VGroup([
                    ui.Label(msg),
                    ui.VGap(GAP * 3),
                    ui.HGroup({"Weight": 0}, [ui.Label(hint), ui.Button(ok),
                                              ui.Button(act)]),
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
        titles = {"send": "送入失败", "write": "写回失败"}
        title = titles.get(j["action"], "操作失败")
        body = self._fail_text(j["action"], rep, txt, code)
        action, label, extra = j["action"], j["label"], j.get("extra") or []
        ok = self._popup(title, body, act_label="重试",
                         act=lambda: self.start_job(action, label, extra=extra))
        if not ok:
            # 弹窗建不出来也不能什么都不说
            self.say_result("%s · 失败：%s"
                            % (j["label"], body.splitlines()[0][:80]), tone="err")

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
                self.poll_diag()
                self.maybe_refresh()
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
