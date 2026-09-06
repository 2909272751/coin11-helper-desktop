# -*- coding: utf-8 -*-
"""0.4.0 轻量版：组件清单/来源校验/运行时优先级/验证/下载与安装状态单元测试。

全部测试零网络：pip/子进程用 mock 替身，模型下载用本地临时文件代替 zip。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # helpers

from desktop_app import constants  # noqa: E402
from desktop_app import runtime_components as rc  # noqa: E402
from desktop_app import runtime_manager as rm  # noqa: E402
from desktop_app.runtime import easyocr_model_dir, runtime_python_exe  # noqa: E402


class ComponentDefinitionsTest(unittest.TestCase):
    """组件清单固定版本 + 来源 HTTPS allowlist + 覆盖完整依赖清单。"""

    def test_all_components_validate(self):
        for comp in rc.DOWNLOADABLE_COMPONENTS:
            self.assertTrue(comp.validate(), f"组件定义非法: {comp.id}")
        # 基座
        self.assertTrue(rc.COMPONENT_PYTHON_BOOTSTRAP.validate())

    def test_component_allowlist_is_exact_https(self):
        # 模型 URL 必须 https 且为官方 release 主机
        for key, src in rc.EASYOCR_MODEL_SOURCES.items():
            self.assertTrue(src["url"].startswith("https://"), key)
            self.assertTrue(
                rc.is_allowed_model_url(src["url"]), f"模型 {key} URL 不在 allowlist")
            self.assertGreater(src["min_bytes"], 0)
            self.assertTrue(src["file"].endswith(".pth"))
        # 拒绝非白名单 URL
        self.assertFalse(rc.is_allowed_model_url("http://evil.com/model.zip"))
        self.assertFalse(rc.is_allowed_model_url("https://evil.com/model.zip"))
        self.assertFalse(rc.is_allowed_model_url("file:///tmp/x"))
        # pip index 只允许空串（PyPI）或官方 CPU index
        self.assertTrue(rc.is_allowed_index_url(""))
        self.assertTrue(rc.is_allowed_index_url(rc.PYTORCH_CPU_INDEX_URL))
        self.assertFalse(rc.is_allowed_index_url("https://evil.dev/simple"))
        self.assertFalse(rc.is_allowed_index_url("http://pypi.org/simple"))

    def test_pinned_versions_cover_full_runtime_requirements(self):
        """组件锁定集合 == requirements-desktop-runtime.txt 的直接依赖 + 模型。"""
        req_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "requirements-desktop-runtime.txt")
        with open(req_path, encoding="utf-8") as fh:
            req_text = fh.read()
        # 收集组件锁定包名（去掉 +cpu 与版本）
        pinned = {}
        for comp in rc.DOWNLOADABLE_COMPONENTS:
            for line in comp.pinned:
                name = line.split("==")[0].strip()
                if name and not name.startswith("-"):
                    pinned.setdefault(name.lower(), line)
        # requirements 中非注释/非 index 行都必须在组件锁定里
        for line in req_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            name = stripped.split("==")[0].strip()
            self.assertIn(name.lower(), pinned,
                          f"requirements 的 {name} 未在组件清单锁定")
            self.assertEqual(pinned[name.lower()].split("==")[1],
                             stripped.split("==")[1],
                             f"{name} 版本与完整锁定不一致")
        # 模型必须有两条
        self.assertEqual(set(rc.EASYOCR_MODEL_SOURCES),
                         {"craft_mlt_25k", "zh_sim_g2"})

    def test_install_order_sane(self):
        self.assertEqual(rc.INSTALL_ORDER[-1], "easyocr-models")
        # torch 在 ocr 之前（easyocr 依赖 torch）
        self.assertLess(rc.INSTALL_ORDER.index("torch"),
                        rc.INSTALL_ORDER.index("ocr"))

    def test_reqs_text_matches_pinned(self):
        for group, comp in [("automation", rc.COMPONENT_AUTOMATION),
                            ("ocr", rc.COMPONENT_OCR),
                            ("torch", rc.COMPONENT_TORCH)]:
            text = rc.REQS_TEXT_BY_GROUP[group]
            for line in comp.pinned:
                self.assertIn(line, text)
        # torch 组清单文本必须带官方 CPU index
        self.assertIn(rc.PYTORCH_CPU_INDEX_URL,
                      rc.REQS_TEXT_BY_GROUP["torch"])


class RuntimePriorityTest(unittest.TestCase):
    """数据运行时定位优先级（SPEC）：显式 env > 数据运行时 > 完整内置。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self._tmp.name, "Coin11Helper")
        self._old_data = os.environ.get(constants.ENV_DATA_DIR)
        self._old_env_py = os.environ.get("COIN11_RUNTIME_PYTHON")
        os.environ[constants.ENV_DATA_DIR] = self.data

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_data is None:
            os.environ.pop(constants.ENV_DATA_DIR, None)
        else:
            os.environ[constants.ENV_DATA_DIR] = self._old_data
        if self._old_env_py is None:
            os.environ.pop("COIN11_RUNTIME_PYTHON", None)
        else:
            os.environ["COIN11_RUNTIME_PYTHON"] = self._old_env_py

    def _mk_frozen(self, exe_dir):
        """把 sys 模拟成冻结 onedir：exe 在 exe_dir。"""
        return mock.patch.object(sys, "frozen", True, create=True), \
            mock.patch.object(sys, "executable",
                              os.path.join(exe_dir, "Coin11助手.exe")), \
            mock.patch.object(sys, "_MEIPASS",
                              os.path.join(exe_dir, "_internal"), create=True)

    def test_default_data_runtime_dir_under_data(self):
        self.assertEqual(rm.default_user_runtime_dir(self.data),
                         os.path.join(self.data, "runtime"))

    def test_resolve_from_settings(self):
        data = self.data

        class _S:
            def get(self, key, default=None):
                return os.path.join(data, "custom") \
                    if key == "runtime_dir" else default
        self.assertEqual(rm.resolve_data_runtime_dir(_S()),
                         os.path.join(self.data, "custom"))

    def test_env_explicit_wins_over_data_runtime(self):
        with tempfile.TemporaryDirectory() as exe_dir:
            f1, f2, f3 = self._mk_frozen(exe_dir)
            with f1, f2, f3:
                # 数据运行时里有 python
                data_rt = os.path.join(self.data, "runtime")
                py_dir = os.path.join(data_rt, "python")
                os.makedirs(py_dir, exist_ok=True)
                with open(os.path.join(py_dir, "python.exe"), "wb") as fh:
                    fh.write(b"MZ-data")
                # env 指向一个真实存在的解释器（显式最高优先）
                env_py = os.path.join(exe_dir, "env_python.exe")
                with open(env_py, "wb") as fh:
                    fh.write(b"MZ-env")
                os.environ["COIN11_RUNTIME_PYTHON"] = env_py
                self.assertEqual(runtime_python_exe(), env_py)

    def test_frozen_prefers_data_runtime_over_bundled_full(self):
        with tempfile.TemporaryDirectory() as exe_dir:
            f1, f2, f3 = self._mk_frozen(exe_dir)
            with f1, f2, f3:
                data_rt = os.path.join(self.data, "runtime")
                py_dir = os.path.join(data_rt, "python")
                os.makedirs(py_dir, exist_ok=True)
                py_path = os.path.join(py_dir, "python.exe")
                with open(py_path, "wb") as fh:
                    fh.write(b"MZ-data")
                # 发行根也放一个完整内置
                bundled = os.path.join(exe_dir, "runtime", "python")
                os.makedirs(bundled, exist_ok=True)
                with open(os.path.join(bundled, "python.exe"), "wb") as fh:
                    fh.write(b"MZ-full")
                self.assertEqual(runtime_python_exe(), py_path)

    def test_frozen_without_runtime_returns_empty(self):
        """轻量版未装数据运行时且无完整内置 -> ""（不误用 bootstrap/_internal）。"""
        with tempfile.TemporaryDirectory() as exe_dir:
            f1, f2, f3 = self._mk_frozen(exe_dir)
            with f1, f2, f3:
                self.assertEqual(runtime_python_exe(), "")

    def test_model_dir_data_preferred_over_bundled(self):
        with tempfile.TemporaryDirectory() as exe_dir:
            f1, f2, f3 = self._mk_frozen(exe_dir)
            with f1, f2, f3:
                data_models = os.path.join(self.data, "runtime", "easyocr-models")
                os.makedirs(data_models, exist_ok=True)
                bundled_models = os.path.join(exe_dir, "runtime", "easyocr-models")
                os.makedirs(bundled_models, exist_ok=True)
                self.assertEqual(easyocr_model_dir(), data_models)


