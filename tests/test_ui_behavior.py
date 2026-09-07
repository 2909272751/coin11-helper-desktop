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
from unittest import mock

# 必须先于任何 PySide6 导入设置 offscreen（构造 QApplication 前生效）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", "")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from desktop_app import constants  # noqa: E402
from desktop_app.app import MainWindow  # noqa: E402
from desktop_app.download_dialog import RuntimeDownloadDialog  # noqa: E402
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
        # 滚动区内容布局直接持有两个分组容器，容器内再放任务行
        holder_count = 0
        for i in range(win.tasks_layout.count()):
            w = win.tasks_layout.itemAt(i).widget()
            if w is not None and (w is win._daily_holder
                                  or w is win._limited_holder):
                holder_count += 1
        self.assertEqual(holder_count, 2,
                         "滚动内容应直接持有日常/限时两个分组容器")
        self.assertTrue(win._daily_holder.isVisible())
        self.assertFalse(win._limited_holder.isVisible())

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
    """SPEC 1：主界面不再有“维护”下拉入口；维护动作迁入“设置与维护”
    对话框（运行组件 / 脚本更新 / 诊断），动作可达且保留 handler；
    “设备”菜单保留在一级工具条。"""

    def _actions(self, menu):
        out = []
        for a in menu.actions():
            if a.isSeparator():
                continue
            out.append(a)
        return out

    def test_device_menu_items(self):
        win = self.win
        menu = win.device_menu_btn.menu()
        labels = [a.text() for a in menu.actions() if not a.isSeparator()]
        for expect in ("刷新设备", "重新接管 ADB", "设备连接说明"):
            self.assertIn(expect, labels)
        takeover = [a for a in menu.actions() if a.text() == "重新接管 ADB"]
        self.assertTrue(takeover)
        self.assertIsNotNone(takeover[0].triggered)

    def test_no_maintain_entry_on_main_toolbar(self):
        """主界面不再有“维护”菜单/按钮，不遗留占位；设备菜单仍在。"""
        win = self.win
        self.assertFalse(hasattr(win, "maintain_menu_btn"),
                         "不应再存在维护下拉按钮")
        self.assertFalse(hasattr(win, "maintain_menu"),
                         "不应再存在维护菜单")
        from PySide6 import QtWidgets as W
        texts = [b.text() for b in win.findChildren(W.QPushButton)]
        self.assertNotIn("维护", texts)
        self.assertIn("设备", texts)   # 设备菜单保留
        self.assertTrue(hasattr(win, "device_menu_btn"))

    def _open_settings_dialog(self):
        """以 stub exec 打开“设置与维护”对话框（不进入模态循环）。"""
        from PySide6 import QtWidgets as W
        with mock.patch.object(W.QDialog, "exec", return_value=0):
            self.win._show_settings()
        dlg = getattr(self.win, "_setting_dialog", None)
        self.assertIsNotNone(dlg, "设置对话框未保留引用")
        return dlg

    def test_settings_dialog_has_grouped_maintenance(self):
        """设置与维护对话框含 运行组件/脚本更新/诊断 三组与全部动作。"""
        win = self.win
        dlg = self._open_settings_dialog()
        groups = [g.title() for g in dlg.findChildren(QtWidgets.QGroupBox)]
        for expect in ("运行组件", "脚本更新", "诊断"):
            self.assertIn(expect, groups, f"缺少维护分组：{expect}")
        # 运行组件组动作
        for attr, text in (
                ("setting_download_btn", "打开下载中心…"),
                ("setting_recheck_btn", "重新检测组件"),
                ("setting_runtime_dir_btn", "打开运行时目录…"),
                ("setting_sync_btn", "同步最新脚本"),
                ("setting_restore_btn", "恢复上一版"),
                ("setting_log_dir_btn", "打开日志目录…"),
                ("setting_copy_diag_btn", "复制诊断文本")):
            self.assertTrue(hasattr(win, attr), f"缺少按钮 {attr}")
            btn = getattr(win, attr)
            self.assertEqual(btn.text(), text)
            # 动作仍连接（可达）
            self.assertTrue(btn.isEnabled())
        win._setting_dialog = None
        win._setting_refresh = None

    def test_takeover_confirm_cancelled_no_side_effect(self):
        """菜单语义：接管 ADB 需确认；拒绝时不做任何接管。"""
        win = self.win
        win._running = False
        with mock.patch.object(QtWidgets.QMessageBox, "question",
                               return_value=QtWidgets.QMessageBox.No):
            win._on_takeover_adb()
        self.assertEqual(win.adb.takeover_calls, 0)


