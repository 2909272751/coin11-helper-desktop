# -*- coding: utf-8 -*-
"""设置持久化与日志脱敏单元测试。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from desktop_app.logutil import is_error_line, redact, should_hold_back  # noqa: E402
from desktop_app.settings_store import SettingsStore  # noqa: E402


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults(self):
        store = SettingsStore(self.dir)
        self.assertTrue(store.auto_takeover_adb)
        self.assertEqual(store.last_device_serial, "")
        self.assertEqual(store.last_task_ids, [])

    def test_roundtrip_and_atomic(self):
        store = SettingsStore(self.dir)
        store.set("last_device_serial", "R58M1234")
        store.set("last_task_ids", ["taobao_coin", "xianyu_dice"])
        store.set("auto_takeover_adb", False)
        store2 = SettingsStore(self.dir)
        self.assertEqual(store2.last_device_serial, "R58M1234")
        self.assertEqual(store2.last_task_ids, ["taobao_coin", "xianyu_dice"])
        self.assertFalse(store2.auto_takeover_adb)
        # 只写入一个 json 文件（原子替换不留 tmp）
        files = os.listdir(self.dir)
        self.assertIn("settings.json", files)
        self.assertFalse(any(f.endswith(".tmp") for f in files))

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(os.path.join(self.dir, "settings.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{broken json!!")
        store = SettingsStore(self.dir)
        self.assertEqual(store.last_device_serial, "")
        self.assertTrue(store.auto_takeover_adb)


class RedactTest(unittest.TestCase):
    def test_phone_numbers(self):
        out = redact("联系 13800138000 或 +8613900138000 完成")
        self.assertNotIn("13800138000", out)
        self.assertIn("<手机号>", out)

    def test_tokens_and_long_strings(self):
        out = redact("token='abcdefghijklmnop1234567890' ok")
        self.assertNotIn("abcdefghijklmnop1234567890", out)
        self.assertIn("<已脱敏>", out)
        # 纯 commit sha 保留（无害）
        sha = "a" * 40
        self.assertIn(sha, redact(f"commit {sha}"))

    def test_hold_back_words(self):
        self.assertTrue(should_hold_back("遇到验证码，请人工处理"))
        self.assertTrue(should_hold_back("需要登录后继续"))
        self.assertTrue(should_hold_back("支付未完成"))
        self.assertFalse(should_hold_back("任务循环第 3 次"))

    def test_error_line_filter(self):
        self.assertTrue(is_error_line("[错误] 无法打开"))
        self.assertTrue(is_error_line("Traceback (most recent call last)"))
        self.assertTrue(is_error_line("   File \"C:\\x\\y.py\", line 1"))
        self.assertTrue(is_error_line("RuntimeError: boom"))
        self.assertTrue(is_error_line("任务执行失败 退出码 3"))
        self.assertFalse(is_error_line("[完成] 淘宝签到：脚本进程正常结束"))
        self.assertFalse(is_error_line(""))
        self.assertFalse(is_error_line("普通输出"))


if __name__ == "__main__":
    unittest.main()
