"""桌面应用入口：python -m desktop_app

组装服务层并启动 GUI。

目录策略：
- 数据目录（可写）= %LOCALAPPDATA%\\Coin11Helper：settings.json、logs、
  scripts/current 与 scripts/previous（同步/回退都发生在这里）。
- 出厂种子 = 程序目录下 scripts/current（只读捆绑）。首次启动若无数据副本，
  静默把出厂种子复制到数据目录作为初始 current；此后运行/更新/回退
  全部基于数据目录副本 —— 程序目录只读也能正常工作。
- 不在启动时做任何自动联网同步。
"""
from __future__ import annotations

import os
import shutil
import sys

from . import constants
from .adb_service import AdbService
from .logutil import setup_logging
from .settings_store import SettingsStore
from .task_catalog import TaskCatalog
from .update_service import UpdateService

DATA_DIR_SUB = "scripts"


def _copy_seed(src_dir: str, dst_dir: str) -> None:
    """把种子目录复制成数据副本（白名单：受支持任务脚本 + 基础文件 + img）。

    源码模式下 src 是仓库根，含 .git/.codex-helper 等无关/占用文件，必须过滤。
    0.3.0 起白名单含全部受支持任务（日常 + 限时活动），与 DEFAULT_TASKS 列表无关。
    """
    from . import constants as C
    allowed_names = {spec["script"] for spec in C.ALL_TASKS}
    allowed_names.update({"utils.py", "LICENSE", "README_DESKTOP.md",
                          "COMPAT_PATCHES.md",
                          ".coin11-deps.json", ".coin11-meta.json"})
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        full = os.path.join(src_dir, name)
        if os.path.isfile(full) and name in allowed_names:
            shutil.copy2(full, os.path.join(dst_dir, name))
        elif os.path.isdir(full) and name == "img":
            shutil.copytree(full, os.path.join(dst_dir, name))
    # 无 meta/deps 时补写：源码模式（无捆绑运行时）保持可见标记 false；
    # 冻结发行 seed 总带 true 的 deps 文件，不会走到这里。
    if not os.path.isfile(os.path.join(dst_dir, ".coin11-deps.json")):
        import json
        bundled = getattr(sys, "frozen", False)
        with open(os.path.join(dst_dir, ".coin11-deps.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"heavy_deps_bundled": bundled,
                       "note": C.HEAVY_DEPS_NOTE}, fh,
                      ensure_ascii=False, indent=2)


def ensure_data_runtime(data_dir: str) -> str:
    """返回应作为当前任务脚本根的目录。

    数据目录 scripts/current 不存在时，把出厂种子目录复制过去作为初始 current。
    种子目录判定：
      - 冻结发行：_internal/scripts/current（已含 meta/deps）。
      - 源码模式：仓库根（任务脚本在顶层），复制时白名单过滤并补写标记。
    若数据目录已有 current（同步/回退产生），直接使用之 —— 程序目录只读可运行。
    """
    data_scripts = os.path.join(data_dir, DATA_DIR_SUB)
    data_current = os.path.join(data_scripts, constants.DIR_CURRENT)
    if os.path.isdir(data_current):
        return data_current
    seed = constants.seed_current_dir()
    if os.path.isdir(seed) and os.path.isdir(os.path.join(seed, "img")):
        os.makedirs(data_scripts, exist_ok=True)
        _copy_seed(seed, data_current)
        return data_current
    return seed if os.path.isdir(seed) else constants.app_base_dir()


def main() -> int:
    data_dir = constants.default_data_dir()
    for sub in ("logs", DATA_DIR_SUB):
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
    setup_logging(os.path.join(data_dir, "logs"))

    script_root = ensure_data_runtime(data_dir)
    adb = AdbService()
    settings = SettingsStore(data_dir)
    # 进程级注入用户设置的数据运行时目录，保证运行组件/任务解释器定位一致
    rt = settings.get("runtime_dir", "")
    if rt:
        from .runtime_manager import ENV_DATA_RUNTIME
        os.environ[ENV_DATA_RUNTIME] = os.path.abspath(str(rt))
    # 更新/回退都以数据目录为基地（scripts/current、scripts/previous 在其中）
    update = UpdateService(data_dir, on_log=lambda t: None)
    catalog = TaskCatalog(script_root)

    from .app import run_gui
    return run_gui(adb, settings, update, catalog)


if __name__ == "__main__":
    sys.exit(main())
