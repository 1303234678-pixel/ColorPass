"""测试包的入口。

这里只做一件事：**在 macOS 上跑单测时，把草稿读取/写回链路按 Windows 语义走**。

为什么必须这么做
----------------
剪映草稿在两端是两条完全不同的链路：
  · Windows：磁盘是密文，明文只在剪映进程内存里 —— 靠扫进程内存取；
  · macOS ：磁盘是密文，密钥只在剪映进程内有效 —— 靠注入读取器解密。

绝大多数用例是围绕 Windows 那条链路写的（mock `draftio._scan_open_draft`、
`probe.snapshot`，验证体积对账、stale 判据、写回后的落盘内容）。如果在 mac 上
按真实平台走，这些用例会绕过 mock 去连本机剪映：没装剪映就 `NoDraft`，装了剪映
就变成「测试结果取决于本机装了什么」—— 这是最糟的一种测试。

mac 链路本身没法进单测：它要求剪映在跑、读取器注入成功、密钥在手。那部分靠
真机联调（见 docs/MAC测试交接.md）和 tools/mac_probe.sh。需要专门验 mac 分支
的用例，自己在 setUp 里把 `platforms.OVERRIDE` 置成 "darwin"（见
test_draftbuild.TestMacInstallFromSource、test_update 里筛选 darwin 资产那条）。

为什么放在包 `__init__` 里
--------------------------
原来的做法是 conftest.py 里的 pytest fixture —— 但 CI 跑的是
`python -m unittest discover`，conftest.py 根本不会被加载，于是这段保护从来没
生效过：从 0.4.x 起 CI 上 macOS 两个作业连续 18 次红，34 个用例在 mac 上按真实
平台跑，全部撞在上面说的坑里。包 `__init__` 是 unittest 与 pytest 都会先执行的
唯一位置，所以判定搬到这里，只留一份，不再两套。

注意 discover 需要带 `-t .`（top_level_dir 指向仓库根），否则 unittest 会把
tests 目录当成顶层目录、以 `test_xxx` 的名字导入，包 `__init__` 就不会执行。
"""
import sys


def _use_windows_semantics():
    from colorpass import platforms

    platforms.OVERRIDE = "win32"


if sys.platform == "darwin":
    _use_windows_semantics()
