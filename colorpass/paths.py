"""ColorPass · 目录约定

为什么要有这个模块
------------------
之前 cube 落在会话工作区 `rs-bridge/out/luts/`，工作区一被清理，剪映里就
「LUT 丢失」。这里把三个目录定死：

  LUT_ROOT    <数据目录>/luts/<草稿ID>/    取回调色的工作副本
  RUN_ROOT    <数据目录>/runs/<草稿ID>/    每次动作的报告与日志
  EMBED_DIR   写回时把 cube 复制进新草稿目录下的 Resources/ColorPass/

第三条是关键：LUT 跟着草稿走。草稿被复制、迁移、换机器，cube 都还在，
不依赖任何工作区或临时目录。

数据目录按各平台惯例放（Windows 的 %LOCALAPPDATA% 搬到 mac 上会落在
`~/Library/Caches` 那种会被系统清理的位置，不能照抄）：
  Windows  %LOCALAPPDATA%\\ColorPass
  macOS    ~/Library/Application Support/ColorPass
  Linux    ~/.local/share/ColorPass
`COLORPASS_DATA` 可以整个覆盖，绿色版/测试用。
"""
import os
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_DIR = os.path.dirname(PKG_DIR)

WINDOWS = sys.platform.startswith("win")
MACOS = sys.platform == "darwin"


def data_root():
    """数据目录（不创建）。"""
    env = os.environ.get("COLORPASS_DATA")
    if env:
        return os.path.abspath(env)
    home = os.path.expanduser("~")
    if WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA") or home, "ColorPass")
    if MACOS:
        return os.path.join(home, "Library", "Application Support", "ColorPass")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or
                        os.path.join(home, ".local", "share"), "ColorPass")


BASE = data_root()
LUT_ROOT = os.path.join(BASE, "luts")
RUN_ROOT = os.path.join(BASE, "runs")
RETIME_ROOT = os.path.join(BASE, "retime")
LOG_PATH = os.path.join(BASE, "colorpass.log")

# 写回时嵌入草稿目录的相对位置
EMBED_SUBDIR = os.path.join("Resources", "ColorPass")

FPS = 30
PAD_SUFFIX = "|PAD"

# 我们自己建的时间线都以此开头。使用前闸门靠它区分「我们的」和「用户的」，
# send 前的清理也只删这个前缀，绝不碰用户原有时间线。
TIMELINE_TAG = "CP"
# 改名前建的时间线（`JY · 草稿名`）仍算自己的，否则升级后老工程会被
# 误判成「别人的时间线」而被闸门拦住。
LEGACY_TIMELINE_TAGS = ("JY",)
# 段上 marker 的名字。读回时只认 customData 里的 segment_id，名字仅作标记，
# 所以旧名 "JY" 的 marker 不用迁移。
MARKER_NAME = TIMELINE_TAG

# 变速段的落地方式：
#   slot   占位优先 —— 时长按剪映的 target 摆放，时间线严格对齐（默认）
#   source 内容优先 —— 按源帧数 1x 摆放，内容一帧不丢，但后面的段会后移
#   bake   预渲染   —— 用 ffmpeg 把源区间按 speed 重采样成等长新素材，
#                      达芬奇里是「真变速」，时长/内容/帧号都与剪映一致
SPEED_MODES = ("slot", "source", "bake")


def ensure(*parts):
    p = os.path.join(*parts) if parts else BASE
    os.makedirs(p, exist_ok=True)
    return p


def is_own_timeline(name, tag=None):
    """这条时间线是不是本工具建的（含改名前的 `JY · …`）。"""
    if tag and tag != TIMELINE_TAG:
        tags = (tag,)
    else:
        tags = (TIMELINE_TAG,) + LEGACY_TIMELINE_TAGS
    return (name or "").startswith(tags)


def migrate_legacy_base():
    """把改名前的 `%LOCALAPPDATA%\\JYBridge` 迁到新目录。

    只在新目录还不存在时搬一次：缓存里是明文草稿副本，丢了大不了重读一遍，
    但重读要剪映里还留着明文，所以能搬就搬。失败不影响运行。
    """
    old = os.path.join(os.path.dirname(BASE), "JYBridge")
    if os.path.isdir(old) and not os.path.exists(BASE):
        try:
            os.rename(old, BASE)
            return True
        except OSError:
            return False
    return False


def draft_lut_dir(draft_id):
    return ensure(LUT_ROOT, str(draft_id))


def draft_run_dir(draft_id):
    return ensure(RUN_ROOT, str(draft_id))


def retime_dir(draft_id):
    return ensure(RETIME_ROOT, str(draft_id))


def embed_dir(new_draft_folder):
    return ensure(new_draft_folder, EMBED_SUBDIR)


def note(msg):
    """统一日志。面板和 CLI 都往这里写，出问题只看一个文件。"""
    import time
    try:
        ensure()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass
