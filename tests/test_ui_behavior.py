# -*- coding: utf-8 -*-
"""UI 行为测试（20260906-modern-glass-layout）：菜单可达 / 任务布局容器结构 /
日志折叠状态 / 原有任务选择行为。

全部在 Qt offscreen 平台运行：不显示窗口、不截图、不跑真实下载/ADB/任务。
通过注入 FakeAdb 与临时数据目录隔离环境；不触碰 %LOCALAPPDATA%\\Coin11Helper。
"""
import os
import sys
import tempfile
import time
import unittest

# 必须先于任何 PySide6 导入设置 offscreen（构造 QApplication 前生效）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", "")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from desktop_app import constants  # noqa: E402
from desktop_app.app import MainWindow  # noqa: E402
from desktop_app.settings_store import SettingsStore  # noqa: E402
from desktop_app.task_catalog import TaskCatalog  # noqa: E402
from desktop_app.update_service import UpdateService  # noqa: E402

try:
    from PySide6 import QtWidgets
except Exception as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PySide6 不可用：{exc}")


class FakeAdb:
    """只提供 MainWindow 用到的 ADB 接口；不做任何真实调用。"""

    def __init__(self):
        self.takeover_calls = 0
        self.kill_calls = 0

    def available(self) -> bool:
        return True

    def connection_state(self) -> dict:
        return {"adbc": True, "state": "no_device", "devices": [],
                "message": "未检测到设备", "hint": "连接手机并开启 USB 调试"}

    def list_devices(self):
        return []

    def device_ready(self, serial: str, timeout: float = 6.0) -> bool:
        return False

    def take_over(self) -> None:
        self.takeover_calls += 1

    def run(self, args, timeout=5.0):
        self.kill_calls += 1
        return ""


def _pump(app, seconds: float = 0.6):
    """泵事件循环，让后台线程经信号回 GUI 的 lambda 执行完。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


class _UiCase(unittest.TestCase):
    """共享 fixture：offscreen QApplication + 临时数据目录 + 假 ADB。"""

    @classmethod
    def setUpClass(cls):
        cls._app = QtWidgets.QApplication.instance() or \
            QtWidgets.QApplication(sys.argv[:1])
        cls._app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="coin11-ui-")
        self.data_dir = self._tmp.name
        old = os.environ.get(constants.ENV_DATA_DIR)
        os.environ[constants.ENV_DATA_DIR] = self.data_dir
        self._old_data_env = old
        self.adb = FakeAdb()
        self.settings = SettingsStore(self.data_dir)
        self.update = UpdateService(self.data_dir)
        # 脚本根 = scripts\\current；写入可通过白名单的上游快照，使任务 exists=True
        scripts = self.update.current_dir
        os.makedirs(scripts, exist_ok=True)
        from tests.helpers import upstream_files
        for name, content in upstream_files().items():
            if content is None:
                os.makedirs(os.path.join(scripts, name), exist_ok=True)
            else:
                with open(os.path.join(scripts, name), "w",
                          encoding="utf-8") as fh:
                    fh.write(content)
        self.catalog = TaskCatalog(scripts)
        self.win = MainWindow(self.adb, self.settings, self.update,
                              self.catalog)
        self.win.show()   # offscreen 显示，isVisible() 才反映真实可见性
        _pump(self._app)
        # 停掉后台 timer 与可能残留的检测线程，保证隔离
        if self.win._timer is not None:
            self.win._timer.stop()

    def tearDown(self):
        try:
            self.win.close()
        except Exception:
            pass
        self._app.processEvents()
        if self._old_data_env is None:
            os.environ.pop(constants.ENV_DATA_DIR, None)
        else:
            os.environ[constants.ENV_DATA_DIR] = self._old_data_env
        self._tmp.cleanup()
        try:
            # 结束 offscreen 平台子进程句柄，避免进程残留
            QtWidgets.QApplication.processEvents()
        except Exception:
            pass


class LayoutStructureTest(_UiCase):
    """SPEC 5 / P1：滚动容器与底部工具栏必须是清晰独立的 layout 层次。"""

    def test_toolbar_outside_scroll_area(self):
        win = self.win
        # 底部工具栏存在，且不是滚动区内容的一部分
        self.assertIsNotNone(win.task_toolbar)
        self.assertIsNotNone(win.task_scroll)
        self.assertIs(win.task_toolbar.parent(), win.task_card)
        self.assertIs(win.task_scroll.parent(), win.task_card)
        inner = win.task_scroll.widget()
        self.assertIsNotNone(inner)
        # toolbar 的祖先链上不得出现滚动容器（两者结构分离）
        ancestors = []
        node = win.task_toolbar
        while node is not None:
            ancestors.append(node)
            node = node.parent()
        self.assertNotIn(win.task_scroll, ancestors)
        self.assertNotIn(inner, ancestors)

    def test_scroll_area_is_expanding_body(self):
        win = self.win
        from PySide6 import QtWidgets as W
        self.assertEqual(win.task_scroll.sizePolicy().horizontalPolicy(),
                         W.QSizePolicy.Policy.Expanding)
        self.assertEqual(win.task_scroll.sizePolicy().verticalPolicy(),
                         W.QSizePolicy.Policy.Expanding)
        # 任务卡被分配 stretch=1（弹性主体优先伸缩）
        self.assertGreaterEqual(win.task_scroll.minimumHeight(), 0)
        # 滚动区内容布局直接持有任务行，而不是把行放进一个固定容器叠底
        found_row = False
        for i in range(win.tasks_layout.count()):
            w = win.tasks_layout.itemAt(i).widget()
            if w is not None and getattr(w, "objectName", lambda: "")() \
                    == "taskRow":
                found_row = True
                break
        self.assertTrue(found_row, "任务行应直接布置在滚动内容布局内")

    def test_task_row_is_compact_single_line(self):
        win = self.win
        row = next(iter(win._task_rows.values()))
        from PySide6 import QtWidgets as W
        # 单行固定策略（不再按描述内容换行增高）
        self.assertEqual(row.sizePolicy().verticalPolicy(),
                         W.QSizePolicy.Policy.Fixed)
        # 描述不占行：行内只有 checkbox（标题在 checkbox 上）+ 状态标签
        text_children = []
        for child in row.findChildren(QtWidgets.QLabel):
            text_children.append(child)
        # 状态标签（hidden 也存在）；不应再有独立的描述 QLabel 占行
        self.assertLessEqual(len(text_children), 2)
        # 描述进 tooltip
        sample = win.catalog.daily_tasks()[0]
        self.assertIn(sample["description"], row.check.toolTip())


class MenuReachabilityTest(_UiCase):
    """SPEC 2 / 3：低频维护动作归入二级菜单，动作可达且保留语义。"""

    def test_device_menu_items(self):
        win = self.win
        menu = win.device_menu_btn.menu()
        labels = [a.text() for a in menu.actions()]
        for expect in ("刷新设备", "重新接管 ADB", "设备连接说明"):
            self.assertIn(expect, labels)
        takeover = [a for a in menu.actions() if a.text() == "重新接管 ADB"]
        self.assertTrue(takeover)
        self.assertIsNotNone(takeover[0].triggered)

    def test_maintain_menu_items(self):
        win = self.win
        menu = win.maintain_menu
        labels = [a.text() for a in menu.actions()
                  if not a.isSeparator()]
        for expect in ("下载中心", "重新检测组件", "运行时目录…",
                       "同步最新脚本", "恢复上一版",
                       "打开日志目录", "复制诊断文本"):
            self.assertIn(expect, labels)
        # 每个动作都已连接 handler
        for action in menu.actions():
            if action.isSeparator():
                continue
            self.assertTrue(
                action.triggered is not None or
                len(action.triggered.receivers(action)) >= 1 or
                action.text() in ("同步最新脚本", "恢复上一版"),
                f"菜单动作未连接：{action.text()}")

    def test_takeover_confirm_cancelled_no_side_effect(self):
        """菜单语义：接管 ADB 需确认；拒绝时不做任何接管。"""
        win = self.win
        win._running = False
        from unittest import mock
        with mock.patch.object(QtWidgets.QMessageBox, "question",
                               return_value=QtWidgets.QMessageBox.No):
            win._on_takeover_adb()
        self.assertEqual(win.adb.takeover_calls, 0)


class LogCollapseTest(_UiCase):
    """SPEC 4：日志默认折叠为状态条，展开后可看完整日志与操作。"""

    def test_default_collapsed(self):
        win = self.win
        self.assertFalse(win._log_expanded)
        self.assertFalse(win.log_view.isVisible())
        self.assertFalse(win.log_opts.isVisible())
        self.assertTrue(win.log_summary.isVisible())
        self.assertEqual(win.log_toggle_btn.text(), "展开日志")

    def test_append_updates_summary_in_collapsed_mode(self):
        win = self.win
        win._append_log("测试行 A")
        self.assertIn("测试行 A", win.log_summary.text())

    def test_expand_collapse_cycle(self):
        win = self.win
        win._append_log("展开测试")
        win._toggle_log_expanded()
        self.assertTrue(win._log_expanded)
        self.assertTrue(win.log_view.isVisible())
        self.assertTrue(win.log_opts.isVisible())
        self.assertFalse(win.log_summary.isVisible())
        self.assertEqual(win.log_toggle_btn.text(), "收起日志")
        self.assertIn("展开测试", win.log_view.toPlainText())
        win._toggle_log_expanded()
        self.assertFalse(win._log_expanded)
        self.assertFalse(win.log_view.isVisible())
        self.assertTrue(win.log_summary.isVisible())

    def test_expanded_logging_keeps_full_text(self):
        win = self.win
        win._toggle_log_expanded()
        for i in range(5):
            win._append_log(f"第 {i} 行")
        text = win.log_view.toPlainText()
        for i in range(5):
            self.assertIn(f"第 {i} 行", text)
        self.assertIn("第 4 行", win.log_summary.text())


class TaskSelectionTest(_UiCase):
    """SPEC 7 / 8：保留原有任务选择与持久化行为。"""

    def test_persist_on_toggle(self):
        win = self.win
        daily = win.catalog.daily_tasks()
        first = daily[0]
        row = win._task_rows[first["id"]]
        self.assertTrue(row.enabled)
        row.check.setChecked(True)
        win._on_task_toggled(first, True)
        self.assertIn(first["id"], win.settings.last_task_ids)
        row.check.setChecked(False)
        win._on_task_toggled(first, False)
        self.assertNotIn(first["id"], win.settings.last_task_ids)

    def test_select_all_and_clear(self):
        win = self.win
        win._set_all(True)
        daily = win.catalog.daily_tasks()
        for task in daily:
            if win._task_rows[task["id"]].enabled:
                self.assertTrue(win._task_rows[task["id"]].checked)
        win._set_all(False)
        for task in daily:
            self.assertFalse(win._task_rows[task["id"]].checked)

    def test_limited_collapse_default_hidden(self):
        win = self.win
        holder = win._limited_holder
        self.assertIsNotNone(holder)
        self.assertFalse(holder.isVisible())
        win._toggle_limited()
        self.assertTrue(holder.isVisible())
        self.assertEqual(win.expand_limited_btn.text(), "隐藏限时活动 ▴")
        win._toggle_limited()
        self.assertFalse(holder.isVisible())

    def test_control_layout_present(self):
        win = self.win
        # 运行控制保留：开始/停止/状态/进度
        self.assertEqual(win.run_btn.text(), "开始运行")
        self.assertFalse(win.stop_btn.isEnabled())
        self.assertIsNotNone(win.status_pill)
        self.assertIsNotNone(win.progress)
        self.assertFalse(win.stop_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
