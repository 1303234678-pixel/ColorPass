"""自更新：版本比较、清单解析、下载校验、失败路径。

不联网 —— 清单和安装包都用 `file://`，跑起来和真的下载走同一段代码
（`urllib` 对 file 与 https 是同一套接口），但不会因为网不好而随机失败。
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from colorpass import update


def _uri(path):
    return Path(path).resolve().as_uri()


class TestVersion(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(update.parse_ver("v0.4.4"), (0, 4, 4))
        self.assertEqual(update.parse_ver("1.2"), (1, 2, 0))
        self.assertEqual(update.parse_ver("0.4.4-rc1"), (0, 4, 4))
        self.assertEqual(update.parse_ver(""), (0, 0, 0))

    def test_newer_is_numeric(self):
        # 字符串比较会把 0.4.10 判成比 0.4.9 旧，这是这类功能最常见的坑
        self.assertTrue(update.newer("0.4.10", "0.4.9"))
        self.assertFalse(update.newer("0.4.4", "0.4.4"))
        self.assertFalse(update.newer("0.4.3", "0.4.4"))
        self.assertTrue(update.newer("0.5", "0.4.99"))


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp_upd_")
        self._old = (update.CACHE, update.STATE, update.UPDATES, update.MANIFESTS)
        update.CACHE = os.path.join(self.d, "cache.json")
        update.STATE = os.path.join(self.d, "state.json")
        update.UPDATES = os.path.join(self.d, "updates")
        self._env = os.environ.pop("COLORPASS_MANIFEST", None)

    def tearDown(self):
        update.CACHE, update.STATE, update.UPDATES, update.MANIFESTS = self._old
        if self._env is not None:
            os.environ["COLORPASS_MANIFEST"] = self._env
        shutil.rmtree(self.d, ignore_errors=True)

    def manifest(self, url="", sha256="", size=0, version="9.0.0", raw=None):
        p = os.path.join(self.d, "version.json")
        if raw is not None:
            Path(p).write_text(raw, encoding="utf-8")
        else:
            data = {"version": version, "notes": "· 测试用",
                    "assets": {"windows": {"url": url, "sha256": sha256,
                                           "size": size}}}
            Path(p).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        update.MANIFESTS = (_uri(p),)
        return p

    def fake_exe(self, size=None):
        p = os.path.join(self.d, "ColorPass-Setup-9.0.0.exe")
        size = size or (update.MIN_SETUP + 1024)
        Path(p).write_bytes(b"x" * size)
        return p


class TestCheck(_Base):
    def test_finds_new_version(self):
        p = self.fake_exe()
        self.manifest(url=_uri(p), size=os.path.getsize(p))
        info = update.check()
        self.assertTrue(info["ok"])
        self.assertTrue(info["has_new"])
        self.assertEqual(info["latest"], "9.0.0")
        self.assertEqual(info["size"], os.path.getsize(p))

    def test_mac_never_offers_windows_installer(self):
        """macOS 上没有自动更新通道：发布产物只有 .exe。

        以前照样返回 has_new=True，面板会弹「下载并安装」，点下去先下 40MB
        再装不上。现在直接说清楚，且不联网。
        """
        p = self.fake_exe()
        self.manifest(url=_uri(p), size=os.path.getsize(p))
        from colorpass import platforms
        old = platforms.OVERRIDE
        platforms.OVERRIDE = "darwin"
        try:
            info = update.check(force=True)
        finally:
            platforms.OVERRIDE = old
        self.assertFalse(info["ok"])
        self.assertFalse(info["has_new"])
        self.assertIn("macOS", info["error"])

    def test_same_version_is_not_new(self):
        self.manifest(version=update.__version__)
        info = update.check()
        self.assertTrue(info["ok"])
        self.assertFalse(info["has_new"])

    def test_older_manifest_is_not_new(self):
        # 开发机上跑的是 0.4.4，清单还停在 0.4.3 —— 别提示「有新版」
        self.manifest(version="0.1.0")
        self.assertFalse(update.check()["has_new"])

    def test_broken_json_is_reported_not_raised(self):
        self.manifest(raw="{not json")
        info = update.check()
        self.assertFalse(info["ok"])
        self.assertTrue(info["error"])

    def test_missing_version_field(self):
        self.manifest(raw=json.dumps({"assets": {}}))
        info = update.check()
        self.assertFalse(info["ok"])

    def test_cache_is_used_within_ttl(self):
        self.manifest()
        self.assertTrue(update.check()["ok"])
        # 把清单地址换成不存在的，仍应读到缓存
        update.MANIFESTS = (_uri(os.path.join(self.d, "nope.json")),)
        info = update.check()
        self.assertTrue(info["ok"])
        self.assertTrue(info["from_cache"])

    def test_force_ignores_cache(self):
        self.manifest()
        update.check()
        update.MANIFESTS = (_uri(os.path.join(self.d, "nope.json")),)
        info = update.check(force=True)
        self.assertFalse(info["ok"])

    def test_top_level_url_fallback(self):
        p = os.path.join(self.d, "version.json")
        Path(p).write_text(json.dumps({
            "version": "9.0.0", "url": "https://example.com/a.exe",
            "sha256": "ABC", "size": 123}), encoding="utf-8")
        update.MANIFESTS = (_uri(p),)
        info = update.check()
        self.assertEqual(info["url"], "https://example.com/a.exe")
        self.assertEqual(info["sha256"], "abc")     # 统一小写，比对时才不会错

    def test_manifest_notes_newlines_are_real(self):
        """清单里「反斜杠 + n」两个字符要变成真换行。

        手工写 `--notes "· 一\\n· 二"` 时，落进 JSON 的是反斜杠加 n，
        面板弹窗里就显示成一行「…\\n· 二」。0.4.6 之前发出去的清单都是
        这个毛病，所以读端也兜一层。
        """
        self.manifest(raw=json.dumps({
            "version": "9.0.0",
            "notes": "· 一\\n· 二",
            "assets": {"windows": {"url": "u", "sha256": "", "size": 1}}}))
        info = update.check()
        self.assertEqual(info["notes"].splitlines(), ["· 一", "· 二"])


class TestSources(_Base):
    """三种清单形态都要认：contents API 的 base64 包装、releases/latest、
    以及直接返回清单的地址（仓库 raw / 本地 file / 将来的镜像）。"""

    def test_contents_api_base64_is_unwrapped(self):
        import base64
        inner = json.dumps({"version": "9.0.0", "notes": "· x",
                            "assets": {"windows": {"url": "u", "sha256": "",
                                                   "size": 1}}})
        p = os.path.join(self.d, "api.json")
        Path(p).write_text(json.dumps({
            "encoding": "base64",
            "content": base64.b64encode(inner.encode()).decode(),
        }), encoding="utf-8")
        update.MANIFESTS = (_uri(p),)
        info = update.check()
        self.assertTrue(info["ok"])
        self.assertEqual(info["latest"], "9.0.0")

    def test_release_payload_is_normalised(self):
        rel = {"tag_name": "v9.0.0",
               "html_url": "https://github.com/x/y/releases/tag/v9.0.0",
               "body": "## 修复\n- 写回名字被清空\n- 面板更快",
               "assets": [{"name": "ColorPass-Setup-9.0.0.exe",
                           "size": 58000000,
                           "browser_download_url": "https://example.com/a.exe"}]}
        p = os.path.join(self.d, "rel.json")
        Path(p).write_text(json.dumps(rel), encoding="utf-8")
        update.MANIFESTS = (_uri(p),)
        info = update.check()
        self.assertTrue(info["ok"])
        self.assertEqual(info["latest"], "9.0.0")
        self.assertEqual(info["url"], "https://example.com/a.exe")
        self.assertEqual(info["size"], 58000000)
        self.assertIn("写回名字被清空", info["notes"])
        self.assertNotIn("##", info["notes"])       # Markdown 记号要洗掉

    def test_release_without_exe_asset(self):
        # 只建了 Release 没挂附件：仍然算「有新版」，但下载地址为空 ——
        # run_install 会据此提示「去发布页手动下载」，而不是去下个空 URL
        rel = {"tag_name": "v9.0.0", "body": "x", "assets": []}
        p = os.path.join(self.d, "rel2.json")
        Path(p).write_text(json.dumps(rel), encoding="utf-8")
        update.MANIFESTS = (_uri(p),)
        info = update.check()
        self.assertTrue(info["has_new"])
        self.assertEqual(info["url"], "")
        self.assertEqual(update.run_install(quiet=True), 4)

    def test_all_sources_fail_says_so_plainly(self):
        update.MANIFESTS = (_uri(os.path.join(self.d, "nope.json")),)
        info = update.check()
        self.assertFalse(info["ok"])
        # 给用户看的这句不能是一长串 URL
        self.assertEqual(info["error"], "连不上更新服务")
        self.assertTrue(info.get("detail"))


class TestDownload(_Base):
    def test_download_and_verify(self):
        src = self.fake_exe()
        digest = hashlib.sha256(Path(src).read_bytes()).hexdigest()
        dst = os.path.join(self.d, "got.exe")
        seen = []
        out = update.download(_uri(src), dst, sha256=digest,
                              size=os.path.getsize(src),
                              on_pct=lambda got, total: seen.append((got, total)))
        self.assertEqual(out["sha256"], digest)
        self.assertEqual(os.path.getsize(dst), os.path.getsize(src))
        self.assertFalse(os.path.exists(dst + ".part"))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], os.path.getsize(src))

    def test_sha_mismatch_leaves_nothing(self):
        src = self.fake_exe()
        dst = os.path.join(self.d, "got.exe")
        with self.assertRaises(ValueError):
            update.download(_uri(src), dst, sha256="0" * 64)
        self.assertFalse(os.path.exists(dst))
        self.assertFalse(os.path.exists(dst + ".part"))

    def test_tiny_file_is_rejected(self):
        # 下到一张 HTML 错误页时不能把它当安装包跑起来
        src = os.path.join(self.d, "err.exe")
        Path(src).write_bytes(b"<html>404</html>")
        with self.assertRaises(ValueError):
            update.download(_uri(src), os.path.join(self.d, "got.exe"))


class TestRunInstall(_Base):
    def test_install_already_latest(self):
        self.manifest(version=update.__version__)
        self.assertEqual(update.run_install(quiet=True), 0)
        st = update.read_state()
        self.assertEqual(st.get("phase"), "idle")

    def test_install_without_url(self):
        self.manifest(url="")
        self.assertEqual(update.run_install(quiet=True), 4)
        self.assertEqual(update.read_state().get("phase"), "error")

    def test_install_download_failure(self):
        self.manifest(url=_uri(os.path.join(self.d, "missing.exe")))
        self.assertEqual(update.run_install(quiet=True), 5)
        st = update.read_state()
        self.assertEqual(st.get("phase"), "error")
        # 给用户看的必须是话，不是 WinError 的堆栈
        self.assertNotIn("Traceback", st.get("msg") or "")

    def test_install_happy_path_hands_over_to_installer(self):
        src = self.fake_exe()
        digest = hashlib.sha256(Path(src).read_bytes()).hexdigest()
        self.manifest(url=_uri(src), sha256=digest,
                      size=os.path.getsize(src))
        calls = []

        class FakeProc(object):
            def poll(self):
                return None          # 安装器还在跑

        def fake_detach(exe, args, log_path=""):
            calls.append((exe, list(args), log_path))
            return FakeProc()

        old = update._detach
        update._detach = fake_detach
        try:
            rc = update.run_install(quiet=True)
        finally:
            update._detach = old
        self.assertEqual(rc, 2)
        self.assertEqual(len(calls), 1)
        exe, args, log = calls[0]
        self.assertTrue(exe.endswith("ColorPass-Setup-9.0.0.exe"))
        # 静默安装失败时 install.log 是唯一线索，必须落到数据目录里
        self.assertTrue(log.endswith("install.log"))
        self.assertTrue(os.path.isfile(exe))
        # 0.6.0 起安装包是 Inno Setup 编的：静默参数是它那一套，不能再传
        # 旧的 --silent（那是我们自己的 PyInstaller 安装器认的）
        self.assertIn("/SILENT", args)
        self.assertIn("/SUPPRESSMSGBOXES", args)
        self.assertNotIn("--silent", args)
        st = update.read_state()
        self.assertEqual(st.get("phase"), "installing")
        self.assertEqual(st.get("target"), "9.0.0")


class TestInstalledRecord(_Base):
    def test_mark_and_read(self):
        p = os.path.join(self.d, "installed.json")
        old = update.INSTALLED
        update.INSTALLED = p
        try:
            self.assertEqual(update.installed_version(), "")
            update.mark_installed("9.0.0", "C:/x/bin")
            self.assertEqual(update.installed_version(), "9.0.0")
        finally:
            update.INSTALLED = old


if __name__ == "__main__":
    unittest.main()
