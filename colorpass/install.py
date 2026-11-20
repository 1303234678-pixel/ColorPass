"""把面板装进达芬奇（Workflow Integration Plugins），并记下装在哪。

用法：
    python -m colorpass.install                    # 安装/更新
    python -m colorpass.install --remove           # 卸载面板
    python -m colorpass.install --python <exe>     # 指定面板该用哪个解释器

为什么还要写 config.json
------------------------
面板跑在达芬奇的 Fusion 进程里，那个进程的 sys.path 上没有这个工程，所以它
必须知道两件事才能干活：**源码目录在哪**、**用哪个 python 起子进程**。

以前这两个值是硬编码在面板里的绝对路径，只有作者的机器能跑。现在改为：
install 时把实测值写进 `%LOCALAPPDATA%\\ColorPass\\config.json`，面板启动时读它。
克隆源码 / pip 安装 / 绿色解压三种情况都能对上，换机器也不用改代码。
"""
import json
import os
import shutil
import sys

from . import paths

PANEL_SRC = os.path.join(paths.PKG_DIR, "panel", "ColorPass.py")
PANEL_NAME = "ColorPass.py"
# 改名前的面板文件名：不删掉的话，达芬奇菜单里会同时出现两个入口
LEGACY_PANEL_NAME = "JYBridge.py"
CONF_PATH = os.path.join(paths.BASE, "config.json")


def _targets():
    """面板该复制到哪几个目录 —— 按平台给，别把 Windows 的 PROGRAMDATA 套到 mac。

    Workflow Integration 的插件目录三个平台各一处；Fusion 的 Utility 目录是
    老路径，留着兼容 17.x 与面板脚本方式，两处都放不会重复出现菜单项。
    """
    if paths.WINDOWS:
        return [
            os.path.join(os.environ.get("PROGRAMDATA", r"C:/ProgramData"),
                         "Blackmagic Design", "DaVinci Resolve", "Support",
                         "Workflow Integration Plugins"),
            os.path.join(os.environ.get("APPDATA", ""),
                         "Blackmagic Design", "DaVinci Resolve", "Support",
                         "Fusion", "Scripts", "Utility"),
        ]
    if paths.MACOS:
        home = os.path.expanduser("~")
        return [
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve"
            "/Support/Workflow Integration Plugins",
            os.path.join(home, "Library/Application Support/Blackmagic Design"
                                "/DaVinci Resolve/Support/Workflow Integration Plugins"),
            os.path.join(home, "Library/Application Support/Blackmagic Design"
                                "/DaVinci Resolve/Fusion/Scripts/Utility"),
        ]
    return []


TARGETS = _targets()


def write_config(product=None, python=None, cli=None):
    """记下面板要找的路径。失败不致命 —— 面板还有环境变量兜底。

    product/python：源码模式，面板用 `python -m colorpass.cli` 起子进程
    cli           ：打包模式，面板直接调那个 exe（不需要装 Python）
    传空串表示「这一项不写」—— 打包安装时 python 就不该写。
    """
    if product is None:
        product = paths.PRODUCT_DIR
    if python is None:
        python = sys.executable
    cfg = {}
    if product:
        cfg["product"] = os.path.abspath(product)
    if python:
        cfg["python"] = python
    if cli:
        cfg["cli"] = os.path.abspath(cli)
    try:
        os.makedirs(paths.BASE, exist_ok=True)
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        return cfg
    except Exception as exc:
        print("写配置失败（面板可用 COLORPASS_HOME / COLORPASS_PYTHON 顶替）：", exc)
        return {}


def install_panels(panel_src=None, remove=False, say=print):
    """把面板放进达芬奇的插件目录（或删掉）。返回装成功的目录数。

    抽成函数是因为它有两条入口：源码模式（`python -m colorpass.install`）和
    打包安装器（setup 里 import 它，面板源文件从 _MEIPASS 取）。
    """
    src = panel_src or PANEL_SRC
    done = 0
    for d in TARGETS:
        if not d or not os.path.isdir(d):
            say("跳过（目录不存在）：%s" % d)
            continue
        dst = os.path.join(d, PANEL_NAME)
        stale = os.path.join(d, LEGACY_PANEL_NAME)
        if os.path.exists(stale):
            try:
                os.remove(stale)
                say("已移除旧面板 %s" % stale)
            except OSError as exc:
                say("旧面板删不掉（请在达芬奇里确认后手动删除）：%s %s" % (stale, exc))
        if remove:
            if os.path.exists(dst):
                os.remove(dst)
                done += 1
                say("已删除 %s" % dst)
            continue
        os.makedirs(d, exist_ok=True)
        shutil.copy2(src, dst)
        done += 1
        say("已安装 %s" % dst)
    return done


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    remove = "--remove" in argv
    paths.migrate_legacy_base()

    def _arg(name):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    python = _arg("--python")
    product = _arg("--product")
    cli = _arg("--cli")

    if not remove:
        if not cli and not os.path.isfile(PANEL_SRC):
            print("找不到面板源文件：", PANEL_SRC)
            return 2
        cfg = write_config(product=product, python=python, cli=cli)
        if cfg:
            for k, v in cfg.items():
                print("已记录：%-7s = %s" % (k, v))

    installed = install_panels(remove=remove)

    if not remove and not installed:
        print("\n没有找到达芬奇的插件目录。请先启动一次达芬奇，再运行本命令。")
        return 1
    if not remove:
        print("\n重启达芬奇，菜单：工作区 → Workflow Integrations → ColorPass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