class ComponentStateTest(unittest.TestCase):
    """state.json 持久化。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rt = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_state_roundtrip(self):
        states = {"automation": "ok", "torch": "failed"}
        rm.save_component_states(self.rt, states)
        loaded = rm.load_component_states(self.rt)
        self.assertEqual(loaded["automation"], "ok")
        self.assertEqual(loaded["torch"], "failed")
        # 未记录组件回 idle
        self.assertEqual(loaded["ocr"], "idle")
        # 文件原子（无 .tmp 残留）
        files = os.listdir(self.rt)
        self.assertFalse(any(f.endswith(".tmp") for f in files))

    def test_corrupt_state_falls_back(self):
        with open(os.path.join(self.rt, "state.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{bad json")
        loaded = rm.load_component_states(self.rt)
        self.assertEqual(loaded["ocr"], "idle")


class ImportProbeTest(unittest.TestCase):
    """import 探针：mock 子进程（不联网）。"""

    def test_probe_ok_when_returncode_zero_and_ok_marker(self):
        with mock.patch.object(rm.subprocess, "run",
                               return_value=mock.Mock(
                                   returncode=0, stdout="RUNTIME_PROBE_OK\n",
                                   stderr="")):
            ok, detail = rm.run_import_probe("py.exe", ("numpy",))
        self.assertTrue(ok)
        self.assertIn("RUNTIME_PROBE_OK", detail)

    def test_probe_fail_when_missing(self):
        with mock.patch.object(rm.subprocess, "run",
                               return_value=mock.Mock(
                                   returncode=2,
                                   stdout="MISSING: numpy:boom\n",
                                   stderr="")):
            ok, detail = rm.run_import_probe("py.exe", ("numpy",))
        self.assertFalse(ok)
        self.assertIn("MISSING", detail)

    def test_probe_empty_modules_is_ok(self):
        ok, _ = rm.run_import_probe("py.exe", ())
        self.assertTrue(ok)

    def test_probe_error_redacts_long_secrets(self):
        with mock.patch.object(rm.subprocess, "run",
                               return_value=mock.Mock(
                                   returncode=1,
                                   stdout="token='abcdefghijklmnopqrstuvwxyz1234567890'\n",
                                   stderr="")):
            _, detail = rm.run_import_probe("py.exe", ("torch",))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz1234567890", detail)


class VerifyComponentTest(unittest.TestCase):
    """verify_component：静态存在性（不 import，避免依赖真实包）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rt = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _mk_python(self):
        py = os.path.join(self.rt, "python", "python.exe")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        with open(py, "wb") as fh:
            fh.write(b"MZ")
        return py

    def test_missing_python_returns_false(self):
        ok, _ = rm.verify_component(self.rt, "automation", fast=True)
        self.assertFalse(ok)

    def test_fast_static_detects_site_packages_presence(self):
        self._mk_python()
        # 无 site-packages -> 未就绪
        ok, _ = rm.verify_component(self.rt, "automation", fast=True)
        self.assertFalse(ok)
        sp = os.path.join(self.rt, "python", "Lib", "site-packages")
        os.makedirs(sp, exist_ok=True)
        for mod in ("uiautomator2", "uiautodev", "requests"):
            os.makedirs(os.path.join(sp, mod), exist_ok=True)
        ok, _ = rm.verify_component(self.rt, "automation", fast=True)
        self.assertTrue(ok)

    def test_model_verification_size_gate(self):
        self._mk_python()
        models = os.path.join(self.rt, "easyocr-models")
        os.makedirs(models, exist_ok=True)
        ok, _ = rm.verify_component(self.rt, "easyocr-models", fast=True)
        self.assertFalse(ok)
        # 造一个过小的 pth -> 仍不算就绪
        for key, src in rc.EASYOCR_MODEL_SOURCES.items():
            with open(os.path.join(models, src["file"]), "wb") as fh:
                fh.write(b"x" * 100)
        ok, _ = rm.verify_component(self.rt, "easyocr-models", fast=True)
        self.assertFalse(ok)
        # 造足体积
        for key, src in rc.EASYOCR_MODEL_SOURCES.items():
            with open(os.path.join(models, src["file"]), "wb") as fh:
                fh.truncate(src["min_bytes"])
        ok, _ = rm.verify_component(self.rt, "easyocr-models", fast=True)
        self.assertTrue(ok)


