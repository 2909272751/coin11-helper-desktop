"""Coin11 助手桌面版主窗口（PySide6）。

0.3.0 界面（现代浅色卡片布局）：
- 标题区：Coin11 logo + 应用名 + 连接状态胶囊；
- 设备 / 上游脚本 / 任务（日常 + 可折叠的限时活动）/ 运行控制 / 日志分区卡片；
- 任务区为可滚动卡片/分组，含描述与状态标签；按钮主次层级与禁用态；
- 日志区深色等宽字体，带“自动滚动”与“仅看错误”过滤。

运行模型：
- 全部系统调用（ADB / 任务子进程）在后台线程执行；设备断线看门狗在
  TaskRunner 内部线程中周期调用私有 ADB（adb 层有进程内锁，设备列表刷新
  与看门狗不会并发轰炸 adb），UI 只编排与呈现。
- 任务运行期间显示当前任务、实时日志、已用时、队列进度（i/n）与不确定
  进度动画；每个任务结束显示成功/失败/取消/跳过。
- 单实例：第二次启动激活已有窗口；关窗即退出应用并停止后台子进程。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import constants
from .adb_service import AdbError, AdbService, DeviceInfo
from .download_dialog import RuntimeDownloadDialog
from .logutil import is_error_line, redact
from .runtime import easyocr_model_dir, runtime_python_exe
from .runtime_manager import (
    ensure_runtime_ready,
    resolve_data_runtime_dir,
    runtime_data_python,
    verify_component,
)
from .runtime_components import (
    COMPONENT_PYTHON_BOOTSTRAP,
    INSTALL_ORDER,
    STATE_OK,
)
from .settings_store import SettingsStore
from .task_catalog import TaskCatalog
from .task_runner import RunState, TaskRunner
from .update_service import UpdateError, UpdateService

try:
    from PySide6.QtCore import QStandardPaths
except Exception:  # pragma: no cover
    QStandardPaths = None

APP_ID = "Coin11Helper.DesktopWrapper.0.3"

# --------------------------------------------------------------------- 主题

C_BG = "#f4f7f6"          # 窗口浅灰
C_CARD = "#ffffff"        # 卡片白
C_PRIMARY = "#0e7d74"     # 柔和深青绿（主色）
C_PRIMARY_HOVER = "#0a6a63"
C_PRIMARY_DISABLED = "#9cc7c3"
C_TEXT = "#22302e"
C_TEXT_SUB = "#5b6b68"
C_BORDER = "#e2e9e7"
C_RUNNING = "#0e7d74"
C_OK = "#1a7f37"
C_ERR = "#b42318"
C_CANCEL = "#9a6700"
C_SKIP = "#6b7280"

QSS = f"""
* {{ font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif; }}
QMainWindow, QWidget#root {{ background: {C_BG}; }}
QWidget {{ color: {C_TEXT}; font-size: 13px; }}
QToolTip {{ background: #2b2b2b; color: #f0f0f0; border: none; padding: 6px; }}

/* 卡片 */
QFrame#card {{
    background: {C_CARD}; border: 1px solid {C_BORDER};
    border-radius: 10px;
}}
QLabel#cardTitle {{ font-size: 15px; font-weight: 600; color: {C_TEXT}; }}
QLabel#cardHint {{ font-size: 12px; color: {C_TEXT_SUB}; }}
QLabel#sectionTitle {{ font-size: 13px; font-weight: 600; color: {C_TEXT}; }}

/* 品牌条 */
QFrame#brandBar {{ background: {C_PRIMARY}; border: none; border-radius: 10px; }}
QLabel#brandName {{ font-size: 20px; font-weight: 700; color: #ffffff; }}
QLabel#brandSub {{ font-size: 12px; color: #d6ece9; }}

/* 连接状态胶囊 */
QLabel#connPill {{
    background: rgba(255,255,255,0.18); color: #ffffff;
    border-radius: 12px; padding: 4px 12px; font-size: 12px;
}}
QLabel#connDot {{ color: #ffd666; font-size: 13px; }}
QLabel#connDotOk {{ color: #7cf0b3; font-size: 13px; }}

/* 设备/通用标签 */
QLabel#fieldLabel {{ color: {C_TEXT_SUB}; font-size: 12px; }}
QLabel#stateTagOk {{ color: {C_OK}; background: #e6f4ea; border-radius: 8px; padding: 2px 8px; font-size: 12px; }}
QLabel#stateTagErr {{ color: {C_ERR}; background: #fdecea; border-radius: 8px; padding: 2px 8px; font-size: 12px; }}
QLabel#stateTagCancel {{ color: {C_CANCEL}; background: #fdf3e0; border-radius: 8px; padding: 2px 8px; font-size: 12px; }}
QLabel#stateTagSkip {{ color: {C_SKIP}; background: #eef0f2; border-radius: 8px; padding: 2px 8px; font-size: 12px; }}
QLabel#stateTagIdle {{ color: {C_TEXT_SUB}; background: #f0f4f3; border-radius: 8px; padding: 2px 8px; font-size: 12px; }}
QLabel#stateTagRun {{ color: #ffffff; background: {C_PRIMARY}; border-radius: 8px; padding: 2px 8px; font-size: 12px; }}
QLabel#stateTagReady {{ color: {C_OK}; font-size: 12px; }}

/* 任务行 */
QFrame#taskRow {{ background: #fbfdfc; border: 1px solid #edf2f0; border-radius: 8px; }}
QFrame#taskRow:hover {{ border: 1px solid {C_PRIMARY}; }}
QCheckBox#taskCheck {{ font-size: 13px; spacing: 6px; }}
QCheckBox#taskCheck::indicator {{
    width: 16px; height: 16px; border: 1px solid #b9c7c3; border-radius: 4px;
    background: #ffffff;
}}
QCheckBox#taskCheck::indicator:checked {{
    background: {C_PRIMARY}; border: 1px solid {C_PRIMARY};
    image: none;
}}
QCheckBox#taskCheck::indicator:disabled {{ background: #eef0ef; border-color: #d7dddb; }}
QLabel#taskDesc {{ color: {C_TEXT_SUB}; font-size: 12px; }}

