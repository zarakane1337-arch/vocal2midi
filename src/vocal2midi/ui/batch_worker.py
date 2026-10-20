"""批次转换的工作线程。

与 `PipelineWorker` 的分工：那个跑单文件、信号只描述「这一首」，这个跑整个
批次、额外汇报批次级进度与曲目级状态变更。核心逻辑全在 `core.batch`，这里
只做线程与信号的桥接。
"""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..core.batch import BatchDeps, BatchOptions, BatchResult, run_batch
from ..core.cancellation import Cancelled
from ..core.queue import QueueItem

log = logging.getLogger("vocal2midi.batch_worker")


class BatchWorker(QThread):
    """在子线程跑完整批，通过信号汇报进度与每首曲目的状态。"""

    progress = Signal(float, str)  # (0..1 整体进度, 状态文本)
    logged = Signal(str)
    trackChanged = Signal(object)  # QueueItem（状态已更新）
    finishedOk = Signal(object)  # BatchResult
    failed = Signal(str)
    cancelledByUser = Signal(object)  # BatchResult

    def __init__(
        self,
        items: list[QueueItem],
        options: BatchOptions,
        deps: BatchDeps | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.items = list(items)
        self.options = options
        self.deps = deps
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            result = run_batch(
                self.items,
                self.options,
                deps=self.deps,
                on_progress=lambda frac, text: self.progress.emit(float(frac), str(text)),
                on_log=lambda text: self.logged.emit(str(text)),
                on_track=lambda item: self.trackChanged.emit(item),
                cancel_event=self._cancel,
            )
        except Cancelled:
            self.cancelledByUser.emit(BatchResult(cancelled=True))
        except Exception as exc:  # noqa: BLE001 - 所有失败都要回到界面
            log.error("批次失败：%s\n%s", exc, traceback.format_exc())
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            if result.cancelled:
                self.cancelledByUser.emit(result)
            else:
                self.finishedOk.emit(result)