class PipCommandSecurityTest(unittest.TestCase):
    """pip 参数列表、无 shell、禁用版本检查；安装成功/失败/取消状态。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rt = self._tmp.name
        self.states = []
        self.logs = []

    def tearDown(self):
        self._tmp.cleanup()

    def _installer(self, stop=None):
        return rm.ComponentInstaller(
            self.rt, stop_event=stop,
            on_log=self.logs.append,
            on_state=lambda cid, s: self.states.append((cid, s)))

    def _mk_python(self):
        py = os.path.join(self.rt, "python", "python.exe")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        with open(py, "wb") as fh:
            fh.write(b"MZ")
        return py

    def test_pip_command_shape(self):
        """命令必须以参数列表调用、含 -m pip install、禁用版本检查、-r 清单。"""
        inst = self._installer()
        cmd = inst._pip_cmd("py.exe", "C:/req.txt")
        self.assertEqual(cmd[0], "py.exe")
        self.assertIn("-m", cmd)
        self.assertIn("install", cmd)
        self.assertIn("--disable-pip-version-check", cmd)
        idx = cmd.index("-r")
        self.assertEqual(cmd[idx + 1], "C:/req.txt")
        self.assertNotIn("shell", cmd)

    @mock.patch.object(rm.subprocess, "Popen")
    def test_install_success_runs_pip_and_probe(self, popen):
        """成功：pip 退出 0 + 探针通过 -> 状态 installing->verifying->ok。"""
        self._mk_python()
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.returncode = 0
        proc.stdout = iter(["Collecting numpy", ""])
        popen.return_value = proc
        inst = self._installer()
        with mock.patch.object(rm, "verify_component",
                               return_value=(True, "导入探针通过")):
            ok = inst.install_component("automation")
        self.assertTrue(ok)
        self.assertEqual(self.states[-1], ("automation", "ok"))
        # Popen 以参数列表调用（无 shell）
        call_args = popen.call_args
        cmd = call_args[0][0] if call_args[0] else call_args.kwargs["args"]
        self.assertIsInstance(cmd, list)
        self.assertTrue(any("pip" in str(x) for x in cmd))

    @mock.patch.object(rm.subprocess, "Popen")
    def test_install_failure_marks_failed(self, popen):
        """pip 失败（退出码 1）-> failed，且不标 ok。"""
        self._mk_python()
        proc = mock.Mock()
        proc.poll.return_value = 0
        proc.returncode = 1
        proc.stdout = iter(["ERROR: Could not install", ""])
        popen.return_value = proc
        inst = self._installer()
        ok = inst.install_component("automation")
        self.assertFalse(ok)
        self.assertEqual(self.states[-1], ("automation", "failed"))
        self.assertTrue(any("失败" in l for l in self.logs))

    @mock.patch.object(rm.subprocess, "Popen")
    def test_install_cancel_marks_cancelled(self, popen):
        """取消：stop event 置位后 install 返回 False 且标 cancelled。"""
        self._mk_python()
        stop = threading.Event()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.returncode = None
        proc.stdout = iter(["..."])
        popen.return_value = proc
        inst = self._installer(stop=stop)
        # 预置 stop
        stop.set()
        ok = inst.install_component("automation")
        self.assertFalse(ok)
        self.assertEqual(self.states[-1], ("automation", "cancelled"))

    def test_unknown_component_raises(self):
        inst = self._installer()
        with self.assertRaises(rm.RuntimeError2):
            inst.install_component("not-exists")


class ModelDownloadTest(unittest.TestCase):
    """模型下载：允许 URL 校验 + zip 白名单抽取 + 体积门槛（不联网）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rt = self._tmp.name
        self.models = os.path.join(self.rt, "easyocr-models")
        self.dl = os.path.join(self.rt, ".downloads")
        os.makedirs(self.models, exist_ok=True)
        os.makedirs(self.dl, exist_ok=True)
        self.states = []
        self.inst = rm.ComponentInstaller(
            self.rt, on_state=lambda cid, s: self.states.append((cid, s)))

    def tearDown(self):
        self._tmp.cleanup()

    def _make_zip(self, target_file: str, size: int, name: str = "") -> str:
        import zipfile
        import io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"models/{target_file}", b"\0" * size)
        path = os.path.join(self.dl, name or f"{target_file}.zip")
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())
        return path

    @mock.patch.object(rm.ComponentInstaller, "_http_download")
    def test_download_success_extracts_and_verifies(self, http):
        """本地 zip -> 抽取 .pth -> 校验体积 -> ok 状态。

        全零内容会被高度压缩，故把 zip 最小体积门槛临时置 0（生产门槛针对
        真实不可压缩模型 zip 依旧生效）；.pth 解压后体积校验仍然严格生效。
        """
        import unittest.mock as _mock
        with _mock.patch.object(rm, "_MIN_MODEL_ZIP", 0):
            # 预生成 zip 放入独立 prep 目录（避免与 dest 同路径导致覆盖损坏）
            prep_dir = os.path.join(self.dl, "prep")
            os.makedirs(prep_dir, exist_ok=True)
            prep = {}
            for key, src in rc.EASYOCR_MODEL_SOURCES.items():
                url_name = os.path.basename(src["url"]) or "model.zip"
                prep[url_name] = self._make_zip(src["file"], src["min_bytes"],
                                                name=os.path.join(prep_dir,
                                                                  url_name))

            def _fake_download(url, dest, on_progress=None, timeout=60.0):
                url_name = os.path.basename(url) or "model.zip"
                # 先读入内存再写 dest（dest 与预置 zip 可能同路径，边读边写会损坏）
                with open(prep[url_name], "rb") as fr:
                    payload = fr.read()
                with open(dest, "wb") as fw:
                    fw.write(payload)

            http.side_effect = _fake_download
            ok = self.inst._install_models(rc.COMPONENT_MODELS)
            self.assertTrue(ok, "模型安装失败")
            self.assertEqual(self.states[-1], ("easyocr-models", "ok"))
            for key, src in rc.EASYOCR_MODEL_SOURCES.items():
                final = os.path.join(self.models, src["file"])
                self.assertTrue(os.path.isfile(final), f"缺少 {src['file']}")
                self.assertGreaterEqual(os.path.getsize(final),
                                        src["min_bytes"])

    def test_rejects_foreign_url_before_download(self):
        ok = self.inst._download_model(
            {"url": "https://evil.com/m.zip", "file": "x.pth",
             "min_bytes": 1, "label": "x"},
            self.dl, self.models)
        self.assertFalse(ok)


