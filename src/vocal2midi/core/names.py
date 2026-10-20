"""产物命名：目录 + 主名 → 文件路径。

三处产物（人声轨、MIDI、音符表）的名字只在这里定义一次。分离层、批次和
流水线都从这里取，谁也别再手拼一遍——批次写下的文件另一个阶段必须能找到，
这件事只能有一个答案。
"""

from __future__ import annotations

from pathlib import Path

VOCALS_SUFFIX = "_vocals.wav"
MIDI_SUFFIX = "_vocals.mid"
CSV_SUFFIX = "_vocals_notes.csv"


def vocals_file(out_dir: str | Path, stem: str) -> Path:
    """人声轨的落盘位置。与输入文件路径无关，重试才能命中同一个文件。"""
    return Path(out_dir) / f"{stem}{VOCALS_SUFFIX}"


def midi_file(out_dir: str | Path, stem: str) -> Path:
    """MIDI 的落盘位置。"""
    return Path(out_dir) / f"{stem}{MIDI_SUFFIX}"


def csv_file(out_dir: str | Path, stem: str) -> Path:
    """音符表（CSV）的落盘位置。"""
    return Path(out_dir) / f"{stem}{CSV_SUFFIX}"
