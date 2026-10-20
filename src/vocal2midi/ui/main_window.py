"""主窗口。

布局结构（自上而下）：
    顶栏      应用名 + 环境徽标 + 主题切换 + 设置菜单
    文件卡片  拖放区 / 浏览按钮 / 最近使用
    参数卡片  标签页：分离设置 / 转写参数（滑块 + 输入框）/ 自定义参数
    操作区    开始转换 / 取消 + 进度条 + 状态
    结果卡片  输出文件链接 + 打开目录 + 日志折叠

所有业务逻辑都通过 core.* 调用，界面本身不含算法。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_TITLE, __version__, paths
from ..core import environment, params as param_registry, separation, settings as settings_mod
from ..core.pipeline import AUDIO_EXTS, PipelineOptions
from ..core.transcription import TranscriptionParams
from .deps_dialog import DependencyDialog
from .theme import THEME_DARK, THEME_LIGHT, THEME_SYSTEM, ThemeManager
from .widgets import Card, ChoiceRow, ParamRow, make_path_label, scroll_page
from .worker import PipelineWorker

log = logging.getLogger("vocal2midi.window")

THEME_LABELS = {"system": "跟随系统", "light": "浅色", "dark": "深色"}
AUDIO_FILTER = "音频文件 (" + " ".join("*" + ext for ext in AUDIO_EXTS) + ");;所有文件 (*.*)"


class DropLineEdit(QLineEdit):
    """支持拖放音频文件的路径输入框。"""

    fileDropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText("拖入音频文件，或点击右侧「浏览…」")
        self.setClearButtonEnabled(True)

    # ---- 拖放 -----------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).suffix.lower() in AUDIO_EXTS:
                self.setText(path)
                self.fileDropped.emit(path)
                event.acceptProposedAction()
                return
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, settings: settings_mod.Settings | None = None) -> None:
        super().__init__()
        self.settings = settings or settings_mod.Settings.load()
        self.report = environment.inspect()
        self.worker: PipelineWorker | None = None
        self.params_rows: dict[str, ParamRow] = {}
        self.choice_rows: dict[str, ChoiceRow] = {}
        self._last_result = None

        self.theme = ThemeManager(QApplication.instance(), self)
        self.theme.apply(self.settings.theme)

        self.setWindowTitle(APP_TITLE)
        self.resize(self.settings.window_width, self.settings.window_height)
        self.setMinimumSize(880, 640)
        self.setAcceptDrops(True)

        self._build_ui()
        self._load_settings_into_ui()
        self._apply_report()
        self._update_convert_enabled()
        QTimer.singleShot(0, self._check_environment_on_start)

    # ==================================================================
    # 界面构建
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_file_card())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_separation_tab(), "人声分离")
        self.tabs.addTab(self._build_transcription_tab(), "转写参数")
        self.tabs.addTab(self._build_custom_tab(), "自定义参数")
        outer.addWidget(self.tabs, 1)

        outer.addWidget(self._build_action_bar())
        outer.addWidget(self._build_result_card())

    # ---- 顶栏 -----------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("vocal2midi")
        title.setObjectName("Title")
        subtitle = QLabel(f"人声分离 · 旋律转 MIDI    v{__version__}")
        subtitle.setObjectName("Subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        row.addLayout(titles)
        row.addStretch(1)

        self.env_badge = QLabel("检测中…")
        self.env_badge.setObjectName("Status")
        row.addWidget(self.env_badge)

        self.theme_combo = QComboBox()
        for key, label in THEME_LABELS.items():
            self.theme_combo.addItem(label, key)
        self.theme_combo.setFixedWidth(110)
        self.theme_combo.setToolTip("切换界面主题；「跟随系统」会自动响应 Windows 深浅色设置")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        row.addWidget(self.theme_combo)

        self.menu_button = QPushButton("⋯")
        self.menu_button.setObjectName("MenuDots")
        self.menu_button.setFixedWidth(38)
        self.menu_button.setToolTip("更多操作")
        self.menu_button.setMenu(self._build_menu())
        row.addWidget(self.menu_button)
        return header

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        act_env = QAction("重新检测环境", self)
        act_env.triggered.connect(self._recheck_environment)
        menu.addAction(act_env)

        act_deps = QAction("安装 / 修复依赖…", self)
        act_deps.triggered.connect(self._open_dependency_dialog)
        menu.addAction(act_deps)

        act_weights = QAction("下载分离模型权重…", self)
        act_weights.triggered.connect(self._download_weights)
        menu.addAction(act_weights)

        menu.addSeparator()

        act_out = QAction("打开输出目录", self)
        act_out.triggered.connect(self._open_output_dir)
        menu.addAction(act_out)

        act_log = QAction("打开日志目录", self)
        act_log.triggered.connect(lambda: self._open_path(paths.user_data_dir()))
        menu.addAction(act_log)

        menu.addSeparator()

        act_about = QAction("关于 vocal2midi", self)
        act_about.triggered.connect(self._show_about)
        menu.addAction(act_about)
        return menu

    # ---- 文件卡片 -------------------------------------------------------
    def _build_file_card(self) -> QWidget:
        card = Card("音频文件", "支持 mp3 / wav / flac / m4a / ogg 等格式，可直接把文件拖进输入框")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.file_edit = DropLineEdit()
        self.file_edit.fileDropped.connect(lambda _p: self._update_convert_enabled())
        self.file_edit.textChanged.connect(lambda _t: self._update_convert_enabled())
        row.addWidget(self.file_edit, 1)

        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_file)
        row.addWidget(browse)

        recent = QPushButton("最近")
        recent.setToolTip("选择最近使用过的文件")
        recent_menu = QMenu(recent)
        self.recent_menu = recent_menu
        recent_menu.aboutToShow.connect(self._fill_recent_menu)
        recent.setMenu(recent_menu)
        row.addWidget(recent)
        card.body.addLayout(row)

        self.drop_hint = QLabel("把音频文件拖到窗口任意位置也可以")
        self.drop_hint.setObjectName("Faint")
        card.body.addWidget(self.drop_hint)
        return card

    # ---- 分离标签页 -----------------------------------------------------
    def _build_separation_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 10, 2, 2)
        layout.setSpacing(12)

        grid_card = Card("分离设置", "分离指把歌曲拆成人声 / 伴奏等音轨，只有人声轨会继续转 MIDI")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("分离模型"), 0, 0)
        self.model_combo = QComboBox()
        for model in separation.AVAILABLE_MODELS:
            self.model_combo.addItem(model, model)
        self.model_combo.setToolTip("htdemucs_ft 质量最好但速度最慢；mdx_extra_q 体积最小")
        grid.addWidget(self.model_combo, 0, 1)

        grid.addWidget(QLabel("计算设备"), 1, 0)
        self.device_combo = QComboBox()
        self.device_combo.addItem("自动选择", "auto")
        self.device_combo.addItem("CPU", "cpu")
        self.device_combo.addItem("CUDA（NVIDIA 显卡）", "cuda")
        grid.addWidget(self.device_combo, 1, 1)

        grid.addWidget(QLabel("输出目录"), 2, 0)
        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(str(paths.default_output_dir()))
        out_row.addWidget(self.output_edit, 1)
        out_browse = QPushButton("更改…")
        out_browse.clicked.connect(self._browse_output_dir)
        out_row.addWidget(out_browse)
        grid.addLayout(out_row, 2, 1)
        grid.setColumnStretch(1, 1)
        grid_card.body.addLayout(grid)
        layout.addWidget(grid_card)

        options_card = Card("运行选项")
        self.vocals_only_check = QCheckBox("输入文件已经是人声轨（跳过分离）")
        self.vocals_only_check.setToolTip("跳过 Demucs，直接把输入当人声转 MIDI，速度快很多")
        options_card.body.addWidget(self.vocals_only_check)
        self.overwrite_check = QCheckBox("强制重新分离（忽略已缓存的人声 wav）")
        options_card.body.addWidget(self.overwrite_check)
        self.csv_check = QCheckBox("同时导出音符 CSV（便于核对错音）")
        options_card.body.addWidget(self.csv_check)
        layout.addWidget(options_card)
        layout.addStretch(1)
        return scroll_page(page)

    # ---- 转写参数标签页 -------------------------------------------------
    def _build_transcription_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 10, 2, 2)
        layout.setSpacing(10)

        hint = QLabel("拖动滑块快速调整，也可以直接在右侧输入框键入精确数值（回车生效）")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        card = Card("转写参数", "这些参数直接决定识别的灵敏度、音域与 MIDI 输出格式")
        reset = QPushButton("恢复默认")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self._reset_params)
        card.add_header_widget(reset)

        for spec in param_registry.iter_specs():
            if spec.choices:
                row = ChoiceRow(spec)
                self.choice_rows[spec.key] = row
            else:
                row = ParamRow(spec)
                self.params_rows[spec.key] = row
            row.valueChanged.connect(self._on_param_changed)  # type: ignore[attr-defined]
            card.body.addWidget(row)
            card.body.addSpacing(6)
        layout.addWidget(card)
        layout.addStretch(1)
        return scroll_page(page)

    # ---- 自定义参数标签页 -----------------------------------------------
    def _build_custom_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 10, 2, 2)
        layout.setSpacing(10)

        card = Card(
            "自定义参数",
            "在这里补充或覆盖上面的参数，支持 key=value 或 --key value，可用空格 / 逗号 / 换行分隔。\n"
            "例如：onset_threshold=0.45 min_note_length_ms=60 quantize=1/16 transpose=-2",
        )
        self.custom_edit = QPlainTextEdit()
        self.custom_edit.setObjectName("Log")
        self.custom_edit.setPlaceholderText("onset_threshold=0.45 min_note_length_ms=60")
        self.custom_edit.setMinimumHeight(110)
        self.custom_edit.setMaximumHeight(200)
        self.custom_edit.textChanged.connect(self._validate_custom_params)
        card.body.addWidget(self.custom_edit)

        self.custom_status = QLabel("")
        self.custom_status.setObjectName("Faint")
        self.custom_status.setWordWrap(True)
        card.body.addWidget(self.custom_status)

        keys = "、".join(spec.key for spec in param_registry.iter_specs())
        available = QLabel(f"可用参数：{keys}")
        available.setObjectName("Faint")
        available.setWordWrap(True)
        card.body.addWidget(available)

        layout.addWidget(card)
        layout.addStretch(1)
        return scroll_page(page)

    # ---- 操作区 ---------------------------------------------------------
    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.convert_button = QPushButton("开始转换")
        self.convert_button.setObjectName("Primary")
        self.convert_button.setMinimumWidth(150)
        self.convert_button.clicked.connect(self._on_convert_clicked)
        row.addWidget(self.convert_button)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self.cancel_button)

        row.addStretch(1)
        self.status_label = QLabel("准备就绪")
        self.status_label.setObjectName("Status")
        row.addWidget(self.status_label)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        return bar

    # ---- 结果卡片 -------------------------------------------------------
    def _build_result_card(self) -> QWidget:
        card = Card("输出")

        self.log_toggle = QPushButton("显示日志")
        self.log_toggle.setObjectName("Ghost")
        self.log_toggle.setCheckable(True)
        self.log_toggle.toggled.connect(self._toggle_log)
        card.add_header_widget(self.log_toggle)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.result_label = QLabel("转换完成后这里会显示生成的文件")
        self.result_label.setObjectName("Muted")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.result_label.setOpenExternalLinks(True)
        row.addWidget(self.result_label, 1)

        self.open_dir_button = QPushButton("打开输出目录")
        self.open_dir_button.clicked.connect(self._open_output_dir)
        row.addWidget(self.open_dir_button)
        card.body.addLayout(row)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(110)
        self.log_view.setVisible(False)
        card.body.addWidget(self.log_view)

        self._result_card = card
        return card

    # ==================================================================
    # 设置读写
    # ==================================================================
    def _load_settings_into_ui(self) -> None:
        s = self.settings

        index = self.theme_combo.findData(s.theme)
        if index >= 0:
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(index)
            self.theme_combo.blockSignals(False)

        model_index = self.model_combo.findData(s.model)
        if model_index >= 0:
            self.model_combo.setCurrentIndex(model_index)
        device_index = self.device_combo.findData(s.device)
        if device_index >= 0:
            self.device_combo.setCurrentIndex(device_index)

        self.vocals_only_check.setChecked(s.vocals_only)
        self.overwrite_check.setChecked(s.overwrite)
        self.csv_check.setChecked(s.write_csv)
        self.output_edit.setText(s.output_dir)
        self.custom_edit.setPlainText(s.custom_params)

        stored = param_registry.normalize(s.params)
        for key, row in self.params_rows.items():
            row.set_value(stored.get(key, row.spec.default), emit=False)
        for key, row in self.choice_rows.items():
            row.set_value(stored.get(key, row.spec.default))
        self._validate_custom_params()

    def _collect_settings(self) -> settings_mod.Settings:
        s = self.settings
        s.theme = str(self.theme_combo.currentData())
        s.model = str(self.model_combo.currentData())
        s.device = str(self.device_combo.currentData())
        s.vocals_only = self.vocals_only_check.isChecked()
        s.overwrite = self.overwrite_check.isChecked()
        s.write_csv = self.csv_check.isChecked()
        s.output_dir = self.output_edit.text().strip()
        s.custom_params = self.custom_edit.toPlainText()

        payload: dict[str, object] = {}
        for key, row in self.params_rows.items():
            payload[key] = row.value
        for key, row in self.choice_rows.items():
            payload[key] = row.value
        s.params = payload

        s.window_width = max(880, self.width())
        s.window_height = max(640, self.height())
        return s

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "正在转换",
                "转换尚未完成，确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(3000)
        self._collect_settings().save()
        super().closeEvent(event)

    # ==================================================================
    # 环境
    # ==================================================================
    def _apply_report(self) -> None:
        report = self.report
        if report.ready:
            text = "环境就绪"
            style = "StatusOk"
        elif not report.deps_ready:
            text = "缺少依赖"
            style = "StatusError"
        else:
            text = "环境不完整"
            style = "StatusWarn"
        self.env_badge.setText(text)
        self.env_badge.setObjectName(style)
        self.env_badge.setToolTip(report.summary() + "\n" + "\n".join(report.issues()))
        self.env_badge.style().unpolish(self.env_badge)
        self.env_badge.style().polish(self.env_badge)

        if report.gpu_available:
            self.device_combo.setItemText(2, f"CUDA（{report.gpu_name or 'NVIDIA'}）")
        if report.weights_ready:
            self.drop_hint.setText("把音频文件拖到窗口任意位置也可以 · 分离模型已缓存")
        else:
            self.drop_hint.setText("把音频文件拖到窗口任意位置也可以 · 首次分离会下载约 400MB 模型")

    def _check_environment_on_start(self) -> None:
        if self.report.deps_ready:
            return
        missing = "、".join(label for _m, label in self.report.missing[:4]) or "核心依赖"
        reply = QMessageBox.question(
            self,
            "运行环境不完整",
            f"检测到缺少：{missing}\n\n是否现在打开依赖安装向导自动补齐？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._open_dependency_dialog()

    def _recheck_environment(self) -> None:
        self.report = environment.inspect()
        self._apply_report()
        QMessageBox.information(self, "环境检测", self.report.summary() + "\n\n" + "\n".join(self.report.issues() or ["一切正常"]))

    def _open_dependency_dialog(self) -> None:
        dialog = DependencyDialog(self, auto_start=True)
        dialog.exec()
        if dialog.succeeded:
            self.report = environment.inspect()
            self._apply_report()

    def _download_weights(self) -> None:
        from ..core import weights

        if weights.weights_ready():
            QMessageBox.information(self, "模型权重", f"权重已就绪：\n{weights.checkpoints_dir()}")
            return
        confirm = QMessageBox.question(
            self,
            "下载模型权重",
            "htdemucs_ft 权重约 400MB，将从官方源多连接下载。\n下载期间界面会保持响应，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.status_label.setText("正在下载模型权重…")
        self.progress.setValue(0)

        import threading

        def work() -> None:
            ok = weights.ensure_weights(
                on_progress=lambda frac, text: self._post(self._on_progress, frac, text),
                on_log=lambda text: self._post(self._append_log, text),
            )
            self._post(self._on_weights_done, ok)

        threading.Thread(target=work, daemon=True).start()

    def _on_weights_done(self, ok: bool) -> None:
        self.report = environment.inspect()
        self._apply_report()
        if ok:
            self.progress.setValue(1000)
            self.status_label.setText("模型权重已就绪")
        else:
            self.progress.setValue(0)
            self.status_label.setText("模型下载失败，可稍后重试")

    # ==================================================================
    # 交互
    # ==================================================================
    def _browse_file(self) -> None:
        start_dir = ""
        current = self.file_edit.text().strip()
        if current:
            start_dir = str(Path(current).parent)
        elif self.settings.recent_files:
            start_dir = str(Path(self.settings.recent_files[0]).parent)
        path, _ = QFileDialog.getOpenFileName(self, "选择音频文件", start_dir, AUDIO_FILTER)
        if path:
            self.file_edit.setText(path)
            self._remember(path)

    def _browse_output_dir(self) -> None:
        start = self.output_edit.text().strip() or str(self.settings.resolved_output_dir())
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if path:
            self.output_edit.setText(path)

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        recent = [p for p in self.settings.recent_files if Path(p).exists()]
        if not recent:
            action = self.recent_menu.addAction("（暂无记录）")
            action.setEnabled(False)
            return
        for item in recent[:8]:
            action = self.recent_menu.addAction(Path(item).name)
            action.setToolTip(item)
            action.triggered.connect(lambda _checked=False, p=item: self._use_recent(p))

    def _use_recent(self, path: str) -> None:
        self.file_edit.setText(path)
        self._remember(path)

    def _remember(self, path: str) -> None:
        self.settings.remember_file(path)
        self.settings.save()

    def _on_theme_changed(self) -> None:
        mode = str(self.theme_combo.currentData())
        self.theme.apply(mode)
        self.settings.theme = mode
        self.settings.save()

    def _on_param_changed(self, _key: str, _value: object) -> None:
        self._validate_custom_params()

    def _reset_params(self) -> None:
        for row in self.params_rows.values():
            row.set_value(row.spec.default, emit=False)
        for row in self.choice_rows.values():
            row.set_value(row.spec.default)
        self._validate_custom_params()
        self.status_label.setText("参数已恢复默认")

    def _validate_custom_params(self) -> None:
        result = param_registry.parse_custom_params(self.custom_edit.toPlainText() if hasattr(self, "custom_edit") else "")
        if not result.values and not result.errors and not result.unknown:
            self.custom_status.setText("")
            return
        message = result.message()
        self.custom_status.setText(message)
        self.custom_status.setObjectName("StatusError" if result.errors else "Faint")
        self.custom_status.style().unpolish(self.custom_status)
        self.custom_status.style().polish(self.custom_status)

    def _toggle_log(self, visible: bool) -> None:
        self.log_view.setVisible(visible)
        self.log_toggle.setText("隐藏日志" if visible else "显示日志")

    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _post(self, fn, *args) -> None:
        """把子线程里的界面更新排到主线程执行。"""
        QTimer.singleShot(0, lambda: fn(*args))

    def _update_convert_enabled(self) -> None:
        has_file = bool(self.file_edit.text().strip())
        running = bool(self.worker and self.worker.isRunning())
        self.convert_button.setEnabled(has_file and not running)

    # ==================================================================
    # 转换流程
    # ==================================================================
    def _current_params(self) -> TranscriptionParams:
        payload: dict[str, object] = {}
        for key, row in self.params_rows.items():
            payload[key] = row.value
        for key, row in self.choice_rows.items():
            payload[key] = row.value
        # 自定义参数覆盖上面的滑块值
        custom = param_registry.parse_custom_params(self.custom_edit.toPlainText())
        payload.update(custom.values)
        return TranscriptionParams.from_mapping(payload)

    def _on_convert_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        audio = self.file_edit.text().strip()
        if not audio:
            QMessageBox.warning(self, "缺少输入", "请先选择要转换的音频文件")
            return
        path = Path(audio)
        if not path.exists():
            QMessageBox.warning(self, "文件不存在", f"找不到文件：\n{audio}")
            return

        custom = param_registry.parse_custom_params(self.custom_edit.toPlainText())
        if custom.errors:
            QMessageBox.warning(self, "自定义参数有误", "；".join(custom.errors))
            return
        if custom.unknown:
            reply = QMessageBox.question(
                self,
                "存在未知参数",
                "以下参数无法识别，将被忽略：\n" + "、".join(custom.unknown) + "\n\n仍然继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if self.report.missing:
            QMessageBox.warning(
                self,
                "依赖缺失",
                "当前缺少运行依赖，请先在「⋯ → 安装 / 修复依赖」中补齐。",
            )
            return

        out_dir = Path(self.output_edit.text().strip()) if self.output_edit.text().strip() else paths.default_output_dir()
        options = PipelineOptions(
            audio_path=path,
            out_dir=out_dir,
            model=str(self.model_combo.currentData()),
            device=str(self.device_combo.currentData()),
            vocals_only=self.vocals_only_check.isChecked(),
            overwrite=self.overwrite_check.isChecked(),
            params=self._current_params(),
            write_csv=self.csv_check.isChecked(),
        )

        self._remember(str(path))
        self.log_view.clear()
        self.log_view.appendPlainText(f"输入：{path}")
        self.log_view.appendPlainText(f"参数：{param_registry.describe_params(options.params.as_mapping())}")
        self.progress.setValue(0)
        self._set_running(True)
        self.status_label.setText("准备开始…")

        self.worker = PipelineWorker(options, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.logged.connect(self._append_log)
        self.worker.finishedOk.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelledByUser.connect(self._on_cancelled)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _set_running(self, running: bool) -> None:
        self.convert_button.setEnabled(not running and bool(self.file_edit.text().strip()))
        self.cancel_button.setEnabled(running)
        for widget in (
            self.file_edit,
            self.model_combo,
            self.device_combo,
            self.output_edit,
            self.vocals_only_check,
            self.overwrite_check,
            self.csv_check,
            self.custom_edit,
            self.tabs,
        ):
            widget.setEnabled(not running)

    def _on_cancel_clicked(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status_label.setText("正在取消…")
            self.cancel_button.setEnabled(False)

    def _on_progress(self, fraction: float, text: str) -> None:
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        self.status_label.setText(text)

    def _on_finished(self, result) -> None:
        self._last_result = result
        self.progress.setValue(1000)
        self.status_label.setText(f"完成 · {result.note_count} 个音符 · 用时 {result.elapsed_s:.1f}s")
        links = []
        if result.midi_path:
            links.append(f'<a href="{Path(result.midi_path).as_uri()}">MIDI</a>')
        if result.csv_path:
            links.append(f'<a href="{Path(result.csv_path).as_uri()}">音符 CSV</a>')
        if result.vocals_path:
            links.append(f'<a href="{Path(result.vocals_path).as_uri()}">人声 WAV</a>')
        self.result_label.setText("生成文件：" + " · ".join(links) if links else "未生成文件")
        self._append_log(f"完成：{result.note_count} 个音符，用时 {result.elapsed_s:.1f}s")

    def _on_failed(self, message: str) -> None:
        self.progress.setValue(0)
        self.status_label.setText("转换失败")
        self._append_log("错误：" + message)
        self.log_view.setVisible(True)
        self.log_toggle.setChecked(True)
        QMessageBox.critical(self, "转换失败", message)

    def _on_cancelled(self) -> None:
        self.progress.setValue(0)
        self.status_label.setText("已取消")
        self._append_log("用户取消了转换")

    def _on_worker_finished(self) -> None:
        self._set_running(False)
        self._update_convert_enabled()

    # ==================================================================
    # 杂项
    # ==================================================================
    def _open_output_dir(self) -> None:
        target = Path(self.output_edit.text().strip()) if self.output_edit.text().strip() else paths.default_output_dir()
        if not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "无法打开", str(exc))
                return
        self._open_path(target)

    def _open_path(self, target: Path) -> None:
        if sys.platform.startswith("win"):
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
                return
            except OSError:
                pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _show_about(self) -> None:
        report = self.report
        QMessageBox.about(
            self,
            "关于 vocal2midi",
            f"<h3>vocal2midi {__version__}</h3>"
            "<p>人声分离（Demucs） + 人声转 MIDI（SwiftF0）</p>"
            f"<p>Python {report.python_version}<br>"
            f"torch {report.torch_version or '未安装'}"
            f"{'（CUDA ' + report.cuda_version + '）' if report.cuda_version else ''}<br>"
            f"{'GPU：' + report.gpu_name if report.gpu_available else '运行设备：CPU'}</p>"
            f"<p>设置文件：{paths.settings_file()}</p>",
        )

    # ---- 全局拖放 -------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).suffix.lower() in AUDIO_EXTS:
                self.file_edit.setText(path)
                self._remember(path)
                self._update_convert_enabled()
                event.acceptProposedAction()
                return
        event.ignore()
