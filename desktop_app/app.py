"""Coin11 助手桌面版主窗口（PySide6）。

工作台骨架（任务执行为中心）：
- 紧凑品牌条（透明浅条）：Coin11 logo + 应用名 + “轻量版”标签 + 连接状态 + 设置；
- 紧凑设备/上下文条：设备下拉 + 刷新（设备选择保留在一级），`设备 ▾` 二级菜单
  收纳“刷新设备 / 重新接管 ADB / 设备连接说明”；原“维护”下拉已删除，其全部
  动作（运行环境 / 脚本 / 诊断）迁入“设置与维护”对话框；
- QSplitter 双面板弹性主体：左侧任务面板 = 固定头部 + 任务滚动 + 底部固定选择
  工具栏（滚动区与工具栏结构分离）；右侧运行面板 = 选中计数 + 运行组件摘要 +
  开始 / 停止 / 状态胶囊 / 当前状态 / 任务计数 / 进度。窗口窄于 980px 时
  splitter 自动切为 vertical（resizeEvent 完成）；
- 任务行为紧凑单行：勾选 + 标题 + 状态标签，描述进 tooltip；
- 实时日志抽屉：默认折叠为简短状态条（显示最近一行输出），可展开查看完整日志；
  自动滚动 / 仅看错误 / 复制 / 打开目录在展开态可用（复用原折叠逻辑）。

运行模型：
- 全部系统调用（ADB / 任务子进程）在后台线程执行；设备断线看门狗在
  TaskRunner 内部线程中周期调用私有 ADB（adb 层有进程内锁，设备列表刷新
  与看门狗不会并发轰炸 adb），UI 只编排与呈现。
- 任务运行期间显示当前任务、实时日志、已用时、队列进度（i/n）与不确定
  进度动画；每个任务结束显示成功/失败/取消/跳过。
- 单实例：第二次启动激活已有窗口；关窗即退出应用并停止后台子进程。
- 保留：任务实时日志、ADB 私有服务退出清理、断线停任务、运行组件下载中心、
  脚本同步/回退、单实例、下载依赖安全边界。

视觉：纯 QSS——冷灰平色背景 + 白色半透明面板 + 细边框 + 圆角 +
#0F766E 主色，克制、高对比，无特效环境同样可读。
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
# 毛玻璃感：柔和渐变底 + 半透明白卡 + 细边框。全部为 QSS 绘制的视觉层次，
# 不依赖系统透明/毛玻璃特效；文本色保持足够对比，无特效环境同样可读。

C_BG = "#F5F7F8"            # 冷灰背景（工作台，单一平色）
C_CARD = "rgba(255,255,255,0.82)"   # 白色半透明面板
C_CARD_SOLID = "#ffffff"
C_PRIMARY = "#0F766E"       # 主色（深青）
C_PRIMARY_HOVER = "#0b625c"
C_PRIMARY_DISABLED = "#9cc7c3"
C_TEXT = "#172B2A"
C_TEXT_SUB = "#637371"
C_BORDER = "rgba(20,70,65,.10)"
C_RUNNING = "#0F766E"
C_OK = "#14662b"
C_ERR = "#a33a2f"
C_CANCEL = "#8a5a00"
C_SKIP = "#5f6b72"

QSS = f"""
* {{ font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif; }}
QMainWindow, QWidget#root {{ background: {C_BG}; }}
QWidget {{ color: {C_TEXT}; font-size: 13px; }}
QToolTip {{
    background: #24312f; color: #f3f8f7; border: none;
    padding: 6px 8px; border-radius: 4px;
}}

/* ---- 玻璃面板 ---- */
QFrame#panel {{
    background: {C_CARD}; border: 1px solid {C_BORDER};
    border-radius: 13px;
}}
QFrame#panel:disabled {{ background: {C_CARD_SOLID}; }}
QLabel#cardTitle {{ font-size: 14px; font-weight: 600; color: {C_TEXT}; }}
QLabel#cardHint {{ font-size: 12px; color: {C_TEXT_SUB}; }}
QLabel#sectionTitle {{ font-size: 12px; font-weight: 600; color: {C_TEXT_SUB}; }}

/* ---- 顶栏（透明浅条，54px 级紧凑） ---- */
QFrame#brandBar {{ background: transparent; border: none; }}
QLabel#brandName {{ font-size: 15px; font-weight: 700; color: {C_TEXT}; }}
QLabel#brandBadge {{
    color: {C_PRIMARY}; background: #e2f0ee; border: 1px solid #bcdcd7;
    border-radius: 9px; padding: 1px 8px; font-size: 11px;
}}

/* ---- 设备/状态 ---- */
QFrame#toolStrip {{ background: rgba(255,255,255,0.6); border: 1px solid {C_BORDER};
    border-radius: 10px; padding: 0px; }}
QLabel#fieldLabel {{ color: {C_TEXT_SUB}; font-size: 12px; }}
QLabel#connPill {{ color: {C_TEXT_SUB}; font-size: 12px; }}
QLabel#connDot {{ color: #b0a14a; font-size: 13px; }}
QLabel#connDotOk {{ color: {C_PRIMARY}; font-size: 13px; }}
QPushButton#settingsBtn {{
    background: transparent; border: 1px solid {C_BORDER}; color: {C_TEXT};
    border-radius: 8px; padding: 4px 12px; font-size: 12px;
}}
QPushButton#settingsBtn:hover {{ border: 1px solid {C_PRIMARY}; color: {C_PRIMARY}; }}
QLabel#stateTagOk {{
    color: {C_OK}; background: #e3f2e6; border: 1px solid #bfe3c6;
    border-radius: 9px; padding: 2px 9px; font-size: 12px;
}}
QLabel#stateTagErr {{
    color: {C_ERR}; background: #fbeae7; border: 1px solid #f0c8c2;
    border-radius: 9px; padding: 2px 9px; font-size: 12px;
}}
QLabel#stateTagCancel {{
    color: {C_CANCEL}; background: #faf1dc; border: 1px solid #ead9ae;
    border-radius: 9px; padding: 2px 9px; font-size: 12px;
}}
QLabel#stateTagSkip {{
    color: {C_SKIP}; background: #eef1f3; border: 1px solid #d9e0e3;
    border-radius: 9px; padding: 2px 9px; font-size: 12px;
}}
QLabel#stateTagIdle {{
    color: {C_TEXT_SUB}; background: #eef3f1; border: 1px solid #dde7e4;
    border-radius: 9px; padding: 2px 9px; font-size: 12px;
}}
QLabel#stateTagRun {{
    color: #ffffff; background: {C_PRIMARY}; border-radius: 9px;
    padding: 2px 9px; font-size: 12px;
}}
QLabel#stateTagReady {{ color: {C_OK}; font-size: 12px; }}

