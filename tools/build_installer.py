"""一键打包：运行时目录 + 单文件安装器。

为什么是两个产物
----------------
* **运行时**（`dist/ColorPass/`，onedir）被安装器整个复制到用户机器上。
  面板每点一次按钮都要起一个子进程，`status` 轮询更是常态；onefile 每次启动
  都要把几十 MB 重新解包到临时目录，会明显卡顿，所以必须是 onedir。
* **安装器**（`dist/ColorPass-Setup-<ver>.exe`，onefile）只跑一次，慢一点无所谓。
  发给测试者就一个文件，双击即可 —— 这是「一键」的全部含义。

用法：
    python tools/build_installer.py            # 完整构建
    python tools/build_installer.py --setup-only   # 只重打安装器（复用已有运行时）
"""
import hashlib
import os
import subprocess
import sys
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


def _version_file(path, desc):
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
        StringStruct('OriginalFilename', 'ColorPass.exe'),
        StringStruct('ProductName', 'ColorPass'),
        StringStruct('ProductVersion', '%(ver)s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % {"v": v, "ver": __version__, "desc": desc},
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


def build_runtime(with_ffmpeg=False):
    BUILD.mkdir(exist_ok=True)
    vf = _version_file(BUILD / "version_runtime.txt", "ColorPass 运行时")
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
          # 不画界面也不处理图片，这几个包只会白占空间
          "--exclude-module", "tkinter",
          "--exclude-module", "PIL",
          "--exclude-module", "numpy",
          "--exclude-module", "unittest",
          # ffmpeg（imageio_ffmpeg 里那个 80 MB 的 exe）与 PyAV 是可选依赖：
          # 装了就用预渲染变速，没装退回占位对齐，不会失败。默认不塞进安装包，
          # 否则发给测试者的文件从 37 MB 变成 186 MB。要全量版加 --with-ffmpeg。
          *([] if with_ffmpeg else ["--exclude-module", "imageio_ffmpeg",
                                    "--exclude-module", "av"]),
          *_modules(),
          str(ROOT / "installer" / "entry_cli.py")])


def build_setup(with_ffmpeg=False):
    BUILD.mkdir(exist_ok=True)
    name = "ColorPass-Setup-%s" % __version__
    vf = _version_file(BUILD / "version_setup.txt", "ColorPass 安装程序")
    _run([sys.executable, "-m", "PyInstaller",
          "--noconfirm", "--clean", "--onefile", "--console",
          "--name", name,
          "--distpath", str(DIST), "--workpath", str(BUILD / "pyi"),
          "--specpath", str(BUILD),
          "--icon", str(ASSETS / "icon.ico"),
          "--version-file", str(vf),
          "--add-data", "%s%s%s" % (DIST / "ColorPass", SEP, "payload"),
          "--add-data", "%s%s%s" % (ROOT / "colorpass" / "panel" / "ColorPass.py",
                                    SEP, "colorpass/panel"),
          # 跟运行时同一套裁剪（tkinter 要留着，安装完成要弹结果框）。
          # 漏在这里排除的话，安装器会把 150 MB 的 ffmpeg/PyAV 又背一遍。
          "--exclude-module", "PIL",
          "--exclude-module", "numpy",
          "--exclude-module", "unittest",
          *([] if with_ffmpeg else ["--exclude-module", "imageio_ffmpeg",
                                    "--exclude-module", "av"]),
          *_modules(),
          str(ROOT / "installer" / "entry_setup.py")])
    return DIST / (name + (".exe" if os.name == "nt" else ""))


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

    with_ffmpeg = "--with-ffmpeg" in argv
    if not setup_only:
        print("== 1/2 运行时（onedir） ==")
        build_runtime(with_ffmpeg)
    if not (DIST / "ColorPass").is_dir():
        print("没有运行时目录，先完整构建一次")
        return 2

    print("== 2/2 安装器（onefile） ==")
    exe = build_setup(with_ffmpeg)

    print("\n产物：")
    print("  %-46s %6.1f MB" % (DIST / "ColorPass", _size(DIST / "ColorPass") / 1e6))
    print("  %-46s %6.1f MB" % (exe, _size(exe) / 1e6))
    if exe.is_file():
        h = hashlib.sha256(exe.read_bytes()).hexdigest()
        print("\nSHA256  %s\n        %s" % (h[:32], h[32:]))
        print("\n把这个 exe 发出去即可：双击 → 装好 → 重启达芬奇。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
