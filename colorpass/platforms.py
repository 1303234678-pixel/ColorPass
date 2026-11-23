"""平台判定的单一入口 —— 让平台分支可被测试覆盖。

剪映草稿在 Windows 与 macOS 上是两条完全不同的读取链路：
  · Windows：磁盘是密文，明文只在剪映进程内存里，靠 ScanProcessMemory 扫；
  · macOS ：磁盘是密文，密钥只在剪映进程内有效，靠注入的读取器解密。

两条链路的单元测试互相不适用，而 CI/开发机常常是 mac 却要测 Windows 语义，
所以把「现在算哪个平台」抽出来，测试可以临时覆盖，不去 monkeypatch
sys.platform（它是只读的内建属性语义，改它容易连带影响 subprocess 等模块）。
"""
import sys

# 测试专用：置为 "win32" / "darwin" 可强制走某条链路；None 表示按真实平台。
OVERRIDE = None


def is_mac():
    if OVERRIDE is not None:
        return OVERRIDE == "darwin"
    return sys.platform == "darwin"


def name():
    return OVERRIDE or sys.platform
