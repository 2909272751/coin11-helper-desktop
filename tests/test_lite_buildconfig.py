# -*- coding: utf-8 -*-
"""0.4.0 轻量版构建配置单元测试：轻量构建脚本、Inno Setup iss、种子标记。

纯静态检查（不执行真实构建、不下载大依赖）：
- 新增 scripts/build-lite.ps1 存在且不装配 site-packages/easyocr-models；
- 新增 packaging/Coin11Helper-lite.iss 存在且包含关键向导/卸载/AppId；
- requirements-lite-*.txt 与组件清单同源；
- make_seed 支持 heavy_deps_bundled=false 标记。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_app import make_seed  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LiteBuildScriptTest(unittest.TestCase):
    def test_build_lite_exists_and_avoids_heavy_assembly(self):
        ps1 = os.path.join(_REPO, "scripts", "build-lite.ps1")
        self.assertTrue(os.path.isfile(ps1), "缺少 scripts/build-lite.ps1")
        with open(ps1, encoding="utf-8-sig") as fh:
            text = fh.read()
        # 轻量装配目标是 python-bootstrap（基座），不是完整 runtime\python
        self.assertIn("python-bootstrap", text)
        # 输出目录名为“轻量版”
        self.assertIn("Coin11助手轻量版", text)
        # 主动负向校验（forbidden 列表）：轻量产物不得携带 torch/easyocr/模型
        self.assertIn("site-packages\\torch", text)
        self.assertIn("runtime\\easyocr-models", text)
        # 若缺 ISCC，脚本应清晰失败并提示安装方式（不静默下载）
        self.assertIn("ISCC", text)
        self.assertIn("Inno Setup", text)
        # 装配阶段绝不复制 .build\runtime-src 或 .build\easyocr-models 进发行
        # （只允许在“禁止列表/说明”中出现，因此检查装配动作源变量是否存在）
        self.assertNotIn('Join-Path $build "runtime-src"', text)
        self.assertNotIn('Join-Path $build "easyocr-models"', text)

    def test_requirements_lite_files_match_components(self):
        from desktop_app import runtime_components as rc
        pairs = [("requirements-lite-automation.txt", "automation"),
                 ("requirements-lite-ocr.txt", "ocr"),
                 ("requirements-lite-torch.txt", "torch")]
        for filename, group in pairs:
            path = os.path.join(_REPO, filename)
            self.assertTrue(os.path.isfile(path), f"缺少 {filename}")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            comp = rc.COMPONENT_BY_ID[group]
            for line in comp.pinned:
                self.assertIn(line, text, f"{filename} 缺少 {line}")
            # 锁定版本必须带 ==（固定版本不变式）
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("-"):
                    self.assertIn("==", s, f"{filename} 含未固定行: {line}")

    def test_iss_exists_with_wizard_and_uninstall(self):
        iss = os.path.join(_REPO, "packaging", "Coin11Helper-lite.iss")
        self.assertTrue(os.path.isfile(iss), "缺少 packaging/Coin11Helper-lite.iss")
        with open(iss, encoding="utf-8") as fh:
            text = fh.read()
        # 安装器关键要素
        self.assertIn("AppName=", text)
        self.assertIn("AppId=", text)
        self.assertIn("DefaultDirName=", text)
        self.assertIn("Coin11助手轻量版", text)
        self.assertIn("UninstallDisplayName=", text)
        # 桌面快捷方式可选项
        self.assertIn("desktopicon", text.lower())
        # 卸载时默认保留 %LOCALAPPDATA% 数据（除非用户明确选择删除）：
        # 用 [Code] CurUninstallStep 询问“是否删除数据”，是才 DelTree。
        self.assertIn("CurUninstallStep", text)
        self.assertIn("usPostUninstall", text)
        self.assertIn("ShouldDeleteData", text)
        self.assertIn("DelTree", text)
        self.assertIn("{localappdata}", text.lower())

    def test_spec_seed_dir_env_support(self):
        spec = os.path.join(_REPO, "packaging", "Coin11Helper.spec")
        with open(spec, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("COIN11_SEED_DIR", text)


class LiteSeedMarkTest(unittest.TestCase):
    """轻量 seed：.coin11-deps.json 标记 heavy_deps_bundled=false。"""

    def test_make_seed_lite_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "seed-current")
            # 用仓库根生成（受支持任务/基础文件齐全）
            out = make_seed.build_seed(_REPO, dest, "0" * 40,
                                       heavy_deps_bundled=False)
            self.assertTrue(os.path.isdir(out))
            deps = os.path.join(out, ".coin11-deps.json")
            self.assertTrue(os.path.isfile(deps))
            with open(deps, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertFalse(data["heavy_deps_bundled"])
            # 完整构建仍为 true
            dest2 = os.path.join(tmp, "seed-full")
            make_seed.build_seed(_REPO, dest2, "0" * 40,
                                 heavy_deps_bundled=True)
            with open(os.path.join(dest2, ".coin11-deps.json"),
                      encoding="utf-8") as fh:
                self.assertTrue(json.load(fh)["heavy_deps_bundled"])


if __name__ == "__main__":
    unittest.main()
