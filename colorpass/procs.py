"""进程枚举与结束 —— 不依赖 PATH，也不 exec 外部命令。

为什么不用 pgrep / ps：

  面板是从达芬奇内部 Popen 出来的，子进程的 PATH、权限、locale 都跟终端
  不一样。实测在这台机器上：终端里 `pgrep` 能拿到 Resolve 的 PID，面板里
  拿不到 —— 于是「达芬奇明明开着」被判成没启动，send 1 秒就退出（码 4）。

  libproc 就在 libSystem 里，ctypes 直接调用，不 fork 任何东西，也就没有
  PATH、locale、TCC 这些变量可被继承错。macOS 上进程的「真实身份」是
  可执行文件全路径（`proc_pidpath`），比按进程名猜更准。

Windows 不用这里，那侧继续走 tasklist。
"""
import ctypes
import os
import signal

MAXPATH = 4096

_lib = None
_unavailable = False


def _libsys():
    """libSystem 句柄；拿不到就返回 None（调用方退回外部命令）。"""
    global _lib, _unavailable
    if _lib is not None or _unavailable:
        return _lib
    try:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.proc_listallpids.restype = ctypes.c_int
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidpath.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_uint32]
        _lib = lib
    except Exception:
        _unavailable = True
        _lib = None
    return _lib


def all_pids():
    """当前所有进程 PID 集合；底层不可用时返回 None。

    进程会在两次调用之间生灭，所以按返回的个数重新申请一次缓冲区再列。
    """
    lib = _libsys()
    if lib is None:
        return None
    try:
        n = lib.proc_listallpids(None, 0)
        if n <= 0:
            return set()
        for _ in range(3):
            arr = (ctypes.c_int * n)()
            got = lib.proc_listallpids(arr, ctypes.sizeof(arr))
            if got <= 0:
                return set()
            if got < n:
                return {p for p in arr[:got] if p > 0}
            n = int(n * 1.5) + 32          # 正好塞满，说明可能被截断
        return {p for p in arr[:got] if p > 0}
    except Exception:
        return None


def exec_path(pid):
    """某进程的可执行文件全路径；拿不到返回空串。"""
    lib = _libsys()
    if lib is None:
        return ""
    try:
        buf = ctypes.create_string_buffer(MAXPATH)
        n = lib.proc_pidpath(int(pid), buf, MAXPATH)
        if n <= 0:
            return ""
        return buf.value.decode("utf-8", "replace")
    except Exception:
        return ""


def find(substr="", name="", suffix=""):
    """按可执行文件路径找进程，返回 PID 集合；底层不可用时返回 None。

    substr  —— 路径必须包含这段（区分大小写，macOS 路径本来就这么写）
    name    —— 必须是这个 basename（如 VideoFusion-macOS）
    suffix  —— 路径必须以这段结尾（防止匹配到 helper 子进程）

    「找不到」和「查不了」必须分开：前者是真空集，后者是 None —— 混在一起
    会让调用方把「探测失败」当成「程序没开」。
    """
    pids = all_pids()
    if pids is None:
        return None
    hits = set()
    for p in pids:
        path = exec_path(p)
        if not path:
            continue
        if substr and substr not in path:
            continue
        if suffix and not path.endswith(suffix):
            continue
        if name and os.path.basename(path) != name:
            continue
        hits.add(int(p))
    return hits


def terminate(pid, sig=signal.SIGTERM):
    """给进程发信号（正常退出用 TERM，它会自己保存）。返回是否发出。"""
    try:
        os.kill(int(pid), sig)
        return True
    except OSError:
        return False
