"""一键打包：运行时目录 + 正规安装程序（Inno Setup）。

两个产物，各管一段
------------------
* **运行时**（`dist/ColorPass/`，onedir）被安装程序整个复制到用户机器上。
  面板每点一次按钮都要起一个子进程，`status` 轮询更是常态；onefile 每次启动
  都要把几十 MB 重新解包到临时目录，会明显卡顿，所以必须是 onedir。
* **安装程序**（`dist/ColorPass-Setup-<ver>.exe`）由 Inno Setup 编译：
  欢迎页 → 说明 → 进度 → 完成页，进「设置 → 应用」，带卸载程序。

为什么不再用 PyInstaller 打安装器
--------------------------------
0.5.x 及以前是「PyInstaller onefile + 控制台 + tkinter 消息框」：双击先闪一个
黑窗口，装完弹个灰框，控制面板里查不到，卸载只能靠命令行 `--uninstall`。
0.6.0 起改用 Inno Setup —— 压缩率更好（lzma2）、有真正的向导和进度、
系统里有一份正经的卸载条目。文件复制归 Inno 管，注册面板 / 写配置仍然由
`ColorPass.exe setup --bin <app>` 完成（见 colorpass/postinstall.py）。

用法：
    python tools/build_installer.py            # 完整构建（含自带 ffmpeg）
    python tools/build_installer.py --setup-only   # 只重打安装包（复用已有运行时）
    python tools/build_installer.py --no-ffmpeg    # 开发用的瘦包（不装图片/慢放，别发出去）

依赖：Inno Setup 6 的 ISCC.exe（装到默认位置即可，或用环境变量 ISCC 指定）。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import __version__  # noqa: E402

DIST = ROOT / "dist"
BUILD = ROOT / "build"
ASSETS = ROOT / "assets"
SEP = os.pathsep


def _run(args):
    print("$ " + " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, cwd=str(ROOT))


def _version_file(path, desc, original="ColorPass.exe"):
    """Windows 文件属性里显示的那一栏 —— 右键 exe 能看到版本，测试者报问题方便。"""
    nums = (__version__.split(".") + ["0", "0", "0"])[:4]
    v = tuple(int(n) if n.isdigit() else 0 for n in nums)
    path.write_text(
        """VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(v)s, prodvers=%(v)s, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
        StringStruct('CompanyName', 'ColorPass'),
        StringStruct('FileDescription', '%(desc)s'),
        StringStruct('FileVersion', '%(ver)s'),
        StringStruct('InternalName', 'ColorPass'),
        StringStruct('OriginalFilename', '%(orig)s'),
        StringStruct('ProductName', 'ColorPass'),
        StringStruct('ProductVersion', '%(ver)s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % {"v": v, "ver": __version__, "desc": desc, "orig": original},
        encoding="utf-8")
    return path


def _modules():
    """包里所有顶层模块，显式声明成 hidden-import。

    有几处是函数内 import（doctor、memscan），静态分析不一定全收；
    漏一个的单位是「运行时才炸」，而这里列一遍的成本几乎为零。
    """
    out = []
    for p in sorted((ROOT / "colorpass").glob("*.py")):
        if p.stem != "__init__":
            out += ["--hidden-import", "colorpass.%s" % p.stem]
    return out


FFMPEG_EXE = ROOT / "vendor" / "ffmpeg" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")


def _retire(path):
    """把上一次的产物挪进临时目录，而不是就地删掉。

    带 ffmpeg 之后 `dist/ColorPass` 是 150+ 个文件，直接 rmtree 会被安全软件
    或沙箱当成「批量删除」拦下来（PyInstaller 自己的 --noconfirm 也一样）。
    挪走最省事，剩下的交给系统的临时目录清理。
    """
    if not path.is_dir():
        return
    dst = Path(tempfile.mkdtemp(prefix="colorpass-old-")) / path.name
    try:
        shutil.move(str(path), str(dst))
        print("旧运行时已挪到 %s" % dst)
    except Exception as exc:  # 挪不动就交给 PyInstaller 自己覆盖
        print("旧运行时挪不动（%s），让 PyInstaller 覆盖它" % exc)


