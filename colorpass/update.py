"""检查更新、下载安装包、交给安装器。

拆成三件事，每件都只有一条路：

  · **清单**是一个自己维护的 `version.json`，不查 GitHub API。理由很实际：
    Release 附件没挂的时候 API 返回的是空 assets，「有没有新版」根本判不出来；
    而清单里的下载地址可以随时换成任何镜像，装了旧版的用户读到的还是同一份。
  · **检查**只读清单、比版本号，缓存 12 小时。失败一律当成「不知道」，不弹窗 ——
    更新提示不该变成每次开面板都要看一眼的错误。
  · **下载与安装**跑在独立进程里。安装器要 taskkill 所有 ColorPass.exe（不杀掉
    就替换不掉 bin 里被占用的 dll），所以干活的进程必然会被它自己启动的安装器
    杀掉；面板只读它写出来的状态文件，进程没了也能继续显示进度。

清单格式（`version.json`）：:

    {
      "version": "0.4.4",
      "notes": "修了写回时名字被清空的问题",
      "page": "https://github.com/.../releases/tag/v0.4.4",
      "assets": {
        "windows": {
          "url":    "https://github.com/.../ColorPass-Setup-0.4.4.exe",
          "mirror": "",
          "sha256": "…",
          "size":   56666297
        }
      }
    }

顶层也可以直接写 `url` / `sha256` / `size`（手写清单时省事）。
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import __version__, paths, platforms

OWNER = "1303234678-pixel"
REPO = "ColorPass"
PAGE = "https://github.com/%s/%s/releases" % (OWNER, REPO)

# raw 在国内不一定通，jsDelivr 作为第二个来源。两个都失败就是「不知道」，
# 不影响任何既有的送入 / 写回流程。
# 顺序是拿真网络量出来的，不是猜的：
#   · raw.githubusercontent.com 与 jsDelivr 在国内多数网络下被直接阻断
#     （SSL UNEXPECTED_EOF，0.1 秒就断），当主源会让「检查更新」永远失败；
#   · api.github.com 反而能通（约 1 秒），所以先走它读仓库里的 version.json；
#   · 没挂 version.json 时退到 releases/latest（tag + 说明 + 附件一起给）；
#   · 后两个留给海外网络：它们更快，也能省掉 API 的 60 次/小时限流。
MANIFESTS = (
    "https://api.github.com/repos/%s/%s/contents/version.json?ref=main" % (OWNER, REPO),
    "https://api.github.com/repos/%s/%s/releases/latest" % (OWNER, REPO),
    "https://raw.githubusercontent.com/%s/%s/main/version.json" % (OWNER, REPO),
    "https://cdn.jsdelivr.net/gh/%s/%s@main/version.json" % (OWNER, REPO),
)

CACHE_TTL = 12 * 3600          # 自动检查的间隔；手动检查总是重查
CHUNK = 262144
MIN_SETUP = 1 << 20            # 小于 1 MB 的「安装包」一定是错误页

STATE = os.path.join(paths.BASE, "update.json")        # 安装进度
CACHE = os.path.join(paths.BASE, "update_cache.json")  # 上次检查结果
INSTALLED = os.path.join(paths.BASE, "installed.json")  # 安装器写的版本记录
UPDATES = os.path.join(paths.BASE, "updates")


# ---------------------------------------------------------------- 小工具

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def parse_ver(text):
    """`v0.4.4` / `0.4.4-rc1` / `0.5` 都折成 3 段整数。"""
    out = []
    body = str(text or "").strip().lstrip("vV")
    for part in body.replace("-", ".").replace("_", ".").split("."):
        num = ""
        for ch in part:
            if not ch.isdigit():
                break
            num += ch
        out.append(int(num) if num else 0)
        if len(out) == 3:
            break
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def newer(latest, mine=None):
    return parse_ver(latest) > parse_ver(mine or __version__)


def installed_version():
    """安装器写下的版本。面板脚本自带的 `__version__` 是它被复制那一刻的版本，
    更新后没重启时两者会不一致 —— 判断「装到哪一版了」要用这个。"""
    return str(_read_json(INSTALLED).get("version") or "")


def mark_installed(version, where=""):
    return _write_json(INSTALLED, {"version": version,
                                   "at": int(time.time()),
                                   "bin": where})


# ---------------------------------------------------------------- 清单

def manifest_urls():
    """COLORPASS_MANIFEST 可以指出一个镜像清单（内网、Gitee、自己的 CDN）。"""
    env = (os.environ.get("COLORPASS_MANIFEST") or "").strip()
    out, seen = [], set()
    for u in ([env] if env else []) + list(MANIFESTS):
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _get(url, timeout=6):
    req = urllib.request.Request(url, headers={
        "User-Agent": "ColorPass/%s (+%s)" % (__version__, PAGE),
        "Accept": "application/vnd.github+json",
        # 中间可能隔着 CDN，别让它把旧清单端上来
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _plain(text):
    """Release 的 body 是 Markdown，面板只能显示纯文本。"""
    lines = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln or set(ln) <= {"-", "*", "_"}:
            continue
        ln = ln.lstrip("#>").strip().replace("**", "").replace("`", "")
        if ln.startswith(("- ", "* ")):
            ln = "· " + ln[2:]
        if ln:
            lines.append(ln)
        if len(lines) == 6:
            break
    return "\n".join(lines)[:500]


def _from_release(rel):
    """把 GitHub releases/latest 归一成清单格式（没维护 version.json 时用）。"""
    win = {}
    for a in (rel.get("assets") or []):
        if str(a.get("name") or "").lower().endswith(".exe"):
            win = a
            break
    return {"version": str(rel.get("tag_name") or "").strip(" vV"),
            "notes": _plain(rel.get("body")),
            "page": rel.get("html_url") or PAGE,
            "assets": {"windows": {
                "url": win.get("browser_download_url") or "",
                "sha256": "",
                "size": win.get("size") or 0}}}


def _load(url, timeout=6):
    """读一个来源，归一成清单 dict。

    三种形态都要认：仓库里的 version.json（可能被 contents API 包了一层
    base64）、releases/latest、以及任何直接返回清单的地址。
    """
    raw = _get(url, timeout=timeout)
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise ValueError("返回的不是 JSON 对象")
    if data.get("encoding") == "base64" and data.get("content"):
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
        data = json.loads(text)
    if "tag_name" in data and "assets" in data:
        data = _from_release(data)
    if not isinstance(data, dict) or not data.get("version"):
        raise ValueError("清单里没有 version 字段")
    return data


def fetch_manifest(timeout=6):
    """按顺序试清单地址。返回 (data, source, error)。"""
    urls = manifest_urls()
    errs = []
    for url in urls:
        try:
            return _load(url, timeout=timeout), url, ""
        except urllib.error.HTTPError as exc:
            errs.append("%s：HTTP %s" % (url, exc.code))
        except Exception as exc:
            errs.append("%s：%s" % (url, type(exc).__name__))
    return {}, "", "%d 个来源都没成功（%s）" % (len(urls), "；".join(errs))


def _lines(text):
    """更新说明里的换行。

    清单手工写的时候（`--notes "· 一\\n· 二"`）很容易把「反斜杠 + n」原样
    写进去，读出来就成一行长文。老版本已经这么发出去过，所以读端也兜一下：
    真有反斜杠的场景这里不存在，说明都是给人看的中文短句。
    """
    s = str(text or "").strip()
    return s.replace("\\r\\n", "\n").replace("\\n", "\n")


def _asset(data):
    """取 Windows 包信息。缺 assets 时退回顶层字段。"""
    win = ((data.get("assets") or {}).get("windows") or {})
    if not win:
        win = {"url": data.get("url") or "",
               "sha256": data.get("sha256") or "",
               "size": data.get("size") or 0}
    win.setdefault("mirror", "")
    win["size"] = int(win.get("size") or 0)
    return win


# ---------------------------------------------------------------- 检查

def check(force=False, timeout=6):
    """看一眼有没有新版。永不抛异常 —— 失败就是 ok=False + error。"""
    res = {"ok": False, "current": __version__, "latest": "", "has_new": False,
           "url": "", "mirror": "", "sha256": "", "size": 0, "notes": "",
           "page": PAGE, "error": "", "source": "", "from_cache": False,
           "at": int(time.time())}

    # macOS 没有自动更新通道：发布产物只有 Windows 安装器（.exe）。以前这里
    # 照样返回 has_new=True，面板会弹「下载并安装」—— 点下去是下一份 40MB 的
    # exe，然后装不上。与其下错东西，不如直接说清楚。
    if platforms.is_mac():
        res["error"] = ("macOS 版暂不提供自动更新（发布包里只有 Windows 安装器）。"
                        "新版本请到 GitHub Releases 手动下载。")
        return res

    cached = {} if force else _read_json(CACHE)
    if cached.get("ok") and time.time() - (cached.get("at") or 0) < CACHE_TTL:
        res.update(cached)
        res["from_cache"] = True
    else:
        data, source, error = fetch_manifest(timeout=timeout)
        if not data:
            res["error"] = "连不上更新服务" if error else "检查更新失败"
            res["detail"] = error or ""
            return res
        a = _asset(data)
        res.update({"ok": True, "latest": str(data["version"]).strip(" vV"),
                    "url": a.get("url") or "", "mirror": a.get("mirror") or "",
                    "sha256": (a.get("sha256") or "").lower(),
                    "size": a.get("size") or 0,
                    "notes": _lines(data.get("notes")),
                    "page": str(data.get("page") or PAGE), "source": source})
        _write_json(CACHE, res)

    # 缓存里的结论会过期：用户可能已经更新过了，所以每次都按当前版本重算。
    res["current"] = __version__
    res["has_new"] = bool(res.get("latest")) and newer(res["latest"], __version__)
    return res


# ---------------------------------------------------------------- 下载

def _human(n):
    # 用 1e6：构建日志、面板提示、资源管理器之外的场合都按这个算，
    # 混着 1024 会出现「55.5 MB」和「58.2 MB」指同一个文件的怪事。
    return "%.1f MB" % (n / 1e6) if n >= 1e6 else "%d KB" % (n // 1000)


def _explain(exc):
    """把网络异常翻成用户能照着做的一句话。原始 URL 和技术名只进日志。"""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return "这个版本的安装包还没传上去，过一会儿再试"
        if exc.code in (403, 429):
            return "下载被限流了，过一会儿再试"
        return "下载地址返回 HTTP %s" % exc.code
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        if "timed out" in text.lower():
            return "下载超时（网络太慢或连不上）"
        return "连不上下载地址（%s）" % text.split(":")[0][:40]
    return str(exc)[:80]


def download(url, dest, sha256="", size=0, on_pct=None, timeout=30):
    """流式下载 + 校验。半截文件绝不留给安装器 —— 那会装出个坏运行时。

    on_pct(got, total)：total 为 0 时表示服务器没给长度。
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    h = hashlib.sha256()
    got = 0
    last = 0.0
    req = urllib.request.Request(url, headers={
        "User-Agent": "ColorPass/%s (+%s)" % (__version__, PAGE)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0) or size
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                now = time.time()
                if on_pct and now - last > 0.25:
                    last = now
                    on_pct(got, total)
        if on_pct:
            on_pct(got, size or got)
        if size and got != size:
            raise ValueError("大小不对：收到 %s，应为 %s" % (_human(got), _human(size)))
        if sha256 and h.hexdigest().lower() != sha256:
            raise ValueError("校验不通过（文件可能没下完，或被中间节点改过）")
        if got < MIN_SETUP:
            raise ValueError("文件只有 %s，不像安装包（可能下到了错误页）" % _human(got))
        os.replace(part, dest)
        return {"ok": True, "path": dest, "size": got, "sha256": h.hexdigest()}
    except Exception:
        try:
            os.remove(part)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------- 安装进度

def write_state(**kw):
    data = dict(read_state())
    data.update(kw)
    data["at"] = int(time.time())
    _write_json(STATE, data)
    return data


def read_state():
    return _read_json(STATE)


def clear_state():
    try:
        os.remove(STATE)
    except Exception:
        pass


# ---------------------------------------------------------------- 安装

def _detach(exe, args, log_path=""):
    """让安装器脱离本进程：本进程马上会被它 taskkill 掉。

    输出重定向到文件而不是 DEVNULL —— 静默安装失败时这是唯一的线索，
    用户把 install.log 发过来就能看出是文件被占还是解包失败。
    """
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    out = subprocess.DEVNULL
    if log_path:
        try:
            out = open(log_path, "w", encoding="utf-8", errors="replace")
        except Exception:
            out = subprocess.DEVNULL
    return subprocess.Popen([exe] + list(args), cwd=os.path.dirname(exe) or None,
                            close_fds=True, creationflags=flags,
                            stdin=subprocess.DEVNULL,
                            stdout=out, stderr=subprocess.STDOUT)


def run_install(timeout=6, quiet=False):
    """下载并安装最新版。全程往状态文件里写进度，返回退出码。

    退出码：0 已是最新 / 2 已开始安装 / 3 检查失败 / 4 清单缺地址 / 5 下载失败
    """
    def say(msg, **kw):
        if not quiet:
            print(msg, flush=True)
        write_state(msg=msg, **kw)

    say("正在确认版本", phase="checking", pct=0)
    info = check(timeout=timeout)
    if not info.get("ok"):
        say("检查更新失败：%s" % (info.get("error") or "未知原因"),
            phase="error", pct=0)
        return 3
    if not info.get("has_new"):
        say("已经是最新版本（v%s）" % __version__,
            phase="idle", pct=100, target=__version__)
        return 0

    latest = info["latest"]
    url = info.get("url") or ""
    if not url:
        say("清单里没有 %s 的下载地址，请到 %s 手动下载" % (latest, info.get("page")),
            phase="error", target=latest)
        return 4

    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not name.lower().endswith(".exe"):
        name = "ColorPass-Setup-%s.exe" % latest
    dest = os.path.join(UPDATES, name)

    def on_pct(got, total):
        pct = int(got * 100 / total) if total else 0
        say("正在下载 v%s · %d%%（%s）" % (latest, pct, _human(got)),
            phase="downloading", pct=pct, target=latest, path=dest,
            total=total or 0, got=got)

    cleanup(keep=name)      # 上一次留下的旧包先清掉，别越积越多
    say("正在下载 v%s" % latest, phase="downloading", pct=0, target=latest,
        url=url, size=info.get("size") or 0, path=dest)
    errs = []
    for src in [u for u in (url, info.get("mirror")) if u]:
        try:
            download(src, dest, sha256=info.get("sha256") or "",
                     size=info.get("size") or 0, on_pct=on_pct)
            errs = []
            break
        except Exception as exc:
            errs.append(_explain(exc))
            if not quiet:
                print("  %s 不行：%s" % (src, exc), flush=True)
    if errs:
        say("下载失败：%s" % errs[0], phase="error", target=latest, path="")
        return 5

    say("正在安装 v%s（达芬奇会被短暂关闭助手进程）" % latest,
        phase="installing", pct=100, target=latest, path=dest)
    log = os.path.join(paths.BASE, "install.log")
    try:
        # 0.6.0 起安装包是 Inno Setup 编的，静默参数换成它那一套：
        #   /SILENT        不显示向导，但保留进度条（用户得看见有东西在动）
        #   /SUPPRESSMSGBOXES 出错时不弹框等人点（否则会一直挂着）
        #   /NORESTART     不自己重启系统
        #   /NOCANCEL      不给「取消」—— 半装完的状态最难收拾
        proc = _detach(dest, ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                              "/NOCANCEL"], log_path=log)
    except Exception as exc:
        say("启动安装程序失败：%s" % _explain(exc), phase="error", target=latest)
        return 5
    # 安装器一起来就会 taskkill 本进程，后面的代码基本跑不到 —— 所以状态先落盘。
    # 但还是等一下：起不来的情况（被杀毒拦、包损坏）必须当场报，不能让面板
    # 干等两分钟。
    time.sleep(1.5)
    rc = proc.poll()
    if rc not in (None, 0):
        say("安装程序没跑起来（退出码 %s）。关掉达芬奇后重新点一次更新，"
            "或手动运行 %s" % (rc, dest), phase="error", target=latest, path=dest)
        return 5
    return 2


