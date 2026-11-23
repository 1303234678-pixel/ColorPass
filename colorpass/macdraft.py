"""macOS 剪映草稿读取 —— 借用剪映进程内的解密能力。

为什么必须这么做（2026-11-23 在本机 11.8 上实测）：

  剪映 11.7 起把 AES 密钥从 `libvideoeditor.dylib` 挪进了进程（`lvve::
  ICryptoKeyStore`）。磁盘上的 `draft_info.json` 是密文，外部进程：
    · 调 `EncryptUtils::decrypt` 返回「成功 + 0 字节」——密钥不在库里；
    · `task_for_pid` 被拒（hardened runtime 且无 get-task-allow）；
    · `launchctl setenv` 在 macOS 27 上被系统禁止，无法全局注入环境变量。
  唯一可行的路：让剪映带着 `reader.dylib` 启动，在它进程内解密。
  密钥始终不出进程，读取器只通过本地 socket 交出明文，只读不写。

所以 macOS 的约定是「受控启动」：
  · 剪映由 ColorPass 启动（或授权重启一次）→ socket 通 → 秒级读取；
  · 剪映被用户自行启动 → socket 不通 → 明确提示「需要重启剪映一次」。
这不是降级方案，而是这条链路上唯一不破坏剪映签名、不关 SIP 的做法。
"""
import json
import os
import socket
import subprocess
import time

from . import procs

ROOT = os.path.expanduser(
    "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft")
APP = "/Applications/VideoFusion-macOS.app"
BIN = APP + "/Contents/MacOS/VideoFusion-macOS"
SUPPORT = os.path.expanduser("~/Library/Application Support/ColorPass")
SOCK = os.path.join(SUPPORT, "reader.sock")
READER_LOG = os.path.join(SUPPORT, "reader.log")
PROC_NAME = "VideoFusion-macOS"

# 主草稿的密文文件名。11.7+ 用它；更早的版本是 draft_content.json。
MAIN_FILES = ("draft_info.json", "draft_content.json")


class ReaderDown(Exception):
    """剪映在跑，但不是受控启动 —— 进程内没有读取器。"""


class NotInstalled(Exception):
    """没装剪映（或装在非标准位置）。"""


def installed():
    return os.path.isfile(BIN)


