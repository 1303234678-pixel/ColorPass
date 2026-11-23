"""生成 ColorPass 的图标与横幅（纯 Pillow 画，不依赖任何图片素材）。

为什么不直接用生成式图：图标要缩到 16×16 出现在 Windows 任务栏和 exe 上，
任何渐变噪点、水印、边缘模糊都会变成一团脏点。这里用几何图形画，边缘是
抗锯齿的矢量感，任何尺寸缩下去都干净。

产物：
    assets/icon.png         1024×1024，透明圆角（深色底）
    assets/icon.ico         16/24/32/48/64/128/256 多尺寸，给 exe 与快捷方式
    assets/logo-light.png   1024×1024，浅色底（放在白底文档/README 里用）
    assets/banner.png       1280×480，图标 + 字标 + 一句话，发人测试时贴图用

运行：python tools/make_logo.py
"""
import colorsys
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 为了读到 __version__
OUT = ROOT / "assets"
S = 1024                     # 主尺寸
RADIUS = int(S * 0.225)      # 圆角半径，接近 Windows 11 图标比例
BG = (27, 27, 30, 255)       # #1B1B1E
BG_LIGHT = (246, 246, 248, 255)
BAND_TOP, BAND_BOTTOM = int(S * 0.415), int(S * 0.585)
RING_R_OUT = int(S * 0.168)
RING_W = int(S * 0.040)
WHITE = (255, 255, 255, 255)


def _band(x, w):
    """一根从红到紫的水平色带。用 HSV 走一圈，色相均匀不脏。"""
    for i in range(w):
        h = (i / max(w - 1, 1)) * 0.83       # 0=红 … 0.83=紫
        r, g, b = colorsys.hsv_to_rgb(h, 0.88, 0.98)
        yield x + i, (int(r * 255), int(g * 255), int(b * 255), 255)


def icon(bg=BG, size=S, ring=True):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * RADIUS / S), fill=bg)

    scale = size / S
    top, bottom = int(BAND_TOP * scale), int(BAND_BOTTOM * scale)
    for x, color in _band(0, size):
        d.line([(x, top), (x, bottom)], fill=color)

    if ring:
        cx = cy = size // 2
        ro, rw = int(RING_R_OUT * scale), int(RING_W * scale)
        d.ellipse([cx - ro, cy - ro, cx + ro, cy + ro], outline=WHITE, width=rw)
    return img


def _font(name, px):
    """按名字找字体。Windows 的微软雅黑与 mac 的苹方都要试 —— 缺中文的字体
    会把「剪映」画成一排豆腐块。"""
    cands = [Path("C:/Windows/Fonts") / name,
             Path("/System/Library/Fonts") / name,
             Path("/usr/share/fonts/opentype/noto") / name,
             Path("/usr/share/fonts/truetype/dejavu") / name]
    for p in cands:
        if p.is_file():
            return ImageFont.truetype(str(p), px)
    return ImageFont.load_default()


def banner(path, version=""):
    """1280×480 横幅：左图标、右字标。发人测试时一张图说清是什么。"""
    W, H = 1280, 480
    img = Image.new("RGBA", (W, H), BG)

    # 右上角的柔光：必须画在单独图层上再合成 —— ImageDraw 的 fill 是替换像素，
    # 直接画半透明圆会得到一块实心白饼（踩过一次）。
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(560, 0, -8):
        a = int(16 * (1 - r / 560))
        gd.ellipse([W - 180 - r, -r - 60, W - 180 + r, r - 60],
                   fill=(120, 120, 150, a))
    img.alpha_composite(glow)

    ic = icon(size=248)
    img.alpha_composite(ic, (96, (H - 248) // 2))

    latin = _font("segoeuib.ttf", 108)
    cjk = _font("msyh.ttc", 40)
    tiny = _font("msyh.ttc", 22)
    d = ImageDraw.Draw(img)
    d.text((392, 132), "ColorPass", font=latin, fill=(245, 245, 248, 255))

    # 主色带里取一段做分隔线，和图标呼应
    seg = Image.new("RGBA", (300, 6), (0, 0, 0, 0))
    sd = ImageDraw.Draw(seg)
    for x, c in _band(0, 300):
        sd.line([(x, 0), (x, 5)], fill=c)
    img.alpha_composite(seg, (394, 272))

    # 微软雅黑里没有 ⇄ / ↔，会渲染成豆腐块 —— 只用 →，并在下面一行把话说全
    d.text((392, 296), "剪映 → 达芬奇 → 剪映", font=cjk, fill=(178, 178, 190, 255))
    d.text((392, 352), "调完色回到剪映，原来的剪辑照样能改", font=tiny, fill=(138, 138, 150, 255))
    if version:
        d.text((392, 398), "v%s · Windows" % version, font=tiny, fill=(104, 104, 116, 255))
    img.save(path)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dark = icon()
    dark.save(OUT / "icon.png")
    icon(BG_LIGHT, ring=True).save(OUT / "logo-light.png")
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    dark.save(OUT / "icon.ico", sizes=sizes)
    try:
        from colorpass import __version__ as ver
    except Exception:
        ver = ""
    banner(OUT / "banner.png", ver)
    for p in sorted(OUT.iterdir()):
        print("%-18s %8.1f KB" % (p.name, p.stat().st_size / 1024))


if __name__ == "__main__":
    main()
