"""依赖修复对话框（运行期缺少依赖 / 首次启动时使用）。

安装器负责首次安装；这个对话框是「装坏了 / 用户手工删了包 / 想换 CUDA 版本」
时的兜底入口，复用同一套 core.installer 逻辑。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import environment, installer

log = logging.getLogger("vocal2midi.deps_dialog")


class _Bridge(QObject):
    progress = Signal(float, str)
    logged = Signal(str)
    finished = Signal(bool, str)


class DependencyDialog(QDialog):
    """安装/修复依赖的进度对话框。"""

    def __init__(self, parent: QWidget | None = None, auto_start: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("依赖安装 · vocal2midi")
        self.setMinimumWidth(620)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._ok = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("正在准备运行环境")
        title.setObjectName("Title")
        layout.addWidget(title)

        report = environment.inspect()
        hint = QLabel(
            "将自动测试国内镜像速度并从最快的源下载依赖。\n" + " · ".join(report.issues() or ["依赖看起来已经齐全，可以重新校验"])
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("torch 版本"))
        self.flavor_combo = QComboBox()
        self.flavor_combo.addItem("自动检测（推荐）", "auto")
        self.flavor_combo.addItem("CPU 版（约 120MB）", "cpu")
        self.flavor_combo.addItem("CUDA 12.8 版（约 2.7GB）", "cu128")
        row.addWidget(self.flavor_combo, 1)
        layout.addLayout(row)

        self.force_check = QCheckBox("强制重新安装（忽略已安装的包）")
        layout.addWidget(self.force_check)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        layout.addWidget(self.bar)

        self.status = QLabel("等待开始")
        self.status.setObjectName("Status")
        layout.addWidget(self.status)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)
        layout.addWidget(self.log_view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.install_button = QPushButton("开始安装")
        self.install_button.setObjectName("Primary")
        self.install_button.clicked.connect(self.start)
        buttons.addWidget(self.install_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self._on_close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self._bridge = _Bridge(self)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.logged.connect(self._on_log)
        self._bridge.finished.connect(self._on_finished)

        if auto_start:
            self.start()

    # ---- 安装流程 -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._cancel.clear()
        self._ok = False
        self.install_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.flavor_combo.setEnabled(False)
        self.force_check.setEnabled(False)
        self.bar.setValue(0)
        self.status.setText("正在准备…")
        self.log_view.clear()

        flavor = str(self.flavor_combo.currentData())
        force = self.force_check.isChecked()

        def work() -> None:
            try:
                ok = installer.install_dependencies(
                    flavor=flavor,
                    force=force,
                    on_progress=lambda frac, text: self._bridge.progress.emit(float(frac), str(text)),
                    on_log=lambda text: self._bridge.logged.emit(str(text)),
                )
                message = "依赖安装完成" if ok else "依赖安装失败，请查看日志"
            except Exception as exc:  # noqa: BLE001
                ok, message = False, f"安装异常：{exc}"
                self._bridge.logged.emit(message)
            self._bridge.finished.emit(ok, message)

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _on_progress(self, fraction: float, text: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        self.status.setText(text)

    def _on_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _on_finished(self, ok: bool, message: str) -> None:
        self._ok = ok
        self.status.setText(message)
        if ok:
            self.status.setObjectName("StatusOk")
        else:
            self.status.setObjectName("StatusError")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.install_button.setEnabled(True)
        self.install_button.setText("重新检查")
        self.close_button.setEnabled(True)
        self.flavor_combo.setEnabled(True)
        self.force_check.setEnabled(True)

    # ---- 生命周期 -------------------------------------------------------
    def _on_close(self) -> None:
        self._cancel.set()
        if self._ok:
            self.accept()
        else:
            self.reject()

    @property
    def succeeded(self) -> bool:
        return self._ok


def run_dependency_dialog(parent: QWidget | None = None, auto_start: bool = True) -> bool:
    dialog = DependencyDialog(parent, auto_start=auto_start)
    dialog.exec()
    return dialog.succeeded
