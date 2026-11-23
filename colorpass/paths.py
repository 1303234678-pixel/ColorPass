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
PHOTO_ROOT = os.path.join(BASE, "photos")
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
    """建目录并返回路径。相对路径一律接在 BASE 下面。

    原来直接 `os.path.join(*parts)`，于是 `ensure("bin")` 得到的是相对于
    当前工作目录的 `bin` —— 打包安装器正好是这么调的，结果运行时被复制到了
    安装器 exe 所在目录（也就是用户的下载文件夹），配置里写了个随时会被
    清掉的路径。绝对路径（LUT_ROOT 那些）不受影响：join 遇到绝对段就直接换根。
    """
    p = os.path.join(BASE, *parts) if parts else BASE
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


def photo_dir(draft_id):
    """静帧转等长视频的缓存目录（图很多也很小：1080p 一张 5 秒约 1 MB）。"""
    return ensure(PHOTO_ROOT, str(draft_id))


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


def atomic_write(path, text, encoding="utf-8"):
    """先写同目录的临时文件再 os.replace —— 读的一方永远看到完整内容。

    为什么写回必须用它：草稿正文、草稿元信息、剪映的索引文件都会被「另一边」
    同时读（剪映正处于打开状态，我们写的时候它可能正好在重载草稿）。直接
    write_text 是先截断再写，这个窗口里读到的是空文件或半截 JSON —— 剪映那边
    的表现就是草稿打不开，而且事后完全查不出原因。

    临时名带 pid + 随机后缀：两个窗口同时写同一个文件时，用固定的
    `.tmp` 名字会互相把对方的临时文件搬走（实测：一边 os.replace 之后另一边
    直接 ENOENT 崩溃）。
    """
    import os
    import tempfile
    path = str(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                               suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 建出来的文件是 0600，直接 replace 会把原文件的权限一起换掉：
        # 剪映的草稿正文/索引原本是 0644，改成 0600 之后，凡是以别的身份
        # （同步盘代理、备份工具、另一账号运行的剪映）读它们的都会读不到。
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o644
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class FileLock(object):
    """跨进程独占锁（POSIX flock / Windows msvcrt）。

    用途只有一个：剪映的 root_meta_info.json 是「读—改—写」，两个窗口（或面板
    与命令行同时操作）会丢条目 —— 实测 8 个进程并发登记，最后索引里只剩 1~2 条，
    而且用同一个 .tmp 名会让其中一方 os.replace 到别人的半截文件。

    锁文件放在数据目录（不往剪映的草稿根里丢文件）。拿不到锁就等，超时返回
    False，调用方按「没能登记」如实报出来，绝不假装成功。
    """

    def __init__(self, key, timeout=15.0):
        import hashlib
        import os
        raw = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(BASE, "locks", "meta-%s.lock" % raw)
        self.timeout = float(timeout)
        self.fd = None

    def __enter__(self):
        import os
        import time
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        end = time.time() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return self
            except OSError:
                if time.time() >= end:
                    os.close(fd)
                    raise TimeoutError("等锁超时：%s" % self.path)
                time.sleep(0.05)

    def __exit__(self, *exc):
        import os
        try:
            if self.fd is not None:
                if os.name == "nt":
                    import msvcrt
                    try:
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
        finally:
            self.fd = None
        return False
