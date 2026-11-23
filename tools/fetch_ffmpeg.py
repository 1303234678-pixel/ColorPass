"""把一份 ffmpeg 收进 vendor/ffmpeg/，让它随安装包一起发出去。

为什么不再当可选依赖
--------------------
0.4.0 里 ffmpeg 是「有就用、没有就退回占位对齐」，结果两种机器的表现不一样：
装了 ffmpeg 的人看到真变速，没装的人看到尾部缺帧；图片段更是直接送不进去。
用户没法自己解决（谁会为了一个调色插件去配 PATH），所以从 0.4.2 起改成
**自带**：安装包里有，面板里就再也不该出现「找不到 ffmpeg」。

体积账
------
ffmpeg 静态构建 100 MB，zlib 压完 37 MB —— 安装器从 20 MB 变成 ~57 MB。
换掉「图片段送不进去」和「慢放只能占位」，这个代价值得。

从哪拿
------
优先级：--src 参数 → COLORPASS_FFMPEG_SRC → 已装的 imageio_ffmpeg → 官方下载。
本机开发环境里 imageio_ffmpeg 基本都在，所以通常不需要联网。

用法：
    python tools/fetch_ffmpeg.py            # 收进 vendor/（已有且校验通过就跳过）
    python tools/fetch_ffmpeg.py --force    # 重新拷一份
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "ffmpeg"
EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
TARGET = VENDOR / EXE
MANIFEST = VENDOR / "manifest.json"

# 下载兜底。用 BtbN 的 win64-gpl 静态构建（含 libx264 / prores_ks）。
# 这里不敢写死「latest」，那样每次构建拿到的东西都可能不一样；真要重建时
# 把 URL 换成具体某个 tag 的直链，并把校验值一起更新进 manifest。
FALLBACK_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/"
                "latest/ffmpeg-master-latest-win64-gpl.zip")

# 我们要的编码器/滤镜，缺一个这包就不能用。
#   prores_ks  变速预渲染（ProRes 422 10bit，调色不丢色深）
#   libx264    图片段转码（H.264，体积小、达芬奇解起来快）
#   atempo     变速时保住音高（单次只吃 0.5~2.0，超出由 retime 串联）
NEED_ENCODERS = ("prores_ks", "libx264")
NEED_FILTERS = ("atempo", "setpts")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe(path):
    """跑一遍 `ffmpeg -version`，返回 (version_string, encoders, filters)。

    光看文件大小不够：拷到一半被打断、或者拿了份 ARM 的包，大小可能正常但
    一跑就崩。这里实际起一次进程，顺便把能力清单抓出来做校验。
    """
    try:
        p = subprocess.run([str(path), "-hide_banner", "-version"],
                           capture_output=True, text=True, errors="ignore",
                           timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "", "", "跑不起来：%s" % exc
    if p.returncode != 0:
        return "", "", "", "退出码 %d：%s" % (p.returncode, (p.stderr or "")[:200])
    first = (p.stdout or "").splitlines()[0] if p.stdout else ""
    ver = first.split(" version ")[-1].split(" Copyright")[0].strip()
    enc = fil = ""
    for args, key in ((["-hide_banner", "-encoders"], "enc"),
                      (["-hide_banner", "-filters"], "fil")):
        try:
            q = subprocess.run([str(path)] + args, capture_output=True,
                               text=True, errors="ignore", timeout=60,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            q = None
        text = (q.stdout or "") if q else ""
        if key == "enc":
            enc = text
        else:
            fil = text
    return ver, enc, fil, ""


def _check(path):
    """这个 ffmpeg 能不能干我们的活。返回 (ok, info, why)。"""
    ver, enc, fil, why = _probe(path)
    if why:
        return False, {}, why
    missing = [e for e in NEED_ENCODERS if (" %s " % e) not in enc]
    missing += [f for f in NEED_FILTERS if (" %s " % f) not in fil]
    if missing:
        return False, {"version": ver}, "缺少 %s（这个构建裁剪过）" % "、".join(missing)
    return True, {"version": ver}, ""


def _candidates(src=None):
    """按优先级列出候选来源：(kind, value)。"""
    out = []
    if src:
        out.append(("file", src))
    env = os.environ.get("COLORPASS_FFMPEG_SRC") or os.environ.get("COLORPASS_FFMPEG")
    if env:
        out.append(("file", env))
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p:
            out.append(("file", p))
    except Exception:
        pass
    out.append(("url", FALLBACK_URL))
    return out


def _download(url):
    import io
    import zipfile
    print("下载 %s …" % url, flush=True)
    with urllib.request.urlopen(url, timeout=300) as r:
        blob = r.read()
    print("  %.1f MB，解包找 %s" % (len(blob) / 1e6, EXE), flush=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if name.endswith("/bin/" + EXE) or os.path.basename(name) == EXE:
                tmp = TARGET.with_suffix(".part")
                with z.open(name) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(tmp, TARGET)
                return TARGET
    raise RuntimeError("压缩包里没有 %s" % EXE)


def fetch(src=None, force=False):
    """把 ffmpeg 放到 vendor/ffmpeg/。返回 (ok, manifest, why)。"""
    VENDOR.mkdir(parents=True, exist_ok=True)
    if TARGET.is_file() and not force:
        ok, info, why = _check(TARGET)
        if ok:
            return True, _manifest(info), "已在位（要重拷加 --force）"
    for kind, value in _candidates(src):
        try:
            if kind == "file":
                if not os.path.isfile(value) or os.path.getsize(value) < 1 << 20:
                    print("跳过（不是可用的文件）：%s" % value)
                    continue
                print("从 %s 复制 …" % value, flush=True)
                tmp = TARGET.with_suffix(".part")
                shutil.copy2(value, tmp)
                os.replace(tmp, TARGET)
            else:
                if os.name != "nt":
                    print("跳过下载兜底（只对 Windows 有意义）")
                    continue
                _download(value)
        except Exception as exc:
            print("这条路不通：%s（%s）" % (value, exc))
            continue
        ok, info, why = _check(TARGET)
        if not ok:
            print("这个不能用：%s" % why)
            continue
        return True, _manifest(info, source=value), "已收进 vendor/"
    return False, {}, ("没拿到可用的 ffmpeg。装一个 imageio_ffmpeg"
                       "（pip install imageio-ffmpeg）或加 --src 指向 ffmpeg.exe")


def _manifest(info, source=""):
    """记下版本、大小、校验值 —— 下一个人/下一台机器能核对拿到的是不是同一份。"""
    m = {
        "exe": EXE,
        "bytes": TARGET.stat().st_size,
        "sha256": _sha(TARGET),
        "version": info.get("version", ""),
        "source": source or os.environ.get("COLORPASS_FFMPEG_SRC", ""),
        "need": {"encoders": list(NEED_ENCODERS), "filters": list(NEED_FILTERS)},
        "note": "二进制不进 Git（>50 MB）；重新拉取运行 python tools/fetch_ffmpeg.py",
    }
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    return m


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    src = None
    if "--src" in argv:
        i = argv.index("--src")
        if i + 1 < len(argv):
            src = argv[i + 1]
    ok, m, why = fetch(src, force="--force" in argv)
    if not ok:
        print("\n失败：" + why)
        return 1
    print("\n%s" % why)
    print("  %-8s %s" % ("版本", m.get("version", "?")))
    print("  %-8s %.1f MB" % ("体积", m.get("bytes", 0) / 1e6))
    print("  %-8s %s" % ("SHA256", m.get("sha256", "")))
    print("  %-8s %s" % ("位置", TARGET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
