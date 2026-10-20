"""队列：跨批次存续的曲目列表。

队列是用户长期维护的对象，批次只是它的一次执行（见 `docs/adr/0001`）。
这里只放数据与 JSON 互转，不依赖 Qt，也不去碰磁盘上的产物文件——产物的
校验属于执行器的事，因为「什么时候校验」是批次语义的一部分。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .pipeline import AUDIO_EXTS

log = logging.getLogger("vocal2midi.queue")


class TrackStatus(str, Enum):
    """曲目状态。取值与 `GLOSSARY.md` 的中文名一一对应。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            TrackStatus.PENDING: "待转换",
            TrackStatus.RUNNING: "转换中",
            TrackStatus.DONE: "完成",
            TrackStatus.FAILED: "失败",
        }[self]

    @property
    def marker(self) -> str:
        """列表里显示的单字符标记。"""
        return {
            TrackStatus.PENDING: "·",
            TrackStatus.RUNNING: "▶",
            TrackStatus.DONE: "✓",
            TrackStatus.FAILED: "!",
        }[self]


# 状态从字符串恢复时的容错映射：写坏的值退回「待转换」，不让整个队列读不出来。
_STATUS_FROM_TEXT = {status.value: status for status in TrackStatus}


def normalise_status(value: object) -> TrackStatus:
    if isinstance(value, TrackStatus):
        return value
    return _STATUS_FROM_TEXT.get(str(value or "").strip().lower(), TrackStatus.PENDING)


@dataclass
class QueueItem:
    """队列里的一首曲目。字段全部可 JSON 化，直接进设置文件。"""

    path: str
    status: str = TrackStatus.PENDING.value
    # 产物主名在曲目首次进入批次目录时确定并钉住（重试必须落在同一个主名上）。
    stem: str = ""
    # 这首曲目自己的批次目录；重试回到它，而不是新建一个。
    batch_dir: str = ""
    vocals_path: str = ""
    midi_path: str = ""
    csv_path: str = ""
    error: str = ""

    # ---- 只读派生 -------------------------------------------------------
    @property
    def track_status(self) -> TrackStatus:
        return normalise_status(self.status)

    @property
    def display_name(self) -> str:
        return Path(self.path).name or self.path

    @property
    def products(self) -> list[str]:
        return [p for p in (self.vocals_path, self.midi_path, self.csv_path) if p]

    def products_exist(self) -> bool:
        """三个产物里凡是记了路径的，是否都还在磁盘上。

        只记了 MIDI 也算「有产物」：`vocals_only` 模式下没有 wav，
        `write_csv=False` 时没有 csv，用「全部记了路径的都得在」来判定。
        """
        recorded = self.products
        if not recorded:
            return False
        return all(Path(p).is_file() for p in recorded)

    # ---- 状态迁移 -------------------------------------------------------
    def mark_running(self) -> None:
        self.status = TrackStatus.RUNNING.value
        self.error = ""

    def mark_done(self, vocals_path="", midi_path="", csv_path="") -> None:
        self.status = TrackStatus.DONE.value
        self.error = ""
        self.vocals_path = str(vocals_path or "")
        self.midi_path = str(midi_path or "")
        self.csv_path = str(csv_path or "")

    def mark_failed(self, reason: str) -> None:
        self.status = TrackStatus.FAILED.value
        self.error = str(reason)

    def mark_pending(self, drop_products: bool = True) -> None:
        """退回待转换。

        `drop_products=False` 用于「状态是被校验降级的」这种情况：产物路径
        已经确认失效，但批次目录要留着，重试还得回那里。
        """
        self.status = TrackStatus.PENDING.value
        if drop_products:
            self.vocals_path = ""
            self.midi_path = ""
            self.csv_path = ""

    # ---- JSON 互转 ------------------------------------------------------
    _TEXT_FIELDS = ("path", "status", "stem", "batch_dir", "vocals_path", "midi_path", "csv_path", "error")

    def to_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name) or "") for name in self._TEXT_FIELDS}

    @classmethod
    def from_dict(cls, payload: object) -> "QueueItem | None":
        if not isinstance(payload, dict):
            return None
        path = str(payload.get("path") or "").strip()
        if not path:
            return None
        item = cls(path=path)
        for name in cls._TEXT_FIELDS:
            if name in ("path",):
                continue
            value = payload.get(name)
            if isinstance(value, str):
                setattr(item, name, value)
        item.status = normalise_status(item.status).value
        return item


# ---- 入队校验 ----------------------------------------------------------

SUPPORTED_SUFFIXES = tuple(ext.lower() for ext in AUDIO_EXTS)


def check_importable(path: str | Path) -> str:
    """能不能入队。返回空字符串表示可以，否则是给用户看的原因。"""
    text = str(path or "").strip()
    if not text:
        return "路径为空"
    candidate = Path(text)
    if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
        return f"不支持的格式：{candidate.suffix or '（无扩展名）'}"
    if not candidate.exists():
        return "文件不存在"
    if not candidate.is_file():
        return "不是文件"
    if not os.access(candidate, os.R_OK):
        return "文件不可读"
    return ""


@dataclass
class Queue:
    """曲目的有序容器。不截断数量（「最近文件」上限 8 的规则不适用于队列）。"""

    items: list[QueueItem] = field(default_factory=list)

    # ---- 增删 -----------------------------------------------------------
    def add_paths(self, paths: list[str] | tuple[str, ...]) -> tuple[list[QueueItem], list[tuple[str, str]]]:
        """按给定顺序追加到队尾。

        返回 (被接受的条目, [(被拒绝的路径, 原因)])。单条被拒绝不影响其余。
        """
        accepted: list[QueueItem] = []
        rejected: list[tuple[str, str]] = []
        for raw in paths:
            reason = check_importable(raw)
            if reason:
                rejected.append((str(raw), reason))
                continue
            item = QueueItem(path=str(Path(str(raw)).resolve()))
            self.items.append(item)
            accepted.append(item)
        return accepted, rejected

    def remove_at(self, index: int) -> bool:
        if 0 <= index < len(self.items):
            del self.items[index]
            return True
        return False

    def clear(self) -> None:
        self.items.clear()

    def move_up(self, index: int) -> bool:
        if 1 <= index < len(self.items):
            self.items[index - 1], self.items[index] = self.items[index], self.items[index - 1]
            return True
        return False

    def move_down(self, index: int) -> bool:
        if 0 <= index < len(self.items) - 1:
            self.items[index + 1], self.items[index] = self.items[index], self.items[index + 1]
            return True
        return False

    def index_of(self, item: QueueItem) -> int:
        try:
            return self.items.index(item)
        except ValueError:
            return -1

    # ---- 查询 -----------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.items)

    def pending_items(self) -> list[QueueItem]:
        """下一次批次会处理的曲目：待转换与失败。"""
        return [item for item in self.items if item.track_status in (TrackStatus.PENDING, TrackStatus.FAILED)]

    def counts(self) -> dict[str, int]:
        tally = {status.value: 0 for status in TrackStatus}
        for item in self.items:
            tally[item.track_status.value] += 1
        return tally

    # ---- 持久化 ---------------------------------------------------------
    def to_dicts(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.items]

    @classmethod
    def from_dicts(cls, payload: object) -> "Queue":
        queue = cls()
        if not isinstance(payload, list):
            return queue
        for entry in payload:
            item = QueueItem.from_dict(entry)
            if item is not None:
                queue.items.append(item)
        return queue
