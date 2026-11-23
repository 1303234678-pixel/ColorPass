"""生成 version.json（自更新用的清单）。

发版时最后一步：算安装包的 SHA256 与体积，把版本号和说明写进清单。
清单跟着源码一起推，装了旧版的用户下次开面板就能看到「有新版」。

    python tools/make_manifest.py                    # 用 dist 里最新的安装包
    python tools/make_manifest.py --notes "修了…"     # 手写更新说明
    python tools/make_manifest.py --exe path/to.exe
    python tools/make_manifest.py --mac dist/ColorPass-X.Y.Z-mac.zip

说明不传时会从 CHANGELOG.md 里取该版本的第一段（有这个文件的话）。

`--mac` 会在清单里补一个 `assets.darwin`（url / sha256 / size / format）。
它本身不影响 Windows 更新；只是把「macOS 该下哪个文件、校验值是多少」提前
钉在清单里 —— macOS 侧的更新逻辑（当前明确不支持）以后照这个字段实现即可，
不用再改一次清单格式。
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT = ROOT / "version.json"
OWNER, REPO = "1303234678-pixel", "ColorPass"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version():
    """从包里读，不从命令行读 —— 版本号只有一个来源。"""
    src = (ROOT / "colorpass" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    if not m:
        raise SystemExit("读不到 colorpass/__init__.py 里的 __version__")
    return m.group(1)


def find_exe(explicit=None):
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit("找不到安装包：%s" % p)
        return p
    cands = sorted(DIST.glob("ColorPass-Setup-*.exe"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        raise SystemExit("dist 里没有 ColorPass-Setup-*.exe，先跑 build_installer.py")
    return cands[0]


def notes_from_changelog(ver):
    p = ROOT / "CHANGELOG.md"
    if not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8")
    # 取 "## 0.4.4" 到下一个 "## " 之间的正文
    m = re.search(r"^##\s*v?%s\b.*?$(.*?)(?=^##\s|\Z)" % re.escape(ver),
                  text, re.M | re.S)
    if not m:
        return ""
    lines = [ln.strip() for ln in m.group(1).strip().splitlines()]
    keep = []
    for ln in lines:
        if not ln:
            continue
        keep.append(re.sub(r"^[-*]\s*", "· ", ln))
        if len(keep) == 4:
            break
    return "\n".join(keep)


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 version.json")
    ap.add_argument("--exe", help="安装包路径（默认 dist 里最新的）")
    ap.add_argument("--notes", default="", help="更新说明（几行短句）")
    ap.add_argument("--mirror", default="", help="备用下载地址（可选）")
    ap.add_argument("--page", default="", help="发布页地址（可选）")
    ap.add_argument("--mac", default="",
                    help="macOS 产物（源码 zip，含 reader.dylib）；给了才写 assets.darwin")
    a = ap.parse_args(argv)

    ver = version()
    exe = find_exe(a.exe)
    # `--notes "· 一\n· 二"` 从 shell 传进来的是字面反斜杠加 n（bash 在双引号里
    # 不转义），直接存进 JSON 会在面板弹窗里原样显示成一行「…\n· 二」。
    # 这里把两种写法都归一成真换行；说明里本来就要写 \n 字样的场合没有。
    notes = (a.notes.strip().replace("\\r\\n", "\n").replace("\\n", "\n")
             or notes_from_changelog(ver) or "修复与稳定性改进。")
    url = ("https://github.com/%s/%s/releases/download/v%s/%s"
           % (OWNER, REPO, ver, exe.name))
    data = {
        "version": ver,
        "notes": notes,
        "page": a.page or "https://github.com/%s/%s/releases/tag/v%s"
        % (OWNER, REPO, ver),
        "assets": {
            "windows": {
                "url": url,
                "mirror": a.mirror.strip(),
                "sha256": sha256(exe),
                "size": exe.stat().st_size,
            }
        },
    }
    if a.mac.strip():
        mac = Path(a.mac.strip())
        if not mac.is_file():
            raise SystemExit("找不到 macOS 产物：%s" % mac)
        data["assets"]["darwin"] = {
            "url": ("https://github.com/%s/%s/releases/download/v%s/%s"
                    % (OWNER, REPO, ver, mac.name)),
            "mirror": a.mirror.strip(),
            "sha256": sha256(mac),
            "size": mac.stat().st_size,
            # 格式决定下载后怎么解：zip 直接解到新目录，再跑里面的
            # installer/mac/install.command。以后若改成签名 dmg，改这个字段。
            "format": "zip",
            "install": "解压后双击 installer/mac/install.command",
        }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("version.json → v%s  %s  %.1f MB"
          % (ver, data["assets"]["windows"]["sha256"][:12],
             exe.stat().st_size / 1e6))
    print("  下载地址 %s" % url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
