"""联系作者 —— 邮箱 + 微信/支付宝收款码。

为什么不是画在面板里
-------------------
面板跑在达芬奇的 Fusion 里，那套 UI Manager **画不出图片**（21.2 实测：
`Label`/`Button` 的 `Icon` 属性被静默忽略，`ui.Picture` 根本不存在）。
二维码必须是像素级的，用文字排出来扫不响，所以只能另开一个窗口。

为什么不用 tkinter
-----------------
打包运行时用的解释器没带 tkinter（早先为瘦身加了 `--exclude-module tkinter`），
而且 tkinter 的排版也做不出参考稿那种卡片质感。这里改成：生成一份自包含的
HTML（二维码以 base64 内嵌，单文件、无外部依赖），用 Edge 的 `--app` 模式
打开 —— 没有地址栏和标签页，看起来就是软件自己的一个窗口。没有 Edge/Chrome
就退回默认浏览器，功能一样。

二维码图片来自 `colorpass/assets/qr_*.png`，由 tools/make_qr_assets.py 从
作者的收款码截图里裁出来：只留二维码本体，套白色圆角卡，留足静区。
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import webbrowser

from . import __version__, paths, platforms

EMAIL = "1303234678@qq.com"
TITLE = "ColorPass · 联系作者"

# 与面板同一套色值，一眼看是同一个软件
C_BG = "#1A1A1C"
C_TEXT = "#EDEDF0"
C_SUB = "#9A9AA2"
C_DIM = "#6C6C74"
C_LINE = "#2C2C31"
C_ACCENT = "#3B82F6"
C_WECHAT = "#34C759"

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE64 = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME86 = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

# macOS 上的浏览器路径（--app 无边框模式在 mac 上也生效）。
# 联系作者窗口在 mac 上同样要能开出「软件自己的窗口」，而不是退到浏览器标签页。
CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
EDGE_MAC = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<link rel="icon" href="{icon}">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html {{ height: 100%; }}
  body {{
    min-height: 100vh; height: 100vh;
    background: {bg}; color: {text};
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased; overflow: hidden;
    position: relative; user-select: none;
  }}
  /* 容器铺满视口、内容用 grid 居中：不依赖 100vh 的取整，也不依赖子元素
     实际高度 —— flex + height:100vh 在 --app 窗口里实测会贴顶。 */
  .wrap {{
    position: fixed; inset: 0;   /* fixed 才是对着视口，absolute 会跟着 100vh 走 */
    display: grid; place-content: center;
    justify-items: stretch;
    padding: 0 24px;
  }}
  header {{ display: flex; align-items: baseline; gap: 10px; }}
  h1 {{ font-size: 17px; font-weight: 600; letter-spacing: .4px; }}
  .ver {{ font-size: 11px; color: #55555C; }}
  .mail {{ margin-top: 9px; display: flex; align-items: center; gap: 9px; }}
  .mail a {{
    color: {accent}; font-size: 14px; text-decoration: none;
    border-bottom: 1px solid rgba(59,130,246,.45); padding-bottom: 1px;
  }}
  .mail a:hover {{ border-bottom-color: {accent}; }}
  .mail button {{
    font: inherit; font-size: 11px; color: {sub}; cursor: pointer;
    background: #26262A; border: 1px solid {line}; border-radius: 6px;
    padding: 4px 10px; transition: .15s;
  }}
  .mail button:hover {{ background: #2E2E33; color: {text}; }}
  .hint {{ font-size: 11px; color: {dim}; }}
  .rule {{ height: 1px; background: {line}; margin: 12px 0 14px; }}
  /* 右侧说明比二维码高，居中对齐才不会把整块往下推 */
  .body {{ display: flex; gap: 22px; align-items: center; }}
  .codes {{ display: flex; gap: 16px; }}
  figure {{ display: flex; flex-direction: column; align-items: center; }}
  figure img {{
    width: 150px; height: 150px; display: block; border-radius: 16px;
    box-shadow: 0 6px 20px rgba(0,0,0,.45);
  }}
  figcaption {{
    margin-top: 8px; display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: {sub};
  }}
  .dot {{ width: 7px; height: 7px; border-radius: 50%; display: inline-block; }}
  .wx {{ background: {wechat}; }}
  .ali {{ background: {accent}; }}
  .pitch {{ padding-top: 2px; max-width: 200px; }}
  .pitch .lead {{ font-size: 13px; color: {text}; line-height: 1.7; }}
  .pitch .sub {{ font-size: 12px; color: {sub}; line-height: 1.75; margin-top: 8px; }}
  .pitch .fine {{ font-size: 11px; color: {dim}; line-height: 1.8; margin-top: 12px; }}
</style></head>
<body><div class="wrap">
  <header><h1>联系作者</h1><span class="ver">v{version}</span></header>
  <div class="mail">
    <a href="mailto:{email}" title="点一下打开邮件客户端">{email}</a>
    <button id="cp" type="button">复制邮箱</button>
    <span class="hint">点邮箱直接发信</span>
  </div>
  <div class="rule"></div>
  <div class="body">
    <div class="codes">
      <figure><img alt="微信收款码" src="{qr_wechat}">
        <figcaption><i class="dot wx"></i>微信</figcaption></figure>
      <figure><img alt="支付宝收款码" src="{qr_alipay}">
        <figcaption><i class="dot ali"></i>支付宝</figcaption></figure>
    </div>
    <div class="pitch">
      <p class="lead">如果你觉得软件不错，<br>可以打赏支持。</p>
      <p class="sub">所有的收入将用于插件的进一步优化。</p>
      <p class="fine">微信 / 支付宝扫一扫即可，<br>金额随意，几块钱也是心意。</p>
    </div>
  </div>
</div>
<script>
  var btn = document.getElementById('cp');
  btn.onclick = function () {{
    var t = document.createElement('textarea');
    t.value = {email_js};
    document.body.appendChild(t); t.select();
    try {{ document.execCommand('copy'); btn.textContent = '已复制'; }}
    catch (e) {{ btn.textContent = '复制失败'; }}
    document.body.removeChild(t);
    setTimeout(function () {{ btn.textContent = '复制邮箱'; }}, 1500);
  }};
</script>
</body></html>
"""


