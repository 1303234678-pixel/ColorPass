#!/bin/bash
# ColorPass · macOS 预检
#
# 只读：不装东西、不改设置、不碰草稿。目的是在动任何代码之前，先把那台 Mac 的
# 真实情况拿回来 —— 系统架构、Python、ffmpeg、达芬奇版本与脚本模块位置、剪映
# 草稿目录到底长什么样、draft_content.json 是明文还是密文。
#
# 用法（在 Mac 的 Terminal 里）：
#     bash ~/Desktop/mac_probe.sh > ~/colorpass-mac.txt 2>&1
#     open ~/colorpass-mac.txt
# 然后把 colorpass-mac.txt 整段发回来。

say() { printf '\n===== %s =====\n' "$1"; }

say "1. 系统"
sw_vers 2>/dev/null
echo "arch: $(uname -m)"
df -h / | tail -1

say "2. Python"
for p in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$p" >/dev/null 2>&1; then
    echo "$p -> $("$p" -V 2>&1)"
  fi
done
python3 -c "import PIL; print('pillow', PIL.__version__)" 2>&1 | tail -1
python3 -c "import imageio_ffmpeg as m; print('imageio-ffmpeg', m.get_ffmpeg_exe())" 2>&1 | tail -1
python3 -c "import venv; print('venv ok')" 2>&1 | tail -1

say "3. ffmpeg"
command -v ffmpeg && ffmpeg -hide_banner -version 2>&1 | head -2

say "4. 达芬奇"
ls -d /Applications/DaVinci*.app 2>/dev/null
for a in /Applications/DaVinci*.app; do
  [ -d "$a" ] && echo "$a -> $(defaults read "$a/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)"
done
MOD="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
if [ -d "$MOD" ]; then echo "脚本模块: $MOD"; ls "$MOD" | head -5; else echo "缺脚本模块目录: $MOD"; fi
ls -d "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Support/Workflow Integration Plugins" 2>/dev/null
pgrep -x "DaVinci Resolve" >/dev/null 2>&1 && echo "Resolve: 正在运行" || echo "Resolve: 未运行"

say "5. 剪映"
for a in /Applications/"剪映专业版".app /Applications/JianyingPro.app /Applications/CapCut.app; do
  if [ -d "$a" ]; then
    echo "$a -> $(defaults read "$a/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)"
  fi
done
pgrep -x JianyingPro >/dev/null 2>&1 && echo "剪映: 正在运行" || echo "剪映: 未运行（预检不要求开着）"

JY="$HOME/Movies/JianyingPro/User Data"
if [ -d "$JY" ]; then echo "用户数据: $JY"; else echo "缺用户数据目录: $JY"; fi
R="$JY/Projects/com.lveditor.draft"
if [ -d "$R" ]; then
  echo "草稿数: $(ls -1 "$R" 2>/dev/null | wc -l | tr -d ' ')"
  echo "root_meta_info.json: $([ -f "$R/root_meta_info.json" ] && echo 有 || echo 无)"
  echo "最近 5 份:"; ls -t "$R" 2>/dev/null | head -5
else
  echo "缺草稿目录: $R"
fi

say "6. 最新一份草稿的文件结构 + 是否明文"
LATEST=$(ls -t "$R" 2>/dev/null | head -1)
if [ -n "$LATEST" ]; then
  echo "草稿: $LATEST"
  find "$R/$LATEST" -maxdepth 2 \( -name 'draft_content.json' -o -name '*.bak' -o -name 'draft_meta_info.json' \) 2>/dev/null | head -8
  F="$R/$LATEST/draft_content.json"
  if [ -f "$F" ]; then
    echo "draft_content.json 大小: $(stat -f%z "$F") 字节"
    echo "前 80 字节:"; head -c 80 "$F" | xxd | head -3
  fi
  ls -la "$R/$LATEST/.backup" 2>/dev/null | head -5
fi

say "7. 其他"
command -v git >/dev/null 2>&1 && git --version || echo "没有 git"
command -v brew >/dev/null 2>&1 && brew --version | head -1 || echo "没有 Homebrew"
echo "可达芬奇的用户目录: $HOME/Library/Application Support/Blackmagic Design"

say "完"
