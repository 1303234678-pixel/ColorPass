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
TARGETS = [
    os.path.join(os.environ.get("PROGRAMDATA", r"C:/ProgramData"),
                 "Blackmagic Design", "DaVinci Resolve", "Support",
                 "Workflow Integration Plugins"),
    os.path.join(os.environ.get("APPDATA", ""),
                 "Blackmagic Design", "DaVinci Resolve", "Support",
                 "Fusion", "Scripts", "Utility"),
]


def write_config(product=None, python=None):
    """记下面板要找的两个路径。失败不致命 —— 面板还有环境变量兜底。"""
    cfg = {
        "product": os.path.abspath(product or paths.PRODUCT_DIR),
        "python": python or sys.executable,
    }
    try:
        os.makedirs(paths.BASE, exist_ok=True)
        with open(CONF_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        return cfg
    except Exception as exc:
        print("写配置失败（面板可用 COLORPASS_HOME / COLORPASS_PYTHON 顶替）：", exc)
        return {}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    remove = "--remove" in argv
    paths.migrate_legacy_base()

    python = None
    if "--python" in argv:
        i = argv.index("--python")
        if i + 1 < len(argv):
            python = argv[i + 1]

    if not remove:
        if not os.path.isfile(PANEL_SRC):
            print("找不到面板源文件：", PANEL_SRC)
            return 2
        cfg = write_config(python=python)
        if cfg:
            print("已记录：源码目录 =", cfg["product"])
            print("        解释器   =", cfg["python"])

    installed = 0
    for d in TARGETS:
        if not d or not os.path.isdir(d):
            print("跳过（目录不存在）：", d)
            continue
        dst = os.path.join(d, PANEL_NAME)
        stale = os.path.join(d, LEGACY_PANEL_NAME)
        if os.path.exists(stale):
            try:
                os.remove(stale)
                print("已移除旧面板", stale)
            except OSError as exc:
                print("旧面板删不掉（请在达芬奇里确认后手动删除）：", stale, exc)
        if remove:
            if os.path.exists(dst):
                os.remove(dst)
                print("已删除", dst)
            continue
        shutil.copy2(PANEL_SRC, dst)
        installed += 1
        print("已安装", dst)

    if not remove and not installed:
        print("\n没有找到达芬奇的插件目录。请先启动一次达芬奇，再运行本命令。")
        return 1
    if not remove:
        print("\n重启达芬奇，菜单：工作区 → Workflow Integrations → ColorPass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
