# -*- coding: utf-8 -*-
"""测试共享辅助：按受支持任务表生成上游快照/种子文件字典。

随 DEFAULT_TASKS 扩展（0.3.0 起 11 个日常任务 + 4 个限时活动）自动保持一致。
仅被 tests/*.py 导入，不参与单元测试发现（不以 test_ 开头）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_app import constants  # noqa: E402


def all_daily_scripts() -> list:
    """日常任务脚本文件名列表（validate_snapshot 必须全部存在）。"""
    return [spec["script"] for spec in constants.DEFAULT_TASKS]


def all_supported_scripts() -> list:
    """全部受支持（日常 + 限时）脚本文件名。"""
    return [spec["script"] for spec in constants.ALL_TASKS]


def upstream_files(overrides=None, remove=(), extra=None):
    """生成一个可通过 UpdateService.validate_snapshot 的顶层文件字典。

    - 含全部受支持脚本（日常 + 限时活动，默认内容 "import time"）、utils.py、
      LICENSE，以及 "img" 目录标记（值为 None，tarball 语义 = 目录）。
    - overrides: {文件名: 内容} 覆盖默认内容（用于语法错误等）。
    - remove:    需要移除的文件名集合（模拟缺失）。
    - extra:     额外文件 {名: 内容}。
    """
    files = {name: "import time\n" for name in all_supported_scripts()}
    files["utils.py"] = "def select_device():\n    pass\n"
    files["LICENSE"] = "Apache 2.0\n"
    files["img"] = None
    if overrides:
        files.update(overrides)
    for name in remove:
        files.pop(name, None)
    if extra:
        files.update(extra)
    return files
