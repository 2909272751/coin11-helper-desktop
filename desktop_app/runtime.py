"""运行时定位：优先用户数据运行时，其次轻量内置基座 / 完整发行内置解释器。

0.4.0 轻量安装版 + 0.3.0 完整运行时并存的定位规则（SPEC）：
1. 环境变量 COIN11_RUNTIME_PYTHON（显式测试/排障）仍最优先（等价已验证候选）；
2. 用户设置的数据运行时解释器（默认 %LOCALAPPDATA%\\Coin11Helper\\runtime\\
   python\\python.exe）：轻量版在“下载中心”安装组件后出现，是任务唯一接受的
   已验证解释器；
3. 完整运行时发行版：发行根 runtime\\python\\python.exe（内置全部依赖）；
4. 轻量发行内置 runtime\\python-bootstrap\\python.exe —— 仅 bootstrap，不作为
   任务解释器（没有任务依赖，除非数据运行时已把依赖装上，但它的位置在数据
   目录；这里返回它只会导致误用，因此源码/轻量未装数据运行时返回 ""，
   由下载中心引导用户安装）；
5. 非冻结（源码开发）：sys.executable —— 桌面壳与任务脚本共用当前解释器。

EasyOCR 离线模型目录：优先 COIN11_EASYOCR_MODEL_DIR 环境变量；否则用户数据
运行时的 easyocr-models（轻量版下载/完整版可配置共用）；否则完整发行内置
runtime\\easyocr-models；都不存在返回 "" 由调用方决定。
"""
from __future__ import annotations

import os
import sys

from . import constants
from .runtime_manager import (
    default_user_runtime_dir,
    runtime_data_python,
)

# 允许测试/自定义时显式指定内置解释器（正式发行不设置）
_ENV_RUNTIME_PYTHON = "COIN11_RUNTIME_PYTHON"


def _frozen_base() -> str:
    """冻结发行根 = exe 所在目录；源码模式返回仓库根。"""
    return constants.app_base_dir()


def _candidate(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def runtime_python_exe() -> str:
    """返回用于运行上游任务脚本的 Python 解释器绝对路径；找不到返回 ""。

    选择顺序（SPEC“运行时定位”）：
      1. 环境变量 COIN11_RUNTIME_PYTHON（显式指定，测试/排障用）；
      2. 冻结发行：用户设置的数据运行时解释器（轻量版已验证数据运行时；
         组件装好后即出现；未装时返回 "" 由调用方引导下载中心）；
      3. 冻结发行：发行根 runtime\\python\\python.exe（0.3.0 完整运行时内置）；
      4. 源码（非冻结）：sys.executable —— 桌面壳与任务脚本共用当前解释器。
    全部不可用返回 ""（由调用方在启动前给出明确失败，绝不递归启动 EXE）。

    注意：轻量版在“数据运行时”装好前返回 ""（不会回退到只含 pip 的
    python-bootstrap 或 PyInstaller 壳的 _internal\\python.exe —— 两者都没有
    任务依赖，误用只会得到假成功/难读错误；界面应提示去“下载中心”）。
    """
    explicit = os.environ.get(_ENV_RUNTIME_PYTHON, "")
    if _candidate(explicit):
        return os.path.abspath(explicit)
    if not getattr(sys, "frozen", False):
        # 源码开发模式：使用当前解释器（桌面壳依赖已在其中）
        return os.path.abspath(sys.executable) or ""
    # 冻结发行：优先用户数据运行时（轻量版），再退回完整发行内置
    data_py = runtime_data_python(default_user_runtime_dir())
    if data_py:
        return data_py
    bundled = os.path.join(_frozen_base(), constants.DIR_RUNTIME_PY, "python.exe")
    return bundled if _candidate(bundled) else ""


def runtime_bundled() -> bool:
    """当前发行是否捆绑了完整运行时（内置解释器在旁）。源码模式为 False。"""
    if not getattr(sys, "frozen", False):
        return False
    exe = runtime_python_exe()
    if not exe:
        return False
    # 只有指向发行内置解释器才算捆绑（env 覆盖在冻结下通常不用于真实判断）
    return os.path.abspath(exe) != os.path.abspath(sys.executable)


def easyocr_model_dir() -> str:
    """返回 EasyOCR 离线模型目录（存在则用，否则返回空字符串让调用方决定）。

    - 优先取环境变量 COIN11_EASYOCR_MODEL_DIR（用户/测试可覆盖）；
    - 未设置时：
        * 冻结发行：用户数据运行时 easyocr-models（轻量版“下载中心”下载位置），
          再退发行根 runtime\\easyocr-models（0.3.0 完整运行时构建期预下载）；
        * 源码模式：环境变量未设置 -> ""（保持上游可运行默认，联网自动下载）。
    调用方只在该目录存在时注入 COIN11_EASYOCR_MODEL_DIR；不存在则完全不注入，
    让 EasyOCR 走自己的默认行为（保持上游可运行）。
    """
    override = os.environ.get(constants.ENV_EASYOCR_MODEL_DIR, "").strip()
    if override:
        return os.path.abspath(override)
    if not getattr(sys, "frozen", False):
        return ""
    # 用户数据运行时模型目录（轻量版“下载中心”下载位置）
    data_models = os.path.join(default_user_runtime_dir(), "easyocr-models")
    if os.path.isdir(data_models):
        return data_models
    candidate = os.path.join(_frozen_base(), constants.DIR_RUNTIME_MODELS)
    return candidate if os.path.isdir(candidate) else ""
