"""转写进度工作线程。

Qt 的信号槽必须run在主线程，而耗时任务要放到子线程；这里用 `QThread` + 信号
把 core.pipeline 的进度回调桥接回界面。
"""

from __future__ import annotations

import logging
import threading
import traceback

from PySide6.QtCore import QObject, QThread, Signal

from ..core.pipeline import Cancelled, PipelineOptions, PipelineResult, run_pipeline

log = logging.getLogger("vocal2midi.worker")


class PipelineWorker(QThread):
    """在子线程跑完整流水线，通过信号汇报进度。"""

    progress = Signal(float, str)  # (0..1, 状态文本)
    stage = Signal(str)  # 阶段名
    logged = Signal(str)
    finishedOk = Signal(object)  # PipelineResult
    failed = Signal(str)
    cancelledByUser = Signal()

    def __init__(self, options: PipelineOptions, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.options = options
        self._cancel = threading.Event()

    # ---- 供界面调用 -----------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    # ---- QThread 主体 ---------------------------------------------------
    def run(self) -> None:  # noqa: D102 - QThread 约定的入口
        try:
            result = run_pipeline(
                self.options,
                on_progress=lambda frac, text: self.progress.emit(float(frac), str(text)),
                on_log=lambda text: self.logged.emit(str(text)),
                cancel_event=self._cancel,
            )
        except Cancelled:
            self.cancelledByUser.emit()
        except Exception as exc:  # noqa: BLE001 - 所有失败都要回到界面
            log.error("流水线失败：%s\n%s", exc, traceback.format_exc())
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.finishedOk.emit(result)
