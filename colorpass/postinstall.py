"""安装包「落盘之后」的那一步：把面板注册进达芬奇、写配置、记版本。

为什么要单独成模块
------------------
0.6.0 起安装包交给 Inno Setup 生成（欢迎页、进度条、完成页、控制面板里的
卸载条目都由它负责），复制文件不再归我们管。但还有三件事必须在 Python 侧做：

  1. 往达芬奇的插件目录放面板 —— 目录清单在 `install.TARGETS` 里，
     ProgramData 与 Fusion Utility 两处、大小写和空格都不能写错；
  2. 写 `config.json` —— 面板启动时靠它找到 cli，键名和写法和源码模式
     必须逐字一致；
  3. 记下「装到哪一版」—— 自更新靠它判断这次安装有没有落地。

在 .iss 的 Pascal 里把这套规则重写一遍，等于同一份知识维护两个地方，
迟早对不上（面板路径改一次就要同步改两遍）。所以分工是：

    安装程序只负责复制文件
    复制完调 `ColorPass.exe setup --bin "<app>"`
    卸载前调 `ColorPass.exe setup --remove`

返回码沿用旧安装器的约定：0 成功 / 1 没找到达芬奇插件目录 / 2 运行时缺失。
"""
import os

from . import __version__, install, paths

EXE_NAME = "ColorPass.exe" if paths.WINDOWS else "ColorPass"


def default_bin_dir():
    """运行时装在哪 —— 必须和安装程序里的 `{app}` 一致。"""
    return os.path.join(paths.BASE, "bin")


def register(bin_dir="", say=print):
    """注册面板 + 写配置。返回 (退出码, 给人看的一段话)。"""
    where = os.path.abspath(bin_dir or default_bin_dir())
    exe = os.path.join(where, EXE_NAME)
    if not os.path.isfile(exe):
        return 2, ("安装不完整：找不到运行时 %s\n"
                   "请重新下载完整的安装包再运行一次。" % exe)

    n = install.install_panels(say=say)
    if not n:
        return 1, ("没有找到达芬奇的插件目录。\n\n"
                   "文件已经放好了，但面板还没地方装 —— 请先启动一次达芬奇"
                   "（让它建好 Hello World 工程），然后重新运行这个安装包。\n"
                   "插件目录通常是：\n  " + "\n  ".join(t for t in install.TARGETS))

    cfg = install.write_config(product=where, python="", cli=exe)
    say("配置：%s" % install.CONF_PATH)

    # 记下装到哪一版了。面板靠它判断自更新有没有装完 —— 面板脚本自带的
    # 版本号是它被复制那一刻的，更新后没重启达芬奇时两者还不一样。
    try:
        from . import update as _upd
        _upd.mark_installed(__version__, where)
        _upd.cleanup()          # 装完就把下载的包删掉，几十 MB 留着没意义
    except Exception as exc:
        say("安装记录没写成功：%r" % (exc,))

    msg = ("ColorPass %s 已安装。\n\n"
           "· 运行时：%s\n"
           "· 面板：%d 个插件目录\n"
           "· 配置：%s\n\n"
           "接下来：重启达芬奇 → 工作区 → Workflow Integrations → ColorPass。"
           % (__version__, where, n, install.CONF_PATH))
    if not cfg:
        msg += ("\n\n注意：配置没写成功，面板会提示找不到入口 —— "
                "用管理员身份再运行一次安装包。")
    return 0, msg


def unregister(say=print):
    """移除面板与配置。运行时目录交给卸载程序删，数据（缓存/报告）保留。

    顺序很重要：先删面板，再删配置 —— 反过来的话中途失败会留下一个
    「配置说装了、菜单里却没有」的半残状态。
    """
    n = install.install_panels(remove=True, say=say)
    removed = []
    if n:
        removed.append("%d 个面板文件" % n)
    try:
        if os.path.exists(install.CONF_PATH):
            os.remove(install.CONF_PATH)
            removed.append("config.json")
    except OSError as exc:
        say("删不掉 %s：%s" % (install.CONF_PATH, exc))
    return 0, ("已移除 %d 项（%s）。\n\n"
               "草稿缓存与历次报告留在 %s，需要腾空间可以手动删，"
               "或重装后运行 colorpass gc。"
               % (len(removed), "、".join(removed) or "无", paths.BASE))
