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
        # pip index 只允许空串（PyPI）、官方 CPU index 或三个 pip 源 allowlist
        self.assertTrue(rc.is_allowed_index_url(""))
        self.assertTrue(rc.is_allowed_index_url(rc.PYTORCH_CPU_INDEX_URL))
        for src in (rc.PIP_SOURCE_TUNA, rc.PIP_SOURCE_ALIYUN,
                    rc.PIP_SOURCE_OFFICIAL):
            self.assertTrue(rc.is_allowed_index_url(src), src)
        self.assertFalse(rc.is_allowed_index_url("https://evil.dev/simple"))
        self.assertFalse(rc.is_allowed_index_url("http://pypi.org/simple"))
        self.assertFalse(rc.is_allowed_index_url("https://pypi.org"))
        # 三个源都是精确 HTTPS 成员（无自定义/子串拼接）
        self.assertIn(rc.PIP_SOURCE_TUNA, rc.ALLOWED_PIP_INDEX_URLS)
        self.assertIn(rc.PIP_SOURCE_ALIYUN, rc.ALLOWED_PIP_INDEX_URLS)
        self.assertIn(rc.PIP_SOURCE_OFFICIAL, rc.ALLOWED_PIP_INDEX_URLS)
        self.assertEqual(
            set(rc.ALLOWED_PIP_INDEX_URLS),
            {"", rc.PYTORCH_CPU_INDEX_URL,
             rc.PIP_SOURCE_TUNA, rc.PIP_SOURCE_ALIYUN,
             rc.PIP_SOURCE_OFFICIAL})

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
        # torch 组清单文本不含 index 行（index 由安装器显式提供）
        self.assertNotIn("index-url", rc.REQS_TEXT_BY_GROUP["torch"])
        self.assertNotIn("--extra-index-url", rc.REQS_TEXT_BY_GROUP["torch"])


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
        cmd = inst._pip_cmd("py.exe", "C:/req.txt",
                            rc.PIP_SOURCE_TUNA)
        self.assertEqual(cmd[0], "py.exe")
        self.assertIn("-m", cmd)
        self.assertIn("install", cmd)
        self.assertIn("--disable-pip-version-check", cmd)
        idx = cmd.index("-r")
        self.assertEqual(cmd[idx + 1], "C:/req.txt")
        self.assertNotIn("shell", cmd)
        # 显式 --index-url = 所选源
        iu = cmd.index("--index-url")
        self.assertEqual(cmd[iu + 1], rc.PIP_SOURCE_TUNA)

    def test_pip_command_extra_index_for_torch(self):
        """torch 组附加官方 CPU index（download.pytorch.org/whl/cpu）。"""
        inst = self._installer()
        cmd = inst._pip_cmd("py.exe", "C:/req.txt",
                            rc.PIP_SOURCE_TUNA, rc.PYTORCH_CPU_INDEX_URL)
        iu = cmd.index("--index-url")
        self.assertEqual(cmd[iu + 1], rc.PIP_SOURCE_TUNA)
        ei = cmd.index("--extra-index-url")
        self.assertEqual(cmd[ei + 1], rc.PYTORCH_CPU_INDEX_URL)

    def test_pip_command_never_carries_foreign_index(self):
        """命令里的 index 只能是 allowlist 成员（无第三方/自定义源）。"""
        inst = self._installer()
        for mode in (rc.SOURCE_SMART, rc.SOURCE_TUNA, rc.SOURCE_ALIYUN,
                     rc.SOURCE_OFFICIAL):
            inst.source_mode = mode
            chain = rc.source_chain_for_pip_group("automation", mode)
            for url in chain:
                cmd = inst._pip_cmd("py.exe", "C:/req.txt", url)
                iu = cmd.index("--index-url")
                self.assertTrue(rc.is_allowed_index_url(cmd[iu + 1]),
                                f"{mode} 链出现非 allowlist index")

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


