"""任务目录与脚本路径白名单。

只把“上游文件已存在、文件名与默认任务表完全一致”的脚本视为可运行任务，
防止任意路径/脚本注入。限时活动（limited 组）与日常任务使用同一白名单与
可用性规则，仅分组展示不同。
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from . import constants

_SAFE_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\.\- ]+\.py$")
_DEPS_FILE = ".coin11-deps.json"


def _read_deps_bundled(root_dir: str) -> bool:
    """读脚本根目录的 .coin11-deps.json：标记发行版是否捆绑了重型依赖。

    文件缺失视为未捆绑（默认 False）——上游任务都 import torch/easyocr 等，
    未捆绑时运行会失败可见，界面应如实提示，绝不伪装可完整执行。
    """
    try:
        with open(os.path.join(root_dir, _DEPS_FILE), encoding="utf-8") as fh:
            import json
            data = json.load(fh)
        return bool(data.get("heavy_deps_bundled", False))
    except (OSError, ValueError):
        return False


def build_task_index(root_dir: str) -> Dict[str, dict]:
    """扫描受支持任务表（日常 + 限时活动），返回 {task_id: {..., 'path', 'exists'}}。

    root_dir: 脚本所在目录（发行时 = scripts/current，源码时 = 仓库根）。
    同名脚本必须真实存在且文件名在白名单内，否则标记 exists=False，
    UI 应显示为“脚本缺失”，不允许运行。
    """
    deps_bundled = _read_deps_bundled(root_dir)
    index: Dict[str, dict] = {}
    for spec in constants.ALL_TASKS:
        script = spec["script"]
        entry = dict(spec)
        entry["path"] = ""
        entry["exists"] = False
        entry["deps_bundled"] = deps_bundled
        entry["unavailable_reason"] = ""
        if not _SAFE_NAME_RE.match(script):
            entry["unavailable_reason"] = "脚本文件名非法"
        else:
            candidate = os.path.join(root_dir, script)
            if os.path.isfile(candidate):
                entry["path"] = candidate
                entry["exists"] = True
            else:
                entry["unavailable_reason"] = "脚本缺失（请同步最新脚本）"
        index[spec["id"]] = entry
    return index


def default_task_ids() -> List[str]:
    return [spec["id"] for spec in constants.DEFAULT_TASKS]


def limited_task_ids() -> List[str]:
    return [spec["id"] for spec in constants.LIMITED_TASKS]


class TaskCatalog:
    """任务目录：持有当前脚本根目录下可运行任务；root 目录在同步后原子更新。"""

    def __init__(self, root_dir: str):
        self._root_dir = root_dir
        self.refresh()

    def refresh(self) -> None:
        self._index = build_task_index(self._root_dir)

    @property
    def root_dir(self) -> str:
        return self._root_dir

    def set_root_dir(self, root_dir: str) -> None:
        self._root_dir = root_dir
        self.refresh()

    def list_tasks(self) -> List[dict]:
        return [dict(v) for v in self._index.values()]

    def list_group(self, group: str) -> List[dict]:
        """按分组列出任务（daily=日常默认，limited=限时活动）。"""
        return [dict(v) for v in self._index.values()
                if v.get("group") == group]

    def daily_tasks(self) -> List[dict]:
        return self.list_group(constants.GROUP_DAILY)

    def limited_tasks(self) -> List[dict]:
        return self.list_group(constants.GROUP_LIMITED)

    def get(self, task_id: str) -> Optional[dict]:
        entry = self._index.get(task_id)
        return dict(entry) if entry else None

    def resolve(self, task_id: str) -> str:
        """返回脚本绝对路径；任务不存在/不在白名单/文件缺失都抛错（防注入）。"""
        entry = self._index.get(task_id)
        if not entry:
            raise ValueError(f"未知任务: {task_id}")
        if not entry["exists"] or not entry["path"]:
            raise ValueError(f"任务脚本不可用: {entry['title']} "
                             f"（{entry.get('unavailable_reason', '')}）")
        return entry["path"]

    def validate_ids(self, task_ids: List[str]) -> List[str]:
        """只保留存在于受支持任务表（日常 + 限时）中的任务 ID（防任意命令注入）。"""
        return [tid for tid in task_ids if tid in self._index]

    def is_limited(self, task_id: str) -> bool:
        entry = self._index.get(task_id)
        return bool(entry and entry.get("group") == constants.GROUP_LIMITED)
