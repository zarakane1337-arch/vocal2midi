"""人声转 MIDI 层 —— basic-pitch 转写（引擎可替换）。

职责边界（单人一层，改动互不越界）：
    输入：已分离的人声 wav（来自 `separation.py`）
    输出：.mid + 音符 CSV
    对外接口：`transcribe_to_midi()` / `write_notes_csv()` / `F0_MIN` / `F0_MAX`
    调用方：`main.py`（CLI）、`app.py`（UI）

**约定：换转写引擎时保持 `transcribe_to_midi()` 的签名与返回结构不变**
（返回 `(midi_path, events)`，事件为 `(start_s, end_s, pitch_midi, amplitude, [bend…])`），
这样上层 `main.py` / `app.py` 一行都不用改。
例如把 basic-pitch 换成 SwiftF0：只改本文件内部实现，
参考实现在 `docs/swiftf0_main_reference.py`。

单独调试（不经过 UI/CLI）：
    python src/transcription.py out/stems/song_vocals.wav -o out
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path

log = logging.getLogger("vocal2midi.transcription")

F0_MIN = 70.0  # Hz，限定人声基频范围，减少泛音/低频误检
F0_MAX = 900.0


def transcribe_to_midi(
    vocals_path: str | Path,
    out_mid: str | Path,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    min_note_length_ms: float = 100.0,
    f0_min: float | None = F0_MIN,
    f0_max: float | None = F0_MAX,
) -> tuple[Path, list]:
    """basic-pitch 转写人声轨，写出 .mid，返回 (MIDI 路径, 音符事件列表)。"""
    from basic_pitch.inference import predict

    kwargs = {
        "onset_threshold": onset_threshold,
        "frame_threshold": frame_threshold,
        "minimum_note_length": min_note_length_ms,
    }
    log.info(
        "basic-pitch 转写中：onset=%.2f frame=%.2f 最短音符 %.0fms 音域 %.0f-%.0fHz",
        onset_threshold,
        frame_threshold,
        min_note_length_ms,
        f0_min or 0,
        f0_max or 0,
    )
    try:
        _model_output, midi_data, note_events = predict(
            str(vocals_path), minimum_frequency=f0_min, maximum_frequency=f0_max, **kwargs
        )
    except TypeError:
        # 旧版 basic-pitch 没有频率限定参数，去掉后重试
        _model_output, midi_data, note_events = predict(str(vocals_path), **kwargs)

    out_mid = Path(out_mid)
    out_mid.parent.mkdir(parents=True, exist_ok=True)
    midi_data.write(str(out_mid))
    log.info("MIDI 已写出：%s（%d 个音符）", out_mid, len(note_events))
    return out_mid, note_events


def write_notes_csv(note_events: list, out_csv: str | Path) -> Path:
    """音符事件表，便于核对错音（Excel 可直接打开）。"""
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["start_s", "end_s", "dur_s", "pitch_midi", "amplitude", "bend_start"])
        for ev in note_events:
            start, end, pitch, amp = ev[0], ev[1], ev[2], ev[3]
            bends = ev[4] if len(ev) > 4 else []
            writer.writerow(
                [f"{start:.3f}", f"{end:.3f}", f"{end - start:.3f}", pitch, f"{amp:.3f}", bends[0] if bends else 0.0]
            )
    log.info("音符列表已写出：%s", out_csv)
    return out_csv


def main(argv: list[str] | None = None) -> int:
    """分层自测入口：只做转写，不碰分离。"""
    p = argparse.ArgumentParser(prog="transcription", description="人声 → MIDI 转写（分层自测用）")
    p.add_argument("vocals", help="已分离的人声 wav")
    p.add_argument("-o", "--out", default="out", help="MIDI / CSV 输出目录")
    p.add_argument("--onset", type=float, default=0.5, help="起始点阈值")
    p.add_argument("--frame", type=float, default=0.3, help="持续帧阈值")
    p.add_argument("--min-note-len", type=float, default=100.0, help="最短音符时长（毫秒）")
    p.add_argument("--f0-min", type=float, default=F0_MIN, help="最低基频 Hz，0 = 不限制")
    p.add_argument("--f0-max", type=float, default=F0_MAX, help="最高基频 Hz，0 = 不限制")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    vocals = Path(args.vocals)
    if not vocals.exists():
        log.error("找不到人声文件：%s", vocals)
        return 2
    stem = vocals.stem.replace("_vocals", "")
    out_dir = Path(args.out)
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
    except ImportError as exc:
        log.error("未安装 basic-pitch（%s）：pip install basic-pitch", exc)
        return 1
    if not events:
        log.warning("未检出音符：可调低 --onset/--frame，或检查 --f0-min/--f0-max 是否把人声排除在外")
    write_notes_csv(events, out_dir / f"{stem}_vocals_notes.csv")
    print(midi_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