def open_page(url=None):
    import webbrowser
    try:
        webbrowser.open(url or PAGE)
        return True
    except Exception:
        return False


def cleanup(keep=""):
    """删掉下载过的安装包（几十 MB，留着没意义）。

    keep：这次要用的那一个不动。安装器自己调这个时删不掉正在运行的包
    （Windows 上文件被映射着），所以真正的清理由面板在装完之后做。
    """
    try:
        for name in os.listdir(UPDATES):
            if name == keep:
                continue
            if name.lower().endswith((".exe", ".part")):
                try:
                    os.remove(os.path.join(UPDATES, name))
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------- 入口

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check" in argv:
        print(json.dumps(check(force=True), ensure_ascii=False, indent=1))
        return 0
    if "--cached" in argv:
        print(json.dumps(check(), ensure_ascii=False, indent=1))
        return 0
    if "--install" in argv:
        return run_install()
    if "--page" in argv:
        info = check()
        return 0 if open_page(info.get("page") or PAGE) else 1
    if "--clean" in argv:
        cleanup()
        print("已清掉下载缓存")
        return 0

    info = check()
    if not info.get("ok"):
        print("检查更新失败：%s" % info.get("error"))
        print("可以到 %s 手动下载。" % PAGE)
        return 3
    if not info.get("has_new"):
        print("已经是最新版本（v%s）" % __version__)
        return 0
    print("有新版本 v%s（当前 v%s）" % (info["latest"], __version__))
    if info.get("notes"):
        print("\n" + info["notes"])
    if info.get("size"):
        print("\n安装包 %s" % _human(info["size"]))
    print("下载地址：%s" % (info.get("url") or info.get("page")))
    print("\n装的话：colorpass update --install")
    return 0


if __name__ == "__main__":
    sys.exit(main())