def asset(name):
    """二维码图片的绝对路径（打包后从 _MEIPASS 取）。"""
    base = os.path.join(paths.PKG_DIR, "assets")
    if not os.path.isdir(base):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = os.path.join(meipass, "colorpass", "assets")
    return os.path.join(base, name)


def _data_uri(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def page():
    """渲染 HTML。图片以 base64 内嵌 —— 单文件，拷到哪都能开。"""
    return HTML.format(
        title=TITLE, version=__version__, bg=C_BG, text=C_TEXT, sub=C_SUB, dim=C_DIM,
        line=C_LINE, accent=C_ACCENT, wechat=C_WECHAT,
        email=EMAIL, email_js=json.dumps(EMAIL),
        icon=_data_uri(asset("app_icon.png")),
        qr_wechat=_data_uri(asset("qr_wechat.png")),
        qr_alipay=_data_uri(asset("qr_alipay.png")),
    )


def browser():
    """能开无边框窗口的浏览器（优先 Edge，其次 Chrome）。没有就返回空串。"""
    for p in (EDGE, EDGE64, CHROME, CHROME86, CHROME_MAC, EDGE_MAC):
        if os.path.isfile(p):
            return p
    return ""


# 窗口尺寸（CSS px）。注意这是**外框**：Chromium 的 --app 窗口带标题栏和一圈
# 边框，本机 620×330 实测客户区只有 606×293（标题栏 37、边框 14）。内容按客户区
# 排版，改这里要同步改 CSS，否则会挤成两行或留一大片空。
# 538×253 是「两张 150 码 + 右侧三句话」的实测内容尺寸。
WIN_W, WIN_H = 620, 330

# 专用配置目录。必须独立：Edge 已经在跑的时候，新起的 --app 会被并进那个实例，
# --window-size / --window-position 全部失效，窗口按上次的尺寸弹（本机实测
# 请求 780×440 得到的是 1257×1410 像素，贴左上角）—— 用户看到的就是「太大了」。
PROFILE_DIR = os.path.join(paths.BASE, "about-profile")

# 独立配置目录的副作用：Edge 把它当新用户，第一次会弹「我们正在你的所有设备上
# 同步你的浏览数据」（本机实测，会把整个窗口盖住）。这三个开关正是压它的：
#   --disable-sync + 关掉 msEdgeIdentityFRE / msEdgeSyncPromo（Edge 的登录推广）
#   --no-first-run/--no-default-browser-check 关掉「设为默认浏览器」那一套
# 试过 --incognito，压不住，别再加回来。
QUIET_ARGS = ["--no-first-run", "--no-default-browser-check", "--disable-sync",
              "--disable-features=msEdgeIdentityFRE,msEdgeSyncPromo,"
              "msEdgeSyncPromoAlert"]


def _screen_scale():
    """系统缩放比例（150% → 1.5）。--window-size 的单位是 DIP，屏幕坐标是像素，
    居中换算要用它。拿不到就按 1.0 算。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        for fn in ("GetDpiForSystem",):
            f = getattr(user32, fn, None)
            if f:
                f.restype = ctypes.c_uint
                dpi = f()
                if dpi:
                    return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _work_area():
    """主屏可用区（不含任务栏），像素。拿不到返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return None


def window_args():
    """尺寸 + 位置。位置按缩放换算后居中，略偏上（对话框的视觉重心）。"""
    args = ["--window-size=%d,%d" % (WIN_W, WIN_H)]
    area = _work_area()
    if area:
        left, top, w, h = area
        s = _screen_scale() or 1.0
        x = left + max(0, int(w / s - WIN_W) // 2)
        y = top + max(0, int(h / s - WIN_H) // 2 - 24)
        args.append("--window-position=%d,%d" % (x, y))
    return args


def open_page(html_path):
    """打开窗口。返回 (ok, how)。

    `--app` 是 Chromium 的无边框模式：没有地址栏、没有标签页，只剩一个窗口，
    看起来就是软件弹的对话框。拿不到这种浏览器时退回默认浏览器打开同一份
    HTML —— 少一层窗口样式，功能一样。
    """
    url = "file:///" + html_path.replace("\\", "/")
    exe = browser()
    why = "没有找到 Edge/Chrome"
    if exe:
        try:
            try:
                os.makedirs(PROFILE_DIR, exist_ok=True)
            except Exception:
                pass
            # creationflags（CREATE_NO_WINDOW）只在 Windows 有意义；POSIX 上
            # 传了会直接抛 ValueError，把本该能开的窗口打成「打不开」。所以
            # 按平台给参数：Windows 压掉黑框，mac/Linux 用 start_new_session
            # 让浏览器独立于面板进程。平台判定走 platforms.is_mac()，测试能
            # 用 OVERRIDE 强制 Windows 语义（本机是 mac，直接看 sys.platform
            # 会让「不闪黑框」这条用例跑不成）。
            kw = {}
            if not platforms.is_mac():
                kw["creationflags"] = 0x08000000
            else:
                kw["start_new_session"] = True
            subprocess.Popen([exe, "--app=" + url,
                              "--user-data-dir=" + PROFILE_DIR]
                             + QUIET_ARGS + window_args(), **kw)
            return True, "app"
        except Exception as exc:
            why = str(exc)
    try:
        webbrowser.open(url)
        return True, "browser"
    except Exception as exc:
        return False, "%s；%s" % (why, exc)


def main(argv=None):
    """打开「联系作者」。开不了图形窗口时退化成打印邮箱，绝不抛异常。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--print" in argv:
        print(EMAIL)
        return 0
    if "--check" in argv:
        # 打包后验产物用：图片没跟着进运行时，窗口会开成两个破图
        out = {"ok": True, "email": EMAIL, "assets": [], "browser": browser()}
        for name in ("qr_wechat.png", "qr_alipay.png"):
            p = asset(name)
            ok = os.path.isfile(p)
            out["assets"].append({"path": p, "ok": ok,
                                  "bytes": os.path.getsize(p) if ok else 0})
            out["ok"] = out["ok"] and ok
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["ok"] else 2
    try:
        html = page()
        fd, path = tempfile.mkstemp(prefix="colorpass-about-", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as exc:
        print("打不开窗口（%s: %s）\n邮箱：%s" % (type(exc).__name__, exc, EMAIL))
        return 1
    ok, how = open_page(path)
    if not ok:
        print("打不开窗口（%s）\n邮箱：%s" % (how, EMAIL))
        return 1
    print("已打开联系作者窗口。邮箱：%s" % EMAIL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
