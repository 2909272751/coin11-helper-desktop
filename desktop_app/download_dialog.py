"""运行组件下载中心（RuntimeDownloadDialog）：表格列出组件与状态，后台安装。

需求落地（SPEC 3/4/5）：
- 独立窗口，从主界面明显入口打开；复用 app.QSS 浅色主题。
- 表格每行 = 一个组件：名称 / 用途 / 预估大小 / 状态 / 操作（下载、重试、
  取消）+ 阶段进度显示（下载 / 安装 / 校验 / 模型 x/y）。
- 总操作：下载必需组件（补齐缺失）、下载全部（全量重装/修复）、停止、关闭。
- 安装全程在后台线程执行，界面不卡死；取消/超时/失败原因/重试；安装后
  校验；重启后重新检测（打开即 fast 检测）。
- 运行时目录可改（选择可写目录并持久化到 settings），默认
  %LOCALAPPDATA%\\Coin11Helper\\runtime。

进度显示策略（SPEC“优先阶段进度，pip 无可靠总量不伪造百分比”）：
- pip 组：进度列显示当前阶段文本（安装中 / 校验中…），不显示伪造百分比；
- 模型组：下载时显示“下载中 (1/2)”，整体进度 = 已就绪组件数 / 总数。
"""
from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from PySide6 import QtCore, QtWidgets

from .runtime_components import (
    COMPONENT_BY_ID,
    COMPONENT_PYTHON_BOOTSTRAP,
    GROUP_LABELS,
    INSTALL_ORDER,
    PIP_SOURCE_CHOICES,
    SETTINGS_PIP_SOURCE_KEY,
    SOURCE_SMART,
    STATE_CANCELLED,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_IDLE,
    STATE_INSTALLING,
    STATE_OK,
    STATE_PENDING,
    STATE_VERIFYING,
    pip_source_mode_label,
)
from .runtime_manager import (
    ComponentInstaller,
    default_user_runtime_dir,
    load_component_states,
    resolve_data_runtime_dir,
    runtime_data_python,
    save_component_states,
    verify_component,
)

# 状态 -> 中文标签 / 颜色对象名（对应 app.QSS 的 stateTag*）
_STATE_TEXT = {
    STATE_IDLE: "未安装",
    STATE_PENDING: "等待中",
    STATE_DOWNLOADING: "下载中",
    STATE_INSTALLING: "安装中",
    STATE_VERIFYING: "校验中",
    STATE_OK: "已就绪",
    STATE_FAILED: "失败",
    STATE_CANCELLED: "已取消",
}
_STATE_TAG = {
    STATE_IDLE: "stateTagIdle",
    STATE_PENDING: "stateTagIdle",
    STATE_DOWNLOADING: "stateTagRun",
    STATE_INSTALLING: "stateTagRun",
    STATE_VERIFYING: "stateTagRun",
    STATE_OK: "stateTagOk",
    STATE_FAILED: "stateTagErr",
    STATE_CANCELLED: "stateTagCancel",
}

# pip 组显示顺序 = INSTALL_ORDER 中 pip/model；python-bootstrap 置顶只读展示
_DISPLAY_ORDER = (COMPONENT_PYTHON_BOOTSTRAP.id,) + tuple(INSTALL_ORDER)

# 组件前置依赖（安装某组件前必须已就绪的组件；模型组依赖 easyocr 所在 ocr 组）
_DEPENDENCIES = {
    "ocr": ("torch",),            # easyocr Requires torch：先装锁定 torch 防 PyPI 抢装
    "automation": (),
    "torch": (),
    "easyocr-models": ("ocr",),
}


def _expand_with_deps(ids) -> list:
    """把请求的组件扩展为含前置依赖、按 INSTALL_ORDER 排序的去重序列。"""
    needed = set(ids)
    changed = True
    while changed:
        changed = False
        for cid in list(needed):
            for dep in _DEPENDENCIES.get(cid, ()):
                if dep not in needed:
                    needed.add(dep)
                    changed = True
    return [c for c in INSTALL_ORDER if c in needed]


