"""ColorPass · 剪映 ⇄ 达芬奇 往返（达芬奇 21.2 专用）

只做一件事：**把剪映的剪辑送进达芬奇，调完色再写回一份新草稿。**

面板是两个步骤，不是两个按钮：
  1 送入达芬奇 —— 剪映当前草稿 → 达芬奇时间线（逐段按帧号摆放）
  2 写回剪映   —— 达芬奇逐段导出 LUT → 克隆出新的剪映草稿
第 2 步读的是「上次成功送入」的快照，没送入过时整块是灰的 —— 这样「为什么
点不了」不用看说明，一眼就懂。

**只支持达芬奇 21.2。** 18/19/20 的回退分支已经删掉：多机位脚本接口、
真变速、片段类型这些能力在旧版上是「缺了只能换做法」，留着只会让每条路径
都多一层永远走不到的分支。低版本启动时会明确报错，而不是悄悄降级。

界面规则来自 21.2 的实测（`tools/uilab.py` / `tools/mock_ui.py`）：
  · Button / LineEdit 的高度由字号 + QSS padding 决定，给布局 MinimumSize
    会把字切掉一半 —— 所以这里只用 padding，控件高度天然贴合文字；
  · 窗口 Geometry 的高度**包含标题栏**，内容一旦超出客户区就不重绘，
    直接表现为「下半截是整个空的」—— 所以固定高度必须一次给够；
  · 层级只靠字号、灰阶、一条 1px 细线。没有卡片、阴影、动效。

线程模型（沿用实测结论，别改）：
  Fusion 的 UIDispatcher 没有 AddTimer，RunLoop 还会抢 GIL，所以面板只能
  单线程 StepLoop 轮询；真正的活在子进程里干（`colorpass.cli ...`），
  面板只负责起进程、读它写出来的结果文件。

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
#   python    COLORPASS_PYTHON → config.json 的 python → 常见安装位置（取版本最高）
#             → PATH 上的 python
#   product   有 colorpass/cli.py 的目录：COLORPASS_HOME → config.json 的 product
#             → <数据目录>/product → 当前目录
#
# config.json 由 `python -m colorpass.install` 写，里面记着「装的时候用的是谁」。


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
# 自更新用的两个文件（由 colorpass/update.py 和安装器写）：
#   update.json    下载 / 安装的实时进度
#   installed.json 安装器记下的「已装到哪一版」
UPD_FILE = os.path.join(BASE_DIR, "update.json")
UPD_INSTALLED = os.path.join(BASE_DIR, "installed.json")


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
    """打包安装时，install 会把 ColorPass.exe 的路径写进 config.json。"""
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
        direct = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                  "/usr/bin/python3"]
        found = [p for p in direct if os.path.isfile(p)]
        if found:
            return found[0]
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
    return "python"


PY = _find_python()
PRODUCT = _find_product()
CLI = _find_cli()


def _cmd(action, extra=None):
    """子进程该跑什么：(命令, 工作目录)。"""
    if CLI:
        return [CLI, action] + list(extra or []), (PRODUCT or None)
    return [PY, "-m", "colorpass.cli", action] + list(extra or []), PRODUCT


def _no_backend():
    return not PRODUCT and not CLI


OUT = os.path.join(BASE_DIR, "panel")
LOG = os.path.join(OUT, "colorpass_panel.log")
NO_WIN = 0x08000000 if sys.platform.startswith("win") else 0

_NO_PRODUCT = (
    "找不到 ColorPass 的运行入口（源码目录或打包好的 ColorPass.exe）。\n\n"
    "任选一种：\n"
    "  1) 双击安装程序 ColorPass-Setup.exe 重新安装\n"
    "  2) 在克隆下来的目录里运行  python -m colorpass.install\n"
    "  3) 设环境变量 COLORPASS_HOME 指向源码目录\n\n"
    "装好后重启达芬奇。")

# COLORPASS_PROGRESS：面板把子进程输出写进文件（不是终端，isatty 为假），
# 不显式打开的话后端一句进度都不会输出，长任务就只剩一个跳动的秒数。
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
           PYTHONUNBUFFERED="1", COLORPASS_PROGRESS="1")

WIN_ID = "jybWin"
# 460×430 是「内容占满、又不挤」的尺寸。原来的 460×578 不是内容需要的高度：
# Fusion 的每个 VGroup/HGroup 自带约 4px spacing，嵌套几层就是几份叠加，外加
# 每个 VGap 本身也是一个 item（两侧再各叠一次），于是四成高度被间距吃掉
# （真机日志量过：460×578 里内容只有 328px，页脚底边在 466）。
#
# 0.5.0 又按新令牌重算了一遍：内容 357px + 左右各 20 的留白，弹簧 33px。
# 宽 460 下留白 20 → 内容列 420，右列按钮 120 + 间距 16 → 左列 284。
#
# 0.6.1 又长高了一截：说明区从「1 行 16」变成「2 行 34」（+18），结果区
# 从 17 变成 51（+34），结果行上面的间距 16 → 20（+4）。0.5.1 的 405 里
# 内容正好占满（差 2px 就溢出），所以需要 405 + 56 ≈ 461 的客户区，加上
# 标题栏约 30 → 491 —— 但真机排版比画稿多出约 28px 的组间距，实际内容
# 约 436，窗口给 465：留十来像素给弹簧，也不再赌「刚好装下」。
# 弹簧在结果行和页脚之间，窗口再被拉高也只是那块空白变大，内容不会被裁。
WIN_W, WIN_H = 460, 465
SLEEP_IDLE = 0.05
SLEEP_BUSY = 0.02
STATUS_TIMEOUT = 15

AUTO_REFRESH = 10.0
AUTO_REFRESH_SLOW = 30.0
SLOW_SCAN = 2.5


LOG_MAX = 1_500_000      # 约 1.5 MB


def note(msg):
    """面板日志。超过上限就轮转一份 .1。

    以前只追加、从不清理：状态每 10 秒刷一次、每次都记一行，天天开着面板的人
    那份 log 会一路涨到几十 MB，出问题时反而没法打开看。
    """
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) > LOG_MAX:
                os.replace(LOG, LOG + ".1")    # 只留最近一份，不堆 .2/.3
        except OSError:
            pass
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def elide(text, n):
    """长草稿名截断加省略号 —— 面板只有一行给它，剪到半个字更像坏了。"""
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def _greedy(text, per_line):
    """把一段没有换行的文字按显示宽度切成若干行。"""
    lines, cur, width = [], "", 0.0
    for ch in text:
        w = 1.0 if ord(ch) > 0x2E7F else 0.55
        if width + w > per_line and cur:
            lines.append(cur)
            cur, width = "", 0.0
        cur += ch
        width += w
    if cur:
        lines.append(cur)
    return lines


def wrap_text(text, per_line=18, max_lines=2):
    """折行 + 封顶行数，末行放不下就加省略号。

    为什么不用 `WordWrap: True`：21.2 的 Label 开了 WordWrap 也不会把行数
    折算进布局高度 —— 真机日志里 jybInfo 只有 16px 高（正好一行），而「没认出
    当前草稿：…」这类说明是两三行，第二行被齐腰切掉，看着像乱码。

    所以行数由这里决定，控件高度按「固定 max_lines 行」给死，排版是确定的。
    显式换行（作者自己分段的那句「先关掉多余的剪映窗口」）优先保留 ——
    整段被 ellipsis 吃掉，用户就只剩「出错了」而拿不到「该怎么办」。
    """
    raw = str(text or "").replace("\r", "")
    paras = [p.strip() for p in raw.split("\n") if p.strip()]
    if not paras:
        return ""
    out = []
    for para in paras:
        for line in _greedy(para, per_line):
            out.append(line)
            if len(out) >= max_lines:
                break
        if len(out) >= max_lines:
            break
    if len(out) > max_lines:
        out = out[:max_lines]
    if sum(len(x) for x in out) < sum(len(p) for p in paras):
        out[-1] = out[-1][:-1] + "…"
    return "\n".join(out)


def _tail(path, n=4096):
    """读文件最后 n 字节。还没建出来/读不了就当空。

    子进程的 stdout 已经被 Popen 以写方式打开了，这里再开一个句柄读同一份
    文件 —— Windows 上允许共享读（实测过），不需要改动 Popen 那边的写法。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def short_stamp(s):
    """「2026-11-19 23:47:41」→「11-19 23:47」。"""
    m = re.search(r"(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", str(s or ""))
    return "%s-%s %s:%s" % m.groups() if m else ""