/* ---- 任务行（紧凑单行，约 42px：勾选 + 标题 + 状态标签） ---- */
QFrame#taskRow {{
    background: rgba(255,255,255,0.55); border: 1px solid transparent;
    border-radius: 8px;
}}
QFrame#taskRow:hover {{ background: #f0f7f5; border: 1px solid #cfe6e1; }}
QFrame#taskRow[rowChecked="true"] {{
    background: #e6f2ef; border: 1px solid #a8d5cd;
}}
QFrame#taskRow[rowChecked="true"]:hover {{ background: #def0ec; }}
QCheckBox#taskCheck {{ font-size: 13px; spacing: 7px; }}
QCheckBox#taskCheck::indicator {{
    width: 16px; height: 16px; border: 1px solid #a9bdb7; border-radius: 4px;
    background: {C_CARD_SOLID};
}}
QCheckBox#taskCheck::indicator:checked {{
    background: {C_PRIMARY}; border: 1px solid {C_PRIMARY}; image: none;
}}
QCheckBox#taskCheck::indicator:disabled {{ background: #eef0ef; border-color: #d7dddb; }}
QCheckBox#taskCheck:checked {{ color: {C_TEXT}; }}

/* ---- segmented 切换（日常任务 / 限时活动） ---- */
QFrame#segSwitch {{ background: #e6ecea; border-radius: 8px; }}
QPushButton#segBtn {{
    background: transparent; border: none; color: {C_TEXT_SUB};
    border-radius: 6px; padding: 4px 10px; font-size: 12px; font-weight: 600;
}}
QPushButton#segBtn:checked {{
    background: {C_CARD_SOLID}; color: {C_PRIMARY};
    border: 1px solid {C_BORDER};
}}
QPushButton#segBtn:hover:!checked {{ color: {C_TEXT}; }}

/* ---- 按钮层级 ---- */
QPushButton {{
    background: rgba(255,255,255,0.9); color: {C_TEXT};
    border: 1px solid {C_BORDER}; border-radius: 8px;
    padding: 6px 14px; min-height: 18px;
}}
QPushButton:hover {{ border: 1px solid {C_PRIMARY}; color: {C_PRIMARY}; }}
QPushButton:disabled {{ color: #9aa8a4; background: #eef2f1; border: 1px solid #e2e9e7; }}
QPushButton#primary {{
    background: {C_PRIMARY}; color: #ffffff; border: none; font-weight: 600;
    padding: 8px 22px; font-size: 13px;
}}
QPushButton#primary:hover {{ background: {C_PRIMARY_HOVER}; color: #ffffff; }}
QPushButton#primary:disabled {{
    background: {C_PRIMARY_DISABLED}; color: #e6f3f1; border: none;
}}
QPushButton#danger {{ color: {C_ERR}; border: 1px solid #efc3bd; }}
QPushButton#danger:hover {{ background: #fdf0ef; border: 1px solid {C_ERR}; }}
QPushButton#ghost {{ border: none; background: transparent; color: {C_TEXT_SUB}; }}
QPushButton#ghost:hover {{ color: {C_PRIMARY}; }}
QPushButton#link {{ border: none; background: transparent; color: {C_PRIMARY}; padding: 4px 8px; }}
QPushButton#link:hover {{ text-decoration: underline; color: {C_PRIMARY_HOVER}; }}
QPushButton#menuBtn {{
    background: rgba(255,255,255,0.85); border: 1px solid {C_BORDER};
    border-radius: 8px; padding: 5px 10px; color: {C_TEXT};
}}
QPushButton#menuBtn:hover {{ border: 1px solid {C_PRIMARY}; color: {C_PRIMARY}; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 4px; }}