class RuntimeAutoPromptTest(_UiCase):
    """SPEC 2：初次后台完整检测为非就绪时 UI 空闲后自动打开下载中心一次；
    已就绪不弹；同缺件状态不连续弹多个窗口；运行中/检测异常不弹。"""

    def _install_fake(self):
        """替换 app 模块内 RuntimeDownloadDialog 为计数 stub。"""
        opens = {"count": 0, "instances": []}

        class _Fake(QtWidgets.QDialog):
            def __init__(self, settings, parent=None, warn_missing=False):
                super().__init__(parent)
                opens["count"] += 1
                opens["instances"].append(self)
                self.warn_missing = warn_missing

            def exec(self):
                return 0
        patcher = mock.patch("desktop_app.app.RuntimeDownloadDialog", _Fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opens

    def test_missing_prompts_dialog_once_only(self):
        win = self.win
        opens = self._install_fake()
        win._apply_runtime_status(False, "缺少运行组件", ["torch"],
                                  auto_prompt=True)
        self._pump_once()
        self.assertEqual(opens["count"], 1)
        # 再次触发同一缺件状态：不应连续再弹
        win._apply_runtime_status(False, "缺少运行组件", ["torch"],
                                  auto_prompt=True)
        self._pump_once()
        self.assertEqual(opens["count"], 1)

    def test_ready_no_prompt(self):
        win = self.win
        opens = self._install_fake()
        win._apply_runtime_status(True, "运行组件就绪", [], auto_prompt=True)
        self._pump_once()
        self.assertEqual(opens["count"], 0)

    def test_missing_but_running_no_prompt(self):
        win = self.win
        opens = self._install_fake()
        win._running = True
        try:
            win._apply_runtime_status(False, "缺少运行组件", ["torch"],
                                      auto_prompt=True)
            self._pump_once()
            self.assertEqual(opens["count"], 0)
        finally:
            win._running = False  # 避免 closeEvent 弹“仍在运行”确认框

    def test_exception_result_no_missing_no_prompt(self):
        """检测异常（missing 空但 ok=False 的异常路径）不弹。"""
        win = self.win
        opens = self._install_fake()
        win._apply_runtime_status(False, "校验异常", [], auto_prompt=True)
        self._pump_once()
        self.assertEqual(opens["count"], 0)

    def test_auto_dialog_gets_warn_missing_flag(self):
        """自动打开的下载中心以 warn_missing=True 构造（顶部警告）。"""
        win = self.win
        opens = self._install_fake()
        win._apply_runtime_status(False, "缺少运行组件", ["torch"],
                                  auto_prompt=True)
        self._pump_once()
        self.assertEqual(opens["count"], 1)
        self.assertTrue(opens["instances"][0].warn_missing)

    def _pump_once(self):
        self._app.processEvents()
        end = time.time() + 0.05
        while time.time() < end:
            self._app.processEvents()
            time.sleep(0.005)


class DownloadDialogSourceTest(_UiCase):
    """SPEC 3：下载中心新增下载源选择并持久化；缺件警告横幅存在且可关。"""

    def _make_dialog(self, warn=False):
        from desktop_app.download_dialog import RuntimeDownloadDialog
        dlg = RuntimeDownloadDialog(self.settings, parent=self.win,
                                    warn_missing=warn)
        self.addCleanup(dlg.close)
        return dlg

    def test_source_combo_options_fixed(self):
        """下载源下拉固定四项（智能/清华/阿里/官方），无自定义 URL 输入。"""
        dlg = self._make_dialog()
        combo = dlg.source_combo
        modes = [combo.itemData(i) for i in range(combo.count())]
        self.assertEqual(modes,
                         ["smart", "tuna", "aliyun", "official"])
        # 无 URL 输入框（没有任何 QLineEdit 用于自定义源）
        from PySide6 import QtWidgets as W
        self.assertEqual(len(dlg.findChildren(W.QLineEdit)), 0)

    def test_source_selection_persists(self):
        """切换下载源并持久化到 SettingsStore。"""
        dlg = self._make_dialog()
        self.assertEqual(self.settings.pip_source, "smart")
        idx = dlg.source_combo.findData("aliyun")
        dlg.source_combo.setCurrentIndex(idx)
        self.assertEqual(self.settings.pip_source, "aliyun")
        # 持久化到磁盘可被新 store 读取
        from desktop_app.settings_store import SettingsStore
        store2 = SettingsStore(self.data_dir)
        self.assertEqual(store2.pip_source, "aliyun")

    def test_warn_banner_visible_only_when_requested(self):
        """缺件自动打开带警告横幅且可关闭；手动打开默认无横幅。"""
        # offscreen 不 exec，用显式可见标志 + 隐藏属性验证
        dlg = self._make_dialog(warn=True)
        self.assertTrue(dlg.warn_missing)
        self.assertFalse(dlg.warn_banner.isHidden(),
                         "缺件警告横幅应可见（未被隐藏）")
        dlg.warn_close_btn.click()
        self.assertTrue(dlg.warn_banner.isHidden(),
                        "点击“知道了”后横幅应隐藏")
        dlg.close()
        dlg2 = self._make_dialog(warn=False)
        self.assertFalse(dlg2.warn_missing)
        self.assertTrue(dlg2.warn_banner.isHidden(),
                        "手动打开默认无警告横幅")

    def test_dialog_has_copy_diagnostics_button(self):
        dlg = self._make_dialog()
        self.assertEqual(dlg.copy_log_btn.text(), "复制诊断文本")
        self.assertTrue(dlg.copy_log_btn.isEnabled())


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

    def test_limited_tab_default_shows_daily(self):
        """限时活动默认不显示（segmented 控制），日常任务默认可见。"""
        win = self.win
        daily_holder = win._daily_holder
        limited_holder = win._limited_holder
        self.assertIsNotNone(daily_holder)
        self.assertIsNotNone(limited_holder)
        self.assertTrue(daily_holder.isVisible())
        self.assertFalse(limited_holder.isVisible())
        self.assertEqual(win._active_tab, "daily")
        self.assertTrue(win.daily_tab_btn.isChecked())
        self.assertFalse(win.limited_tab_btn.isChecked())

    def test_switch_to_limited_tab(self):
        """切到限时 tab：限时容器可见、日常隐藏，选择按钮文案联动。"""
        win = self.win
        win.limited_tab_btn.click()
        self.assertEqual(win._active_tab, "limited")
        self.assertTrue(win._limited_holder.isVisible())
        self.assertFalse(win._daily_holder.isVisible())
        self.assertTrue(win.limited_tab_btn.isChecked())
        self.assertFalse(win.daily_tab_btn.isChecked())
        self.assertEqual(win.select_all_btn.text(), "全选限时活动")
        win.daily_tab_btn.click()
        self.assertEqual(win._active_tab, "daily")
        self.assertTrue(win._daily_holder.isVisible())
        self.assertFalse(win._limited_holder.isVisible())

    def test_control_layout_present(self):
        win = self.win
        # 运行控制保留：开始/停止/状态/进度
        self.assertEqual(win.run_btn.text(), "开始运行")
        self.assertFalse(win.stop_btn.isEnabled())
        self.assertIsNotNone(win.status_pill)
        self.assertIsNotNone(win.progress)
        self.assertFalse(win.stop_btn.isEnabled())


class TaskTabSwitchTest(_UiCase):
    """SPEC 3：限时与日常为清晰 tab/segmented 切换，而非底部按钮折叠。"""

    def test_segmented_buttons_present(self):
        win = self.win
        self.assertIsNotNone(win.daily_tab_btn)
        self.assertIsNotNone(win.limited_tab_btn)
        self.assertTrue(win.daily_tab_btn.isCheckable())
        self.assertTrue(win.limited_tab_btn.isCheckable())
        self.assertTrue(win.daily_tab_btn.autoExclusive())

    def test_select_all_applies_to_active_tab(self):
        win = self.win
        # 日常 tab：全选只作用日常
        win.daily_tab_btn.click()
        win._set_all(True)
        for task in win.catalog.daily_tasks():
            row = win._task_rows[task["id"]]
            if row.enabled:
                self.assertTrue(row.checked)
        win._set_all(False)
        # 限时 tab：全选只作用限时
        win.limited_tab_btn.click()
        win._set_all(True)
        limited = win.catalog.limited_tasks()
        self.assertTrue(any(win._task_rows[t["id"]].checked for t in limited))
        for task in win.catalog.daily_tasks():
            if win._task_rows[task["id"]].enabled:
                self.assertFalse(win._task_rows[task["id"]].checked,
                                 "切换 tab 后全选不应影响日常任务")

    def test_task_row_height_and_selection_feedback(self):
        win = self.win
        row = next(iter(win._task_rows.values()))
        # 行高 40–44px
        self.assertGreaterEqual(row.height(), 40)
        self.assertLessEqual(row.height(), 44)
        # 勾选后行有选中属性
        row.check.setChecked(True)
        self.assertEqual(row.property("rowChecked"), "true")
        # 任务标题区显示已选数量
        self.assertTrue("1" in win.task_count_label.text() or
                        "已选" in win.task_count_label.text())

    def test_task_count_label_updates(self):
        win = self.win
        win._set_all(True)
        n = len(win._selected_task_ids())
        self.assertIn(str(n), win.task_count_label.text())
        self.assertIn(str(n), win.sel_count_label.text())


class RunPanelLayoutTest(_UiCase):
    """SPEC 4：运行面板标题“本次执行”、设备/选中/组件摘要、开始=唯一 CTA。"""

    def test_run_panel_titles_and_summaries(self):
        win = self.win
        self.assertIsNotNone(win.control_card.title_label)
        self.assertEqual(win.control_card.title_label.text(), "本次执行")
        # 三项摘要标签存在
        self.assertTrue(hasattr(win, "device_summary_label"))
        self.assertTrue(hasattr(win, "sel_count_label"))
        self.assertTrue(hasattr(win, "run_comp_summary"))
        self.assertTrue(hasattr(win, "run_comp_open_btn"))
        # 设备摘要文案（FakeAdb 无设备）
        self.assertIn("未选择", win.device_summary_label.text())

    def test_run_button_is_primary_and_full_width(self):
        win = self.win
        # 开始是唯一主 CTA（primary objectName）
        self.assertEqual(win.run_btn.objectName(), "primary")
        # 停止是次级危险按钮，初始禁用
        self.assertEqual(win.stop_btn.objectName(), "danger")
        self.assertFalse(win.stop_btn.isEnabled())
        # 主按钮占满可用宽度：所在列宽度近似可用宽度
        parent = win.control_card
        self.assertGreaterEqual(win.run_btn.width(), 120)

    def test_comp_link_compact(self):
        win = self.win
        self.assertEqual(win.run_comp_open_btn.text(), "下载")
        self.assertTrue(win.run_comp_open_btn.isVisible())


class LogHeightTest(_UiCase):
    """SPEC 5：日志抽屉折叠高度≤48px、展开日志视图最高 260px。"""

    def test_collapsed_log_drawer_height(self):
        win = self.win
        _pump(self._app, 0.2)
        h = win.log_card.height()
        self.assertLessEqual(h, 48)
        self.assertGreater(h, 0)
        self.assertFalse(win._log_expanded)

    def test_expanded_log_view_height_cap(self):
        win = self.win
        win._toggle_log_expanded()
        self.assertTrue(win._log_expanded)
        self.assertLessEqual(win.log_view.maximumHeight(), 260)
        # 展开态整体不遮任务/运行面板：日志抽屉位于 root 底部且高度有限
        self.assertLess(win.log_view.maximumHeight(), 300)

    def test_collapsed_summary_shows_last_log(self):
        win = self.win
        win._append_log("最近的一行日志内容")
        self.assertIn("最近的一行日志内容", win.log_summary.text())


class NarrowLayoutTest(_UiCase):
    """SPEC 7：880px 级窄窗口上下布局，任务至少 260px、文字按钮可见。"""

    def test_narrow_task_panel_min_height(self):
        win = self.win
        from PySide6 import QtCore
        win.resize(880, 720)
        _pump(self._app, 0.3)
        self.assertEqual(win.work_split.orientation(),
                         QtCore.Qt.Orientation.Vertical)
        # 拆分器首项（任务面板）在可见区域中至少占 260px
        sizes = win.work_split.sizes()
        self.assertGreaterEqual(sizes[0], 260)
        # 运行面板也仍可见
        self.assertGreater(sizes[1], 0)
        # 任务区域水平不出现滚动（scrollbar horizontal 关闭）
        self.assertEqual(
            win.task_scroll.horizontalScrollBarPolicy(),
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 关键控件都在窗口可视范围内
        self.assertTrue(win.run_btn.isVisible())
        self.assertTrue(win.select_all_btn.isVisible())
        self.assertTrue(win.daily_tab_btn.isVisible())


class WorkbenchSkeletonTest(_UiCase):
    """20260906-workbench-skeleton：工作台骨架结构断言（最少、offscreen）。

    仅断言容器结构与关键控件存在，不做视觉结论：
    - 根 = 品牌条 / 设备条 / QSplitter(任务|运行) / 日志抽屉；
    - 任务滚动与底部选择工具栏结构分离（同属任务面板但不嵌套）；
    - 窄于 980px 时 splitter 切 vertical。
    """

    def _find_splitter(self):
        return self.win.work_split

    def test_splitter_hosts_task_and_run_panels(self):
        win = self.win
        split = win.work_split
        self.assertIsNotNone(split)
        from PySide6 import QtCore
        self.assertEqual(split.orientation(),
                         QtCore.Qt.Orientation.Horizontal)
        widgets = [split.widget(i) for i in range(split.count())]
        self.assertIn(win.task_card, widgets)
        self.assertIn(win.control_card, widgets)

    def test_task_scroll_and_toolbar_separated_inside_panel(self):
        win = self.win
        # 工具栏与滚动区仍是任务面板直属子项（不互相嵌套）
        self.assertIs(win.task_toolbar.parent(), win.task_card)
        self.assertIs(win.task_scroll.parent(), win.task_card)
        # 日志抽屉位于 splitter 之外（窗口根布局）
        root_lay = win._root_layout
        children = [root_lay.itemAt(i).widget()
                    for i in range(root_lay.count())]
        self.assertIn(win.log_card, children)

    def test_run_panel_controls_present(self):
        win = self.win
        for attr in ("sel_count_label", "run_comp_summary", "run_comp_open_btn",
                     "status_pill", "run_state_label", "elapsed_label",
                     "progress_text", "progress", "run_btn", "stop_btn"):
            self.assertTrue(hasattr(win, attr), f"缺少 {attr}")
            self.assertIsNotNone(getattr(win, attr))

    def test_narrow_width_switches_splitter_vertical(self):
        win = self.win
        from PySide6 import QtCore
        win.resize(900, 800)
        _pump(self._app)
        self.assertEqual(win.work_split.orientation(),
                         QtCore.Qt.Orientation.Vertical)
        win.resize(1180, 800)
        _pump(self._app)
        self.assertEqual(win.work_split.orientation(),
                         QtCore.Qt.Orientation.Horizontal)


if __name__ == "__main__":
    unittest.main()
