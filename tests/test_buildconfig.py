# -*- coding: utf-8 -*-
"""构建配置单元测试（0.3.0）：图标源 PNG 存在、.ico 生成、spec 含图标资源与版本。"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_app import constants  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BuildConfigAssetsTest(unittest.TestCase):
    def test_logo_png_source_present(self):
        png = os.path.join(_REPO, "assets", constants.ASSET_LOGO_PNG)
        self.assertTrue(os.path.isfile(png), "缺少 coin11-logo-v1.png 源文件")
        with open(png, "rb") as fh:
            head = fh.read(8)
        self.assertEqual(head[:4], b"\x89PNG", "源文件不是 PNG")

    def test_logo_ico_generated_and_not_source(self):
        ico = os.path.join(_REPO, "assets", constants.ASSET_LOGO_ICO)
        self.assertTrue(os.path.isfile(ico),
                        "缺少构建用 .ico（由 PNG 生成，不得覆盖 PNG）")
        png = os.path.join(_REPO, "assets", constants.ASSET_LOGO_PNG)
        with open(ico, "rb") as fh:
            head = fh.read(6)
        # ICO 头: reserved=0(2B), type=1(2B), count=1(2B)
        self.assertEqual(head, b"\x00\x00\x01\x00\x01\x00",
                         ".ico 文件头不合法")
        # .ico 是独立生成物（PNG 内嵌 ICO），与源 PNG 内容不同
        with open(ico, "rb") as a, open(png, "rb") as b:
            self.assertNotEqual(a.read(), b.read(),
                                ".ico 不应等于源 PNG（构建必须派生而非覆盖）")

    def test_spec_version_and_png_ico_reference(self):
        spec_path = os.path.join(_REPO, "packaging", "Coin11Helper.spec")
        with open(spec_path, encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn('APP_VERSION = "0.3.0"', spec)
        # icon 参数引用 .ico；datas 必须包含 assets（PNG 运行时展示）
        self.assertIn(constants.ASSET_LOGO_ICO, spec)
        self.assertIn(constants.ASSET_LOGO_PNG, spec)
        self.assertIn("assets", spec)

    def test_build_script_version_and_zip(self):
        build = os.path.join(_REPO, "scripts", "build.ps1")
        with open(build, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("0.3.0", text)
        self.assertIn("Coin11助手-0.3.0-windows-x64.zip", text)
        self.assertIn(constants.ASSET_LOGO_ICO, text)
        self.assertIn(constants.ASSET_LOGO_PNG, text)


if __name__ == "__main__":
    unittest.main()