def reader_dylib():
    """读取器的位置。随包发布，安装时不需要额外动作。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "mac", "reader.dylib"),
                 os.path.join(SUPPORT, "reader.dylib")):
        if os.path.isfile(cand):
            return cand
    return ""


def pid():
    """剪映主进程 PID；没跑返回 0。

    不能按进程名找：剪映会另起几个同名的 helper（路径在 Contents/Frameworks
    下），名字一样但都不是主进程。主进程的唯一特征是可执行文件路径正好是
    BIN，所以按路径尾部匹配。

    libproc 优先，不 exec 外部命令 —— 面板从达芬奇内部 Popen 出来时 PATH
    与终端不同，`pgrep` 在那儿会拿不到结果（实测）。
    """
    found = procs.find(substr=BIN)
    if found is not None:
        return min(found) if found else 0
    try:
        out = subprocess.run(["pgrep", "-f", BIN + "$"],
                             capture_output=True, timeout=5).stdout
    except Exception:
        return 0
    for line in out.decode().split():
        if line.strip().isdigit():
            return int(line.strip())
    return 0


def running():
    return pid() > 0


def reader_ping(timeout=2.0):
    """读取器是否在位。返回 (ok, info)。"""
    if not os.path.exists(SOCK):
        return False, "no-socket"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(b"PING\n")
        head = s.makefile("rb").readline().decode("utf-8", "replace").strip()
        return head.startswith("PONG"), head
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            s.close()
        except Exception:
            pass


def decrypt(path, timeout=60.0):
    """请求剪映进程内解密一个草稿文件，返回明文字节。"""
    if not os.path.exists(SOCK):
        raise ReaderDown("剪映进程内没有读取器（剪映不是由 ColorPass 启动的）")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(("DECRYPT " + path + "\n").encode("utf-8"))
        f = s.makefile("rb")
        head = f.readline().decode("utf-8", "replace").strip()
        if head.startswith("OK "):
            n = int(head[3:])
            buf = b""
            while len(buf) < n:
                chunk = f.read(n - len(buf))
                if not chunk:
                    break
                buf += chunk
            return buf
        if head.startswith("ERR"):
            raise ReaderDown(head[4:].strip() or "解密失败")
        raise ReaderDown("读取器返回了无法识别的响应：%s" % head)
    finally:
        try:
            s.close()
        except Exception:
            pass


def main_file(folder):
    """草稿目录里的主草稿文件（密文）。"""
    for name in MAIN_FILES:
        p = os.path.join(folder, name)
        if os.path.isfile(p) and os.path.getsize(p) > 2:
            return p
    return None


def is_plain(path):
    """这份正文是不是明文（json 以 `{` 开头）。

    为什么必须区分：写回生成的新草稿正文就是明文的（让剪映自己加密）——
    在剪映打开并保存它之前，它一直是明文。这时候读草稿如果还走
    `EncryptUtils::decrypt`，对方只会回「成功 + 0 字节」，上层看到的是
    `ReaderDown: empty-result`，面板再翻译成「剪映需要重启一次」——
    用户刚写完回、什么都没动，却被要求重启剪映。
    """
    try:
        with open(str(path), "rb") as f:
            return f.read(1) == b"{"
    except OSError:
        return False


def draft_dirs():
    """草稿根下所有草稿目录，按活动时间倒序。"""
    if not os.path.isdir(ROOT):
        return []
    rows = []
    for name in os.listdir(ROOT):
        if name.startswith("."):
            continue
        folder = os.path.join(ROOT, name)
        if not os.path.isdir(folder):
            continue
        main = main_file(folder)
        if not main:
            continue
        try:
            mtime = os.path.getmtime(main)
        except OSError:
            continue
        rows.append({"name": name, "folder": folder, "main": main,
                     "mtime": mtime,
                     "plain": is_plain(main),
                     "locked": os.path.isfile(os.path.join(folder, ".locked"))})
    rows.sort(key=lambda r: (r["locked"], r["mtime"]), reverse=True)
    return rows


def current():
    """当前该读哪份草稿。

    剪映打开草稿会建 `.locked`，但异常退出可能留下残留，所以先看 locked
    再看 mtime；两者都拿不到就报没有草稿。
    """
    rows = draft_dirs()
    if not rows:
        raise NotInstalled(
            "在 %s 下没有找到任何剪映草稿。请先在剪映里新建或打开一份草稿。" % ROOT)
    return rows[0]


def identity(row, reader_pid=0):
    """拼出 draftio 需要的草稿身份（与 Windows 的 cur 同构）。"""
    folder = row["folder"]
    return {
        "name": row["name"],
        "folder": folder,
        "locked": row["locked"],
        "timeline_id": "",
        "pid": reader_pid or pid(),
        "hwnd": 0,
        "window_title": PROC_NAME,
        "platform": "darwin",
        "main_file": row["main"],
    }


def read_draft(row=None, timeout=None):
    """解密并返回 (draft, cur)。明文草稿直接读，不需要读取器。

    timeout 只影响「等读取器回话」这一步：剪映假死时默认 60 秒会让人以为
    插件卡住了，调用方可以按自己的预算收紧。
    """
    row = row or current()
    if is_plain(row["main"]):
        with open(row["main"], "rb") as f:
            raw = f.read()
    else:
        raw = decrypt(row["main"], timeout=timeout or 60.0)
    if not raw:
        raise ReaderDown("读取器没有返回内容")
    try:
        draft = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ReaderDown("解密结果不是合法 JSON：%s" % exc)
    ok, info = reader_ping()
    pid_val = 0
    if ok and info.startswith("PONG "):
        try:
            pid_val = int(info.split()[1])
        except (IndexError, ValueError):
            pid_val = 0
    cur = identity(row, pid_val)
    cur["timeline_id"] = str(draft.get("id") or "")
    return draft, cur


# ---------------------------------------------------------------- 受控启动
def stop(timeout=15.0):
    """让剪映正常退出（它会自己保存）。"""
    p = pid()
    if not p:
        return True
    procs.terminate(p)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not running():
            return True
        time.sleep(0.3)
    return False


def launch(reader_dylib):
    """带读取器启动剪映。返回是否成功起来且读取器就绪。"""
    if not os.path.isfile(reader_dylib):
        raise ReaderDown("读取器文件不存在：%s" % reader_dylib)
    env = dict(os.environ)
    env["DYLD_INSERT_LIBRARIES"] = reader_dylib
    try:
        os.makedirs(SUPPORT, exist_ok=True)
    except OSError:
        pass
    with open(os.path.join(SUPPORT, "launch.log"), "ab") as log:
        subprocess.Popen([BIN], env=env, stdout=log, stderr=log,
                         start_new_session=True)
    t0 = time.time()
    while time.time() - t0 < 60:
        ok, _ = reader_ping(timeout=1.0)
        if ok:
            return True
        time.sleep(0.5)
    return False


def ensure_reader(reader_dylib, allow_restart=True):
    """确保「剪映在跑且读取器在位」。

    返回 (状态, 说明)。状态取值：
      ready     —— 直接可用
      launched  —— 刚由我们启动，可用
      needs_restart —— 剪映在跑但不是受控启动，需要用户同意重启
      not_running   —— 剪映没开
    """
    ok, info = reader_ping()
    if ok:
        return "ready", info
    if running():
        if not allow_restart:
            return "needs_restart", (
                "剪映正在运行，但它不是由 ColorPass 启动的，进程内没有读取器。"
                "读取草稿需要重启剪映一次（剪映会自动保存当前内容）。")
        if stop() and launch(reader_dylib):
            return "launched", "已带读取器重启剪映"
        return "needs_restart", "重启剪映失败，请手动退出剪映后再试。"
    if launch(reader_dylib):
        return "launched", "已启动剪映"
    return "not_running", "没能启动剪映，请检查 %s 是否可执行。" % BIN
