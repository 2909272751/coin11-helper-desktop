"""生成“出厂种子”脚本目录（scripts/current），供发行包捆绑与首次运行复制。

做法（与 UpdateService 的同步副本布局一致）：
1. 从仓库根复制受支持的上游任务脚本（默认任务表内）与运行需要的基础文件
   （utils.py 已含 select_device 兼容补丁；img/；LICENSE；README_DESKTOP.md；
   不受支持的历史活动脚本不捆绑）。
2. 写入 .coin11-deps.json：0.2.0 完整运行时发行版已捆绑重型运行时依赖
   （torch/easyocr/ddddocr/opencv/uiautomator2 随发行内置 runtime/python），
   标记 heavy_deps_bundled=true。
3. 写入 .coin11-meta.json：记录来源 commit（构建时注入）与打包时间，
   供界面显示“出厂版本”。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from . import compat_patch, constants
from .task_catalog import _SAFE_NAME_RE

# 需要捆绑的顶层条目（白名单，避免把无关文件打进发行版）。
# 0.3.0 起捆绑全部受支持任务脚本：日常任务 + 明确标注“限时活动/可能过期”的活动脚本。
SEED_FILES = [spec["script"] for spec in constants.ALL_TASKS]
SEED_FILES += [
    "utils.py",
    "LICENSE",
    "README_DESKTOP.md",
]
SEED_DIRS = ["img"]

# 额外保留的兼容补丁说明（可选）
PATCH_NOTE = "COMPAT_PATCHES.md"


def _git_head(repo_root: str) -> str:
    try:
        out = subprocess.run(["git", "-C", repo_root, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip()[:40]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _copy_retry(src: str, dst: str, tries: int = 5, delay: float = 0.4) -> None:
    """带短暂重试的复制：规避 Windows 杀软实时扫描导致的瞬态占用。"""
    import time as _time
    last = None
    for attempt in range(tries):
        try:
            shutil.copy2(src, dst)
            return
        except OSError as exc:
            last = OSError(f"{exc} | src={src!r} attempt={attempt}") if attempt >= tries - 1 else exc
            _time.sleep(delay)
    raise last  # type: ignore[misc]


def build_seed(repo_root: str, dest: str, commit: Optional[str] = None,
               heavy_deps_bundled: bool = True) -> str:
    """把仓库当前内容打成可运行种子目录。返回该目录路径。

    heavy_deps_bundled=False 时（轻量构建调用）写入 .coin11-deps.json 标记
    未捆绑：界面据此显示“运行组件待下载”，任务在组件装齐前由运行时卡片拦截。
    """
    os.makedirs(dest, exist_ok=True)
    for name in SEED_FILES:
        src = os.path.join(repo_root, name)
        if not os.path.isfile(src):
            raise FileNotFoundError(f"缺少种子文件: {name}")
        _copy_retry(src, os.path.join(dest, name))
    for dname in SEED_DIRS:
        src_dir = os.path.join(repo_root, dname)
        dst_dir = os.path.join(dest, dname)
        if not os.path.isdir(src_dir):
            raise FileNotFoundError(f"缺少种子目录: {dname}")
        if os.path.isdir(dst_dir):
            shutil.rmtree(dst_dir)
        shutil.copytree(src_dir, dst_dir)
    # 兼容补丁说明
    note_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "docs", "desktop", PATCH_NOTE)
    if os.path.isfile(note_path):
        shutil.copy2(note_path, os.path.join(dest, PATCH_NOTE))
    # 依赖标记：完整构建 = 已捆绑重型依赖；轻量构建 = 未捆绑（false）
    note = constants.HEAVY_DEPS_NOTE if heavy_deps_bundled else (
        "轻量版：运行组件未捆绑，首次使用请到桌面端“下载中心”下载必需组件"
        "（Python 基座内置，仅依赖/模型在线下载）。")
    with open(os.path.join(dest, ".coin11-deps.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"heavy_deps_bundled": bool(heavy_deps_bundled),
                   "note": note}, fh,
                  ensure_ascii=False, indent=2)
    # 版本 meta
    sha = commit or _git_head(repo_root)
    with open(os.path.join(dest, ".coin11-meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"commit": sha,
                   "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "source": constants.UPSTREAM_REPO_URL,
                   "mode": "factory-seed"}, fh, ensure_ascii=False, indent=2)
    # 确保 utils.py 已带补丁（幂等）
    utils_path = os.path.join(dest, "utils.py")
    with open(utils_path, encoding="utf-8") as fh:
        text = fh.read()
    patched = compat_patch.patch_utils_select_device(text)
    if patched != text:
        with open(utils_path, "w", encoding="utf-8") as fh:
            fh.write(patched)
    # 裸 u2.connect() 的脚本也补丁
    for name in os.listdir(dest):
        if not name.endswith(".py") or name == "utils.py":
            continue
        full = os.path.join(dest, name)
        with open(full, encoding="utf-8") as fh:
            t = fh.read()
        patched2 = compat_patch.apply_known_patches(name, t)
        if patched2 != t:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(patched2)
    return dest


if __name__ == "__main__":
    # 用法: python -m desktop_app.make_seed <dest> [commit]
    # 仓库根由模块位置自动推导，不接受参数（避免把源文件复制到自身）。
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dest = os.environ.get("COIN11_SEED_DEST") or sys.argv[1]
    commit = sys.argv[2] if len(sys.argv) > 2 else ""
    bundled = os.environ.get("COIN11_SEED_HEAVY_DEPS", "1") != "0"
    print(build_seed(repo, dest, commit, heavy_deps_bundled=bundled))
