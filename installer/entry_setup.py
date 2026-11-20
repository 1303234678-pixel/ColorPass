"""ColorPass 安装器入口（PyInstaller 打成单文件 exe）。

双击就装完，不需要测试者装 Python：

  1. 把随包携带的运行时（onedir 构建）复制到 %LOCALAPPDATA%\\ColorPass\\bin
  2. 把面板脚本放进达芬奇的 Workflow Integration 插件目录
  3. 写 config.json，其中 cli 指向 bin 里的 ColorPass.exe

面板读 config.json 里的 cli，子进程直接调 exe，所以本机有没有 Python 都无所谓。

参数：
  --silent      不弹结果框（给批处理用）
  --uninstall   移除面板、配置和运行时（草稿缓存与报告保留，清理用 colorpass gc）

退出码：0 成功 / 1 插件目录都没找到 / 2 安装包自身不完整
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from colorpass import __version__, install, paths  # noqa: E402

FROZEN = bool(getattr(sys, "_MEIPASS", None))
EXE_NAME = "ColorPass.exe" if paths.WINDOWS else "ColorPass"


def _res(*parts):
    """打包后从解包目录取，源码模式从仓库根取（方便本地先试一遍流程）。"""
    base = getattr(sys, "_MEIPASS", None) or str(ROOT)
    return os.path.join(base, *parts)


def _payload():
    """随包携带的运行时目录。源码模式下就是 dist/ColorPass。"""
    return _res("payload") if FROZEN else _res("dist", "ColorPass")


def _say(msg=""):
    print(msg, flush=True)


def _dialog(title, msg):
    """结束时弹一句 —— 双击运行的窗口一闪就没了，测试者根本看不到结果。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        messagebox.showinfo(title, msg, parent=root)
        root.destroy()
    except Exception:
        # 没 tkinter（或没有图形界面）就退回「按回车」，不能因此判失败
        try:
            input("\n按回车关闭…")
        except Exception:
            pass


def do_install():
    payload = _payload()
    src = os.path.join(payload, EXE_NAME)
    if not os.path.isfile(src):
        return 2, ("安装包不完整：找不到运行时 %s\n"
                   "请重新下载完整的 ColorPass-Setup-%s.exe。" % (src, __version__))

    bin_dir = paths.ensure("bin")
    try:
        shutil.copytree(payload, bin_dir, dirs_exist_ok=True)
    except Exception as exc:
        return 2, "复制运行时失败：%s\n目标：%s" % (exc, bin_dir)

    exe = os.path.join(bin_dir, EXE_NAME)
    _say("运行时：%s" % exe)

    n = install.install_panels()
    if not n:
        return 1, ("没有找到达芬奇的插件目录。\n\n"
                   "请先启动一次达芬奇（让它建好 Hello World 工程）再运行本安装程序。\n"
                   "插件目录通常是：\n  " + "\n  ".join(t for t in install.TARGETS))

    cfg = install.write_config(product=bin_dir, python="", cli=exe)
    _say("配置：%s" % install.CONF_PATH)

    msg = (
        "ColorPass %s 已安装。\n\n"
        "· 运行时：%s\n"
        "· 面板：%d 个插件目录\n"
        "· 配置：%s\n\n"
        "接下来：重启达芬奇 → 工作区 → Workflow Integrations → ColorPass。\n"
        "（菜单里没有的话，确认达芬奇版本 ≥ 18，并把「工作区」菜单展开看一眼。）"
        % (__version__, bin_dir, n, install.CONF_PATH)
    )
    if not cfg:
        msg += "\n\n注意：配置没写成功，面板会提示找不到入口 —— 用管理员身份再运行一次。"
    return 0, msg


def do_uninstall():
    removed = []
    n = install.install_panels(remove=True)
    if n:
        removed.append("%d 个面板文件" % n)
    for p in (install.CONF_PATH,):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(p)
        except OSError as exc:
            _say("删不掉 %s：%s" % (p, exc))
    bin_dir = os.path.join(paths.BASE, "bin")
    try:
        if os.path.isdir(bin_dir):
            shutil.rmtree(bin_dir)
            removed.append(bin_dir)
    except OSError as exc:
        _say("删不掉 %s（可能还有进程在用）：%s" % (bin_dir, exc))
    msg = ("已卸载 ColorPass。\n\n移除了 %d 项。\n"
           "草稿缓存与历次报告留在 %s，需要腾空间可以手动删，或装回来后运行 colorpass gc。"
           % (len(removed), paths.BASE))
    return 0, msg


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    silent = "--silent" in argv

    # 不钉成 utf-8 的话，Windows 按 cp936 写管道，重定向到文件或别的程序里
    # 就成了「°²×°³ÌÐò」这种乱码（面板那边早就踩过同一个坑）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # 不钉成 utf-8 的话，Windows 按 cp936 写管道，重定向到文件或别的程序里
    # 就成了「°²×°³ÌÐò」这种乱码（面板那边早就踩过同一个坑）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    _say("ColorPass %s · 安装程序" % __version__)
    _say("=" * 46)

    if "--uninstall" in argv:
        code, msg = do_uninstall()
    else:
        code, msg = do_install()

    _say(msg)
    if not silent:
        _dialog("ColorPass %s" % ("卸载" if "--uninstall" in argv else "安装"), msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
