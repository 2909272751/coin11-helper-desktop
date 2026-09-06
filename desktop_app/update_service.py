"""上游脚本同步与版本回退服务。

- 同步源固定 https://github.com/czl0325/coin11-tb.git：请求 URL 一律与常量做
  精确字符串比较（UPSTREAM_REPO_URL / GITHUB_API_REPO / GITHUB_CODELOAD_PREFIX），
  不做子串/拼接，杜绝任意 URL 注入。
- 下载到本地缓存临时目录 -> 确认拿到的是 Git commit（40 位十六进制 sha）-> 应用
  受控兼容补丁 -> 做基础语法与清单校验 -> 原子切换 current/previous。
- 失败时保持 current 不变；提供“恢复上一版”。
- 不在启动时后台同步；只由用户点击触发。所有网络操作带超时。
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.request
from typing import Callable, Dict, Optional

from . import compat_patch, constants
from .task_catalog import _SAFE_NAME_RE

logger = logging.getLogger("coin11.update")

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMEOUT = 45.0

# codeload tarball 内层为单顶层目录 <repo>-<sha>/
_TOPLEVEL_ENTRY = "coin11-tb-{sha}"


class UpdateError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def validate_repo_url(url: str) -> bool:
    """URL 校验：必须与固定上游 HTTPS 源完全一致（防注入）。"""
    return isinstance(url, str) and url == constants.UPSTREAM_REPO_URL


def validate_commit(sha: str) -> bool:
    return bool(_COMMIT_RE.match(sha or ""))


def http_get_json(url: str, timeout: float = _TIMEOUT) -> dict:
    """GET GitHub API 并解析 JSON；只允许 czl0325/coin11-tb 仓库的 API 地址。"""
    if not url.startswith("https://api.github.com/repos/czl0325/coin11-tb/"):
        raise UpdateError("更新源 URL 校验失败，已拒绝。", "请勿修改程序内置的更新源。")
    req = urllib.request.Request(url, headers={"User-Agent": "Coin11Helper/0.1",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"查询上游仓库失败（HTTP {exc.code}）。",
                          "请检查网络后重试。" if exc.code != 403
                          else "上游接口限流，请稍后重试。")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError(f"无法访问上游仓库：{exc}", "请检查网络连接后重试。")


def http_get_bytes(url: str, timeout: float = _TIMEOUT) -> bytes:
    """下载源码包；只允许固定 codeload 前缀（commit sha 后校验 40 hex）。"""
    if not url.startswith(constants.GITHUB_CODELOAD_PREFIX):
        raise UpdateError("下载 URL 校验失败，已拒绝。", "")
    sha = url[len(constants.GITHUB_CODELOAD_PREFIX):]
    if not validate_commit(sha):
        raise UpdateError("提交号不合法，已拒绝下载。", "")
    req = urllib.request.Request(url, headers={"User-Agent": "Coin11Helper/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if len(data) < 512:
            raise UpdateError("下载内容异常（过小）。", "请稍后重试。")
        return data
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"下载失败（HTTP {exc.code}）。", "请检查网络后重试。")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"下载失败：{exc}", "请检查网络连接后重试。")


def extract_tarball(data: bytes, dest_dir: str, sha: str) -> str:
    """解压上游 tar.gz 到 dest_dir，返回解压出的单顶层目录路径。

    校验 gzip/tar 合法性，并确认含且仅含一个顶层目录（源码根）。
    """
    try:
        raw = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise UpdateError("下载内容不是有效的源码包。", "请重新同步。")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError as exc:
        raise UpdateError(f"源码包解压失败：{exc}", "请重新同步。")
    expected = _TOPLEVEL_ENTRY.format(sha=sha)
    top_level = set()
    with tf:
        members = tf.getmembers()
        for member in members:
            if member.isdir():
                parts = member.name.split("/")
                if len(parts) >= 1 and parts[0]:
                    top_level.add(parts[0])
            elif member.isfile():
                parts = member.name.split("/")
                if len(parts) >= 2:
                    top_level.add(parts[0])
        # 排除内层文件顶层的各种异常
        top = [t for t in top_level if t != ""]
        if not top or len(top) != 1:
            raise UpdateError("源码包结构异常（顶层目录不唯一）。", "请重新同步。")
        if top[0] != expected:
            raise UpdateError("源码包顶层目录与提交号不符，已拒绝。", "请重新同步。")
        os.makedirs(dest_dir, exist_ok=True)
        tf.extractall(dest_dir, filter="data")
    extracted = os.path.join(dest_dir, expected)
    if not os.path.isdir(extracted):
        raise UpdateError("解压后未找到源码根目录。", "请重新同步。")
    return extracted


class UpdateService:
    """管理 scripts/current 与 scripts/previous 两代副本的同步/回退。"""

    def __init__(self, base_dir: str,
                 on_log: Optional[Callable[[str], None]] = None,
                 fetch_json: Optional[Callable[[str], dict]] = None,
                 fetch_bytes: Optional[Callable[[str], bytes]] = None):
        self.base_dir = os.path.abspath(base_dir)
        self.scripts_root = os.path.join(self.base_dir, constants.DIR_SCRIPTS)
        self.current_dir = os.path.join(self.scripts_root, constants.DIR_CURRENT)
        self.previous_dir = os.path.join(self.scripts_root, constants.DIR_PREVIOUS)
        self.on_log = on_log or (lambda _t: None)
        self._fetch_json = fetch_json or http_get_json
        self._fetch_bytes = fetch_bytes or http_get_bytes
        self._lock = threading.Lock()
        self._meta_path = os.path.join(self.current_dir, ".coin11-meta.json")
        os.makedirs(self.scripts_root, exist_ok=True)

    # ---- 状态 ----
    def _read_meta(self, meta_path: str) -> dict:
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def status(self) -> Dict[str, str]:
        """本地版本信息：current 与 previous 的 commit、更新时间。"""
        info = {"current_commit": "", "current_updated": "",
                "previous_commit": "", "previous_updated": "",
                "checked_at": ""}
        cur_meta = self._read_meta(
            os.path.join(self.current_dir, ".coin11-meta.json"))
        info["current_commit"] = str(cur_meta.get("commit", ""))
        info["current_updated"] = str(cur_meta.get("updated_at", ""))
        prev_meta = self._read_meta(
            os.path.join(self.previous_dir, ".coin11-meta.json"))
        info["previous_commit"] = str(prev_meta.get("commit", ""))
        info["previous_updated"] = str(prev_meta.get("updated_at", ""))
        return info

    def set_local_meta(self, commit: str, updated: str) -> None:
        """构建/安装时由 packaging 调用，记录本地快照 commit。"""
        os.makedirs(self.current_dir, exist_ok=True)
        self._write_meta(self.current_dir, commit, updated)

    @staticmethod
    def _write_meta(directory: str, commit: str, updated: str) -> None:
        payload = {"commit": commit, "updated_at": updated,
                   "source": constants.UPSTREAM_REPO_URL}
        with open(os.path.join(directory, ".coin11-meta.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    @staticmethod
    def _write_deps_bundled(directory: str) -> None:
        """在脚本根写入“已捆绑完整运行时”标记。

        运行时依赖由桌面发行本身捆绑（runtime/python 内置），与脚本来源无关；
        因此无论出厂种子、同步还是回退得到的 current，都恒定标记捆绑，界面
        不再出现“依赖待就绪”。
        """
        payload = {"heavy_deps_bundled": True,
                   "note": constants.HEAVY_DEPS_NOTE}
        with open(os.path.join(directory, ".coin11-deps.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ---- 校验 ----
    def validate_snapshot(self, snapshot_dir: str) -> Dict[str, object]:
        """清单 + 语法校验 + 补丁校验。返回 {'ok': bool, 'errors': [...]}。

        校验全部受支持任务（日常 + 限时活动）：同步/回退后的 current 必须能
        让界面列出的每一项都可运行，否则视为快照不完整。
        """
        errors = []
        base = snapshot_dir
        for spec in constants.ALL_TASKS:
            script = spec["script"]
            if not _SAFE_NAME_RE.match(script):
                errors.append(f"任务脚本名非法: {script}")
                continue
            path = os.path.join(base, script)
            if not os.path.isfile(path):
                errors.append(f"缺少任务脚本: {script}")
        utils_path = os.path.join(base, "utils.py")
        if not os.path.isfile(utils_path):
            errors.append("缺少 utils.py")
        if not os.path.isfile(os.path.join(base, "LICENSE")):
            errors.append("缺少上游 LICENSE")
        # 语法编译检查（顶层全部 .py 与 img 目录）
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if os.path.isfile(full) and name.endswith(".py"):
                try:
                    with open(full, encoding="utf-8") as fh:
                        source = fh.read()
                    compile(source, full, "exec")
                except SyntaxError as exc:
                    errors.append(f"语法错误 {name}: {exc}")
                except OSError as exc:
                    errors.append(f"无法读取 {name}: {exc}")
        if not os.path.isdir(os.path.join(base, "img")):
            errors.append("缺少 img 资源目录")
        return {"ok": not errors, "errors": errors}

    def apply_compat_patches_to(self, snapshot_dir: str) -> None:
        """把受控兼容补丁应用到快照副本（在副本上操作，不触碰仓库/当前副本）。"""
        utils_path = os.path.join(snapshot_dir, "utils.py")
        if os.path.isfile(utils_path):
            with open(utils_path, encoding="utf-8") as fh:
                text = fh.read()
            patched = compat_patch.patch_utils_select_device(text)
            if patched != text:
                with open(utils_path, "w", encoding="utf-8") as fh:
                    fh.write(patched)
        for name in sorted(os.listdir(snapshot_dir)):
            if not name.endswith(".py"):
                continue
            full = os.path.join(snapshot_dir, name)
            with open(full, encoding="utf-8") as fh:
                text = fh.read()
            patched = compat_patch.apply_known_patches(name, text)
            if patched != text:
                with open(full, "w", encoding="utf-8") as fh:
                    fh.write(patched)

    # ---- 同步 ----
    def sync(self) -> Dict[str, str]:
        """同步最新上游到 current；失败抛 UpdateError 且保持 current 不变。"""
        if not self._lock.acquire(blocking=False):
            raise UpdateError("已有同步在进行中。", "请等待当前同步完成。")
        try:
            return self._sync_locked()
        finally:
            self._lock.release()

    def _sync_locked(self) -> Dict[str, str]:
        self.on_log("正在连接上游仓库 …")
        repo = self._fetch_json(constants.GITHUB_API_REPO)
        default_branch = repo.get("default_branch", "") or "master"
        head = repo.get("sha") or (repo.get("object") or {}).get("sha")
        if not validate_commit(head or ""):
            self.on_log("正在查询默认分支提交 …")
            branch = self._fetch_json(
                constants.GITHUB_API_REPO + "/commits/" + default_branch)
            head = str(branch.get("sha", ""))
        if not validate_commit(head):
            raise UpdateError("未能确认上游 Git 提交号。", "请稍后重试。")

        meta = self.status()
        if meta["current_commit"] == head:
            return {"changed": False, "commit": head,
                    "message": "已是最新脚本版本。"}

        # 下载到 staging（缓存临时目录）；任何失败都清理 staging 并保持 current
        staging_root = os.path.join(self.scripts_root, constants.DIR_STAGING)
        try:
            if os.path.isdir(staging_root):
                shutil.rmtree(staging_root, ignore_errors=True)
            os.makedirs(staging_root, exist_ok=True)
            self.on_log(f"下载上游提交 {head[:12]} …")
            data = self._fetch_bytes(constants.GITHUB_CODELOAD_PREFIX + head)
            self.on_log("解压与校验 …")
            extracted = extract_tarball(data, staging_root, head)
            self.apply_compat_patches_to(extracted)
            check = self.validate_snapshot(extracted)
            if not check["ok"]:
                raise UpdateError(
                    "上游快照未通过校验：" + "；".join(check["errors"][:5]),
                    "已保留当前可用版本，未做任何替换。")
            self._write_meta(extracted, head,
                             time.strftime("%Y-%m-%d %H:%M:%S"))
            self._write_deps_bundled(extracted)
            self._swap(extracted)
            self.on_log(f"同步完成：当前版本 {head[:12]}。")
            return {"changed": True, "commit": head,
                    "message": f"已更新到 {head[:12]}。"}
        finally:
            if os.path.isdir(staging_root):
                shutil.rmtree(staging_root, ignore_errors=True)

    def _swap(self, staged: str) -> None:
        """原子切换：current -> previous（丢弃旧 previous），staged -> current。"""
        os.makedirs(self.scripts_root, exist_ok=True)
        if os.path.isdir(self.previous_dir):
            shutil.rmtree(self.previous_dir, ignore_errors=True)
        if os.path.isdir(self.current_dir):
            os.replace(self.current_dir, self.previous_dir)
        os.replace(staged, self.current_dir)

    def restore_previous(self) -> Dict[str, str]:
        """恢复上一版：previous -> current；原 current 保留为新 previous。"""
        if not os.path.isdir(self.previous_dir):
            raise UpdateError("没有可恢复的上一版本。",
                              "首次同步成功前无法回退。")
        if not self._lock.acquire(blocking=False):
            raise UpdateError("已有同步在进行中。", "请稍后重试。")
        try:
            tmp = self.current_dir + ".tmp-restore"
            if os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            # 1) 旧 current -> tmp   2) previous -> current   3) tmp -> previous
            os.replace(self.current_dir, tmp)
            try:
                os.replace(self.previous_dir, self.current_dir)
                os.replace(tmp, self.previous_dir)
            except OSError:
                # 任一步失败：回滚 tmp 回 current，尽量保持原状
                if not os.path.isdir(self.current_dir) and os.path.isdir(tmp):
                    os.replace(tmp, self.current_dir)
                raise
        finally:
            self._lock.release()
        # 回退得到的版本同样标记完整运行时已捆绑（依赖在桌面发行，与脚本来源无关）
        self._write_deps_bundled(self.current_dir)
        self.on_log("已恢复上一脚本版本。")
        return {"message": "已恢复上一脚本版本。"}