def ensure_ffmpeg():
    """把 ffmpeg 备齐（vendor 里没有就自动取一份）。

    从 0.4.2 起它是**必需**的：图片段要转码、慢放要预渲染，没它这两类素材
    直接送不进去。以前「有就用、没有就降级」的做法只会让用户看到一句他解决
    不了的提示，所以改成打进包里 —— 代价是安装器从 20 MB 变 57 MB。
    """
    if FFMPEG_EXE.is_file() and FFMPEG_EXE.stat().st_size > 1 << 20:
        return FFMPEG_EXE
    print("vendor/ffmpeg 里没有 ffmpeg，自动取一份（只需一次）…")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_fetch_ffmpeg", str(ROOT / "tools" / "fetch_ffmpeg.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ok, _m, why = mod.fetch()
    if not ok or not FFMPEG_EXE.is_file():
        raise SystemExit("拿不到 ffmpeg：%s" % why)
    return FFMPEG_EXE


def build_runtime(with_ffmpeg=True):
    BUILD.mkdir(exist_ok=True)
    vf = _version_file(BUILD / "version_runtime.txt", "ColorPass 运行时")
    pack_ff = ensure_ffmpeg() if with_ffmpeg else None
    _run([sys.executable, "-m", "PyInstaller",
          "--noconfirm", "--clean", "--onedir", "--console",
          "--name", "ColorPass",
          "--distpath", str(DIST), "--workpath", str(BUILD / "pyi"),
          "--specpath", str(BUILD),
          "--icon", str(ASSETS / "icon.ico"),
          "--version-file", str(vf),
          "--add-data", "%s%s%s" % (ROOT / "colorpass" / "templates", SEP,
                                    "colorpass/templates"),
          "--add-data", "%s%s%s" % (ROOT / "colorpass" / "panel" / "ColorPass.py",
                                    SEP, "colorpass/panel"),
          # 「联系作者」窗口里的两张收款码和窗口图标。少这个目录，窗口能开
          # 但二维码是空框 —— 打赏入口就成了摆设。
          "--add-data", "%s%s%s" % (ROOT / "colorpass" / "assets", SEP,
                                    "colorpass/assets"),
          # ffmpeg 作为**数据文件**放进 ffmpeg/ 子目录（不是 import 进来的包）：
          # 这样运行时不用装 imageio_ffmpeg，也不会被 PyInstaller 再解一遍依赖。
          *(["--add-data", "%s%s%s" % (pack_ff, SEP, "ffmpeg")] if pack_ff else []),
          # 不画界面也不处理图片，这几个包只会白占空间
          "--exclude-module", "tkinter",
          "--exclude-module", "PIL",
          "--exclude-module", "numpy",
          "--exclude-module", "unittest",
          # ffmpeg 我们自己带二进制，不需要 imageio_ffmpeg 这层 Python 包装，
          # 更不需要 PyAV（它只为「探一下有没有音轨」存在，已改成让 ffmpeg 试）。
          "--exclude-module", "imageio_ffmpeg",
          "--exclude-module", "av",
          *_modules(),
          str(ROOT / "installer" / "entry_cli.py")])


ISCC_NAMES = [
    "C:/Program Files (x86)/Inno Setup 6/ISCC.exe",
    "C:/Program Files/Inno Setup 6/ISCC.exe",
    "C:/Program Files (x86)/Inno Setup 7/ISCC.exe",
    "C:/Program Files/Inno Setup 7/ISCC.exe",
]
ISCC_HINT = ("需要 Inno Setup 6 的 ISCC.exe。装一份即可：\n"
             "  https://jrsoftware.org/isdl.php\n"
             "或用环境变量 ISCC 指向它：set ISCC=D:\\path\\ISCC.exe")


