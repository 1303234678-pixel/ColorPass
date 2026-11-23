"""安装与打包相关的回归测试。

这几条都对应真实踩过的坑：
  · `paths.ensure("bin")` 曾把运行时复制到「当前工作目录」，配置里写下的是
    下载文件夹里的路径 —— 用户一清空下载目录就失效；
  · 打包安装写 config 时如果照旧写 python，面板会去调一个不存在的解释器；
  · 面板必须优先用打包的 exe，否则测试者机器上没装 Python 就点不动。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import install, paths  # noqa: E402

PANEL = ROOT / "colorpass" / "panel" / "ColorPass.py"


class TestEnsure(unittest.TestCase):
    def test_relative_part_lands_under_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "BASE", tmp):
                p = paths.ensure("bin")
            self.assertEqual(os.path.dirname(p), os.path.abspath(tmp))
            self.assertTrue(os.path.isdir(p))

    def test_absolute_part_is_not_prefixed(self):
        """LUT_ROOT 那些调用传的是绝对路径，不能被 BASE 套一层。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "BASE", "/should/not/be/used"):
                p = paths.ensure(os.path.join(tmp, "luts", "d1"))
            self.assertEqual(os.path.abspath(p), os.path.abspath(os.path.join(tmp, "luts", "d1")))


class TestWriteConfig(unittest.TestCase):
    def _write(self, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "config.json")
            with mock.patch.object(install, "CONF_PATH", conf), \
                    mock.patch.object(paths, "BASE", tmp):
                cfg = install.write_config(**kw)
            with io.open(conf, encoding="utf-8") as f:
                return cfg, json.load(f)

    def test_bundled_mode_records_cli_and_no_python(self):
        """打包安装：只有 cli，没有 python —— 面板不该再去猜解释器。"""
        cfg, disk = self._write(product="/tmp/bin", python="", cli="/tmp/bin/ColorPass.exe")
        self.assertEqual(disk["cli"], os.path.abspath("/tmp/bin/ColorPass.exe"))
        self.assertNotIn("python", disk)
        self.assertEqual(cfg["product"], os.path.abspath("/tmp/bin"))

    def test_source_mode_records_product_and_python(self):
        cfg, disk = self._write(product="/tmp/src", python="/usr/bin/python3")
        self.assertIn("python", disk)
        self.assertNotIn("cli", disk)
        self.assertEqual(cfg["python"], "/usr/bin/python3")


class TestPanelPrefersCli(unittest.TestCase):
    def _load(self, conf):
        """按面板实际方式 exec（截掉末尾自启动），只取我们要看的常量。"""
        tmp = tempfile.mkdtemp(prefix="cp_panel_")
        with io.open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump(conf, f)
        src = PANEL.read_text(encoding="utf-8")
        src = src[:src.rindex("def main():")]
        env = {"COLORPASS_DATA": tmp, "COLORPASS_HOME": "", "COLORPASS_PYTHON": ""}
        ns = {"__name__": "panel_probe", "__file__": str(PANEL)}
        with mock.patch.dict(os.environ, env):
            exec(compile(src, str(PANEL), "exec"), ns)
        return ns

    def test_cli_from_config_wins(self):
        fake = os.path.join(tempfile.mkdtemp(), "ColorPass.exe")
        Path(fake).write_bytes(b"")
        ns = self._load({"product": "/tmp/bin", "cli": fake})
        self.assertEqual(ns["CLI"], fake)
        cmd, _cwd = ns["_cmd"]("status")
        self.assertEqual(cmd, [fake, "status"])
        self.assertFalse(ns["_no_backend"]())

    def test_falls_back_to_python_module(self):
        ns = self._load({"product": str(ROOT), "python": sys.executable})
        self.assertEqual(ns["CLI"], "")
        cmd, cwd = ns["_cmd"]("send")
        self.assertEqual(cmd[:3], [sys.executable, "-m", "colorpass.cli"])
        self.assertEqual(cmd[3], "send")
        self.assertEqual(cwd, str(ROOT))