def _copy_zip(src: str, dest: str):
    import shutil
    shutil.copyfile(src, dest)


class EnsureRuntimeReadyTest(unittest.TestCase):
    """任务开始前的缺运行时拦截（SPEC 7：缺基础依赖不启动子进程）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self._tmp.name, "Coin11Helper")
        self._old_data = os.environ.get(constants.ENV_DATA_DIR)
        os.environ[constants.ENV_DATA_DIR] = self.data

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_data is None:
            os.environ.pop(constants.ENV_DATA_DIR, None)
        else:
            os.environ[constants.ENV_DATA_DIR] = self._old_data

    def _patch_probe_ok(self):
        return mock.patch.object(rm, "run_import_probe",
                                 return_value=(True, "RUNTIME_PROBE_OK"))

    def test_source_mode_no_gate(self):
        """源码（非冻结）不设门禁：开发模式直接放行。"""
        ok, message, missing = rm.ensure_runtime_ready(self.data)
        self.assertTrue(ok)
        self.assertIn("开发模式", message)

    def test_frozen_lite_without_runtime_blocked(self):
        """冻结轻量版且数据运行时未装 -> 拦截并列出缺失组件。"""
        with tempfile.TemporaryDirectory() as exe_dir:
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable",
                                   os.path.join(exe_dir, "Coin11助手.exe")), \
                 mock.patch.object(sys, "_MEIPASS",
                                   os.path.join(exe_dir, "_internal"),
                                   create=True):
                ok, message, missing = rm.ensure_runtime_ready(self.data)
        self.assertFalse(ok)
        self.assertIn("下载中心", message)
        # 缺失列表含全部四类运行组件
        self.assertEqual(set(missing),
                         {c.id for c in rm.COMPONENT_BY_ID.values()
                          if c.id != "python-bootstrap"})

    def test_frozen_full_bundled_runtime_passes(self):
        """冻结完整发行（发行根 runtime\\python）无需下载中心直接就绪。"""
        with tempfile.TemporaryDirectory() as exe_dir:
            bundled = os.path.join(exe_dir, "runtime", "python")
            os.makedirs(bundled, exist_ok=True)
            with open(os.path.join(bundled, "python.exe"), "wb") as fh:
                fh.write(b"MZ")
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable",
                                   os.path.join(exe_dir, "Coin11助手.exe")), \
                 mock.patch.object(sys, "_MEIPASS",
                                   os.path.join(exe_dir, "_internal"),
                                   create=True):
                ok, message, missing = rm.ensure_runtime_ready(self.data)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_frozen_lite_with_full_data_runtime_passes(self):
        """冻结轻量版且数据运行时组件齐全 -> 放行。"""
        # 造数据运行时解释器 + mock import 探针通过 + mock 模型齐全
        py = os.path.join(self.data, "runtime", "python", "python.exe")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        with open(py, "wb") as fh:
            fh.write(b"MZ")
        models = os.path.join(self.data, "runtime", "easyocr-models")
        os.makedirs(models, exist_ok=True)
        for key, src in rc.EASYOCR_MODEL_SOURCES.items():
            with open(os.path.join(models, src["file"]), "wb") as fh:
                fh.truncate(src["min_bytes"])
        with tempfile.TemporaryDirectory() as exe_dir:
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable",
                                   os.path.join(exe_dir, "Coin11助手.exe")), \
                 mock.patch.object(sys, "_MEIPASS",
                                   os.path.join(exe_dir, "_internal"),
                                   create=True), \
                 self._patch_probe_ok():
                ok, message, missing = rm.ensure_runtime_ready(self.data)
        self.assertTrue(ok, message)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
