# -*- coding: utf-8 -*-
"""任务清单分组（0.3.0）：日常任务默认展示、限时活动单列、排除工具/测试脚本。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # helpers

from desktop_app import constants  # noqa: E402
from desktop_app.task_catalog import TaskCatalog  # noqa: E402
from tests.helpers import all_supported_scripts, upstream_files  # noqa: E402

# 绝不纳入清单的文件（工具/测试/旧命令行批处理器）
_EXCLUDED = {"chromedriver.py", "识别图片测试.py", "淘宝多任务执行.py"}


class CatalogGroupsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        for name, content in upstream_files().items():
            if content is None:
                os.makedirs(os.path.join(self.root, name), exist_ok=True)
            else:
                with open(os.path.join(self.root, name), "w",
                          encoding="utf-8") as fh:
                    fh.write(content)

    def tearDown(self):
        self._tmp.cleanup()

    def test_daily_group_has_11_and_excludes_utilities(self):
        cat = TaskCatalog(self.root)
        daily = cat.daily_tasks()
        self.assertEqual(len(daily), len(constants.DEFAULT_TASKS))
        self.assertEqual(len(daily), 11)
        daily_scripts = {t["script"] for t in daily}
        self.assertFalse(daily_scripts & _EXCLUDED)

    def test_limited_group_is_4_and_marked(self):
        cat = TaskCatalog(self.root)
        limited = cat.limited_tasks()
        self.assertEqual(len(limited), len(constants.LIMITED_TASKS))
        self.assertEqual(len(limited), 4)
        for t in limited:
            self.assertTrue(t.get("limited"))
            self.assertEqual(t["group"], constants.GROUP_LIMITED)
            self.assertIn("限时", t.get("description", ""))
        self.assertTrue(all(t["exists"] for t in limited))

    def test_all_supported_scripts_covered(self):
        # 清单里的每个 script 都是仓库真实存在的受支持脚本文件名
        cat = TaskCatalog(self.root)
        listed = {t["script"] for t in cat.list_tasks()}
        self.assertEqual(listed, set(all_supported_scripts()))
        # 限时活动也必须存在
        for spec in constants.LIMITED_TASKS:
            self.assertTrue(os.path.isfile(
                os.path.join(self.root, spec["script"])))

    def test_validate_ids_allows_limited(self):
        cat = TaskCatalog(self.root)
        got = cat.validate_ids(["taobao_coin", "limited_2025_double11",
                                "evil"])
        self.assertEqual(got, ["taobao_coin", "limited_2025_double11"])


if __name__ == "__main__":
    unittest.main()
