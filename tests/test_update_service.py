# -*- coding: utf-8 -*-
"""更新服务单元测试：URL/commit/路径校验、tarball 校验、原子切换、回退、
语法与清单验证、兼容补丁、注入式网络替身（不访问真实网络）。"""
import gzip
import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # helpers

from desktop_app import compat_patch, constants  # noqa: E402
from desktop_app.update_service import (  # noqa: E402
    UpdateError,
    UpdateService,
    extract_tarball,
    http_get_bytes,
    http_get_json,
    validate_commit,
    validate_repo_url,
)
from tests.helpers import all_daily_scripts, upstream_files  # noqa: E402

SHA = "a" * 40
SHA2 = "b" * 40


def make_tarball(sha, files=None, extra_top=None):
    """构造与 GitHub codeload 同构的 tar.gz（单顶层目录 coin11-tb-<sha>/）。

    files: {相对路径: 内容字符串}；值为 None 表示创建目录。
    """
    if files is None:
        files = upstream_files()
    top = extra_top or f"coin11-tb-{sha}"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(top + "/")
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        for name, content in files.items():
            if content is None:
                t = tarfile.TarInfo(f"{top}/{name}/")
                t.type = tarfile.DIRTYPE
                tf.addfile(t)
                continue
            data = content.encode("utf-8")
            t = tarfile.TarInfo(f"{top}/{name}")
            t.size = len(data)
            tf.addfile(t, io.BytesIO(data))
    return buf.getvalue()


class ValidateTest(unittest.TestCase):
    def test_repo_url_must_be_exact(self):
        self.assertTrue(validate_repo_url(constants.UPSTREAM_REPO_URL))
        for bad in ("https://evil.com/x.git",
                    "https://github.com/czl0325/coin11-tb.git@evil",
                    "https://github.com/czl0325/other.git",
                    "file:///etc/passwd", "", None):
            self.assertFalse(validate_repo_url(bad))

    def test_commit_sha(self):
        self.assertTrue(validate_commit(SHA))
        self.assertTrue(validate_commit("0123456789abcdef0123456789abcdef01234567"))
        for bad in ("short", "zz" * 20, "", None, SHA + "ff"):
            self.assertFalse(validate_commit(bad))

    def test_http_json_rejects_foreign_url(self):
        with self.assertRaises(UpdateError):
            http_get_json("https://api.github.com/repos/evil/repo")

    def test_http_bytes_rejects_foreign_prefix_and_bad_sha(self):
        with self.assertRaises(UpdateError):
            http_get_bytes("https://codeload.github.com/evil/repo/tar.gz/" + SHA)
        with self.assertRaises(UpdateError):
            http_get_bytes("https://codeload.github.com/czl0325/coin11-tb/tar.gz/not-a-sha")


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_single_top_level(self):
        data = make_tarball(SHA)
        out = extract_tarball(data, self.dir, SHA)
        self.assertTrue(os.path.isdir(out))
        self.assertTrue(os.path.isfile(os.path.join(out, "utils.py")))

    def test_rejects_wrong_toplevel_name(self):
        data = make_tarball(SHA, extra_top="coin11-tb-ffffffffffffffffffffffffffffffffffffffff")
        with self.assertRaises(UpdateError):
            extract_tarball(data, self.dir, SHA)

    def test_rejects_bad_gzip(self):
        with self.assertRaises(UpdateError):
            extract_tarball(b"not gzip at all", self.dir, SHA)


class UpdateServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.logs = []
        self.service = UpdateService(self.base, on_log=self.logs.append)
        self.seed = os.path.join(self.base, "scripts", "current")

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_current(self, sha=SHA, content_extra=""):
        os.makedirs(self.seed, exist_ok=True)
        files = upstream_files(overrides={
            "淘宝成就中心签到.py": "import time\n" + content_extra})
        for name, content in files.items():
            p = os.path.join(self.seed, name)
            if content is None:
                os.makedirs(p, exist_ok=True)
            else:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(content)
        self.service._write_meta(self.seed, sha, "2026-01-01 00:00:00")

    def test_status_empty(self):
        status = self.service.status()
        self.assertEqual(status["current_commit"], "")
        self.assertEqual(status["previous_commit"], "")

    def test_sync_new_version_atomic_swap_and_previous(self):
        self._seed_current(SHA)
        new_files = upstream_files(overrides={
            "淘宝成就中心签到.py": "print('v2')\n"})
        data = make_tarball(SHA2, files=new_files)

        def fake_json(url):
            return {"default_branch": "main",
                    "sha": SHA2}

        def fake_bytes(url):
            self.assertTrue(url == constants.GITHUB_CODELOAD_PREFIX + SHA2)
            return data

        svc = UpdateService(self.base, on_log=self.logs.append,
                            fetch_json=fake_json, fetch_bytes=fake_bytes)
        result = svc.sync()
        self.assertTrue(result["changed"])
        self.assertEqual(result["commit"], SHA2)
        # 当前 = SHA2，上一版 = SHA（保留可回退）
        status = svc.status()
        self.assertEqual(status["current_commit"], SHA2)
        self.assertEqual(status["previous_commit"], SHA)
        # current 里已打补丁（env 兼容）
        with open(os.path.join(self.service.current_dir, "utils.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("COIN11_DEVICE_SERIAL", text)

    def test_sync_failure_keeps_current(self):
        self._seed_current(SHA)

        def fake_json(url):
            return {"sha": SHA2}

        def fake_bytes(url):
            raise UpdateError("模拟下载失败", "请重试")

        svc = UpdateService(self.base, on_log=self.logs.append,
                            fetch_json=fake_json, fetch_bytes=fake_bytes)
        with self.assertRaises(UpdateError):
            svc.sync()
        status = svc.status()
        self.assertEqual(status["current_commit"], SHA)
        self.assertEqual(status["previous_commit"], "")
        # 没有留下 staging
        self.assertFalse(os.path.isdir(
            os.path.join(self.base, "scripts", ".staging")))

    def test_sync_invalid_snapshot_keeps_current(self):
        self._seed_current(SHA)
        # 缺 LICENSE -> 不 ok
        bad_files = upstream_files(remove=["LICENSE"])
        data = make_tarball(SHA2, files=bad_files)

        def fake_json(url):
            return {"sha": SHA2}

        def fake_bytes(url):
            return data

        svc = UpdateService(self.base, on_log=self.logs.append,
                            fetch_json=fake_json, fetch_bytes=fake_bytes)
        with self.assertRaises(UpdateError):
            svc.sync()
        self.assertEqual(svc.status()["current_commit"], SHA)

    def test_sync_missing_limited_script_rejected(self):
        """同步快照缺限时活动脚本 -> 校验不通过（不能静默丢掉界面列出的任务）。"""
        self._seed_current(SHA)
        bad_files = upstream_files(remove=["2025淘宝双11.py"])
        data = make_tarball(SHA2, files=bad_files)

        def fake_json(url):
            return {"sha": SHA2}

        svc = UpdateService(self.base, on_log=self.logs.append,
                            fetch_json=fake_json,
                            fetch_bytes=lambda u: data)
        with self.assertRaises(UpdateError) as ctx:
            svc.sync()
        self.assertIn("2025淘宝双11.py", str(ctx.exception))
        self.assertEqual(svc.status()["current_commit"], SHA)

    def test_syntax_error_detected(self):
        self._seed_current(SHA)
        data = make_tarball(SHA2, files=upstream_files(overrides={
            "淘宝成就中心签到.py": "def broken(:\n"}))

        def fake_json(url):
            return {"sha": SHA2}

        svc = UpdateService(self.base, fetch_json=fake_json,
                            fetch_bytes=lambda u: data)
        with self.assertRaises(UpdateError) as ctx:
            svc.sync()
        self.assertIn("语法", str(ctx.exception))

    def test_restore_previous(self):
        self._seed_current(SHA)
        new_files = upstream_files(overrides={
            "淘宝成就中心签到.py": "print('v2')\n"})
        svc = UpdateService(
            self.base, on_log=self.logs.append,
            fetch_json=lambda u: {"sha": SHA2},
            fetch_bytes=lambda u: make_tarball(SHA2, files=new_files))
        svc.sync()
        svc.restore_previous()
        status = svc.status()
        # current 回到 SHA（原 previous），SHA2 进 previous
        self.assertEqual(status["current_commit"], SHA)
        self.assertEqual(status["previous_commit"], SHA2)

    def test_restore_without_previous_raises(self):
        self._seed_current(SHA)
        with self.assertRaises(UpdateError):
            self.service.restore_previous()

    def test_compat_patch_utils_select_device(self):
        text = ("# comment 1\n"
                "def select_device():\n"
                "    # 获取所有连接的设备\n"
                "    devices = get_connected_devices()\n"
                "    return devices\n")
        patched = compat_patch.patch_utils_select_device(text)
        self.assertIn("COIN11_DEVICE_SERIAL", patched)
        self.assertIn("devices = get_connected_devices()", patched)  # 原行保留
        self.assertIn("def select_device():", patched)
        # 幂等
        self.assertEqual(patched, compat_patch.patch_utils_select_device(patched))

    def test_compat_patch_u2_connect(self):
        text = "import uiautomator2 as u2\nd = u2.connect()\n"
        patched = compat_patch.patch_u2_connect(text)
        self.assertIn("COIN11_DEVICE_SERIAL", patched)
        self.assertIn("d = u2.connect(_coin11_os.environ", patched)
        self.assertIn("d = u2.connect()", patched)  # 分支里保留默认行为
        self.assertEqual(patched, compat_patch.patch_u2_connect(patched))


if __name__ == "__main__":
    unittest.main()