def _env_note():
    """一行环境实况。

    面板是达芬奇内部起的子进程，环境和终端不一样 —— 出过「pgrep 在终端
    好用、在这里返回空」的假故障，导致「达芬奇明明开着」被判成没启动。
    以后再有类似问题，这一行就能直接指认是哪一侧。
    """
    bits = ["PATH=%s" % (os.environ.get("PATH") or "<unset>")]
    try:
        r = subprocess.run(["pgrep", "-f", "DaVinci Resolve"],
                           capture_output=True, text=True, timeout=5)
        bits.append("pgrep rc=%s out=%r" % (r.returncode, (r.stdout or "")[:40]))
    except Exception as exc:
        bits.append("pgrep %s: %s" % (type(exc).__name__, exc))
    return " | ".join(bits)


note("module loaded (pid=%d) %s" % (os.getpid(), _env_note()))


# ---------------------------------------------------------------- 外观
#
# 三级信息：草稿名（18，唯一的主角）→ 说明与结果（12）→ 步骤名 / 页脚（11）。
# 全屏只有一个实心蓝，只给「送入达芬奇」。其余靠灰阶和留白。
# 间距一律 4 的倍数；分隔只用 1px 细线，不堆边框、不堆卡片底色。
# 字体用系统中文字体 —— 指定拉丁字体会让中英混排一高一低。


def _font_family():
    if sys.platform == "darwin":
        return "PingFang SC"
    if sys.platform.startswith("win"):
        return "Microsoft YaHei UI"
    return "Noto Sans CJK SC"


FONT = _font_family()

C_ACCENT = "#3B82F6"
C_TEXT = "#EDEDF0"
C_SUB = "#9A9AA2"
C_DIM = "#6C6C74"
C_LINE = "#2E2E33"
C_LINE_SOFT = "#26262A"
C_FIELD = "#26262A"
C_BG = "#1A1A1C"
C_PILL = "#222226"
C_OK = "#34C759"
C_WARN = "#FF9F0A"
C_ERR = "#FF453A"

# ---- 排版令牌 ----
#
# 字号只有三档：15 标题 / 13 正文 / 11 元信息。
# 12 那一档原本专门给「步骤名」，可它和 13 摆在一起只差 1px，看不出层级，
# 反倒让版面里同时存在三种几乎一样大的字 —— 现在是同 13、靠 600 字重区分。
F_TITLE = 15
F_NAME = 15
F_BTN = 13
F_BODY = 13
F_STEP = 13
F_NOTE = 11

# 控件尺寸：按钮和输入框完全同高（34），右列按钮同宽（120）。
# 高度 = 行高 18 + padding 7×2 + 边框 1×2；主按钮也画一圈同色 1px 边框，
# 少了这 2px 两颗按钮就会差一截 —— 截图上第一眼就能看出来。
CTRL_PAD_V = 7
CTRL_W = 120
# 按钮/输入框的目标高度：行高 18 + padding 7×2 + 边框 1×2。
# 只用来卡 MaximumSize —— Fusion 会把同一 HGroup 里的子项拉满行高，
# 不设上限的话主按钮会被左边的两行说明撑到 60。
CTRL_H = 34

# 间距网格。Fusion 的每个组自带约 4px spacing，而 VGap 本身也是一个 item，
# 两侧再各叠一次 —— 所以「两个元素之间的距离」= VGap 参数 + 8。
# 下面这些值都按 +8 换算过：8/12/16/20/24 五档。
GAP_XS, GAP_S, GAP_M, GAP_L = 0, 4, 8, 12

# 单行高度。真机日志量的：15px 的草稿名占 20，13px 的说明每行 17。
# Label 给了固定高度就自己截字（Fusion 不会把行数折算进高度），所以凡是可能
# 换行的文本，高度都必须按「行数 × 行高」显式给出来。
LH_NAME = 20
LH_INFO = 17
RESULT_LINES = 3

# 每行能放几个「中文字宽」。左边那列实测 246px 宽（13px 字 → 18 个），
# 结果行占满 390（→ 30 个）。估小一点，宁可提前折行也不要顶到边框。
INFO_CHARS = 18
RESULT_CHARS = 30

HINT_IDLE = "写回会在剪映里新建一份草稿，原草稿保持不动。"

# 输入框里那行灰字。它替面板解释了「为什么现在填不了」，比一句状态说明省地方。
PH_WAIT = "先送入达芬奇，才能写回"
PH_READY = "留空＝自动命名「草稿名_调色」"

# 面板是独立脚本，装进 Fusion 后 import 不到 colorpass 包，版本号只能写在这。
# tests/test_panel.py 会拿它和 colorpass.__version__ 对一遍，防止漏改。
PANEL_VERSION = "1.0.0"


def _version():
    return PANEL_VERSION


def _qss(body):
    return "font-family:%s;%s" % (FONT, body)


def _text_qss(size, color, weight=0):
    body = "color:%s;font-size:%spx;background:transparent;" % (color, size)
    if weight:
        body += "font-weight:%d;" % weight
    return _qss(body)


# 按钮宽度固定：两颗按钮尺寸必须一模一样，靠文字撑的话「送入达芬奇」5 个字、
# 「写回剪映」4 个字，右边就会一长一短（实测 118 vs 114）。
_SIZING = ("min-width:%dpx;max-width:%dpx;" % (CTRL_W, CTRL_W))


def _btn_qss(kind, enabled=True):
    """按钮样式。禁用态直接换一套样式 —— 不靠 :disabled 选择器。

    主、次按钮只差「填充还是描边」，尺寸走同一套令牌：13px 字、7px 上下
    padding、1px 边框、8px 圆角、120px 宽。任何一项分开写，两边就会对不齐。

    高度只由 font-size + padding 决定：21.2 的 Fusion 不把布局高度分给
    Button，设 MinimumSize 只会把文字切掉，padding 才是安全的。
    """
    box = ("border-radius:8px;font-size:%dpx;padding:%dpx 0;%s"
           % (F_BTN, CTRL_PAD_V, _SIZING))
    if kind == "primary":
        if not enabled:
            # 禁用态保留淡边框：整个按钮退回一行灰字的话，用户看不出这里
            # 本来是个按钮，只当是一条说明文字。
            return _qss("color:%s;background-color:transparent;"
                        "border:1px solid %s;" % (C_DIM, C_LINE) + box)
        # 边框和底色同色：不是为了看得见，是为了那 2px 高度和次按钮对齐。
        #
        # 这里**不能**加 font-weight。21.2 遇到粗体会换一套字面，行高从 18
        # 变成 25 —— 真机日志量到主按钮 41 高、次按钮 34 高，就是它。
        # 而且换过的字面看起来比次按钮大一号，两颗按钮摆在一起像两种字号。
        # 主次的区别只靠实心/描边，字号、字重、padding 三项都和次按钮一致。
        return _qss("color:#FFFFFF;background-color:%s;"
                    "border:1px solid %s;"
                    % (C_ACCENT, C_ACCENT) + box)
    if kind == "secondary":
        if not enabled:
            return _qss("color:%s;background-color:transparent;"
                        "border:1px solid %s;" % (C_DIM, C_LINE) + box)
        return _qss("color:%s;background-color:transparent;"
                    "border:1px solid #3A3A40;" % C_TEXT + box)
    # 页脚的两个变体：**都不给底色和边框**。
    #
    # 0.6.0 给它们套了一层 C_PILL 的胶囊底，本意是「让人看出能点」，
    # 结果四个灰方块排在底部，和上面的输入框长得一模一样 —— 用户第一反应是
    # 「诊断和联系作者怎么变成框了」。现在按 shadcn 的 ghost/link 处理：
    # 平时只是一行字，鼠标移上去才浮出一点底色。
    if kind == "link":
        color = C_ACCENT if enabled else C_DIM
        # 左右 padding 给 0：页脚左右两端必须和上面的正文对齐在同一条竖轴上。
        # 给 8px 的话「检查更新」会离右边框差 8px，整行看着像缩进去一截。
        body = ("color:%s;background-color:transparent;border:0;"
                "border-radius:6px;font-size:%dpx;padding:4px 0;"
                % (color, F_NOTE))
        return _qss(body)
    # ghost：页脚的「诊断」「联系作者」。比正文暗一档，但比 0.5.0 那句
    # C_DIM 亮 —— 那是「看不清」，现在只是「不抢眼」。
    body = ("color:%s;background-color:transparent;border:0;"
            "border-radius:6px;font-size:%dpx;padding:4px 0;"
            % (C_SUB, F_NOTE))
    return _qss(body)


def _input_qss(enabled=True):
    """输入框。尺寸令牌和按钮同一套：13px 字 + 7px padding + 1px 边框 = 34。

    不能填的时候底色退回面板色、字变暗 —— 但它仍然是一个 34 高的框，
    不会因为禁用就比旁边的按钮矮一截。
    """
    box = ("border-radius:8px;font-size:%dpx;padding:%dpx 10px;"
           % (F_BTN, CTRL_PAD_V))
    if not enabled:
        return _qss("color:%s;background-color:transparent;"
                    "border:1px solid %s;" % (C_DIM, C_LINE_SOFT) + box)
    return _qss("color:%s;background-color:%s;border:1px solid %s;"
                "selection-background-color:%s;"
                % (C_TEXT, C_FIELD, C_LINE, C_ACCENT) + box)


