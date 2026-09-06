# -*- coding: utf-8 -*-
"""0.3.0 完整运行时单元测试：内置解释器选择 / 任务环境变量注入 / 依赖标记。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # helpers

from desktop_app import compat_patch, constants  # noqa: E402
from desktop_app.runtime import (  # noqa: E402
    easyocr_model_dir,
    runtime_bundled,
    runtime_python_exe,
)
from desktop_app.task_catalog import TaskCatalog, build_task_index  # noqa: E402
from desktop_app.task_runner import (  # noqa: E402
    RunState,
    build_task_env,
    resolve_task_python,
)
from tests.helpers import all_supported_scripts  # noqa: E402


class RuntimePythonExeTest(unittest.TestCase):
    """runtime_python_exe / resolve_task_python 的选择规则。"""

    def test_source_mode_returns_current_interpreter(self):
        # 源码模式（非冻结）应返回当前解释器（sys.executable 有效）
        self.assertNotEqual(runtime_python_exe(), "")
        exe = resolve_task_python()
        self.assertEqual(exe, os.path.abspath(sys.executable))
        self.assertTrue(os.path.isfile(exe))

    def test_runtime_bundled_false_in_source_mode(self):
        self.assertFalse(runtime_bundled())

    def test_explicit_python_wins(self):
        exe = os.path.abspath(sys.executable)
        self.assertEqual(resolve_task_python(exe), exe)
        # 空串回退默认
        self.assertNotEqual(resolve_task_python(""), "")

    def test_env_override_takes_priority(self):
        old = os.environ.get("COIN11_RUNTIME_PYTHON")
        os.environ["COIN11_RUNTIME_PYTHON"] = os.path.abspath(sys.executable)
        try:
            self.assertEqual(runtime_python_exe(),
                             os.path.abspath(sys.executable))
        finally:
            if old is None:
                os.environ.pop("COIN11_RUNTIME_PYTHON", None)
            else:
                os.environ["COIN11_RUNTIME_PYTHON"] = old

    def test_missing_frozen_python_returns_empty(self):
        # 冻结布局下没有内置解释器时必须返回 ""（启动前明确失败，绝不递归 EXE）
        # 用临时目录模拟发行根但不存在 runtime\\python\\python.exe
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = os.path.join(tmp, "Coin11助手.exe")
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable", fake_exe), \
                 mock.patch.object(sys, "_MEIPASS", os.path.join(tmp, "_internal"),
                                   create=True):
                self.assertEqual(runtime_python_exe(), "")


class BuildTaskEnvTest(unittest.TestCase):
    """build_task_env：serial / EasyOCR 模型目录 / PATH 注入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        os.environ[constants.ENV_DATA_DIR] = self.root

    def tearDown(self):
        self._tmp.cleanup()
        os.environ.pop(constants.ENV_DATA_DIR, None)

    def test_injects_serial_and_model_dir_when_present(self):
        # 设置 COIN11_EASYOCR_MODEL_DIR 指向存在的目录 -> 注入 env
        models = os.path.join(self.root, "models")
        os.makedirs(models, exist_ok=True)
        old = os.environ.get(constants.ENV_EASYOCR_MODEL_DIR)
        os.environ[constants.ENV_EASYOCR_MODEL_DIR] = models
        try:
            env = build_task_env(device_serial="ABC-1")
            self.assertEqual(env.get(constants.ENV_DEVICE_SERIAL), "ABC-1")
            self.assertEqual(env.get(constants.ENV_EASYOCR_MODEL_DIR),
                             os.path.abspath(models))
            self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")
        finally:
            if old is None:
                os.environ.pop(constants.ENV_EASYOCR_MODEL_DIR, None)
            else:
                os.environ[constants.ENV_EASYOCR_MODEL_DIR] = old

    def test_no_model_dir_no_env_key(self):
        old = os.environ.get(constants.ENV_EASYOCR_MODEL_DIR)
        os.environ.pop(constants.ENV_EASYOCR_MODEL_DIR, None)
        try:
            # 源码模式无 runtime 目录 -> 不注入 model dir 键
            env = build_task_env(device_serial="")
            self.assertNotIn(constants.ENV_EASYOCR_MODEL_DIR, env)
        finally:
            if old is not None:
                os.environ[constants.ENV_EASYOCR_MODEL_DIR] = old

    def test_easyocr_model_dir_override_is_abs(self):
        old = os.environ.get(constants.ENV_EASYOCR_MODEL_DIR)
        os.environ[constants.ENV_EASYOCR_MODEL_DIR] = self.root
        try:
            # override 分支按绝对路径返回（即使目录空/不存在也只是路径层）
            self.assertEqual(easyocr_model_dir(), self.root)
        finally:
            if old is None:
                os.environ.pop(constants.ENV_EASYOCR_MODEL_DIR, None)
            else:
                os.environ[constants.ENV_EASYOCR_MODEL_DIR] = old

    def test_path_prepend_includes_interp_dir(self):
        env = build_task_env(python_exe=os.path.abspath(sys.executable))
        path = env.get("PATH", "")
        interp_dir = os.path.dirname(os.path.abspath(sys.executable))
        self.assertIn(interp_dir, path)

    def test_no_adb_dir_no_prepend_noise(self):
        # 源码模式仓库根可能无 platform-tools：不注入也不报错
        env = build_task_env(python_exe="")
        self.assertIsInstance(env.get("PATH", ""), str)


class DepsBundledFlagTest(unittest.TestCase):
    """.coin11-deps.json heavy_deps_bundled=true 的标记与兼容补丁。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as fh:
            fh.write(content)

    def test_deps_bundled_marked_true_lists_runnable(self):
        for spec in constants.ALL_TASKS:
            self._write(spec["script"], "# dummy\n")
        with open(os.path.join(self.root, ".coin11-deps.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"heavy_deps_bundled": True}, fh)
        index = build_task_index(self.root)
        self.assertEqual(set(index), {s["id"] for s in constants.ALL_TASKS})
        for entry in index.values():
            self.assertTrue(entry["deps_bundled"])
            self.assertEqual(entry["unavailable_reason"], "")

    def test_easyocr_utils_patch_applies_to_upstream_text(self):
        text = ("from PIL import Image\n"
                "import easyocr\n"
                "easyocr_reader = easyocr.Reader(['ch_sim', 'en'], gpu=True)\n")
        patched = compat_patch.patch_utils_easyocr(text)
        self.assertIn("gpu': False", patched)
        self.assertIn("COIN11_EASYOCR_MODEL_DIR", patched)
        # 幂等
        again = compat_patch.patch_utils_easyocr(patched)
        self.assertEqual(patched, again)


if __name__ == "__main__":
    unittest.main()