def find_iscc():
    """找 ISCC。先看环境变量，再看默认安装位置（含 6.7 起的「当前用户安装」）。"""
    env = (os.environ.get("ISCC") or "").strip('"')
    if env and Path(env).is_file():
        return Path(env)
    local = os.environ.get("LOCALAPPDATA", "")
    cands = list(ISCC_NAMES)
    if local:
        for name in ("InnoSetup6", "Inno Setup 6", "InnoSetup7", "Inno Setup 7"):
            cands.append(os.path.join(local, "Programs", name, "ISCC.exe"))
    for c in cands:
        if Path(c).is_file():
            return Path(c)
    return None


def build_setup(with_ffmpeg=True):
    """编译 Inno Setup 脚本。文件复制交给 Inno，注册交给 ColorPass.exe setup。"""
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("找不到 ISCC.exe。\n" + ISCC_HINT)
    if not (DIST / "ColorPass" / "ColorPass.exe").is_file():
        raise SystemExit("dist/ColorPass 里没有运行时，先完整构建一次")

    name = "ColorPass-Setup-%s" % __version__
    _run([iscc,
          "/DMyAppVersion=%s" % __version__,
          "/DPayload=%s" % (DIST / "ColorPass"),
          "/O%s" % DIST,
          str(ROOT / "installer" / "ColorPass.iss")])
    exe = DIST / (name + (".exe" if os.name == "nt" else ""))
    if not exe.is_file():
        raise SystemExit("编译结束但没找到 %s —— 看上面 ISCC 的输出" % exe)
    return exe


def _size(p):
    p = Path(p)
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return p.stat().st_size


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_only = "--setup-only" in argv

    if not (ASSETS / "icon.ico").is_file():
        print("缺少 assets/icon.ico，先跑 python tools/make_logo.py")
        return 2

    with_ffmpeg = "--no-ffmpeg" not in argv
    if not setup_only:
        print("== 1/2 运行时（onedir） ==")
        _retire(DIST / "ColorPass")
        build_runtime(with_ffmpeg)
    if not (DIST / "ColorPass").is_dir():
        print("没有运行时目录，先完整构建一次")
        return 2

    print("== 2/2 安装程序（Inno Setup） ==")
    exe = build_setup(with_ffmpeg)

    print("\n产物：")
    print("  %-46s %6.1f MB" % (DIST / "ColorPass", _size(DIST / "ColorPass") / 1e6))
    print("  %-46s %6.1f MB" % (exe, _size(exe) / 1e6))
    # PyInstaller 6 把 datas 放进 _internal/，运行时 sys._MEIPASS 就指向那里，
    # retime.bundled() 顺着他找 —— 所以两种落点都算通过。
    qr = DIST / "ColorPass" / "_internal" / "colorpass" / "assets" / "qr_wechat.png"
    print("  %-46s %s" % ("colorpass/assets/", "已随包" if qr.is_file() else "!! 缺失"))
    ff = None
    for rel in (("_internal", "ffmpeg", FFMPEG_EXE.name),
                ("ffmpeg", FFMPEG_EXE.name)):
        cand = DIST / "ColorPass"
        for part in rel:
            cand = cand / part
        if cand.is_file():
            ff = cand
            break
    if ff is not None:
        print("  %-46s %6.1f MB（随包自带，用户无需安装）"
              % ("_internal/ffmpeg/" + ff.name, _size(ff) / 1e6))
    else:
        print("  !! 运行时里没有 ffmpeg —— 图片段和慢放会不可用")
    if exe.is_file():
        h = hashlib.sha256(exe.read_bytes()).hexdigest()
        print("\nSHA256  %s\n        %s" % (h[:32], h[32:]))
        print("\n把这个 exe 发出去即可：双击 → 向导 → 装好 → 重启达芬奇。\n"
              "卸载走「设置 → 应用 → ColorPass」，不需要命令行参数。")
        if with_ffmpeg:
            # 更新清单：装了旧版的人靠它发现新版，所以必须和这个 exe 对得上。
            # 瘦包不生成 —— 清单指向一个不能用的包比没有清单更糟。
            print("\n== 3/3 更新清单 ==")
            subprocess.call([sys.executable, str(ROOT / "tools" / "make_manifest.py")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
