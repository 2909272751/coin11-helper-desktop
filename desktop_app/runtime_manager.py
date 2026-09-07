"""数据运行时管理：定位优先级、组件验证、锁定依赖下载/安装/校验/取消。

设计（SPEC“运行时定位”与“下载中心”两条要求的落地）：
- 运行时定位顺序（runtime_resolve_data_python）：
    1. 环境变量 COIN11_DATA_RUNTIME（显式测试/排障）；
    2. 用户设置的数据运行时目录（默认 %LOCALAPPDATA%\\Coin11Helper\\runtime）；
    3. 轻量发行内置 python-bootstrap（仅用于 bootstrap，绝不做任务解释器）；
    4. 源码模式回退：当前解释器。
  任务执行只接受“已验证的数据运行时解释器”（import 探针通过 + 模型齐全）。
  轻量版绝不误用 PyInstaller 壳的 _internal\\python.exe（它没有任务依赖）。
- 下载/安装安全：
    * 组件来源全部来自 runtime_components 内嵌 HTTPS allowlist（固定版本）；
    * pip 用参数列表 + --disable-pip-version-check + 显式 --index-url/-r，
      无 shell，可超时/取消，日志脱敏（logutil.redact）；
    * 每个 pip 组安装到数据运行时 site-packages（--target 亦可，但这里用
      -m pip install --upgrade 装入该解释器的 site-packages，保证依赖可被
      import 探针命中，且不与用户全局 Python 混用）；
    * 模型下载：先落 .downloads 临时文件 -> 校验 zip 体积 -> 用标准库 zipfile
      只抽取白名单 .pth -> 体积校验 -> os.replace 原子入位。
- 验证状态写入 <runtime_dir>\\state.json；损坏/不完整一律视为未安装。
- 组件之间有序依赖：ocr/torch 必须先于 automation 安装没有硬依赖关系，
  但模型组依赖 easyocr（在 ocr 组）就位；由调用方按 DOWNLOADABLE 顺序串行。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from . import constants
from .logutil import redact
from .runtime_components import (
    COMPONENT_BY_ID,
    COMPONENT_PYTHON_BOOTSTRAP,
    EASYOCR_MODEL_SOURCES,
    STATE_CANCELLED,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_IDLE,
    STATE_INSTALLING,
    STATE_OK,
    STATE_VERIFYING,
)

# 运行时目录里存放状态的相对路径
STATE_FILE = "state.json"
DOWNLOADS_DIR = ".downloads"
USER_RUNTIME_DIRNAME = "runtime"          # 默认 %LOCALAPPDATA%\\Coin11Helper\\runtime
ENV_DATA_RUNTIME = "COIN11_DATA_RUNTIME"  # 显式数据运行时目录（测试/排障）
# 数据运行时下解释器/模型/探针布局
RUNTIME_PY_SUBDIR = "python"
RUNTIME_MODELS_SUBDIR = "easyocr-models"

# 导入探针脚本模板（以数据运行时解释器执行；只输出 OK 或 MISSING）
_IMPORT_PROBE = (
    "import importlib, sys\n"
    "mods = {mods!r}\n"
    "missing = []\n"
    "for m in mods:\n"
    "    try:\n"
    "        importlib.import_module(m)\n"
    "    except Exception as exc:\n"
    "        missing.append('%s:%s' % (m, exc))\n"
    "if missing:\n"
    "    print('MISSING: ' + '; '.join(missing)); sys.exit(2)\n"
    "print('RUNTIME_PROBE_OK')\n"
)

_PIP_TIMEOUT = 1800.0   # pip 单组最长等待（取消由 stop_event 短路）
_NET_TIMEOUT = 60.0     # 单次下载读超时（模型大文件按分块累计时间另行兜底）
_DOWNLOAD_CHUNK = 1024 * 256
_MIN_MODEL_ZIP = 10 * 1024 * 1024   # 每个模型 zip 的最小体积（低于视为不完整）


class RuntimeError2(RuntimeError):
    """数据运行时相关的可显示错误（message/hint 供 UI 展示）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# ---------------------------------------------------------------------------
# 纯函数：定位 / 验证
# ---------------------------------------------------------------------------

