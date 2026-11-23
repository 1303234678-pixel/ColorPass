"""把两张收款码截图裁成「只有二维码」的白色圆角卡片。

为什么要有这一步
---------------
微信/支付宝的收款码截图里带一堆东西：绿色/蓝色背景、「推荐使用微信支付」、
「打开支付宝[扫一扫]」、甚至品牌 logo。塞进插件里既难看又像广告。
这里只取二维码本体，套一张和参考稿一样的白色圆角卡，四周留足静区
（二维码规范要求 4 个模块，裁太紧扫码会失败）。

用法：
    python tools/make_qr_assets.py --wechat <微信截图> --alipay <支付宝截图>
产物：
    colorpass/assets/qr_wechat.png
    colorpass/assets/qr_alipay.png
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "colorpass" / "assets"
CARD = 220          # 卡片边长（与窗口里显示的像素一致，1:1 最清晰）
INSET = 11          # 卡片到二维码的留白（约 4 个模块，扫码静区）
RADIUS = 22         # 圆角
SS = 4              # 超采样倍数：先画大图再缩，圆角与二维码边缘不锯齿


def clusters(flags, gap):
    """把「密集」位置聚成簇，允许中间空 gap 个（二维码内部本来就有空行）。"""
    out, start, last = [], None, None
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            last = i
        elif start is not None and i - last > gap:
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return out


def find_qr(path):
    """在截图里找出二维码的包围盒。

    不能用「整幅图的黑块边界」：底部的品牌字和名字也够黑，会把框撑到整张图。
    只在**密集**的行/列里找，再按连通簇挑最大的那块 —— 二维码是唯一
    同时满足「宽高都是几百像素、且近乎正方形」的黑块。
    """
    im = Image.open(path).convert("L")
    w, h = im.size
    px = im.load()
    rows = [sum(1 for x in range(w) if px[x, y] < 100) for y in range(h)]
    y0, y1 = max(clusters([v > max(rows) * 0.2 for v in rows], gap=60),
                 key=lambda c: c[1] - c[0])
    band = range(y0, y1 + 1)
    cols = [sum(1 for y in band if px[x, y] < 100) for x in range(w)]
    x0, x1 = max(clusters([v > max(cols) * 0.2 for v in cols], gap=60),
                 key=lambda c: c[1] - c[0])
    return x0, y0, x1, y1


def card(src, dst):
    x0, y0, x1, y1 = find_qr(src)
    side = max(x1 - x0, y1 - y0) + 1
    if abs((x1 - x0 + 1) - (y1 - y0 + 1)) > side * 0.04:
        raise SystemExit("二维码不是正方形（%s），定位可能偏了" % src)
    im = Image.open(src).convert("RGB")
    qr = im.crop((x0, y0, x0 + side, y0 + side))

    box = CARD * SS
    canvas = Image.new("RGB", (box, box), "#1A1A1C")   # 与面板底色一致，圆角外不留白边
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([0, 0, box - 1, box - 1], radius=RADIUS * SS, fill="white")

    inner = (CARD - INSET * 2) * SS
    qr = qr.resize((inner, inner), Image.LANCZOS)
    off = (box - inner) // 2
    canvas.paste(qr, (off, off))
    canvas = canvas.resize((CARD, CARD), Image.LANCZOS)

    # 卡片外的 4 个角是深色底，PNG 需要透明才能在任意背景上贴合
    mask = Image.new("L", (CARD, CARD), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CARD - 1, CARD - 1],
                                           radius=RADIUS, fill=255)
    out = Image.new("RGBA", (CARD, CARD), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst)
    return dst, side


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wechat")
    ap.add_argument("--alipay")
    a = ap.parse_args(argv)
    for name, src in (("wechat", a.wechat), ("alipay", a.alipay)):
        if not src:
            continue
        dst, side = card(src, OUT / ("qr_%s.png" % name))
        print("%-7s 二维码 %dpx → %s" % (name, side, dst.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
