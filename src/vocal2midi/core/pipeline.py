"""两阶段流水线：分离 → 转写 → 落盘。

界面层只调用 `run_pipeline()`，把「进度 / 日志 / 取消」通过回调注入，
这样界面逻辑与业务逻辑彻底解耦。

批量转换要复用同一个已加载的分离器，并且自己决定产物的目录与主名，
所以这两件事都可以由调用方注入；不注入时行为与单文件转换完全一致。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import params as param_registry
from . import separation, transcription
from .cancellation import Cancelled

log = logging.getLogger("vocal2midi.pipeline")

AUDIO_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".ape")

# 各阶段在总进度中的占比
STAGE_WEIGHTS = {"separate": 0.62, "transcribe": 0.32, "write": 0.06}


# 取消语义只有一个定义（`core.cancellation`），这里重新导出，
# 让既有的 `from .pipeline import Cancelled` 继续可用。
__all__ = [
    "AUDIO_EXTS",
    "STAGE_WEIGHTS",
    "Cancelled",
    "PipelineOptions",
    "PipelineResult",
    "Pipeline",
    "run_pipeline",
    "default_params",
]


@dataclass
class PipelineOptions:
    audio_path: Path
    out_dir: Path
    model: str = separation.DEMUCS_MODEL
    device: str = "auto"
    vocals_only: bool = False
    overwrite: bool = False
    params: transcription.TranscriptionParams = field(default_factory=transcription.TranscriptionParams)
    write_csv: bool = True
    # 产物主名与已加载的分离器都可以由调用方指定；留空时沿用「用输入文件名现算」。
    stem: str | None = None
    session: separation.SeparatorSession | None = None

    def __post_init__(self) -> None:
        # 允许直接传 dict（界面层用映射更顺手），统一在这里转换成参数对象，
        # 避免下游 `params.hold_ms` 之类访问在 dict 上炸掉。
        if not isinstance(self.params, transcription.TranscriptionParams):
            self.params = transcription.TranscriptionParams.from_mapping(self.params or {})
        self.audio_path = Path(self.audio_path)
        self.out_dir = Path(self.out_dir)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "PipelineOptions":
        raw = dict(mapping)
        param_values = raw.pop("params", None) or {}
        if not isinstance(param_values, Mapping):
            param_values = {}
        stem = raw.get("stem")
        return cls(
            audio_path=Path(raw.get("audio_path", "")),
            out_dir=Path(raw.get("out_dir", ".")),
            model=str(raw.get("model", separation.DEMUCS_MODEL)),
            device=str(raw.get("device", "auto")),
            vocals_only=bool(raw.get("vocals_only", False)),
            overwrite=bool(raw.get("overwrite", False)),
            params=transcription.TranscriptionParams.from_mapping(param_values),
            write_csv=bool(raw.get("write_csv", True)),
            stem=str(stem) if stem else None,
            session=raw.get("session"),
        )


@dataclass
class PipelineResult:
    vocals_path: Path | None
    midi_path: Path | None
    csv_path: Path | None
    events: list
    elapsed_s: float = 0.0

    @property
    def note_count(self) -> int:
        return len(self.events)


ProgressFn = Callable[[float, str], None]
LogFn = Callable[[str], None]


class Pipeline:
    """可取消的流水线封装。"""

    def __init__(
        self,
        options: PipelineOptions,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.options = options
        self._on_progress = on_progress or (lambda _v, _t: None)
        self._on_log = on_log or (lambda _t: None)
        self._cancel = cancel_event or threading.Event()
        self._stage = "separate"
        self._stage_fraction = 0.0
        self._done_weight = 0.0

    # ---- 进度换算 -------------------------------------------------------
    def _emit(self, fraction: float, text: str) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self._stage_fraction = fraction
        total = self._done_weight + STAGE_WEIGHTS[self._stage] * fraction
        self._on_progress(min(0.999, total), text)

    def _enter(self, stage: str, text: str) -> None:
        self._done_weight = sum(
            weight for name, weight in STAGE_WEIGHTS.items() if list(STAGE_WEIGHTS).index(name) < list(STAGE_WEIGHTS).index(stage)
        )
        self._stage = stage
        self._on_log(text)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---- 主流程 ---------------------------------------------------------
    def run(self) -> PipelineResult:
        import time

        started = time.monotonic()
        opts = self.options
        if not opts.audio_path.exists():
            raise FileNotFoundError(f"找不到音频文件：{opts.audio_path}")
        opts.out_dir.mkdir(parents=True, exist_ok=True)

        vocals_path: Path | None = None
        if opts.vocals_only:
            self._enter("separate", "跳过分离：输入文件已是人声轨")
            vocals_path = opts.audio_path
            self._done_weight = STAGE_WEIGHTS["separate"]
        else:
            self._enter("separate", "开始人声分离")
            vocals_path = separation.separate_vocals(
                opts.audio_path,
                opts.out_dir,
                model=opts.model,
                device=opts.device,
                overwrite=opts.overwrite,
                on_progress=lambda frac, text: self._emit(frac, text),
                stem=opts.stem,
                session=opts.session,
                cancel_event=self._cancel,
            )
            if self.cancelled:
                raise Cancelled("已取消")

        if self.cancelled:
            raise Cancelled("已取消")

        if opts.vocals_only:
            stem = opts.stem or opts.audio_path.stem.replace("_vocals", "")
            midi_base = opts.out_dir
        else:
            stem = opts.stem or opts.audio_path.stem
            midi_base = Path(vocals_path).parent
        midi_path = midi_base / f"{stem}_vocals.mid"
        csv_path = midi_base / f"{stem}_vocals_notes.csv"

        self._enter("transcribe", "开始人声转 MIDI")
        self._emit(0.1, "分析音高与音符")
        midi_path, events = transcription.transcribe_file(vocals_path, midi_path, opts.params)
        self._emit(1.0, f"转写完成，共 {len(events)} 个音符")
        if self.cancelled:
            raise Cancelled("已取消")

        out_csv: Path | None = None
        if opts.write_csv:
            self._enter("write", "写出音符表")
            out_csv = transcription.write_notes_csv(events, csv_path)

        if not events:
            self._on_log("未检出音符：可放宽置信度阈值或调整基频范围")

        elapsed = time.monotonic() - started
        self._on_progress(1.0, "全部完成")
        self._on_log(f"完成，用时 {elapsed:.1f}s")
        return PipelineResult(
            vocals_path=Path(vocals_path) if vocals_path else None,
            midi_path=midi_path,
            csv_path=out_csv,
            events=events,
            elapsed_s=elapsed,
        )


def run_pipeline(
    options: PipelineOptions,
    on_progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
    cancel_event: threading.Event | None = None,
) -> PipelineResult:
    """阻塞式执行一次流水线（调用方负责放到线程里）。"""
    return Pipeline(options, on_progress, on_log, cancel_event).run()


def default_params() -> dict[str, Any]:
    return param_registry.default_params()