def default_user_runtime_dir(data_dir: Optional[str] = None) -> str:
    """默认数据运行时目录。

    优先级：进程级 COIN11_DATA_RUNTIME（launcher 读 settings.runtime_dir 后注入）
    > <data_dir>\\runtime（data_dir 默认 %LOCALAPPDATA%\\Coin11Helper）。
    保证任务解释器定位 / 模型目录 / 下载中心读到同一目录。
    """
    env_rt = os.environ.get(ENV_DATA_RUNTIME, "").strip()
    if env_rt:
        return os.path.abspath(env_rt)
    base = data_dir or constants.default_data_dir()
    return os.path.join(base, USER_RUNTIME_DIRNAME)


def resolve_data_runtime_dir(settings=None) -> str:
    """用户设置的数据运行时目录；无设置回默认。settings 需有 get 接口。"""
    if settings is not None:
        val = settings.get("runtime_dir", "")
        if val:
            return os.path.abspath(val)
    return default_user_runtime_dir()


def _python_rel(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, RUNTIME_PY_SUBDIR, "python.exe")


def _models_rel(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, RUNTIME_MODELS_SUBDIR)


def bootstrap_python_exe() -> str:
    """轻量发行内置 python-bootstrap（仅用于安装依赖；不用于运行任务）。

    源码模式返回 ""（无内置基座），由下载中心自行准备引导解释器（开发回退）。
    """
    base = constants.app_base_dir()
    candidate = os.path.join(base, COMPONENT_PYTHON_BOOTSTRAP.bundled_relpath)
    return candidate if os.path.isfile(candidate) else ""


def runtime_data_python(runtime_dir: str) -> str:
    """返回数据运行时解释器绝对路径（无论是否已验证）；不存在返回 ""。"""
    py = _python_rel(runtime_dir)
    return py if os.path.isfile(py) else ""