/* 按钮层级 */
QPushButton {{
    background: #ffffff; color: {C_TEXT}; border: 1px solid {C_BORDER};
    border-radius: 6px; padding: 6px 14px; min-height: 18px;
}}
QPushButton:hover {{ border: 1px solid {C_PRIMARY}; color: {C_PRIMARY}; }}
QPushButton:disabled {{ color: #a8b3b0; background: #f2f5f4; border: 1px solid #e7ecea; }}
QPushButton#primary {{
    background: {C_PRIMARY}; color: #ffffff; border: none; font-weight: 600;
    padding: 8px 22px;
}}
QPushButton#primary:hover {{ background: {C_PRIMARY_HOVER}; }}
QPushButton#primary:disabled {{ background: {C_PRIMARY_DISABLED}; color: #eaf6f4; }}
QPushButton#danger {{ color: {C_ERR}; border: 1px solid #f0c6c2; }}
QPushButton#danger:hover {{ background: #fdf0ef; border: 1px solid {C_ERR}; }}
QPushButton#ghost {{ border: none; background: transparent; color: {C_TEXT_SUB}; }}
QPushButton#ghost:hover {{ color: {C_PRIMARY}; }}
QPushButton#link {{ border: none; background: transparent; color: {C_PRIMARY}; }}
QPushButton#link:hover {{ text-decoration: underline; }}

QComboBox, QLineEdit {{
    background: #ffffff; border: 1px solid {C_BORDER}; border-radius: 6px;
    padding: 5px 10px;
}}
QComboBox:focus, QLineEdit:focus {{ border: 1px solid {C_PRIMARY}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{ background: #c9d4d1; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #a9bcb8; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #c9d4d1; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QCheckBox#logOpt {{ font-size: 12px; color: {C_TEXT_SUB}; spacing: 5px; }}
QCheckBox#logOpt::indicator {{
    width: 14px; height: 14px; border: 1px solid #b9c7c3; border-radius: 3px; background: #ffffff;
}}
QCheckBox#logOpt::indicator:checked {{ background: {C_PRIMARY}; border-color: {C_PRIMARY}; }}

/* 日志（深色等宽） */
QPlainTextEdit#logView {{
    background: #12211f; color: #d7e6e2; border: none; border-radius: 8px;
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
    font-size: 12px; padding: 6px;
}}
QLabel#statusPill {{
    background: {C_PRIMARY}; color: #ffffff; border-radius: 9px;
    padding: 3px 12px; font-size: 12px; font-weight: 600;
}}
QLabel#statusPillIdle {{ background: #dbe5e2; color: {C_TEXT_SUB}; }}
QLabel#statusPillStop {{ background: #f3d9c4; color: #8a4b12; }}
QProgressBar {{
    background: #e7eeec; border: none; border-radius: 4px; height: 8px; text-align: center;
}}
QProgressBar::chunk {{ background: {C_PRIMARY}; border-radius: 4px; }}

