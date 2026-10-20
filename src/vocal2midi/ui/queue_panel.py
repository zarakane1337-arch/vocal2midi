"""队列面板：曲目列表 + 增删按钮 + 产物取件。

界面只负责显示与点击，队列的增删排序与状态都由 `core.queue` 决定。
"""

from __future__ import annotations

import logging

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
    errorRequested = Signal(object)  # QueueItem，要看失败原因
    openBatchDirRequested = Signal(str)  # 要整体取件的批次目录，空串 = 无

    def __init__(self, queue: Queue, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.queue = queue
        self._locked = False
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

        self.up_button = QPushButton("上移")
        self.up_button.clicked.connect(self.move_selected_up)
        row.addWidget(self.up_button)

        self.down_button = QPushButton("下移")
        self.down_button.clicked.connect(self.move_selected_down)
        row.addWidget(self.down_button)

        # 整体取件：一次处理一整批产物，而不用逐首点开。具体开哪个目录
        # 由主窗口决定（它知道上一次批次写了哪里），这里只负责发出请求。
        self.batch_dir_button = QPushButton("打开批次目录")
        self.batch_dir_button.clicked.connect(
            lambda: self.openBatchDirRequested.emit(self._batch_dir_target())
        )
        row.addWidget(self.batch_dir_button)

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
        running = self._locked
        row = self._selected_row()
        self.remove_button.setEnabled(has_selection and not running)
        self.clear_button.setEnabled(self.queue.total > 0 and not running)
        # 跑批期间特意允许继续加歌：批次开始时已经取过快照，新加的歌
        # 只会等下一批，不会插进正在跑的那批里。
        self.add_button.setEnabled(True)
        self.up_button.setEnabled(not running and row is not None and row > 0)
        self.down_button.setEnabled(
            not running and row is not None and row < self.queue.total - 1
        )
        self.batch_dir_button.setEnabled(self._batch_dir_target() is not None)

        counts = self.queue.counts()
        if not self.queue.total:
            self.count_label.setText("队列为空")
            return
        parts = [f"共 {self.queue.total} 首"]
        for status in (TrackStatus.PENDING, TrackStatus.RUNNING, TrackStatus.DONE, TrackStatus.FAILED):
            if counts[status.value]:
                parts.append(f"{status.label} {counts[status.value]}")
        self.count_label.setText(" · ".join(parts))

    def _batch_dir_target(self) -> str:
        """会拿哪个批次目录去打开：先看选中那首，再看整个队列。"""
        item = self.selected_item()
        if item is not None and item.batch_dir:
            return item.batch_dir
        for candidate in reversed(self.queue.items):
            if candidate.batch_dir:
                return candidate.batch_dir
        return ""

    def set_locked(self, locked: bool) -> None:
        """跑批期间：允许删改队列吗？不允许删，但允许继续拖入。"""
        self._locked = locked
        self._refresh_buttons()

    def _selected_row(self) -> int | None:
        entries = self.list.selectedItems()
        if len(entries) != 1:
            return None
        return self.list.row(entries[0])

    def selected_item(self) -> QueueItem | None:
        """当前选中的那一首（多选或无选中时为 None）。"""
        row = self._selected_row()
        return None if row is None else self._item_at(row)

    def move_selected_up(self) -> None:
        row = self._selected_row()
        if row is None or row <= 0:
            return
        self.queue.move_up(row)
        self.refresh(keep_row=row - 1)
        self.list.setCurrentRow(row - 1)
        self.changed.emit()

    def move_selected_down(self) -> None:
        row = self._selected_row()
        if row is None or row >= self.queue.total - 1:
            return
        self.queue.move_down(row)
        self.refresh(keep_row=row + 1)
        self.list.setCurrentRow(row + 1)
        self.changed.emit()

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
            act_reason.triggered.connect(lambda: self.errorRequested.emit(item))
            menu.addAction(act_reason)
        if menu.actions():
            menu.exec(self.list.viewport().mapToGlobal(position))