def _badge_qss(on):
    """步骤序号：20×20 圆形，中性灰底。

    不用蓝色：整屏唯一的实心蓝要留给主按钮。两个蓝圆点 + 蓝按钮摆在一起，
    视线会被拉成三处，反而看不出哪里该点。
    """
    return _qss("color:%s;background-color:%s;border-radius:10px;"
                "font-size:%dpx;font-weight:700;"
                "qproperty-alignment:AlignCenter;"
                % (C_TEXT if on else C_DIM,
                   "#303038" if on else "#252529", F_NOTE))


def _pill_qss(color):
    return _qss("color:%s;background-color:%s;border:1px solid %s;"
                "border-radius:11px;font-size:%dpx;padding:0 9px;"
                "qproperty-alignment:AlignCenter;"
                % (color, C_PILL, C_LINE, F_NOTE))


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
        self._can_write = False
        self._sent_name = ""
        self._edited = False
        self._edit_text = ""
        self._open_text = ""
        self._result_text = ""
        self._dlg = None
        self._dlg_seq = 0
        self._act = None
        self.speed_mode = "bake"
        self._last_scan = time.monotonic()
        self._scan_interval = AUTO_REFRESH

        self._st = threading.Lock()
        self._st_running = False
        self._st_result = None
        self._st_err = None
        self._want_refresh = False

        self._diag_running = False
        self._diag_text = ""

        # 自更新：检查结果 → 页脚按钮 → 独立进程下载安装 → 读 update.json
        self._upd_busy = False        # 正在下载 / 安装
        self._upd_checking = False    # 后台检查中
        self._upd_pending = None      # (结果, 是否手动触发)
        self._upd_ready = False       # 已知有新版：再点一下就直接装
        self._upd_latest = ""
        self._upd_size = 0
        self._upd_phase = ""
        self._upd_done = ""           # 装完的新版本号
        self._upd_inst_at = 0.0
        self._upd_read_at = 0.0       # 上次读进度文件的时间（节流用）
        self._upd_started = 0.0       # 点下更新的时刻（判断「进程没起来」）

        self._close_existing()
        if not self._build_window():
            return
        self._wire()
        self._sync_buttons()

        self.refresh_draft()
        self.win.Show()
        note("window shown")
        self._dump_geometry()
        # 开窗不查更新。查一次要起子进程、走网络（国内 2 秒上下），而且用户
        # 打开面板是为了送入/写回，不是为了更新 —— 右下角有按钮，想查再查。
        self.loop()

    def _dump_geometry(self):
        """把窗口和关键控件的实际尺寸写进日志。

        21.2 的排版问题只靠截图分不清「控件被压扁」还是「文字画歪」；
        记一行数字，事后能对着日志核对。

        0.5.0 这里算错过一次：`item.Geometry` 是**相对父容器**的，页脚的
        y 一直是 0，于是 footer_bottom 报 22/417 一片祥和，实际页脚早被顶到
        客户区外面看不见了。所以现在把「窗口 → 内容列 → 页脚行」这条链上的
        偏移量加起来，报的是窗口坐标系里的绝对底边。
        """
        try:
            win_geo = dict(self.win.GetGeometry())
            limit = int(win_geo.get(4, WIN_H))
            parts = ["win=%s" % win_geo]
            for wid in ("btnSend", "btnWrite", "txtName", "jybInfo",
                        "jybResult", "jybDraft", "jybVer"):
                it = (self.items or {}).get(wid)
                if it is not None:
                    parts.append("%s=%s" % (wid, dict(it.Geometry)))

            def geo(wid):
                try:
                    return dict(self.items[wid].Geometry)
                except Exception:
                    return None

            # 绝对底边 = 内容列的 y + 页脚行的 y + 页脚行的高。
            col, foot = geo("colBody"), geo("rowFooter")
            if col and foot:
                bottom = (int(col.get(2, 0)) + int(foot.get(2, 0))
                          + int(foot.get(4, 0)))
                parts.append("footer_bottom=%d/%d%s"
                             % (bottom, limit,
                                " OVERFLOW" if bottom > limit else ""))
            # 设计约束自查：两颗按钮必须同宽同高，输入框必须和按钮同高。
            # Fusion 对 QSS 的 max-width 支持不一定完整，这里把实测值写进日志 ——
            # 用户重启一次就能确认排版在真机上有没有走样，不用再靠肉眼比对截图。
            send, write, name = geo("btnSend"), geo("btnWrite"), geo("txtName")
            if send and write:
                dw = abs(int(send.get(3, 0)) - int(write.get(3, 0)))
                dh = abs(int(send.get(4, 0)) - int(write.get(4, 0)))
                parts.append("buttons=%dx%d/%dx%d%s"
                             % (int(send.get(3, 0)), int(send.get(4, 0)),
                                int(write.get(3, 0)), int(write.get(4, 0)),
                                " MISMATCH" if dw > 1 or dh > 1 else ""))
            if send and name:
                d = abs(int(send.get(4, 0)) - int(name.get(4, 0)))
                parts.append("input_vs_button=%s"
                             % ("MISMATCH" if d > 1 else "ok"))
            # 文本裁切自查：折了两行却只给一行的高度，就是「字被切一半」。
            # 这次 0.6.0 的乱码就是这么来的 —— 光看截图分不清是缺字还是缺高。
            for wid, lh in (("jybInfo", LH_INFO), ("jybResult", LH_INFO),
                            ("jybDraft", LH_NAME)):
                g = geo(wid)
                if not g:
                    continue
                try:
                    lines = self.items[wid].Text.count("\n") + 1
                except Exception:
                    lines = 1
                parts.append("%s=%dx%d/%dL%s"
                             % (wid, int(g.get(3, 0)), int(g.get(4, 0)),
                                lines,
                                " CLIP" if lines * lh > int(g.get(4, 0)) + 2
                                else ""))
            note("geometry " + " ".join(parts))
        except Exception as exc:
            note("geometry dump failed: %r" % (exc,))

    # ---------- 构造分段 ----------
    def _close_existing(self):
        """同名旧窗口还在（重复点菜单）就先收掉，免得叠出两个。"""
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
    # 三级降级：带样式 → 不带样式 → 一个空壳。样式可以没有，面板不能没有。

    def _S(self, qss):
        return {"StyleSheet": qss} if getattr(self, "styled", True) else {}

    def _pad(self, w):
        """左右留白。Fusion 的组没有 margin 属性，只能用定宽空标签撑。"""
        props = {"Text": "", "Weight": 0}
        if getattr(self, "styled", True):
            props.update({"MinimumSize": [w, 0], "MaximumSize": [w, 9999]})
            props["StyleSheet"] = "background:transparent;"
        return self.ui.Label(props)

    def _spacer(self, weight=1):
        """吃掉多余空间。空 Label 带 Weight 是最稳的弹簧。"""
        return self.ui.Label({"Text": "", "Weight": weight})

    def _pad_v(self, h):
        """底部留一道固定高度的空白。

        页脚永远贴在内容最下面，而 Fusion 对「正好压在客户区底边」的内容
        是不重绘的（0.5.0 页脚整行消失就是这么来的）。留 10px 的余量，
        即使窗口高度算差几像素，页脚也还在可见范围内。
        """
        return self.ui.Label({"Text": "", "Weight": 0,
                              "MinimumSize": [0, h], "MaximumSize": [9999, h]})

    def _rule(self, color=None):
        """1px 分隔线：拿 Label 的底色画，不引分割控件。"""
        props = {"Text": "", "Weight": 0}
        if getattr(self, "styled", True):
            props.update({"MinimumSize": [0, 1], "MaximumSize": [9999, 1],
                          "StyleSheet": "background-color:%s;"
                                        % (color or C_LINE)})
        return self.ui.Label(props)

    def _header(self):
        """产品名 + 状态胶囊同一行，副标在下面。

        胶囊原来和「标题 + 副标」两行整体居中对齐，看着比标题低半行；
        把它放进标题那一行，两者基线就自然齐了。
        """
        ui, s = self.ui, self._S
        title = {"ID": "jybTitle", "Text": "ColorPass", "Weight": 1}
        title.update(s(_text_qss(F_TITLE, C_TEXT, 600)))
        # 状态做成一个胶囊标签（「● 就绪」），不拆点 + 文字两个控件。
        state = {"ID": "jybState", "Text": "● 检测中", "Weight": 0,
                 "MinimumSize": [0, 22], "MaximumSize": [140, 22]}
        state.update(s(_pill_qss(C_SUB)))
        sub = {"ID": "jybSub", "Text": "剪映 → 达芬奇 · 往返调色", "Weight": 0}
        sub.update(s(_text_qss(F_NOTE, C_SUB)))
        return ui.VGroup({"Weight": 0}, [
            ui.HGroup({"Weight": 0}, [ui.Label(title), ui.Label(state)]),
            ui.Label(sub),
        ])

    def _step_head(self, num, title, on=False):
        """「① 读取草稿 · 等待剪映」—— 序号 + 标题 + 状态字。

        字号和正文同为 13（靠 600 字重区分），状态字用 11 的弱色挂在后面。
        原来标题和状态之间还夹一颗 6px 的点：那颗点的左右间距和字号都不是
        整数倍，排出来像一条跑偏的项目符号，而且状态字本身已经换色了
        （灰=等、绿=就绪、蓝=执行中），点纯属重复。
        """
        ui, s = self.ui, self._S
        badge = {"ID": "step%sBadge" % num, "Text": str(num), "Weight": 0,
                 "MinimumSize": [20, 20], "MaximumSize": [20, 20]}
        badge.update(s(_badge_qss(on)))
        label = {"ID": "step%sTitle" % num, "Text": title, "Weight": 0}
        label.update(s(_text_qss(F_STEP, C_TEXT if on else C_SUB, 600)))
        tail = {"ID": "step%sState" % num, "Text": "", "Weight": 0}
        tail.update(s(_text_qss(F_NOTE, C_DIM)))
        return ui.HGroup({"Weight": 0}, [
            ui.Label(badge), self._pad(GAP_M), ui.Label(label),
            self._pad(GAP_M), ui.Label(tail), self._spacer(1)])

    def _draft_block(self):
        """第 1 步的主体：草稿名（主角）+ 一句现状说明。

        两个 Label 都平铺在 _body 里，不再各套一层 VGroup。

        高度全部写死：真机实测 15px 的名字占 20、13px 的说明每行 17。说明
        固定两行（34）—— 原来只给了它一行的高度，而状态说明常常是两三行，
        第二行被切掉一半，截图里就是一行「鬼画符」。名字只留一行，超长靠
        elide 截断。
        """
        ui, s = self.ui, self._S
        name = {"ID": "jybDraft", "Text": "正在读取剪映…", "Weight": 0,
                "MinimumSize": [0, LH_NAME], "MaximumSize": [9999, LH_NAME]}
        name.update(s(_text_qss(F_NAME, C_DIM)))
        info = {"ID": "jybInfo", "Text": "", "Weight": 0, "WordWrap": True,
                "MinimumSize": [0, LH_INFO * 2],
                "MaximumSize": [9999, LH_INFO * 2]}
        info.update(s(_text_qss(F_BODY, C_SUB)))
        return ui.VGroup({"Weight": 1}, [ui.Label(name), ui.Label(info)])

    def _send_button(self):
        """唯一的主按钮，整屏只有它是实心的。尺寸和次按钮共用一套令牌。

        必须给 MaximumSize 卡住高度：它和「草稿名 + 两行说明」在同一个
        HGroup 里，Fusion 会把同排子项拉满行高 —— 左边那块两行文字约 60，
        按钮就跟着变成 60×120，而右边第 2 步的按钮只有 34 高，两颗按钮摆在
        一起一高一矮（macOS 真机量到 60 vs 35）。只限制上限不动下限：
        MinimumSize 会被 Fusion 当成硬裁切，把字切掉半行。
        """
        props = {"ID": "btnSend", "Text": "送入达芬奇", "Weight": 0,
                 "MaximumSize": [CTRL_W + 2, CTRL_H]}
        props.update(self._S(_btn_qss("primary")))
        return self.ui.Button(props)

    def _write_button(self):
        """第 2 步的行动按钮。描边样式，和主按钮区分主次但尺寸一致。"""
        write = {"ID": "btnWrite", "Text": "写回剪映", "Weight": 0,
                 "MaximumSize": [CTRL_W + 2, CTRL_H]}
        write.update(self._S(_btn_qss("secondary")))
        return self.ui.Button(write)

    def _name_input(self):
        """LineEdit 拿不到就退化成一行说明；面板本身必须能开。

        Weight 1 让它和右边的按钮并排时吃掉剩下的宽度，高度和按钮同为 34。
        """
        ui = self.ui
        props = {"ID": "txtName", "Text": "", "Weight": 1,
                 "PlaceholderText": PH_WAIT, "MinimumSize": [160, 34]}
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
        """结果区：这次点了之后发生了什么。空着时给一句「写回不会覆盖原草稿」。

        只放**操作结果**。原来草稿状态的说明也往这里写（say_r），于是点一次
        「检查更新」就把「剪映没在运行，先启动剪映」顶掉了 —— 状态说明回到
        第 1 步的 jybInfo 去了。

        高度固定三行：写回成功那一条要把「帧数 / 分辨率 / 剪映是否又改过」
        一起说完，两行装不下，第三行会被切掉 —— 而那句「不含这些改动」正是
        用户最需要看见的。
        """
        props = {"ID": "jybResult", "Text": HINT_IDLE, "Weight": 0,
                 "WordWrap": True,
                 "MinimumSize": [0, LH_INFO * RESULT_LINES],
                 "MaximumSize": [9999, LH_INFO * RESULT_LINES]}
        props.update(self._S(_text_qss(F_BODY, C_DIM)))
        return self.ui.Label(props)

    def _footer(self):
        """页脚：左边「诊断 · 联系作者」，右边「vX.Y.Z  检查更新」。

        这里踩过两次坑，都记一下：

        · 0.5.0 只用 C_DIM 的裸字 → 用户「看不懂这是什么」；
        · 0.6.0 给每个加了一层胶囊底 → 四个灰方块排在底部，和输入框一个长相，
          用户「诊断和联系作者怎么变成框了」。

        现在的做法是 ghost / link 两种按钮：**透明底、无边框**，就是一行字。
        左边两项用 C_SUB（能读，但不抢眼），「检查更新」用强调色 —— 页脚只有
        一个地方是蓝的，指哪儿点哪儿。

        没做 :hover —— Fusion 的 StyleSheet 是一整串交给 Qt 解析，混进
        `QPushButton:hover{}` 这种规则万一解析失败，整块样式会被丢掉，页脚直接
        变成黑字黑底。宁可少一个悬停反馈，也不能赌它。
        """
        ui = self.ui
        props = {"ID": "btnDiag", "Text": "诊断", "Weight": 0}
        props.update(self._S(_btn_qss("ghost")))
        about = {"ID": "btnAbout", "Text": "联系作者", "Weight": 0}
        about.update(self._S(_btn_qss("ghost")))
        sep = {"ID": "jybSep", "Text": "·", "Weight": 0}
        sep.update(self._S(_text_qss(F_NOTE, C_DIM)))
        ver = {"ID": "jybVer", "Text": "v" + _version(), "Weight": 0}
        ver.update(self._S(_text_qss(F_NOTE, C_DIM)))
        chk = {"ID": "btnUpdate", "Text": "检查更新", "Weight": 0}
        chk.update(self._S(_btn_qss("link", enabled=True)))
        # 行本身给个最小高度：按钮不给（21.2 给 Button 定高会把字切掉），
        # 高 24 由 11px 字 + 上下 4px padding 自己长出来。
        return ui.HGroup({"ID": "rowFooter", "Weight": 0,
                          "MinimumSize": [0, 24]}, [
            ui.Button(props), self._pad(GAP_S),
            ui.Label(sep), self._pad(GAP_S),
            ui.Button(about), self._spacer(1),
            ui.Label(ver), self._pad(GAP_M), ui.Button(chk)])

    def _body(self):
        """竖排内容 + 左右各 20 的留白。

        整页只用一个 VGroup，中间不再套块 —— Fusion 每层组都会加一次默认
        spacing，套三层就等于把行距翻了三倍，这正是「页面利用率低」的由来。

        版面是一条严格的竖轴，一根细线把它切成两段：

            产品名 / 副标 · 状态胶囊
            ──────────────────────────────
            ① 读取草稿 · 就绪
               草稿名（一行，超长省略）        [送入达芬奇]
               一句现状（两行封顶）
            ──────────────────────────────
            ② 命名并写回 · 等待前一步
               [新草稿名…]                    [写回剪映]
            结果说明（两行封顶）
            （弹簧）
            诊断 · 联系作者            vX.Y.Z  检查更新

        弹簧放在**结果行之后、页脚之前**：面板在达芬奇里可以被拉高，弹簧要是
        放在内容中间，窗口一高就变成「上半页有字、中间一大块空、底下又冒出两
        行」—— 用户看到的就是「整个 UI 很乱」。现在内容从顶部往下连续排完，
        多出来的高度全部落在最后一行的上方，页脚照旧贴着底边，像一条状态栏。

        0.5.0 在结果和页脚之间还有第三根线，占掉 21px，正好把页脚顶出
        客户区；页脚现在靠 16px 的空隙和上面分开，不再要那根线。
        """
        ui = self.ui
        # 只建一次：两个 Label 各自带 ID，建两遍会让 items 指向第二份，
        # 状态更新写进去，界面上那份却纹丝不动。
        step1 = ui.HGroup({"Weight": 0}, [
            self._draft_block(),
            self._pad(GAP_M + 4),
            self._send_button(),
        ])
        step2 = ui.HGroup({"Weight": 0}, [
            self._name_input(),
            self._pad(GAP_M + 4),
            self._write_button(),
        ])
        inner = ui.VGroup({"ID": "colBody", "Weight": 1}, [
            self._header(),
            ui.VGap(GAP_M),                 # 16
            self._rule(),
            ui.VGap(GAP_M),                 # 16
            self._step_head(1, "读取草稿"),
            ui.VGap(GAP_S),                 # 12
            step1,
            ui.VGap(GAP_L),                 # 20
            self._rule(C_LINE_SOFT),
            ui.VGap(GAP_M),                 # 16
            self._step_head(2, "命名并写回"),
            ui.VGap(GAP_S),                 # 12
            step2,
            ui.VGap(GAP_L),                 # 20
            self._result_line(),
            self._spacer(1),                # 弹簧只吃「结果行到页脚」之间
            ui.VGap(GAP_M),                 # 16
            self._footer(),
            self._pad_v(10),                # 页脚不贴客户区底边
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
        hook("btnAbout", lambda _ev: self._on_about())
        hook("btnUpdate", lambda _ev: self._on_update())

    # ---------- 界面小工具 ----------
    def _paint(self, wid, qss):
        """换一个控件的样式。plain 模式下什么都不做。"""
        if not getattr(self, "styled", True):
            return
        try:
            self.items[wid].StyleSheet = qss
        except Exception:
            pass

    def set_state(self, word, color):
        try:
            self.items["jybState"].Text = "● " + word
        except Exception:
            pass
        self._paint("jybState", _pill_qss(color))

    def say_r(self, text):
        try:
            self.items["jybInfo"].Text = wrap_text(text, INFO_CHARS, 2)
        except Exception:
            pass

    def _set_text(self, wid, text, qss=None):
        """改文字（顺带换样式）。Text 和 StyleSheet 是两条独立属性，
        漏掉样式的话「未检测到草稿」会顶着 18px 粗体显示，比不显示还难堪。"""
        try:
            self.items[wid].Text = text
        except Exception:
            pass
        if qss:
            self._paint(wid, qss)

    def _show_draft(self, name):
        """第 1 步的主角：草稿名用大字，没有草稿时换成一句灰话。

        不再显示「—」：那根横线孤零零占一整行，看着像断掉的分隔线，
        也是「排版很奇怪」的来源之一。
        """
        if name:
            # 15 个中文字宽 ≈ 225px，左边那列 246 —— 再长就一定折行，而这一行
            # 只给了 20px 高，折行等于被切。所以这里必须先截。
            self._set_text("jybDraft", elide(name, 15),
                           _text_qss(F_NAME, C_TEXT, 600))
        else:
            # 空状态也用 15px：和第 1 步的高度不变，按钮不会上下跳，
            # 只是降成灰字（12px 的话这一步会突然缩一截）。
            self._set_text("jybDraft", "未检测到草稿",
                           _text_qss(F_NAME, C_DIM))

    def _set_tail(self, wid, text, color=C_DIM):
        """步骤标题右侧那句小状态。"""
        self._set_text(wid, text, _text_qss(F_NOTE, color))

    def say_result(self, text, tone="sub"):
        """任务结果行。空串时退回那句固定说明。

        结果区固定两行：折行在这里做（wrap_text），高度在 _result_line 里给死。
        原来按 splitlines() 取前三行 —— 换行是 Fusion 自己折的，它折几行、
        给多高，两边对不上，就出现「第二行被切一半」。
        """
        text = text or HINT_IDLE
        shown = wrap_text(text, RESULT_CHARS, RESULT_LINES)
        self._result_text = text if text != HINT_IDLE else ""
        try:
            self.items["jybResult"].Text = shown
        except Exception:
            pass
        color = {"accent": C_ACCENT, "ok": C_TEXT,
                 "err": C_ERR}.get(tone, C_DIM if text == HINT_IDLE else C_SUB)
        self._paint("jybResult",
                    _text_qss(F_BODY, color, 600 if tone == "accent" else 0))

    def _new_name(self):
        """面板上填的新草稿名；没有输入框或留空就返回空串（走自动命名）。

        用的是 `items["txtName"]` 而不是 `.get()` —— Fusion 的 GetItems()
        返回的是它自己的对象，不是 dict。
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
            # 那句灰字，它得回答「为什么填不了」。
            self._paint(wid, _input_qss(bool(on)))
            try:
                self.items[wid].PlaceholderText = PH_READY if on else PH_WAIT
            except Exception:
                pass

    def _sync_buttons(self):
        """两个按钮、一个输入框、第 2 步的序号，各由一条规则决定：

        · 剪映里有没有认得出的草稿 —— 没有就「送入」灰掉；
        · 这份草稿成功送入过没有 —— 没有就「写回」/输入框/第 2 步序号都灰；
        · 有没有任务在跑 —— 跑着全灰。
        """
        # 更新期间不要让人再点「送入」：安装器会把助手进程 taskkill 掉，
        # 那时候起的任务必然失败，还会弹一个看不懂的错。
        busy = self.job is not None or self._upd_busy
        self._set_enabled("btnSend", self._has_draft and not busy)
        self._set_enabled("btnWrite", self._can_write and not busy)
        self._set_enabled("txtName", self._can_write and not busy)
        on = self._can_write and not busy
        self._paint("step1Badge", _badge_qss(self._has_draft and not busy))
        self._paint("step1Title", _text_qss(
            F_STEP, C_TEXT if (self._has_draft and not busy) else C_SUB, 600))
        self._paint("step2Badge", _badge_qss(on))
        self._paint("step2Title", _text_qss(F_STEP, C_TEXT if on else C_SUB, 600))
        # 每步标题旁自己说「现在能不能做」，不用人从两个按钮的灰度去猜。
        # 状态点跟着一起换色：灰=等，绿=可以，蓝=正在跑。
        if busy:
            self._set_state(1, "执行中…", C_ACCENT)
            self._set_state(2, "", C_DIM)
        else:
            self._set_state(1, "就绪" if self._has_draft else "等待剪映",
                            C_OK if self._has_draft else C_DIM)
            self._set_state(2, "就绪" if self._can_write else "等待前一步",
                            C_OK if self._can_write else C_DIM)

    def _set_state(self, num, text, tone):
        """步骤状态：只有一行字，颜色就是状态（灰=等 / 绿=就绪 / 蓝=执行中）。"""
        self._set_tail("step%dState" % num, text, tone)

    def _on_send(self):
        """点「送入」时先过一道本地闸门。

        草稿在打开之后被改过 → 直接弹窗说清楚并让人重开，不启动子进程。
        判据只有两个文件时间（draft_settings / .locked），几十毫秒就有结论；
        不这么做的话，用户点完要等 CLI 扫完内存（几秒到几十秒）才知道结果。

        macOS 上还有一道前置：草稿密钥只在剪映进程内有效，必须先确认进程里
        有读取器；没有就让用户点一下「重启剪映并继续」，之后一直是就绪状态，
        不需要再关心这一步。

        两道检查的顺序有讲究：草稿改动的判据来自后台 status 命令（读两个文件
        时间），不依赖读取器、几十毫秒就出结果。如果先查读取器，用户会在
        「草稿被改过 + 读取器不在位」时被要求重启剪映（白做），重启完才被告知
        草稿还得再重开。所以本地能立刻判断的「草稿改过」必须排在前面。
        """
        if self._edited:
            self._popup("草稿改过了，先重开", self._stale_body(),
                        act=self._go_send, act_label="我已重开，再试",
                        height=430)
            return
        if sys.platform == "darwin" and self._mac_reader_state() != "ready":
            self._popup("剪映要先配合一次", self._mac_reader_body(),
                        act=self._mac_restart_then_send,
                        act_label="重启剪映并继续", height=430,
                        show_log=False)
            return
        self._go_send()

    # ---------- macOS：剪映进程内的读取器 ----------
    #
    # 剪映 11.7 起把草稿 AES 密钥从 libvideoeditor.dylib 挪进了进程
    # （lvve::ICryptoKeyStore）：磁盘上的 draft_info.json 外部解不开，进程内存
    # 也读不到（hardened runtime + 无 get-task-allow）。唯一可行的是让剪映带着
    # ColorPass 的读取器启动，在它进程内解密 —— 密钥不出剪映，只交出明文。
    #
    # 对用户来说这只发生一次：剪映自动保存 → 重启 → 之后照常使用。
    def _mac_reader_state(self):
        """剪映进程里有没有读取器。只探一次本地 socket，几毫秒。"""
        import socket as _socket
        sock = os.path.join(_data_dir(), "reader.sock")
        if not os.path.exists(sock):
            return "down"
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect(sock)
            s.sendall(b"PING\n")
            head = s.makefile("rb").readline()
            s.close()
            return "ready" if head.startswith(b"PONG") else "down"
        except Exception:
            return "down"

    def _mac_reader_body(self):
        return ("macOS 上剪映从 11.7 起把草稿密钥放进了自己的进程里，"
                "磁盘文件是加密的，外部解不开。\n\n"
                "所以读取草稿需要让剪映带着 ColorPass 的读取器运行一次。"
                "点下面的按钮，工具会：\n"
                "1  让剪映自动保存当前草稿\n"
                "2  由它重新打开剪映（约 10 秒）\n"
                "3  读取草稿并继续送入达芬奇\n\n"
                "之后剪映照常使用，编辑器、导出、自动保存都不受影响；"
                "这个提示也不会再出现（除非你自己重启过剪映）。")

    def _mac_restart_then_send(self):
        """带读取器重启剪映，成功后直接接着送入。"""
        cmd, cwd = _cmd("mac-launch")
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=180, env=ENV)
        except Exception as exc:
            self._popup("没能启动剪映",
                        "重启剪映时出错：%s\n\n"
                        "处理：手动退出剪映，再点一次「送至达芬奇」。" % exc,
                        height=330)
            return
        if r.returncode != 0:
            detail = (r.stdout or "") + (r.stderr or "")
            self._popup("没能启动剪映",
                        "%s\n\n处理：\n1  手动退出剪映\n"
                        "2  再点一次「送至达芬奇」，由工具启动它"
                        % (detail.strip()[-1200:] or "未知原因"),
                        height=380)
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
        spent = time.monotonic() - t0
        self._scan_interval = (AUTO_REFRESH if spent <= SLOW_SCAN
                               else AUTO_REFRESH_SLOW)

    def refresh_draft(self):
        """触发一次后台状态查询。

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
        """空闲时每 10 秒自己看一眼；一次查询超过 2.5 秒就退到 30 秒。"""
        if self.job is not None or self._st_running or self._diag_running:
            return
        # 安装期间 bin 里的助手正被替换，这时候去查状态必然失败 —— 让位。
        if self._upd_busy:
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
            if err and err.startswith("找不到 ColorPass"):
                self.say_r(err)
            else:
                self.say_r("读取草稿状态失败，详见「诊断」。")
            if err:
                note("status failed: %s" % err)
            self._has_draft = self._can_write = False
            self._edited = False
            self._show_draft("")
            self.set_state("状态未知", C_ERR)
            return
        name = rep.get("draft") or ""
        jy = rep.get("jianying") or {}
        alive = bool(jy.get("processes"))
        opened = bool(rep.get("opened"))
        self._has_draft = bool(name)
        self._draft_name = name
        self._edited = bool(rep.get("draft_edited"))
        self._edit_text = rep.get("draft_edit_text") or ""
        self._open_text = rep.get("draft_opened_text") or ""
        last = rep.get("last_send") or {}
        committed = (bool(last.get("committed"))
                     and (last.get("draft") or "") == name)
        self._can_write = bool(name) and (committed or self._sent_name == name)
        self._show_draft(name)
        if not alive:
            word, tone = "未运行", C_DIM
            msg = "剪映没在运行，先启动它再打开草稿。"
        elif not name:
            word, tone = "待识别", C_WARN
            msg = "剪映在运行，但没认出当前草稿。在剪映里打开一份草稿，面板会自己跟上。"
            why = str(rep.get("draft_error") or "").strip()
            if why and "未运行" not in why:
                # 后端那句 why 常有四五十字（「窗口工程名或实时文件句柄对应多个
                # 进程或草稿目录，无法精确匹配。」），原样塞进两行的说明区只会
                # 被截成半句。用户要的是「为什么」+「怎么办」，原文留给「诊断」。
                if "多个" in why:
                    msg = ("没认出草稿：开了多个剪映窗口。\n"
                           "先关掉多余的，只留一份。")
                else:
                    msg = "没认出草稿。\n" + elide(why, 16)
        elif not opened:
            word, tone = "待打开", C_WARN
            msg = "没检测到打开状态（可能已回到首页）。重新打开这份草稿再送入。"
        elif self._edited:
            word, tone = "待重开", C_WARN
            # 两行封顶：第一行「什么时候改的」，第二行「怎么办」。
            # 写回的影响并进第二行（「写回仍用旧版」）—— 原来它是第三段，
            # 在两行的框里会被整段截掉，用户就会以为写回能带上新改动。
            act = ("关掉重开再送入；写回仍用旧版。" if self._can_write
                   else "关掉重开再送入。")
            msg = "打开后又改过（%s）。\n%s" % (
                self._edit_text or "时间未知", act)
        elif self._can_write:
            word, tone = "已送入", C_ACCENT
            at = short_stamp(last.get("at"))
            msg = "已送入%s。调完色就能写回。" % (
                ("（%s）" % at) if at else "")
        else:
            word, tone = "就绪", C_OK
            msg = "已打开，可以送入。"
        self.set_state(word, tone)
        self.say_r(msg)
        note("status: draft=%s alive=%s opened=%s writable=%s"
             % (name or "-", alive, opened, self._can_write))

    # ---------- 检查更新 ----------
    #
    # 手动一步：点「检查更新」→ 查一次 → 有新版先弹窗问「要不要装」→ 点了才装。
    # 开窗不查、定时不查 —— 用户打开面板是为了送入/写回，不该被网络请求和
    # 「新版本」提示打扰；查不到时也只有主动点的人才需要看到失败原因。
    #
    # 为什么不复用 job 通道：安装器会 taskkill 所有 ColorPass.exe（不杀掉就
    # 替换不掉 bin 里被占用的 dll），而干活的就是其中一个 —— 它必然死在半路。
    # 所以面板不盯子进程的 stdout，只读它写下的 update.json；进程没了也能
    # 继续显示「正在安装」。
    def _on_update(self):
        """页脚「检查更新」：点一下才查。"""
        if self._upd_done:
            self.say_result("已更新到 v%s · 关掉并重开达芬奇就生效。"
                            % self._upd_done, tone="ok")
            return
        if self._upd_busy:
            self.say_result("正在更新（%s）…" % (self._upd_phase or "处理中"),
                            tone="accent")
            return
        if self._upd_checking:
            self.say_result("正在检查更新…", tone="accent")
            return
        if self.job:
            self.say_result("先把「%s」跑完再更新。" % (self.job.get("label") or "任务"),
                            tone="accent")
            return
        if _no_backend():
            self.say_r(_NO_PRODUCT)
            return
        self.say_result("正在检查更新…", tone="accent")
        self._set_update_button("检查中…", enabled=False)
        self._check_update_async()

    def _install_update(self):
        """弹窗里点了「下载并安装」才走到这里。"""
        if self._upd_busy or not self._upd_latest:
            return
        cmd, cwd = _cmd("update", ["--install"])
        try:
            subprocess.Popen(cmd, cwd=cwd, creationflags=NO_WIN, env=ENV,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.say_result("更新没启动起来：%s" % exc, tone="err")
            self._set_update_button("检查更新")
            return
        self._upd_busy = True
        self._upd_phase = "准备中"
        self._upd_inst_at = 0.0
        self._upd_started = time.time()
        self._set_update_button("更新中…", enabled=False)
        self.say_result("正在准备更新到 v%s…" % self._upd_latest, tone="accent")
        self._sync_buttons()
        note("update start target=%s" % self._upd_latest)

    def _check_update_async(self):
        """后台查一次（不走缓存 —— 是用户自己点的，就要当下的结果）。"""
        if self._upd_checking:
            return
        self._upd_checking = True

        def work():
            data = {}
            try:
                cmd, cwd = _cmd("update", ["--check"])
                r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                   timeout=25, creationflags=NO_WIN,
                                   encoding="utf-8", errors="replace", env=ENV)
                data = json.loads((r.stdout or "").strip() or "{}")
            except Exception as exc:
                note("update check failed: %r" % (exc,))
                data = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            self._upd_checking = False
            self._upd_pending = data

        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception as exc:
            self._upd_checking = False
            self._set_update_button("检查更新")
            note("update check thread failed: %r" % (exc,))

    def poll_update(self):
        item = self._upd_pending
        if item is not None:
            self._upd_pending = None
            self._apply_update(item)
        if self._upd_busy:
            self._poll_install()

    def _apply_update(self, data):
        """检查结果：新版就弹窗问，其余情况只在结果区说一句。"""
        self._set_update_button("检查更新")
        if not data or not data.get("ok"):
            err = str((data or {}).get("error") or "网络不通")
            self.say_result("检查更新失败：%s\n"
                            "可到 github.com/1303234678-pixel/ColorPass/releases "
                            "手动下载安装包" % err[:48], tone="err")
            note("update check: %s" % err)
            return
        if not data.get("has_new"):
            self.say_result("已是最新版本（v%s）。" % _version(), tone="ok")
            return
        self._upd_latest = str(data.get("latest") or "")
        self._upd_size = int(data.get("size") or 0)
        self._upd_ready = True
        self.say_result("发现新版本 v%s · 已弹窗确认。" % self._upd_latest,
                        tone="accent")
        note("update available %s" % self._upd_latest)
        if not self._popup("发现新版本", self._update_body(data),
                           act=self._install_update,
                           act_label="下载并安装", ok_label="以后再说",
                           height=380, show_log=False):
            # 弹窗建不起来（Fusion 在某些环境下会失败）也不能把人堵死：
            # 结果区里已经写了版本号，再补一句手动下载的办法。
            self.say_result("发现新版本 v%s。弹窗没打开，可到 "
                            "github.com/1303234678-pixel/ColorPass/releases 下载。"
                            % self._upd_latest, tone="accent")

    def _update_body(self, data):
        ver = str(data.get("latest") or "")
        size = int(data.get("size") or 0)
        lines = ["发现新版本 v%s（当前 v%s）%s。"
                 % (ver, _version(),
                    ("，安装包约 %.1f MB" % (size / 1e6)) if size else "")]
        notes = str(data.get("notes") or "").strip()
        if notes:
            lines += ["", "本次更新", notes]
        lines += ["", "现在下载并安装吗？装完要重启达芬奇才生效。"]
        return "\n".join(lines)

    def _set_update_button(self, text="检查更新", enabled=True):
        try:
            self.items["btnUpdate"].Text = text
        except Exception:
            return
        self._paint("btnUpdate", _btn_qss("link", enabled=bool(enabled)))

    @staticmethod
    def _read_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _poll_install(self):
        """读 update.json 推进度。没有这个文件说明进程还没写第一条。

        主循环 20ms 一轮，不节流就是每秒 50 次磁盘读（和 `_tail_phase` 同一个坑）。
        """
        now = time.time()
        if now - self._upd_read_at < 0.4:
            return
        self._upd_read_at = now
        st = self._read_json(UPD_FILE)
        if not st and self._upd_started and time.time() - self._upd_started > 45:
            # update.json 一条都没写：进程根本没起来（缺运行库、被杀毒拦）。
            # 不兜这一下的话，面板会永远停在「正在准备更新」。
            self._upd_busy = False
            self._sync_buttons()
            self._set_update_button("检查更新")
            self.say_result("更新没启动起来。重启达芬奇后再试一次，\n或到 "
                            "github.com/1303234678-pixel/ColorPass/releases 手动下载。",
                            tone="err")
            return
        phase = str(st.get("phase") or "")
        target = str(st.get("target") or self._upd_latest)
        if phase == "error":
            self._upd_busy = False
            self._upd_phase = ""
            self._sync_buttons()
            self._set_update_button("检查更新")
            self.say_result("更新没有完成：%s\n"
                            "可到 github.com/1303234678-pixel/ColorPass/releases "
                            "手动下载安装包"
                            % str(st.get("msg") or "未知原因")[:52], tone="err")
            return
        if phase == "idle":
            self._upd_busy = False
            self._upd_ready = False
            self._set_update_button("检查更新")
            self.say_result("已经是最新版本（v%s）。" % _version(), tone="ok")
            return
        if phase == "downloading":
            pct = int(st.get("pct") or 0)
            got = float(st.get("got") or 0)
            self._upd_phase = "下载 %d%%" % pct
            self.say_result("正在下载 v%s · %d%%%s"
                            % (target, pct,
                               ("（%.1f MB）" % (got / 1e6)) if got else ""),
                            tone="accent")
            return
        if phase == "installing":
            installed = str(self._read_json(UPD_INSTALLED).get("version") or "")
            if installed and installed == target:
                self._finish_install(target, str(st.get("path") or ""))
                return
            if not self._upd_inst_at:
                self._upd_inst_at = time.time()
            elif time.time() - self._upd_inst_at > 120:
                self._upd_busy = False
                self._upd_phase = ""
                self._sync_buttons()
                self.say_result("安装没成功（多半是达芬奇占着文件）。\n"
                                "关掉达芬奇后重新运行安装包即可。", tone="err")
                return
            self._upd_phase = "安装中"
            self.say_result("正在安装 v%s…（后台助手会被重启，面板留着就行）"
                            % target, tone="accent")
            return
        if phase == "checking":
            self.say_result("正在确认版本…", tone="accent")

    def _finish_install(self, ver, setup_path=""):
        self._upd_busy = False
        self._sync_buttons()
        # 安装器自己删不掉正在运行的包，等它退出后由面板收拾。删不掉也无所谓，
        # 下次更新会覆盖同名文件。
        if setup_path:
            try:
                os.remove(setup_path)
            except Exception:
                pass
        self._upd_ready = False
        self._upd_done = ver
        self._upd_phase = ""
        self._set_update_button("已更新 ✓", enabled=False)
        self.say_result("已更新到 v%s · 关掉达芬奇再打开就生效。" % ver, tone="ok")
        self.say_r("更新完成，重启达芬奇后生效。")
        note("update installed %s" % ver)

    # ---------- 兼容性自检 ----------
    def _on_about(self):
        """弹「联系作者」窗口：邮箱 + 两张收款码。

        为什么不在面板里画：Fusion 的 UI Manager 画不出图片（21.2 实测
        `Label`/`Button` 的 `Icon` 被静默忽略，`ui.Picture` 不存在），
        而二维码必须是像素级的，用文字排出来扫不响。所以起一个自己的进程，
        由它开一个和面板同配色的小窗 —— 也不占用面板的 job 通道（那个通道
        按 JSON 解析输出，弹窗没有 JSON）。
        """
        if _no_backend():
            self.say_r(_NO_PRODUCT)
            return
        cmd, cwd = _cmd("about")
        try:
            subprocess.Popen(cmd, cwd=cwd, creationflags=NO_WIN, env=ENV,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as exc:
            self.say_r("打不开联系作者窗口：%s（邮箱：1303234678@qq.com）" % exc)

    def _on_diag(self):
        """点一下、全选、复制、发回来 —— 出问题的人未必会开终端。"""
        if self._upd_busy:
            self.say_result("更新装完再诊断，现在助手进程正在被替换。", tone="accent")
            return
        if self._diag_running:
            self.say_result("正在收集运行环境信息…", tone="accent")
            return
        self._diag_running = True
        self.say_result("正在收集运行环境信息…", tone="accent")

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
        props = {"ID": wid, "Geometry": [300, 200, 620, 420],
                 "WindowTitle": "ColorPass · 运行环境"}
        if getattr(self, "styled", True):
            props["StyleSheet"] = "background-color:%s;" % C_BG
        box_qss = _qss("color:%s;background-color:%s;border:1px solid %s;"
                       "border-radius:8px;padding:8px;font-size:11px;"
                       % (C_TEXT, C_FIELD, C_LINE))
        ok = {"ID": "dOk", "Text": "关闭", "Weight": 0}
        ok.update(s_(_btn_qss("secondary")))
        try:
            note_label = {"Text": "下面这一整段可以直接全选复制，发给开发者。",
                          "Weight": 0}
            note_label.update(s_(_text_qss(10, C_DIM)))
            try:
                box = ui.TextEdit({"ID": "dMsg", "Text": text, "ReadOnly": True,
                                   **s_(box_qss)})
            except Exception:
                box = ui.Label({"ID": "dMsg", "Text": text, "WordWrap": True,
                                "Weight": 1, **s_(_text_qss(11, C_TEXT))})
            dlg = self.disp.AddWindow(props, ui.VGroup([
                ui.Label(note_label), ui.VGap(4), box, ui.VGap(8),
                ui.HGroup({"Weight": 0}, [self._spacer(1), ui.Button(ok)]),
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
            # POSIX 上单独开一个进程组：关窗口时能整组收掉（子进程里还会再
            # spawn ffmpeg 等孙子进程，只杀直接子进程会留下孤儿继续写盘）。
            kw = {"start_new_session": True} if not sys.platform.startswith("win") else {}
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=f,
                                    stderr=subprocess.STDOUT,
                                    creationflags=NO_WIN, env=ENV, **kw)
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

    @staticmethod
    def _tail_phase(j, el):
        """子进程最后一句进度，当作「现在在干嘛」。

        0.5 秒最多读一次 —— 主循环 20ms 一轮，不节流就是每秒 50 次磁盘读。
        """
        if time.time() - (j.get("_ph_at") or 0) < 0.5:
            return j.get("_ph") or "执行中 "
        j["_ph_at"] = time.time()
        ph = ""
        for line in reversed(_tail(j.get("out")).splitlines()):
            s = line.strip()
            # 最后那段 JSON 报告的每一行、空行都不算进度
            if not s or s[0] in '{}[]",' or '": ' in s:
                continue
            ph = s[3:] if s.startswith("·· ") else s
            break
        ph = ph[:22]
        j["_ph"] = (ph + " · ") if ph else "执行中 "
        return j["_ph"]

    def poll_job(self):
        j = self.job
        if not j:
            return
        if j["proc"].poll() is None:
            el = time.time() - j["t0"]
            if el > 1.0:
                # 一段大工程写回要几十秒到两分钟。只跳秒数的话，用户没法判断
                # 它是「在推进」还是「卡住了」—— 把子进程最后一句进度贴出来。
                ph = self._tail_phase(j, el)
                self.say_result("%s · %s%.0f 秒" % (j["label"], ph, el),
                                tone="accent")
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
        ok = (code == 0) and bool(rep.get("ok", True))
        self.job = None
        note("job end %s code=%s ok=%s" % (j["action"], code, ok))
        if ok:
            msg = self._summary(j["action"], rep)
            notes = self._notes(txt)
            if notes:
                msg += "\n" + "\n".join(notes)
            self.say_result(msg, tone="ok")
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
        """取子进程输出里最后一个完整 JSON 对象。"""
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
            baked = rep.get("baked") or []
            if sm == "bake":
                if baked:
                    msg += ("\n变速段 %d 段已预渲染：达芬奇里就是真变速"
                            "（帧位与剪映一致）" % len(baked))
            elif rep.get("speed_fallback"):
                # 已经从「用户自己去装 ffmpeg」改成「装包自带」，还能走到这里
                # 说明安装残缺（多半被杀毒软件删了），给的动作只能是重装。
                msg += ("\n!! 自带的 ffmpeg 不见了，变速段只能近似摆放"
                        "—— 重新运行一次安装包即可恢复")
            if rep.get("disabled"):
                msg += "\n停用段 %d 段已按停用送入（占位保留、不出画面）" % rep["disabled"]
            return msg
        if action == "write":
            name = rep.get("draft_name") or "新草稿"
            n = rep.get("luts") or rep.get("segments") or 0
            msg = "写回完成：「%s」，%d 段 LUT" % (name, n)
            lines = [msg]
            bs = rep.get("basics") or {}
            if bs:
                # 用户最怕的就是「回去以后帧数变了」。这一行是逐项比对的结果，
                # 不是「应该没变」——排在最前，任何时候都不许被挤掉。
                lines.append("帧数 %s → %s｜%sx%s｜%s 段一致"
                             % (bs.get("frames_src"), bs.get("frames_new"),
                                (bs.get("canvas_new") or ["?", "?"])[0],
                                (bs.get("canvas_new") or ["?", "?"])[1],
                                bs.get("segments_checked")))
            # 附注按重要性排队，结果区只有 3 行：改过 > 原样保留 > 多机位。
            extra = []
            d = rep.get("drift") or {}
            if d and not d.get("same"):
                # 写回永远按「送入那版」生成，用户在剪映里后来加/删/改的东西
                # 不会出现在新草稿里。这件事必须写在脸上 —— 不然用户拿到新草稿
                # 才发现少了两段，只会以为工具把东西弄丢了。
                #
                # 措辞压缩到一行放得下（30 字）：结果区只有三行，这句一旦折行
                # 就会把「不含这些改动」挤到第四行、被截掉 —— 恰好是最该看的那半句。
                extra.append("!! 剪映里又改过（+%s −%s 改%s），新草稿不含这些改动"
                             % (d.get("added"), d.get("removed"),
                                d.get("changed")))
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
                more = len(bits) - 4
                extra.append("原样保留：" + "·".join(bits[:4])
                             + ("等 %d 项" % (more + 4) if more > 0 else ""))
            if rep.get("coverage_segments"):
                extra.append("多机位/合并片段：%d 段共用同一份调色"
                             % rep["coverage_segments"])
            if rep.get("alignment_ok") is False:
                extra.insert(0, "注意：对齐校验未通过，建议在剪映里抽查")
            # 有帧数那行时只留一条附注，保证三段全是一行写完；没有帧数
            # （老报告/异常情况）才给两条。
            lines.extend(extra[:1 if bs else 2])
            return "\n".join(lines)
        if action == "reset":
            w = rep.get("will_use") or {}
            if w.get("draft"):
                return ("已重置：将按「%s」%s 那次送入的版本继续。"
                        "再点一次「写回剪映」。" %
                        (elide(w["draft"], 16), short_stamp(w.get("at")) or ""))
            return "已重置。现在可以重新「送入达芬奇」。"
        return "完成"

    @staticmethod
    def _notes(txt, limit=2):
        """成功摘要下面最多附两行 —— 「达芬奇里比剪映少了什么」。"""
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

    def _popup(self, title, body, act=None, act_label="重试", height=340,
               ok_label="知道了", show_log=True):
        """结果弹窗（失败 / 需要用户再决定一步）。不用模态，开独立小窗。

        `ok_label` 是「不动手，关掉」那颗按钮 —— 更新确认里叫「以后再说」，
        其他场合「知道了」就够；`show_log=False` 用于没失败的场合。
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
        msg.update(s_(_text_qss(F_BODY, C_TEXT)))
        # 失败弹窗才贴日志路径；更新确认这类「一切正常，只是问一句」的弹窗
        # 带上日志路径只会让人以为出事了。
        # 日志路径单独占一行：它贴在按钮左边时只剩一半宽度，一个完整路径
        # 必然折成两行，而 HGroup 只按一行给高度 —— 第二行就被切了。
        hint = {"ID": "dHint", "Text": "完整日志：%s" % LOG if show_log else "",
                "Weight": 0, "WordWrap": False}
        hint.update(s_(_text_qss(10, C_DIM)))
        ok = {"ID": "dOk", "Text": ok_label, "Weight": 0}
        ok.update(s_(_btn_qss("secondary")))
        actb = {"ID": "dAct", "Text": act_label, "Weight": 0}
        actb.update(s_(_btn_qss("primary")))
        try:
            dlg = self.disp.AddWindow(
                props, ui.VGroup([
                    ui.Label(msg),
                    ui.VGap(8),
                    ui.Label(hint),
                    ui.VGap(8),
                    ui.HGroup({"Weight": 0}, [self._spacer(1),
                                              ui.Button(actb),
                                              self._pad(GAP_S),
                                              ui.Button(ok)]),
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
        if rep.get("need_reset"):
            # 「上一次送入没走完」这一类，重试只会原地报同一句话。面板里没有
            # 终端，让用户去敲 `colorpass reset` 等于没给出口 —— 直接把重置
            # 做成按钮，点一下回到上一次成功的版本，再点「写回剪映」即可。
            ok = self._popup(title, body, act_label="重置这次送入",
                             act=lambda: self.start_job("reset", "重置这次送入"))
        else:
            ok = self._popup(title, body, act_label="重试",
                             act=lambda: self.start_job(action, label,
                                                        extra=extra))
        if not ok:
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
                self.poll_update()
                self.maybe_refresh()
            except Exception:
                note("step error:\n" + traceback.format_exc())
                self.say_r("处理出错，详见 %s" % LOG)
            time.sleep(SLEEP_BUSY if self.job else SLEEP_IDLE)

    # ---------- 生命周期 ----------
    def _kill_job(self):
        """收掉正在跑的任务（关窗口时）。

        Windows 用 taskkill /T 连子孙一起；macOS 上根本没有 taskkill ——
        以前这里是死代码（异常被吞掉），关掉面板后写回的 python 还活着，
        用户再点一次就变成两个进程同时往剪映草稿目录里写（索引丢条目、
        同名草稿互相覆盖）。
        """
        j = self.job
        if not j:
            return
        pid = j["proc"].pid
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=NO_WIN)
            else:
                import os
                import signal
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            note("kill job %s failed: %r" % (pid, exc))

    def on_close(self, _ev):
        self.closing = True
        self._close_popup()
        self._kill_job()
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