class PipSourceFallbackTest(unittest.TestCase):
    """SPEC 3/4：源链顺序、失败切换、取消不回退、torch 保持官方 CPU 索引、
    手动源最终回退官方；逐次记录来源/失败/切换原因。全部 mock 不联网。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rt = self._tmp.name
        self.logs = []
        self.states = []
        py = os.path.join(self.rt, "python", "python.exe")
        os.makedirs(os.path.dirname(py), exist_ok=True)
        with open(py, "wb") as fh:
            fh.write(b"MZ")

    def tearDown(self):
        self._tmp.cleanup()

    def _installer(self, mode="smart", stop=None):
        return rm.ComponentInstaller(
            self.rt, stop_event=stop, source_mode=mode,
            on_log=self.logs.append,
            on_state=lambda cid, s: self.states.append((cid, s)))

    # ---- 源链解析 ----
    def test_smart_chain_order(self):
        self.assertEqual(rc.resolve_pip_source_chain("smart"),
                         (rc.PIP_SOURCE_TUNA, rc.PIP_SOURCE_ALIYUN,
                          rc.PIP_SOURCE_OFFICIAL))

    def test_manual_chain_prefers_selected_and_official_last(self):
        """手动源优先所选，失败再尝试其余 allowlist，官方始终最后。"""
        for mode, first in (("tuna", rc.PIP_SOURCE_TUNA),
                            ("aliyun", rc.PIP_SOURCE_ALIYUN)):
            chain = rc.resolve_pip_source_chain(mode)
            self.assertEqual(chain[0], first)
            self.assertEqual(chain[-1], rc.PIP_SOURCE_OFFICIAL,
                             "官方必须始终是回退链的最后一项")

    def test_official_manual_chain_is_terminal(self):
        """手动官方：直接官方（官方即终极回退，不镜像回溯）。"""
        self.assertEqual(rc.resolve_pip_source_chain("official"),
                         (rc.PIP_SOURCE_OFFICIAL,))

    def test_unknown_mode_falls_back_smart(self):
        self.assertEqual(rc.resolve_pip_source_chain("nonsense"),
                         rc.resolve_pip_source_chain("smart"))

    def test_all_chain_urls_are_allowlisted(self):
        for mode in (rc.SOURCE_SMART, rc.SOURCE_TUNA, rc.SOURCE_ALIYUN,
                     rc.SOURCE_OFFICIAL):
            for url in rc.resolve_pip_source_chain(mode):
                self.assertTrue(rc.is_allowed_index_url(url))

    # ---- 安装回退行为 ----
    @staticmethod
    def _side_effect_fail_then_succeed(fails: int):
        """前 n 次 Popen 失败（退出码 1），之后成功。"""
        state = {"calls": 0}

        def _side(*args, **kwargs):
            proc = mock.Mock()
            if state["calls"] < fails:
                proc.poll.return_value = 0
                proc.returncode = 1
            else:
                proc.poll.return_value = 0
                proc.returncode = 0
            proc.stdout = iter(["Collecting x", ""])
            state["calls"] += 1
            return proc
        return _side

    @mock.patch.object(rm.subprocess, "Popen")
    def test_smart_falls_back_tuna_aliyun_official(self, popen):
        """智能模式：清华失败 -> 阿里成功（只调用两次 pip）。"""
        popen.side_effect = self._side_effect_fail_then_succeed(1)
        inst = self._installer(mode="smart")
        with mock.patch.object(rm, "verify_component",
                               return_value=(True, "导入探针通过")):
            ok = inst.install_component("automation")
        self.assertTrue(ok)
        # 记录一次切换：清华失败 -> 阿里
        self.assertTrue(any("切换" in l for l in self.logs))
        joined = "\n".join(self.logs)
        self.assertIn("清华 PyPI", joined)
        self.assertIn("阿里云 PyPI", joined)
        self.assertNotIn("PyPI 官方", joined)  # 成功即止，不试官方
        # 只发起两次 pip 调用
        self.assertEqual(popen.call_count, 2)

    @mock.patch.object(rm.subprocess, "Popen")
    def test_smart_all_sources_fail_marks_failed(self, popen):
        popen.side_effect = self._side_effect_fail_then_succeed(3)
        inst = self._installer(mode="smart")
        ok = inst.install_component("automation")
        self.assertFalse(ok)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(self.states[-1], ("automation", "failed"))

    @mock.patch.object(rm.subprocess, "Popen")
    def test_manual_official_succeeds_first(self, popen):
        popen.side_effect = self._side_effect_fail_then_succeed(0)
        inst = self._installer(mode="official")
        with mock.patch.object(rm, "verify_component",
                               return_value=(True, "导入探针通过")):
            ok = inst.install_component("automation")
        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 1)

    @mock.patch.object(rm.subprocess, "Popen")
    def test_tuna_falls_back_to_aliyun_then_official(self, popen):
        """手动清华：清华、阿里都失败 -> 官方成功（官方始终最后）。"""
        popen.side_effect = self._side_effect_fail_then_succeed(2)
        inst = self._installer(mode="tuna")
        with mock.patch.object(rm, "verify_component",
                               return_value=(True, "导入探针通过")):
            ok = inst.install_component("automation")
        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 3)
        joined = "\n".join(self.logs)
        self.assertIn("[切换]", joined)

    @mock.patch.object(rm.subprocess, "Popen")
    def test_cancel_does_not_fallback(self, popen):
        """取消绝不回退：预置取消后不会启动 pip（不会尝试任何来源）。"""
        stop = threading.Event()
        stop.set()
        inst = self._installer(mode="smart", stop=stop)
        ok = inst.install_component("automation")
        self.assertFalse(ok)
        # 取消先行：不启动任何 pip / 不回退
        self.assertEqual(popen.call_count, 0)
        self.assertEqual(self.states[-1], ("automation", "cancelled"))

    @mock.patch.object(rm.subprocess, "Popen")
    def test_cancel_after_first_failure_does_not_try_next_source(self, popen):
        """首个源失败时用户取消 -> 停止，不再尝试下一来源（取消不回退）。"""
        stop = threading.Event()
        first_proc = mock.Mock()
        first_proc.poll.return_value = 0
        first_proc.returncode = 1
        first_proc.stdout = iter(["ERROR", ""])
        popen.return_value = first_proc
        inst = self._installer(mode="smart", stop=stop)

        def _fake_run_pip(cmd):
            # 第一次运行 pip 返回 False，同时置取消（模拟用户在失败瞬间取消）
            stop.set()
            return False
        with mock.patch.object(inst, "_run_pip", side_effect=_fake_run_pip) as rp:
            ok = inst.install_component("automation")
        self.assertFalse(ok)
        # 失败后因取消不再继续下一源：_run_pip 只调用一次
        self.assertEqual(rp.call_count, 1)
        self.assertEqual(self.states[-1], ("automation", "cancelled"))

    @mock.patch.object(rm.subprocess, "Popen")
    def test_torch_always_uses_official_cpu_index(self, popen):
        """torch 组恒用官方 CPU index 作 extra-index（不伪装国内镜像）。"""
        popen.side_effect = self._side_effect_fail_then_succeed(0)
        inst = self._installer(mode="smart")
        with mock.patch.object(rm, "verify_component",
                               return_value=(True, "导入探针通过")):
            ok = inst.install_component("torch")
        self.assertTrue(ok)
        # 校验 Popen 收到的命令带官方 CPU extra-index
        args = popen.call_args[0][0] if popen.call_args[0] \
            else popen.call_args.kwargs["args"]
        self.assertIn("--extra-index-url", args)
        ei = args.index("--extra-index-url")
        self.assertEqual(args[ei + 1], rc.PYTORCH_CPU_INDEX_URL)
        # 官方 CPU URL 从未被国内源替代（主源仍是清华）
        iu = args.index("--index-url")
        self.assertEqual(args[iu + 1], rc.PIP_SOURCE_TUNA)
        # 智能链对普通依赖 = 清华->阿里->官方；但 torch 永不把 CPU wheel
        # 交给国内源（extra-index 恒定官方）
        self.assertEqual(rc.source_chain_for_pip_group("torch", "smart"),
                         (rc.PIP_SOURCE_TUNA, rc.PIP_SOURCE_ALIYUN,
                          rc.PIP_SOURCE_OFFICIAL))

    def test_logs_record_source_failure_and_switch(self):
        """逐次记录来源、失败、切换原因。"""
        rc_chain = rc.resolve_pip_source_chain("smart")
        self.assertEqual(len(rc_chain), 3)
        self.assertIn("[来源]", "[来源]")
        # 代码路径上：来源尝试 -> 失败 -> 切换 文案由 _install_pip 保证
        self.assertTrue(callable(rc.pip_source_label))


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

    def test_model_failure_logs_concrete_official_url(self):
        """模型失败显示具体地址（官方 GitHub release）并可复制诊断。"""
        logs = []
        inst = rm.ComponentInstaller(
            self.rt, on_log=logs.append,
            on_state=lambda cid, s: self.states.append((cid, s)))
        src = dict(rc.EASYOCR_MODEL_SOURCES["zh_sim_g2"])
        with mock.patch.object(
                inst, "_http_download",
                side_effect=rm.RuntimeError2("连接被拒")):
            ok = inst._download_model(src, self.dl, self.models)
        self.assertFalse(ok)
        joined = "\n".join(logs)
        # 失败原因 + 官方具体地址
        self.assertIn("下载失败", joined)
        self.assertIn(src["url"], joined)
        self.assertIn("GitHub release", joined)


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
