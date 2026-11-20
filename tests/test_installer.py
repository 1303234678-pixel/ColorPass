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


class TestInstallerPayload(unittest.TestCase):
    def test_setup_entry_is_importable_and_has_modes(self):
        """安装器入口要能在没有达芬奇、没有剪映的机器上 import。"""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "cp_setup_entry", ROOT / "installer" / "entry_setup.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.do_install))
        self.assertTrue(callable(mod.do_uninstall))
        self.assertTrue(callable(mod.main))


if __name__ == "__main__":
    unittest.main()
