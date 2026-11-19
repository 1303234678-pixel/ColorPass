"""JYBridge · 目录约定

为什么要有这个模块
------------------
之前 cube 落在会话工作区 `rs-bridge/out/luts/`，工作区一被清理，剪映里就
「LUT 丢失」。这里把三个目录定死：

  LUT_ROOT    %LOCALAPPDATA%\\JYBridge\\luts\\<草稿ID>\\   取回调色的工作副本
  RUN_ROOT    %LOCALAPPDATA%\\JYBridge\\runs\\<草稿ID>\\   每次动作的报告与日志
  EMBED_DIR   写回时把 cube 复制进新草稿目录下的 Resources/JYBridge/

第三条是关键：LUT 跟着草稿走。草稿被复制、迁移、换机器，cube 都还在，
不依赖任何工作区或临时目录。
"""
import os

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_DIR = os.path.dirname(PKG_DIR)

BASE = os.path.join(os.environ.get("LOCALAPPDATA") or
                    os.path.expanduser("~"), "JYBridge")
LUT_ROOT = os.path.join(BASE, "luts")
RUN_ROOT = os.path.join(BASE, "runs")
RETIME_ROOT = os.path.join(BASE, "retime")
LOG_PATH = os.path.join(BASE, "jybridge.log")

# 写回时嵌入草稿目录的相对位置
EMBED_SUBDIR = os.path.join("Resources", "JYBridge")

FPS = 30
PAD_SUFFIX = "|PAD"

# 我们自己建的时间线都以此开头。使用前闸门靠它区分「我们的」和「用户的」，
# send 前的清理也只删这个前缀，绝不碰用户原有时间线。
TIMELINE_TAG = "JY"

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
