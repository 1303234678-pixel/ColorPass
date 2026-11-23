"""抓某个窗口的截图（不依赖窗口是否在最前面）。

ImageGrab 只能抓屏幕区域，窗口被挡住就抓到别的东西。这里用 PrintWindow
直接让窗口把自己画到内存位图里，全屏被遮住也能拿到正确画面。
"""
import ctypes
import sys
from ctypes import wintypes as wt

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDPIAware()


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def titles():
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def grab(hwnd):
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise ValueError("bad rect %s" % ((w, h),))
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    # 2 = PW_RENDERFULLCONTENT，Qt 的窗口必须用这个标志，否则只拿到白块
    ok = user32.PrintWindow(hwnd, mem, 2)
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    return img.convert("RGB"), ok


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "build/shot.png"
    kw = sys.argv[2] if len(sys.argv) > 2 else "ColorPass"
    hit = [(h, t) for h, t in titles() if kw in t]
    if not hit:
        print("no window matching", kw)
        for h, t in titles()[:25]:
            print(" ", t)
        return 1
    hwnd, title = hit[0]
    img, ok = grab(hwnd)
    img.save(out)
    print("saved", out, img.size, "printwindow=%s" % ok, "|", title)
    return 0


if __name__ == "__main__":
    sys.exit(main())
