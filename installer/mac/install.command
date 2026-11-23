#!/bin/bash
# ColorPass macOS 一键安装 / 更新脚本
#
# 双击运行（或终端执行 `bash 安装ColorPass.command`）。它会：
#   1. 找到本脚本所在的源码目录（不需要 cd，也不依赖当前工作目录）；
#   2. 检查 Python、达芬奇、剪映是否就绪；
#   3. 编译 macOS 读取器 reader.dylib（universal + ad-hoc 签名）；
#   4. 把面板装进达芬奇的插件目录；
#   5. 写 config.json（记下源码目录和 Python 解释器）；
#   6. 自检并给出下一步指引。
#
# 安全约定：只新增文件、不删除任何用户数据；卸载用 `python3 -m colorpass.install --remove`。

set -u

# ---------------------------------------------------------------- 定位源码目录
# 脚本自身所在目录，向上找「含 colorpass/cli.py 的目录」就是源码根。
# 两种放法都能用：放在源码根、或放在源码的 installer/mac/ 下。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SRC=""
CUR="$SCRIPT_DIR"
for _ in 1 2 3 4; do
    if [ -f "$CUR/colorpass/cli.py" ] && [ -f "$CUR/colorpass/panel/ColorPass.py" ]; then
        SRC="$CUR"; break
    fi
    CUR="$(dirname "$CUR")"
done

if [ -z "$SRC" ]; then
    echo "❌ 找不到 ColorPass 源码目录（需要 colorpass/cli.py）。"
    echo "   请把本脚本放在源码目录里，或源码目录的 installer/mac/ 下。"
    read -r -p "按回车退出…" _
    exit 1
fi

cd "$SRC" || exit 1

# ---------------------------------------------------------------- 配色与工具函数
C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[90m'; C_RST=$'\033[0m'
BOLD=$'\033[1m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓ %s%s\n' "$C_OK" "$*" "$C_RST"; }
warn() { printf '%s⚠ %s%s\n' "$C_WARN" "$*" "$C_RST"; }
err()  { printf '%s✗ %s%s\n' "$C_ERR" "$*" "$C_RST"; }
hdr()  { printf '\n%s%s%s\n' "$BOLD" "$*" "$C_RST"; }

# ---------------------------------------------------------------- 1. 环境检查
hdr "ColorPass · macOS 一键安装"
say "${C_DIM}源码目录：$SRC${C_RST}"

# Python 3
PY=""
for cand in python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
done
if [ -z "$PY" ]; then
    err "没有找到 Python 3。请先安装（https://www.python.org 或 brew install python）。"
    read -r -p "按回车退出…" _
    exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
ok "Python：${PY}（${PYVER}）"

# 达芬奇
DR_DIR="/Library/Application Support/Blackmagic Design/DaVinci Resolve"
DR_APP="/Applications/DaVinci Resolve/DaVinci Resolve.app"
if [ -d "$DR_APP" ]; then
    ok "达芬奇：已安装"
elif [ -d "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve" ]; then
    warn "达芬奇插件目录存在，但没在标准位置找到 App。装完面板后请手动确认。"
else
    warn "没找到达芬奇。插件仍会装到标准目录，但请先启动一次达芬奇让它建好目录。"
fi

# 剪映
JY_APP="/Applications/VideoFusion-macOS.app"
if [ -d "$JY_APP" ]; then
    ok "剪映：已安装（VideoFusion-macOS）"
else
    warn "没在标准位置找到剪映（VideoFusion-macOS.app）。若装在别处，读取草稿会受影响。"
fi

# ---------------------------------------------------------------- 2. 编译读取器
hdr "编译 macOS 读取器"
if command -v clang++ >/dev/null 2>&1; then
    say "clang++：$(clang++ --version 2>/dev/null | head -1)"
    if [ -f "tools/mac/reader.cpp" ]; then
        if sh tools/mac/build.sh >/dev/null 2>&1; then
            ok "reader.dylib 已编译（universal arm64+x86_64，ad-hoc 签名）"
        else
            err "编译 reader.dylib 失败。"
            say "  可手动执行：sh tools/mac/build.sh"
        fi
    else
        warn "找不到 tools/mac/reader.cpp，跳过编译（沿用已存在的 reader.dylib）。"
    fi
else
    warn "没有 clang++（未装 Xcode Command Line Tools）。"
    say "  若 colorpass/mac/reader.dylib 已存在则无需编译；"
    say "  否则请运行：xcode-select --install 后重跑本脚本。"
fi

if [ ! -f "colorpass/mac/reader.dylib" ]; then
    err "读取器 reader.dylib 缺失，安装无法完成。"
    read -r -p "按回车退出…" _
    exit 1
fi

# ---------------------------------------------------------------- 3. 安装面板 + 写配置
hdr "安装面板并写入配置"
"$PY" -m colorpass.install
RC=$?

# ---------------------------------------------------------------- 4. 自检
hdr "自检"
# 只做「不依赖剪映/达芬奇是否运行」的检查：包能导入、面板源文件在、收款码资源齐全。
# 真正的端到端验证要等用户打开达芬奇、跑一次「送入」才知道。
if "$PY" -c "import colorpass.cli, colorpass.macdraft, colorpass.draftbuild" 2>/dev/null; then
    ok "核心模块导入正常"
else
    warn "核心模块导入有告警，请留意首次使用。"
fi
if [ -f "colorpass/panel/ColorPass.py" ]; then
    ok "面板源文件就位"
else
    err "面板源文件缺失：colorpass/panel/ColorPass.py"
fi
if "$PY" -m colorpass.cli about --check >/dev/null 2>&1; then
    ok "资源文件齐全（图标 / 收款码）"
else
    warn "资源自检未通过（不影响核心功能，只影响「联系作者」窗口）。"
fi

# ---------------------------------------------------------------- 5. 收尾
hdr "完成"
if [ $RC -eq 0 ]; then
    say ""
    say "ColorPass 已安装。接下来："
    say "  1. 完全退出并重新打开达芬奇；"
    say "  2. 菜单：工作区 → Workflow Integrations → ColorPass；"
    say "  3. 首次点「送入达芬奇」时，工具会带读取器重启剪映一次（约 10 秒），之后照常使用。"
else
    say ""
    say "面板注册未完成（退出码 ${RC}）。"
    say "常见原因：达芬奇还没启动过一次（插件目录还没建）。"
    say "请先打开一次达芬奇，再重跑本脚本。"
fi
say ""
read -r -p "按回车关闭窗口…" _
exit 0