QHeaderView::section, QTableWidget {{ background: #ffffff; }}
QDialog {{ background: {C_BG}; }}
QDialog QLabel#aboutText {{ color: {C_TEXT_SUB}; font-size: 12px; }}
"""


class Worker(QtCore.QObject):
    """在后台线程运行的通用 worker（封装 callable + 回调）。"""

    finished = QtCore.Signal(object)   # (ok, result, error_text)

    def __init__(self, fn, *args):
        super().__init__()
        self._fn = fn
        self._args = args

    @QtCore.Slot()
    def run(self):
        try:
            result = self._fn(*self._args)
            self.finished.emit((True, result, ""))
        except Exception as exc:  # noqa: BLE001
            err = getattr(exc, "message", None) or str(exc)
            self.finished.emit((False, None, err))


class SingleInstance(QtCore.QObject):
    """极简单实例锁：第二次启动会触发 activated 信号（用于激活既有窗口）。"""
    activated = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server: Optional[QtCore.QLocalServer] = None

    def acquire(self) -> bool:
        try:
            from PySide6.QtNetwork import QLocalServer, QLocalSocket
        except ImportError:
            return True  # 无 QtNetwork 时退化为允许多实例
        sock = QLocalSocket()
        sock.connectToServer(APP_ID)
        if sock.waitForConnected(300):
            sock.disconnectFromServer()
            return False  # 已有一个实例
        QLocalServer.removeServer(APP_ID)
        self._server = QLocalServer()
        if not self._server.listen(APP_ID):
            return False
        self._server.newConnection.connect(self._on_connection)
        return True

    def _on_connection(self):
        # 新实例连接进来：通知主窗口激活自己
        if self._server is not None:
            while self._server.hasPendingConnections():
                conn = self._server.nextPendingConnection()
                if conn is not None:
                    conn.disconnectFromServer()
        self.activated.emit()


def _rounded_pixmap(path: str, size: int) -> QtGui.QPixmap:
    """读取 PNG 并缩放为方形圆角图标。失败返回空 QPixmap（调用方降级）。"""
    pm = QtGui.QPixmap(path)
    if pm.isNull():
        return pm
    pm = pm.scaled(size, size, QtCore.Qt.KeepAspectRatio,
                   QtCore.Qt.SmoothTransformation)
    out = QtGui.QPixmap(size, size)
    out.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(out)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    path_q = QtGui.QPainterPath()
    path_q.addRoundedRect(QtCore.QRectF(0, 0, size, size), size * 0.22,
                          size * 0.22)
    painter.setClipPath(path_q)
    painter.drawPixmap(
        (size - pm.width()) // 2, (size - pm.height()) // 2, pm)
    painter.end()
    return out


class Card(QtWidgets.QFrame):
    """圆角浅色卡片：可选标题 + 简短说明 + 内容区。"""

    def __init__(self, title: str = "", hint: str = "",
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        if title:
            head = QtWidgets.QHBoxLayout()
            t = QtWidgets.QLabel(title)
            t.setObjectName("cardTitle")
            head.addWidget(t)
            head.addStretch(1)
            lay.addLayout(head)
        if hint:
            h = QtWidgets.QLabel(hint)
            h.setObjectName("cardHint")
            h.setWordWrap(True)
            lay.addWidget(h)
        self.body = QtWidgets.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        lay.addLayout(self.body)

    def add(self, widget) -> None:
        self.body.addWidget(widget)

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class TaskRow(QtWidgets.QFrame):
    """单个任务的勾选行：状态标签 + 标题 + 描述。"""

    def __init__(self, task: dict, on_toggle, parent=None):
        super().__init__(parent)
        self.setObjectName("taskRow")
        # 任务描述可能换行；让行高始终跟随内容，避免在滚动容器里挤到下一行。
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Minimum)
        self.task = task
        self._status = "idle"   # idle/ready/running/success/failed/cancelled/skipped
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 6)
        outer.setSpacing(2)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(8)
        self.check = QtWidgets.QCheckBox(task["title"])
        self.check.setObjectName("taskCheck")
        self.check.setToolTip(task.get("description", ""))
        self.check.toggled.connect(lambda on, t=task: on_toggle(t, on))
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("stateTagIdle")
        self.status_label.setVisible(False)
        top.addWidget(self.check)
        top.addStretch(1)
        top.addWidget(self.status_label)
        outer.addLayout(top)

        desc = QtWidgets.QLabel(task.get("description", ""))
        desc.setObjectName("taskDesc")
        desc.setWordWrap(True)
        desc.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Minimum)
        outer.addWidget(desc)

    def set_runnable(self, runnable: bool, reason: str = "") -> None:
        self.check.setEnabled(runnable)
        if not runnable:
            self.status_label.setText("脚本缺失")
            self.status_label.setToolTip(reason or "请先同步最新脚本")
            self._show_tag("stateTagErr")

    def _show_tag(self, object_name: str) -> None:
        self.status_label.setVisible(True)
        self.status_label.setObjectName(object_name)
        style = self.status_label.style()
        style.unpolish(self.status_label)
        style.polish(self.status_label)

    def mark_state(self, state: str, text: str = "") -> None:
        """把状态标签置为指定状态并显示；空 text 用默认文案。"""
        default_text = {
            "ready": "可运行", "running": "运行中…", "success": "成功",
            "failed": "失败", "cancelled": "已停止", "skipped": "跳过",
            "limited": "限时活动", "missing": "脚本缺失",
        }
        obj = {
            "ready": "stateTagReady", "running": "stateTagRun",
            "success": "stateTagOk", "failed": "stateTagErr",
            "cancelled": "stateTagCancel", "skipped": "stateTagSkip",
            "limited": "stateTagCancel", "missing": "stateTagErr",
            "idle": "stateTagIdle",
        }
        if state == "idle":
            self.status_label.setVisible(False)
            return
        self.status_label.setText(text or default_text.get(state, state))
        self._show_tag(obj.get(state, "stateTagIdle"))

    def mark_running(self) -> None:
        self.mark_state("running")

    def mark_done(self, state: str) -> None:
        if state == "idle":
            self.mark_state("idle")
        else:
            self.mark_state(state)

    @property
    def checked(self) -> bool:
        return self.check.isChecked()

    @property
    def enabled(self) -> bool:
        return self.check.isEnabled()


class MainWindow(QtWidgets.QMainWindow):
    # 后台线程 -> GUI 线程调度（PySide6 支持 Python 对象直传）
    _ui_call = QtCore.Signal(object)

    def __init__(self, adb: AdbService, settings: SettingsStore,
                 update: UpdateService, catalog: TaskCatalog):
        super().__init__()
        self._ui_call.connect(self._run_on_ui)
        self.adb = adb
        self.settings = settings
        self.update = update
        self.catalog = catalog
        self.devices: list = []
        self.selected_serial = ""
        self._worker_threads = []
        self.runner: Optional[TaskRunner] = None
        self._running = False
        self._stop_reason = ""
        self._task_rows: dict = {}
        self._all_lines: list = []
        self._log_auto_scroll = True
        self._log_errors_only = False
        self._run_started_at = 0.0
        self._current_title = ""
        self._task_index = 0
        self._task_total = 0
        self._build_ui()
        self._load_persisted_choice()
        self._refresh_device_state(initial=True)
        self._runtime_ready = False
        self._runtime_missing = []
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        self.setWindowTitle("Coin11 助手")
        self.resize(1060, 860)
        self.setMinimumSize(880, 640)
        central = QtWidgets.QWidget()
        central.setObjectName("root")
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        self.setCentralWidget(central)
        self.setStyleSheet(QSS)

        icon = constants.logo_ico_path()
        if icon:
            self.setWindowIcon(QtGui.QIcon(icon))

        # ---- 品牌条 ----
        self._build_brand_bar(root)

        # ---- 设备卡片 ----
        self._build_device_card(root)

        # ---- 运行组件卡片（轻量版：缺失时提示下载） ----
        self._build_runtime_card(root)

        # ---- 上游脚本版本卡片 ----
        self._build_version_card(root)

        # ---- 任务卡片（滚动） ----
        self._build_task_card(root)

        # ---- 运行控制 ----
        self._build_control_card(root)

        # ---- 日志卡片 ----
        self._build_log_card(root)

        self.statusBar().showMessage("就绪")
        base_font = self.font()
        if base_font.pointSize() < 9:
            base_font.setPointSize(9)
            self.setFont(base_font)
        self._populate_tasks()
        self._refresh_version()
        # 后台检测运行组件（不阻塞首屏）
        QtCore.QTimer.singleShot(300, lambda: self._refresh_runtime_status())

    def _build_brand_bar(self, root: QtWidgets.QVBoxLayout) -> None:
        bar = QtWidgets.QFrame()
        bar.setObjectName("brandBar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(14)
        png = constants.logo_png_path()
        icon_label = QtWidgets.QLabel()
        icon_label.setFixedSize(52, 52)
        if png:
            pm = _rounded_pixmap(png, 52)
            if not pm.isNull():
                icon_label.setPixmap(pm)
        lay.addWidget(icon_label)
        text_col = QtWidgets.QVBoxLayout()
        text_col.setSpacing(2)
        name = QtWidgets.QLabel("Coin11 助手")
        name.setObjectName("brandName")
        sub = QtWidgets.QLabel("淘宝 / 闲鱼 / 支付宝 日常任务 · 完整运行时离线版")
        sub.setObjectName("brandSub")
        text_col.addWidget(name)
        text_col.addWidget(sub)
        lay.addLayout(text_col)
        lay.addStretch(1)
        self.conn_pill = QtWidgets.QLabel()
        self.conn_pill.setObjectName("connPill")
        self.conn_dot = QtWidgets.QLabel("●")
        self.conn_dot.setObjectName("connDot")
        lay.addWidget(self.conn_dot)
        lay.addWidget(self.conn_pill)
        settings_btn = QtWidgets.QPushButton("设置与关于")
        settings_btn.setObjectName("ghost")
        settings_btn.setStyleSheet(
            "QPushButton { color: #ffffff; border: none; }"
            "QPushButton:hover { color: #ffffff; text-decoration: underline; }")
        settings_btn.clicked.connect(self._show_settings)
        lay.addWidget(settings_btn)
        root.addWidget(bar)
        self._set_connection_pill("检测中", ok=False)

    def _set_connection_pill(self, text: str, ok: bool = True) -> None:
        self.conn_dot.setText("●")
        self.conn_dot.setObjectName("connDotOk" if ok else "connDot")
        style = self.conn_dot.style()
        style.unpolish(self.conn_dot)
        style.polish(self.conn_dot)
        self.conn_pill.setText(text)

    def _make_device_card(self, card: Card) -> None:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        lab = QtWidgets.QLabel("目标设备")
        lab.setObjectName("fieldLabel")
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setMinimumWidth(300)
        self.device_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.refresh_btn = QtWidgets.QPushButton("刷新设备")
        self.takeover_btn = QtWidgets.QPushButton("重新接管 ADB")
        self.refresh_btn.clicked.connect(
            lambda: self._refresh_device_state())
        self.takeover_btn.clicked.connect(self._on_takeover_adb)
        row.addWidget(lab)
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_btn)
        row.addWidget(self.takeover_btn)
        card.add_layout(row)

    def _build_device_card(self, root: QtWidgets.QVBoxLayout) -> None:
        card = Card("连接与设备",
                    "选择本次任务要操作的手机；同一时间只运行一个设备、一个任务。")
        self._make_device_card(card)
        root.addWidget(card)

    def _build_runtime_card(self, root: QtWidgets.QVBoxLayout) -> None:
        """运行组件状态卡片：轻量版在缺失时提示下载入口，任务前拦截。"""
        self.runtime_card = Card(
            "运行组件",
            "任务脚本需要内置的 Python 运行时与依赖组件；缺失时任务不会启动，"
            "请先到“下载中心”下载必需组件。")
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        self.runtime_status_label = QtWidgets.QLabel("检测中…")
        self.runtime_status_label.setObjectName("stateTagIdle")
        self.runtime_dir_label = QtWidgets.QLabel("")
        self.runtime_dir_label.setObjectName("fieldLabel")
        self.runtime_dir_label.setWordWrap(True)
        self.open_download_btn = QtWidgets.QPushButton("下载中心")
        self.open_download_btn.setObjectName("primary")
        self.runtime_refresh_btn = QtWidgets.QPushButton("重新检测")
        self.open_download_btn.clicked.connect(self._on_open_download_center)
        self.runtime_refresh_btn.clicked.connect(
            self._on_refresh_runtime_status)
        row.addWidget(self.runtime_status_label)
        row.addWidget(self.runtime_dir_label, 1)
        row.addWidget(self.runtime_refresh_btn)
        row.addWidget(self.open_download_btn)
        self.runtime_card.add_layout(row)
        root.addWidget(self.runtime_card)
        # 初始状态（延迟到首个 timer tick 后台检测，避免启动卡顿）

    def _runtime_dir_label_text(self) -> str:
        rt = resolve_data_runtime_dir(self.settings)
        return f"目录：{rt}"

    def _on_refresh_runtime_status(self):
        self._refresh_runtime_status()

    def _refresh_runtime_status(self, background: bool = True):
        """后台线程全量检测运行组件；UI 状态回到 GUI 线程。"""
        self.runtime_status_label.setText("检测中…")
        self.runtime_status_label.setObjectName("stateTagIdle")
        rt_dir = resolve_data_runtime_dir(self.settings)

        def _do():
            ok, message, missing = ensure_runtime_ready(rt_dir)
            self._on_ui(lambda: self._apply_runtime_status(ok, message, missing))

        if background:
            threading.Thread(target=_do, daemon=True).start()
        else:
            _do()

    def _apply_runtime_status(self, ok: bool, message: str, missing: list):
        self.runtime_dir_label.setText(self._runtime_dir_label_text())
        self.runtime_status_label.setText("运行组件就绪" if ok else "缺少运行组件")
        self.runtime_status_label.setObjectName(
            "stateTagOk" if ok else "stateTagErr")
        style = self.runtime_status_label.style()
        style.unpolish(self.runtime_status_label)
        style.polish(self.runtime_status_label)
        self._runtime_ready = ok
        self._runtime_missing = list(missing)
        # 缺组件时任务仍可勾选，但开始运行会被拦截并引导下载中心
        self._log_line(f"[运行时] {message}")

    def _on_open_download_center(self):
        if getattr(self, "_running", False):
            QtWidgets.QMessageBox.information(
                self, "正在运行", "任务运行中请先停止，再打开下载中心。")
            return
        dlg = RuntimeDownloadDialog(self.settings, parent=self)
        dlg.exec()
        self._refresh_runtime_status()

    def _build_version_card(self, root: QtWidgets.QVBoxLayout) -> None:
        card = Card("上游脚本版本",
                    "脚本保存在用户数据目录，可一键同步最新或回退上一版（失败保持当前可用版本）。")
        row = QtWidgets.QHBoxLayout()
        self.version_label = QtWidgets.QLabel("读取中…")
        self.version_label.setWordWrap(True)
        self.sync_btn = QtWidgets.QPushButton("同步最新脚本")
        self.restore_btn = QtWidgets.QPushButton("恢复上一脚本版本")
        self.sync_btn.clicked.connect(self._on_sync)
        self.restore_btn.clicked.connect(self._on_restore)
        row.addWidget(self.version_label, 1)
        row.addWidget(self.sync_btn)
        row.addWidget(self.restore_btn)
        card.add_layout(row)
        root.addWidget(card)

    def _build_task_card(self, root: QtWidgets.QVBoxLayout) -> None:
        self.task_card = Card(
            "任务清单",
            "勾选要运行的任务（按列表顺序执行）。限时活动默认折叠：页面可能已改版/过期，"
            "运行前会再次确认。")
        # 滚动容器
        scroll = QtWidgets.QScrollArea()
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumHeight(230)
        scroll.setMaximumHeight(360)
        scroll.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                             QtWidgets.QSizePolicy.Policy.Expanding)
        inner = QtWidgets.QWidget()
        inner.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                            QtWidgets.QSizePolicy.Policy.Minimum)
        self.tasks_layout = QtWidgets.QVBoxLayout(inner)
        self.tasks_layout.setContentsMargins(2, 2, 2, 2)
        self.tasks_layout.setSpacing(6)
        # 关键：滚动区的内容按子项实际高度计算最小尺寸，超出视口时由滚动条接管。
        # 否则 QScrollArea 会把后续任务排到视口外，与下面的操作栏发生重叠。
        self.tasks_layout.setSizeConstraint(
            QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)
        scroll.setWidget(inner)
        self.task_card.add(scroll)

        sel_row = QtWidgets.QHBoxLayout()
        self.select_all_btn = QtWidgets.QPushButton("全选日常任务")
        self.clear_all_btn = QtWidgets.QPushButton("取消选择")
        self.expand_limited_btn = QtWidgets.QPushButton("显示限时活动 ▾")
        self.select_all_btn.setObjectName("link")
        self.clear_all_btn.setObjectName("link")
        self.expand_limited_btn.setObjectName("ghost")
        self.select_all_btn.clicked.connect(lambda: self._set_all(True))
        self.clear_all_btn.clicked.connect(lambda: self._set_all(False))
        self.expand_limited_btn.clicked.connect(self._toggle_limited)
        sel_row.addWidget(self.select_all_btn)
        sel_row.addWidget(self.clear_all_btn)
        sel_row.addStretch(1)
        sel_row.addWidget(self.expand_limited_btn)
        self.task_card.add_layout(sel_row)
        root.addWidget(self.task_card, 3)

    def _build_control_card(self, root: QtWidgets.QVBoxLayout) -> None:
        card = Card("运行控制")
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)
        self.run_btn = QtWidgets.QPushButton("开始运行")
        self.run_btn.setObjectName("primary")
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_start_run)
        self.stop_btn.clicked.connect(self._on_stop_run)
        self.status_pill = QtWidgets.QLabel("空闲")
        self.status_pill.setObjectName("statusPillIdle")
        self.run_state_label = QtWidgets.QLabel("未开始")
        self.run_state_label.setObjectName("fieldLabel")
        self.elapsed_label = QtWidgets.QLabel("")
        self.elapsed_label.setObjectName("fieldLabel")
        self.progress_text = QtWidgets.QLabel("0/0")
        self.progress_text.setObjectName("fieldLabel")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(180)
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        row.addSpacing(6)
        row.addWidget(self.status_pill)
        row.addSpacing(6)
        row.addWidget(self.run_state_label)
        row.addStretch(1)
        row.addWidget(self.elapsed_label)
        row.addWidget(self.progress_text)
        row.addWidget(self.progress)
        card.add_layout(row)
        root.addWidget(card)

    def _build_log_card(self, root: QtWidgets.QVBoxLayout) -> None:
        card = Card("实时日志",
                    "脚本输出逐行实时显示（不等任务结束）；可用“仅看错误”过滤与自动滚动。")
        bar = QtWidgets.QHBoxLayout()
        self.auto_scroll_check = QtWidgets.QCheckBox("自动滚动")
        self.auto_scroll_check.setObjectName("logOpt")
        self.auto_scroll_check.setChecked(True)
        self.auto_scroll_check.toggled.connect(
            lambda on: setattr(self, "_log_auto_scroll", on))
        self.errors_only_check = QtWidgets.QCheckBox("仅看错误")
        self.errors_only_check.setObjectName("logOpt")
        self.errors_only_check.toggled.connect(self._toggle_errors_only)
        self.open_log_btn = QtWidgets.QPushButton("打开日志目录")
        self.copy_btn = QtWidgets.QPushButton("复制诊断文本")
        self.open_log_btn.setObjectName("link")
        self.copy_btn.setObjectName("link")
        self.open_log_btn.clicked.connect(self._on_open_log_dir)
        self.copy_btn.clicked.connect(self._on_copy_diag)
        bar.addWidget(self.auto_scroll_check)
        bar.addWidget(self.errors_only_check)
        bar.addStretch(1)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.open_log_btn)
        card.add_layout(bar)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        card.add(self.log_view)
        root.addWidget(card, 4)

    # ------------------------------------------------------------- 任务列表
    def _clear_task_containers(self):
        while self.tasks_layout.count():
            item = self.tasks_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        # 折叠容器内还持有限时行控件，一并释放
        holder = getattr(self, "_limited_holder", None)
        if holder is not None:
            holder.setParent(None)
            holder.deleteLater()
        self._limited_holder = None
        self._daily_rows = []
        self._limited_rows = []
        self._task_rows.clear()

    def _add_task_row(self, task: dict, layout) -> TaskRow:
        row = TaskRow(task, on_toggle=self._on_task_toggled)
        layout.addWidget(row)
        self._task_rows[task["id"]] = row
        return row

    def _add_section_label(self, layout, text: str) -> None:
        lab = QtWidgets.QLabel(text)
        lab.setObjectName("sectionTitle")
        layout.addWidget(lab)

    def _populate_tasks(self):
        self._clear_task_containers()
        last_ids = set(self.settings.last_task_ids)

        # 日常任务组（直接放主布局，默认可见）
        self._add_section_label(self.tasks_layout, "日常任务（默认展示）")
        self._daily_rows = []
        for task in self.catalog.daily_tasks():
            row = self._add_task_row(task, self.tasks_layout)
            self._daily_rows.append(row)
            if task.get("exists"):
                row.set_runnable(True)
                row.mark_state("ready")
            else:
                row.set_runnable(False, task.get("unavailable_reason", ""))
            tid = task["id"]
            if tid in last_ids and row.enabled:
                row.check.setChecked(True)

        # 限时活动折叠容器（默认隐藏）
        holder = QtWidgets.QWidget()
        hlay = QtWidgets.QVBoxLayout(holder)
        hlay.setContentsMargins(0, 0, 0, 0)
        hlay.setSpacing(6)
        self._add_section_label(hlay, "限时活动（可能已过期，页面可能已变更）")
        self._limited_rows = []
        for task in self.catalog.limited_tasks():
            row = self._add_task_row(task, hlay)
            self._limited_rows.append(row)
            if task.get("exists"):
                row.set_runnable(True)
                row.mark_state("limited")
            else:
                row.set_runnable(False, task.get("unavailable_reason", ""))
            tid = task["id"]
            if tid in last_ids and row.enabled:
                row.check.setChecked(True)
        holder.setVisible(False)
        self._limited_holder = holder
        self.tasks_layout.addWidget(holder)
        self.tasks_layout.addStretch(1)
        self.expand_limited_btn.setText("显示限时活动 ▾")

    def _toggle_limited(self):
        if self._limited_holder is None:
            return
        visible = self._limited_holder.isVisible()
        self._limited_holder.setVisible(not visible)
        self.expand_limited_btn.setText("隐藏限时活动 ▴" if not visible
                                        else "显示限时活动 ▾")

    def _on_task_toggled(self, task: dict, on: bool):
        # 持久化勾选
        ids = self._selected_task_ids()
        self.settings.set("last_task_ids", ids)

    def _selected_task_ids(self):
        return [tid for tid, row in self._task_rows.items()
                if row.checked]

    def _set_all(self, on: bool):
        rows = getattr(self, "_daily_rows", [])
        for row in rows:
            if row.enabled:
                row.check.setChecked(on)
        self.settings.set("last_task_ids", self._selected_task_ids())

    # ------------------------------------------------------------ 设备状态
    def _load_persisted_choice(self):
        last = self.settings.last_device_serial
        if last:
            self.selected_serial = last

    def _on_device_changed(self, index: int):
        if 0 <= index < len(self.devices):
            dev = self.devices[index]
            self.selected_serial = dev.serial
            self.settings.set("last_device_serial", dev.serial)

    def _refresh_device_state(self, initial: bool = False):
        self._set_busy(self.refresh_btn, True)
        self._set_connection_pill("正在检测 ADB…", ok=False)
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            info = self.adb.connection_state()
        except Exception as exc:  # noqa: BLE001
            msg = getattr(exc, "message", None) or str(exc)
            info = {"adbc": self.adb.available(), "state": "error",
                    "devices": [], "message": msg, "hint": ""}
        # worker 线程只产生数据，UI 更新回到 GUI 线程
        self._on_ui(lambda: self._apply_connection(info))

    def _apply_connection(self, info: dict):
        """GUI 线程内应用连接信息（可能由 _apply_refresh_finish 回调链触发）。"""
        state = info.get("state", "error")
        self.devices = info.get("devices", [])
        state_names = {"no_adb": "ADB 缺失", "error": "ADB 错误",
                       "no_device": "无设备", "unauthorized": "未授权",
                       "offline": "离线", "device": "已就绪"}
        text = f"ADB：{state_names.get(state, state)}"
        if state in ("device", "no_device"):
            ready = [d for d in self.devices if d.is_device]
            text = f"ADB：{state_names.get(state, state)} · 可用设备 {len(ready)} 台"
        self._set_connection_pill(text, ok=(state == "device"))
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for dev in self.devices:
            tag = f"{dev.serial}"
            if dev.model:
                tag += f"  [{dev.model}]"
            if not dev.is_device:
                tag += f"  ({dev.state})"
            self.device_combo.addItem(tag, dev.serial)
        if self.devices:
            idx = 0
            serials = [d.serial for d in self.devices]
            if self.selected_serial in serials:
                idx = serials.index(self.selected_serial)
            self.device_combo.setCurrentIndex(idx)
            dev = self.devices[idx]
            self.selected_serial = dev.serial
            self.settings.set("last_device_serial", dev.serial)
        self.device_combo.blockSignals(False)
        if state in ("no_adb", "no_device", "offline", "unauthorized", "error"):
            hint = info.get("hint", "")
            if state == "no_adb":
                self._show_guide_4steps(info.get("hint", ""))
            else:
                self._log_line(f"[提示] {info.get('message', '')}")
                if hint:
                    self._log_line(f"[操作] {hint}")
        else:
            self._log_line(f"[设备] {info.get('message', '')}")
        self._set_busy(self.refresh_btn, False)

    def _show_guide_4steps(self, hint: str):
        """首次无 ADB：给出可操作的四步提示。"""
        self._log_line("[提示] 未找到内置 ADB 程序。")
        self._log_line("[操作] 请确认发行目录含 platform-tools\\adb.exe，或重新安装本应用。")
        self._log_line("[操作] 若要手动修复：1) 重新安装本应用；"
                       "2) 确认杀毒软件未隔离 adb.exe；"
                       "3) 以管理员身份重新运行一次；4) 重启应用。")

    def _refresh_version(self):
        status = self.update.status()
        cur = status["current_commit"]
        prev = status["previous_commit"]
        if cur:
            self.version_label.setText(
                f"本地脚本：{cur[:12]}（更新于 {status['current_updated']}）"
                + (f"　上一版：{prev[:12]}" if prev else ""))
        else:
            self.version_label.setText(
                "本地脚本：出厂版本（源码/首次启动，显示仓库当前代码）")
        self._refresh_tasks()

    def _refresh_tasks(self):
        """脚本根目录变化后重建任务列表（保持勾选状态尽量一致）。"""
        self.catalog.set_root_dir(self._script_root())
        self._populate_tasks()
        self._log_line("[提示] 任务可用状态已按当前脚本目录刷新。")

    def _script_root(self):
        return self.update.current_dir

    def _set_busy(self, btn: QtWidgets.QPushButton, busy: bool):
        btn.setEnabled(not busy)
        btn.setText("检测中…" if busy else
                    ("重新接管 ADB" if btn is self.takeover_btn else "刷新设备"))

    # ------------------------------------------------------------ 接管 ADB
    def _on_takeover_adb(self):
        if not self.adb.available():
            self._log_line("[错误] 未找到内置 ADB。")
            return
        ret = QtWidgets.QMessageBox.question(
            self, "重新接管 ADB",
            "这会执行 adb kill-server 与 adb start-server。\n\n"
            "若其它工具（如手机助手、Appium、uiautomator2）正在使用同一 ADB 服务，"
            "它们的连接会被中断，需要重新连接。是否继续？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if ret != QtWidgets.QMessageBox.Yes:
            return
        threading.Thread(target=self._takeover_worker, daemon=True).start()

    def _takeover_worker(self):
        self._log_line("[ADB] 正在重新接管 ADB 服务 …")
        try:
            self.adb.take_over()
            self._log_line("[ADB] 接管完成。")
        except AdbError as exc:
            self._log_line(f"[错误] {exc.message}")
            if exc.hint:
                self._log_line(f"[操作] {exc.hint}")
        finally:
            self._on_ui(self._refresh_device_state)

    # ------------------------------------------------------------ 同步/回退
    def _on_sync(self):
        if self._running:
            QtWidgets.QMessageBox.information(
                self, "正在运行", "任务运行中不能同步脚本，请先停止。")
            return
        self.sync_btn.setEnabled(False)
        self.restore_btn.setEnabled(False)
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        try:
            result = self.update.sync()
            self._log_line(f"[同步] {result.get('message', '')}")
            if result.get("changed"):
                self._on_ui(lambda: (self._refresh_version(),
                                     self._refresh_tasks()))
        except UpdateError as exc:
            self._log_line(f"[错误] {exc.message}")
            if exc.hint:
                self._log_line(f"[操作] {exc.hint}")
        except Exception as exc:  # noqa: BLE001
            self._log_line(f"[错误] 同步失败：{exc}")
        finally:
            self._on_ui(lambda: (self.sync_btn.setEnabled(True),
                                 self.restore_btn.setEnabled(True)))

    def _on_restore(self):
        if self._running:
            QtWidgets.QMessageBox.information(
                self, "正在运行", "任务运行中不能回退脚本，请先停止。")
            return
        self.restore_btn.setEnabled(False)
        threading.Thread(target=self._restore_worker, daemon=True).start()

    def _restore_worker(self):
        try:
            result = self.update.restore_previous()
            self._log_line(f"[回退] {result.get('message', '')}")
            self._on_ui(lambda: (self._refresh_version(),
                                 self._refresh_tasks()))
        except UpdateError as exc:
            self._log_line(f"[错误] {exc.message}")
            if exc.hint:
                self._log_line(f"[操作] {exc.hint}")
        except Exception as exc:  # noqa: BLE001
            self._log_line(f"[错误] 回退失败：{exc}")
        finally:
            self._on_ui(lambda: self.restore_btn.setEnabled(True))

    # ------------------------------------------------------------ 运行控制
    def _on_start_run(self):
        if self._running:
            return
        ids = self._selected_task_ids()
        if not ids:
            QtWidgets.QMessageBox.information(
                self, "请选择任务", "请先勾选要运行的任务。")
            return
        if not self.devices:
            QtWidgets.QMessageBox.warning(
                self, "没有设备", "请先连接并授权 USB 调试设备。")
            return
        ready = [d for d in self.devices if d.is_device]
        if not ready:
            QtWidgets.QMessageBox.warning(
                self, "设备不可用", "当前设备未处于可用状态，请先在手机上完成授权。")
            return
        # 运行前基础依赖校验（后台执行 import 探针，避免卡 UI）：
        # 缺失/损坏时不启动子进程，引导去下载中心
        self.run_btn.setEnabled(False)
        self.run_btn.setText("校验运行组件…")
        rt_dir = resolve_data_runtime_dir(self.settings)
        threading.Thread(
            target=self._pre_run_check_worker, args=(rt_dir, ids),
            daemon=True).start()

    def _pre_run_check_worker(self, rt_dir: str, ids: list):
        """后台线程：全量校验运行组件后回 GUI 决定是否启动。"""
        try:
            ok, message, missing = ensure_runtime_ready(rt_dir)
        except Exception as exc:  # noqa: BLE001
            ok, message, missing = False, f"校验异常：{exc}", []
        self._on_ui(lambda: self._pre_run_check_finished(ok, message, missing,
                                                        ids))

    def _pre_run_check_finished(self, ok: bool, message: str, missing: list,
                                ids: list):
        self.run_btn.setEnabled(True)
        self.run_btn.setText("开始运行")
        if not ok:
            self._apply_runtime_status(False, message, missing)
            box = QtWidgets.QMessageBox(
                QtWidgets.QMessageBox.Icon.Warning,
                "缺少运行组件",
                "任务脚本需要完整运行组件才能启动，当前尚未安装齐备：\n\n"
                f"{message}\n\n是否现在打开“下载中心”下载必需组件？",
                QtWidgets.QMessageBox.StandardButton.Yes |
                QtWidgets.QMessageBox.StandardButton.No,
                self)
            if box.exec() == QtWidgets.QMessageBox.StandardButton.Yes:
                self._on_open_download_center()
            return
        self._apply_runtime_status(True, message, missing)
        self._launch_run(ids)

    def _launch_run(self, ids: list):
        """校验通过后的实际启动逻辑（GUI 线程）。"""
        limited = [tid for tid in ids if self.catalog.is_limited(tid)]
        if limited:
            titles = "、".join(self.catalog.get(t)["title"] for t in limited)
            ret = QtWidgets.QMessageBox.warning(
                self, "确认运行限时活动",
                constants.LIMITED_TASK_RUN_WARNING.format(title=titles),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ret != QtWidgets.QMessageBox.Yes:
                return
        self.settings.set("last_task_ids", ids)
        # 重置 UI
        for tid, row in self._task_rows.items():
            row.mark_done("idle")
            if tid in ids:
                row.mark_state("ready", "等待运行")
        self.log_view.clear()
        self._all_lines.clear()
        self._task_index = 0
        self._task_total = len(ids)
        self._update_progress()
        self._running = True
        self._stop_reason = ""
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_status_pill("running")
        self.run_state_label.setText("正在启动…")
        self._run_started_at = time.time()
        self.elapsed_label.setText("已用时 0 秒")
        threading.Thread(target=self._run_worker, args=(ids,),
                         daemon=True).start()

    def _set_status_pill(self, mode: str):
        text = {"running": "运行中", "idle": "空闲", "stop": "已停止"}.get(
            mode, "空闲")
        self.status_pill.setText(text)
        self.status_pill.setObjectName(
            "statusPill" if mode == "running" else
            ("statusPillStop" if mode == "stop" else "statusPillIdle"))
        style = self.status_pill.style()
        style.unpolish(self.status_pill)
        style.polish(self.status_pill)

    def _on_stop_run(self):
        if self._running and self.runner:
            self._log_line("[操作] 正在停止任务（安全停止进程树）…")
            self.runner.stop()

    def _run_worker(self, ids):
        auto_takeover = self.settings.auto_takeover_adb and self.adb.available()
        try:
            if auto_takeover:
                self._log_line("[ADB] 按设置自动接管 ADB 服务（会中断共享该服务的其它工具）…")
                self.adb.take_over()
            self.runner = TaskRunner(
                self.catalog, device_serial=self.selected_serial,
                on_log=self._on_log_signal,
                on_state=self._on_state_signal,
                on_progress=self._on_progress_signal,
                device_watch=self.adb.device_ready)
            py = runtime_python_exe()
            if py:
                self._log_line(f"[运行时] 使用数据运行时 Python：{py}")
            else:
                self._log_line("[运行时] 未找到可用数据运行时解释器。"
                               "请到“下载中心”下载必需运行组件。")
            model_dir = easyocr_model_dir()
            if model_dir:
                self._log_line(f"[运行时] EasyOCR 离线模型：{model_dir}")
            self._log_line(f"[运行] 队列共 {len(ids)} 个任务，"
                           f"设备 {self.selected_serial}。运行期间每约 2 秒检查设备在线状态。")
            outcomes = self.runner.run_tasks(ids, python_exe=py)
            device_lost = bool(getattr(self.runner, "_device_lost", False))
            if device_lost:
                self._stop_reason = "设备已断开，队列已自动停止"
            else:
                stopped = any(o.state == RunState.CANCELLED.value
                              for o in outcomes)
                self._stop_reason = "已手动停止" if stopped else ""
            for outcome in outcomes:
                if outcome.state == RunState.SUCCESS.value:
                    self._on_ui(lambda o=outcome: self._task_rows[
                        o.task_id].mark_done("success"))
                elif outcome.state == RunState.FAILED.value:
                    self._on_ui(lambda o=outcome: self._task_rows[
                        o.task_id].mark_done("failed"))
                elif outcome.state == RunState.CANCELLED.value:
                    self._on_ui(lambda o=outcome: self._task_rows[
                        o.task_id].mark_done("cancelled"))
                elif outcome.state == RunState.SKIPPED.value:
                    self._on_ui(lambda o=outcome: self._task_rows[
                        o.task_id].mark_done("skipped"))
            self._on_ui(lambda: self._finish_run_ui())
        except Exception as exc:  # noqa: BLE001
            self._log_line(f"[错误] 运行失败：{exc}")
            self._on_ui(lambda: self._finish_run_ui())
        finally:
            self.runner = None
            self._on_ui(lambda: self._set_running_ui(False))

    # 线程 -> UI 信号
    def _on_log_signal(self, text: str):
        self._on_ui(lambda: self._log_line(text))

    def _on_state_signal(self, state: dict):
        def apply():
            task = state.get("task", "")
            idx = state.get("index", 0)
            total = state.get("total", 0)
            if task:
                self._current_title = task
                self.run_state_label.setText(f"当前任务：{task}")
                if idx and total:
                    self._task_index = idx
                    self._task_total = total
                    self._update_progress()
                tid = state.get("task_id")
                if tid and tid in self._task_rows:
                    self._task_rows[tid].mark_running()
            else:
                self._current_title = ""
                self.run_state_label.setText(
                    f"状态：{state.get('state', '')}")
        self._on_ui(apply)

    def _on_progress_signal(self, prog: dict):
        def apply():
            idx = prog.get("index", 0)
            total = prog.get("total", 0)
            if idx and total:
                self._task_index = idx
                self._task_total = total
                self._update_progress()
        self._on_ui(apply)

    def _update_progress(self):
        total = max(1, self._task_total)
        done = max(0, min(total, self._task_index))
        self.progress_text.setText(f"{done}/{total}")
        self.progress.setRange(0, 100)
        self.progress.setValue(int(done * 100 / total))
        self.progress.setFormat(f"队列 {done}/{total}")

    def _tick(self):
        if self._running and self._run_started_at:
            secs = int(time.time() - self._run_started_at)
            self.elapsed_label.setText(f"已用时 {secs} 秒")

    def _finish_run_ui(self):
        if self._stop_reason:
            self._set_status_pill("stop")
            self.run_state_label.setText(self._stop_reason)
        else:
            self._set_status_pill("idle")
            self.run_state_label.setText("运行结束")
        self.elapsed_label.setText("")
        self._log_line("[结果] 任务列表执行结束。"
                       "注意：脚本正常结束不代表手机上的真实任务全部成功，"
                       "请以手机界面为准。")

    def _set_running_ui(self, running: bool):
        self._running = running
        self.run_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        if not running:
            self.elapsed_label.setText("")
            self._run_started_at = 0.0
            self.progress.setValue(self.progress.maximum())

    # ------------------------------------------------------------ UI 工具
    def _on_ui(self, fn):
        """在 GUI 线程执行 fn；已在 GUI 线程则同步执行（保证顺序），否则排队。"""
        if QtCore.QThread.currentThread() is self.thread():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"[界面错误] {exc}")
            return
        self._ui_call.emit(fn)

    @QtCore.Slot(object)
    def _run_on_ui(self, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[界面错误] {exc}")

    def _log_line(self, text: str):
        safe = redact(text)
        self._on_ui(lambda: self._append_log(safe))

    def _append_log(self, text: str):
        self._all_lines.append(text)
        if len(self._all_lines) > 12000:
            del self._all_lines[:4000]
        if self._log_errors_only and not is_error_line(text):
            return
        self.log_view.appendPlainText(text)
        if self._log_auto_scroll:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _toggle_errors_only(self, on: bool):
        self._log_errors_only = on
        self.log_view.clear()
        for line in self._all_lines:
            if not on or is_error_line(line):
                self.log_view.appendPlainText(line)
        if self._log_auto_scroll:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------ 设置/关于
    def _show_settings(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("设置与关于")
        lay = QtWidgets.QVBoxLayout(dlg)
        form = QtWidgets.QFormLayout()
        self.auto_check = QtWidgets.QCheckBox("运行前自动接管 ADB（默认开启）")
        self.auto_check.setChecked(self.settings.auto_takeover_adb)
        self.auto_check.toggled.connect(
            lambda on: self.settings.set("auto_takeover_adb", on))
        form.addRow(self.auto_check)
        lay.addLayout(form)
        info = QtWidgets.QLabel()
        info.setObjectName("aboutText")
        info.setWordWrap(True)
        status = self.update.status()
        lines = [
            "Coin11 助手（桌面版）",
            f"版本：{constants.APP_VERSION}",
            f"本地上游脚本：{status['current_commit'][:12] or '无'}",
            "上游仓库：https://github.com/czl0325/coin11-tb（Apache-2.0）",
            "",
            "本软件把开源项目 coin11-tb 包装为桌面应用（非官方），",
            "仅在本机操作你已授权的手机，不收集/上传任何数据，",
            "不处理登录、验证码、支付或绕过授权。",
            "任务脚本可能因 App 改版而失效，失败会如实显示；",
            "限时活动脚本可能已过期。",
            "",
            f"数据运行时解释器：{runtime_python_exe() or '（未安装，请到下载中心）'}",
            f"EasyOCR 模型：{easyocr_model_dir() or '（未配置，联网自动下载）'}",
            f"运行组件目录：{resolve_data_runtime_dir(self.settings)}",
            f"日志目录：{self.settings.data_dir}\\logs",
            f"脚本目录：{self._script_root()}",
        ]
        info.setText("\n".join(lines))
        lay.addWidget(info)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn, 0, alignment=QtCore.Qt.AlignRight)
        dlg.resize(560, 420)
        dlg.exec()

    def _on_open_log_dir(self):
        log_dir = os.path.join(self.settings.data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(log_dir)  # type: ignore[attr-defined]
            else:
                import shlex
                subprocess.Popen(["xdg-open", log_dir])
        except OSError as exc:
            self._log_line(f"[错误] 无法打开日志目录：{exc}")

    def _on_copy_diag(self):
        QtWidgets.QApplication.clipboard().setText(self.log_view.toPlainText())
        self.statusBar().showMessage("诊断文本已复制", 3000)

    # ------------------------------------------------------------ 关窗
    def closeEvent(self, event):
        if self._running:
            ret = QtWidgets.QMessageBox.question(
                self, "确认退出",
                "任务仍在运行，退出将停止当前任务。是否退出？",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ret != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
        if self.runner:
            self.runner.stop()
        # 私有 platform-tools 的 server 会脱离主窗口继续驻留，并锁住 adb.exe。
        # 退出前显式关闭它，下一次启动或覆盖升级都不会留下旧进程。
        try:
            if self.adb.available():
                self.adb.run(["kill-server"], timeout=5.0)
        except (AdbError, OSError) as exc:
            # 退出不因清理失败被阻断；下次接管 ADB 时仍会再次清理。
            self._log_line(f"[提示] 退出时关闭私有 ADB 未完成：{exc}")
        event.accept()


def run_gui(adb: AdbService, settings: SettingsStore,
            update: UpdateService, catalog: TaskCatalog) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Coin11助手")
    app.setOrganizationName("Coin11Helper")
    instance = SingleInstance()
    if not instance.acquire():
        print("已有 Coin11 助手实例在运行，已请求激活既有窗口。")
        return 0
    win = MainWindow(adb, settings, update, catalog)

    def _activate():
        win.showNormal()
        win.raise_()
        win.activateWindow()

    instance.activated.connect(_activate)
    win.show()
    return app.exec()
