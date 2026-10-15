#!/usr/bin/env python
"""vocal2midi 命令行入口 —— 编排：分离 → 转写 → 落盘。

分层结构（各层职责写在各自模块的 docstring 里，改动互不越界）：
    src/separation.py      分离层：Demucs 混音 → 人声 wav
    src/transcription.py   转写层：人声 wav → 音符 → .mid / .csv
    src/main.py            本文件：CLI 参数 + 流程编排 + 版本号
    src/app.py             Gradio UI（只调用下面两个层的接口）

启动：
    python src/main.py "song.mp3"
    python src/main.py --vocals out/stems/song_vocals.wav
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

try:  # python -m src.main
    from .separation import DEMUCS_MODEL, resolve_device, separate_vocals
    from .transcription import F0_MAX, F0_MIN, transcribe_to_midi, write_notes_csv
except ImportError:  # python src/main.py
    from separation import DEMUCS_MODEL, resolve_device, separate_vocals
    from transcription import F0_MAX, F0_MIN, transcribe_to_midi, write_notes_csv

log = logging.getLogger("vocal2midi")

__version__ = "0.1.0"

# 兼容旧的导入路径（曾经的 `from main import separate_vocals` 仍然可用）
__all__ = [
    "DEMUCS_MODEL",
    "F0_MAX",
    "F0_MIN",
    "resolve_device",
    "separate_vocals",
    "transcribe_to_midi",
    "write_notes_csv",
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vocal2midi",
        description="歌曲 → 人声 MIDI：Demucs 分离人声 + basic-pitch 转写",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", nargs="?", help="输入歌曲文件（mp3/wav/flac…）")
    p.add_argument("-o", "--out", default="out", help="输出目录")
    p.add_argument("--vocals", help="已分离好的人声 wav，指定后跳过 Demucs")
    p.add_argument("--model", default=DEMUCS_MODEL, help="Demucs 模型名")
    p.add_argument("--device", default="auto", help="auto / cpu / cuda")
    p.add_argument("--overwrite", action="store_true", help="强制重新分离人声")

    g = p.add_argument_group("basic-pitch 转写参数")
    g.add_argument("--onset", type=float, default=0.5, help="起始点阈值；调高（0.6~0.8）可减少碎音和鬼音")
    g.add_argument("--frame", type=float, default=0.3, help="持续帧阈值；调高音符更短更少")
    g.add_argument("--min-note-len", type=float, default=100.0, help="最短音符时长（毫秒）")
    g.add_argument("--f0-min", type=float, default=F0_MIN, help="限定最低基频 Hz，0 表示不限制")
    g.add_argument("--f0-max", type=float, default=F0_MAX, help="限定最高基频 Hz，0 表示不限制")

    p.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 第一层：人声分离（或直接复用人声 wav）----
    if args.vocals:
        vocals = Path(args.vocals)
        if not vocals.exists():
            log.error("找不到人声文件：%s", vocals)
            return 2
        stem = vocals.stem.replace("_vocals", "")
    else:
        if not args.input:
            log.error("请提供输入歌曲文件，或用 --vocals 指定已分离的人声")
            return 2
        src = Path(args.input)
        if not src.exists():
            log.error("找不到输入文件：%s", src)
            return 2
        stem = src.stem
        try:
            vocals = separate_vocals(
                src,
                out_dir / "stems",
                model=args.model,
                device=resolve_device(args.device),
                overwrite=args.overwrite,
            )
        except (ImportError, KeyError, FileNotFoundError, RuntimeError, ValueError) as exc:
            log.error("人声分离失败：%s", exc)
            return 1

    # ---- 第二层：人声 → MIDI ----
    try:
        midi_path, events = transcribe_to_midi(
            vocals,
            out_dir / f"{stem}_vocals.mid",
            onset_threshold=args.onset,
            frame_threshold=args.frame,
            min_note_length_ms=args.min_note_len,
            f0_min=args.f0_min or None,
            f0_max=args.f0_max or None,
        )
    except ImportError:
        log.error("未安装 basic-pitch：pip install basic-pitch")
        return 1

    if not events:
        log.warning("未检出音符：可调低 --onset/--frame，或检查 --f0-min/--f0-max 是否把人声排除在外")
    write_notes_csv(events, out_dir / f"{stem}_vocals_notes.csv")
    log.info("完成：%s", midi_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
