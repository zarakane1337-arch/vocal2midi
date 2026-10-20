"""队列面板：曲目列表 + 增删按钮 + 产物取件。

界面只负责显示与点击，队列的增删排序与状态都由 `core.queue` 决定。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.queue import Queue, QueueItem, TrackStatus
from .widgets import Card

log = logging.getLogger("vocal2midi.queue_panel")

_ROLE_INDEX = Qt.ItemDataRole.UserRole + 1


class QueueListWidget(QListWidget):
    """接收拖放的曲目列表。"""

    pathsDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(False)
        self.setMinimumHeight(150)
        self.setObjectName("QueueList")

    # ---- 拖放 -----------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls()]
        paths = [p for p in paths if p]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class QueuePanel(QWidget):
    """曲目队列的完整界面。"""

    changed = Signal()  # 队列内容变了（增删）
    rejected = Signal(list)  # [(路径, 原因)]
    openProducts = Signal(object)  # QueueItem
    openBatchDir = Signal(object)  # QueueItem

    def __init__(self, queue: Queue, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self._build()

    # ---- 构建 -----------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        self.list = QueueListWidget()
        self.list.pathsDropped.connect(self.add_paths)
        self.list.itemDoubleClicked.connect(self._on_double_click)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_context_menu)
        self.list.itemSelectionChanged.connect(self._refresh_buttons)
        outer.addWidget(self.list, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.add_button = QPushButton("添加歌曲…")
        self.add_button.clicked.connect(lambda: self.addRequested.emit())
        row.addWidget(self.add_button)

        self.remove_button = QPushButton("删除")
        self.remove_button.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_button)

        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self.clear_queue)
        row.addWidget(self.clear_button)

        row.addStretch(1)

        self.count_label = QLabel("队列为空")
        self.count_label.setObjectName("Faint")
        row.addWidget(self.count_label)
        outer.addLayout(row)

        self.refresh()

    # 追加到末尾，方便主窗口接上「添加歌曲…」
    addRequested = Signal()

    # ---- 显示 -----------------------------------------------------------
    def refresh(self, keep_row: int | None = None) -> None:
        current = self.list.currentRow() if keep_row is None else keep_row
        self.list.blockSignals(True)
        self.list.clear()
        for index, item in enumerate(self.queue.items):
            entry = QListWidgetItem(self._row_text(item))
            entry.setData(_ROLE_INDEX, index)
            entry.setToolTip(self._row_tooltip(item))
            self.list.addItem(entry)
        self.list.blockSignals(False)
        if 0 <= current < self.list.count():
            self.list.setCurrentRow(current)
        self._refresh_buttons()

    @staticmethod
    def _row_text(item: QueueItem) -> str:
        status = item.track_status
        text = f"{status.marker}  {item.display_name}"
        if status is TrackStatus.FAILED and item.error:
            reason = item.error.splitlines()[0]
            text += f"    — {reason[:80]}"
        return text

    @staticmethod
    def _row_tooltip(item: QueueItem) -> str:
        status = item.track_status
        lines = [f"路径：{item.path}", f"状态：{status.label}"]
        if item.batch_dir:
            lines.append(f"批次目录：{item.batch_dir}")
        if item.stem:
            lines.append(f"产物主名：{item.stem}")
        if status is TrackStatus.FAILED and item.error:
            lines.append(f"失败原因：{item.error}")
        if status is TrackStatus.DONE:
            lines.append("双击可打开产物")
        return "\n".join(lines)

    def _refresh_buttons(self) -> None:
        has_selection = bool(self.list.selectedItems())
        running = getattr(self, "_locked", False)
        self.remove_button.setEnabled(has_selection and not running)
        self.clear_button.setEnabled(self.queue.total > 0 and not running)
        self.add_button.setEnabled(not running)

        counts = self.queue.counts()
        if not self.queue.total:
            self.count_label.setText("队列为空")
            return
        parts = [f"共 {self.queue.total} 首"]
        for status in (TrackStatus.PENDING, TrackStatus.RUNNING, TrackStatus.DONE, TrackStatus.FAILED):
            if counts[status.value]:
                parts.append(f"{status.label} {counts[status.value]}")
        self.count_label.setText(" · ".join(parts))

    def set_locked(self, locked: bool) -> None:
        """跑批期间：允许删改队列吗？不允许删，但允许继续拖入。"""
        self._locked = locked
        self.remove_button.setEnabled(bool(self.list.selectedItems()) and not locked)
        self.clear_button.setEnabled(self.queue.total > 0 and not locked)
        self._refresh_buttons()

    # ---- 增删 -----------------------------------------------------------
    def add_paths(self, paths: list[str]) -> None:
        accepted, rejected = self.queue.add_paths(paths)
        if rejected:
            self.rejected.emit(rejected)
        if accepted:
            self.refresh()
            self.list.setCurrentRow(self.list.count() - 1)
            self.changed.emit()
        else:
            self._refresh_buttons()

    def remove_selected(self) -> None:
        rows = sorted({self.list.row(entry) for entry in self.list.selectedItems()}, reverse=True)
        if not rows:
            return
        for row in rows:
            self.queue.remove_at(row)
        self.refresh()
        self.changed.emit()

    def clear_queue(self) -> None:
        if not self.queue.total:
            return
        self.queue.clear()
        self.refresh()
        self.changed.emit()

    # ---- 打开产物 -------------------------------------------------------
    def _item_at(self, row: int) -> QueueItem | None:
        if 0 <= row < self.queue.total:
            return self.queue.items[row]
        return None

    def _on_double_click(self, entry: QListWidgetItem) -> None:
        item = self._item_at(self.list.row(entry))
        if item is None:
            return
        if item.track_status is TrackStatus.DONE and item.products:
            self.openProducts.emit(item)
        elif item.batch_dir:
            self.openBatchDir.emit(item)

    def _on_context_menu(self, position) -> None:
        entry = self.list.itemAt(position)
        if entry is None:
            return
        item = self._item_at(self.list.row(entry))
        if item is None:
            return
        menu = QMenu(self)
        if item.products:
            act_open = QAction("打开产物", self)
            act_open.triggered.connect(lambda: self.openProducts.emit(item))
            menu.addAction(act_open)
        if item.batch_dir:
            act_dir = QAction("打开批次目录", self)
            act_dir.triggered.connect(lambda: self.openBatchDir.emit(item))
            menu.addAction(act_dir)
        if item.track_status is TrackStatus.FAILED and item.error:
            menu.addSeparator()
            act_reason = QAction("查看失败原因", self)
            act_reason.triggered.connect(lambda: self.openProducts.emit(item))
            menu.addAction(act_reason)
        if menu.actions():
            menu.exec(self.list.viewport().mapToGlobal(position))

    def selected_items(self) -> list[QueueItem]:
        return [
            item
            for item in (self._item_at(self.list.row(entry)) for entry in self.list.selectedItems())
            if item is not None
        ]


class QueueCard(Card):
    """把队列面板包进一张卡片，与其它卡片视觉一致。"""

    def __init__(self, progress_hint: str, parent: QWidget | None = None) -> None:
        super().__init__("歌曲队列", progress_hint, parent)
        self.panel: QueuePanel | None = None

    def mount(self, panel: QueuePanel) -> None:
        self.panel = panel
        self.body.addWidget(panel)
