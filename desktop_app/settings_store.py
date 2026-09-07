"""设置持久化（%LOCALAPPDATA%\\Coin11Helper）。

存储上次设备/任务选择、自动接管 ADB 开关、最近一次结果摘要、下载源模式等。
JSON 原子写入，任何时刻读取都得到完整旧值或完整新值。
"""
from __future__ import annotations

import json
import os
import tempfile

from .constants import APP_NAME

_DEFAULTS = {
    "last_device_serial": "",
    "last_task_ids": [],
    "auto_takeover_adb": True,
    "last_result_summary": "",
    "last_run_at": "",
    # 0.4.0 轻量版：数据运行时目录（空 = 默认 %LOCALAPPDATA%\\Coin11Helper\\runtime）
    "runtime_dir": "",
    # 下载中心 pip 下载源模式（smart/tuna/aliyun/official，见 runtime_components）
    "pip_source": "smart",
}


class SettingsStore:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "settings.json")
        self._values = dict(_DEFAULTS)
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in _DEFAULTS:
                    if key in data:
                        self._values[key] = data[key]
        except (OSError, ValueError):
            # 缺失或损坏：使用默认值，不覆盖（下次保存时修复）
            self._values = dict(_DEFAULTS)

    def get(self, key: str, default=None):
        return self._values.get(key, _DEFAULTS.get(key, default))

    def set(self, key: str, value) -> None:
        self._values[key] = value
        self.save()

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp",
                                   dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._values, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @property
    def last_device_serial(self) -> str:
        return str(self._values.get("last_device_serial", ""))

    @property
    def last_task_ids(self) -> list:
        value = self._values.get("last_task_ids", [])
        return list(value) if isinstance(value, list) else []

    @property
    def auto_takeover_adb(self) -> bool:
        return bool(self._values.get("auto_takeover_adb", True))

    @property
    def pip_source(self) -> str:
        """当前 pip 下载源模式（默认 smart）。"""
        val = self._values.get("pip_source", "smart")
        return val if isinstance(val, str) and val else "smart"