def run_import_probe(python_exe: str, modules: Tuple[str, ...]) -> Tuple[bool, str]:
    """用指定解释器运行 import 探针。返回 (ok, detail)。绝不联网。"""
    if not modules:
        return True, ""
    code = _IMPORT_PROBE.format(mods=list(modules))
    try:
        proc = subprocess.run(
            [python_exe, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"探针无法运行: {exc}"
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode == 0 and "RUNTIME_PROBE_OK" in out:
        return True, out.strip()
    return False, redact(out).strip()


def _model_status(models_dir: str) -> Dict[str, bool]:
    """模型文件状态：存在且达到体积下限才算 ok。"""
    status: Dict[str, bool] = {}
    for key, src in EASYOCR_MODEL_SOURCES.items():
        path = os.path.join(models_dir, src["file"])
        ok = False
        try:
            ok = os.path.isfile(path) and os.path.getsize(path) >= src["min_bytes"]
        except OSError:
            ok = False
        status[key] = ok
    return status


def verify_component(runtime_dir: str, component_id: str,
                     fast: bool = False) -> Tuple[bool, str]:
    """验证单个组件是否已安装可用。

    fast=True 时跳过 import 探针（只做静态存在性检查，供 UI 状态刷新/离线检查；
    真正运行任务前必须 fast=False 全量验证）。返回 (ok, detail)。
    """
    comp = COMPONENT_BY_ID.get(component_id)
    if comp is None:
        return False, f"未知组件: {component_id}"
    py = runtime_data_python(runtime_dir)
    if not py:
        return False, "数据运行时解释器缺失"
    if comp.kind == "model":
        st = _model_status(_models_rel(runtime_dir))
        bad = [src["label"] for key, src in EASYOCR_MODEL_SOURCES.items()
               if not st.get(key)]
        return (not bad), ("模型齐全" if not bad else "缺少: " + "、".join(bad))
    if comp.kind == "pip":
        if fast:
            # 静态：解释器存在 + site-packages 有各主包目录（不 import）
            return _static_pip_ok(runtime_dir, comp), "静态检查"
        ok, detail = run_import_probe(py, comp.probe_modules())
        return ok, (detail or ("导入探针通过" if ok else "导入探针失败"))
    if comp.kind == "python":
        return os.path.isfile(py), "基座存在" if os.path.isfile(py) else "基座缺失"
    return False, f"未知组件类型: {comp.kind}"


def _static_pip_ok(runtime_dir: str, comp) -> bool:
    sp = os.path.join(runtime_dir, RUNTIME_PY_SUBDIR, "Lib", "site-packages")
    if not os.path.isdir(sp):
        return False
    tops = {comp.id: comp.probe_modules()}
    for mod in tops.get(comp.id, ()):
        # 主包目录或 .py 文件（PIL -> PIL 目录、cv2 -> cv2 目录等）
        name = mod.replace(".", os.sep).split(os.sep)[0]
        if not os.path.exists(os.path.join(sp, name)):
            return False
    return True


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

def _state_path(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, STATE_FILE)


def load_component_states(runtime_dir: str) -> Dict[str, str]:
    """读 state.json -> {component_id: state}；缺失/损坏回默认 idle。"""
    default = {c.id: STATE_IDLE for c in COMPONENT_BY_ID.values()}
    try:
        with open(_state_path(runtime_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in data.get("components", {}):
                if key in default:
                    default[key] = str(data["components"][key])
    except (OSError, ValueError):
        pass
    return default


def save_component_states(runtime_dir: str, states: Dict[str, str]) -> None:
    os.makedirs(runtime_dir, exist_ok=True)
    payload = {"version": 1, "runtime_dir": runtime_dir,
               "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "components": dict(states)}
    fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".tmp",
                               dir=runtime_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _state_path(runtime_dir))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# pip 安装（参数列表、无 shell、超时/取消）
# ---------------------------------------------------------------------------

def _write_requirements(runtime_dir: str, group: str) -> str:
    """把内嵌锁定清单写入临时文件，返回路径（用于 pip -r）。

    清单文本不含任何 index 行：普通 PyPI 依赖的来源由安装器按所选/智能源
    显式 --index-url 提供；torch 组 CPU wheel 恒用官方 CPU 索引（SPEC 3/4）。
    """
    from .runtime_components import REQS_TEXT_BY_GROUP
    text = REQS_TEXT_BY_GROUP.get(group, "")
    if not text:
        raise RuntimeError2(f"缺少 {group} 的锁定清单", "")
    os.makedirs(runtime_dir, exist_ok=True)
    path = os.path.join(runtime_dir, f".req-{group}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class ComponentInstaller:
    """组件安装/下载执行器：支持进度回调、超时、取消。

    stop_event 由调用方持有并跨多次安装保持（取消后不再启动新组件）。
    组件安装按依赖安全顺序由调用方队列驱动；本类只负责单组件。
    普通 pip 依赖按下述规则选择下载源（SPEC 3）：
      - torch 组：恒用 PyTorch 官方 CPU wheel index（download.pytorch.org/whl/cpu）；
      - 其余组：按 source_mode 的 allowlist 链（智能 = 清华→阿里→官方；
        手动源 = 所选优先，失败再试其余 allowlist，官方始终最后）。
      链内上一来源失败会逐次记录原因并切换到下一来源；取消绝不回退。
    """

    def __init__(self, runtime_dir: str,
                 stop_event: Optional[threading.Event] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_state: Optional[Callable[[str, str], None]] = None,
                 on_progress: Optional[Callable[[str, int], None]] = None,
                 source_mode: Optional[str] = None):
        from .runtime_components import DEFAULT_PIP_SOURCE
        self.runtime_dir = runtime_dir
        self.on_log = on_log or (lambda _t: None)
        self.on_state = on_state or (lambda _cid, _s: None)
        self.on_progress = on_progress or (lambda _cid, _pct: None)
        self._stop = stop_event or threading.Event()
        self._current_op = ""
        self._thread: Optional[threading.Thread] = None
        self.source_mode = source_mode or DEFAULT_PIP_SOURCE

    # ---------------- 公共 API ----------------
    def install_component(self, component_id: str) -> bool:
        """串行安装一个组件（阻塞调用，可被 cancel 中断）。返回是否成功。

        pip 组安装前确保数据运行时解释器就绪（首次自动从内置基座复制）。
        """
        if self._stop.is_set():
            self.on_state(component_id, STATE_CANCELLED)
            return False
        comp = COMPONENT_BY_ID.get(component_id)
        if comp is None:
            raise RuntimeError2(f"未知组件: {component_id}")
        os.makedirs(self.runtime_dir, exist_ok=True)
        self._current_op = component_id
        if comp.kind == "pip":
            if not runtime_data_python(self.runtime_dir):
                self.on_log("[安装] 正在准备数据运行时 Python（复制内置基座）…")
                self.on_state(comp.id, STATE_INSTALLING)
                try:
                    copy_bootstrap_to(self.runtime_dir,
                                      on_log=self.on_log)
                except RuntimeError2 as exc:
                    self.on_state(comp.id, STATE_FAILED)
                    self.on_log(f"[错误] {exc.message}")
                    return False
            if self._stop.is_set():
                self.on_state(comp.id, STATE_CANCELLED)
                return False
            return self._install_pip(comp)
        if comp.kind == "model":
            return self._install_models(comp)
        raise RuntimeError2(f"组件类型不可安装: {comp.kind}", "")

    def cancel(self) -> None:
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---------------- pip ----------------
    def _pip_cmd(self, python_exe: str, req_file: str,
                 index_url: str, extra_index_url: str = "") -> List[str]:
        """普通 pip 安装命令：参数列表 + 无 shell + 禁用版本检查 + 显式源。

        仅接受 allowlist 成员（调用方把关）；清单为本地文件，绝不读取远程
        requirements。extra_index_url 供 torch 组附加官方 CPU wheel index。
        """
        cmd = [python_exe, "-m", "pip", "install",
               "--disable-pip-version-check", "--no-input", "--upgrade"]
        if index_url:
            cmd += ["--index-url", index_url]
        if extra_index_url:
            cmd += ["--extra-index-url", extra_index_url]
        cmd += ["-r", req_file]
        return cmd

    def _install_pip(self, comp) -> bool:
        python_exe = runtime_data_python(self.runtime_dir)
        if not python_exe:
            # 兜底：源码/无基座开发路径，直接调用当前解释器（仅开发测试）
            python_exe = os.path.abspath(sys.executable)
        req_file = _write_requirements(self.runtime_dir, comp.group)
        self.on_state(comp.id, STATE_INSTALLING)
        from .runtime_components import (
            pip_source_label,
            pip_source_mode_label,
            source_chain_for_pip_group,
            torch_cpu_extra_index,
        )
        self.on_log(f"[安装] 安装 {comp.name}（下载源模式："
                    f"{pip_source_mode_label(self.source_mode)}）…")
        self._current_op = comp.id
        chain = source_chain_for_pip_group(comp.group, self.source_mode)
        # torch 组：CPU wheel 恒由官方 CPU 索引提供（SPEC 4：不伪装国内镜像）
        extra = torch_cpu_extra_index() if comp.group == "torch" else ""
        if not chain:
            self.on_state(comp.id, STATE_FAILED)
            self.on_log("[错误] 没有可用下载源（allowlist 为空）。")
            return False
        ok = False
        for idx, url in enumerate(chain):
            if self._stop.is_set():
                self.on_state(comp.id, STATE_CANCELLED)
                self.on_log("[取消] 安装已取消（不会回退下载源）。")
                return False
            label = pip_source_label(url)
            self.on_log(f"[来源] 尝试 {label}：{url or 'PyPI 默认'}")
            ok = self._run_pip(self._pip_cmd(python_exe, req_file, url, extra))
            if ok:
                self.on_log(f"[来源] {label} 安装成功。")
                break
            if self._stop.is_set():
                self.on_state(comp.id, STATE_CANCELLED)
                self.on_log("[取消] 安装已取消（不会回退下载源）。")
                return False
            if idx + 1 < len(chain):
                nxt = pip_source_label(chain[idx + 1])
                self.on_log(f"[切换] {label} 失败，改用 {nxt}。")
        if not ok:
            self.on_state(comp.id, STATE_FAILED)
            self.on_log(f"[错误] {comp.name} 安装失败：全部可用下载源"
                        f"尝试完毕（或已取消）。")
            return False
        # 安装后 import 探针验证
        self.on_state(comp.id, STATE_VERIFYING)
        self.on_log(f"[校验] 验证 {comp.name} …")
        ok_v, detail = verify_component(self.runtime_dir, comp.id, fast=False)
        if not ok_v:
            self.on_state(comp.id, STATE_FAILED)
            self.on_log(f"[错误] {comp.name} 安装后验证失败：{detail}")
            return False
        self.on_state(comp.id, STATE_OK)
        self.on_log(f"[完成] {comp.name} 安装并验证通过。")
        self._cleanup_artifacts()
        return True

    def _run_pip(self, cmd: List[str]) -> bool:
        """运行 pip：参数列表 + 无 shell + 可取消/超时；输出实时脱敏转发。

        读取线程边读边转发，避免管道缓冲阻塞 pip；取消用 taskkill 进程树。
        """
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x08000000
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=creationflags)
        except OSError as exc:
            self.on_log(f"[错误] 无法启动 pip：{exc}")
            return False

        def _reader():
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    self.on_log(redact(line.rstrip("\n")))
            except (ValueError, OSError):
                pass
            finally:
                close = getattr(proc.stdout, "close", None)
                if close is not None:
                    try:
                        close()
                    except (ValueError, OSError):
                        pass

        import threading as _t
        reader = _t.Thread(target=_reader, daemon=True)
        reader.start()
        deadline = time.time() + _PIP_TIMEOUT
        while True:
            if self._stop.is_set():
                self._terminate(proc)
                self.on_state(self._current_op, STATE_CANCELLED)
                self.on_log("[取消] 安装已取消。")
                reader.join(timeout=3)
                return False
            if proc.poll() is not None:
                break
            if time.time() > deadline:
                self._terminate(proc)
                self.on_state(self._current_op, STATE_FAILED)
                self.on_log("[错误] pip 安装超时，已中止。")
                reader.join(timeout=3)
                return False
            time.sleep(0.1)
        reader.join(timeout=5)
        code = proc.returncode
        if code != 0:
            self.on_state(self._current_op, STATE_FAILED)
            self.on_log(f"[错误] pip 安装失败（退出码 {code}）。")
            return False
        return True

    @staticmethod
    def _terminate(proc) -> None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.kill()
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _cleanup_artifacts(self) -> None:
        for name in (".req-automation.txt", ".req-ocr.txt", ".req-torch.txt",
                     ".import-probe.py"):
            try:
                os.unlink(os.path.join(self.runtime_dir, name))
            except OSError:
                pass

    # ---------------- 模型 ----------------
    def _install_models(self, comp) -> bool:
        models_dir = _models_rel(self.runtime_dir)
        os.makedirs(models_dir, exist_ok=True)
        dl_dir = os.path.join(self.runtime_dir, DOWNLOADS_DIR)
        os.makedirs(dl_dir, exist_ok=True)
        total = len(comp.model_keys)
        for idx, key in enumerate(comp.model_keys, start=1):
            if self._stop.is_set():
                self.on_state(comp.id, STATE_CANCELLED)
                self.on_log("[取消] 模型下载已取消。")
                return False
            src = EASYOCR_MODEL_SOURCES[key]
            self.on_state(comp.id, STATE_DOWNLOADING)
            self.on_log(f"[下载] {src['label']}（{idx}/{total}）…")
            self._current_op = comp.id
            ok = self._download_model(src, dl_dir, models_dir)
            if not ok:
                if self._stop.is_set():
                    self.on_state(comp.id, STATE_CANCELLED)
                else:
                    self.on_state(comp.id, STATE_FAILED)
                return False
        # 全部模型落位后做存在性+体积校验
        self.on_state(comp.id, STATE_VERIFYING)
        st = _model_status(models_dir)
        bad = [EASYOCR_MODEL_SOURCES[k]["label"] for k in comp.model_keys
               if not st.get(k)]
        if bad:
            if self._stop.is_set():
                self.on_state(comp.id, STATE_CANCELLED)
            else:
                self.on_state(comp.id, STATE_FAILED)
            self.on_log("[错误] 模型校验失败：" + "、".join(bad))
            return False
        self.on_state(comp.id, STATE_OK)
        self.on_log("[完成] EasyOCR 离线模型就绪。")
        return True

    def _download_model(self, src: dict, dl_dir: str, models_dir: str) -> bool:
        """下载官方 zip -> 临时目录 -> 只抽取白名单 .pth -> 原子入位。"""
        # 来源校验（allowlist）
        from .runtime_components import is_allowed_model_url
        url = src["url"]
        if not is_allowed_model_url(url):
            self.on_log(f"[错误] 模型下载 URL 不在允许列表：{url}")
            return False
        label = src.get("label") or os.path.basename(url) or url
        # 模型来源保持官方 GitHub release；国内镜像只覆盖 pip 依赖（SPEC 4）。
        # 任何失败都给出具体官方地址，便于复制诊断日志定位。
        def _log_fail(reason: str):
            self.on_log(f"[错误] {label} 下载失败：{reason}")
            self.on_log(f"[来源] {label} 官方地址（GitHub release）：{url}")
        try:
            zip_path = os.path.join(dl_dir, os.path.basename(url) or "model.zip")
            self._http_download(url, zip_path, self._dl_progress)
        except RuntimeError2 as exc:
            _log_fail(exc.message)
            return False
        except Exception as exc:  # noqa: BLE001
            _log_fail(redact(str(exc)))
            return False
        # 最小体积校验
        try:
            if os.path.getsize(zip_path) < _MIN_MODEL_ZIP:
                _log_fail("下载文件过小（下载不完整或来源异常）。")
                return False
            target_file = src["file"]
            with zipfile.ZipFile(zip_path) as zf:
                for info in zf.infolist():
                    name = os.path.basename(info.filename)
                    if name != target_file:
                        continue
                    if info.file_size < src["min_bytes"]:
                        _log_fail(f"{name} 解压内容过小（不完整）。")
                        return False
                    tmp_pth = os.path.join(dl_dir, name + ".part")
                    with zf.open(info) as zsrc, open(tmp_pth, "wb") as zdst:
                        shutil.copyfileobj(zsrc, zdst, 1024 * 1024)
                    if os.path.getsize(tmp_pth) < src["min_bytes"]:
                        _log_fail(f"{name} 校验失败（不完整）。")
                        try:
                            os.unlink(tmp_pth)
                        except OSError:
                            pass
                        return False
                    # 原子替换入位
                    final = os.path.join(models_dir, name)
                    os.replace(tmp_pth, final)
                    self.on_log(f"[完成] {label} 就绪：{name}")
                    return True
            _log_fail(f"zip 内未找到 {target_file}（内容异常）。")
            return False
        except zipfile.BadZipFile:
            _log_fail("下载文件不是有效 zip（可能被拦截/不完整）。")
            return False
        finally:
            try:
                os.unlink(zip_path)
            except OSError:
                pass

    def _http_download(self, url: str, dest: str,
                       on_progress: Optional[Callable[[int], None]] = None,
                       timeout: float = _NET_TIMEOUT) -> None:
        """带进度/超时的 HTTPS 下载（失败抛异常）。按块写临时文件。"""
        req = urllib.request.Request(url, headers={"User-Agent": "Coin11Helper/0.3"})
        tmp = dest + ".part"
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                last = time.time()
                with open(tmp, "wb") as fh:
                    while True:
                        if self._stop.is_set():
                            raise RuntimeError2("下载已取消", "")
                        if time.time() - last > timeout:
                            raise RuntimeError2("下载超时", "")
                        chunk = resp.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        last = time.time()
                        if on_progress and total:
                            on_progress(int(got * 100 / total))
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _dl_progress(self, pct: int) -> None:
        self.on_progress(self._current_op, pct)


# ---------------------------------------------------------------------------
# 顶层便捷函数（供 GUI / 其它模块使用）
# ---------------------------------------------------------------------------

def copy_bootstrap_to(runtime_dir: str,
                      on_log: Optional[Callable[[str], None]] = None) -> str:
    """把轻量发行内置 python-bootstrap 复制成数据运行时解释器（首次安装用）。

    复制自包含 base 树（python.exe / python312.dll / Lib / DLLs / Scripts 等）。
    幂等：目标已存在 python.exe 则直接返回。返回数据运行时解释器路径。
    """
    log = on_log or (lambda _t: None)
    py = runtime_data_python(runtime_dir)
    if py and os.path.isfile(py):
        return py
    boot = bootstrap_python_exe()
    src_root = os.path.dirname(os.path.abspath(boot)) if boot else ""
    if not src_root or not os.path.isfile(os.path.join(src_root, "python.exe")):
        raise RuntimeError2("未找到内置 Python 基座（发行目录不完整）。",
                            "请重新安装轻量版；若在源码模式开发，请直接使用"
                            "完整构建的发行目录。")
    dst_py_dir = os.path.join(runtime_dir, RUNTIME_PY_SUBDIR)
    os.makedirs(runtime_dir, exist_ok=True)
    log("[安装] 复制内置 Python 基座到数据运行时（首次约 1–2 分钟）…")
    shutil.copytree(src_root, dst_py_dir, dirs_exist_ok=True)
    return runtime_data_python(runtime_dir)


def task_interpreter() -> str:
    """任务解释器定位（与 runtime.runtime_python_exe 同语义，独立实现避免循环导入）。

    1. 环境变量 COIN11_RUNTIME_PYTHON（显式）；
    2. 非冻结源码：当前解释器；
    3. 冻结：用户数据运行时解释器（轻量版安装后出现）；
    4. 冻结：发行根 runtime\\python\\python.exe（0.3.0 完整运行时内置）。
    """
    explicit = os.environ.get("COIN11_RUNTIME_PYTHON", "")
    if explicit and os.path.isfile(explicit):
        return os.path.abspath(explicit)
    if not getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable) or ""
    data_py = runtime_data_python(default_user_runtime_dir())
    if data_py:
        return data_py
    bundled = os.path.join(constants.app_base_dir(),
                           constants.DIR_RUNTIME_PY, "python.exe")
    return bundled if os.path.isfile(bundled) else ""


def complete_bundled_runtime_present() -> bool:
    """发行根是否内置完整运行时（0.3.0 runtime\\python，自带全部依赖/模型）。"""
    if not getattr(sys, "frozen", False):
        return False
    return os.path.isfile(os.path.join(
        constants.app_base_dir(), constants.DIR_RUNTIME_PY, "python.exe"))


def ensure_runtime_ready(runtime_dir: str) -> Tuple[bool, str, List[str]]:
    """任务开始前调用：全量验证任务解释器与组件。

    返回 (ok, message, missing_component_ids)。任何组件缺失/损坏都返回 False，
    调用方不得启动子进程，应引导用户去下载中心。

    判定：
    - 源码（非冻结）：开发模式，不设门禁（脚本依赖由当前解释器自担）。
    - 冻结且发行根内置完整运行时：直接视为就绪（0.3.0 完整发行自带依赖与模型）。
    - 冻结且只有数据运行时（轻量版）：对数据运行时解释器跑 import 探针、
      对模型目录做存在性/体积校验，缺则列出缺失组件。
    """
    missing: List[str] = []
    ordered = [c for c in COMPONENT_BY_ID.values()
               if c.id != COMPONENT_PYTHON_BOOTSTRAP.id]
    if not getattr(sys, "frozen", False):
        return True, "开发模式：使用当前 Python 解释器。", []
    if complete_bundled_runtime_present():
        return True, "完整运行时已内置（0.3.0 完整发行版）。", []
    py = task_interpreter()
    if not py:
        return False, "未找到可用运行组件（请先到“下载中心”下载必需组件）。", \
            [c.id for c in ordered]
    for comp in ordered:
        if comp.kind == "model":
            ok, _ = verify_model_anywhere()
        else:
            ok, _ = run_import_probe(py, comp.probe_modules())
        if not ok:
            missing.append(comp.id)
    if missing:
        labels = "、".join(COMPONENT_BY_ID[c].name for c in missing)
        return False, f"缺少运行组件：{labels}（请到“下载中心”下载）。", missing
    return True, "运行组件就绪。", []


def verify_model_anywhere() -> Tuple[bool, str]:
    """模型是否就绪：环境变量 / 数据运行时 / 完整发行内置任一位置齐全即可。"""
    from . import constants as C
    candidates = []
    env_dir = os.environ.get(C.ENV_EASYOCR_MODEL_DIR, "").strip()
    if env_dir:
        candidates.append(env_dir)
    candidates.append(_models_rel(default_user_runtime_dir()))
    candidates.append(os.path.join(constants.app_base_dir(),
                                   C.DIR_RUNTIME_MODELS))
    for cand in candidates:
        st = _model_status(cand)
        if all(st.values()):
            return True, cand
    return False, "缺少 EasyOCR 离线模型（请到“下载中心”下载）"
