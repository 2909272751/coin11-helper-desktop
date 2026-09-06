"""PyInstaller 真正的进程入口（避免 __main__.py 包内相对导入问题）。

以普通脚本方式运行（无父包），因此全部使用绝对导入，并先把仓库根放入
sys.path（冻结时由 PyInstaller 提供 desktop_app 包路径）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_app import constants  # noqa: E402
from desktop_app.adb_service import AdbService  # noqa: E402
from desktop_app.logutil import setup_logging  # noqa: E402
from desktop_app.settings_store import SettingsStore  # noqa: E402
from desktop_app.task_catalog import TaskCatalog  # noqa: E402
from desktop_app.update_service import UpdateService  # noqa: E402
from desktop_app.__main__ import ensure_data_runtime  # noqa: E402


def main() -> int:
    data_dir = constants.default_data_dir()
    for sub in ("logs", "scripts"):
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
    setup_logging(os.path.join(data_dir, "logs"))

    script_root = ensure_data_runtime(data_dir)
    adb = AdbService()
    settings = SettingsStore(data_dir)
    # 进程级注入用户设置的数据运行时目录（settings.runtime_dir），使
    # runtime_python_exe / easyocr_model_dir / 下载中心读到同一目录
    _inject_data_runtime(settings)
    update = UpdateService(data_dir, on_log=lambda t: None)
    catalog = TaskCatalog(script_root)

    from desktop_app.app import run_gui
    return run_gui(adb, settings, update, catalog)


def _inject_data_runtime(settings) -> None:
    from desktop_app.runtime_manager import ENV_DATA_RUNTIME
    val = settings.get("runtime_dir", "")
    if not val:
        return
    os.environ[ENV_DATA_RUNTIME] = os.path.abspath(val)


if __name__ == "__main__":
    sys.exit(main())
