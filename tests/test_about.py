"""「联系作者」窗口的回归测试。

它只有三件事必须对：邮箱写对、两张码是完整的（能扫）、开不出来时别把异常
甩给面板。
"""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import about  # noqa: E402


class TestContent(unittest.TestCase):
    def test_email(self):
        self.assertEqual(about.EMAIL, "1303234678@qq.com")
        self.assertIn("@qq.com", about.EMAIL)

    def test_both_qr_assets_exist_and_are_real_images(self):
        """图片必须随包带上；缺一张，窗口里就是一个空框。"""
        for name in ("qr_wechat.png", "qr_alipay.png"):
            p = about.asset(name)
            self.assertTrue(os.path.isfile(p), p)
            with open(p, "rb") as f:
                head = f.read(8)
            self.assertEqual(head[:4], b"\x89PNG", "%s 不是 PNG" % name)
            self.assertGreater(os.path.getsize(p), 4096, "%s 太小，可能是空图" % name)

    def test_page_is_self_contained(self):
        html = about.page()
        self.assertIn(about.EMAIL, html)
        self.assertIn("微信", html)
        self.assertIn("支付宝", html)
        self.assertIn("打赏", html)
        self.assertIn("所有的收入将用于插件的进一步优化", html)
        # 二维码以 base64 内嵌：不依赖任何外部文件，拷到哪都能开
        self.assertGreaterEqual(html.count("data:image/png;base64,"), 3)
        self.assertNotIn("file:///", html.split("<script>")[0])

    def test_page_has_no_placeholder_leak(self):
        html = about.page()
        self.assertNotIn("{email}", html)
        self.assertNotIn("{qr_wechat}", html)


class TestModes(unittest.TestCase):
    def _out(self, fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a, **kw)
        return rc, buf.getvalue()

    def test_print_mode(self):
        rc, out = self._out(about.main, ["--print"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), about.EMAIL)

    def test_check_mode_ok(self):
        rc, out = self._out(about.main, ["--check"])
        self.assertEqual(rc, 0, out)
        self.assertIn("qr_wechat.png", out)

    def test_check_mode_reports_missing(self):
        with mock.patch.object(about, "asset",
                               side_effect=lambda n: os.path.join("Z:/nope", n)):
            rc, out = self._out(about.main, ["--check"])
        self.assertEqual(rc, 2)
        self.assertIn('"ok": false', out)

    def test_popen_app_mode(self):
        """有 Edge/Chrome 时走 --app：无边框窗口，看起来像软件自己的对话框。"""
        with mock.patch.object(about, "browser", return_value="C:/fake/msedge.exe"), \
                mock.patch.object(about.subprocess, "Popen") as pop:
            ok, how = about.open_page(r"C:\tmp\a.html")
        self.assertTrue(ok)
        self.assertEqual(how, "app")
        cmd = pop.call_args[0][0]
        self.assertIn("--app=file:///C:/tmp/a.html", cmd)
        # 不能闪黑框
        self.assertEqual(pop.call_args[1]["creationflags"], 0x08000000)

    def test_popen_uses_own_profile(self):
        """必须带独立 --user-data-dir，否则 --window-size 会被已在运行的 Edge
        吃掉，窗口按上次尺寸弹（本机实测 780×440 变成 1257×1410）。"""
        with mock.patch.object(about, "browser", return_value="C:/fake/msedge.exe"), \
                mock.patch.object(about.subprocess, "Popen") as pop:
            about.open_page(r"C:\tmp\a.html")
        cmd = pop.call_args[0][0]
        self.assertTrue(any(c.startswith("--user-data-dir=") for c in cmd), cmd)

    def test_popen_suppresses_sync_prompt(self):
        """独立配置目录会让 Edge 弹「同步你的浏览数据」并把窗口整个盖住 ——
        实测只有 --disable-sync + 关掉登录推广能压住（--incognito 不管用）。"""
        with mock.patch.object(about, "browser", return_value="C:/fake/msedge.exe"), \
                mock.patch.object(about.subprocess, "Popen") as pop:
            about.open_page(r"C:\tmp\a.html")
        cmd = pop.call_args[0][0]
        self.assertIn("--disable-sync", cmd)
        self.assertIn("--no-first-run", cmd)
        feats = [c for c in cmd if c.startswith("--disable-features=")]
        self.assertEqual(len(feats), 1)
        self.assertIn("msEdgeIdentityFRE", feats[0])

    def test_window_fits_content(self):
        """窗口尺寸要装得下内容：外框减去标题栏(37)和边框(14)才是客户区。
        客户区塞不下就会像 0.4.4 那样挤出上下裁切、或者留一大片空。"""
        args = about.window_args()
        size = [a for a in args if a.startswith("--window-size=")][0]
        w, h = [int(x) for x in size.split("=")[1].split(",")]
        self.assertEqual((w, h), (about.WIN_W, about.WIN_H))
        client_w, client_h = w - 14, h - 37
        # 两张 150 码 + 16 间距 + 22 + 200 字宽；高度是标题/邮箱/线/码+注
        self.assertGreaterEqual(client_w, 316 + 22 + 200 + 20)
        self.assertGreaterEqual(client_h, 253)
        # 也不能宽到「窗口一大半是空的」
        self.assertLessEqual(client_w, 700)

    def test_falls_back_to_default_browser(self):
        """没有 Edge/Chrome 也得能看 —— 功能一样，只是少一层窗口样式。"""
        with mock.patch.object(about, "browser", return_value=""), \
                mock.patch.object(about.webbrowser, "open") as wb:
            ok, how = about.open_page(r"C:\tmp\a.html")
        self.assertTrue(ok)
        self.assertEqual(how, "browser")
        wb.assert_called_once()

    def test_never_raises_when_nothing_works(self):
        """面板点一下而已，不能因为它把面板搞崩。"""
        with mock.patch.object(about, "browser", return_value="C:/x.exe"), \
                mock.patch.object(about.subprocess, "Popen",
                                  side_effect=OSError("boom")), \
                mock.patch.object(about.webbrowser, "open",
                                  side_effect=OSError("no browser")):
            rc, out = self._out(about.main, [])
        self.assertEqual(rc, 1)
        self.assertIn(about.EMAIL, out)


if __name__ == "__main__":
    unittest.main()
