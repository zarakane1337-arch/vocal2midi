"""批次：按队列顺序把一组曲目跑完。

这是批量转换唯一的新接缝。它只做编排——决定每首曲目落在哪个目录、产物
叫什么名字、跑完/失败/取消之后曲目变成什么状态、整体进度怎么聚合——而真正
烧 CPU 的两件事（分离、转写）由 `BatchDeps` 注入。测试用一个内存假依赖替换它，
于是排序、失败隔离、取消、重试复用、进度聚合全部可测，而不需要模型。

设计约束（见 `docs/adr/0002`）：串行执行，整批只用一个已加载的分离器。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

from . import names, separation, transcription
from .cancellation import Cancelled
from .pipeline import STAGE_WEIGHTS
from .queue import QueueItem, TrackStatus
from .transcription import TranscriptionParams

log = logging.getLogger("vocal2midi.batch")

# 单首曲目内部各阶段在总进度里的边界。占比只在 `pipeline.STAGE_WEIGHTS` 里
# 定义一次，这里只做累加——否则批次报出的阶段进度会和单曲流水线对不上。
SEPARATE_END = STAGE_WEIGHTS["separate"]
TRANSCRIBE_END = SEPARATE_END + STAGE_WEIGHTS["transcribe"]

ProgressFn = Callable[[float, str], None]
LogFn = Callable[[str], None]
TrackFn = Callable[[QueueItem], None]


# ---- 参数与产物 --------------------------------------------------------


@dataclass
class BatchOptions:
    """一整个批次共用的一套参数。"""

    out_dir: Path
    model: str = separation.DEMUCS_MODEL
    device: str = "auto"
    vocals_only: bool = False
    overwrite: bool = False
    params: TranscriptionParams = field(default_factory=TranscriptionParams)
    write_csv: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.params, TranscriptionParams):
            self.params = TranscriptionParams.from_mapping(self.params or {})
        self.out_dir = Path(self.out_dir)


@dataclass
class TranscribeOutcome:
    midi_path: Path | None = None
    csv_path: Path | None = None
    note_count: int = 0


class BatchDeps(Protocol):
    """批次依赖的两件事。真实实现见 `PipelineDeps`。"""

    def separate(
        self,
        audio_path: Path,
        out_dir: Path,
        stem: str,
        options: BatchOptions,
        on_progress: ProgressFn,
        cancel_event: threading.Event,
    ) -> Path: ...

    def transcribe(
        self,
        vocals_path: Path,
        out_dir: Path,
        stem: str,
        options: BatchOptions,
        on_progress: ProgressFn,
        cancel_event: threading.Event,
    ) -> TranscribeOutcome: ...


# 产物命名只有一处定义（`core.names`）；这里保留旧的公开名字，
# 因为测试与既有调用点从 `core.batch` 取它们。
midi_file = names.midi_file
csv_file = names.csv_file


def make_batch_dir(root: str | Path, now: datetime | None = None) -> Path:
    """在输出根目录下新建一个时间戳目录，同名时追加序号。"""
    root = Path(root)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = root / stamp
    suffix = 2
    while candidate.exists():
        candidate = root / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


# ---- 结果 --------------------------------------------------------------


@dataclass
class BatchFailure:
    item: QueueItem
    reason: str


@dataclass
class BatchResult:
    succeeded: list[QueueItem] = field(default_factory=list)
    failed: list[BatchFailure] = field(default_factory=list)
    cancelled: bool = False
    # 本次真的跑起来过的曲目数（不含跳过的已完成项）
    attempted: int = 0
    elapsed_s: float = 0.0
    batch_dirs: list[Path] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return len(self.succeeded)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def summary_text(self) -> str:
        if self.cancelled:
            return f"已取消 · 成功 {self.ok_count} · 失败 {self.failed_count} · 未处理 {self.remaining}"
        return f"成功 {self.ok_count} · 失败 {self.failed_count}"

    @property
    def remaining(self) -> int:
        return max(0, self.attempted - self.ok_count - self.failed_count)


# ---- 执行器 ------------------------------------------------------------


class BatchRunner:
    """把一个批次的曲目串行跑完。

    `items` 是这一批要处理的曲目（调用方已经从队列里挑出来了）；已完成的不在
    其中。执行器只改这些 item 的状态，不碰队列本身。
    """

    def __init__(
        self,
        options: BatchOptions,
        deps: BatchDeps | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
        on_track: TrackFn | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.options = options
        self.deps = deps or PipelineDeps()
        self._on_progress = on_progress or (lambda _f, _t: None)
        self._on_log = on_log or (lambda _t: None)
        self._on_track = on_track or (lambda _item: None)
        self._cancel = cancel_event or threading.Event()
        self._batch_dir: Path | None = None
        self._claimed: dict[str, set[str]] = {}
        self._used_dirs: list[Path] = []
        self._last_emitted = 0.0

    # ---- 进度聚合 -------------------------------------------------------
    def _emit(self, index: int, total: int, fraction: float, text: str) -> None:
        """整体进度 = (已完成曲目数 + 当前曲目内部进度) / 本批要跑的曲目数。"""
        if total <= 0:
            return
        fraction = max(0.0, min(1.0, fraction))
        overall = min(1.0, (index + fraction) / total)
        # 单调不减：曲目之间切阶段时不应该往回跳
        overall = max(overall, self._last_emitted)
        self._last_emitted = overall
        self._on_progress(overall, f"第 {index + 1}/{total} 首 · {text}")

    # ---- 批次目录 -------------------------------------------------------
    def _ensure_batch_dir(self) -> Path:
        if self._batch_dir is None:
            self._batch_dir = make_batch_dir(self.options.out_dir)
            log.info("本次批次目录：%s", self._batch_dir)
            self._on_log(f"批次目录：{self._batch_dir}")
        return self._batch_dir

    def _dir_for(self, item: QueueItem) -> Path:
        """曲目自己的批次目录；没有就分一个（重试会沿用已有的）。"""
        if item.batch_dir:
            target = Path(item.batch_dir)
            target.mkdir(parents=True, exist_ok=True)
            return target
        target = self._ensure_batch_dir()
        item.batch_dir = str(target)
        return target

    def _stem_for(self, item: QueueItem, out_dir: Path) -> str:
        """产物主名：首次进批时确定并钉在曲目上，同批重名追加序号。

        重试的曲目已经有主名，直接用——否则它会换到一个新主名上，上次的
        产物就成了没人认领的孤儿。
        """
        claimed = self._claimed.setdefault(str(out_dir), set())
        if item.stem:
            claimed.add(item.stem)
            return item.stem

        base = Path(item.path).stem or "track"
        stem = base
        index = 2
        while stem in claimed or self._stem_taken(out_dir, stem):
            stem = f"{base}-{index}"
            index += 1
        claimed.add(stem)
        item.stem = stem
        return stem

    @staticmethod
    def _stem_taken(out_dir: Path, stem: str) -> bool:
        """目录里已经有这个主名的产物，就换一个，别往上叠同名文件。"""
        return names.vocals_file(out_dir, stem).exists() or midi_file(out_dir, stem).exists()

    # ---- 主流程 ---------------------------------------------------------
    def run(self, items: Sequence[QueueItem] | Iterable[QueueItem]) -> BatchResult:
        items = list(items)
        result = BatchResult(attempted=len(items))
        started = time.monotonic()
        total = len(items)

        if not items:
            self._on_log("没有待转换的曲目")
            result.elapsed_s = time.monotonic() - started
            return result

        self._on_log(f"开始处理 {total} 首曲目")
        for index, item in enumerate(items):
            if self._cancel.is_set():
                result.cancelled = True
                break
            try:
                self._run_one(index, total, item)
            except Cancelled:
                # 用户取消不代表这首曲目有问题：退回待转换，下一次继续。
                item.mark_pending()
                self._on_track(item)
                result.cancelled = True
                self._on_log(f"已取消：{item.display_name} 退回待转换")
                break
            except Exception as exc:  # noqa: BLE001 - 单首失败不能中断整批
                log.error("曲目失败 %s：%s", item.path, exc)
                item.mark_failed(str(exc) or exc.__class__.__name__)
                self._on_track(item)
                result.failed.append(BatchFailure(item=item, reason=item.error))
                self._on_log(f"失败：{item.display_name} — {item.error}")
                continue

            result.succeeded.append(item)
            self._on_track(item)
            self._emit(index, total, 1.0, f"完成 · {item.display_name}")

        if not result.cancelled:
            self._last_emitted = 1.0
            # 统一成「第 N/M 首 · …」的形状，界面才能拿它直接当同一行进度文案。
            self._on_progress(1.0, f"第 {total}/{total} 首 · 全部完成")

        # 只收真正跑过的曲目用到的目录，按出现顺序去重；没轮到的曲目可能还带着
        # 上一轮的目录，算进来会凭空多出一个链接。
        result.batch_dirs.extend(self._used_dirs)
        result.elapsed_s = time.monotonic() - started
        self._on_log(result.summary_text())
        return result

    def _run_one(self, index: int, total: int, item: QueueItem) -> None:
        audio = Path(item.path)
        if not audio.is_file():
            raise FileNotFoundError(f"找不到音频文件：{item.path}")

        out_dir = self._dir_for(item)
        # 汇总里要列出本批**实际写过**的目录：全重试的批次一个目录都不新建，
        # 但产物确实落在旧目录里，漏了它会让汇总的批次目录链接消失。
        if out_dir not in self._used_dirs:
            self._used_dirs.append(out_dir)
        stem = self._stem_for(item, out_dir)
        item.mark_running()
        self._on_track(item)
        self._on_log(f"开始：{item.display_name}")
        self._emit(index, total, 0.0, f"分离中 · {item.display_name}")

        vocals: Path
        if self.options.vocals_only:
            self._on_log("跳过分离：输入文件已是人声轨")
            vocals = audio
            self._emit(index, total, SEPARATE_END, f"分离中 · {item.display_name}")
        else:
            target = names.vocals_file(out_dir, stem)
            if not self.options.overwrite and separation.is_reusable_vocals(target):
                self._on_log(f"已复用人声轨：{target.name}")
                vocals = target
                self._emit(index, total, SEPARATE_END, f"分离中 · {item.display_name}")
            else:
                vocals = self.deps.separate(
                    audio,
                    out_dir,
                    stem,
                    self.options,
                    lambda frac, text: self._emit(index, total, frac * SEPARATE_END, text),
                    self._cancel,
                )
            if self._cancel.is_set():
                raise Cancelled("已取消")

        if self._cancel.is_set():
            raise Cancelled("已取消")

        self._emit(index, total, SEPARATE_END, f"转写中 · {item.display_name}")
        outcome = self.deps.transcribe(
            vocals,
            out_dir,
            stem,
            self.options,
            lambda frac, text: self._emit(
                index, total, SEPARATE_END + STAGE_WEIGHTS["transcribe"] * max(0.0, min(1.0, frac)), text
            ),
            self._cancel,
        )
        if self._cancel.is_set():
            raise Cancelled("已取消")

        # 写盘是第三阶段，但真正写文件这件事包含在 `transcribe` 依赖里。所以这里
        # 只补一帧，让「写盘」这一段在进度上真的存在，且三段加起来恰好到 1.0。
        self._emit(index, total, TRANSCRIBE_END, f"写盘中 · {item.display_name}")

        item.mark_done(
            vocals_path="" if self.options.vocals_only else vocals,
            midi_path=outcome.midi_path or "",
            csv_path=outcome.csv_path or "",
        )
        self._emit(index, total, 1.0, f"完成 · {item.display_name}")


# ---- 真实依赖 ----------------------------------------------------------


class PipelineDeps:
    """把批次接到真实的分离层与转写层上。

    分离器按 (模型, 设备) 缓存：整批只加载一次权重（`docs/adr/0002`）。
    """

    def __init__(self, cache: separation.SeparatorCache | None = None) -> None:
        self.cache = cache or separation.SeparatorCache()

    def separate(
        self,
        audio_path: Path,
        out_dir: Path,
        stem: str,
        options: BatchOptions,
        on_progress: ProgressFn,
        cancel_event: threading.Event,
    ) -> Path:
        session = self.cache.get(options.model, options.device, on_progress)
        return separation.separate_vocals(
            audio_path,
            out_dir,
            model=options.model,
            device=options.device,
            overwrite=options.overwrite,
            on_progress=on_progress,
            stem=stem,
            session=session,
            cancel_event=cancel_event,
        )

    def transcribe(
        self,
        vocals_path: Path,
        out_dir: Path,
        stem: str,
        options: BatchOptions,
        on_progress: ProgressFn,
        cancel_event: threading.Event,
    ) -> TranscribeOutcome:
        if cancel_event.is_set():
            raise Cancelled("已取消")
        on_progress(0.1, "分析音高与音符")
        midi_path, events = transcription.transcribe_file(
            vocals_path, midi_file(out_dir, stem), options.params
        )
        if cancel_event.is_set():
            raise Cancelled("已取消")
        on_progress(0.9, f"转写完成，共 {len(events)} 个音符")

        out_csv: Path | None = None
        if options.write_csv:
            out_csv = transcription.write_notes_csv(events, csv_file(out_dir, stem))
        return TranscribeOutcome(midi_path=midi_path, csv_path=out_csv, note_count=len(events))


# ---- 启动前校验 --------------------------------------------------------


def downgrade_missing_products(items: Iterable[QueueItem]) -> list[QueueItem]:
    """把「状态是完成但产物已经不在了」的曲目降级为待转换。

    保留批次目录与产物主名：降级的曲目重试时还要回到同一个地方。
    """
    downgraded: list[QueueItem] = []
    for item in items:
        if item.track_status is TrackStatus.DONE and not item.products_exist():
            item.mark_pending()
            downgraded.append(item)
    return downgraded


def run_batch(
    items: Sequence[QueueItem],
    options: BatchOptions,
    deps: BatchDeps | None = None,
    on_progress: ProgressFn | None = None,
    on_log: LogFn | None = None,
    on_track: TrackFn | None = None,
    cancel_event: threading.Event | None = None,
) -> BatchResult:
    """阻塞式跑完一个批次（调用方负责放到线程里）。"""
    return BatchRunner(options, deps, on_progress, on_log, on_track, cancel_event).run(items)