QComboBox, QLineEdit {{
    background: rgba(255,255,255,0.95); border: 1px solid {C_BORDER};
    border-radius: 8px; padding: 5px 10px;
}}
QComboBox:focus, QLineEdit:focus {{ border: 1px solid {C_PRIMARY}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}

/* ---- 菜单（毛玻璃弹层） ---- */
QMenu {{
    background: rgba(255,255,255,0.96); border: 1px solid #d8e4e1;
    border-radius: 8px; padding: 6px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px; border-radius: 6px; color: {C_TEXT};
    font-size: 13px;
}}
QMenu::item:selected {{ background: #e0f1ee; color: {C_PRIMARY}; }}
QMenu::item:disabled {{ color: #a3afac; }}
QMenu::separator {{ height: 1px; background: #e5ecea; margin: 5px 8px; }}
QMenu::title {{
    padding: 4px 12px 2px 12px; color: {C_TEXT_SUB};
    font-size: 11px; font-weight: 600; background: transparent;
}}

/* ---- 滚动区（任务主体） ---- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{ background: #b9c9c5; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #96b0ab; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #b9c9c5; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QCheckBox#logOpt {{ font-size: 12px; color: {C_TEXT_SUB}; spacing: 5px; }}
QCheckBox#logOpt::indicator {{
    width: 14px; height: 14px; border: 1px solid #a9bdb7; border-radius: 3px;
    background: {C_CARD_SOLID};
}}
QCheckBox#logOpt::indicator:checked {{ background: {C_PRIMARY}; border-color: {C_PRIMARY}; }}

/* ---- 日志（折叠状态条 + 深色展开面板） ---- */
QLabel#logSummary {{
    color: {C_TEXT_SUB}; font-size: 12px; background: transparent;
    padding: 0 6px;
}}
QPlainTextEdit#logView {{
    background: #14211f; color: #d9e8e4; border: none; border-radius: 9px;
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
    font-size: 12px; padding: 6px;
}}
QLabel#statusPill {{
    background: {C_PRIMARY}; color: #ffffff; border-radius: 9px;
    padding: 3px 12px; font-size: 12px; font-weight: 600;
}}
QLabel#statusPillIdle {{ background: #dce7e4; color: {C_TEXT_SUB}; }}
QLabel#statusPillStop {{ background: #f3ddc6; color: #7c4510; }}
QProgressBar {{
    background: #e2ebe9; border: none; border-radius: 4px; height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {C_PRIMARY}; border-radius: 4px; }}

QHeaderView::section, QTableWidget {{ background: {C_CARD_SOLID}; }}
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
    """圆角浅色毛玻璃卡片：可选标题 + 简短说明 + 内容区。"""

    def __init__(self, title: str = "", hint: str = "",
                 parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setObjectName("panel")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)
        self.head_layout: Optional[QtWidgets.QHBoxLayout] = None
        self.title_label: Optional[QtWidgets.QLabel] = None
        if title:
            head = QtWidgets.QHBoxLayout()
            t = QtWidgets.QLabel(title)
            t.setObjectName("cardTitle")
            head.addWidget(t)
            head.addStretch(1)
            lay.addLayout(head)
            self.head_layout = head
            self.title_label = t
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

    def set_title_label(self, label: QtWidgets.QLabel) -> None:
        """替换 head_layout 中标题 label（供标题+计数组合使用）。"""
        if self.head_layout is None:
            return
        old = self.title_label
        if old is not None:
            self.head_layout.replaceWidget(old, label)
            old.deleteLater()
        self.title_label = label


class TaskRow(QtWidgets.QFrame):
    """单个任务的紧凑单行：勾选（标题）+ 状态标签；描述进 tooltip。"""

    def __init__(self, task: dict, on_toggle, parent=None):
        super().__init__(parent)
        self.setObjectName("taskRow")
        # 单行任务行：行高固定紧凑（40–44px），由滚动容器统一管理。
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(40)
        self.setMaximumHeight(44)
        self.task = task
        self._status = "idle"   # idle/ready/running/success/failed/cancelled/skipped
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 5, 10, 5)
        lay.setSpacing(8)
        # 行固定高度 42px（min/max 已约束 40–44），hover/checked 仅做克制底色
        self.setFixedHeight(42)
        self.setProperty("rowChecked", "false")
        self.check = QtWidgets.QCheckBox(task["title"])
        self.check.setObjectName("taskCheck")
        desc = task.get("description", "")
        if desc:
            # 描述不再常驻占行：作为 tooltip 提供，标题处可悬停查看
            self.check.setToolTip(desc)
            self.setToolTip(desc)
        self.check.toggled.connect(self._on_checked)
        self.check.toggled.connect(lambda on, t=task: on_toggle(t, on))
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("stateTagIdle")
        self.status_label.setVisible(False)
        lay.addWidget(self.check)
        lay.addStretch(1)
        lay.addWidget(self.status_label)

    def _on_checked(self, on: bool):
        self.setProperty("rowChecked", "true" if on else "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)

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
        self._log_expanded = False
        self._run_started_at = 0.0
        self._current_title = ""
        self._task_index = 0
        self._task_total = 0
        self._build_ui()
        self._load_persisted_choice()
        self._refresh_device_state(initial=True)
        self._runtime_ready = False
        self._runtime_missing = []
        # 缺件自动打开下载中心的单次状态（SPEC 2）：初次后台全量检测结果
        # 为“非就绪”且 UI 空闲后自动打开一次；已弹过/已就绪/检测异常/
        # 运行中不弹；可关闭稍后处理，不自动发起下载。
        self._runtime_prompted_once = False
        self._runtime_dialog_open = False
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
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)
        self._root_layout = root
        self.setCentralWidget(central)
        self.setStyleSheet(QSS)

        icon = constants.logo_ico_path()
        if icon:
            self.setWindowIcon(QtGui.QIcon(icon))

        # ---- 紧凑品牌条 ----
        self._build_brand_bar(root)

        # ---- 紧凑设备/上下文条：设备（一级）+ 设备▾（二级） ----
        self._build_tool_strip(root)

        # ---- 工作台：QSplitter(任务面板 | 运行面板)（弹性主体） ----
        self.work_split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.work_split.setObjectName("workSplit")
        self.work_split.setChildrenCollapsible(False)
        self._build_task_card()      # 左：固定头 + 任务滚动 + 固定选择工具栏
        self._build_control_card()   # 右：选中计数 + 组件摘要 + 开始/停止/状态/进度
        root.addWidget(self.work_split, 1)
        # 初始左右比例：任务约 62% / 运行约 38%（由相对数值分配）
        self.work_split.setStretchFactor(0, 1)
        self.work_split.setStretchFactor(1, 0)
        self.work_split.setSizes([620, 380])

        # ---- 日志抽屉（默认折叠为状态条，复用现有折叠逻辑） ----
        self._build_log_card()

        self.statusBar().showMessage("就绪")
        base_font = self.font()
        if base_font.pointSize() < 9:
            base_font.setPointSize(9)
            self.setFont(base_font)
        self._populate_tasks()
        self._refresh_version()
        # 后台检测运行组件（不阻塞首屏）：结果写日志/状态条；若初次检测
        # 为非就绪，UI 空闲后自动打开一次下载中心（带顶部警告）。
        QtCore.QTimer.singleShot(
            300, lambda: self._refresh_runtime_status(auto_prompt=True))

    def _build_brand_bar(self, root: QtWidgets.QVBoxLayout) -> None:
        """顶栏（52–56px 级）：小 logo + 产品名 + 轻量版标签；右侧 ADB 状态 + 设置。

        不再使用大青绿横幅/副标题；ADB 状态以点 + 文本呈现（克制、非胶囊大块）。
        """
        bar = QtWidgets.QFrame()
        bar.setObjectName("brandBar")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)
        png = constants.logo_png_path()
        icon_label = QtWidgets.QLabel()
        icon_label.setFixedSize(32, 32)
        if png:
            pm = _rounded_pixmap(png, 32)
            if not pm.isNull():
                icon_label.setPixmap(pm)
        lay.addWidget(icon_label)
        name = QtWidgets.QLabel("Coin11 助手")
        name.setObjectName("brandName")
        lay.addWidget(name)
        badge = QtWidgets.QLabel("轻量版")
        badge.setObjectName("brandBadge")
        lay.addWidget(badge)
        lay.addStretch(1)
        self.conn_dot = QtWidgets.QLabel("●")
        self.conn_dot.setObjectName("connDot")
        self.conn_pill = QtWidgets.QLabel()
        self.conn_pill.setObjectName("connPill")
        lay.addWidget(self.conn_dot)
        lay.addWidget(self.conn_pill)
        settings_btn = QtWidgets.QPushButton("设置")
        settings_btn.setObjectName("settingsBtn")
        settings_btn.setToolTip("设置与关于")
        settings_btn.clicked.connect(self._show_settings)
        lay.addWidget(settings_btn)
        root.addWidget(bar)
        self._set_connection_pill("正在检测…", ok=False)

    def _build_tool_strip(self, root: QtWidgets.QVBoxLayout) -> None:
        """上下文条：设备下拉 + 刷新；`设备` 为菜单入口（维护动作在设置对话框）。"""
        strip = QtWidgets.QFrame()
        strip.setObjectName("toolStrip")
        row = QtWidgets.QHBoxLayout(strip)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(8)
        lab = QtWidgets.QLabel("设备")
        lab.setObjectName("fieldLabel")
        row.addWidget(lab)
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setMinimumWidth(220)
        self.device_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.refresh_btn = QtWidgets.QPushButton("刷新")
        self.refresh_btn.setToolTip("重新检测已连接设备")
        self.refresh_btn.clicked.connect(
            lambda: self._refresh_device_state())
        row.addWidget(self.device_combo, 1)
        row.addWidget(self.refresh_btn)

        # 缺运行组件时的轻提示（主行内小提示，不遮挡）
        self.runtime_hint = QtWidgets.QLabel("")
        self.runtime_hint.setVisible(False)
        row.addSpacing(4)
        row.addWidget(self.runtime_hint)

        row.addStretch(1)
        self._build_device_menu_button(row)
        root.addWidget(strip)

    def _build_device_menu_button(self, row: QtWidgets.QHBoxLayout) -> None:
        """设备 ▾ 二级菜单：刷新设备 / 重新接管 ADB / 设备连接说明。"""
        self.device_menu_btn = QtWidgets.QPushButton("设备")
        self.device_menu_btn.setObjectName("menuBtn")
        menu = QtWidgets.QMenu(self.device_menu_btn)
        act_refresh = menu.addAction("刷新设备")
        act_refresh.triggered.connect(lambda: self._refresh_device_state())
        act_takeover = menu.addAction("重新接管 ADB")
        act_takeover.triggered.connect(self._on_takeover_adb)
        act_help = menu.addAction("设备连接说明")
        act_help.triggered.connect(self._on_device_help)
        self.device_menu_btn.setMenu(menu)
        self.device_menu_btn.setToolTip("刷新设备 / 重新接管 ADB / 设备连接说明")
        row.addWidget(self.device_menu_btn)

    def _set_connection_pill(self, text: str, ok: bool = True) -> None:
        self.conn_dot.setText("●")
        self.conn_dot.setObjectName("connDotOk" if ok else "connDot")
        style = self.conn_dot.style()
        style.unpolish(self.conn_dot)
        style.polish(self.conn_dot)
        self.conn_pill.setText(text)

    def _build_task_card(self) -> None:
        """左面板：标题“任务”+已选计数；顶部 segmented（日常任务/限时活动）
        切换；任务滚动区为弹性主体；底部固定选择工具栏（独立布局，不与列表重叠）。

        布局层次（P1 修复延续）：滚动区（占位 1，弹性）与底部工具栏是
        面板 body 中两个顺序独立的条目——工具栏绝不会进入滚动容器，也
        不会被滚动内容顶出视口。
        """
        self.task_card = Card("任务")
        self.task_count_label = QtWidgets.QLabel("已选 0 个")
        self.task_count_label.setObjectName("cardHint")
        head = self.task_card.head_layout
        if head is not None:
            # 标题后紧跟已选计数（位于 stretch 之前，靠左紧贴标题）
            head.insertWidget(1, self.task_count_label)

        # ---- 日常 / 限时 segmented 切换（替代原底部“显示限时活动”） ----
        seg_host = QtWidgets.QFrame()
        seg_host.setObjectName("segSwitch")
        seg_lay = QtWidgets.QHBoxLayout(seg_host)
        seg_lay.setContentsMargins(2, 2, 2, 2)
        seg_lay.setSpacing(2)
        self.daily_tab_btn = QtWidgets.QPushButton("日常任务")
        self.limited_tab_btn = QtWidgets.QPushButton("限时活动")
        for b in (self.daily_tab_btn, self.limited_tab_btn):
            b.setObjectName("segBtn")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.daily_tab_btn.setChecked(True)
        self.daily_tab_btn.clicked.connect(lambda: self._show_task_tab(False))
        self.limited_tab_btn.clicked.connect(lambda: self._show_task_tab(True))
        seg_lay.addWidget(self.daily_tab_btn)
        seg_lay.addWidget(self.limited_tab_btn)
        self.task_card.body.addWidget(seg_host)
        self._seg_host = seg_host

        # 滚动容器（任务区弹性主体）
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("taskScroll")
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMinimumHeight(90)
        scroll.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                             QtWidgets.QSizePolicy.Policy.Expanding)
        inner = QtWidgets.QWidget()
        inner.setObjectName("taskScrollInner")
        inner.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                            QtWidgets.QSizePolicy.Policy.Expanding)
        self.tasks_layout = QtWidgets.QVBoxLayout(inner)
        self.tasks_layout.setContentsMargins(2, 2, 6, 2)
        self.tasks_layout.setSpacing(5)
        # 关键：widgetResizable + 内容自然高度。绝不设置会把内容排到视口外
        # 的 sizeHint 覆盖；超出由滚动条接管，工具栏在滚动容器之外。
        scroll.setWidget(inner)
        self.task_card.body.addWidget(scroll, 1)   # 弹性主体：优先伸缩
        self.task_scroll = scroll

        # 底部选择工具栏：与滚动区同一面板、但为独立 layout 层级（固定不滚动）
        self.task_toolbar = QtWidgets.QFrame()
        self.task_toolbar.setObjectName("toolStrip")
        sel_row = QtWidgets.QHBoxLayout(self.task_toolbar)
        sel_row.setContentsMargins(0, 4, 0, 0)
        sel_row.setSpacing(6)
        self.select_all_btn = QtWidgets.QPushButton("全选日常任务")
        self.clear_all_btn = QtWidgets.QPushButton("取消选择")
        self.select_all_btn.setObjectName("link")
        self.clear_all_btn.setObjectName("link")
        self.select_all_btn.clicked.connect(lambda: self._set_all(True))
        self.clear_all_btn.clicked.connect(lambda: self._set_all(False))
        sel_row.addWidget(self.select_all_btn)
        sel_row.addWidget(self.clear_all_btn)
        sel_row.addStretch(1)
        self.task_card.body.addWidget(self.task_toolbar)   # 结构上独立于滚动区
        self.work_split.addWidget(self.task_card)

    def _build_control_card(self) -> None:
        """运行面板：标题“本次执行”；设备/选中/组件三项摘要；
        开始 = 唯一全宽实心 CTA；停止 = 次级危险；状态与进度成组。
        内容置于可滚动容器：窄窗上下布局时即便高度不足也可滚出完整可操作区。"""
        card = Card(title="本次执行")
        self.control_card = card
        # 内容放进垂直滚动区（widgetResizable），保证窄/矮窗口下全部可见可操作
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("ctrlScroll")
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        inner.setObjectName("ctrlScrollInner")
        col = QtWidgets.QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        scroll.setWidget(inner)
        card.body.addWidget(scroll)
        # ---- 三项摘要：设备 / 已选任务 / 运行组件（紧凑行，缺件行提供下载链接） ----
        self.device_summary_label = QtWidgets.QLabel("设备：未选择")
        self.device_summary_label.setObjectName("cardHint")
        self.sel_count_label = QtWidgets.QLabel("已选任务：0 个")
        self.sel_count_label.setObjectName("cardHint")
        col.addWidget(self.device_summary_label)
        col.addWidget(self.sel_count_label)
        comp = QtWidgets.QFrame()
        comp.setObjectName("taskRow")   # 复用浅色底小卡样式
        comp_lay = QtWidgets.QHBoxLayout(comp)
        comp_lay.setContentsMargins(10, 6, 10, 6)
        comp_lay.setSpacing(8)
        self.run_comp_summary = QtWidgets.QLabel("运行组件：就绪")
        self.run_comp_summary.setObjectName("cardHint")
        self.run_comp_open_btn = QtWidgets.QPushButton("下载")
        self.run_comp_open_btn.setObjectName("link")
        self.run_comp_open_btn.setToolTip("打开下载中心补齐运行组件")
        self.run_comp_open_btn.clicked.connect(self._on_open_download_center)
        comp_lay.addWidget(self.run_comp_summary, 1)
        comp_lay.addWidget(self.run_comp_open_btn)
        col.addWidget(comp)
        col.addSpacing(4)
        # ---- 开始（唯一实心主 CTA，占满可用宽度） ----
        self.run_btn = QtWidgets.QPushButton("开始运行")
        self.run_btn.setObjectName("primary")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.clicked.connect(self._on_start_run)
        col.addWidget(self.run_btn)
        self.stop_btn = QtWidgets.QPushButton("停止运行")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop_run)
        col.addWidget(self.stop_btn)
        col.addSpacing(4)
        # ---- 状态与进度成组（未运行时无大空白） ----
        state_row = QtWidgets.QHBoxLayout()
        state_row.setSpacing(8)
        self.status_pill = QtWidgets.QLabel("空闲")
        self.status_pill.setObjectName("statusPillIdle")
        self.run_state_label = QtWidgets.QLabel("未开始")
        self.run_state_label.setObjectName("fieldLabel")
        self.elapsed_label = QtWidgets.QLabel("")
        self.elapsed_label.setObjectName("fieldLabel")
        state_row.addWidget(self.status_pill)
        state_row.addWidget(self.run_state_label, 1)
        state_row.addWidget(self.elapsed_label)
        col.addLayout(state_row)
        self.progress_text = QtWidgets.QLabel("0/0")
        self.progress_text.setObjectName("fieldLabel")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        col.addWidget(self.progress_text)
        col.addWidget(self.progress)
        # 运行面板内容固定高度（滚动容器内），不往下拉大空白
        self.work_split.addWidget(card)

    def _build_log_card(self) -> None:
        """日志抽屉：默认折叠为简短状态条（高度≤48px，显示最近日志），
        展开后显示完整实时日志（视图最高 260px）与操作。"""
        card = Card()
        self.log_card = card
        # 紧凑外边距：折叠态整卡高度约 40–44px（≤48px）
        log_lay = card.layout()
        log_lay.setContentsMargins(12, 5, 12, 6)
        card.body.setSpacing(4)
        # 头部：标题 + 折叠状态条 + 展开/收起
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        title = QtWidgets.QLabel("实时日志")
        title.setObjectName("cardTitle")
        self.log_summary = QtWidgets.QLabel("尚未产生日志")
        self.log_summary.setObjectName("logSummary")
        self.log_summary.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                       QtWidgets.QSizePolicy.Policy.Preferred)
        self.log_toggle_btn = QtWidgets.QPushButton("展开日志")
        self.log_toggle_btn.setObjectName("link")
        self.log_toggle_btn.setToolTip("展开查看完整实时日志（自动滚动 / "
                                       "仅看错误 / 复制 / 打开目录）")
        self.log_toggle_btn.clicked.connect(self._toggle_log_expanded)
        header.addWidget(title)
        header.addWidget(self.log_summary, 1)
        header.addWidget(self.log_toggle_btn)
        card.add_layout(header)

        # 展开态：选项行（自动滚动 / 仅看错误 / 复制 / 打开目录）
        self.log_opts = QtWidgets.QWidget()
        bar = QtWidgets.QHBoxLayout(self.log_opts)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        self.auto_scroll_check = QtWidgets.QCheckBox("自动滚动")
        self.auto_scroll_check.setObjectName("logOpt")
        self.auto_scroll_check.setChecked(True)
        self.auto_scroll_check.toggled.connect(
            lambda on: setattr(self, "_log_auto_scroll", on))
        self.errors_only_check = QtWidgets.QCheckBox("仅看错误")
        self.errors_only_check.setObjectName("logOpt")
        self.errors_only_check.toggled.connect(self._toggle_errors_only)
        self.copy_btn = QtWidgets.QPushButton("复制诊断文本")
        self.open_log_btn = QtWidgets.QPushButton("打开日志目录")
        self.copy_btn.setObjectName("link")
        self.open_log_btn.setObjectName("link")
        self.copy_btn.clicked.connect(self._on_copy_diag)
        self.open_log_btn.clicked.connect(self._on_open_log_dir)
        bar.addWidget(self.auto_scroll_check)
        bar.addWidget(self.errors_only_check)
        bar.addStretch(1)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.open_log_btn)
        self.log_opts.setVisible(False)
        card.add(self.log_opts)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setMinimumHeight(80)
        self.log_view.setMaximumHeight(260)
        self.log_view.setVisible(False)
        card.add(self.log_view)
        self._root_layout.addWidget(card)
        self._set_log_expanded(False)

    # ------------------------------------------------------- 日志折叠
    def _toggle_log_expanded(self):
        self._set_log_expanded(not self._log_expanded)

    def _set_log_expanded(self, expanded: bool):
        self._log_expanded = bool(expanded)
        self.log_opts.setVisible(expanded)
        self.log_view.setVisible(expanded)
        self.log_summary.setVisible(not expanded)
        self.log_toggle_btn.setText("收起日志" if expanded else "展开日志")
        if expanded and self._log_auto_scroll:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ------------------------------------------------------------- 任务列表
    def _clear_task_containers(self):
        # 两个分组容器（日常 / 限时）都挂在滚动内容布局中，各自持独立行
        for holder_name in ("_daily_holder", "_limited_holder"):
            holder = getattr(self, holder_name, None)
            if holder is not None:
                holder.setParent(None)
                holder.deleteLater()
            setattr(self, holder_name, None)
        self._daily_rows = []
        self._limited_rows = []
        self._task_rows.clear()
        # 滚动布局内的 section/分组清空由容器重建代替：滚动布局本身只保留两个容器
        while self.tasks_layout.count():
            item = self.tasks_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _add_task_row(self, task: dict, layout) -> TaskRow:
        row = TaskRow(task, on_toggle=self._on_task_toggled)
        layout.addWidget(row)
        self._task_rows[task["id"]] = row
        return row

    def _add_section_label(self, layout, text: str) -> None:
        lab = QtWidgets.QLabel(text)
        lab.setObjectName("sectionTitle")
        layout.addWidget(lab)

    def _make_group_container(self) -> QtWidgets.QWidget:
        holder = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(holder)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(5)
        return holder

    def _populate_tasks(self):
        self._clear_task_containers()
        last_ids = set(self.settings.last_task_ids)

        def _fill(tasks: list, rows_out: list, is_limited: bool):
            holder = self._make_group_container()
            v = holder.layout()
            self._add_section_label(
                v, "限时活动（可能已过期，页面可能已变更）" if is_limited
                else "日常任务")
            for task in tasks:
                row = self._add_task_row(task, v)
                rows_out.append(row)
                if task.get("exists"):
                    row.set_runnable(True)
                    row.mark_state("limited" if is_limited else "ready")
                else:
                    row.set_runnable(False,
                                     task.get("unavailable_reason", ""))
                tid = task["id"]
                if tid in last_ids and row.enabled:
                    row.check.setChecked(True)
            return holder

        # 两个分组容器按顺序进滚动布局；显示由 segmented tab 控制
        self._daily_holder = _fill(self.catalog.daily_tasks(),
                                   self._daily_rows, False)
        self.tasks_layout.addWidget(self._daily_holder)
        self._limited_holder = _fill(self.catalog.limited_tasks(),
                                     self._limited_rows, True)
        self.tasks_layout.addWidget(self._limited_holder)
        self._set_task_tab(False, quiet=True)

    # ------------------------------------------------- segmented 切换
    def _show_task_tab(self, limited: bool):
        self._set_task_tab(limited, quiet=False)

    def _set_task_tab(self, limited: bool, quiet: bool = False):
        """显示限时活动(True)/日常任务(False)分组，并同步选择工具栏文案。"""
        self._active_tab = "limited" if limited else "daily"
        if self._daily_holder is not None:
            self._daily_holder.setVisible(not limited)
        if self._limited_holder is not None:
            self._limited_holder.setVisible(limited)
        if hasattr(self, "select_all_btn"):
            self.select_all_btn.setText(
                "全选限时活动" if limited else "全选日常任务")
        if not quiet and hasattr(self, "daily_tab_btn"):
            self.daily_tab_btn.setChecked(not limited)
            self.limited_tab_btn.setChecked(limited)
        self._sync_task_count_label()

    def _on_task_toggled(self, task: dict, on: bool):
        # 持久化勾选
        ids = self._selected_task_ids()
        self.settings.set("last_task_ids", ids)
        self._update_idle_selection()
        self._sync_task_count_label()

    def _selected_task_ids(self):
        return [tid for tid, row in self._task_rows.items()
                if row.checked]

    def _active_rows(self):
        """当前 tab 的任务行（供全选/取消作用于可见分组）。"""
        if self._active_tab == "limited":
            return self._limited_rows
        return self._daily_rows

    def _set_all(self, on: bool):
        rows = self._active_rows()
        for row in rows:
            if row.enabled:
                row.check.setChecked(on)
        self.settings.set("last_task_ids", self._selected_task_ids())
        self._update_idle_selection()
        self._sync_task_count_label()

    def _sync_task_count_label(self):
        n = len(self._selected_task_ids())
        if hasattr(self, "task_count_label"):
            self.task_count_label.setText(f"已选 {n} 个")
        if hasattr(self, "sel_count_label"):
            if self._running:
                return
            self.sel_count_label.setText(f"已选任务：{n} 个")

    def _update_idle_selection(self):
        """空闲时在运行面板给出选中计数反馈（不改变任何运行语义）。"""
        if self._running:
            return
        n = len(self._selected_task_ids())
        self.run_state_label.setText(
            f"未开始 · 已选 {n} 个任务" if n else "未开始 · 请选择任务")
        if hasattr(self, "sel_count_label"):
            self.sel_count_label.setText(f"已选任务：{n} 个")

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
        self._update_device_summary()
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

    def _set_busy(self, btn: QtWidgets.QPushButton, busy: bool):
        btn.setEnabled(not busy)
        btn.setText("检测中…" if busy else "刷新")

    def _update_device_summary(self):
        if not hasattr(self, "device_summary_label"):
            return
        ready = [d for d in self.devices if d.is_device]
        if ready:
            dev = self.devices[0]
            model = f"（{dev.model}）" if dev.model else ""
            self.device_summary_label.setText(
                f"设备：{dev.serial}{model}")
        elif self.devices:
            self.device_summary_label.setText(
                f"设备：{self.devices[0].serial}（{self.devices[0].state}）")
        else:
            self.device_summary_label.setText("设备：未选择")

    def _show_guide_4steps(self, hint: str):
        """首次无 ADB：给出可操作的四步提示（写入日志，菜单可弹说明）。"""
        self._log_line("[提示] 未找到内置 ADB 程序。")
        self._log_line("[操作] 请确认发行目录含 platform-tools\\adb.exe，或重新安装本应用。")
        self._log_line("[操作] 若要手动修复：1) 重新安装本应用；"
                       "2) 确认杀毒软件未隔离 adb.exe；"
                       "3) 以管理员身份重新运行一次；4) 重启应用。")

    def _on_device_help(self):
        """设备 ▾ > 设备连接说明：USB 调试四步 + 常见状态处理。"""
        QtWidgets.QMessageBox.information(
            self, "设备连接说明",
            "1. 手机进入 设置 → 关于手机，连续点击“版本号”7 次开启开发者选项；\n"
            "2. 回到 设置 → 开发者选项，打开“USB 调试”；\n"
            "3. 用数据线连接电脑与手机，在手机上允许 USB 调试；\n"
            "4. 在本应用点“刷新设备”检测。\n\n"
            "提示：若显示 unauthorized，解锁手机并在授权弹窗勾选“始终允许”，\n"
            "再点刷新；若 offline，重插数据线并关闭其它占用 ADB 的工具。\n\n"
            "需要重置 ADB 服务时，请使用 设备 → 重新接管 ADB。")

    def _refresh_version(self):
        status = self.update.status()
        cur = status["current_commit"]
        prev = status["previous_commit"]
        if cur:
            self._log_line(
                f"[脚本版本] 当前 {cur[:12]}（更新于 {status['current_updated']}）"
                + (f"；上一版 {prev[:12]}" if prev else ""))
        self._refresh_tasks()

    def _refresh_tasks(self):
        """脚本根目录变化后重建任务列表（保持勾选状态尽量一致）。"""
        self.catalog.set_root_dir(self._script_root())
        self._populate_tasks()
        self._log_line("[提示] 任务可用状态已按当前脚本目录刷新。")

    def _script_root(self):
        return self.update.current_dir

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

    # ---------------------------------------------------- 运行组件 / 维护
    def _on_refresh_runtime_status(self):
        # 手动“重新检测组件”（设置对话框动作）不触发缺件自动弹窗
        self._refresh_runtime_status(auto_prompt=False)

    def _refresh_runtime_status(self, background: bool = True,
                                auto_prompt: bool = False):
        """后台线程全量检测运行组件；UI 状态回到 GUI 线程（不再占用大卡）。

        auto_prompt=True：仅当这是进程内初次后台完整检测、且结果为“非就绪”
        时，UI 空闲后自动打开一次 RuntimeDownloadDialog（带顶部缺件警告）。
        """
        self.statusBar().showMessage("正在重新检测运行组件…")
        rt_dir = resolve_data_runtime_dir(self.settings)

        def _do():
            ok, message, missing = ensure_runtime_ready(rt_dir)
            self._on_ui(lambda: self._apply_runtime_status(
                ok, message, missing, auto_prompt=auto_prompt))

        if background:
            threading.Thread(target=_do, daemon=True).start()
        else:
            _do()

    def _apply_runtime_status(self, ok: bool, message: str, missing: list,
                              auto_prompt: bool = False):
        self._runtime_ready = ok
        self._runtime_missing = list(missing)
        if ok:
            self.runtime_hint.setText("")
            self.runtime_hint.setVisible(False)
            self.statusBar().showMessage("运行组件就绪", 4000)
            if hasattr(self, "run_comp_summary"):
                self.run_comp_summary.setText("运行组件：就绪")
                self.run_comp_summary.setStyleSheet("color: #14662b;")
        else:
            self.runtime_hint.setText("⚠ 缺少运行组件")
            self.runtime_hint.setObjectName("stateTagErr")
            self.runtime_hint.setToolTip(
                "任务运行需要内置 Python 运行时与依赖组件。\n"
                "请打开 设置 → 运行组件 → 下载中心 下载必需组件。")
            style = self.runtime_hint.style()
            style.unpolish(self.runtime_hint)
            style.polish(self.runtime_hint)
            self.runtime_hint.setVisible(True)
            self.statusBar().showMessage("缺少运行组件，请到下载中心补齐", 6000)
            if hasattr(self, "run_comp_summary"):
                self.run_comp_summary.setText("运行组件：缺少必需组件")
                self.run_comp_summary.setStyleSheet("color: #a33a2f;")
            # 初次后台完整检测为非就绪：UI 空闲后自动打开一次下载中心。
            # 检测异常（missing 为空）不弹；运行中不弹；同一缺件状态不连续
            # 弹多个窗口（_runtime_prompted_once 保证每个进程仅一次）。
            if auto_prompt and not self._runtime_prompted_once \
                    and not self._runtime_dialog_open \
                    and not getattr(self, "_running", False) \
                    and self._runtime_missing:
                self._runtime_prompted_once = True
                self._open_download_center_auto()
        # 缺组件时任务仍可勾选，但开始运行会被拦截并引导下载中心
        self._log_line(f"[运行时] {message}")

    def _open_download_center_auto(self):
        """UI 空闲后自动打开下载中心（带缺件警告；不强制下载/不自动请求）。"""
        QtCore.QTimer.singleShot(0, self._open_download_dialog_auto)

    def _open_download_dialog_auto(self):
        if getattr(self, "_runtime_dialog_open", False) \
                or getattr(self, "_running", False):
            return
        self._runtime_dialog_open = True

        def _closed():
            self._runtime_dialog_open = False

        dlg = RuntimeDownloadDialog(self.settings, parent=self,
                                    warn_missing=True)
        try:
            dlg.finished.connect(lambda _r: _closed())
            dlg.exec()
        finally:
            self._runtime_dialog_open = False
            self._refresh_runtime_status()

    def _on_show_runtime_dir(self):
        rt = resolve_data_runtime_dir(self.settings)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("运行时目录")
        box.setIcon(QtWidgets.QMessageBox.Icon.Information)
        box.setText(
            "任务脚本运行组件的安装目录（下载中心安装/修复）：\n\n"
            f"{rt}\n\n"
            "可在“下载中心 → 更改目录”中调整；程序不在此目录外的位置下载任何组件。")
        box.addButton("打开目录", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        close_btn = box.addButton("关闭", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not close_btn:
            os.makedirs(rt, exist_ok=True)
            try:
                if sys.platform == "win32":
                    os.startfile(rt)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", rt])
            except OSError as exc:
                self._log_line(f"[错误] 无法打开运行时目录：{exc}")

    def _on_open_download_center(self):
        if getattr(self, "_running", False):
            QtWidgets.QMessageBox.information(
                self, "正在运行", "任务运行中请先停止，再打开下载中心。")
            return
        dlg = RuntimeDownloadDialog(self.settings, parent=self)
        dlg.exec()
        self._refresh_runtime_status()

    # ------------------------------------------------------------ 同步/回退
    def _on_sync(self):
        if self._running:
            QtWidgets.QMessageBox.information(
                self, "正在运行", "任务运行中不能同步脚本，请先停止。")
            return
        if hasattr(self, "setting_sync_btn"):
            self.setting_sync_btn.setEnabled(False)
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
            self._on_ui(self._settings_sync_finished)

    def _on_restore(self):
        if self._running:
            QtWidgets.QMessageBox.information(
                self, "正在运行", "任务运行中不能回退脚本，请先停止。")
            return
        if hasattr(self, "setting_restore_btn"):
            self.setting_restore_btn.setEnabled(False)
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
            self._on_ui(self._settings_sync_finished)

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
        self._update_idle_selection()
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
        # 折叠态：状态条显示最近一行（截断为单行）
        flat = " ".join(str(text).splitlines())
        if len(flat) > 110:
            flat = flat[:107] + "…"
        self.log_summary.setText(flat)
        if self._log_expanded and self._log_auto_scroll:
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

    # ------------------------------------------------------------ 设置/维护
    def _show_settings(self):
        """“设置与维护”对话框：运行组件 / 脚本更新 / 诊断 + 自动接管 ADB。

        原工具条“维护 ▾”菜单的全部动作迁入此处（SPEC 1），按
        运行组件（下载中心、重新检测组件、打开运行时目录）、脚本更新（同步
        最新脚本、恢复上一版）、诊断（打开日志目录、复制诊断文本）分组；
        动作全部复用既有 handler，运行中禁用规则保持。
        """
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("设置与维护")
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setSpacing(8)
        # ---- 运行组件 ----
        comp_group = QtWidgets.QGroupBox("运行组件")
        comp_form = QtWidgets.QVBoxLayout(comp_group)
        self.setting_download_btn = QtWidgets.QPushButton("打开下载中心…")
        self.setting_download_btn.setToolTip(
            "下载 / 安装 / 修复任务运行必需组件（依赖锁定、来源官方）")
        self.setting_download_btn.clicked.connect(self._on_open_download_center)
        self.setting_recheck_btn = QtWidgets.QPushButton("重新检测组件")
        self.setting_recheck_btn.setToolTip("后台全量检测运行组件是否就绪")
        self.setting_recheck_btn.clicked.connect(self._on_refresh_runtime_status)
        self.setting_runtime_dir_btn = QtWidgets.QPushButton("打开运行时目录…")
        self.setting_runtime_dir_btn.clicked.connect(self._on_show_runtime_dir)
        for b in (self.setting_download_btn, self.setting_recheck_btn,
                  self.setting_runtime_dir_btn):
            comp_form.addWidget(b)
        lay.addWidget(comp_group)

        # ---- 脚本更新 ----
        upd_group = QtWidgets.QGroupBox("脚本更新")
        upd_form = QtWidgets.QVBoxLayout(upd_group)
        self.setting_sync_btn = QtWidgets.QPushButton("同步最新脚本")
        self.setting_sync_btn.clicked.connect(self._on_sync)
        self.setting_restore_btn = QtWidgets.QPushButton("恢复上一版")
        self.setting_restore_btn.clicked.connect(self._on_restore)
        upd_form.addWidget(self.setting_sync_btn)
        upd_form.addWidget(self.setting_restore_btn)
        lay.addWidget(upd_group)

        # ---- 诊断 ----
        diag_group = QtWidgets.QGroupBox("诊断")
        diag_form = QtWidgets.QVBoxLayout(diag_group)
        self.setting_log_dir_btn = QtWidgets.QPushButton("打开日志目录…")
        self.setting_log_dir_btn.clicked.connect(self._on_open_log_dir)
        self.setting_copy_diag_btn = QtWidgets.QPushButton("复制诊断文本")
        self.setting_copy_diag_btn.clicked.connect(self._on_copy_diag)
        diag_form.addWidget(self.setting_log_dir_btn)
        diag_form.addWidget(self.setting_copy_diag_btn)
        lay.addWidget(diag_group)

        # ---- 通用设置 ----
        auto_group = QtWidgets.QGroupBox("通用")
        auto_form = QtWidgets.QVBoxLayout(auto_group)
        self.auto_check = QtWidgets.QCheckBox("运行前自动接管 ADB（默认开启）")
        self.auto_check.setChecked(self.settings.auto_takeover_adb)
        self.auto_check.toggled.connect(
            lambda on: self.settings.set("auto_takeover_adb", on))
        auto_form.addWidget(self.auto_check)
        lay.addWidget(auto_group)

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
        # 运行中禁用规则：任务运行期间脚本更新动作不可用（与原菜单语义一致）
        running = bool(self._running)
        self.setting_sync_btn.setEnabled(not running)
        self.setting_restore_btn.setEnabled(not running)
        if running:
            self.setting_sync_btn.setToolTip("任务运行中不能同步脚本，请先停止")
            self.setting_restore_btn.setToolTip("任务运行中不能回退脚本，请先停止")
        self._setting_dialog = dlg

        def _refresh_dialog():
            # 同步/回退完成后恢复可用（worker 内通过 _on_ui 调度）
            if hasattr(self, "setting_sync_btn"):
                self.setting_sync_btn.setEnabled(True)
            if hasattr(self, "setting_restore_btn"):
                self.setting_restore_btn.setEnabled(True)

        self._setting_refresh = _refresh_dialog

        def _dialog_closed(_r):
            # 对话框关闭后按钮对象将被销毁：断开回调，避免 worker 碰已删对象
            self._setting_dialog = None
            self._setting_refresh = None

        dlg.finished.connect(_dialog_closed)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn, 0, alignment=QtCore.Qt.AlignRight)
        dlg.resize(620, 720)
        dlg.exec()

    def _settings_sync_finished(self):
        """设置对话框内同步/回退完成后恢复按钮（worker 经 _on_ui 调用）。"""
        fn = getattr(self, "_setting_refresh", None)
        if fn is not None:
            fn()

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

    # ------------------------------------------------------------ 自适应切向
    def resizeEvent(self, event):
        super().resizeEvent(event)
        split = getattr(self, "work_split", None)
        if split is None:
            return
        narrow = event.size().width() < 980
        desired = (QtCore.Qt.Orientation.Vertical if narrow
                   else QtCore.Qt.Orientation.Horizontal)
        if split.orientation() != desired:
            split.setOrientation(desired)
            if narrow:
                # 上下布局：任务面板至少 260px（规格 7）；运行面板内容自带滚动，
                # 可在剩余空间内完整可操作，不产生水平滚动。
                self.task_card.setMinimumHeight(260)
                split.setStretchFactor(0, 1)
                split.setStretchFactor(1, 0)
                split.setSizes([260, max(0, split.height() - 260)])
            else:
                self.task_card.setMinimumHeight(0)
                split.setStretchFactor(0, 1)
                split.setStretchFactor(1, 0)
                split.setSizes([620, 380])

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