class TestPostInstall(unittest.TestCase):
    """安装包复制完文件之后那一步（0.6.0 起由 Inno Setup 调 `setup --bin`）。

    这些用例守的是老坑：装完面板却没写 config，面板一打开就「找不到入口」；
    打包模式误写 python 字段，面板去调一个不存在的解释器。
    """

    def _mod(self):
        from colorpass import postinstall
        return postinstall

    def test_missing_runtime_is_reported_not_crashed(self):
        mod = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            code, msg = mod.register(tmp, say=lambda *_: None)
        self.assertEqual(code, 2)
        self.assertIn("不完整", msg)

    @unittest.skipUnless(paths.WINDOWS,
                         "这两条断言的是 Inno 安装包的 ColorPass.exe；"
                         "mac/Linux 没有这套安装器")
    def test_no_davinci_dir_returns_1_with_actionable_text(self):
        """插件目录不存在时不能装作成功 —— 用户会以为装好了，菜单里却啥也没有。"""
        mod = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "ColorPass.exe").write_bytes(b"exe")
            with mock.patch.object(mod.install, "install_panels", return_value=0):
                code, msg = mod.register(tmp, say=lambda *_: None)
        self.assertEqual(code, 1)
        self.assertIn("达芬奇", msg)

    @unittest.skipUnless(paths.WINDOWS,
                         "这两条断言的是 Inno 安装包的 ColorPass.exe；"
                         "mac/Linux 没有这套安装器")
    def test_register_writes_bundled_config_only(self):
        """打包安装只写 cli，不写 python —— 与 TestWriteConfig 的约定一致。"""
        mod = self._mod()
        from colorpass import update
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, "bin")
            bin_dir.mkdir()
            (bin_dir / "ColorPass.exe").write_bytes(b"exe")
            with mock.patch.object(mod.install, "install_panels", return_value=2), \
                    mock.patch.object(mod.install, "write_config",
                                      return_value={"cli": "x"}) as wc, \
                    mock.patch.object(update, "mark_installed"), \
                    mock.patch.object(update, "cleanup"):
                code, msg = mod.register(str(bin_dir), say=lambda *_: None)
            self.assertEqual(code, 0)
            kw = wc.call_args[1]
            self.assertEqual(kw["python"], "")
            self.assertEqual(kw["product"], str(bin_dir))
            self.assertTrue(kw["cli"].endswith("ColorPass.exe"))
            self.assertIn("已安装", msg)

    def test_unregister_removes_panels_and_config(self):
        mod = self._mod()
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp, "config.json")
            conf.write_text("{}", encoding="utf-8")
            with mock.patch.object(mod.install, "install_panels", return_value=2), \
                    mock.patch.object(mod.install, "CONF_PATH", str(conf)):
                code, msg = mod.unregister(say=lambda *_: None)
            self.assertEqual(code, 0)
            self.assertFalse(conf.exists())
            self.assertIn("2 个面板文件", msg)


class TestSetupCommand(unittest.TestCase):
    """`ColorPass.exe setup` 是安装程序唯一的入口，参数拼错就装不上面板。"""

    def test_cli_routes_setup_to_register(self):
        from colorpass import cli, postinstall
        with mock.patch.object(postinstall, "register",
                               return_value=(0, "ok")) as reg:
            rc = cli.main(["setup", "--bin", "/tmp/bin"])
        self.assertEqual(rc, 0)
        self.assertEqual(reg.call_args[0][0], "/tmp/bin")

    def test_remove_flag_routes_to_unregister(self):
        from colorpass import cli, postinstall
        with mock.patch.object(postinstall, "unregister",
                               return_value=(0, "ok")) as un:
            rc = cli.main(["setup", "--remove"])
        self.assertEqual(rc, 0)
        self.assertTrue(un.called)


@unittest.skipUnless(paths.WINDOWS,
                     "Inno Setup 只产出 Windows 安装包；mac/Linux 上没有这套安装器")
class TestInnoScript(unittest.TestCase):
    """安装脚本与 Python 侧必须约好同一件事：装到哪、调谁注册。

    这几条不是「文件存在」检查 —— 目录一旦和 postinstall.default_bin_dir()
    对不上，装完 config.json 里就会指向一个不存在的 cli，面板直接罢工；
    而这类错打包时一切正常，只有真装一遍才暴露。
    """

    def setUp(self):
        self.src = (ROOT / "installer" / "ColorPass.iss").read_text(encoding="utf-8-sig")

    def test_installs_where_postinstall_expects(self):
        """安装目录 = paths.BASE\\bin，且用 %LOCALAPPDATA% 算，不用 Shell 文件夹。

        两者被重定向的情况不一样（漫游 profile、沙箱），一旦不一致就会变成
        「程序装到 A、配置写在 B」，面板起来直接找不到入口。
        """
        from colorpass import paths, postinstall
        expected = os.path.join(paths.BASE, "bin")
        self.assertIn("DefaultDirName={code:DefaultBinDir}", self.src)
        self.assertIn("GetEnv('LOCALAPPDATA')", self.src)
        self.assertIn("GetEnv('APPDATA')", self.src)
        self.assertIn("'\\ColorPass\\bin'", self.src)
        self.assertEqual(os.path.basename(expected), "bin")
        self.assertEqual(os.path.basename(os.path.dirname(expected)), "ColorPass")
        self.assertEqual(os.path.basename(postinstall.default_bin_dir()), "bin")

    def test_calls_python_side_registration(self):
        self.assertIn("setup --bin", self.src)
        self.assertIn("setup --remove", self.src)

    def test_has_stable_appid_and_silent_support(self):
        """AppId 变了会在控制面板里多出一条、旧版卸不干净。"""
        self.assertIn("AppId={{", self.src)
        self.assertIn("PrivilegesRequired=lowest", self.src)
        self.assertIn("UninstallDisplayIcon", self.src)


if __name__ == "__main__":
    unittest.main()