def _fmt_mb(mb: float) -> str:
    if mb <= 0:
        return "—"
    if mb >= 1024:
        return f"约 {mb / 1024:.1f} GB"
    return f"约 {mb:.0f} MB"


class _Signals(QtCore.QObject):
    """后台安装线程 -> GUI 线程的排队信号。"""

    log_line = QtCore.Signal(str)
    state_changed = QtCore.Signal(str, str)      # component_id, state
    progress = QtCore.Signal(str, int)           # component_id, percent(0-100 或 -1)
    all_done = QtCore.Signal(bool, str)          # success?, summary


class RuntimeDownloadDialog(QtWidgets.QDialog):
    """运行组件下载中心窗口。"""

    def __init__(self, settings, parent=None, warn_missing=False):
        super().__init__(parent)
        self.settings = settings
        self.warn_missing = bool(warn_missing)
        self.runtime_dir = resolve_data_runtime_dir(settings)
        self._signals = _Signals(self)
        self._signals.log_line.connect(self._append_log)
        self._signals.state_changed.connect(self._on_state_changed)
        self._signals.progress.connect(self._on_progress)
        self._signals.all_done.connect(self._on_all_done)
        # 安装状态（当前进程）：行状态 & 记录
        self._row_states: Dict[str, str] = {}
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._busy = False
        self.setWindowTitle("运行组件下载中心")
        self.setMinimumSize(880, 600)
        self.resize(980, 640)
        self._build_ui()
        self._refresh_fast_status()
        self._update_buttons()
        self._render_dir()
        # 完整运行时离线版（发行根内置 runtime\python）：无需下载中心
        self._check_bundled_full()

    def _check_bundled_full(self):
        from .runtime_manager import complete_bundled_runtime_present
        if not complete_bundled_runtime_present():
            return
        for btn in (self.install_required_btn, self.install_all_btn,
                    self.choose_dir_btn, self.reset_dir_btn):
            btn.setEnabled(False)
        self._append_log("[提示] 当前为完整运行时离线版：全部运行组件已随发行内置"
                         "（runtime\\python），无需在线下载。轻量版才需要在此下载。")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        from .app import QSS
        self.setStyleSheet(QSS)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        # 标题与说明
        head = QtWidgets.QLabel(
            "下载并安装运行必需组件（依赖锁定、来源官方、校验后启用）")
        head.setObjectName("cardTitle")
        lay.addWidget(head)
        hint = QtWidgets.QLabel(
            "任务脚本依赖 Python 运行时与以下组件，未装齐时不会启动任务。"
            "下载全程在后台进行，可随时停止；文件先落临时目录，校验成功后原子启用，"
            "损坏/不完整的下载不会被当作可用。")
        hint.setObjectName("cardHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # 顶部警告（缺件自动打开时显示；可关闭稍后处理，不强制下载）
        self.warn_banner = QtWidgets.QFrame()
        self.warn_banner.setObjectName("stateTagErr")
        wrow = QtWidgets.QHBoxLayout(self.warn_banner)
        wrow.setContentsMargins(10, 6, 10, 6)
        wrow.setSpacing(8)
        warn_text = QtWidgets.QLabel(
            "⚠ 运行组件未完整安装，任务无法开始；请先下载必需组件，"
            "或关闭本窗口稍后处理。")
        warn_text.setObjectName("cardHint")
        warn_text.setWordWrap(True)
        warn_text.setStyleSheet("color: #a33a2f; font-weight: 600;")
        self.warn_close_btn = QtWidgets.QPushButton("知道了")
        self.warn_close_btn.setObjectName("link")
        self.warn_close_btn.clicked.connect(
            lambda: self.warn_banner.setVisible(False))
        wrow.addWidget(warn_text, 1)
        wrow.addWidget(self.warn_close_btn)
        self.warn_banner.setVisible(self.warn_missing)
        lay.addWidget(self.warn_banner)

        # 紧凑“下载源”选择（持久化到 SettingsStore；固定四项，无自定义 URL）
        src_row = QtWidgets.QHBoxLayout()
        src_lab = QtWidgets.QLabel("下载源：")
        src_lab.setObjectName("fieldLabel")
        src_row.addWidget(src_lab)
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToContents)
        current = str(self.settings.get(SETTINGS_PIP_SOURCE_KEY, SOURCE_SMART))
        idx = 0
        for i, (mode, text) in enumerate(PIP_SOURCE_CHOICES):
            self.source_combo.addItem(text, mode)
            if mode == current:
                idx = i
        self.source_combo.setCurrentIndex(idx)
        self.source_combo.setToolTip(
            "pip 依赖下载源：智能 = 清华→阿里→官方顺序重试；"
            "手动源优先所选，失败再试其余，官方始终最后。PyTorch CPU wheel"
            " 恒用官方索引（download.pytorch.org/whl/cpu）。")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        src_row.addWidget(self.source_combo)
        src_row.addStretch(1)
        mirror_hint = QtWidgets.QLabel(
            "国内镜像仅覆盖 pip 依赖；PyTorch CPU 与 EasyOCR 模型保持官方来源。")
        mirror_hint.setObjectName("cardHint")
        src_row.addWidget(mirror_hint)
        lay.addLayout(src_row)

        # 运行时目录
        dir_row = QtWidgets.QHBoxLayout()
        self.dir_label = QtWidgets.QLabel()
        self.dir_label.setObjectName("fieldLabel")
        dir_row.addWidget(self.dir_label)
        dir_row.addStretch(1)
        self.choose_dir_btn = QtWidgets.QPushButton("更改目录…")
        self.reset_dir_btn = QtWidgets.QPushButton("恢复默认位置")
        self.choose_dir_btn.setObjectName("link")
        self.reset_dir_btn.setObjectName("link")
        self.choose_dir_btn.clicked.connect(self._on_choose_dir)
        self.reset_dir_btn.clicked.connect(self._on_reset_dir)
        dir_row.addWidget(self.choose_dir_btn)
        dir_row.addWidget(self.reset_dir_btn)
        lay.addLayout(dir_row)

        # 组件表格
        table = QtWidgets.QTableWidget()
        table.setObjectName("runtimeTable")
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(
            ["组件", "用途", "大小", "状态 / 进度", "操作"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(
            4, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        table.verticalHeader().setDefaultSectionSize(44)
        lay.addWidget(table, 1)
        self.table = table
        self._row_items: Dict[str, list] = {}
        self._populate_table()

        # 整体计数进度
        self.summary_label = QtWidgets.QLabel()
        self.summary_label.setObjectName("cardHint")
        lay.addWidget(self.summary_label)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(18)
        lay.addWidget(self.progress_bar)

        # 日志（只读）
        log_head = QtWidgets.QHBoxLayout()
        log_title = QtWidgets.QLabel("下载日志（失败原因与来源地址）")
        log_title.setObjectName("cardHint")
        log_head.addWidget(log_title)
        log_head.addStretch(1)
        self.copy_log_btn = QtWidgets.QPushButton("复制诊断文本")
        self.copy_log_btn.setObjectName("link")
        self.copy_log_btn.setToolTip("复制下方完整日志（含失败组件与其官方来源地址）")
        self.copy_log_btn.clicked.connect(self._on_copy_diag)
        log_head.addWidget(self.copy_log_btn)
        lay.addLayout(log_head)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setMinimumHeight(120)
        lay.addWidget(self.log_view)

        # 总操作
        ops = QtWidgets.QHBoxLayout()
        self.install_required_btn = QtWidgets.QPushButton("下载必需组件")
        self.install_required_btn.setObjectName("primary")
        self.install_all_btn = QtWidgets.QPushButton("下载全部")
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setObjectName("danger")
        self.close_btn = QtWidgets.QPushButton("关闭")
        self.install_required_btn.setToolTip("按依赖顺序补齐所有未就绪的必需组件")
        self.install_all_btn.setToolTip("重新安装全部组件（用于修复损坏状态）")
        self.install_required_btn.clicked.connect(
            lambda: self._start_install(required_only=True))
        self.install_all_btn.clicked.connect(
            lambda: self._start_install(required_only=False))
        self.stop_btn.clicked.connect(self._on_stop)
        self.close_btn.clicked.connect(self.accept)
        ops.addWidget(self.install_required_btn)
        ops.addWidget(self.install_all_btn)
        ops.addStretch(1)
        ops.addWidget(self.stop_btn)
        ops.addWidget(self.close_btn)
        lay.addLayout(ops)

    # ------------------------------------------------- 下载源 / 诊断
    def _on_source_changed(self, _index: int):
        """下载源选择变更：持久化到 SettingsStore（幂等；不触发网络请求）。"""
        mode = self.source_combo.currentData()
        if not mode:
            return
        self.settings.set(SETTINGS_PIP_SOURCE_KEY, mode)
        self._append_log(f"[设置] 下载源已切换为："
                         f"{pip_source_mode_label(mode)}。")

    def _on_copy_diag(self):
        text = self.log_view.toPlainText()
        QtWidgets.QApplication.clipboard().setText(text)
        self._append_log("[操作] 诊断文本已复制。")

    def _populate_table(self):
        self.table.setRowCount(len(_DISPLAY_ORDER))
        for row, cid in enumerate(_DISPLAY_ORDER):
            comp = COMPONENT_BY_ID.get(cid)
            if comp is None:
                continue
            # 列 0：名称 + 分组
            name_w = QtWidgets.QWidget()
            name_lay = QtWidgets.QVBoxLayout(name_w)
            name_lay.setContentsMargins(6, 2, 6, 2)
            name_lay.setSpacing(0)
            n = QtWidgets.QLabel(comp.name)
            n.setObjectName("taskDesc" if False else "cardTitle")
            n.setStyleSheet("font-size: 13px; font-weight: 600;")
            g = QtWidgets.QLabel(GROUP_LABELS.get(comp.group, comp.group))
            g.setObjectName("cardHint")
            g.setStyleSheet("font-size: 11px;")
            name_lay.addWidget(n)
            name_lay.addWidget(g)
            self.table.setCellWidget(row, 0, name_w)
            # 列 1：用途
            purpose = QtWidgets.QLabel(comp.purpose)
            purpose.setWordWrap(True)
            purpose.setStyleSheet("font-size: 12px; color: #5b6b68;")
            self.table.setCellWidget(row, 1, purpose)
            # 列 2：大小
            size_l = QtWidgets.QLabel(_fmt_mb(comp.size_mb))
            size_l.setStyleSheet("font-size: 12px;")
            size_l.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(row, 2, size_l)
            # 列 3：状态标签 + 进度文本
            st_w = QtWidgets.QWidget()
            st_lay = QtWidgets.QVBoxLayout(st_w)
            st_lay.setContentsMargins(6, 2, 6, 2)
            st_lay.setSpacing(0)
            tag = QtWidgets.QLabel("")
            tag.setObjectName("stateTagIdle")
            tag.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            detail = QtWidgets.QLabel("")
            detail.setObjectName("cardHint")
            detail.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            detail.setStyleSheet("font-size: 11px;")
            st_lay.addWidget(tag)
            st_lay.addWidget(detail)
            self.table.setCellWidget(row, 3, st_w)
            # 列 4：操作按钮
            btn_w = QtWidgets.QWidget()
            btn_lay = QtWidgets.QHBoxLayout(btn_w)
            btn_lay.setContentsMargins(4, 0, 4, 0)
            action = QtWidgets.QPushButton("下载")
            action.setObjectName("link")
            action.setFixedWidth(84)
            # 只连接一次；行内动作由 _on_action_clicked 依据当前状态分发
            action.clicked.connect(
                lambda _=False, c=cid: self._on_action_clicked(c))
            btn_lay.addWidget(action)
            btn_lay.addStretch(1)
            self.table.setCellWidget(row, 4, btn_w)
            self._row_items[cid] = [name_w, purpose, size_l, st_w, tag,
                                    detail, action]

    def _render_dir(self):
        text = f"运行时目录：{self.runtime_dir}"
        if not self._dir_writable(self.runtime_dir):
            text += "（当前目录不可写，请更改）"
        self.dir_label.setText(text)

    @staticmethod
    def _dir_writable(path: str) -> bool:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write-probe")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.unlink(probe)
            return True
        except OSError:
            return False

    def _on_choose_dir(self):
        if self._busy:
            self._append_log("[提示] 安装进行中，请先停止再更改目录。")
            return
        start = self.runtime_dir if os.path.isdir(self.runtime_dir) else ""
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择运行组件目录（须可写）", start)
        if not chosen:
            return
        if not self._dir_writable(chosen):
            QtWidgets.QMessageBox.warning(
                self, "目录不可写",
                "所选目录不可写（可能位于只读位置或被占用）。\n"
                "请选择本地磁盘上的可写文件夹，例如 D:\\Coin11Helper\\runtime。")
            return
        self._set_runtime_dir(chosen)

    def _on_reset_dir(self):
        if self._busy:
            self._append_log("[提示] 安装进行中，请先停止再更改目录。")
            return
        self._set_runtime_dir(default_user_runtime_dir())

    def _set_runtime_dir(self, path: str):
        self.runtime_dir = os.path.abspath(path)
        self.settings.set("runtime_dir", self.runtime_dir)
        # 同步进程级覆盖，使 runtime_python_exe/easyocr_model_dir 读取同一目录
        from .runtime_manager import ENV_DATA_RUNTIME
        os.environ[ENV_DATA_RUNTIME] = self.runtime_dir
        self._render_dir()
        self._refresh_fast_status()

    # ----------------------------------------------------------- 状态刷新
    def _refresh_fast_status(self):
        """打开/换目录时静态检测（不跑 import 探针，避免长时间卡 UI）。"""
        saved = load_component_states(self.runtime_dir)
        states = {}
        for cid in _DISPLAY_ORDER:
            comp = COMPONENT_BY_ID.get(cid)
            if comp is None:
                continue
            if comp.kind == "python":
                ok = bool(runtime_data_python(self.runtime_dir))
                states[cid] = STATE_OK if ok else STATE_IDLE
                continue
            ok, _ = verify_component(self.runtime_dir, cid, fast=True)
            states[cid] = STATE_OK if ok else STATE_IDLE
        # 保留持久化失败/取消标记用于“重试”按钮（若静态检测仍未就绪）
        for cid, st in saved.items():
            if st in (STATE_FAILED, STATE_CANCELLED) and states.get(cid) != STATE_OK:
                states[cid] = st
        self._row_states = states
        self._apply_rows()
        self._update_summary()

    def _apply_rows(self):
        for cid, state in self._row_states.items():
            items = self._row_items.get(cid)
            if not items:
                continue
            tag, detail, action = items[4], items[5], items[6]
            comp = COMPONENT_BY_ID.get(cid)
            text = _STATE_TEXT.get(state, state)
            tag.setText(text)
            tag.setObjectName(_STATE_TAG.get(state, "stateTagIdle"))
            style = tag.style()
            style.unpolish(tag)
            style.polish(tag)
            if state == STATE_OK:
                detail.setText("")
                action.setEnabled(False)
                action.setText("已就绪")
                action.setObjectName("link")
            elif comp is not None and comp.kind == "python":
                detail.setText("用于安装依赖（内置，自动使用）")
                action.setEnabled(False)
                action.setText("内置")
                action.setObjectName("link")
            elif state in (STATE_DOWNLOADING, STATE_INSTALLING,
                           STATE_VERIFYING):
                action.setEnabled(True)
                action.setText("取消")
                action.setObjectName("danger")
            elif state in (STATE_FAILED, STATE_CANCELLED):
                action.setEnabled(True)
                action.setText("重试")
                action.setObjectName("link")
                detail.setText("点击“重试”重新安装")
            else:
                action.setEnabled(True)
                action.setText("下载")
                action.setObjectName("link")
                detail.setText("")

    def _on_action_clicked(self, cid: str):
        """行内按钮统一入口：根据当前行状态决定动作。"""
        if self._busy:
            self._append_log("[提示] 已有安装任务进行中，请先停止。")
            return
        state = self._row_states.get(cid)
        if state in (STATE_FAILED, STATE_CANCELLED, STATE_IDLE,
                     STATE_PENDING, STATE_OK):
            # 未就绪 -> 下载/重试（会自动跳过已就绪项）
            self._start_install(required_only=True, only=[cid])
        elif state in (STATE_DOWNLOADING, STATE_INSTALLING, STATE_VERIFYING):
            self._on_stop()

    def _update_summary(self):
        comps = [c for c in _DISPLAY_ORDER
                 if c != COMPONENT_PYTHON_BOOTSTRAP.id]
        ok = sum(1 for c in comps
                 if self._row_states.get(c) == STATE_OK)
        total = len(comps)
        need_mb = sum(c.size_mb for c in COMPONENT_BY_ID.values()
                      if c.kind in ("pip", "model"))
        self.summary_label.setText(
            f"已就绪 {ok}/{total} 个组件 · 全部就绪约需下载 {_fmt_mb(need_mb)}")
        self.progress_bar.setValue(int(ok * 100 / total) if total else 0)
        self.progress_bar.setFormat(f"总体 {ok}/{total}")

    # ----------------------------------------------------------- 动作
    def _start_install(self, required_only: bool, only=None):
        """按依赖安全顺序启动后台安装。

        required_only=True：只装未就绪（默认“必需”=全部四类，因全部为运行必需）。
        only：显式指定要装的组件（行内下载/重试），自动补全其前置依赖。
        """
        if self._busy:
            self._append_log("[提示] 已有安装任务进行中，请先停止。")
            return
        self._stop = threading.Event()
        if only:
            order = _expand_with_deps(only)
        else:
            order = list(INSTALL_ORDER)
        if required_only:
            # 只装未就绪；全部四类都是运行必需，故直接过滤
            order = [c for c in order
                     if self._row_states.get(c) != STATE_OK]
        if not order:
            self._append_log("[提示] 没有需要安装的组件（全部已就绪）。")
            return
        # 首次：确保数据运行时解释器先就绪（复制内置基座）
        self._append_log("[检查] 确认数据运行时 Python 基座 …")
        for cid in order:
            self._row_states[cid] = STATE_PENDING
            self._save_state(cid, STATE_PENDING)
        self._apply_rows()
        self._set_busy(True)
        self._append_log("[开始] 安装队列：" + " → ".join(
            COMPONENT_BY_ID[c].name for c in order) + "。")
        worker = _InstallWorker(self, self.runtime_dir, order,
                                source_mode=str(self.source_combo.currentData()
                                                or SOURCE_SMART))
        self._worker = worker
        worker.start()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.install_required_btn.setEnabled(not busy)
        self.install_all_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.choose_dir_btn.setEnabled(not busy)
        self.reset_dir_btn.setEnabled(not busy)

    def _update_buttons(self):
        # 打开/状态变化时刷新总操作可用性（幂等）
        self.stop_btn.setEnabled(self._busy)

    def _on_stop(self):
        if self._busy:
            self._append_log("[操作] 正在停止安装…")
            self._stop.set()
            if self._worker:
                self._worker.cancel()

    # ----------------------------------------------------- 后台线程回调
    def _on_state_changed(self, cid: str, state: str):
        self._row_states[cid] = state
        self._save_state(cid, state)
        self._apply_rows()
        self._update_summary()

    def _on_progress(self, cid: str, _pct: int):
        # pip 阶段不显示伪造百分比；模型下载百分比用于行内 detail（可选）
        items = self._row_items.get(cid)
        if items:
            items[5].setText(f"{_pct}%" if _pct >= 0 else "进行中…")

    def _on_all_done(self, success: bool, summary: str):
        self._append_log(summary)
        self._set_busy(False)
        self._worker = None
        self._refresh_fast_status()
        self._update_buttons()

    def _save_state(self, cid: str, state: str):
        saved = load_component_states(self.runtime_dir)
        saved[cid] = state
        save_component_states(self.runtime_dir, saved)

    def _append_log(self, text: str):
        from .logutil import redact
        self.log_view.appendPlainText(redact(text))

    # ---- worker 用（后台线程调用 Qt 信号）----
    def sig_log(self, text: str):
        self._signals.log_line.emit(text)

    def sig_state(self, cid: str, state: str):
        self._signals.state_changed.emit(cid, state)

    def sig_progress(self, cid: str, pct: int):
        self._signals.progress.emit(cid, pct)

    def sig_done(self, success: bool, summary: str):
        self._signals.all_done.emit(success, summary)


class _InstallWorker(threading.Thread):
    """后台安装线程：逐组件调用 ComponentInstaller（可取消）。"""

    def __init__(self, dialog: RuntimeDownloadDialog, runtime_dir: str,
                 order, source_mode: str):
        super().__init__(daemon=True)
        self.dialog = dialog
        self.runtime_dir = runtime_dir
        self.order = order
        self.source_mode = source_mode
        self.installer: Optional[ComponentInstaller] = None

    def run(self):
        # 0) 确保数据运行时解释器就绪（首次复制内置基座；失败则整体中止）
        from .runtime_manager import copy_bootstrap_to, runtime_data_python
        if not runtime_data_python(self.runtime_dir):
            try:
                copy_bootstrap_to(self.runtime_dir,
                                  on_log=self.dialog.sig_log)
                self.dialog.sig_state(COMPONENT_PYTHON_BOOTSTRAP.id, STATE_OK)
            except Exception as exc:  # noqa: BLE001
                self.dialog.sig_log(f"[错误] 数据运行时 Python 基座准备失败：{exc}")
                self.dialog.sig_done(False, "[结果] 基座准备失败，无法继续安装。")
                return
        ok_all = True
        results = []
        for cid in self.order:
            if self.dialog._stop.is_set():
                self.dialog.sig_state(cid, STATE_CANCELLED)
                ok_all = False
                break
            installer = ComponentInstaller(
                self.runtime_dir,
                stop_event=self.dialog._stop,
                on_log=self.dialog.sig_log,
                on_state=self.dialog.sig_state,
                on_progress=self.dialog.sig_progress,
                source_mode=self.source_mode)
            self.installer = installer
            try:
                ok = installer.install_component(cid)
            except Exception as exc:  # noqa: BLE001
                ok = False
                self.dialog.sig_log(f"[错误] {exc}")
            results.append((cid, ok))
            if not ok:
                ok_all = False
                if self.dialog._stop.is_set():
                    break
        if self.dialog._stop.is_set():
            self.dialog.sig_done(False, "[结果] 安装已停止（未完成组件可稍后重试）。")
        else:
            failed = [cid for cid, ok in results if not ok]
            if failed:
                names = "、".join(COMPONENT_BY_ID[c].name for c in failed)
                self.dialog.sig_done(False, f"[结果] 以下组件失败/未完成：{names}。"
                                           f"点击对应“重试”可重新安装。")
            else:
                self.dialog.sig_done(True, "[结果] 全部组件下载、安装与校验完成。")

    def cancel(self):
        if self.installer:
            self.installer.cancel()
